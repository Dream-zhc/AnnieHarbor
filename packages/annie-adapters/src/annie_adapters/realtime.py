"""Qwen-Audio Realtime wire transport. No robot state or business policy here."""

import base64
import json
import re
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from websockets.asyncio.client import connect

MODEL = "qwen-audio-3.0-realtime-flash"
DEFAULT_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
# Same Qwen-Audio Realtime wire family (3.0 flash/plus, 3.1 plus); other model
# families use different protocols and must not be selected by a typo.
_MODEL_NAME = re.compile(r"qwen-audio-\d+(?:\.\d+)*-realtime-(?:flash|plus)")


def validate_model(model: str) -> str:
    if not isinstance(model, str) or not _MODEL_NAME.fullmatch(model):
        raise ValueError("Realtime model must be a qwen-audio-*-realtime-flash/plus model")
    return model


def url_model(url: str) -> str:
    """The model already pinned in a validated Realtime URL, else the default."""
    return (parse_qs(urlparse(url).query).get("model") or [MODEL])[0]


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
                   reminders: bool = False, language_preferences: bool = False,
                   voice_selection: bool = False,
                   sleep: bool = False,
                   persona_labels: tuple[str, ...] = (),
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
    if voice_selection:
        session["tools"].append({
            "type": "function", "function": {
                "name": "select_voice",
                "description": (
                    "Only when the CURRENT user explicitly asks to switch TTS voices; never because "
                    "emotion, dialect, speaker or persona changed. If no voice is named, still call "
                    "with a catalog name: needs_selection means ask which. scope=chat normally, "
                    "scope=robot only for explicit permanent/all-chats requests. ONLY status=selected "
                    "means the voice changed (from the next reply); otherwise NOTHING changed, so "
                    "never say it did."
                ),
                "parameters": {"type": "object", "properties": {
                    "voice_name": {"type": "string", "description": "Exact configured voice name or 默认音色"},
                    "scope": {"type": "string", "enum": ["chat", "robot"]},
                }, "required": ["voice_name", "scope"]},
            },
        })
    if persona_labels:
        if (not isinstance(persona_labels, tuple) or not persona_labels or len(persona_labels) > 12
                or any(type(label) is not str or not 1 <= len(label) <= 40 for label in persona_labels)):
            raise ValueError("persona labels must be a short tuple of names")
        session["tools"].append({
            "type": "function", "function": {
                "name": "select_persona_style",
                "description": (
                    "Only when the CURRENT user explicitly asks to change THIS chat's personality "
                    "style (not voice, dialect or language). Do not create a new chat. Styles: "
                    + "、".join(persona_labels) + ". Pass the exact label they named, or an empty "
                    "string if they named none (needs_selection: ask once which). ONLY "
                    "status=selected means it changed. Call before speaking; after selected, answer "
                    "them in one new sentence in that style instead of reciting a confirmation."
                ),
                "parameters": {"type": "object", "properties": {
                    "style_name": {"type": "string",
                                   "description": "Exact style label, or empty when the user did not name one"},
                }, "required": ["style_name"]},
            },
        })
    if memory:
        session["tools"].append({
            "type": "function",
            "function": {
                "name": "recall_memory",
                "description": "Look up confirmed long-term user preferences or experiences ONLY when "
                               "the answer is not already in this conversation. If it returns no "
                               "evidence, say you don't remember instead of inventing one.",
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
                    "Physical robot action. Ordinary scripts ONLY when the CURRENT user explicitly "
                    "asks for that action. Expressive scripts surprise, smirk, proud, skeptical, shy "
                    "may also be used once when the CURRENT lighthearted talk clearly fits their "
                    "catalog description; never for distress, serious news, negation or "
                    "cancellation, and never inferred from earlier turns. These five REQUIRE "
                    "anchor_text: an exact word you will say in this reply (anchor_occurrence picks "
                    "which occurrence, from 1); the action fires when your speech reaches it and is "
                    "dropped if you never say it. Other scripts may omit it and start with your "
                    "first audio. shy is a facial script, not shy_retract. Never guess milliseconds "
                    "or samples, never speak tool fields. accepted means scheduled: reply naturally "
                    "at once; never narrate delivery, hardware state, imagined failures or motor "
                    "completion. Enabled catalog: " +
                    "; ".join(
                        f"{script_id}: {(action_descriptions or {}).get(script_id, script_id)}"
                        for script_id in actions
                    )
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "script_id": {"type": "string", "enum": list(actions)},
                        "anchor_text": {"type": "string", "minLength": 1, "maxLength": 80,
                                        "description": "Required for surprise/smirk/proud/skeptical/shy. Exact word in your subsequent spoken reply, original spelling and whitespace."},
                        "anchor_occurrence": {"type": "integer", "minimum": 1, "maximum": 8000,
                                              "description": "1-based nonoverlapping occurrence in the original reply; only with anchor_text, default 1."},
                    },
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
                    "ONLY when explicitly asked to show a facial expression. accepted means "
                    "scheduled: reply naturally without narrating delivery or hardware state."
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
                    "Use ONLY when the CURRENT user asks about current weather. Pass the city "
                    "the user named. If they named no city, omit city: the robot's configured "
                    "home city is used and the result names it, so say which city it is. "
                    "Never invent weather."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string",
                                            "description": "User-named city or adcode; omit if none"}},
                    "required": [],
                },
            },
        })
    if reminders:
        session["tools"].append({
            "type": "function",
            "function": {
                "name": "create_reminder",
                "description": (
                    "Create a real one-time reminder ONLY when the CURRENT user explicitly asks to "
                    "be reminded, before saying it is set. Convert the stated relative delay to "
                    "seconds (1..86400); ask when if no time was given. Only status=scheduled "
                    "with task_id means it exists; a spoken promise alone creates nothing."
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
    if language_preferences:
        for name, description, properties, required in (
            ('set_language_preference',
             'Save ONLY a CURRENT explicit user language preference (scope=chat: this Chat; '
             'scope=robot: all future Chats of this robot). Never save an inference. Only '
             'status=set means saved.',
             {'scope': {'type': 'string', 'enum': ['chat', 'robot']},
              'language': {'type': 'string'}, 'dialect': {'type': 'string'}},
             ['scope', 'language', 'dialect']),
            ('clear_language_preference',
             'Clear a saved language preference ONLY on an explicit CURRENT request naming '
             'Chat or robot scope. Only status=cleared means cleared.',
             {'scope': {'type': 'string', 'enum': ['chat', 'robot']}}, ['scope']),
            ('select_reply_style',
             'OPTIONAL, never needed before speaking: pick reply language/emotion. Dialect '
             'inference needs an EXACT phrase the user said as evidence, never a place name. '
             'Not saved; a late choice affects the next reply only.',
             {'language': {'type': 'string'}, 'dialect': {'type': 'string'},
              'emotion': {'type': 'string', 'enum': ['neutral','warm','playful','sarcastic',
                                                    'curious','serious','sad','excited','calm']},
              'evidence': {'type': 'string'}, 'reason': {'type': 'string'}},
             ['language', 'dialect', 'emotion']),
        ):
            session['tools'].append({'type': 'function', 'function': {
                'name': name, 'description': description, 'parameters': {
                    'type': 'object', 'properties': properties, 'required': required,
                    'additionalProperties': False}}})
    if sleep:
        session["tools"].append({
            "type": "function",
            "function": {
                "name": "enter_sleep",
                "description": (
                    "When the CURRENT user tells you to stop talking, be quiet, leave or end the "
                    "conversation (paraphrases count; questions, negations, quotes and talk about "
                    "others do not). Say one short acknowledgment; listening closes after it plays. "
                    "Only status=accepted means you will go quiet."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
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
        self.url = realtime_url(url, validate_model(url_model(url)))
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
