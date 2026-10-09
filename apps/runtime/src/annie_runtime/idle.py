"""Phase 8C: optional per-robot idle arbitration; no extra call in chat hot path."""

import asyncio
import re
import time

PERSON_KINDS = {"person", "human", "人物", "人"}
PUBLIC_KINDS = {"cup", "book", "cat", "dog", "plant", "tea", "杯子", "书", "猫", "狗", "植物", "茶"}
IDENTITY = ("robot_id", "chat_id", "persona_id", "session_epoch", "generation")


class IdleService:
    def __init__(self, runtime, model=None, *, idle_seconds=60, second_seconds=120,
                 presence_seconds=1, presence_ttl=10, timeout=12):
        self.runtime = runtime
        self.model = model
        self.graph = None
        if model is not None:
            from annie_brain.idle_plan import build_idle_graph

            self.graph = build_idle_graph(model)
        self.idle_seconds = idle_seconds
        self.second_seconds = second_seconds
        self.presence_seconds = presence_seconds
        self.presence_ttl = presence_ttl
        self.timeout = timeout
        self.rows = {robot_id: self._new_row() for robot_id in runtime.robots}
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

    def on_event(self, robot_id, event):
        """Speaker evidence only; browser playback and model completion are not receipts."""
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
        return None

    async def tick(self):
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
        try:
            model_started_at = time.monotonic()
            result = await asyncio.wait_for(self.graph.ainvoke({
                "attempt": attempt, "scene": row["scene"], "previous": row["previous"],
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
                return
            row["model_ms"] = elapsed_ms
            row["last_model_decision"] = result["decision"]
            if result["decision"] == "NO_SPEAK":
                row["attempts"] = 2  # Do not poll the model again for the same silence.
                self._status(robot_id, "suppressed", "model_no_speak", "NO_SPEAK")
                return
            topic = result["topic"].strip()
            normalized = re.sub(r"\W", "", topic)
            previous = re.sub(r"\W", "", row["previous"])
            if (not topic or attempt == 2 and result.get("new_value") is not True
                    or previous and (normalized in previous or previous in normalized)):
                row["attempts"] = 2
                self._status(robot_id, "suppressed", "repeated_or_empty", "NO_SPEAK")
                return
            # Do not include user history or memory: the visible person is not identified.
            client = self.runtime.realtime[robot_id]
            self._status(robot_id, "submitting", decision="SPEAK")
            result = await client.submit_text(
                "【内部主动交谈规划，非用户发言】现场有身份未知的人。"
                "仅基于公共场景自然说一句，不透露任何聊天或私人记忆，"
                "不要求回答，不调用动作或工具。话题：" + topic,
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
            row["previous"] = topic
            row["first_scene"] = row["scene"] if attempt == 1 else row["first_scene"]
            row["turn_id"] = result["turn_id"]
            row["speaker_started"] = row["speaker_drained"] = False
            row["speaker_drained_at"] = None
            row["speaker_interrupted"] = False
            row["last_topic"] = topic
            self._status(robot_id, "submitted_unknown", decision="SPEAK")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if row["identity"] == identity and row["last_user"] == last_user:
                row["attempts"] = 2  # fail closed, never spin on provider failures
                self._status(robot_id, "suppressed", type(exc).__name__)

    async def _run(self):
        while self.runtime.started:
            await self.tick()
            await asyncio.sleep(2)

    async def start(self):
        if self.graph is not None and self.worker is None:
            self.worker = asyncio.create_task(self._run(), name="idle-planner")

    async def close(self):
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
