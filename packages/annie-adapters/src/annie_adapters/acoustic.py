"""Qwen3 ASR Realtime observation-only protocol; no chat or robot policy."""

import base64
import json
import re
from uuid import uuid4

from websockets.asyncio.client import connect

from annie_adapters.net import cloud_proxy
from annie_adapters.realtime import realtime_url

MODEL = "qwen3-asr-flash-realtime"
LANGUAGES = frozenset("zh yue en ja de ko ru fr pt ar it es hi id th tr uk vi cs da fil fi is ms no pl sv".split())
EMOTIONS = frozenset("surprised neutral happy sad disgusted angry fearful".split())
PREFIX = "conversation.item.input_audio_transcription."


def acoustic_session_update() -> dict:
    """Use automatic language recognition, PCM16/16k mono, and server VAD."""
    return {"event_id": uuid4().hex, "type": "session.update", "session": {
        "input_audio_format": "pcm", "sample_rate": 16000,
        "input_audio_transcription": {},
        "turn_detection": {"type": "server_vad", "threshold": 0.2, "silence_duration_ms": 800},
    }}


def acoustic_audio_append(pcm: bytes) -> dict:
    if not isinstance(pcm, bytes) or not pcm or len(pcm) % 2:
        raise ValueError("Expected nonempty mono signed PCM16 bytes")
    return {"event_id": uuid4().hex, "type": "input_audio_buffer.append",
            "audio": base64.b64encode(pcm).decode("ascii")}


def _text(value, limit=2000):
    return value[:limit] if isinstance(value, str) else ""


def parse_acoustic_event(event: dict) -> dict | None:
    """Separate stable text from draft; never infer a regional dialect from zh."""
    kind = event.get("type")
    kinds = {PREFIX + "text": "partial", PREFIX + "completed": "completed",
             PREFIX + "failed": "failed", "error": "error",
             "input_audio_buffer.speech_started": "speech_started",
             "input_audio_buffer.speech_stopped": "speech_stopped"}
    if kind not in kinds:
        return None
    language, emotion = event.get("language"), event.get("emotion")
    text = event.get("transcript") if kind == PREFIX + "completed" else (
        event.get("text") if kind == PREFIX + "text" else "")
    row = {"kind": kinds[kind], "provider_item_id": _text(event.get("item_id"), 256),
           "text": _text(text),
           "stash": _text(event.get("stash")) if kind == PREFIX + "text" else "",
           "language": language if isinstance(language, str) and language in LANGUAGES else "unknown",
           "emotion": emotion if isinstance(emotion, str) and emotion in EMOTIONS else "unknown",
           "dialect": "unknown", "confidence": None}
    for field in ("audio_start_ms", "audio_end_ms"):
        value = event.get(field)
        row[field] = value if type(value) is int and value >= 0 else None
    if row["kind"] in {"error", "failed"}:
        error = event.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        row["error_code"] = code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", code) else "unknown"
    return row


class QwenASRSocket:
    """Bounded transport with immediate abort for superseded recognition streams."""

    def __init__(self, url: str, api_key: str):
        self.url = realtime_url(url, MODEL)
        self.api_key = api_key
        self.socket = None

    async def open(self):
        self.socket = await connect(
            # Direct to Aliyun; see annie_adapters.net.
            self.url, additional_headers={"Authorization": f"Bearer {self.api_key}"},
            proxy=cloud_proxy(), open_timeout=8, close_timeout=1, max_size=2 * 1024 * 1024,
        )
        return self

    async def send(self, event: dict):
        if self.socket is None:
            raise ConnectionError("ASR socket is not connected")
        await self.socket.send(json.dumps(event, ensure_ascii=False))

    async def recv(self):
        if self.socket is None:
            raise ConnectionError("ASR socket is not connected")
        event = json.loads(await self.socket.recv())
        if not isinstance(event, dict):
            raise ValueError("Expected ASR event object")
        return event

    def abort(self):
        if self.socket is not None:
            self.socket.transport.abort()

    async def close(self):
        self.abort()
        self.socket = None
