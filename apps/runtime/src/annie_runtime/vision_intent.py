"""Ask a robot's vision service to look now (2026-10-28, docs/规范/视觉意图协议.md).

When the user asks what she sees, the runtime sends vision.intent on the Room's
reb_main once her head is where it was asked to look, and waits a bounded time
for vision.intent.result with a fresh description. route tells the vision
service which model to look with: Alibaba Cloud or its own local one. One
question per Room at a time; a late or foreign result is dropped by intent_id.
A failure only means she answers from the 1 Hz local view, as before.
"""

import asyncio
import time
from uuid import uuid4

ROUTES = ("cloud", "local")
NETWORK_MARGIN_SECONDS = 0.3
QUESTION_MAX_CHARS = 200
DESCRIPTION_MAX_CHARS = 600


class VisionIntents:
    def __init__(self, publishers, *, route="cloud", deadline_ms=3500):
        if route not in ROUTES:
            raise ValueError("vision intent route must be cloud or local")
        if type(deadline_ms) is not int or not 500 <= deadline_ms <= 15000:
            raise ValueError("vision intent deadline_ms must be 500-15000")
        self.publishers = dict(publishers)  # robot_id -> its Room's reb_main publisher
        self.route = route
        self.deadline_ms = deadline_ms
        self.pending = {}  # robot_id -> (intent_id, future)
        self.last = {}
        self.stats = {robot_id: {"sent": 0, "ok": 0, "failed": 0, "timeout": 0,
                                 "send_failed": 0, "ignored": 0}
                      for robot_id in self.publishers}

    def available(self, robot_id):
        return robot_id in self.publishers

    def snapshot(self, robot_id):
        return {"configured": self.available(robot_id), "route": self.route,
                "deadline_ms": self.deadline_ms, "stats": dict(self.stats.get(robot_id, {})),
                "last": self.last.get(robot_id)}

    async def ask(self, robot_id, conversation_id, question, *, fresh_frame, after_action=None):
        """The vision service's answer; status is ok, failed, timeout or send_failed."""
        intent_id = "vi-" + uuid4().hex[:16]
        packet = {"type": "vision.intent", "ts": time.time_ns() // 1_000_000,
                  "data": {"version": 1, "intent_id": intent_id,
                           "conversation_id": conversation_id, "route": self.route,
                           "question": str(question or "")[:QUESTION_MAX_CHARS],
                           "fresh_frame": bool(fresh_frame), "after_action": after_action,
                           "deadline_ms": self.deadline_ms, "source": "robot_main_brain"}}
        future = asyncio.get_running_loop().create_future()
        # A newer question replaces the one still waiting (the vision side drops it too).
        previous = self.pending.get(robot_id)
        if previous is not None and not previous[1].done():
            previous[1].set_result({"status": "failed", "error": "superseded"})
        self.pending[robot_id] = (intent_id, future)
        stats = self.stats[robot_id]
        started = time.perf_counter()
        try:
            try:
                await asyncio.wait_for(self.publishers[robot_id].publish(packet), 2)
            except Exception as exc:
                stats["send_failed"] += 1
                result = {"status": "send_failed", "error": type(exc).__name__}
            else:
                stats["sent"] += 1
                try:
                    result = await asyncio.wait_for(
                        future, self.deadline_ms / 1000 + NETWORK_MARGIN_SECONDS)
                except TimeoutError:
                    stats["timeout"] += 1
                    result = {"status": "timeout"}
        finally:
            if self.pending.get(robot_id, (None,))[0] == intent_id:
                del self.pending[robot_id]
        if result["status"] in {"ok", "failed"}:
            stats[result["status"]] += 1
        result = {**result, "intent_id": intent_id, "route_requested": self.route,
                  "wait_ms": round((time.perf_counter() - started) * 1000)}
        self.last[robot_id] = {key: value for key, value in result.items()
                               if key != "description"}
        return result

    def on_result(self, robot_id, message):
        """vision.intent.result from reb_main; True if it answered the question waiting."""
        data = message.get("data")
        pending = self.pending.get(robot_id)
        if (not isinstance(data, dict) or pending is None or pending[1].done()
                or data.get("intent_id") != pending[0]):
            if robot_id in self.stats:
                self.stats[robot_id]["ignored"] += 1
            return False
        description = data.get("description")
        if data.get("status") == "ok" and isinstance(description, str) and description.strip():
            result = {"status": "ok", "description": description.strip()[:DESCRIPTION_MAX_CHARS]}
        else:
            error = data.get("error")
            result = {"status": "failed",
                      "error": error[:48] if isinstance(error, str) and error else
                      "empty_description" if data.get("status") == "ok" else "unknown"}
        for key in ("route", "model"):
            if isinstance(data.get(key), str):
                result[key] = data[key][:64]
        for key in ("latency_ms", "frame_after_request_ms"):
            if type(data.get(key)) in (int, float):
                result[key] = data[key]
        pending[1].set_result(result)
        return True

    async def close(self):
        for _intent_id, future in self.pending.values():
            if not future.done():
                future.cancel()
        self.pending.clear()
        await asyncio.gather(*(publisher.close() for publisher in self.publishers.values()),
                             return_exceptions=True)
