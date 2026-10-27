"""Her mood, her own day, what is still open and what is on her mind, per robot.

Beside the reply path, never on it: accepted events change a small state at
once, the decision model reads a finished exchange in the background, a
finished conversation becomes one diary line plus any open threads, and each
night the day becomes a few impressions (annie_brain.inner_life). The Realtime
client is nudged only when the hint text it would send has changed.

Her own wish to speak goes out as an ``inner.thought`` event through the same
Event Router / Speak Policy / TurnCoordinator queue as a greeting: only with a
face in view, never into a conversation, at most PROACTIVE_PER_HOUR times an
hour together with the 8C idle remark.
"""

import asyncio
import logging
import time
from collections import deque
from dataclasses import replace
from datetime import timedelta

from annie_adapters import inner_life_store as rows
from annie_brain.inner_life import (
    APPRAISAL_QUESTION,
    LONELY_REASON,
    PROACTIVE_GAP_MS,
    PROACTIVE_PER_HOUR,
    QUIET_BEFORE_MS,
    THOUGHT_THRESHOLD,
    THREAD_TTL_MS,
    TURN_ENERGY_COST,
    Mood,
    appraisal_feeling,
    beijing_day,
    day_start_ms,
    energy_label,
    event_feeling,
    feel,
    moment_now,
    mood_hint,
    mood_label,
    mood_text,
    my_day,
    parse_reflection,
    parse_summary,
    public_day,
    reflection_hint,
    reflection_request,
    repeats,
    same_thread,
    settle,
    summary_line,
    summary_request,
    thought_prompt,
    thread_visible,
    threads_hint,
)
from annie_core import NormalizedEvent

log = logging.getLogger(__name__)
TICK_SECONDS = 10
DAY_ROWS = 200  # in-memory recent lines per robot; SQLite keeps keep_days
MAX_STRETCH_TURNS = 16
SUMMARY_TIMEOUT_SECONDS = 12
REFLECTION_TIMEOUT_SECONDS = 20
APPRAISAL_TIMEOUT_SECONDS = 3
REFLECT_FROM_HOUR = 3  # look back on yesterday once it is past 03:00 Beijing
REFLECTION_PEOPLE_KEPT = 30
QUIET_AFTER_SLEEP_SECONDS = 30 * 60  # after “退下/闭嘴” her own remarks wait
THOUGHT_TTL_MS = 20_000
MAX_TASKS = 32


class InnerLifeService:
    def __init__(self, runtime, *, summarizer=None, close_seconds=120, keep_days=7,
                 clock=None):
        self.runtime = runtime
        self.store = runtime.store
        self.summarizer = summarizer
        self.close_ms = int(close_seconds * 1000)
        self.keep_ms = int(keep_days * 86_400_000)
        self.clock = clock or (lambda: time.time_ns() // 1_000_000)
        robots = tuple(runtime.robots)
        self.moods = {robot_id: Mood() for robot_id in robots}
        self.day = {robot_id: [] for robot_id in robots}  # newest first
        self.threads = {robot_id: [] for robot_id in robots}  # open, newest first
        self.reflection = dict.fromkeys(robots)
        self.stretch = dict.fromkeys(robots)
        self.deleted = {robot_id: deque(maxlen=32) for robot_id in robots}
        self.notified = dict.fromkeys(robots, "")
        self.proactive = {robot_id: deque(maxlen=16) for robot_id in robots}
        self.thought_keys = {robot_id: deque(maxlen=64) for robot_id in robots}
        self.thought_state = {robot_id: {"status": "idle"} for robot_id in robots}
        self.reflect_tried = dict.fromkeys(robots)
        self.locks = {robot_id: asyncio.Lock() for robot_id in robots}
        self.stats = {robot_id: {"appraisals": 0, "appraisal_failures": 0,
                                 "summaries": 0, "summary_failures": 0,
                                 "reflections": 0, "reflection_failures": 0,
                                 "thoughts_spoken": 0, "write_failures": 0,
                                 "last_appraisal": None}
                      for robot_id in robots}
        self.tasks = set()
        self.worker = None
        self.started = False

    async def start(self):
        now = self.clock()
        for robot_id in self.moods:
            row = await asyncio.to_thread(rows.load_mood, self.store, robot_id)
            self.moods[robot_id] = Mood.from_row(row)
            self.day[robot_id] = await asyncio.to_thread(
                rows.recent_experiences, self.store, robot_id,
                since_ms=now - 2 * 86_400_000, limit=DAY_ROWS)
            self.threads[robot_id] = await asyncio.to_thread(
                rows.open_threads, self.store, robot_id, since_ms=now - THREAD_TTL_MS)
            self.reflection[robot_id] = await asyncio.to_thread(
                rows.latest_reflection, self.store, robot_id)
        self.started = True
        self.worker = asyncio.create_task(self._run(), name="inner-life")

    async def close(self):
        self.started = False
        pending = [*self.tasks, *([self.worker] if self.worker else [])]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self.tasks.clear()
        self.worker = None
        if self.summarizer is not None and hasattr(self.summarizer, "close"):
            await self.summarizer.close()

    # ---- what the Realtime session and the idle planner read ----------------------
    def hint(self, robot_id):
        if not self.started or robot_id not in self.moods:
            return ""
        now = self.clock()
        who = self._partner(robot_id)
        # Her day is not always in view: an always-on diary made her treat the
        # person she was talking to as someone new (replay 2026-10-27). She
        # looks it up with recall_my_day when asked.
        return (mood_hint(self.moods[robot_id], now)
                + threads_hint(self._threads_for(robot_id, who, now), who)
                + reflection_hint(self.reflection[robot_id], who))

    def my_day(self, robot_id):
        """The recall_my_day tool result."""
        if not self.started or robot_id not in self.moods:
            return {"status": "unavailable"}
        return my_day(self.day[robot_id], self.clock(), self._partner(robot_id),
                      self.reflection[robot_id])

    def idle_material(self, robot_id, *, public):
        """Mood for any planner; for a stranger-facing remark only her bodily day."""
        if not self.started or robot_id not in self.moods:
            return {"mood": "", "own_day": ""}
        now = self.clock()
        return {"mood": mood_text(self.moods[robot_id], now, with_reason=not public),
                "own_day": "；".join(public_day(self.day[robot_id], now)) if public else ""}

    def proactive_block(self, robot_id, now_ms=None):
        """Why her next unprompted remark must wait (shared with the 8C idle remark)."""
        now = self.clock() if now_ms is None else now_ms
        recent = [at for at in self.proactive.get(robot_id, ()) if now - at < 3_600_000]
        if len(recent) >= PROACTIVE_PER_HOUR:
            return "proactive_hourly_cap"
        if recent and now - max(recent) < PROACTIVE_GAP_MS:
            return "proactive_gap"
        return None

    def note_proactive(self, robot_id):
        if robot_id in self.proactive:
            self.proactive[robot_id].append(self.clock())

    def greeting_extra(self, robot_id, name):
        """A known face arrived: one open thread of TA's she may ask about, alone with TA."""
        if not self.started or robot_id not in self.threads or not name:
            return ""
        view = self._view(robot_id)
        if view is not None and view.get("count", 1) > 1:
            return ""  # others are there: do not bring up TA's affairs
        now = self.clock()
        pick = next((row for row in self.threads[robot_id]
                     if row["person"] == name and thread_visible(row, now)), None)
        if pick is None:
            return ""
        self._mark_asked(robot_id, [pick], now)
        return (f"TA之前跟你说过：{pick['text']}。打完招呼可以顺口问一句后来怎样，"
                "只问一句，对方不想说就放下。")

    def snapshot(self, robot_id):
        now = self.clock()
        mood = settle(self.moods[robot_id], now)
        start = day_start_ms(now)
        stretch = self.stretch[robot_id]
        hint = self.hint(robot_id)
        client = self.runtime.realtime.get(robot_id)
        sent = getattr(client, "_visual_sent_instructions", None) or ""
        reflection = self.reflection[robot_id]
        who = self._partner(robot_id)
        return {
            "robot_id": robot_id, "enabled": True, "started": self.started,
            "mood": {"label": mood_label(mood, now), "text": mood_text(mood, now),
                     "valence": round(mood.valence, 2), "energy": round(mood.energy, 2),
                     "energy_label": energy_label(mood.energy), "tone": mood.tone,
                     "reason": mood.reason, "reason_ms": mood.reason_ms},
            "today": [{"kind": row["kind"], "text": row["text"], "count": row["count"],
                       "person": row["person"], "created_ms": row["created_ms"]}
                      for row in reversed(self.day[robot_id]) if row["created_ms"] >= start],
            "partner": who,
            "threads": [{"person": row["person"], "text": row["text"],
                         "follow_up": row["follow_up"], "asks": row["asks"],
                         "created_ms": row["created_ms"],
                         "visible": thread_visible(row, now)}
                        for row in self.threads[robot_id][:12]],
            "reflection": ({"day": reflection["day"], "self": reflection["self"],
                            "people": len(reflection["people"]),
                            "partner_impression": reflection["people"].get(who) if who else None}
                           if reflection else None),
            "thought": dict(self.thought_state[robot_id]),
            "proactive_last_hour": sum(now - at < 3_600_000 for at in self.proactive[robot_id]),
            "conversation": ({"open": True, "turns": len(stretch["turns"]),
                              "who": stretch["who"], "started_ms": stretch["started_ms"]}
                             if stretch else {"open": False}),
            "summarizer": getattr(self.summarizer, "model", None),
            # What the live Realtime session was last sent, not what was queued.
            "hint_in_session": bool(hint) and hint in sent,
            "has_hint": bool(hint),
            "stats": dict(self.stats[robot_id]),
        }

    # ---- inputs ---------------------------------------------------------------------
    def on_event(self, robot_id, event):
        """An event the speak policy accepted: a touch, a smell, a known face."""
        if robot_id not in self.moods or not self.started:
            return
        feeling = event_feeling(event.kind, event.payload)
        if feeling is None:
            return
        now = self.clock()
        self.moods[robot_id] = feel(
            self.moods[robot_id], now, valence=feeling["valence"], energy=feeling["energy"],
            tone=feeling.get("tone"), reason=feeling.get("reason"), interaction=True)
        self._save_mood(robot_id)
        self._remember(robot_id, feeling["diary_kind"], feeling["diary"],
                       person=feeling.get("person"))
        self._notify(robot_id)

    def on_turn(self, robot_id, *, chat_id, user_text, reply_text, input_mode, client):
        """A completed user turn: she gets a little tired, and the exchange is read."""
        if robot_id not in self.moods or not self.started or not user_text.strip():
            return
        now = self.clock()
        who = self._partner(robot_id) if input_mode == "audio" else None
        self.moods[robot_id] = feel(self.moods[robot_id], now, energy=-TURN_ENERGY_COST,
                                    interaction=True)
        self._save_mood(robot_id)
        stretch = self.stretch[robot_id]
        if stretch is not None and (stretch["chat_id"] != chat_id
                                    or now - stretch["last_ms"] > self.close_ms):
            self._close_stretch(robot_id)
            stretch = None
        if stretch is None:
            stretch = self.stretch[robot_id] = {"chat_id": chat_id, "who": who, "turns": [],
                                                "started_ms": now, "last_ms": now}
        stretch["turns"].append((user_text[:200], (reply_text or "")[:200]))
        stretch["last_ms"] = now
        stretch["who"] = stretch["who"] or who
        if len(stretch["turns"]) >= MAX_STRETCH_TURNS:
            self._close_stretch(robot_id)
        self._spawn(self._appraise(robot_id, client, user_text, reply_text or "", who))
        self._notify(robot_id)

    def on_chat_deleted(self, robot_id, chat_id):
        if robot_id not in self.moods:
            return
        stretch = self.stretch[robot_id]
        if stretch is not None and stretch["chat_id"] == chat_id:
            self.stretch[robot_id] = None
        self.deleted[robot_id].append(chat_id)
        self.day[robot_id] = [row for row in self.day[robot_id] if row["chat_id"] != chat_id]
        self.threads[robot_id] = [row for row in self.threads[robot_id]
                                  if row["chat_id"] != chat_id]
        reflection = self.reflection[robot_id]
        if reflection is not None and chat_id in reflection["chat_ids"]:
            reflection["people"] = {}
            reflection["chat_ids"] = [item for item in reflection["chat_ids"] if item != chat_id]

        def forget():
            rows.forget_chat(self.store, robot_id, chat_id)
            rows.forget_threads(self.store, robot_id, chat_id=chat_id)
            rows.forget_reflection(self.store, robot_id, chat_id=chat_id)

        self._write(robot_id, forget)
        self._notify(robot_id)

    def on_person_forgotten(self, robot_id, name):
        if robot_id not in self.moods or not name:
            return
        stretch = self.stretch[robot_id]
        if stretch is not None and stretch["who"] == name:
            stretch["who"] = None
        self.day[robot_id] = [row for row in self.day[robot_id]
                              if row["person"] != name and name not in row["text"]]
        self.threads[robot_id] = [row for row in self.threads[robot_id]
                                  if row["person"] != name and name not in row["text"]]
        reflection = self.reflection[robot_id]
        if reflection is not None:
            reflection["people"] = {person: text for person, text in reflection["people"].items()
                                    if person != name and name not in text}
        if name in self.moods[robot_id].reason:
            self.moods[robot_id] = replace(settle(self.moods[robot_id], self.clock()),
                                           tone="neutral", reason="", reason_ms=None)
            self._save_mood(robot_id)

        def forget():
            rows.forget_person(self.store, robot_id, name)
            rows.forget_threads(self.store, robot_id, person=name)
            rows.forget_reflection(self.store, robot_id, person=name)

        self._write(robot_id, forget)
        self._notify(robot_id)

    # ---- internals ------------------------------------------------------------------
    def _view(self, robot_id):
        monitor = getattr(self.runtime, "room_monitors", {}).get(robot_id)
        return monitor.people_view() if monitor is not None else None

    def _partner(self, robot_id):
        """Who she is talking to: a matched voice, else the one known face she sees."""
        client = self.runtime.realtime.get(robot_id)
        speakers = getattr(client, "speakers", None)
        if speakers is not None:
            current = speakers.public()
            if current.get("status") in {"matched", "carried"} and current.get("name"):
                return current["name"]
        view = self._view(robot_id)
        if view:
            if view.get("gaze_name"):
                return view["gaze_name"]
            if view.get("count") == 1 and len(view.get("names") or []) == 1:
                return view["names"][0]
        return None

    def _threads_for(self, robot_id, who, now, chat_id=None):
        """A recognised person's own threads; without a name, only this Chat's."""
        if chat_id is None:
            chat_id = getattr(self.runtime.robots.get(robot_id), "chat_id", None)
        return [row for row in self.threads[robot_id] if thread_visible(row, now) and (
            row["person"] == who if who else row["person"] is None and row["chat_id"] == chat_id)]

    async def _appraise(self, robot_id, client, user_text, reply_text, who):
        ask = getattr(client, "ask_decision", None)
        if ask is None:
            return
        stats = self.stats[robot_id]
        try:
            answers, model = await asyncio.wait_for(
                ask({"用户": user_text[:300], "小安妮回复": reply_text[:200]},
                    {"mood": APPRAISAL_QUESTION}), APPRAISAL_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            raise
        except LookupError:
            return  # no decision model is up: the mood stays where it is
        except Exception as exc:
            stats["appraisal_failures"] += 1
            log.info("inner_life appraisal failed robot=%s error=%s", robot_id, type(exc).__name__)
            return
        stats["appraisals"] += 1
        answer = answers.get("mood") if isinstance(answers, dict) else None
        effect = appraisal_feeling(answer, who)
        stats["last_appraisal"] = {
            "choice": answer.get("choice") if isinstance(answer, dict) else None,
            "confidence": answer.get("confidence") if isinstance(answer, dict) else None,
            "applied": effect is not None, "model": model, "ts_ms": self.clock()}
        if effect is None or not self.started:
            return
        valence, tone, reason = effect
        self.moods[robot_id] = feel(self.moods[robot_id], self.clock(), valence=valence,
                                    tone=tone, reason=reason)
        self._save_mood(robot_id)
        self._notify(robot_id)
        motions = getattr(self.runtime, "motions", None)
        if motions is not None:
            motions.on_feeling(robot_id, tone, reason)

    def _close_stretch(self, robot_id):
        stretch, self.stretch[robot_id] = self.stretch[robot_id], None
        if stretch is not None and stretch["turns"]:
            self._spawn(self._summarize(robot_id, stretch))

    async def _summarize(self, robot_id, stretch):
        stats = self.stats[robot_id]
        who = stretch["who"]
        now = self.clock()
        known = [row for row in self.threads[robot_id] if row["id"] is not None and (
            row["person"] == who if who else
            row["person"] is None and row["chat_id"] == stretch["chat_id"])]
        result = {"summary": (f"和{who or '一位没认出的人'}聊了{len(stretch['turns'])}轮"
                              if len(stretch["turns"]) >= 3 else None),
                  "threads": [], "resolved": [], "asked": []}
        if self.summarizer is not None:
            system, user = summary_request(stretch["turns"], who, known)
            try:
                result = parse_summary(await asyncio.wait_for(
                    self.summarizer.complete(system, user), SUMMARY_TIMEOUT_SECONDS),
                    [row["id"] for row in known])
                stats["summaries"] += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                stats["summary_failures"] += 1
                log.info("inner_life summary failed robot=%s error=%s",
                         robot_id, type(exc).__name__)
        # A deleted Chat must not come back through a summary that was in flight.
        if not self.started or stretch["chat_id"] in self.deleted[robot_id]:
            return
        if result["summary"]:
            self._remember(robot_id, "chat", summary_line(result["summary"], who), person=who,
                           chat_id=stretch["chat_id"], at_ms=stretch["started_ms"])
        by_id = {row["id"]: row for row in known}
        self._mark_asked(robot_id, [by_id[i] for i in result["asked"]], now)
        resolved = [by_id[i] for i in result["resolved"]]
        if resolved:
            self.threads[robot_id] = [row for row in self.threads[robot_id] if row not in resolved]
            ids = [row["id"] for row in resolved]
            self._write(robot_id, lambda: rows.resolve_threads(self.store, robot_id, ids,
                                                               now_ms=now))
        for thread in result["threads"]:
            # The model often writes an existing (or just resolved) thread again.
            if any(same_thread(thread["text"], row["text"]) for row in known):
                continue
            self._add_thread(robot_id, thread, person=who, chat_id=stretch["chat_id"],
                             at_ms=stretch["started_ms"])
        self._notify(robot_id)

    def _add_thread(self, robot_id, thread, *, person, chat_id, at_ms):
        if any(row["person"] == person and row["text"] == thread["text"]
               for row in self.threads[robot_id]):
            return
        row = {"id": None, "person": person, "chat_id": chat_id, "text": thread["text"],
               "follow_up": thread["follow_up"], "status": "open", "asks": 0,
               "asked_ms": None, "created_ms": at_ms, "updated_ms": at_ms}
        self.threads[robot_id].insert(0, row)
        del self.threads[robot_id][100:]

        def insert():
            row["id"] = rows.add_thread(self.store, robot_id, row["text"], row["follow_up"],
                                        person=person, chat_id=chat_id, now_ms=at_ms)

        self._write(robot_id, insert)

    def _mark_asked(self, robot_id, picked, now):
        if not picked:
            return
        for row in picked:
            row["asks"] += 1
            row["asked_ms"] = now
        self._write(robot_id, lambda: rows.mark_threads_asked(
            self.store, robot_id, [row["id"] for row in picked if row["id"]], now_ms=now))

    async def _reflect(self, robot_id, day_key, since_ms, until_ms):
        stats = self.stats[robot_id]
        try:
            day_rows = await asyncio.to_thread(
                rows.recent_experiences, self.store, robot_id,
                since_ms=since_ms, until_ms=until_ms, limit=80)
            if not day_rows:
                return
            names = {row["person"] for row in day_rows if row["person"]}
            latest = self.reflection[robot_id]
            impressions = dict(latest["people"]) if latest else {}
            system, user = reflection_request(
                day_rows, self.threads[robot_id],
                {name: impressions[name] for name in names if name in impressions})
            parsed = parse_reflection(await asyncio.wait_for(
                self.summarizer.complete(system, user, max_tokens=500),
                REFLECTION_TIMEOUT_SECONDS), names)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            stats["reflection_failures"] += 1
            log.info("inner_life reflection failed robot=%s error=%s",
                     robot_id, type(exc).__name__)
            return
        stats["reflections"] += 1
        latest = self.reflection[robot_id]  # may have changed (a forget) while waiting
        people = {**(latest["people"] if latest else {}), **parsed["people"]}
        reflection = {
            "day": day_key,
            "people": dict(list(people.items())[-REFLECTION_PEOPLE_KEPT:]),
            "self": parsed["self"] or (latest["self"] if latest else ""),
            "chat_ids": sorted({row["chat_id"] for row in day_rows if row["chat_id"]}
                               | set(latest["chat_ids"] if latest else ())),
            "created_ms": self.clock(),
        }
        if not self.started:
            return
        self.reflection[robot_id] = reflection
        self._write(robot_id, lambda: rows.save_reflection(self.store, robot_id, reflection))
        self._notify(robot_id)

    def _maybe_reflect(self, robot_id, now):
        """Once a day, after 03:00 Beijing, look back on yesterday."""
        if self.summarizer is None:
            return
        today = beijing_day(now)
        start_today = day_start_ms(now)
        if now - start_today < REFLECT_FROM_HOUR * 3_600_000:
            return
        day_key = (today - timedelta(days=1)).isoformat()
        latest = self.reflection[robot_id]
        if (latest is not None and latest["day"] >= day_key) or self.reflect_tried[robot_id] == day_key:
            return
        self.reflect_tried[robot_id] = day_key
        self._spawn(self._reflect(robot_id, day_key, start_today - 86_400_000, start_today))

    # ---- something on her mind -------------------------------------------------------
    def _thought_block(self, robot_id, now, view):
        client = self.runtime.realtime.get(robot_id)
        if client is None or not client.ready or client.speech is None:
            return "realtime_or_tts_unavailable"
        if not view or view.get("count", 0) < 1:
            return "no_face_in_view"
        if (client.speech_active or client.turn_id or client.response_id
                or client.speech.output_active()):
            return "conversation_busy"
        events = getattr(self.runtime, "events", None)
        if events is not None and events.pending.get(robot_id):
            return "external_event_pending"
        quiet = time.monotonic() - client._last_conversation_activity_t
        if quiet * 1000 < QUIET_BEFORE_MS:
            return "conversation_recent"
        asked_quiet = getattr(client, "_asked_quiet_at", None)
        if asked_quiet is not None and time.monotonic() - asked_quiet < QUIET_AFTER_SLEEP_SECONDS:
            return "asked_to_be_quiet"
        dialogue = getattr(client, "dialogue", None)
        if dialogue is not None and dialogue.snapshot().get("followup") == "avoid":
            return "dialogue_declined"
        return self.proactive_block(robot_id, now)

    def _thoughts(self, robot_id, view, now):
        """What is on her mind right now, each with how much she wants to say it."""
        used = set(self.thought_keys[robot_id])
        names = list(view.get("names") or [])
        alone = view.get("count") == 1
        found = []
        if alone and len(names) == 1:
            for row in self.threads[robot_id]:
                if row["person"] == names[0] and thread_visible(row, now):
                    found.append({"key": f"thread:{row['id']}:{row['asks']}", "weight": 0.75,
                                  "person": names[0], "thread": row,
                                  "intent": f"想起{names[0]}之前说过{row['text']}，惦记着想问问后来怎样"})
                    break
        mood = settle(self.moods[robot_id], now)
        if mood.tone == "bored" and mood.reason == LONELY_REASON:
            found.append({"key": f"lonely:{mood.reason_ms}", "weight": 0.6,
                          "intent": "好久没人跟你说话了，看到有人来有点高兴，想跟对方打个招呼聊两句"})
        for row in self.day[robot_id]:
            if (row["kind"] == "touch" and row["count"] >= 3
                    and now - row["updated_ms"] <= 15 * 60_000):
                found.append({"key": f"touch:{row['created_ms']}:{row['count'] // 3}",
                              "weight": 0.6,
                              "intent": f"刚才{row['text'].removeprefix('有人')}好几次，想对这件事说点什么"})
                break
        moment = moment_now(now)
        if moment is not None:
            found.append({"key": f"moment:{beijing_day(now)}:{moment[0]}", "weight": 0.55,
                          "intent": moment[1]})
        reflection = self.reflection[robot_id]
        if reflection and reflection.get("self"):
            found.append({"key": f"self:{beijing_day(now)}", "weight": 0.5,
                          "intent": f"想随口说说自己最近的状态：{reflection['self']}"})
        return [item for item in found if item["key"] not in used]

    def _set_thought(self, robot_id, status, **fields):
        state = self.thought_state[robot_id]
        if state.get("status") != status or state.get("intent") != fields.get("intent"):
            self.thought_state[robot_id] = {"status": status, "ts_ms": self.clock(), **fields}

    def _consider_thoughts(self, robot_id, now):
        view = self._view(robot_id)
        blocked = self._thought_block(robot_id, now, view)
        if blocked:
            self._set_thought(robot_id, blocked)
            return
        candidates = self._thoughts(robot_id, view, now)
        if not candidates:
            self._set_thought(robot_id, "nothing_on_mind")
            return
        best = max(candidates, key=lambda item: item["weight"])
        if best["weight"] < THOUGHT_THRESHOLD:
            self._set_thought(robot_id, "below_threshold", intent=best["intent"],
                              weight=best["weight"])
            return
        person = best.get("person")
        prompt = thought_prompt(best["intent"], person=person, mood=mood_text(
            self.moods[robot_id], now, with_reason=person is not None))
        row = self.runtime.events.route(robot_id, NormalizedEvent(
            kind="inner.thought", source="inner", payload={"prompt": prompt, "key": best["key"]},
            ts_ms=now, ttl_ms=THOUGHT_TTL_MS), f"inner:{robot_id}")
        if row.get("status") != "waiting":
            self._set_thought(robot_id, "not_queued:" + str(row.get("reason")),
                              intent=best["intent"], weight=best["weight"])
            return
        self.thought_keys[robot_id].append(best["key"])
        self.note_proactive(robot_id)
        self.stats[robot_id]["thoughts_spoken"] += 1
        if best.get("thread") is not None:
            self._mark_asked(robot_id, [best["thread"]], now)
        self._set_thought(robot_id, "queued", intent=best["intent"], weight=best["weight"])

    # ---- persistence and plumbing ----------------------------------------------------
    def _remember(self, robot_id, kind, text, *, person=None, chat_id=None, at_ms=None):
        day = self.day[robot_id]
        now = self.clock()
        if at_ms is None and repeats(day[0] if day else None, kind, text, now):
            row = day[0]
            row["count"] += 1
            row["updated_ms"] = now
            self._write(robot_id, lambda: rows.bump_experience(
                self.store, robot_id, row["id"], now_ms=now) if row["id"] else None)
            return
        created = now if at_ms is None else at_ms
        row = {"id": None, "kind": kind, "text": text, "person": person, "chat_id": chat_id,
               "count": 1, "created_ms": created, "updated_ms": now}
        day.append(row)
        day.sort(key=lambda item: item["created_ms"], reverse=True)
        del day[DAY_ROWS:]

        def insert():
            saved = rows.add_experience(self.store, robot_id, kind, text, now_ms=created,
                                        keep_after_ms=now - self.keep_ms,
                                        person=person, chat_id=chat_id)
            row["id"] = saved["id"]

        self._write(robot_id, insert)

    def _save_mood(self, robot_id):
        snapshot = self.moods[robot_id].to_row()
        self._write(robot_id, lambda: rows.save_mood(self.store, robot_id, snapshot))

    def _write(self, robot_id, operation):
        async def run():
            # One lock per robot keeps an insert ahead of the update that needs its id.
            async with self.locks[robot_id]:
                try:
                    await asyncio.to_thread(operation)
                except Exception:
                    self.stats[robot_id]["write_failures"] += 1
                    log.exception("inner_life write failed robot=%s", robot_id)

        self._spawn(run())

    def _spawn(self, coroutine):
        if len(self.tasks) >= MAX_TASKS:
            coroutine.close()
            log.warning("inner_life task dropped: %d pending", len(self.tasks))
            return
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def _notify(self, robot_id):
        text = self.hint(robot_id)
        if text == self.notified[robot_id]:
            return
        self.notified[robot_id] = text
        client = self.runtime.realtime.get(robot_id)
        if client is None:
            return
        mood = settle(self.moods[robot_id], self.clock())
        client.publish("inner_life.changed", mood=mood_label(mood, self.clock()),
                       energy=energy_label(mood.energy), reason=mood.reason)
        client.inner_changed()

    def tick(self):
        now = self.clock()
        for robot_id, stretch in self.stretch.items():
            if stretch is not None and now - stretch["last_ms"] > self.close_ms:
                self._close_stretch(robot_id)
            self._maybe_reflect(robot_id, now)
            if getattr(self.runtime, "events", None) is not None:
                self._consider_thoughts(robot_id, now)
            self._notify(robot_id)

    async def _run(self):
        while self.started:
            await asyncio.sleep(TICK_SECONDS)
            try:
                self.tick()
            except Exception:
                log.exception("inner_life tick failed")
