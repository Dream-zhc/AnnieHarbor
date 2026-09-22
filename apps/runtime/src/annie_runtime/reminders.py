"""One-time reminder orchestration: durable scope, wake-driven due checks, no chat-path Graph."""

import asyncio
import time
from uuid import uuid4

from annie_adapters.reminder_store import ReminderStore


class ReminderService:
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

    async def create(self, robot_id, text, delay_ms, request_key):
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
        now = self._now()
        due = now + delay_ms
        task_id = uuid4().hex
        if self.graph is None:
            from annie_brain.reminder_graph import build_reminder_graph

            self.graph = build_reminder_graph()
        checkpoint = await self.graph.ainvoke({
            "phase": "create", "task_id": task_id, "text": text, "due_ms": due,
        })
        task = {**{key: identity[key] for key in
                  ("robot_id", "chat_id", "persona_id", "session_epoch", "generation")},
                "task_id": task_id, "request_key": request_key,
                "text": checkpoint["text"], "due_ms": due,
                "expires_ms": due + 60_000, "status": "scheduled",
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

    async def _run(self, robot_id):
        while self.running:
            row = await asyncio.to_thread(self.store.next_due, robot_id)
            if row is None:
                self.armed.discard(robot_id)
                if self.wakes[robot_id].is_set():
                    self.wakes[robot_id].clear()
                    self.armed.add(robot_id)
                    continue
                return
            now = self._now()
            if row["due_ms"] <= now:
                await self.dispatch(robot_id)
                row = await asyncio.to_thread(self.store.next_due, robot_id)
                if row is None:
                    return
                # An already-due, temporarily blocked task must not spin.
                timeout = max(.001, (row["expires_ms"]-self._now()) / 1000)
            else:
                timeout = max(.001, (row["due_ms"]-now)/1000)
            wake = self.wakes[robot_id]
            wake.clear()
            try:
                await asyncio.wait_for(wake.wait(), timeout)
            except asyncio.TimeoutError:
                pass

    async def dispatch(self, robot_id, *, now_ms=None):
        now = self._now() if now_ms is None else now_ms
        row = await asyncio.to_thread(self.store.next_due, robot_id)
        if row is None or row["due_ms"] > now:
            return None
        task_id = row["task_id"]
        identity = self.runtime.session_state(robot_id)
        if row["expires_ms"] <= now:
            new, reason = "expired", "late_reminder"
        elif any(row[k] != identity[k] for k in
                 ("chat_id", "persona_id", "session_epoch", "generation")):
            new, reason = "stale_session", "session_changed"
        else:
            new = reason = None
        if new:
            changed, current = await asyncio.to_thread(self.store.change, robot_id,
                task_id, "scheduled", new, now, reason=reason)
            if changed:
                self._publish(robot_id, current)
            return current
        client = self.runtime.realtime.get(robot_id)
        if client is None or not client.ready:
            return row
        speech = client.speech
        block = self.runtime.robots[robot_id].coordinator.event_block_reason(
            model_busy=bool(client.speech_active or client.turn_id or client.response_id),
            playback_active=bool(speech and (speech.turn_id or
                speech.worker and not speech.worker.done())))
        if block:
            return row
        try:
            if self.graph is None:
                from annie_brain.reminder_graph import build_reminder_graph

                self.graph = build_reminder_graph()
            checkpoint = await self.graph.ainvoke({**row["checkpoint"], "phase": "due"})
        except Exception as exc:
            _, failed = await asyncio.to_thread(self.store.change, robot_id,
                task_id, "scheduled", "failed", now, reason=type(exc).__name__)
            if failed:
                self._publish(robot_id, failed)
            return failed
        if any(row[k] != self.runtime.session_state(robot_id)[k] for k in
               ("chat_id", "persona_id", "session_epoch", "generation")):
            changed, current = await asyncio.to_thread(self.store.change, robot_id,
                task_id, "scheduled", "stale_session", now, reason="session_changed")
            if changed:
                self._publish(robot_id, current)
            return current
        changed, current = await asyncio.to_thread(self.store.change, robot_id,
            task_id, "scheduled", "submitting", now,
            checkpoint=checkpoint, result=checkpoint["result"])
        if not changed:
            return current
        self._publish(robot_id, current)
        try:
            result = await client.submit_text(
                "【已到期的单次提醒，非当前用户发言】请自然简短提醒：" +
                checkpoint["result"]["text"], source="external", expected_identity=row)
        except Exception as exc:
            # No provider-level idempotency token: never re-submit a possibly
            # sent message. A visible unknown result is safer than duplication.
            status, reason, turn = "send_unknown", type(exc).__name__, None
        else:
            status, reason, turn = "submitted_unknown", None, result["turn_id"]
        _, updated = await asyncio.to_thread(self.store.change, robot_id,
            task_id, "submitting", status, self._now(), reason=reason, turn_id=turn)
        if updated:
            self._publish(robot_id, updated)
        return updated

    async def close(self):
        self.running = False
        for task in self.workers.values():
            task.cancel()
        if self.workers:
            await asyncio.gather(*self.workers.values(), return_exceptions=True)
        self.workers.clear()
        self.armed.clear()
