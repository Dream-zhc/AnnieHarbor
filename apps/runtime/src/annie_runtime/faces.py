"""Face registration by conversation: "记住我，我叫小明" and "忘掉我".

The robot's vision service records the face it is looking at (its gaze target,
by default the largest face). Brain only starts that, follows it to the end,
tells the workbench, and lets Annie say how it went through the ordinary event
path (vision.face_registered). Nothing here is shown on the big screen.
"""

import asyncio
import logging
import re
import time

from annie_adapters.face_registry import TERMINAL_STATES
from annie_core import NormalizedEvent

FACE_NAME = re.compile(r"[一-鿿A-Za-z0-9·]{1,20}")
POLL_SECONDS = 1.0
DEADLINE_SECONDS = 135.0  # the vision side gives up after 120 s
DELETE_CHECK_SECONDS = 10.0
RESULT_TTL_MS = 30_000  # Annie may still be talking when it finishes
RENAME_CONFIRM_SECONDS = 60.0
logger = logging.getLogger(__name__)

_REASONS = {
    "timeout": "一直没采到清楚的正脸",
    "runtime_restarted": "视觉服务重启了",
    "unreachable": "视觉服务连不上",
}


def face_name(value):
    """A spoken name as the vision service will store it; None if unusable."""
    if not isinstance(value, str):
        return None
    name = re.sub(r"\s+", "", value)
    return name if FACE_NAME.fullmatch(name) else None


class FaceService:
    def __init__(self, runtime, registries, *, samples=18, poll_seconds=POLL_SECONDS,
                 deadline_seconds=DEADLINE_SECONDS):
        if type(samples) is not int or not 1 <= samples <= 100:
            raise ValueError("face samples must be 1-100")
        self.runtime = runtime
        self.registries = dict(registries)  # robot_id -> FaceRegistry
        self.samples = samples
        self.poll_seconds = poll_seconds
        self.deadline_seconds = deadline_seconds
        self.active = {}  # robot_id -> registration in progress
        self.last = {}  # robot_id -> the latest finished one
        self.renames = {}  # robot_id -> (known name, new name, monotonic) asked once
        self.tasks = set()

    def available(self, robot_id):
        return robot_id in self.registries

    def snapshot(self, robot_id):
        return {"configured": self.available(robot_id),
                "active": dict(self.active[robot_id]) if robot_id in self.active else None,
                "last": self.last.get(robot_id)}

    def _view(self, robot_id):
        monitor = getattr(self.runtime, "room_monitors", {}).get(robot_id)
        return monitor.people_view() if monitor is not None else None

    def _publish(self, robot_id, kind, **fields):
        client = self.runtime.realtime.get(robot_id)
        if client is not None:
            client.publish(kind, **{key: value for key, value in fields.items()
                                    if value is not None})

    def _spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def remember(self, robot_id, name):
        registry = self.registries[robot_id]
        if robot_id in self.active:
            return {"status": "busy", "name": self.active[robot_id]["name"],
                    "instruction": "你正在记另一个人的脸，请对方等几秒再说。"}
        view = self._view(robot_id)
        if view is not None and view["count"] == 0:
            return {"status": "no_person",
                    "instruction": "你现在看不到人。请对方站到你面前、正对着你，再说一次。"}
        # “我是X” alone now starts a registration (10-22), so a face she already
        # knows by another name is asked about once instead of being filed twice.
        known_as = view.get("gaze_name") if view is not None else None
        if known_as and known_as != name:
            asked = self.renames.pop(robot_id, None)
            if not (asked and asked[:2] == (known_as, name)
                    and time.monotonic() - asked[2] <= RENAME_CONFIRM_SECONDS):
                self.renames[robot_id] = (known_as, name, time.monotonic())
                return {"status": "known_as_other", "name": name, "known_as": known_as,
                        "instruction": (f"你看着的这个人你认得是{known_as}。还没有记，也不要叫TA{name}；"
                                        f"用一句话问TA是不是你认错了。TA坚持自己叫{name}、要你记，"
                                        "再调用一次就会重新记。")}
        known = await registry.people()
        if not known["ok"]:
            return {"status": "unavailable", "reason": known["error"],
                    "instruction": "现在记不了人脸，简短告诉对方稍后再试。"}
        if any(person["name"] == name for person in known["people"]):
            return {"status": "already_known", "name": name,
                    "instruction": f"你已经记得叫{name}的人了，不用再记。"}
        started = await registry.register(name, self.samples)
        if not started["ok"]:
            if started["error"] == "registration_in_progress":
                return {"status": "busy", "instruction": "正在记另一个人的脸，请对方等一会儿再说。"}
            if started["error"] == "invalid_name":
                return {"status": "invalid_name", "instruction": "这个名字记不了，请对方换个叫法。"}
            return {"status": "unavailable", "reason": started["error"],
                    "instruction": "现在记不了人脸，简短告诉对方稍后再试。"}
        face_id = started["face_id"][:64]
        self.active[robot_id] = {"face_id": face_id, "name": name, "state": "queued",
                                 "saved": 0, "target_samples": self.samples}
        self._publish(robot_id, "face.registration.started", name=name, face_id=face_id,
                      target_samples=self.samples)
        self._spawn(self._watch(robot_id, face_id, name))
        crowded = view is not None and view["count"] > 1
        return {
            "status": "started", "name": name,
            "people_in_view": view["count"] if view is not None else None,
            "instruction": (
                "开始记TA的脸了，要几秒到十几秒。用一句很短的话请TA正对着你、离近一点、保持一会儿"
                + ("，画面里人多，请其他人稍微让开" if crowded else "")
                + "。现在还没记好，不要说已经记住；记好了你会再告诉TA。"),
        }

    async def _watch(self, robot_id, face_id, name):
        registry = self.registries[robot_id]
        deadline = time.monotonic() + self.deadline_seconds
        state, reason, misses, last_saved = "failed", None, 0, None
        row = self.active[robot_id]
        try:
            while True:
                await asyncio.sleep(self.poll_seconds)
                status = await registry.registration(face_id)
                if status["ok"]:
                    misses = 0
                    state = str(status.get("state") or "")
                    for field in ("saved", "target_samples"):
                        if type(status.get(field)) is int:
                            row[field] = status[field]
                    row["state"] = state
                    if row["saved"] != last_saved:
                        last_saved = row["saved"]
                        self._publish(robot_id, "face.registration.progress", name=name,
                                      saved=row["saved"], target_samples=row["target_samples"],
                                      phase=str(status.get("phase") or "")[:24] or None)
                    if state in TERMINAL_STATES:
                        reason = str(status.get("error") or "")[:48] or None
                        break
                else:
                    misses += 1
                    if status["error"] == "registration_not_found" or misses >= 10:
                        state, reason = "failed", status["error"]
                        break
                if time.monotonic() > deadline:
                    state, reason = "failed", "timeout"
                    break
        finally:
            self.active.pop(robot_id, None)
        self.last[robot_id] = {"face_id": face_id, "name": name, "state": state,
                               "reason": reason, "saved": row["saved"],
                               "target_samples": row["target_samples"],
                               "at_ms": time.time_ns() // 1_000_000}
        self._publish(robot_id, "face.registration." + state, name=name, face_id=face_id,
                      reason=reason, saved=row["saved"], target_samples=row["target_samples"])
        logger.info("face registration %s robot=%s state=%s reason=%s",
                    face_id, robot_id, state, reason)
        if state in {"completed", "failed"}:
            # Annie says how it went; the person is usually still waiting.
            self.runtime.events.route(robot_id, NormalizedEvent(
                kind="vision.face_registered", source="vision", event_id=face_id,
                payload={"id": face_id, "name": name, "state": state,
                         "reason": _REASONS.get(reason or "", "没采到清楚的正脸")},
                ts_ms=time.time_ns() // 1_000_000, ttl_ms=RESULT_TTL_MS),
                f"face:{robot_id}")

    async def forget(self, robot_id, name):
        """Only someone Annie recognises in front of her right now can be forgotten."""
        view = self._view(robot_id)
        if view is None or name not in view["names"]:
            return {"status": "not_in_view", "name": name,
                    "instruction": "只能忘掉此刻站在你面前、你认得的人。告诉对方需要本人在你面前再说。"}
        result = await self.registries[robot_id].delete(name)
        if not result["ok"]:
            return {"status": "unavailable", "reason": result["error"],
                    "instruction": "现在删不了，简短告诉对方稍后再试。"}
        self._publish(robot_id, "face.delete.requested", name=name)
        inner = getattr(self.runtime, "inner_life", None)
        if inner is not None:
            inner.on_person_forgotten(robot_id, name)  # her diary lines about TA go too
        self._spawn(self._confirm_deleted(robot_id, name))
        return {"status": "requested", "name": name,
                "instruction": f"用一句很短的话告诉TA，你会忘掉TA的脸，以后不会再叫{name}。"}

    async def _confirm_deleted(self, robot_id, name):
        registry = self.registries[robot_id]
        deadline = time.monotonic() + DELETE_CHECK_SECONDS
        while time.monotonic() < deadline:
            await asyncio.sleep(self.poll_seconds)
            people = await registry.people()
            if people["ok"] and all(person["name"] != name for person in people["people"]):
                self._publish(robot_id, "face.deleted", name=name)
                return
        self._publish(robot_id, "face.delete.unconfirmed", name=name)

    async def close(self):
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for registry in self.registries.values():
            await registry.close()
