"""Qwen-Audio 3.0 TTS Flash wire protocol (NOT the Qwen Realtime protocol)."""

import asyncio
import json
from urllib.parse import urlparse
from uuid import uuid4

from annie_core.speech_style import SpeechStyle, validate_instruction, validate_tts_number
from websockets.asyncio.client import connect

MODEL = "qwen-audio-3.0-tts-flash"
DEFAULT_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
# Exact raw WS enum, distinct from the broader ASR/reply-language state.
# https://help.aliyun.com/zh/model-studio/qwen-audio-tts-client-events
TTS_LANGUAGE_HINTS = frozenset({"zh", "en", "fr", "de", "ja", "ko", "ru", "pt", "th",
                                "id", "vi", "es", "it", "ms", "fil", "ar"})


def validate_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "wss" or not parsed.netloc or parsed.path != "/api-ws/v1/inference":
        raise ValueError("TTS endpoint must use wss://.../api-ws/v1/inference")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("TTS endpoint must not contain credentials, query or fragment")
    return url


def command(action: str, task_id: str, *, text: str = "", voice: str = "", sample_rate: int = 24000,
            style: SpeechStyle | None = None, instruction: str | None = None,
            rate: float | None = None, pitch: float | None = None, volume: int | None = None,
            language_hint: str | None = None, one_shot: bool = False):
    if action not in {"run-task", "continue-task", "finish-task"}:
        raise ValueError("unsupported TTS command")
    controls = {"instruction": instruction, "rate": rate, "pitch": pitch, "volume": volume}
    supplied = any(value is not None for value in controls.values())
    if type(one_shot) is not bool:
        raise ValueError("one_shot must be boolean")
    if action != "run-task" and (style is not None or supplied or language_hint is not None or one_shot):
        raise ValueError("TTS style parameters are only valid on run-task")
    if style is not None and (not isinstance(style, SpeechStyle) or supplied or language_hint is not None):
        raise ValueError("TTS style must be SpeechStyle and cannot be mixed with raw parameters")
    if language_hint is not None and language_hint not in TTS_LANGUAGE_HINTS:
        raise ValueError("unsupported TTS language_hint")
    if style is not None:
        controls = {"instruction": style.instruction, "rate": style.rate,
                    "pitch": style.pitch, "volume": style.volume}
    if controls["instruction"] is not None:
        validate_instruction(controls["instruction"])
    for name in ("rate", "pitch"):
        if controls[name] is not None:
            validate_tts_number(name, controls[name], .5, 2.0)
    if controls["volume"] is not None:
        validate_tts_number("volume", controls["volume"], 0, 100, integer=True)
    if action == "run-task":
        payload = {
            "task_group": "audio", "task": "tts", "function": "SpeechSynthesizer", "model": MODEL,
            "parameters": {
                "text_type": "PlainText", "voice": voice, "format": "pcm",
                "sample_rate": sample_rate, "enable_ssml": one_shot,
            },
            "input": {},
        }
        # Raw WebSocket uses rate/pitch; speech_rate/pitch_rate belong to the SDK.
        payload["parameters"].update({key: value for key, value in controls.items()
                                      if value is not None})
        if one_shot:
            # Matches DashScope SpeechSynthesizer.call(): one continue-task with
            # enable_ssml=True, allowing emotion tags in a complete sentence.
            # The raw WS header remains duplex per Alibaba's wire specification.
            payload["parameters"].update({"seed": 42, "type": 0})
        if style is not None:
            # The official list has zh but not yue; Cantonese is an instruction.
            hint = "zh" if style.language == "yue" else style.language
            if hint in TTS_LANGUAGE_HINTS:
                payload["parameters"]["language_hints"] = [hint]
        elif language_hint is not None:
            payload["parameters"]["language_hints"] = [language_hint]
    else:
        payload = {"input": {"text": text} if action == "continue-task" else {}}
    return {"header": {"action": action, "task_id": task_id, "streaming": "duplex"}, "payload": payload}


class QwenTtsSocket:
    """One in-flight task per connection; binary data is raw PCM, JSON is control."""

    def __init__(self, url: str, key: str):
        self.url = validate_url(url)
        self.key = key
        self.socket = None

    async def open(self):
        self.socket = await connect(
            self.url,
            additional_headers={"Authorization": f"Bearer {self.key}"},
            open_timeout=8, close_timeout=2, max_size=2 * 1024 * 1024,
        )
        return self

    async def send(self, event: dict):
        if self.socket is None:
            raise ConnectionError("TTS is disconnected")
        await self.socket.send(json.dumps(event, ensure_ascii=False))

    async def recv(self):
        if self.socket is None:
            raise ConnectionError("TTS is disconnected")
        data = await self.socket.recv()
        return data if isinstance(data, bytes) else json.loads(data)

    async def close(self):
        if self.socket is not None:
            socket, self.socket = self.socket, None
            # Mirrors Realtime: a proxy may indefinitely block close handshake.
            socket.transport.abort()


def task_id() -> str:
    return uuid4().hex


class RoomTtsPublisher:
    """One verified Room channel per connection; no cross-robot routing."""

    def __init__(self, room_config, room_id: str, socket_factory=connect, *, channel="tts"):
        if channel not in {"tts", "main", "voice", "display"}:
            raise ValueError("only configured tts/main/voice/display outgoing channels are allowed")
        self.url = room_config.url_for(room_id, channel)
        self.room_id = room_id
        self.channel = room_config.channels[channel]
        self.timeout = room_config.connect_timeout_sec
        self.socket_factory = socket_factory
        self.socket = None
        self.lock = asyncio.Lock()

    async def _ensure_connected(self):
        if self.socket is None:
            from annie_adapters import parse_room_welcome

            ws = await self.socket_factory(
                self.url, open_timeout=self.timeout, close_timeout=2,
            )
            try:
                async with asyncio.timeout(self.timeout):
                    raw = await ws.recv()
                if not isinstance(raw, str):
                    raise ValueError("Room WELCOME must be JSON")
                welcome = parse_room_welcome(json.loads(raw))
                if welcome.room != self.room_id or welcome.robot_id != self.channel:
                    raise ValueError("Room publisher WELCOME identity mismatch")
                self.socket = ws
            except BaseException:
                ws.transport.abort()
                raise

    async def open(self):
        """Optional prewarm for a motion tied to the FIRST already-moving PCM."""
        async with self.lock:
            await self._ensure_connected()

    async def publish(self, message: dict):
        await self.publish_if(message, lambda: True)

    async def publish_if(self, message: dict, permitted):
        """Check an expression's first-PCM barrier under the same socket lock as PCM."""
        async with self.lock:
            await self._ensure_connected()
            if not permitted():
                return False
            try:
                async with asyncio.timeout(2):
                    await self.socket.send(json.dumps(message, ensure_ascii=False, separators=(",", ":")))
            except BaseException:
                await self.close()
                raise
            return True

    async def close(self):
        if self.socket is not None:
            ws, self.socket = self.socket, None
            ws.transport.abort()
