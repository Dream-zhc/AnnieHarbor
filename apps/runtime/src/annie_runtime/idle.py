"""Phase 8C: optional per-robot idle arbitration; no extra call in chat hot path."""

import asyncio
import re
import time
from collections import deque

PERSON_KINDS = {"person", "human", "人物", "人"}
PUBLIC_KINDS = {"cup", "book", "cat", "dog", "plant", "tea", "杯子", "书", "猫", "狗", "植物", "茶"}
IDENTITY = ("robot_id", "chat_id", "persona_id", "session_epoch", "generation")
TOPIC_MEMORY_SECONDS = 2 * 3600  # a proactive line is not said again within this
GROUNDED_ONLY = "不说画面和对话里没有的事实，尤其不凭空说天气、气温或冷热。"


def _topic_key(text):
    return re.sub(r"\W", "", text or "")


def _similar(first, second):
    """Same idea in other words: containment or most character pairs shared."""
    if not first or not second:
        return False
    if first in second or second in first:
        return True
    pairs = lambda text: {text[i:i + 2] for i in range(len(text) - 1)}  # noqa: E731
    a, b = pairs(first), pairs(second)
    return bool(a and b) and len(a & b) / min(len(a), len(b)) >= .6


class SpokenTopics:
    """What Annie said on her own recently, kept across user turns, per robot."""

    def __init__(self, robots, *, seconds=TOPIC_MEMORY_SECONDS, clock=time.monotonic):
        self.seconds, self.clock = seconds, clock
        self.items = {robot_id: deque(maxlen=8) for robot_id in robots}

    def _fresh(self, robot_id):
        now = self.clock()
        return [topic for at, topic in self.items[robot_id] if now - at <= self.seconds]

    def repeated(self, robot_id, topic):
        key = _topic_key(topic)
        return any(_similar(key, _topic_key(old)) for old in self._fresh(robot_id))

    def latest(self, robot_id):
        fresh = self._fresh(robot_id)
        return fresh[-1] if fresh else ""

    def add(self, robot_id, topic):
        self.items[robot_id].append((self.clock(), topic))


class ShortContinuation:
    """One private, consented follow-up per freshly delivered user reply."""

    def __init__(self, runtime, graph, *, enabled=False, delay=15, window=35,
                 timeout=12, spoken=None):
        self.runtime = runtime
        self.spoken = spoken or SpokenTopics(runtime.robots)
        self.graph = graph if enabled else None
        self.delay = delay
        self.window = window
        self.timeout = timeout
        self.rows = {rid: self._blank() for rid in runtime.robots}
        self.tasks = {}
        self.audit = {rid: deque(maxlen=32) for rid in runtime.robots}

    def _record(self, robot_id, *, topic="", result_turn_id=None):
        row = self.rows[robot_id]
        if row["turn_id"] is None or row["identity"] is None:
            return
        self.audit[robot_id].append({
            "mode": "short_continuation", "source_turn_id": row["turn_id"],
            "result_turn_id": result_turn_id,
            "identity": dict(row["identity"]), "channel": row["channel"],
            "status": row["status"], "reason": row["reason"],
            "decision": row["decision"], "topic": str(topic)[:100],
            "model_ms": row["model_ms"], "ts_ms": time.time_ns() // 1_000_000,
        })

    @staticmethod
    def _blank():
        return {"turn_id": None, "identity": None, "user_at": None,
                "drained_at": None, "user_text": "", "assistant_text": "",
                "channel": None, "visible_at": None,
                "attempted": False, "status": "waiting", "reason": "waiting_speaker",
                "decision": None, "model_ms": None}

    def snapshot(self, robot_id):
        row = self.rows[robot_id]
        return {"enabled": self.graph is not None,
                "delay_seconds": self.delay, "status": row["status"],
                "reason": row["reason"], "decision": row["decision"],
                "turn_id": row["turn_id"], "model_ms": row["model_ms"],
                "channel": row["channel"],
                "recent": [dict(item) for item in self.audit[robot_id]
                           if item["identity"] == self.runtime.session_state(robot_id)]}

    def viewed_text(self, robot_id, turn_id, identity):
        """Visible workbench heartbeat; never pretend a browser is a speaker."""
        client = self.runtime.realtime.get(robot_id)
        if self.graph is None or client is None or not client.ready:
            return False
        if any(self.runtime.session_state(robot_id).get(k) != identity.get(k)
               for k in IDENTITY):
            return False
        turn = client.robot.turn_cache.get(turn_id)
        timeline = client._turn_timelines.get(turn_id, {})
        if (not turn or turn.get("turn_source") != "user" or
                turn.get("status") != "completed" or
                not turn.get("assistant_generated_text") or
                timeline.get("input_mode") != "text" or
                getattr(client, "_last_user_turn_id", None) != turn_id or
                (time.time_ns() // 1_000_000 -
                 turn.get("updated_ms", 0)) > 60000):
            return False
        row = self.rows[robot_id]
        if row["turn_id"] == turn_id:
            if row["channel"] in {"text", "browser_test"}:
                row["visible_at"] = time.monotonic()
            return True
        row.update(self._blank())
        row.update(turn_id=turn_id, channel="text",
                   identity=dict(self.runtime.session_state(robot_id)),
                   user_at=client._last_user_turn_at,
                   drained_at=time.monotonic(), visible_at=time.monotonic(),
                   user_text=str(turn["user_text"])[:180],
                   assistant_text=str(turn["assistant_generated_text"])[:180],
                   reason="cooldown")
        return True

    def on_event(self, robot_id, event):
        row = self.rows[robot_id]
        if (event["kind"] == "turn.started" and
                event.get("turn_source") == "external" and
                row["status"] == "submitting"):
            # Our own accepted external turn is the intended continuation.
            return
        if event["kind"] in {"speech.started", "turn.started", "session.replaced"}:
            # Even speech without a final ASR must cancel an in-flight plan.
            row.update(self._blank())
            return
        if (event["kind"] != "tts.playback.drained"
                or event.get("source") not in {"speaker", "browser_test"}):
            return
        client = self.runtime.realtime.get(robot_id)
        if client is None or self.graph is None:
            return
        turn_id = event.get("turn_id")
        turn = client.robot.turn_cache.get(turn_id)
        if (not turn or turn.get("turn_source") != "user" or
                turn.get("status") != "completed" or
                any(event.get(k) != self.runtime.session_state(robot_id).get(k)
                    for k in IDENTITY)):
            return
        source = event.get("source")
        if source == "browser_test" and (
                client._turn_timelines.get(turn_id, {}).get("input_mode") != "text"
                or getattr(client, "_last_user_turn_id", None) != turn_id):
            return  # browser audio is opt-in output, never a physical receipt
        if row["turn_id"] == turn_id and row["identity"] == self.runtime.session_state(robot_id):
            if source == "browser_test" and row["channel"] == "text" and not row["attempted"]:
                row.update(channel="browser_test", drained_at=time.monotonic())
            return  # duplicates do not reset delay or re-arm an attempted turn
        user = str(turn.get("user_text") or "").strip()
        assistant = str(turn.get("assistant_generated_text") or "").strip()
        if not user or not assistant:
            return
        row.update(self._blank())
        row.update(turn_id=turn_id, channel=source,
                   visible_at=time.monotonic() if source == "browser_test" else None,
                   identity=dict(self.runtime.session_state(robot_id)),
                   user_at=client._last_user_turn_at, drained_at=time.monotonic(),
                   user_text=user[:180], assistant_text=assistant[:180],
                   reason="cooldown")

    def _eligible(self, robot_id, now):
        row = self.rows[robot_id]
        if self.graph is None:
            return "disabled"
        if row["turn_id"] is None:
            return "waiting_speaker"
        client = self.runtime.realtime.get(robot_id)
        if not client or not client.ready:
            return "realtime_unavailable"
        if row["channel"] in {"speaker", "browser_test"} and client.speech is None:
            return "tts_unavailable"
        if (row["identity"] != self.runtime.session_state(robot_id)
                or row["user_at"] != client._last_user_turn_at):
            return "new_user_or_session"
        pressure = (client.persona_snapshot or {}).get("question_pressure")
        if pressure != "active":
            return "not_opted_in"
        if row["attempted"]:
            return "already_attempted"
        if (len(row["user_text"]) < 7 or
                any(word in row["user_text"] for word in
                    ("退下", "闭嘴", "安静", "别说", "不想聊", "不用回答", "停止"))):
            return "no_open_topic"
        if row["channel"] in {"text", "browser_test"} and (
                row["visible_at"] is None or now - row["visible_at"] > 8):
            return "web_not_visible"
        if getattr(client, "dialogue", None) and (
                client.dialogue.snapshot()["followup"] == "avoid"):
            return "dialogue_declined"
        if (client.speech_active or client.turn_id or client.response_id or
                self.runtime.reminders._speech_blocks(client.speech)):
            return "conversation_busy"
        if self.runtime.robots[robot_id].coordinator.playback_state in {"playing", "unknown"}:
            return "playback_active"
        events = getattr(self.runtime, "events", None)
        if events is not None and events.pending.get(robot_id):
            return "external_event_pending"
        elapsed = now - row["drained_at"]
        if elapsed > self.window:
            return "window_expired"
        if elapsed < self.delay:
            return "cooldown"
        return None

    async def tick(self):
        if self.graph is None or not self.runtime.started:
            return
        now = time.monotonic()
        for robot_id, row in self.rows.items():
            if (task := self.tasks.get(robot_id)) and not task.done():
                continue
            reason = self._eligible(robot_id, now)
            if reason:
                if row["status"] not in {"submitted_unknown", "model_no_speak"}:
                    row.update(status="suppressed", reason=reason)
                continue
            row.update(status="planning", reason=None)
            self.tasks[robot_id] = asyncio.create_task(
                self._plan(robot_id), name=f"short-continuation:{robot_id}")

    async def _plan(self, robot_id):
        row = self.rows[robot_id]
        turn_id, identity, user_at = row["turn_id"], dict(row["identity"]), row["user_at"]
        client = self.runtime.realtime[robot_id]
        speech_at = getattr(client, "_last_provider_speech_started_t", None)
        started = time.monotonic()
        try:
            recent_plan = ""
            # An unattended speaker cannot disclose cross-Chat plans.
            # Only explicit topic-seeking in a visible private text turn opts in.
            if (row["channel"] == "text" and
                    re.search(r"无聊|聊什么|聊点什么|聊点啥|不知道聊|找个话题|随便聊聊",
                              row["user_text"]) and
                    self.runtime.memory is not None):
                facts = await self.runtime.memory.list(
                    robot_id, identity["persona_id"])
                plans = [item for item in facts
                         if item.get("kind") == "episodes" and
                         item.get("fact_key") == "ongoing_plan_car_viewing" and
                         item.get("source_valid", True) and
                         item.get("source_created_ms", 0) >=
                         time.time_ns() // 1_000_000 - 30 * 86_400_000 and
                         "进展尚未确认" in item["text"]]
                if plans:
                    recent_plan = plans[0]["text"][:100]
            inner = getattr(self.runtime, "inner_life", None)
            result = await asyncio.wait_for(self.graph.ainvoke({
                "mode": "continuation", "user_text": row["user_text"],
                "assistant_text": row["assistant_text"],
                "recent_plan": recent_plan,
                "mood": inner.idle_material(robot_id, public=False)["mood"] if inner else "",
            }), self.timeout)
            elapsed_ms = round((time.monotonic() - started) * 1000)
            if (row["turn_id"] != turn_id or row["identity"] != identity or
                    row["user_at"] != user_at):
                return
            row["model_ms"] = elapsed_ms
            row["decision"] = result["decision"]
            reason = self._eligible(robot_id, time.monotonic())
            if reason or getattr(client, "_last_provider_speech_started_t", None) != speech_at:
                row.update(status="suppressed", reason=reason or "speech_preempted",
                           attempted=True)
                self._record(robot_id)
                return
            row["attempted"] = True
            if result["decision"] != "SPEAK" or not result["topic"].strip():
                row.update(status="model_no_speak", reason="model_no_speak")
                self._record(robot_id, topic=result.get("topic", ""))
                return
            topic = result["topic"].strip()
            if self.spoken.repeated(robot_id, topic):
                row.update(status="model_no_speak", reason="repeated_topic")
                self._record(robot_id, topic=topic)
                return
            row.update(status="submitting", reason=None)
            self.spoken.add(robot_id, topic)
            submitted = await client.submit_text(
                "【内部短时续聊规划，非用户发言】仅在刚刚的话题上自然补一句新观察或一个"
                "不催促的简短问题；如果刚才已经问过，不要催促回答，也不要再重复提问。"
                "不重复上句，不执行旧任务，不调用工具。" + GROUNDED_ONLY + "续聊方向：" + topic,
                source="external", expected_identity=identity,
                text_only=row["channel"] == "text")
            if (row["turn_id"] != turn_id or row["identity"] != identity or
                    getattr(client, "_last_provider_speech_started_t", None) != speech_at):
                return
            row.update(status="submitted_unknown", reason=None)
            self._record(robot_id, topic=topic,
                         result_turn_id=submitted.get("turn_id") if isinstance(submitted, dict) else None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if row["turn_id"] == turn_id and row["identity"] == identity:
                row.update(status="suppressed", reason=type(exc).__name__,
                           attempted=True, model_ms=round((time.monotonic()-started)*1000))
                self._record(robot_id)

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()


class IdleService:
    def __init__(self, runtime, model=None, *, idle_seconds=60, second_seconds=120,
                 presence_seconds=1, presence_ttl=10, timeout=12,
                 continuation_enabled=False, continuation_seconds=15):
        self.runtime = runtime
        self.model = model
        self.graph = None
        if model is not None:
            from annie_brain.idle_plan import build_idle_graph

            self.graph = build_idle_graph(model)
        self.spoken = SpokenTopics(runtime.robots)
        self.short = ShortContinuation(
            runtime, self.graph, enabled=continuation_enabled,
            delay=continuation_seconds, timeout=timeout, spoken=self.spoken,
        )
        self.idle_seconds = idle_seconds
        self.second_seconds = second_seconds
        self.presence_seconds = presence_seconds
        self.presence_ttl = presence_ttl
        self.timeout = timeout
        self.rows = {robot_id: self._new_row() for robot_id in runtime.robots}
        self.audit = {robot_id: deque(maxlen=32) for robot_id in runtime.robots}
        self.worker = None
        self.tasks = {}

    @staticmethod
    def _new_row():
        return {"identity": None, "last_user": None, "attempts": 0,
                "previous": "", "last_spoke": None, "presence_at": None,
                "presence_since": None, "presence_samples": 0,
                "scene": "", "scene_since": None, "scene_samples": 0,
                "status": "idle", "reason": "waiting_user_turn",
                "first_scene": "", "turn_id": None,
                "speaker_started": False, "speaker_drained": False,
                "speaker_drained_at": None,
                "speaker_interrupted": False,
                "presence_subject_id": None,
                "decision": None, "last_model_decision": None,
                "last_topic": "", "model_ms": None, "updated_ms": 0}

    def snapshot(self, robot_id):
        row = self.rows[robot_id]
        return {**self.runtime.session_state(robot_id),
                "continuation": self.short.snapshot(robot_id),
                "recent": [dict(item) for item in self.audit[robot_id]
                           if item["identity"] == self.runtime.session_state(robot_id)],
                "enabled": self.graph is not None,
                "model": getattr(self.model, "model", None),
                "thinking_enabled": getattr(self.model, "enable_thinking", False),
                "graph_ready": self.graph is not None,
                "status": row["status"], "reason": row["reason"],
                "decision": row["decision"], "attempts": row["attempts"],
                "last_model_decision": row["last_model_decision"],
                "last_topic": row["last_topic"],
                "turn_id": row["turn_id"],
                "speaker_started": row["speaker_started"],
                "speaker_drained": row["speaker_drained"],
                "model_ms": row["model_ms"],
                "presence_samples": row["presence_samples"],
                "presence_age_ms": (None if row["presence_at"] is None else
                    max(0, round((time.monotonic() - row["presence_at"]) * 1000))),
                "updated_ms": row["updated_ms"]}

    def _record(self, robot_id, source_turn_id, *, topic="", result_turn_id=None):
        row = self.rows[robot_id]
        self.audit[robot_id].append({
            "mode": "idle_scene", "source_turn_id": source_turn_id,
            "result_turn_id": result_turn_id,
            "identity": dict(self.runtime.session_state(robot_id)),
            "status": row["status"], "reason": row["reason"],
            "decision": row["decision"], "topic": str(topic)[:100],
            "model_ms": row["model_ms"], "ts_ms": time.time_ns() // 1_000_000,
        })

    def on_event(self, robot_id, event):
        """Speaker evidence only; browser playback and model completion are not receipts."""
        self.short.on_event(robot_id, event)
        row = self.rows[robot_id]
        if (event["kind"] == "speech.started" and row["identity"] is not None
                and all(row["identity"].get(k) == event.get(k) for k in IDENTITY)):
            row["attempts"] = 2
            self._status(robot_id, "suppressed", "speech_preempted")
            return
        if (not row["turn_id"] or event.get("turn_id") != row["turn_id"]
                or event.get("source") != "speaker"
                or any(row["identity"].get(k) != event.get(k) for k in IDENTITY)):
            return
        if event["kind"] == "tts.playback.started":
            row["speaker_started"] = True
        elif event["kind"] == "tts.playback.drained" and row["speaker_started"]:
            row["speaker_drained"] = True
            row["speaker_drained_at"] = time.monotonic()
        elif event["kind"] == "tts.playback.stopped":
            row["speaker_interrupted"] = True

    def observe_visual(self, robot_id, message):
        """Only explicit v2 person subjects count; a caption is NOT presence proof."""
        row = self.rows[robot_id]
        facts = message.get("facts")
        subjects = facts.get("subjects") if isinstance(facts, dict) else None
        if not isinstance(subjects, list):
            row.update(presence_since=None, presence_at=None, presence_samples=0,
                       presence_subject_id=None, scene="", scene_since=None,
                       scene_samples=0)
            return
        valid = [item for item in subjects if isinstance(item, dict)
                 and item.get("kind") in PERSON_KINDS
                 and isinstance(item.get("id"), str)
                 and 1 <= len(item["id"].strip()) <= 128
                 and type(item.get("confidence")) in (float, int)
                 and .8 <= item["confidence"] <= 1]
        if len(valid) != 1 or sum(
            isinstance(item, dict) and item.get("kind") in PERSON_KINDS
            for item in subjects
        ) != 1:
            row.update(presence_since=None, presence_at=None, presence_samples=0,
                       presence_subject_id=None, scene="", scene_since=None,
                       scene_samples=0)
            return
        now = time.monotonic()
        subject_id = valid[0].get("id")
        if (row["presence_at"] is None or now - row["presence_at"] > self.presence_ttl
                or subject_id != row["presence_subject_id"]):
            row["presence_since"] = now
            row["presence_samples"] = 0
        row["presence_subject_id"] = subject_id
        row["presence_samples"] += 1
        row["presence_at"] = now
        kinds = [item.get("kind") for item in subjects if isinstance(item, dict)
                 and item.get("kind") in PUBLIC_KINDS]
        scene = "现场有人。可见物品：" + "、".join(kinds[:5]) if kinds else "现场有人。"
        if scene != row["scene"]:
            row["scene_since"] = now
            row["scene_samples"] = 0
        row["scene"] = scene
        row["scene_samples"] += 1

    def _status(self, robot_id, status, reason=None, decision=None):
        row = self.rows[robot_id]
        if (row["status"], row["reason"], row["decision"]) == (status, reason, decision):
            return
        row.update(status=status, reason=reason, decision=decision,
                   updated_ms=time.time_ns() // 1_000_000)
        client = self.runtime.realtime.get(robot_id)
        if client is not None:
            client.publish("idle.policy", status=status, reason=reason,
                           decision=decision, attempts=row["attempts"])

    def _eligible(self, robot_id, now):
        row = self.rows[robot_id]
        client = self.runtime.realtime.get(robot_id)
        if client is None or not client.ready:
            return "realtime_unavailable"
        identity = self.runtime.session_state(robot_id)
        if row["identity"] != identity or row["last_user"] != client._last_user_turn_at:
            same_identity = row["identity"] == identity
            previous_presence = (row["presence_at"], row["presence_since"],
                                 row["presence_samples"], row["scene"],
                                 row["presence_subject_id"], row["scene_since"],
                                 row["scene_samples"])
            row.update(self._new_row())
            row["identity"], row["last_user"] = dict(identity), client._last_user_turn_at
            if same_identity:
                (row["presence_at"], row["presence_since"],
                 row["presence_samples"], row["scene"],
                 row["presence_subject_id"], row["scene_since"],
                 row["scene_samples"]) = previous_presence
        if client._last_user_turn_at is None:
            return "waiting_user_turn"
        if getattr(client, "_last_user_turn_identity", identity) != identity:
            return "waiting_user_turn"
        if client.speech is None:
            return "tts_unavailable"
        if row["attempts"] >= 2:
            return "max_attempts"
        if row["attempts"] == 1:
            if row["speaker_interrupted"]:
                row["attempts"] = 2
                return "speaker_interrupted"
            if not row["speaker_started"] or not row["speaker_drained"]:
                return "await_speaker_receipt"
        if not self.runtime._current_visual(robot_id):
            return "visual_missing_or_disconnected"
        if (row["presence_at"] is None or now - row["presence_at"] > self.presence_ttl
                or row["presence_since"] is None or
                row["presence_at"] - row["presence_since"] < self.presence_seconds or
                row["presence_samples"] < 2):
            return "person_unconfirmed"
        if (client.speech_active or client.turn_id or client.response_id or
                self.runtime.reminders._speech_blocks(client.speech)):
            return "conversation_busy"
        if self.runtime.robots[robot_id].coordinator.playback_state in {"playing", "unknown"}:
            return "playback_active"
        event_service = getattr(self.runtime, "events", None)
        if event_service is not None and event_service.pending.get(robot_id):
            return "external_event_pending"
        idle_at = max(client._last_user_turn_at, client._last_conversation_activity_t)
        if now - idle_at < self.idle_seconds:
            return "idle_wait"
        if (row["attempts"] == 1 and row["speaker_drained_at"] is not None
                and now - row["speaker_drained_at"] < self.second_seconds):
            return "second_cooldown"
        if (row["attempts"] == 1 and
                (row["scene"] == row["first_scene"] or
                 row["scene_samples"] < 2 or row["scene_since"] is None or
                 row["presence_at"] - row["scene_since"] < self.presence_seconds)):
            return "second_no_new_evidence"
        inner = getattr(self.runtime, "inner_life", None)
        if inner is not None:
            # One hourly budget for every unprompted remark (user decision 2026-10-26).
            return inner.proactive_block(robot_id)
        return None

    async def tick(self):
        await self.short.tick()
        if self.graph is None or not self.runtime.started:
            return
        now = time.monotonic()
        for robot_id in self.rows:
            if robot_id not in self.runtime.realtime:
                continue
            if (task := self.tasks.get(robot_id)) is not None and not task.done():
                continue
            reason = self._eligible(robot_id, now)
            if reason:
                if reason == "max_attempts" and self.rows[robot_id]["status"] in {
                    "submitted_unknown", "suppressed",
                }:
                    continue
                self._status(robot_id, "suppressed", reason)
            else:
                self._status(robot_id, "planning")
                self.tasks[robot_id] = asyncio.create_task(
                    self._plan(robot_id), name=f"idle-plan:{robot_id}")

    async def _plan(self, robot_id):
        row = self.rows[robot_id]
        identity, last_user, attempt = (dict(row["identity"]), row["last_user"],
                                         row["attempts"] + 1)
        speech_at = getattr(self.runtime.realtime[robot_id], "_last_provider_speech_started_t", None)
        source_turn_id = getattr(self.runtime.realtime[robot_id], "_last_user_turn_id", None)
        try:
            model_started_at = time.monotonic()
            # The row is reset when the user talks; what was said before is not.
            previous_topic = row["previous"] or self.spoken.latest(robot_id)
            # Her own mood and bodily day give her something of her own to say;
            # the person in view is unidentified, so no names and no talk.
            inner = getattr(self.runtime, "inner_life", None)
            material = (inner.idle_material(robot_id, public=True) if inner
                        else {"mood": "", "own_day": ""})
            result = await asyncio.wait_for(self.graph.ainvoke({
                "attempt": attempt, "scene": row["scene"], "previous": previous_topic,
                **material,
            }), self.timeout)
            elapsed_ms = round((time.monotonic() - model_started_at) * 1000)
            reason = self._eligible(robot_id, time.monotonic())
            if row["identity"] != identity or row["last_user"] != last_user:
                return  # the new session owns its diagnostics; never rewrite it
            if reason == "max_attempts" and row["reason"] == "speech_preempted":
                return
            if (reason or getattr(self.runtime.realtime[robot_id],
                                  "_last_provider_speech_started_t", None) != speech_at):
                self._status(robot_id, "suppressed", reason or "speech_preempted")
                row["model_ms"] = elapsed_ms
                row["last_model_decision"] = result["decision"]
                self._record(robot_id, source_turn_id)
                return
            row["model_ms"] = elapsed_ms
            row["last_model_decision"] = result["decision"]
            if result["decision"] == "NO_SPEAK":
                row["attempts"] = 2  # Do not poll the model again for the same silence.
                self._status(robot_id, "suppressed", "model_no_speak", "NO_SPEAK")
                self._record(robot_id, source_turn_id)
                return
            topic = result["topic"].strip()
            normalized = re.sub(r"\W", "", topic)
            previous = re.sub(r"\W", "", row["previous"])
            if (not topic or attempt == 2 and result.get("new_value") is not True
                    or previous and (normalized in previous or previous in normalized)
                    or self.spoken.repeated(robot_id, topic)):
                row["attempts"] = 2
                self._status(robot_id, "suppressed", "repeated_or_empty", "NO_SPEAK")
                self._record(robot_id, source_turn_id)
                return
            # Do not include user history or memory: the visible person is not identified.
            client = self.runtime.realtime[robot_id]
            self._status(robot_id, "submitting", decision="SPEAK")
            self.spoken.add(robot_id, topic)
            result = await client.submit_text(
                "【内部主动交谈规划，非用户发言】现场有身份未知的人。"
                + ("基于公共场景或你自己今天真实的经历、此刻的心情自然说一句，" if inner is not None
                   else "仅基于公共场景自然说一句，")
                + "不透露任何聊天或私人记忆，"
                "不要求回答，不调用动作或工具。" + GROUNDED_ONLY + "话题：" + topic,
                source="external", expected_identity=identity,
            )
            # speech.started can race with the second socket send. The user
            # owns the channel now; never re-arm or report a clean submission.
            if (row["identity"] != identity or row["last_user"] != last_user):
                return
            if (row["reason"] == "speech_preempted" or
                    getattr(client, "_last_provider_speech_started_t", None) != speech_at):
                row["attempts"] = 2
                self._status(robot_id, "suppressed", "speech_preempted")
                return
            row["attempts"] = attempt
            row["last_spoke"] = time.monotonic()
            if inner is not None:
                inner.note_proactive(robot_id)
            row["previous"] = topic
            row["first_scene"] = row["scene"] if attempt == 1 else row["first_scene"]
            row["turn_id"] = result["turn_id"]
            row["speaker_started"] = row["speaker_drained"] = False
            row["speaker_drained_at"] = None
            row["speaker_interrupted"] = False
            row["last_topic"] = topic
            self._status(robot_id, "submitted_unknown", decision="SPEAK")
            self._record(robot_id, source_turn_id, topic=topic, result_turn_id=row["turn_id"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if row["identity"] == identity and row["last_user"] == last_user:
                row["attempts"] = 2  # fail closed, never spin on provider failures
                self._status(robot_id, "suppressed", type(exc).__name__)
                self._record(robot_id, source_turn_id)

    async def _run(self):
        while self.runtime.started:
            await self.tick()
            await asyncio.sleep(2)

    async def start(self):
        if self.graph is not None and self.worker is None:
            self.worker = asyncio.create_task(self._run(), name="idle-planner")

    async def close(self):
        await self.short.close()
        tasks = list(self.tasks.values())
        if self.worker is not None:
            tasks.append(self.worker)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
        self.worker = None
        if self.model is not None and hasattr(self.model, "close"):
            await self.model.close()
