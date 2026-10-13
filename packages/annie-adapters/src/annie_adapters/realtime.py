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
                    "Only when the CURRENT user explicitly asks to switch TTS voices. "
                    "If they say '换一个音色' without naming a voice, still call this "
                    "tool with a catalog voice name: the Runtime chooses automatically "
                    "only if exactly one other candidate exists; otherwise tool returns "
                    "needs_selection and you MUST ask which. Use scope=chat normally, "
                    "scope=robot ONLY for explicitly permanent/all-chats requests. "
                    "You MUST inspect the tool result: ONLY status=selected means a "
                    "new voice was selected; needs_selection or rejected means NOTHING "
                    "changed. Never claim the voice was changed without status=selected. "
                    "Never pick a new voice merely because emotion, dialect, "
                    "speaker identity or persona changed. Takes effect for the next "
                    "TTS task; it cannot change already synthesized speech."
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
                    "Only when the CURRENT user explicitly asks to change the personality "
                    "style of THIS chat. Do not create a new chat or reconnect. "
                    "Available styles: " + "、".join(persona_labels) + ". "
                    "If they name one, pass that exact label as style_name. "
                    "If they ask to switch but name none, pass style_name as an empty string; "
                    "needs_selection means NOTHING changed and you must ask once which style. "
                    "Never invent a style, and never call this for voice, dialect or language. "
                    "ONLY status=selected means the style changed. "
                    "already_selected, needs_selection or rejected means it did not. "
                    "Call this before speaking. After selected, say one new sentence in that "
                    "style that answers what they just said. Do not recite a fixed confirmation "
                    "or the style rules."
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
                    "script from shy_retract. All five expressive scripts MUST include "
                    "anchor_text, even when explicitly requested, or the tool will reject "
                    "them. Choose an exact word you will speak in your subsequent reply. "
                    "Other scripts may omit anchor_text to use the legacy first-PCM "
                    "trigger; when supplied, the Runtime sends ONE indexed "
                    "action_prepare as soon as your real spoken reply contains the "
                    "exact anchor. The device independently listens to reb_sam for "
                    "acoustic timing; do not wait for a ForcedAligner result. "
                    "anchor_occurrence selects its nonoverlapping occurrence from 1. "
                    "Never guess milliseconds or samples, and never speak tool fields. "
                    "If you choose a word not spoken in your reply, no action is "
                    "sent; a late audio trigger is dropped by the device. "
                    "An accepted tool result means the command is scheduled for async "
                    "publication; immediately respond naturally without waiting for "
                    "device playback or a motor receipt. Successful Room publication "
                    "completes cloud dispatch, not physical execution. Do not narrate "
                    "delivery, hardware uncertainty, past imagined failures or "
                    "unfinished movement. Do not claim the motor completed either. "
                    "Enabled catalog: " +
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
                    "ONLY when explicitly asked to show a facial expression. "
                    "An accepted tool request is handled asynchronously. "
                    "Respond naturally without waiting for device acknowledgement "
                    "or narrating transport, hardware uncertainty or motor completion."
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
    if language_preferences:
        for name, description, properties, required in (
            ('set_language_preference',
             'Save ONLY a CURRENT explicit user language preference. scope=chat for this Chat; '
             'scope=robot for all future Chats of THIS robot. Never persist automatic inference. '
             'Call before claiming saved; status=set confirms committed success.',
             {'scope': {'type': 'string', 'enum': ['chat', 'robot']},
              'language': {'type': 'string'}, 'dialect': {'type': 'string'}},
             ['scope', 'language', 'dialect']),
            ('clear_language_preference',
             'Clear ONLY when the CURRENT user explicitly requests clearing a saved language '
             'preference and names Chat or robot scope. status=cleared confirms committed success.',
             {'scope': {'type': 'string', 'enum': ['chat', 'robot']}}, ['scope']),
            ('select_reply_style',
             'OPTIONAL context choice, never required before speaking. Select reply language and '
             'expression; for province dialect inference supply an EXACT stable user acoustic '
             'phrase as evidence. Do not infer from place names. Never persists preference. '
             'Quoted/translated/ambiguous speech may decline following with reason. Late choices '
             'affect next reply only. Output spoken text without control tags.',
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
                    "Call when the CURRENT user explicitly tells you to stop talking, "
                    "be quiet, leave, or end this conversation, including paraphrases. "
                    "Say one short acknowledgment. Listening closes after that sentence "
                    "finishes playing. Do not call for questions, negation, quotations, "
                    "or talk about someone else. Only status=accepted means this reply "
                    "will enter sleep. Never claim you went quiet without status=accepted."
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
