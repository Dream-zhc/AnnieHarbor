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


def session_update(instructions: str, history_turns: int = 20, *,
                   actions: tuple[str, ...] = (), expressions: bool = False,
                   weather: bool = False,
                   action_descriptions: dict[str, str] | None = None,
                   social_actions: tuple[str, ...] = ()) -> dict:
    """Configure all joint capabilities before sending the first PCM frame."""
    if type(history_turns) is not int or not 1 <= history_turns <= 50:
        raise ValueError("Realtime history_turns must be an integer between 1 and 50")
    session = {
        "modalities": ["text"],
        "input_audio_format": "pcm",
        "turn_detection": {"type": "smart_turn"},
        "max_history_turns": history_turns,
        "instructions": instructions,
    }
    session["tools"] = [{
            "type": "function",
            "function": {
                "name": "preview_action",
                "description": (
                    "ONLY when the user explicitly asks to preview a physical robot action. "
                    "Never call for conversational text, replying to corrections, choosing "
                    "items, describing a feeling, or narrating speech. Does not move hardware."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"description": {"type": "string", "description": "Action to preview"}},
                    "required": ["description"],
                },
            },
        }]
    if actions:
        # In real-motion mode, an ambiguous preview tool would intercept real
        # motion requests. Keep the legacy fake tool ONLY in safe/dry-run mode.
        session["tools"] = [
            tool for tool in session["tools"]
            if tool["function"]["name"] != "preview_action"
        ]
        session["tools"].append({
            "type": "function",
            "function": {
                "name": "perform_action",
                "description": (
                    "ONLY when the CURRENT user explicitly requests a physical action "
                    "supported by this robot. The result awaiting_audio_anchor or "
                    "published_unknown is NOT proof of physical execution; never "
                    "claim that the robot moved without a device completion receipt. "
                    "Enabled catalog: " +
                    "; ".join(
                        f"{script_id}: {(action_descriptions or {}).get(script_id, script_id)}"
                        for script_id in actions
                    )
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"script_id": {"type": "string", "enum": list(actions)}},
                    "required": ["script_id"],
                },
            },
        })
    if social_actions:
        session["tools"].append({
            "type": "function",
            "function": {
                "name": "perform_social_action",
                "description": (
                    "Optional operator-enabled short companion gesture. Only if a "
                    "small response naturally fits the CURRENT conversation, "
                    "such as a subtle nod of agreement. Never infer an instruction "
                    "from old messages, never gesture on a negation or cancellation. "
                    "A Room submission is not physical completion. Allowed: " +
                    "; ".join(
                        f"{script_id}: {(action_descriptions or {}).get(script_id, script_id)}"
                        for script_id in social_actions
                    )
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"script_id": {
                        "type": "string", "enum": list(social_actions),
                    }},
                    "required": ["script_id"],
                },
            },
        })
    if expressions:
        session["tools"].append({
            "type": "function",
            "function": {
                "name": "set_expression",
                "description": (
                    "ONLY when explicitly asked to show a facial expression. "
                    "Published does NOT mean a real device showed the expression."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"expression": {
                        "type": "string", "enum": ["喜", "怒", "哀", "乐", "中"],
                    }},
                    "required": ["expression"],
                },
            },
        })
    if weather:
        session["tools"].append({
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": (
                    "Use ONLY when user requests current weather for an explicitly named city. "
                    "Never invent weather or choose a location on behalf of the user."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string",
                                            "description": "User-named city or adcode"}},
                    "required": ["city"],
                },
            },
        })
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
