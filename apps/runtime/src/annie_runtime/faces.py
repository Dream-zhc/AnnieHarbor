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
from functools import lru_cache

from annie_adapters.face_registry import TERMINAL_STATES
from annie_core import NormalizedEvent
from pypinyin import Style, pinyin

FACE_NAME = re.compile(r"[一-鿿A-Za-z0-9·]{1,20}")
POLL_SECONDS = 1.0
DEADLINE_SECONDS = 135.0  # the vision side gives up after 120 s
DELETE_CHECK_SECONDS = 10.0
RESULT_TTL_MS = 30_000  # Annie may still be talking when it finishes
FORGET_CONFIRM_SECONDS = 60.0  # her “确定要我忘掉你吗” stays open this long
FORGOTTEN_SECONDS = 5.0  # the room view drops a name after 3 observations without it, about 3 s
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


def _fuzzy(syllable):
    """ASR on 11-02 wrote 康兴 as 康星 and 康欣: z/zh, c/ch, s/sh, n/l and -n/-ng are one sound."""
    syllable = re.sub(r"^n", "l", re.sub(r"^([zcs])h", r"\1", syllable))
    return re.sub(r"([aei])ng$", r"\1n", syllable)


@lru_cache(maxsize=4096)
def _readings(char):
    if not "\u4e00" <= char <= "\u9fff":
        return frozenset({char.lower()})
    rows = pinyin(char, style=Style.NORMAL, heteronym=True, errors="ignore")
    return frozenset(_fuzzy(reading) for reading in rows[0]) if rows else frozenset({char})


def _initials(char):
    return {reading[:1] if reading[:1] in "bpmfdtlgkhjqxrzcsyw" else ""
            for reading in _readings(char)}


def sounds_alike(a, b):
    """Same length and every character may be heard as the other (康星 for 康兴)."""
    return len(a) == len(b) and all(x == y or _readings(x) & _readings(y) for x, y in zip(a, b))


def _misheard(a, b):
    """One character heard as another that starts alike (玉香 for 玉箫): a slip, not a name."""
    if len(a) != len(b) or len(a) < 2:
        return False
    off = [(x, y) for x, y in zip(a, b) if x != y and not _readings(x) & _readings(y)]
    return len(off) <= 1 and all(_initials(x) & _initials(y) for x, y in off)


def _spans(texts, size):
    return [text[start:start + size] for text in texts for start in range(len(text) - size + 1)]


def heard_name(name, texts, earlier=()):
    """How the user said this name in texts (ASR's spelling), or None if they did not.

    The model may write what it heard as it knows it: a homophone (康星 -> 康兴), or
    a name said earlier in this chat with one syllable misheard now (玉香 -> 玉箫).
    """
    if any(name in text for text in texts):
        return name
    spans = _spans(texts, len(name))
    alike = next((span for span in spans if sounds_alike(name, span)), None)
    if alike is None and any(name in text for text in earlier):
        alike = next((span for span in spans if _misheard(name, span)), None)
    return alike


def same_person(name, names):
    """The one of names this spoken name stands for, as written there; None if none."""
    return next((known for known in names if known == name), None) or next(
        (known for known in names if sounds_alike(known, name)), None)


def _known_as_other(name, known_as, *, crowded):
    """The face a registration would file is one she knows: the picture's name stands."""
    if crowded:
        say = (f"你正看着的人你认得是{known_as}，以画面为准：不记，也不要用{name}称呼谁。"
               "画面里人多，说话的可能不是TA：用一句话请要记的人站到你正前方、正对着你，再说一次。")
    else:
        say = (f"画面里你认得TA是{known_as}，以画面为准：不记，也不要用{name}称呼TA。"
               f"用一句话告诉TA你认得TA是{known_as}；名字要是记错了，只能先让你忘掉TA的脸，再重新记。")
    return {"status": "known_as_other", "name": name, "known_as": known_as, "instruction": say}


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
        self.forgotten = {}  # robot_id -> (name, monotonic) she was just told to forget
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

    def _unforgotten(self, robot_id, view):
        """The view without a name she was just told to forget: the picture keeps it a few
        seconds, and forgetting then filing again is how a wrong name is corrected."""
        gone = self.forgotten.get(robot_id)
        if view is None or gone is None or time.monotonic() - gone[1] > FORGOTTEN_SECONDS:
            return view
        return {**view, "names": [known for known in view["names"] if known != gone[0]],
                "gaze_name": None if view["gaze_name"] == gone[0] else view["gaze_name"]}

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
        view = self._unforgotten(robot_id, self._view(robot_id))
        result = await self._remember(robot_id, name, view)
        # What she saw decides whether a name is filed; 11-03 16:58 left no trace of it.
        people = (None, None, None) if view is None else (
            view["count"], len(view["names"]), view["gaze_name"] is not None)
        logger.info("face remember robot=%s status=%s people=%s known=%s gaze_known=%s",
                    robot_id, result["status"], *people)
        return result

    async def _remember(self, robot_id, name, view):
        registry = self.registries[robot_id]
        if robot_id in self.active:
            return {"status": "busy", "name": self.active[robot_id]["name"],
                    "instruction": "你正在记另一个人的脸，请对方等几秒再说。"}
        if view is not None and view["count"] == 0:
            return {"status": "no_person",
                    "instruction": "你现在看不到人。请对方站到你面前、正对着你，再说一次。"}
        # 11-02: 康兴 sat at the side, recognised, while she looked at someone else;
        # “我叫康星” is him, not a new face for whoever she looks at.
        seen = same_person(name, view["names"]) if view is not None else None
        if seen is not None:
            return {"status": "recognized", "name": seen,
                    "instruction": (f"你认得TA：画面里的{seen}就是TA"
                                    + (f"（“{name}”是同音的写法）" if seen != name else "")
                                    + f"。不用再记，用一句话告诉TA你认得TA，叫TA{seen}。")}
        # Only a face she does not know yet is filed. One she knows keeps the name the
        # picture shows, whatever name is said: a wrong one is forgotten and filed again,
        # never renamed (user rule 11-03). The only one in view is the face she would file
        # even without the gaze flag, which a face held over a missed frame loses.
        known_as = None
        if view is not None:
            known_as = view["gaze_name"] or (
                view["names"][0] if view["count"] == 1 and view["names"] else None)
        if known_as and view["count"] == 1:
            return _known_as_other(name, known_as, crowded=False)
        known = await registry.people()
        if not known["ok"]:
            return {"status": "unavailable", "reason": known["error"],
                    "instruction": "现在记不了人脸，简短告诉对方稍后再试。"}
        # 11-02 19:07: 张林, long filed, said “记住我” while she looked at 康兴.
        filed = same_person(name, [person["name"] for person in known["people"]])
        if filed is not None:
            return {"status": "already_known", "name": filed,
                    "instruction": (f"你记过{filed}的脸，只是现在没认出TA（可能没正对着你、离得远或光线暗）。"
                                    "不用再记，用一句话请TA正对着你、离近一点。")}
        # With others in view the speaker may not be the one she looks at, and that face
        # is not filed under this name either.
        if known_as:
            return _known_as_other(name, known_as, crowded=True)
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

    async def forget(self, robot_id, name, *, confirmed=True):
        """Only someone Annie recognises in front of her right now can be forgotten;
        unless confirmed, she only asks them once first (user decision 11-03)."""
        view = self._view(robot_id)
        seen = same_person(name, view["names"]) if view is not None else None
        if seen is None:
            known = await self.registries[robot_id].people()
            if known["ok"] and same_person(
                    name, [person["name"] for person in known["people"]]) is None:
                return {"status": "not_known",
                        "instruction": "你没记过TA的脸，本来就认不出TA，不用忘。用一句话告诉TA；不要复述这个名字。"}
            return {"status": "not_in_view",
                    "instruction": ("现在没在面前认出TA（可能没正对着你、离得远或光线暗），"
                                    "只能忘掉此刻在你面前、你认出的人。用一句话请TA正对着你、离近一点再说一次。")}
        if not confirmed:
            return {"status": "confirm", "name": seen,
                    "instruction": (f"还没有忘。用一句很短的话问TA确定要你忘掉{seen}的脸吗，忘了就认不出TA了；"
                                    "TA下一句答应（嗯、对、确定、忘吧，或再说一遍要忘）就马上再调用一次forget_face，"
                                    "name不变，不要只回一句好的；不答应就不忘。")}
        return await self.delete(robot_id, seen)

    async def delete(self, robot_id, name):
        """Forget a face she has already asked about."""
        result = await self.registries[robot_id].delete(name)
        if not result["ok"]:
            return {"status": "unavailable", "reason": result["error"],
                    "instruction": "现在删不了，简短告诉对方稍后再试。"}
        self._publish(robot_id, "face.delete.requested", name=name)
        self.forgotten[robot_id] = (name, time.monotonic())
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
        if self.forgotten.get(robot_id, ("",))[0] == name:
            del self.forgotten[robot_id]  # still filed, so the picture's name holds again
        self._publish(robot_id, "face.delete.unconfirmed", name=name)

    async def close(self):
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for registry in self.registries.values():
            await registry.close()
