"""Qwen-Audio 3.0 TTS Flash wire protocol (NOT the Qwen Realtime protocol)."""

import asyncio
import json
from urllib.parse import urlparse
from uuid import uuid4

from websockets.asyncio.client import connect

MODEL = "qwen-audio-3.0-tts-flash"
DEFAULT_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"


def validate_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "wss" or not parsed.netloc or parsed.path != "/api-ws/v1/inference":
        raise ValueError("TTS endpoint must use wss://.../api-ws/v1/inference")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("TTS endpoint must not contain credentials, query or fragment")
    return url


def command(action: str, task_id: str, *, text: str = "", voice: str = "", sample_rate: int = 24000):
    if action not in {"run-task", "continue-task", "finish-task"}:
        raise ValueError("unsupported TTS command")
    if action == "run-task":
        payload = {
            "task_group": "audio", "task": "tts", "function": "SpeechSynthesizer", "model": MODEL,
            "parameters": {
                "text_type": "PlainText", "voice": voice, "format": "pcm",
                "sample_rate": sample_rate, "enable_ssml": False,
            },
            "input": {},
        }
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
        if channel not in {"tts", "main"}:
            raise ValueError("only verified tts/main outgoing channels are allowed")
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
        async with self.lock:
            await self._ensure_connected()
            try:
                async with asyncio.timeout(2):
                    await self.socket.send(json.dumps(message, ensure_ascii=False, separators=(",", ":")))
            except BaseException:
                await self.close()
                raise

    async def close(self):
        if self.socket is not None:
            ws, self.socket = self.socket, None
            ws.transport.abort()
