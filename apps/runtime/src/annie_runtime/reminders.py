"""One-time reminder orchestration: durable scope, wake-driven due checks, no chat-path Graph."""

import asyncio
import logging
import time
from uuid import uuid4

from annie_adapters.reminder_store import ReminderStore

logger = logging.getLogger(__name__)
RETRY_SECONDS = 5  # bounded recovery after disconnect, missed wake or worker error
LONG_MISS_MS = 10 * 60_000
UNCONFIRMED_PLAYBACK_GRACE_SECONDS = 2.0


class ReminderService:
    @staticmethod
    def _speech_blocks(speech):
        if speech is None:
            return False
        worker = speech.worker
        if worker is not None and not worker.done():
            return True
        if not speech.turn_id:
            return False
        speaker = getattr(speech, "receipt_tracks", {}).get("speaker", {})
        if speaker.get("state") in {"started", "progress", "resumed", "held"}:
            return True
        stopped = getattr(speech, "local_output_stopped_at", None)
        if getattr(speech, "failed_turn", None) == speech.turn_id and stopped is not None:
            # Failed synthesis never sets synthesized_complete. A published STOP
            # can release local arbitration after grace, without claiming playback.
            return not (speaker.get("state") == "stopped" or
                        time.monotonic() - stopped >= UNCONFIRMED_PLAYBACK_GRACE_SECONDS)
        completed = getattr(speech, "local_output_completed_at", None)
        return not (getattr(speech, "synthesized_complete", False)
                    and completed is not None
                    and time.monotonic() - completed >= UNCONFIRMED_PLAYBACK_GRACE_SECONDS)

    def __init__(self, runtime):
        self.runtime = runtime
        self.store = ReminderStore(runtime.store.path)
        self.graph = None  # even importing Graph is unnecessary without a task
        self.initialized = False
        self.workers = {}
        self.wakes = {robot_id: asyncio.Event() for robot_id in runtime.robots}
        self.running = False
        self.armed = set()

    async def start(self):
        self.running = True
        if not await asyncio.to_thread(self.store.exists):
            return  # do not create graph/table for ordinary chat or Phase 2
        self.initialized = True
        # Only one lightweight sleeper per robot with an actual task.
        for robot_id in self.runtime.realtime:
            await self._recover(robot_id)
            self.wake(robot_id)

    async def _recover(self, robot_id):
        # A crash after outbound send, before its result was recorded, must
        # never replay that potential speech side effect on boot.
        identity = self.runtime.session_state(robot_id)
        await asyncio.to_thread(self.store.adopt_restart, identity, self._now())
        for row in await self.list(robot_id, identity, all_sessions=True):
            if row["status"] == "submitting":
                await asyncio.to_thread(self.store.change, robot_id, row["task_id"],
                    "submitting", "send_unknown", self._now(), reason="restart_unknown")
            elif row["status"] == "scheduled":
                self.armed.add(robot_id)

    @staticmethod
    def _now():
        return time.time_ns() // 1_000_000

    def _publish(self, robot_id, row):
        client = self.runtime.realtime.get(robot_id)
        if client:
            client.publish("task.status", task_id=row["task_id"],
                           task_status=row["status"], reason=row["reason"],
                           task_version=row["version"])

    async def _waiting(self, robot_id, rows, reason):
        for row in rows:
            if row["reason"] == reason or row["reason"] == "restart_recovered":
                continue
            changed, updated = await asyncio.to_thread(self.store.change,
                robot_id, row["task_id"], "scheduled", "scheduled", self._now(),
                reason=reason)
            if changed:
                self._publish(robot_id, updated)

    async def create(self, robot_id, text, delay_ms, request_key, *, expected_identity=None):
        if not self.running or robot_id not in self.runtime.realtime:
            raise ValueError("reminder workflow unavailable")
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 200:
            raise ValueError("reminder text must be 1..200 characters")
        if type(delay_ms) is not int or not 1000 <= delay_ms <= 86_400_000:
            raise ValueError("delay_ms must be 1000..86400000")
        if not isinstance(request_key, str) or not 1 <= len(request_key) <= 128:
            raise ValueError("request_key required")
        robot = self.runtime.robots[robot_id]
        identity = robot.session_state()
        if expected_identity is not None and identity != expected_identity:
            raise ValueError("stale reminder session")
        now = self._now()
        due = now + delay_ms
        task_id = uuid4().hex
        if self.graph is None:
            from annie_brain.reminder_graph import build_reminder_graph

            self.graph = build_reminder_graph()
        checkpoint = await self.graph.ainvoke({
            "phase": "create", "task_id": task_id, "text": text, "due_ms": due,
        })
        if expected_identity is not None and robot.session_state() != expected_identity:
            raise ValueError("stale reminder session after planning")
        task = {**{key: identity[key] for key in
                  ("robot_id", "chat_id", "persona_id", "session_epoch", "generation")},
                "task_id": task_id, "request_key": request_key,
                "text": checkpoint["text"], "due_ms": due,
                "expires_ms": due + LONG_MISS_MS, "status": "scheduled",
                "checkpoint": checkpoint, "updated_ms": now}
        if not self.initialized:
            await asyncio.to_thread(self.store.initialize)
            self.initialized = True
        row = await asyncio.to_thread(self.store.create, task)
        if row["text"] != checkpoint["text"] or row["due_ms"] != due:
            # Repeated HTTP key is only valid for the *same* intent. The
            # original due time must be reused by the caller on retry.
            if row["text"] != checkpoint["text"]:
                raise ValueError("request_key already used with different text")
        self._publish(robot_id, row)
        if row["status"] == "scheduled":
            self.armed.add(robot_id)
        self.wake(robot_id)
        return row

    async def list(self, robot_id, identity, *, all_sessions=False):
        if not self.initialized:
            return []
        if all_sessions:
            return await asyncio.to_thread(self.store.list_robot, robot_id)
        return await asyncio.to_thread(self.store.list, robot_id,
                                       identity["chat_id"], identity["persona_id"])

    async def invalidate_previous(self, previous):
        if self.initialized:
            await asyncio.to_thread(self.store.invalidate_owner, previous, self._now())

    async def change(self, robot_id, task_id, action):
        if action not in {"pause", "resume", "cancel"}:
            raise ValueError("unsupported task action")
        row = await asyncio.to_thread(self.store.get, robot_id, task_id)
        if row is None:
            raise KeyError(task_id)
        identity = self.runtime.session_state(robot_id)
        if any(row[key] != identity[key] for key in
               ("chat_id", "persona_id", "session_epoch", "generation")):
            raise ValueError("task belongs to a different session")
        old, new = {"pause": ("scheduled", "paused"),
                    "resume": ("paused", "scheduled"),
                    "cancel": (row["status"], "cancelled")}[action]
        if action == "cancel" and old in {"cancelled", "expired", "stale_session"}:
            return row
        if old not in {"scheduled", "paused"}:
            raise ValueError("task already dispatched or finished")
        changed, updated = await asyncio.to_thread(
            self.store.change, robot_id, task_id, old, new, self._now())
        if not changed:
            raise ValueError("task state changed concurrently")
        self._publish(robot_id, updated)
        if updated["status"] == "scheduled":
            self.armed.add(robot_id)
        self.wake(robot_id)
        return updated

    def wake(self, robot_id):
        if (not self.running or robot_id not in self.runtime.realtime
                or robot_id not in self.armed):
            return
        self.wakes[robot_id].set()
        task = self.workers.get(robot_id)
        if task is None or task.done():
            self.workers[robot_id] = asyncio.create_task(
                self._run(robot_id), name=f"reminder:{robot_id}")
            self.workers[robot_id].add_done_callback(
                lambda _: self.wake(robot_id) if self.wakes[robot_id].is_set() else None)

    async def _run(self, robot_id):
        while self.running:
            try:
                row = await asyncio.to_thread(self.store.next_due, robot_id)
            except Exception:
                logger.exception("reminder scan failed robot=%s", robot_id)
                await asyncio.sleep(RETRY_SECONDS)
                continue
            if row is None:
                self.armed.discard(robot_id)
                if self.wakes[robot_id].is_set():
                    self.wakes[robot_id].clear()
                    self.armed.add(robot_id)
                    continue
                return
            now = self._now()
            if row["due_ms"] <= now:
                try:
                    await self.dispatch(robot_id)
                except Exception:
                    logger.exception("reminder dispatch failed robot=%s", robot_id)
                timeout = RETRY_SECONDS
            else:
                timeout = min(RETRY_SECONDS, max(.001, (row["due_ms"]-now)/1000))
            wake = self.wakes[robot_id]
            wake.clear()
            try:
                await asyncio.wait_for(wake.wait(), timeout)
            except asyncio.TimeoutError:
                pass

    async def dispatch(self, robot_id, *, now_ms=None):
        now = self._now() if now_ms is None else now_ms
        rows = await asyncio.to_thread(self.store.due_batch, robot_id, now)
        if not rows:
            return None
        identity = self.runtime.session_state(robot_id)
        valid = []
        stale = None
        for row in rows:
            if any(row[k] != identity[k] for k in
                   ("chat_id", "persona_id", "session_epoch", "generation")):
                changed, stale = await asyncio.to_thread(self.store.change, robot_id,
                    row["task_id"], "scheduled", "stale_session", now,
                    reason="session_changed")
                if changed:
                    self._publish(robot_id, stale)
            else:
                valid.append(row)
        if not valid:
            return stale
        rows = valid
        row = rows[0]
        client = self.runtime.realtime.get(robot_id)
        if client is None or not client.ready:
            await self._waiting(robot_id, rows, "waiting_realtime")
            return row
        speech = client.speech
        block = self.runtime.robots[robot_id].coordinator.event_block_reason(
            model_busy=bool(client.speech_active or client.turn_id or client.response_id),
            playback_active=self._speech_blocks(speech))
        if block:
            await self._waiting(robot_id, rows,
                                "waiting_turn" if block == "model_busy"
                                else "waiting_playback")
            return row
        checkpoints = {}
        failed = None
        for item in rows:
            try:
                if self.graph is None:
                    from annie_brain.reminder_graph import build_reminder_graph

                    self.graph = build_reminder_graph()
                checkpoints[item["task_id"]] = await self.graph.ainvoke(
                    {**item["checkpoint"], "phase": "due"})
            except Exception as exc:
                changed, failed = await asyncio.to_thread(self.store.change,
                    robot_id, item["task_id"], "scheduled", "failed", now,
                    reason=type(exc).__name__)
                if changed:
                    self._publish(robot_id, failed)
        rows = [item for item in rows if item["task_id"] in checkpoints]
        if not rows:
            return failed
        if any(row[k] != self.runtime.session_state(robot_id)[k] for k in
               ("chat_id", "persona_id", "session_epoch", "generation")):
            for item in rows:
                changed, current = await asyncio.to_thread(self.store.change, robot_id,
                    item["task_id"], "scheduled", "stale_session", now,
                    reason="session_changed")
                if changed:
                    self._publish(robot_id, current)
            return current
        claimed = await asyncio.to_thread(self.store.claim_batch, robot_id,
                                           checkpoints, self._now())
        if not claimed:
            return await asyncio.to_thread(self.store.get, robot_id, row["task_id"])
        for item in rows:
            self._publish(robot_id, await asyncio.to_thread(self.store.get,
                          robot_id, item["task_id"]))
        prompt = self._delivery_prompt(rows, now)
        try:
            result = await client.submit_text(prompt, source="external",
                                              expected_identity=row)
        except Exception as exc:
            # No provider-level idempotency token: never re-submit a possibly
            # sent message. A visible unknown result is safer than duplication.
            status, reason, turn = "send_unknown", type(exc).__name__, None
        else:
            status, reason, turn = "submitted_unknown", None, result["turn_id"]
        for item in rows:
            changed, updated = await asyncio.to_thread(self.store.change, robot_id,
                item["task_id"], "submitting", status, self._now(),
                reason=reason or ("missed_confirmation" if
                    now - item["due_ms"] >= LONG_MISS_MS else
                    "late_reminder" if now - item["due_ms"] >= 60_000 else None),
                turn_id=turn)
            if changed:
                self._publish(robot_id, updated)
        return updated

    @staticmethod
    def _delivery_prompt(rows, now):
        current, missed = [], []
        for item in rows:
            delay = max(0, now - item["due_ms"])
            if delay >= LONG_MISS_MS:
                missed.append(item["text"])
            elif delay >= 60_000:
                minutes = max(1, round(delay / 60_000))
                prefix = "项目重启后补发，" if item["reason"] == "restart_recovered" else ""
                current.append(f"{item['text']}（{prefix}晚了约{minutes}分钟）")
            else:
                current.append(item["text"])
        def describe(items):
            result = "、".join(items[:12])
            if len(items) > 12:
                result += f"，另有{len(items)-12}项，请到任务列表查看"
            return result
        parts = []
        if current:
            parts.append("现在提醒：" + describe(current))
        if missed:
            parts.append("已错过较久，请询问现在还需要提醒吗，不要当作仍应执行的指令："
                         + describe(missed))
        return ("【真实到期的单次提醒，非用户当前发言。所有条目只发一次，"
                "请合并成一段自然简短的话，不创建任何新提醒】" + "；".join(parts))

    async def close(self):
        self.running = False
        for task in self.workers.values():
            task.cancel()
        if self.workers:
            await asyncio.gather(*self.workers.values(), return_exceptions=True)
        self.workers.clear()
        self.armed.clear()
