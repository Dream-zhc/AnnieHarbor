"""Phase 8A: isolated Room facts and wake-driven event-to-Realtime delivery."""

import asyncio
import time
from collections import deque

from annie_brain.event_policy import EventPolicy
from annie_core import RobotUnavailableError

GREETING_QUIET_SECONDS = 20  # no greeting within this long of the last conversation
# Body contact and gas cut her own reply short instead of waiting behind it.
INTERRUPTING_KINDS = frozenset({"sensor.touch", "sensor.pressure", "sensor.gas"})
RETRY_HOLD_SECONDS = 0.4  # the provider's speech start lands before the next try
RETRIES = 2


class EventService:
    def __init__(self, runtime, *, greet_cooldown_ms=30 * 60_000, habituation=False,
                 habit_quiet_ms=120_000):
        self.runtime = runtime
        self.policies = {robot_id: EventPolicy(greet_cooldown_ms=greet_cooldown_ms,
                                               habituation=habituation,
                                               habit_quiet_ms=habit_quiet_ms)
                         for robot_id in runtime.robots}
        self.pending = {robot_id: deque(maxlen=16) for robot_id in runtime.robots}
        self.wakeups = {robot_id: asyncio.Event() for robot_id in runtime.robots}
        self.tasks = {}
        self.last_context_notice = {}

    def snapshot(self, robot_id):
        data = self.policies[robot_id].snapshot(time.time_ns() // 1_000_000)
        identity = self.runtime.session_state(robot_id)
        data["events"] = [row for row in data["events"] if all(
            row[field] == identity[field] for field in
            ("chat_id", "persona_id", "session_epoch", "generation"))]
        return {**identity, **data,
                "pending_count": sum(all(row[field] == identity[field] for field in
                    ("chat_id", "persona_id", "session_epoch", "generation"))
                    for row in self.pending[robot_id]),
                "complex_task": ("one_time_reminder_available"
                    if self.runtime.reminders.running and robot_id in self.runtime.realtime
                    else "not_configured")}

    def _publish(self, robot_id, row):
        client = self.runtime.realtime.get(robot_id)
        if client:
            if row["decision"] == "context_only":
                key = robot_id, row["kind"]
                now = time.monotonic()
                if now - self.last_context_notice.get(key, -100) < 5:
                    return
                self.last_context_notice[key] = now
            client.publish("event.policy", event_kind=row["kind"],
                           event_id=row["event_id"], decision=row["decision"],
                           status=row["status"], reason=row["reason"],
                           transport=row["transport"])

    def route(self, robot_id, event, transport):
        robot = self.runtime.robots[robot_id]
        row = self.policies[robot_id].route(
            event, robot_id=robot_id, transport=transport,
            identity=robot.session_state(), now_ms=time.time_ns() // 1_000_000,
        )
        if row["decision"] == "need_response":
            inner = getattr(self.runtime, "inner_life", None)
            if inner is not None:
                inner.on_event(robot_id, event)  # her mood first, then the reply
                if event.kind == "vision.known_arrival" and row.get("prompt"):
                    # An open thread of TA's she may ask about while greeting.
                    row["prompt"] += inner.greeting_extra(
                        robot_id, str(event.payload.get("name") or "").strip())
            if not self.runtime.started or robot_id not in self.runtime.realtime:
                row.update(status="dropped", reason="realtime_unavailable")
            elif len(self.pending[robot_id]) == self.pending[robot_id].maxlen:
                row.update(status="dropped", reason="queue_full")
            else:
                row["status"] = "waiting"
                self.pending[robot_id].append(row)
                self.wake(robot_id)
        elif row["decision"] == "habituated":
            inner = getattr(self.runtime, "inner_life", None)
            if inner is not None:
                inner.on_event(robot_id, event)
            motions = getattr(self.runtime, "motions", None)
            client = self.runtime.realtime.get(robot_id)
            if motions is not None and client is not None:
                row["motion"] = motions.react_quietly(robot_id, client, row["stimulus"])
        self._publish(robot_id, row)
        return row

    @staticmethod
    def _may_interrupt(row, robot, client):
        """Never the user's own words, nor her reply to an earlier event."""
        if row["kind"] not in INTERRUPTING_KINDS or client.speech_active:
            return False
        speech = client.speech
        for turn_id in (client.turn_id, speech.turn_id if speech else None):
            turn = robot.cached_turn(turn_id) if turn_id else None
            if turn and turn.get("turn_source") == "external":
                return False
        return True

    def _retry(self, robot_id, row, reason):
        """Back in front for another try while the event is still fresh."""
        row["retries"] = row.get("retries", 0) + 1
        if row["retries"] > RETRIES or row["expires_at_ms"] <= time.time_ns() // 1_000_000:
            return False
        row.update(status="waiting", reason=reason,
                   retry_at=time.monotonic() + RETRY_HOLD_SECONDS)
        self.pending[robot_id].appendleft(row)
        self.wake(robot_id)
        return True

    def _refused(self, robot_id, row):
        if not self._retry(robot_id, row, "user_speaking"):
            row.update(status="failed", reason="user_speaking")
        self._publish(robot_id, row)

    def wake(self, robot_id):
        if not self.runtime.started or not self.pending[robot_id]:
            return
        self.wakeups[robot_id].set()
        task = self.tasks.get(robot_id)
        if task is None or task.done():
            self.tasks[robot_id] = asyncio.create_task(
                self._drain(robot_id), name=f"event-policy:{robot_id}",
            )

    async def _drain(self, robot_id):
        pending, wakeup = self.pending[robot_id], self.wakeups[robot_id]
        robot = self.runtime.robots[robot_id]
        while self.runtime.started and pending:
            row = pending[0]
            now_ms = time.time_ns() // 1_000_000
            if row["expires_at_ms"] <= now_ms:
                pending.popleft()
                row.update(status="expired", reason="ttl_expired")
                self._publish(robot_id, row)
                continue
            if any(row[k] != robot.session_state()[k] for k in
                   ("chat_id", "persona_id", "session_epoch", "generation")):
                pending.popleft()
                row.update(status="dropped", reason="session_changed")
                self._publish(robot_id, row)
                continue
            client = self.runtime.realtime.get(robot_id)
            hold = row.get("retry_at", 0) - time.monotonic()
            if client is None or not client.ready:
                blocked = "realtime_unavailable"
            elif hold > 0:
                blocked = "user_speaking"
            else:
                speech = client.speech
                blocked = robot.coordinator.event_block_reason(
                    model_busy=bool(client.speech_active or client.turn_id or client.response_id),
                    playback_active=bool(speech and speech.output_active()),
                )
                if blocked is not None and self._may_interrupt(row, robot, client):
                    await client.cancel(cancel_source="external_event")
                    row["interrupted"] = True
                    # The speaker's own state report lags the local stop: do not wait on it.
                    busy = client.speech_active or client.turn_id or client.response_id
                    blocked = "model_busy" if busy else None
                if (blocked is None and row.get("wait_for_quiet")
                        and time.monotonic() - client._last_conversation_activity_t
                        < GREETING_QUIET_SECONDS):
                    # Do not greet a newcomer into someone else's conversation;
                    # if it keeps going past the event TTL, the greeting lapses.
                    blocked = "conversation_active"
            if blocked is None:
                pending.popleft()
                row["status"] = "submitting"
                self._publish(robot_id, row)
                try:
                    # Mark this as a sensor event, NEVER as a person's words or
                    # proof that the model spoke / the speaker played it.
                    result = await client.submit_text(
                        "【外部事件，非用户发言】" + row["prompt"],
                        source="external", expected_identity=row,
                        listen_after=bool(row.get("listen_after")),
                        on_refused=lambda row=row: self._refused(robot_id, row),
                    )
                    row.update(status="submitted_unknown", turn_id=result["turn_id"])
                except RobotUnavailableError as exc:
                    # Speech started under the submission ("preempted by speech"): try again.
                    if not self._retry(robot_id, row, "user_speaking"):
                        row.update(status="failed", reason=type(exc).__name__)
                except Exception as exc:
                    row.update(status="failed", reason=type(exc).__name__)
                self._publish(robot_id, row)
                continue
            row.update(status="waiting", reason=blocked)
            self._publish(robot_id, row)
            wakeup.clear()
            timeout = (row["expires_at_ms"] - now_ms) / 1000
            try:
                await asyncio.wait_for(wakeup.wait(), min(timeout, hold) if hold > 0 else timeout)
            except asyncio.TimeoutError:
                pass

    async def close(self):
        for task in self.tasks.values():
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        self.tasks.clear()
        for pending in self.pending.values():
            pending.clear()
