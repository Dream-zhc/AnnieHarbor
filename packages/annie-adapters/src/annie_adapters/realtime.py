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
                   memory: bool = False, actions: tuple[str, ...] = (),
                   expressions: bool = False, weather: bool = False,
                   reminders: bool = False,
                   action_descriptions: dict[str, str] | None = None) -> dict:
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
    if memory:
        session["tools"].append({
            "type": "function",
            "function": {
                "name": "recall_memory",
                "description": "Look up confirmed long-term user preferences or experiences ONLY when "
                               "the user's current question needs an older memory. "
                               "Never invent a memory when this tool returns no evidence.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Short relevant memory query"},
                    },
                    "required": ["query"],
                },
            },
        })
        for action, description in (
            ("forget_memory", "Forget ONLY a long-term memory when the user explicitly asks to forget it."),
            ("correct_memory", "Correct a long-term memory when the user explicitly provides a new fact."),
        ):
            properties = {
                "memory_id": {"type": "string", "description": "Exact ID from recall_memory"},
                "version": {"type": "integer", "description": "Exact version from recall_memory"},
            }
            required = ["memory_id", "version"]
            if action == "correct_memory":
                properties["text"] = {"type": "string", "description": "User-confirmed corrected fact"}
                required.append("text")
            session["tools"].append({
                "type": "function",
                "function": {
                    "name": action,
                    "description": description + " First recall the exact ID/version; do not guess.",
                    "parameters": {
                        "type": "object", "properties": properties, "required": required,
                    },
                },
            })
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
                    "For ordinary scripts, ONLY when the CURRENT user explicitly requests "
                    "that physical action. The five expressive scripts surprise, smirk, "
                    "proud, skeptical and shy may ALSO be selected once when the CURRENT "
                    "lighthearted conversation strongly fits their catalog descriptions, "
                    "without an explicit command. Do not infer an action from earlier "
                    "turns, force one every turn, or use playful expressions for distress, "
                    "serious news, negation or cancellation. shy is a different facial "
                    "script from shy_retract. awaiting_audio_anchor is queued until "
                    "the first TTS PCM; do not say it has been sent yet. "
                    "This project's execution criterion is successful publication "
                    "to its Room; no motor completion receipt is required. "
                    "Do not tell the user the device is awaiting execution. "
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
    if expressions:
        session["tools"].append({
            "type": "function",
            "function": {
                "name": "set_expression",
                "description": (
                    "ONLY when explicitly asked to show a facial expression. "
                    "Queued means awaiting Room publication; successful publication "
                    "counts as executed for this project's user-facing action status."
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
    if reminders:
        session["tools"].append({
            "type": "function",
            "function": {
                "name": "create_reminder",
                "description": (
                    "Create a real one-time reminder ONLY when the CURRENT user explicitly "
                    "asks to be reminded. Call this tool BEFORE claiming a reminder is set. "
                    "Convert a stated relative delay to seconds; 1..86400 only. "
                    "Do not invent a time; ask when if not specified. "
                    "Only status=scheduled with task_id confirms successful creation. "
                    "A conversational promise alone does NOT create a reminder."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "What to remind the user to do"},
                        "delay_seconds": {"type": "integer", "minimum": 1,
                                          "maximum": 86400,
                                          "description": "Delay relative to now in seconds"},
                    },
                    "required": ["text", "delay_seconds"],
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
