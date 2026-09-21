"""Qwen-Audio Realtime wire transport. No robot state or business policy here."""

import base64
import json
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from websockets.asyncio.client import connect

MODEL = "qwen-audio-3.0-realtime-flash"
DEFAULT_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"


def realtime_url(base_url: str, model: str = MODEL) -> str:
    parsed = urlparse(base_url)
    if parsed.scheme != "wss" or not parsed.netloc or parsed.fragment:
        raise ValueError("Realtime URL must be an absolute wss:// endpoint without fragment")
    if parsed.path != "/api-ws/v1/realtime":
        raise ValueError("Realtime URL must use /api-ws/v1/realtime")
    query = parse_qs(parsed.query)
    if set(query) - {"model"} or ("model" in query and query["model"] != [model]):
        raise ValueError("Realtime URL query must specify only the configured model")
    return urlunparse(parsed._replace(query=urlencode({"model": model})))


def session_update(instructions: str, history_turns: int = 20, *, sensor_only: bool = False) -> dict:
    """Configure all joint capabilities before sending the first PCM frame."""
    session = {
        "modalities": ["text", "audio"] if sensor_only else ["text"],
        "input_audio_format": "pcm",
        "turn_detection": {"type": "smart_turn"},
        "max_history_turns": history_turns,
        "instructions": instructions,
    }
    if not sensor_only:
        session["tools"] = [{
            "type": "function",
            "function": {
                "name": "preview_action",
                "description": "Preview an action in the developer console without moving the real robot.",
                "parameters": {
                    "type": "object",
                    "properties": {"description": {"type": "string", "description": "Action to preview"}},
                    "required": ["description"],
                },
            },
        }]
    return {
        "type": "session.update",
        "session": session,
    }


def audio_append(pcm: bytes) -> dict:
    if len(pcm) != 640:
        raise ValueError("expected one 20ms 16kHz mono signed PCM frame (640 bytes)")
    return {"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode("ascii")}


class QwenRealtimeSocket:
    def __init__(self, url: str, api_key: str):
        self.url = realtime_url(url)
        self.api_key = api_key
        self.socket = None

    async def open(self):
        # The authorization header is private; it is never logged or sent to the browser.
        self.socket = await connect(
            self.url,
            additional_headers={"Authorization": f"Bearer {self.api_key}"},
            open_timeout=8,
            close_timeout=2,
            max_size=2 * 1024 * 1024,
        )
        return self

    async def send(self, event: dict):
        if self.socket is None:
            raise ConnectionError("Realtime socket is not connected")
        await self.socket.send(json.dumps(event, ensure_ascii=False))

    async def recv(self):
        if self.socket is None:
            raise ConnectionError("Realtime socket is not connected")
        return json.loads(await self.socket.recv())

    async def close(self):
        if self.socket is not None:
            socket = self.socket
            self.socket = None
            # The proxy can hang indefinitely in websockets.close() and resist
            # cancellation, blocking the single Python process on shutdown.
            # The provider Session is discarded, so abort the transport instead
            # of waiting for a remote closing handshake.
            socket.transport.abort()
