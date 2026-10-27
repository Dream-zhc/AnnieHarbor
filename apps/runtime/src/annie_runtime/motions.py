"""Her body during a live conversation (2026-10-26).

Idle liveliness belongs to the embedded player. While she is awake and talking:

- listening: once someone has spoken for a moment, one motion from the listening group;
- speaking: the decision model gives each sentence of her reply a gesture or none,
  sent when the speaker's playback is about to reach that sentence, not when its
  text arrives (text runs seconds ahead of audio);
- her own reaction: after a reply that moved her mood, a wordless motion the
  decision model picks for that feeling, once the reply has finished playing.

Every motion goes through ActionCoordinator.perform_now: the same allowlist,
veto, durable reservation, physical cooldown and device receipts as any action.
Nothing here waits on the reply path; a failure only means no motion.
"""

import asyncio
import logging
import time
from uuid import uuid4

from annie_adapters.decision import error_kind
from annie_brain.decision_policy import (
    GENERIC_MOTION,
    INNER_MOTION_CRITERIA,
    INNER_MOTIONS,
    LISTEN_MOTIONS,
    SPEECH_MOTION_CRITERIA,
    SPEECH_MOTIONS,
    explicit_expression,
    explicit_motion,
    inner_motion_question,
    motion_pick,
    sentences,
    speech_motion_question,
)
from annie_brain.persona import chat_preference

log = logging.getLogger(__name__)
SAMPLE_RATE = 24000
LISTEN_DELAY_SECONDS = 0.7  # a cough or a one-word reply gets no motion
LISTEN_COOLDOWN_SECONDS = 8
SPEECH_MOTIONS_PER_REPLY = 3
SPEECH_THRESHOLD = 0.45
INNER_THRESHOLD = 0.5
INNER_COOLDOWN_SECONDS = 90
LEAD_SECONDS = 0.25  # motors need a moment: start a little before the words
DEVICE_PREBUFFER_SECONDS = 0.18  # room-speaker buffers this much before playing
CHARS_PER_SECOND = 4.3  # fallback pace of Chinese TTS when no alignment is in yet
PLAYBACK_WAIT_SECONDS = 25
POLL_SECONDS = 0.05
MAX_TASKS = 24


def target_sample(speech, char_index):
    """Where in the turn's audio a character starts: aligned if possible, else estimated."""
    points = getattr(speech, "alignment_points", {}) or {}
    if char_index in points:
        return points[char_index]["start_sample"]
    before = [index for index in points if index <= char_index]
    if before:
        anchor = max(before)
        return points[anchor]["start_sample"] + int(
            (char_index - anchor) * SAMPLE_RATE / CHARS_PER_SECOND)
    return int(char_index * SAMPLE_RATE / CHARS_PER_SECOND)


def played_sample(speech, now=None):
    """How far the robot's speaker has played this turn, from receipts or the clock."""
    track = (getattr(speech, "receipt_tracks", {}) or {}).get("speaker")
    if track is not None and track.get("state") not in {"stopped", "drained"}:
        return speech.played_samples
    marks = getattr(speech, "pcm_marks", None)
    if not marks:
        return None
    elapsed = (time.monotonic() if now is None else now) - marks[0][1] - DEVICE_PREBUFFER_SECONDS
    return max(0, min(int(elapsed * SAMPLE_RATE), speech.samples))


class InteractionMotions:
    def __init__(self, runtime, *, listening=True, speaking=True, reactions=True,
                 per_reply=SPEECH_MOTIONS_PER_REPLY):
        self.runtime = runtime
        self.listening = listening
        self.speaking = speaking
        self.reactions = reactions
        self.per_reply = per_reply
        robots = tuple(runtime.robots)
        self.replies = {}  # robot -> {"turn_id", "seen", "planned"}
        self.last_listen = dict.fromkeys(robots, -1e9)
        self.last_listen_script = dict.fromkeys(robots)
        self.last_inner = dict.fromkeys(robots, -1e9)
        self.recent = {robot_id: [] for robot_id in robots}  # last decisions, for the UI
        self.stats = {robot_id: {"listen": 0, "speech": 0, "inner": 0, "skipped": 0,
                                 "decision_failures": 0} for robot_id in robots}
        self.tasks = set()

    # ---- gates ------------------------------------------------------------------------
    def _actions(self):
        actions = getattr(self.runtime, "actions", None)
        return actions if actions is not None and actions.enabled else None

    def _allowed(self, group):
        actions = self._actions()
        return [script for script in group if actions and script in actions.allowlist]

    @staticmethod
    def _awake(client):
        wake = getattr(client, "wake", None)
        return wake is None or wake.state != "SLEEP"

    @staticmethod
    def _wanted(client):
        """The user can turn her spontaneous motions off for this Chat."""
        return chat_preference(getattr(client, "social_state", {}) or {},
                               "spontaneous_motions") != "off"

    def _record(self, robot_id, kind, script, **fields):
        rows = self.recent[robot_id]
        actions = getattr(self.runtime, "actions", None)
        name = (actions.catalog.get(script) or {}).get("name") if actions and script else None
        rows.append({"kind": kind, "script": script, "name": name,
                     "ts_ms": time.time_ns() // 1_000_000, **fields})
        del rows[:-12]

    def snapshot(self, robot_id):
        return {"actions_enabled": self._actions() is not None,
                "listening": self.listening, "speaking": self.speaking,
                "reactions": self.reactions, "per_reply": self.per_reply,
                "groups": {"listen": self._allowed(LISTEN_MOTIONS),
                           "speech": self._allowed(SPEECH_MOTIONS),
                           "inner": self._allowed(INNER_MOTIONS)},
                "recent": list(self.recent[robot_id]), "stats": dict(self.stats[robot_id])}

    # ---- listening --------------------------------------------------------------------
    def on_speech_started(self, robot_id, client):
        """Someone started talking to her: look like she is listening."""
        if not self.listening or not self._allowed(LISTEN_MOTIONS):
            return
        if not self._awake(client) or not self._wanted(client):
            return
        speech = getattr(client, "speech", None)
        if speech is not None and speech.output_active():
            return  # she is talking: this is a barge-in, not a listener's moment
        if time.monotonic() - self.last_listen[robot_id] < LISTEN_COOLDOWN_SECONDS:
            return
        self.last_listen[robot_id] = time.monotonic()
        self._spawn(self._listen(robot_id, client, client.robot.generation))

    async def _listen(self, robot_id, client, generation):
        await asyncio.sleep(LISTEN_DELAY_SECONDS)
        if not client.speech_active or client.robot.generation != generation:
            self.stats[robot_id]["skipped"] += 1
            return
        group = self._allowed(LISTEN_MOTIONS)
        # Mostly the plain listening tilt, never the same one twice in a row.
        last = self.last_listen_script[robot_id]
        script = next((item for item in group if item != last), group[0])
        self.last_listen_script[robot_id] = script
        await self._perform(robot_id, "listen", script, f"listen-{uuid4().hex[:12]}",
                            generation)

    # ---- speaking ---------------------------------------------------------------------
    def on_reply_text(self, robot_id, client):
        """Her reply grew: each newly finished sentence may get a gesture."""
        if not self.speaking or not self._allowed(SPEECH_MOTIONS):
            return
        speech = getattr(client, "speech", None)
        turn_id = client.turn_id
        if (speech is None or not turn_id or speech.turn_id != turn_id
                or getattr(client, "_external_text_only", False)):
            return
        if not self._awake(client) or not self._wanted(client):
            return
        reply = self.replies.get(robot_id)
        if reply is None or reply["turn_id"] != turn_id:
            reply = self.replies[robot_id] = {"turn_id": turn_id, "seen": 0, "planned": 0}
        text = speech.generated_text
        spans = sentences(text)
        cached = client.robot.turn_cache.get(turn_id) or {}
        user_text = str(cached.get("user_text") or client.transcript or "")
        if not reply["seen"] and (
                explicit_motion(user_text, self.runtime.actions.allowlist)
                or explicit_expression(user_text) or GENERIC_MOTION.search(user_text)):
            # The user named the motion: that one runs (auto-expression path);
            # a gesture of her own on “行，我摇头了” would contradict it (d3 replay 10-27).
            reply["seen"] = reply["planned"] = 1 << 30
            return
        for start, end in spans[reply["seen"]:]:
            reply["seen"] += 1
            if reply["planned"] >= self.per_reply:
                break
            reply["planned"] += 1
            self._spawn(self._gesture(robot_id, client, speech, turn_id, speech.stream_epoch,
                                      client.robot.generation, user_text,
                                      text[start:end].strip(), start))

    async def _gesture(self, robot_id, client, speech, turn_id, epoch, generation, user_text,
                       sentence, char_index):
        allowed = self._allowed(SPEECH_MOTIONS)
        descriptions = {script: SPEECH_MOTION_CRITERIA[script] for script in allowed}
        inner = getattr(self.runtime, "inner_life", None)
        state = {"用户": user_text[:200], "小安妮这句话": sentence[:120]}
        if inner is not None:
            mood = inner.idle_material(robot_id, public=False)["mood"]
            if mood:
                state["小安妮此刻的心情"] = mood
        answer = await self._decide(robot_id, client, state, speech_motion_question(descriptions))
        script = motion_pick(answer, allowed, threshold=SPEECH_THRESHOLD)
        if script is None:
            self._record(robot_id, "speech", None, sentence=sentence[:40],
                         choice=(answer or {}).get("choice"))
            return
        if not await self._wait_for_words(speech, turn_id, epoch, char_index):
            self.stats[robot_id]["skipped"] += 1
            self._record(robot_id, "speech", script, sentence=sentence[:40], status="too_late")
            return
        await self._perform(robot_id, "speech", script, f"say-{turn_id[:16]}-{char_index}",
                            generation, conversation_id=turn_id, user_text=user_text or None,
                            sentence=sentence[:40])

    async def _wait_for_words(self, speech, turn_id, epoch, char_index):
        """Until the speaker is about to play the sentence; False once it is gone."""
        deadline = time.monotonic() + PLAYBACK_WAIT_SECONDS
        lead = int(LEAD_SECONDS * SAMPLE_RATE)
        while time.monotonic() < deadline:
            track = (speech.receipt_tracks or {}).get("speaker")
            if (speech.turn_id != turn_id or speech.stream_epoch != epoch or speech.cancelled
                    or (track is not None and track.get("state") in {"stopped", "drained"})):
                return False
            played = played_sample(speech)
            if played is not None and played >= target_sample(speech, char_index) - lead:
                return True
            await asyncio.sleep(POLL_SECONDS)
        return False

    # ---- her own reaction -------------------------------------------------------------
    def on_feeling(self, robot_id, tone, reason):
        """The last exchange moved her mood: maybe a wordless motion once she has spoken."""
        if not self.reactions or not self._allowed(INNER_MOTIONS) or not reason:
            return
        client = self.runtime.realtime.get(robot_id)
        if client is None or not self._awake(client) or not self._wanted(client):
            return
        if time.monotonic() - self.last_inner[robot_id] < INNER_COOLDOWN_SECONDS:
            return
        self.last_inner[robot_id] = time.monotonic()
        self._spawn(self._react(robot_id, client, client.robot.generation, tone, reason))

    async def _react(self, robot_id, client, generation, tone, reason):
        deadline = time.monotonic() + PLAYBACK_WAIT_SECONDS
        speech = client.speech
        while speech is not None and speech.output_active() and time.monotonic() < deadline:
            await asyncio.sleep(0.2)  # her reply finishes first
        if (client.robot.generation != generation or client.speech_active or client.turn_id
                or not self._awake(client)):
            self.stats[robot_id]["skipped"] += 1
            return
        allowed = self._allowed(INNER_MOTIONS)
        descriptions = {script: INNER_MOTION_CRITERIA[script] for script in allowed}
        inner = getattr(self.runtime, "inner_life", None)
        mood = inner.idle_material(robot_id, public=False)["mood"] if inner else ""
        answer = await self._decide(robot_id, client, {"小安妮的感受": reason, "心情": mood},
                                    inner_motion_question(descriptions))
        script = motion_pick(answer, allowed, threshold=INNER_THRESHOLD)
        if script is None:
            self._record(robot_id, "inner", None, reason=reason, choice=(answer or {}).get("choice"))
            return
        await self._perform(robot_id, "inner", script, f"inner-{uuid4().hex[:12]}", generation,
                            reason=reason)

    # ---- plumbing ---------------------------------------------------------------------
    async def _decide(self, robot_id, client, state, question):
        ask = getattr(client, "ask_decision", None)
        if ask is None:
            return None
        try:
            answers, _model = await asyncio.wait_for(ask(state, {"motion": question}), 3)
        except asyncio.CancelledError:
            raise
        except LookupError:
            return None
        except Exception as exc:
            self.stats[robot_id]["decision_failures"] += 1
            log.info("motion decision failed robot=%s error=%s", robot_id, error_kind(exc))
            return None
        return answers.get("motion") if isinstance(answers, dict) else None

    async def _perform(self, robot_id, kind, script, call_id, generation, *,
                       conversation_id=None, user_text=None, **fields):
        actions = self._actions()
        if actions is None:
            return
        try:
            result = await actions.perform_now(robot_id, call_id, conversation_id or call_id,
                                               generation, script, user_text=user_text)
        except ValueError as exc:
            result = {"status": str(exc)}
        status = result.get("status")
        if status in {"published_unknown", "send_unknown"}:
            self.stats[robot_id][kind] += 1
        else:
            self.stats[robot_id]["skipped"] += 1
        self._record(robot_id, kind, script, status=status, **fields)

    def _spawn(self, coroutine):
        if len(self.tasks) >= MAX_TASKS:
            coroutine.close()
            return
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def close(self):
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
