"""Async reb_display relay. Never awaited by speech, TTS, or the PCM pump."""

import asyncio
import logging
import time

logger = logging.getLogger(__name__)

QUEUE_LIMIT = 32
TEXT_LIMIT = 2000

# Kinds that produce a display.event, plus wake-touching kinds that only refresh wake.state.
DISPLAY_KINDS = frozenset({
    "transcript.delta", "transcript.final_preview",
    "text.delta", "text.done",
    "turn.started", "turn.done",
    "action.state", "session.ready", "session.error",
})
WAKE_TOUCH_KINDS = frozenset({
    "speech.started", "speech.stopped", "transcript.done",
    "tts.playback.drained", "wakeword.detected", "wakeword.sleep_command",
    "wakeword.error", "session.ready", "turn.done",
})
DISPLAY_NOTIFY_KINDS = DISPLAY_KINDS | WAKE_TOUCH_KINDS

_LABELS = {
    "transcript.delta": "用户语音识别中间文本",
    "transcript.final_preview": "用户语音识别待确认文本",
    "text.delta": "机器人回复中间文本",
    "text.done": "机器人回复文本结束",
    "turn.started": "一轮对话开始",
    "turn.done": "一轮对话结束",
    "wake.state": "唤醒状态",
    "action.state": "动作或表情指令状态",
    "session.ready": "实时模型已连接",
    "session.error": "实时模型异常",
}


def _clip(value, limit=TEXT_LIMIT):
    if value is None:
        return ""
    return str(value)[:limit]


def wake_fields(client):
    """Public wake lamp only. Detector paths and thresholds stay inside the process."""
    wake = getattr(client, "wake", None)
    if wake is None:
        return {"enabled": False, "state": None, "candidate_speech": False,
                "deadline_remaining_ms": None, "last_keyword": None, "error": None}
    snap = wake.snapshot()
    return {
        "enabled": True,
        "state": snap.get("state"),
        "candidate_speech": bool(snap.get("candidate_speech")),
        "deadline_remaining_ms": snap.get("deadline_remaining_ms"),
        "last_keyword": snap.get("last_keyword"),
        "error": snap.get("error"),
    }


def _wake_key(fields):
    remaining = fields.get("deadline_remaining_ms")
    bucket = None if remaining is None else int(remaining) // 1000
    return (fields.get("enabled"), fields.get("state"), fields.get("candidate_speech"),
            fields.get("error"), fields.get("last_keyword"), bucket)


def build_display_message(kind, event, robot_id, room_id):
    """One display.event, or None when this internal kind is not for the screen."""
    extra = None
    if kind == "transcript.delta":
        extra = {"item_id": event.get("item_id"), "text": _clip(event.get("text")),
                 "stash": _clip(event.get("stash"))}
    elif kind == "transcript.final_preview":
        extra = {"item_id": event.get("item_id"), "text": _clip(event.get("text"))}
    elif kind == "text.delta":
        extra = {"turn_id": event.get("turn_id"), "text": _clip(event.get("text")),
                 "delta": _clip(event.get("delta"))}
    elif kind == "text.done":
        extra = {"turn_id": event.get("turn_id"), "text": _clip(event.get("text"))}
    elif kind == "turn.started":
        extra = {"turn_id": event.get("turn_id"), "user_text": _clip(event.get("user_text"))}
    elif kind == "turn.done":
        extra = {"turn_id": event.get("turn_id"), "text": _clip(event.get("text")),
                 "status": event.get("status")}
    elif kind == "action.state":
        extra = {"call_id": event.get("call_id"), "turn_id": event.get("turn_id"),
                 "action_kind": event.get("action_kind"), "action_value": event.get("action_value"),
                 "status": event.get("status"), "generation": event.get("generation"),
                 "real_device_executed": False}
    elif kind == "session.ready":
        extra = {}
    elif kind == "session.error":
        extra = {"error": _clip(event.get("error"), 200)}
    if extra is None:
        return None
    return {
        "type": "display.event",
        "description": _LABELS[kind],
        "ts": event.get("ts_ms"),
        "data": {"sender": "brain", "robot_id": robot_id, "room_id": room_id,
                 "event": kind, **extra},
    }


class DisplayOutbox:
    """One worker per robot. Partial ASR/reply text keeps only the latest copy."""

    def __init__(self, publisher, robot_id, room_id, wake_reader):
        self._publisher = publisher
        self.robot_id = robot_id
        self.room_id = room_id
        self._wake_reader = wake_reader
        self._queue = asyncio.Queue(maxsize=QUEUE_LIMIT)
        self._latest = {}
        self._queued = set()
        self._wake_key = None
        self._task = None
        self._closed = False

    def observe(self, kind, event):
        message = build_display_message(kind, event, self.robot_id, self.room_id)
        if message is not None:
            if kind in {"transcript.delta", "text.delta"}:
                item = event.get("item_id") or event.get("turn_id") or ""
                self._offer_partial(f"{kind}:{item}", message)
            else:
                self._offer_once(message)
        if kind in WAKE_TOUCH_KINDS or kind in {"turn.started", "transcript.delta"}:
            self.note_wake()

    def note_wake(self):
        if self._closed:
            return
        fields = self._wake_reader()
        key = _wake_key(fields)
        if key == self._wake_key:
            return
        self._wake_key = key
        self._offer_partial("wake.state", {
            "type": "display.event",
            "description": _LABELS["wake.state"],
            "ts": time.time_ns() // 1_000_000,
            "data": {"sender": "brain", "robot_id": self.robot_id, "room_id": self.room_id,
                     "event": "wake.state", **fields},
        })

    def _offer_partial(self, key, message):
        if self._closed:
            return
        self._latest[key] = message
        if key in self._queued:
            return
        self._queued.add(key)
        if not self._put(key):
            self._queued.discard(key)

    def _offer_once(self, message):
        if self._closed:
            return
        self._put(message)

    def _put(self, token):
        self._ensure_worker()
        try:
            self._queue.put_nowait(token)
            return True
        except asyncio.QueueFull:
            pass
        try:
            dropped = self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return False
        if isinstance(dropped, str):
            self._queued.discard(dropped)
        try:
            self._queue.put_nowait(token)
            return True
        except asyncio.QueueFull:
            return False

    def _ensure_worker(self):
        if self._task is not None and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task = loop.create_task(self._run(), name=f"display:{self.robot_id}")

    async def _run(self):
        while not self._closed:
            token = await self._queue.get()
            if isinstance(token, str):
                self._queued.discard(token)
                message = self._latest.pop(token, None)
            else:
                message = token
            if message is None or self._closed:
                continue
            try:
                await self._publisher.publish(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("display publish robot=%s failed: %s",
                               self.robot_id, type(exc).__name__)

    async def close(self):
        self._closed = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        close = getattr(self._publisher, "close", None)
        if close is not None:
            await close()
