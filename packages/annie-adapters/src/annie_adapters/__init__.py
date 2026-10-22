"""Local config/storage plus the verified Phase 0 Room protocol boundary."""

import asyncio
import json
import logging
import math
import re
import sqlite3
import time
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from annie_core import AudioFormat, NormalizedEvent, RobotConfig
from websockets.asyncio.client import connect

from .language_preferences import (
    clear_language_preference,
    delete_unowned_chat_language_preferences,
    load_language_preferences,
    migrate_language_preferences,
    set_language_preference,
)
from .speakers import migrate_speaker_profiles

VERIFIED_MIC_FORMAT = AudioFormat("pcm_s16le", 16000, 1, 20, 640)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RoomServerConfig:
    base_url: str
    room_root: str
    channels: dict[str, str]
    default_urls: dict[str, str]
    subscribe_types: tuple[str, ...] = ("*",)
    reconnect_min_sec: float = 1.0
    reconnect_max_sec: float = 15.0
    connect_timeout_sec: float = 3.0

    def __post_init__(self):
        for name, value in (("base_url", self.base_url), ("room_root", self.room_root)):
            parsed = urlparse(value)
            if parsed.scheme not in {"ws", "wss"} or not parsed.netloc:
                raise ValueError(f"Room {name} must be an absolute ws:// or wss:// URL")
            if parsed.query or parsed.fragment:
                raise ValueError(f"Room {name} must not contain query or fragment")
        required_channels = {"main", "tts", "mic", "visualdata", "vision", "sam"}
        if (not required_channels.issubset(self.channels)
                or set(self.channels) != set(self.default_urls)
                or set(self.channels) - required_channels - {"visualllm", "voice", "display"}):
            raise ValueError("Room config requires six base channels and optional visualllm/voice/display, with matching URLs")
        if not all(self._path_segment(value) for value in self.channels.values()):
            raise ValueError("Room channel names must be URL path segments")
        if not all(urlparse(value).scheme in {"ws", "wss"} for value in self.default_urls.values()):
            raise ValueError("Room default URLs must use ws:// or wss://")
        if not self.subscribe_types or not all(
            isinstance(value, str) and value for value in self.subscribe_types
        ):
            raise ValueError("subscribe_types must contain non-empty strings")
        for name, value in (
            ("reconnect_min_sec", self.reconnect_min_sec),
            ("reconnect_max_sec", self.reconnect_max_sec),
            ("connect_timeout_sec", self.connect_timeout_sec),
        ):
            if type(value) not in (int, float) or value <= 0:
                raise ValueError(f"{name} must be a positive number")
        if self.reconnect_max_sec < self.reconnect_min_sec:
            raise ValueError("reconnect_max_sec must be >= reconnect_min_sec")

    @staticmethod
    def _path_segment(value: str) -> bool:
        return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._-]+", value) is not None

    @property
    def default_room(self) -> str:
        room_id = urlparse(self.base_url).path.rstrip("/").rsplit("/", 1)[-1]
        if not self._path_segment(room_id):
            raise ValueError("Room base_url must end with a room id")
        return room_id

    @property
    def mic_channel(self) -> str:
        return self.channels["mic"]

    def url_for(self, room_id: str, channel: str) -> str:
        if channel not in self.channels:
            raise ValueError(f"unknown Room channel: {channel}")
        if not self._path_segment(room_id):
            raise ValueError("room_id must be one URL path segment")
        if room_id == self.default_room:
            return self.default_urls[channel]
        return f"{self.room_root.rstrip('/')}/{room_id}/{self.channels[channel]}"

    def urls_for_room(self, room_id: str) -> dict[str, str]:
        return {channel: self.url_for(room_id, channel) for channel in self.channels}

    def mic_url(self, room_id: str) -> str:
        return self.url_for(room_id, "mic")


class RoomPcmConnection:
    """One reconnecting reb_mic consumer bound to exactly one configured Room."""

    def __init__(self, config: RoomServerConfig, room_id: str, on_binary, on_disconnect):
        self.config = config
        self.room_id = room_id
        self.url = config.mic_url(room_id)
        self.on_binary = on_binary
        self.on_disconnect = on_disconnect
        self._task = None
        self._ws = None
        self._stopped = True
        self._state = "stopped"
        self._client_id = None
        self._last_error = None
        self._frames_received = 0
        self._reconnects = 0

    def start(self):
        if self._task is not None and not self._task.done():
            return
        self._stopped = False
        self._state = "connecting"
        self._last_error = None
        self._task = asyncio.create_task(
            self._run(),
            name=f"harbor:room:{self.room_id}",
        )

    async def stop(self):
        self._stopped = True
        ws = self._ws
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._state = "stopped"
        self._ws = None
        self._task = None

    def snapshot(self):
        return {
            "room_id": self.room_id,
            "url": self.url,
            "state": self._state,
            "connected": self._state in {"connected", "subscribed"},
            "subscribed": self._state == "subscribed",
            "client_id": self._client_id,
            "frames_received": self._frames_received,
            "reconnects": self._reconnects,
            "last_error": self._last_error,
        }

    @staticmethod
    def _json_message(raw):
        if not isinstance(raw, str):
            raise ValueError("expected Room JSON control message")
        message = json.loads(raw)
        if not isinstance(message, dict):
            raise ValueError("Room control message must be an object")
        return message

    async def _handshake(self, ws):
        async with asyncio.timeout(self.config.connect_timeout_sec):
            welcome = parse_room_welcome(self._json_message(await ws.recv()))
            if welcome.room != self.room_id:
                raise ValueError(f"WELCOME room mismatch: expected {self.room_id}, got {welcome.room}")
            if welcome.robot_id != self.config.mic_channel:
                raise ValueError(
                    f"WELCOME robotId mismatch: expected {self.config.mic_channel}, got {welcome.robot_id}"
                )
            if "MIC_AUDIO" not in welcome.catalog_types:
                raise ValueError("WELCOME catalog does not advertise MIC_AUDIO")
            self._state = "connected"
            self._client_id = welcome.client_id

            await ws.send(
                json.dumps(
                    {"type": "SUBSCRIBE", "data": {"types": list(self.config.subscribe_types)}},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            pending_binary = []
            while True:
                raw = await ws.recv()
                if isinstance(raw, bytes):
                    pending_binary.append(raw)
                    continue
                message = self._json_message(raw)
                if message.get("type") != "ACK":
                    continue
                ack = parse_subscribe_ack(message)
                if not set(self.config.subscribe_types).issubset(ack.subscriptions):
                    raise ValueError("Room ACK subscriptions do not include requested types")
                self._state = "subscribed"
                self._last_error = None
                for frame in pending_binary:
                    self._dispatch_binary(frame)
                return

    def _dispatch_binary(self, frame: bytes):
        self._frames_received += 1
        self.on_binary(frame)

    async def _run(self):
        backoff = self.config.reconnect_min_sec
        while not self._stopped:
            subscribed = False
            try:
                self._state = "connecting"
                async with connect(
                    self.url, proxy=None,  # the Room is on the LAN
                    open_timeout=self.config.connect_timeout_sec,
                    close_timeout=self.config.connect_timeout_sec,
                ) as ws:
                    self._ws = ws
                    await self._handshake(ws)
                    subscribed = True
                    backoff = self.config.reconnect_min_sec
                    logger.info("room_pcm connected room=%s url=%s", self.room_id, self.url)
                    async for raw in ws:
                        if isinstance(raw, bytes):
                            self._dispatch_binary(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._stopped:
                    self._last_error = str(exc)
                    logger.warning(
                        "room_pcm disconnected room=%s url=%s error=%s retry=%.1fs",
                        self.room_id,
                        self.url,
                        exc,
                        backoff,
                    )
            finally:
                self._ws = None
                if not self._stopped:
                    self._state = "disconnected"
                    if subscribed:
                        self._reconnects += 1
                        self.on_disconnect()
            if not self._stopped:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.config.reconnect_max_sec)


@dataclass(frozen=True)
class RoomWelcome:
    client_id: str
    robot_id: str
    room: str
    size: int
    subscriptions: tuple[str, ...]
    catalog_types: tuple[str, ...]


@dataclass(frozen=True)
class SubscriptionAck:
    subscriptions: tuple[str, ...]


def parse_room_welcome(message: dict) -> RoomWelcome:
    """Validate the live-observed Room control-plane WELCOME shape."""
    if not isinstance(message, dict) or message.get("type") != "WELCOME" or message.get("ok") is not True:
        raise ValueError("expected successful WELCOME")
    client_id = message.get("clientId")
    robot_id = message.get("robotId")
    room = message.get("room")
    size = message.get("size")
    subscriptions = message.get("subscriptions")
    catalog = message.get("catalog")
    if not all(isinstance(value, str) and value for value in (client_id, robot_id, room)):
        raise ValueError("WELCOME identity fields must be non-empty strings")
    if type(size) is not int or size < 1:
        raise ValueError("WELCOME size must be a positive integer")
    if not isinstance(subscriptions, list) or not all(
        isinstance(value, str) and value for value in subscriptions
    ):
        raise ValueError("WELCOME subscriptions must be a string list")
    if not isinstance(catalog, list):
        raise ValueError("WELCOME catalog must be a list")
    catalog_types = []
    for entry in catalog:
        if not isinstance(entry, dict) or not isinstance(entry.get("type"), str) or not entry["type"]:
            raise ValueError("WELCOME catalog entries must have a non-empty type")
        catalog_types.append(entry["type"])
    return RoomWelcome(
        client_id=client_id,
        robot_id=robot_id,
        room=room,
        size=size,
        subscriptions=tuple(subscriptions),
        catalog_types=tuple(catalog_types),
    )


def parse_subscribe_ack(message: dict) -> SubscriptionAck:
    """Validate the live-observed ACK returned after SUBSCRIBE."""
    if not isinstance(message, dict) or message.get("type") != "ACK":
        raise ValueError("expected ACK")
    data = message.get("data")
    if not isinstance(data, dict) or data.get("what") != "subscribed":
        raise ValueError("expected subscribed ACK")
    subscriptions = data.get("subscriptions")
    if not isinstance(subscriptions, list) or not all(
        isinstance(value, str) and value for value in subscriptions
    ):
        raise ValueError("ACK subscriptions must be a string list")
    return SubscriptionAck(tuple(subscriptions))


def decode_room_binary_frame(frame: bytes) -> tuple[str, bytes] | None:
    """Decode the verified Room BinaryFrame envelope without guessing payload fields."""
    if len(frame) < 2:
        return None
    type_len = int.from_bytes(frame[:2], "big")
    if type_len <= 0 or len(frame) < 2 + type_len:
        return None
    try:
        message_type = frame[2 : 2 + type_len].decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not message_type:
        return None
    return message_type, frame[2 + type_len :]


def parse_mic_start(message: dict) -> AudioFormat:
    if not isinstance(message, dict) or message.get("type") != "mic.start":
        raise ValueError("expected mic.start")
    data = message.get("data")
    if not isinstance(data, dict):
        raise ValueError("mic.start data must be an object")
    audio = data.get("audio", data)
    if not isinstance(audio, dict):
        raise ValueError("mic.start audio must be an object")
    required = ("codec", "sample_rate", "channels", "frame_ms", "bytes_per_frame")
    if any(key not in audio for key in required):
        raise ValueError("mic.start missing verified audio fields")
    if not isinstance(audio["codec"], str):
        raise ValueError("mic.start codec must be a string")
    for key in required[1:]:
        if type(audio[key]) is not int:
            raise ValueError(f"mic.start {key} must be an integer")
    parsed = AudioFormat(**{key: audio[key] for key in required})
    if parsed != VERIFIED_MIC_FORMAT:
        raise ValueError("mic.start does not match the Phase 0 verified 16kHz mono S16LE 20ms contract")
    return parsed


def _first_text(message: dict) -> str | None:
    keys = ("text", "result", "asr", "transcript", "content")
    data = message.get("data") if isinstance(message.get("data"), dict) else {}
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    for scope in (payload, data, message):
        for key in keys:
            value = scope.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _radar_too_close(message: dict, data: dict) -> dict | None:
    """Embedded radar proximity uses the pressure envelope: id 5, name 距离过近."""
    sensor_id = data.get("id", message.get("id"))
    name = data.get("name", message.get("name"))
    distance = data.get("distance", message.get("distance"))
    if type(sensor_id) is not int or sensor_id != 5:
        return None
    if not isinstance(name, str) or name.strip() != "距离过近":
        return None
    if type(distance) not in (int, float) or not math.isfinite(distance):
        return None
    if not 0 <= distance <= 100_000:
        return None
    return {
        "id": 5,
        "name": "距离过近",
        "distance": distance,
        "sensor_type": "radar",
    }


def normalize_room_event(message: dict) -> NormalizedEvent | None:
    """Normalize only Phase 0-proven inputs; transport target remains intentionally unset."""
    if not isinstance(message, dict):
        return None
    message_type = message.get("type")
    if message_type in {"asr", "ASR_RESULT"}:
        text = _first_text(message)
        if text is None:
            return None
        return NormalizedEvent(
            kind="legacy_asr",
            source=message.get("from") if isinstance(message.get("from"), str) else None,
            event_id=message.get("event_id") if isinstance(message.get("event_id"), str) else None,
            ts_ms=message.get("ts") if type(message.get("ts")) is int else None,
            payload={
                "text": text,
                "asr_turn_token": message.get("asr_turn_token")
                if isinstance(message.get("asr_turn_token"), str)
                else None,
            },
        )

    if message_type == "robot_event":
        data = message.get("data") if isinstance(message.get("data"), dict) else message
        event_type = data.get("event_type")
        payload = data.get("payload")
        if not isinstance(event_type, str) or not event_type or not isinstance(payload, dict):
            return None
        source = data.get("source")
        confidence = data.get("confidence")
        return NormalizedEvent(
            kind=event_type,
            payload=dict(payload),
            source=source if isinstance(source, str) else None,
            event_id=data.get("event_id") if isinstance(data.get("event_id"), str) else None,
            ts_ms=data.get("ts_ms") if type(data.get("ts_ms")) is int else None,
            ttl_ms=data.get("ttl_ms") if type(data.get("ttl_ms")) is int else None,
            confidence=float(confidence) if type(confidence) in (int, float) else None,
        )

    if message_type in {"pressure", "touch", "gas"}:
        # The board's own `ts` is not clock-synced (samples a month behind were
        # sent), so freshness is judged on arrival: ts_ms stays unset.
        data = message.get("data") if isinstance(message.get("data"), dict) else {}
        if message_type == "pressure":
            radar = _radar_too_close(message, data)
            if radar is not None:
                source = data.get("source", message.get("from", message.get("source")))
                return NormalizedEvent(
                    kind="sensor.radar",
                    payload=radar,
                    source=source if isinstance(source, str) else None,
                )
        level = data.get("level", message.get("level"))
        if type(level) is not int:
            return None
        if message_type == "pressure" and not 0 <= level <= 5:
            return None
        if message_type in {"touch", "gas"}:
            level = int(level > 0)
        payload = {"level": level}
        for key in ("id", "name", "sensor_type", "text"):
            value = data.get(key, message.get(key))
            if value is not None:
                payload[key] = value
        source = data.get("source", message.get("from", message.get("source")))
        return NormalizedEvent(
            kind=f"sensor.{message_type}",
            payload=payload,
            source=source if isinstance(source, str) else None,
        )

    if message_type == "vision.perception":
        kind = message.get("kind")
        if kind not in {"face", "object", "gesture"}:
            return None
        payload = {}
        if isinstance(message.get("tracks"), list):
            payload["tracks"] = message["tracks"]
        if isinstance(message.get("gesture"), dict):
            payload["gesture"] = message["gesture"]
        if not payload:
            return None
        return NormalizedEvent(
            kind=f"vision.{kind}",
            payload=payload,
            source=message.get("from") if isinstance(message.get("from"), str) else None,
            ts_ms=message.get("ts") if type(message.get("ts")) is int else None,
        )

    if message_type in {"face", "object", "gesture"} and isinstance(message.get("source"), str):
        business = message.get(message_type)
        if not isinstance(business, dict):
            return None
        return NormalizedEvent(
            kind=f"vision.{message_type}",
            payload={message_type: business},
            source=message["source"],
            ts_ms=message.get("timestamp_ms") if type(message.get("timestamp_ms")) is int else None,
        )

    if message_type == "cancel" and isinstance(message.get("reason"), str):
        return NormalizedEvent(
            kind="cancel",
            payload={key: value for key, value in message.items() if key != "type"},
            source=message.get("source") if isinstance(message.get("source"), str) else None,
        )
    return None


def match_cancel_identity(active: dict, candidate: dict) -> tuple[bool, str]:
    """Preserve the Phase 0 small-brain cancel matching precedence exactly."""
    if active.get("cancelled") is True:
        return False, "already_cancelled"

    active_event_id = active.get("asr_event_id")
    candidate_event_id = candidate.get("asr_event_id")
    if active_event_id is not None and candidate_event_id is not None:
        if active_event_id == candidate_event_id:
            return True, "matched_event_id"
        return False, "asr_event_id_mismatch"

    active_token = active.get("asr_turn_token")
    candidate_token = candidate.get("turn_token", candidate.get("asr_turn_token"))
    if active_token is not None and candidate_token is not None:
        if active_token == candidate_token:
            return True, "matched_turn_token"
        return False, "turn_token_mismatch"

    candidate_source = candidate.get("asr_from")
    if not isinstance(candidate_source, str) or not candidate_source:
        return False, "missing_asr_from"
    candidate_ts = candidate.get("asr_ts")
    if type(candidate_ts) is not int:
        return False, "missing_asr_ts"
    if candidate_source != active.get("asr_from"):
        return False, "asr_from_mismatch"
    if candidate_ts != active.get("asr_ts"):
        return False, "asr_ts_mismatch"
    text = candidate.get("text")
    if isinstance(text, str) and isinstance(active.get("text"), str) and text != active["text"]:
        return False, "text_mismatch"
    return True, "matched_asr_source_ts"


def load_robots(path: Path) -> list[RobotConfig]:
    with path.open("rb") as file:
        configs = [RobotConfig(**row) for row in tomllib.load(file)["robots"]]
    if not configs or len({c.robot_id for c in configs}) != len(configs):
        raise ValueError("robots must have unique robot_id values")
    return configs


def load_room_server(path: Path) -> RoomServerConfig:
    with path.open("rb") as file:
        data = tomllib.load(file)
    server = data.get("server")
    rooms = data.get("rooms")
    urls = data.get("urls")
    if not isinstance(server, dict) or not isinstance(rooms, dict) or not isinstance(urls, dict):
        raise ValueError("rooms.toml requires [server], [rooms], and [urls]")
    subscribe_types = server.get("subscribe_types", ["*"])
    if not isinstance(subscribe_types, list):
        raise ValueError("server.subscribe_types must be a list")
    return RoomServerConfig(
        base_url=server.get("base_url"),
        room_root=server.get("room_root"),
        channels=dict(rooms),
        default_urls=dict(urls),
        subscribe_types=tuple(subscribe_types),
        reconnect_min_sec=server.get("reconnect_min_sec", 1.0),
        reconnect_max_sec=server.get("reconnect_max_sec", 15.0),
        connect_timeout_sec=server.get("connect_timeout_sec", 3.0),
    )


def resolve_database(root: Path, value: str) -> Path:
    root = root.resolve()
    var_root = (root / "var").resolve()
    if not var_root.is_relative_to(root):
        raise ValueError("project var/ directory must remain inside the project root")
    path = (root / value).resolve()
    if not path.is_relative_to(var_root):
        raise ValueError("database must remain inside the project var/ directory")
    return path


class RuntimeStore:
    SCHEMA_VERSION = 10

    load_language_preferences = load_language_preferences
    set_language_preference = set_language_preference
    clear_language_preference = clear_language_preference

    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def preflight(self):
        """Validate an existing database before a running service is replaced."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            return
        if not self.path.is_file():
            raise ValueError(f"database path is not a file: {self.path}")
        try:
            with sqlite3.connect(self.path, timeout=1) as db:
                db.execute("PRAGMA schema_version").fetchone()
                version = db.execute("PRAGMA user_version").fetchone()[0]
        except sqlite3.DatabaseError as exc:
            raise ValueError(f"invalid SQLite database: {self.path}") from exc
        if version > self.SCHEMA_VERSION:
            raise ValueError(f"unsupported future SQLite schema: {version}")

    def migrate(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > self.SCHEMA_VERSION:
                raise ValueError(f"unsupported future SQLite schema: {version}")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS runtime_snapshots (
                robot_id TEXT PRIMARY KEY, snapshot TEXT NOT NULL, updated_ms INTEGER NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS runtime_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, robot_id TEXT NOT NULL,
                room_id TEXT NOT NULL, kind TEXT NOT NULL, request_id TEXT,
                ts_ms INTEGER NOT NULL, elapsed_ms REAL, error TEXT,
                queue_wait_ms REAL, source TEXT)""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(runtime_events)")}
            if "queue_wait_ms" not in columns:
                db.execute("ALTER TABLE runtime_events ADD COLUMN queue_wait_ms REAL")
            if "source" not in columns:
                db.execute("ALTER TABLE runtime_events ADD COLUMN source TEXT")
            db.execute("""CREATE TABLE IF NOT EXISTS conversation_sessions (
                robot_id TEXT PRIMARY KEY,
                room_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                persona_id TEXT NOT NULL,
                persona_revision INTEGER NOT NULL,
                session_epoch INTEGER NOT NULL,
                generation INTEGER NOT NULL,
                updated_ms INTEGER NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS conversation_chats (
                robot_id TEXT NOT NULL, persona_id TEXT NOT NULL,
                chat_id TEXT NOT NULL, persona_revision INTEGER NOT NULL,
                created_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL,
                PRIMARY KEY(robot_id,persona_id,chat_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS persona_instances (
                robot_id TEXT NOT NULL, persona_id TEXT NOT NULL,
                display_name TEXT NOT NULL, created_ms INTEGER NOT NULL,
                updated_ms INTEGER NOT NULL,
                PRIMARY KEY(robot_id,persona_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS conversation_turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                turn_id TEXT NOT NULL UNIQUE,
                robot_id TEXT NOT NULL,
                room_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                persona_id TEXT NOT NULL,
                persona_revision INTEGER NOT NULL,
                session_epoch INTEGER NOT NULL,
                generation INTEGER NOT NULL,
                user_text TEXT NOT NULL,
                turn_source TEXT NOT NULL DEFAULT 'user',
                input_mode TEXT NOT NULL DEFAULT 'unknown',
                external_prompt TEXT,
                assistant_planned_text TEXT,
                assistant_generated_text TEXT,
                assistant_published_text TEXT,
                assistant_spoken_text TEXT,
                playback_evidence_status TEXT NOT NULL DEFAULT 'unknown',
                status TEXT NOT NULL,
                interrupted INTEGER NOT NULL DEFAULT 0,
                cancelled INTEGER NOT NULL DEFAULT 0,
                cancel_source TEXT,
                created_ms INTEGER NOT NULL,
                updated_ms INTEGER NOT NULL)""")
            turn_columns = {row[1] for row in db.execute("PRAGMA table_info(conversation_turns)")}
            if "turn_source" not in turn_columns:
                db.execute("ALTER TABLE conversation_turns ADD COLUMN turn_source TEXT NOT NULL DEFAULT 'user'")
            if "input_mode" not in turn_columns:
                # Legacy rows cannot be proved text or audio: do not guess.
                db.execute("ALTER TABLE conversation_turns ADD COLUMN input_mode TEXT NOT NULL DEFAULT 'unknown'")
            if "external_prompt" not in turn_columns:
                db.execute("ALTER TABLE conversation_turns ADD COLUMN external_prompt TEXT")
            # Repair previously persisted internal input, preserving the real
            # prompt in diagnostics rather than treating it as user testimony.
            if version < 8:
                db.execute("""UPDATE conversation_turns SET turn_source='external',
                    external_prompt=user_text, user_text=''
                    WHERE turn_source='user' AND (
                        user_text LIKE '【真实到期的单次提醒，非用户当前发言。%'
                        OR user_text LIKE '【外部事件，非用户发言】%')""")
                if db.execute("""SELECT 1 FROM sqlite_master
                               WHERE type='table' AND name='phase8_reminders'""").fetchone():
                    db.execute("""UPDATE conversation_turns SET turn_source='external',
                        external_prompt=user_text,user_text=''
                        WHERE turn_source='user' AND turn_id IN (
                            SELECT turn_id FROM phase8_reminders WHERE turn_id IS NOT NULL)""")
            db.execute("""CREATE INDEX IF NOT EXISTS conversation_turn_scope_idx
                ON conversation_turns(robot_id, chat_id, persona_id, id)""")
            db.execute("""INSERT OR IGNORE INTO conversation_chats
                (robot_id,persona_id,chat_id,persona_revision,created_ms,updated_ms)
                SELECT t.robot_id,t.persona_id,t.chat_id,
                    (SELECT latest.persona_revision FROM conversation_turns latest
                     WHERE latest.robot_id=t.robot_id AND latest.persona_id=t.persona_id
                     AND latest.chat_id=t.chat_id ORDER BY latest.id DESC LIMIT 1),
                    MIN(t.created_ms),MAX(t.updated_ms)
                FROM conversation_turns t GROUP BY t.robot_id,t.persona_id,t.chat_id""")
            db.execute("""INSERT OR IGNORE INTO persona_instances
                (robot_id,persona_id,display_name,created_ms,updated_ms)
                SELECT robot_id,persona_id,persona_id,MIN(created_ms),MAX(updated_ms)
                FROM conversation_turns GROUP BY robot_id,persona_id""")
            db.execute("""INSERT OR IGNORE INTO persona_instances
                (robot_id,persona_id,display_name,created_ms,updated_ms)
                SELECT robot_id,persona_id,persona_id,updated_ms,updated_ms
                FROM conversation_sessions""")
            db.execute("""CREATE TABLE IF NOT EXISTS conversation_social_state (
                robot_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                persona_id TEXT NOT NULL,
                state_key TEXT NOT NULL,
                value_json TEXT NOT NULL,
                source TEXT NOT NULL,
                source_turn_id TEXT,
                generation INTEGER NOT NULL,
                expires_at_ms INTEGER,
                updated_ms INTEGER NOT NULL,
                PRIMARY KEY(robot_id, chat_id, persona_id, state_key))""")
            db.execute("""CREATE TABLE IF NOT EXISTS long_term_memories (
                id TEXT PRIMARY KEY, robot_id TEXT NOT NULL,
                persona_id TEXT NOT NULL, kind TEXT NOT NULL,
                fact_key TEXT NOT NULL, text TEXT NOT NULL,
                source_turn_id TEXT, source_created_ms INTEGER NOT NULL,
                version INTEGER NOT NULL, status TEXT NOT NULL,
                updated_ms INTEGER NOT NULL, index_state TEXT NOT NULL,
                embedding BLOB,
                UNIQUE(robot_id, persona_id, kind, fact_key))""")
            db.execute("""INSERT OR IGNORE INTO persona_instances
                (robot_id,persona_id,display_name,created_ms,updated_ms)
                SELECT robot_id,persona_id,persona_id,MIN(source_created_ms),MAX(updated_ms)
                FROM long_term_memories WHERE persona_id <> ''
                GROUP BY robot_id,persona_id""")
            db.execute("""CREATE INDEX IF NOT EXISTS memory_scope_idx
                ON long_term_memories(robot_id, persona_id, status)""")
            db.execute("""CREATE TABLE IF NOT EXISTS chat_memories (
                id TEXT PRIMARY KEY, robot_id TEXT NOT NULL,
                persona_id TEXT NOT NULL, chat_id TEXT NOT NULL,
                fact_key TEXT NOT NULL, text TEXT NOT NULL,
                source_turn_id TEXT, source_created_ms INTEGER NOT NULL,
                version INTEGER NOT NULL, status TEXT NOT NULL,
                expires_at_ms INTEGER, updated_ms INTEGER NOT NULL,
                UNIQUE(robot_id, persona_id, chat_id, fact_key))""")
            db.execute("""CREATE INDEX IF NOT EXISTS chat_memory_scope_idx
                ON chat_memories(robot_id, persona_id, chat_id, status)""")
            db.execute("""CREATE TABLE IF NOT EXISTS memory_progress (
                robot_id TEXT PRIMARY KEY, turn_row_id INTEGER NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS action_requests (
                robot_id TEXT NOT NULL, call_id TEXT NOT NULL, turn_id TEXT NOT NULL,
                generation INTEGER NOT NULL, room_id TEXT NOT NULL,
                kind TEXT NOT NULL, value TEXT NOT NULL, status TEXT NOT NULL,
                error TEXT, ts_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(robot_id,call_id),
                UNIQUE(robot_id,turn_id,kind,value))""")
            if "duration_ms" not in {
                column[1] for column in db.execute("PRAGMA table_info(action_requests)")
            }:
                db.execute(
                    "ALTER TABLE action_requests ADD COLUMN duration_ms INTEGER NOT NULL DEFAULT 0"
                )
            db.execute("""CREATE INDEX IF NOT EXISTS action_requests_recent
                ON action_requests(robot_id,updated_ms DESC)""")
            db.execute("""CREATE TABLE IF NOT EXISTS cloned_voices (
                voice_id TEXT PRIMARY KEY, name TEXT NOT NULL, region TEXT NOT NULL,
                model TEXT NOT NULL, created_ms INTEGER NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS voice_preferences (
                robot_id TEXT NOT NULL, chat_id TEXT NOT NULL,
                persona_id TEXT NOT NULL, voice_id TEXT NOT NULL,
                updated_ms INTEGER NOT NULL,
                PRIMARY KEY(robot_id,chat_id,persona_id))""")
            migrate_language_preferences(db)
            migrate_speaker_profiles(db)
            db.execute(f"PRAGMA user_version={self.SCHEMA_VERSION}")

    def recover_actions(self):
        """Retire in-memory-only reservations after an actual process restart."""
        with self.connect() as db:
            now = time.time_ns() // 1_000_000
            db.execute(
                """UPDATE action_requests SET status='expired_no_pcm',
                   error='process_restarted',updated_ms=?
                   WHERE status='awaiting_audio_anchor'""", (now,),
            )
            db.execute(
                """UPDATE action_requests SET status='expired_no_text_anchor',
                   error='process_restarted',updated_ms=?
                   WHERE status='awaiting_text_anchor'""", (now,),
            )
            # Retire reservations left by an older acoustic-gated binary.
            db.execute(
                """UPDATE action_requests SET status='expired_no_alignment',
                   error='process_restarted',updated_ms=?
                   WHERE status='awaiting_word_alignment'""", (now,),
            )
            db.execute(
                """UPDATE action_requests SET status='cancelled_before_send',
                   error='process_restarted',updated_ms=?
                   WHERE status='reserved'""", (now,),
            )

    def list_actions(self, robot_id, limit=50):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(
                "SELECT * FROM action_requests WHERE robot_id=? ORDER BY ts_ms DESC LIMIT ?",
                (robot_id, limit),
            )]

    def get_action(self, robot_id, call_id):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM action_requests WHERE robot_id=? AND call_id=?",
                (robot_id, call_id),
            ).fetchone()
            return dict(row) if row else None

    def reserve_action(self, robot_id, call_id, turn_id, generation, room_id,
                       kind, value, cooldown_ms, *, word_aligned=False):
        now = time.time_ns() // 1_000_000
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM action_requests WHERE robot_id=? AND call_id=?",
                (robot_id, call_id),
            ).fetchone()
            if existing:
                return dict(existing), True
            same = db.execute(
                """SELECT * FROM action_requests
                   WHERE robot_id=? AND turn_id=? AND kind=? AND value=?""",
                (robot_id, turn_id, kind, value),
            ).fetchone()
            if same:
                return dict(same), True
            if kind == "action":
                last = db.execute(
                    """SELECT duration_ms,updated_ms,status FROM action_requests
                       WHERE robot_id=? AND kind='action'
                       ORDER BY ts_ms DESC LIMIT 1""",
                    (robot_id,),
                ).fetchone()
                if last and (
                    last["status"] in {"awaiting_audio_anchor", "awaiting_text_anchor"}
                    or last["status"] in {"published_unknown", "send_unknown"}
                    and now - last["updated_ms"] < max(
                        cooldown_ms, last["duration_ms"],
                    )
                ):
                    return None, False
            db.execute(
                """INSERT INTO action_requests
                   (robot_id,call_id,turn_id,generation,room_id,kind,value,
                    status,error,ts_ms,updated_ms,duration_ms)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (robot_id, call_id, turn_id, generation, room_id, kind, value,
                 ("awaiting_text_anchor" if word_aligned else "awaiting_audio_anchor")
                 if kind == "action" else "reserved",
                 None, now, now, cooldown_ms),
            )
            row = db.execute(
                "SELECT * FROM action_requests WHERE robot_id=? AND call_id=?",
                (robot_id, call_id),
            ).fetchone()
            return dict(row), False

    def set_action_status(self, robot_id, call_id, before, after, error=None):
        now = time.time_ns() // 1_000_000
        with self.connect() as db:
            cursor = db.execute(
                """UPDATE action_requests SET status=?,error=?,updated_ms=?
                   WHERE robot_id=? AND call_id=? AND status=?""",
                (after, error, now, robot_id, call_id, before),
            )
            return cursor.rowcount == 1

    def write_batch(self, records):
        with self.connect() as db:
            for event, snapshot in records:
                db.execute(
                    """INSERT INTO runtime_events
                    (robot_id,room_id,kind,request_id,ts_ms,elapsed_ms,error,queue_wait_ms,source)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        event.robot_id,
                        event.room_id,
                        event.kind,
                        event.request_id,
                        event.ts_ms,
                        event.elapsed_ms,
                        event.error,
                        event.queue_wait_ms,
                        event.source,
                    ),
                )
                db.execute(
                    """INSERT INTO runtime_snapshots VALUES (?,?,?)
                    ON CONFLICT(robot_id) DO UPDATE SET snapshot=excluded.snapshot,updated_ms=excluded.updated_ms""",
                    (event.robot_id, json.dumps(snapshot, ensure_ascii=False), event.ts_ms),
                )

    def load_conversation_session(self, robot_id: str):
        with self.connect() as db:
            row = db.execute(
                """SELECT robot_id,room_id,chat_id,persona_id,persona_revision,
                session_epoch,generation,updated_ms
                FROM conversation_sessions WHERE robot_id=?""",
                (robot_id,),
            ).fetchone()
        if row is None:
            return None
        keys = (
            "robot_id",
            "room_id",
            "chat_id",
            "persona_id",
            "persona_revision",
            "session_epoch",
            "generation",
            "updated_ms",
        )
        return dict(zip(keys, row, strict=True))

    def save_conversation_session(self, session: dict):
        with self.connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO persona_instances
                (robot_id,persona_id,display_name,created_ms,updated_ms)
                VALUES(?,?,?,?,?)""",
                (session["robot_id"], session["persona_id"], session["persona_id"],
                 session["updated_ms"], session["updated_ms"]),
            )
            db.execute(
                """INSERT INTO conversation_chats
                (robot_id,persona_id,chat_id,persona_revision,created_ms,updated_ms)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(robot_id,persona_id,chat_id) DO UPDATE SET
                persona_revision=excluded.persona_revision,
                updated_ms=excluded.updated_ms""",
                (session["robot_id"], session["persona_id"], session["chat_id"],
                 session["persona_revision"], session["updated_ms"], session["updated_ms"]),
            )
            db.execute(
                """INSERT INTO conversation_sessions
                (robot_id,room_id,chat_id,persona_id,persona_revision,session_epoch,generation,updated_ms)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(robot_id) DO UPDATE SET
                room_id=excluded.room_id,
                chat_id=excluded.chat_id,
                persona_id=excluded.persona_id,
                persona_revision=excluded.persona_revision,
                session_epoch=excluded.session_epoch,
                generation=excluded.generation,
                updated_ms=excluded.updated_ms""",
                (
                    session["robot_id"],
                    session["room_id"],
                    session["chat_id"],
                    session["persona_id"],
                    session["persona_revision"],
                    session["session_epoch"],
                    session["generation"],
                    session["updated_ms"],
                ),
            )

    def list_persona_instances(self, robot_id):
        with self.connect() as db:
            rows = db.execute(
                """SELECT persona_id,display_name,created_ms,updated_ms
                FROM persona_instances WHERE robot_id=? ORDER BY created_ms,persona_id""",
                (robot_id,),
            ).fetchall()
        return [dict(zip(("persona_id", "display_name", "created_ms", "updated_ms"), row,
                         strict=True)) for row in rows]

    def get_persona_name(self, robot_id, persona_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT display_name FROM persona_instances WHERE robot_id=? AND persona_id=?",
                (robot_id, persona_id),
            ).fetchone()
        return row[0] if row else persona_id

    def set_persona_name(self, robot_id, persona_id, display_name):
        now = time.time_ns() // 1_000_000
        with self.connect() as db:
            db.execute(
                """INSERT INTO persona_instances
                (robot_id,persona_id,display_name,created_ms,updated_ms)
                VALUES(?,?,?,?,?) ON CONFLICT(robot_id,persona_id) DO UPDATE SET
                display_name=excluded.display_name,updated_ms=excluded.updated_ms""",
                (robot_id, persona_id, display_name, now, now),
            )
        return {"persona_id": persona_id, "display_name": display_name}

    def create_conversation_turn(self, turn: dict):
        with self.connect() as db:
            db.execute(
                """INSERT INTO conversation_turns
                (turn_id,robot_id,room_id,chat_id,persona_id,persona_revision,
                session_epoch,generation,user_text,turn_source,input_mode,external_prompt,
                status,created_ms,updated_ms)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    turn["turn_id"],
                    turn["robot_id"],
                    turn["room_id"],
                    turn["chat_id"],
                    turn["persona_id"],
                    turn["persona_revision"],
                    turn["session_epoch"],
                    turn["generation"],
                    turn["user_text"],
                    turn.get("turn_source", "user"),
                    turn.get("input_mode", "unknown"),
                    turn.get("external_prompt"),
                    turn["status"],
                    turn["created_ms"],
                    turn["updated_ms"],
                ),
            )

    def finish_conversation_turn(
        self,
        robot_id: str,
        turn_id: str,
        *,
        status: str,
        planned_text: str | None = None,
        generated_text: str | None = None,
        interrupted: bool = False,
        cancelled: bool = False,
        cancel_source: str | None = None,
        updated_ms: int,
    ):
        with self.connect() as db:
            db.execute(
                """UPDATE conversation_turns SET
                status=?,
                assistant_planned_text=COALESCE(?,assistant_planned_text),
                assistant_generated_text=COALESCE(?,assistant_generated_text),
                interrupted=?,
                cancelled=?,
                cancel_source=?,
                updated_ms=?
                WHERE robot_id=? AND turn_id=?""",
                (
                    status,
                    planned_text,
                    generated_text,
                    int(interrupted),
                    int(cancelled),
                    cancel_source,
                    updated_ms,
                    robot_id,
                    turn_id,
                ),
            )

    def set_conversation_turn_user_text(self, robot_id: str, turn_id: str, text: str, updated_ms: int):
        """Backfill ASR final text when it arrives after response text begins."""
        with self.connect() as db:
            db.execute(
                "UPDATE conversation_turns SET user_text=?, updated_ms=? WHERE robot_id=? AND turn_id=?",
                (text, updated_ms, robot_id, turn_id),
            )

    def update_playback(
        self,
        robot_id: str,
        turn_id: str,
        *,
        published_text: str | None,
        spoken_text: str | None,
        evidence_status: str,
        updated_ms: int,
    ):
        with self.connect() as db:
            changed = db.execute(
                """UPDATE conversation_turns SET
                assistant_published_text=COALESCE(?,assistant_published_text),
                assistant_spoken_text=COALESCE(?,assistant_spoken_text),
                playback_evidence_status=?,
                updated_ms=?
                WHERE robot_id=? AND turn_id=?""",
                (published_text, spoken_text, evidence_status, updated_ms, robot_id, turn_id),
            ).rowcount
        return changed

    @staticmethod
    def _turn_row(row):
        keys = (
            "id",
            "turn_id",
            "robot_id",
            "room_id",
            "chat_id",
            "persona_id",
            "persona_revision",
            "session_epoch",
            "generation",
            "user_text",
            "turn_source",
            "input_mode",
            "external_prompt",
            "assistant_planned_text",
            "assistant_generated_text",
            "assistant_published_text",
            "assistant_spoken_text",
            "playback_evidence_status",
            "status",
            "interrupted",
            "cancelled",
            "cancel_source",
            "created_ms",
            "updated_ms",
        )
        item = dict(zip(keys, row, strict=True))
        item["interrupted"] = bool(item["interrupted"])
        item["cancelled"] = bool(item["cancelled"])
        return item

    def get_conversation_turn(self, robot_id: str, turn_id: str):
        with self.connect() as db:
            row = db.execute(
                """SELECT id,turn_id,robot_id,room_id,chat_id,persona_id,persona_revision,
                session_epoch,generation,user_text,turn_source,input_mode,external_prompt,
                assistant_planned_text,assistant_generated_text,
                assistant_published_text,assistant_spoken_text,playback_evidence_status,status,
                interrupted,cancelled,cancel_source,created_ms,updated_ms
                FROM conversation_turns WHERE robot_id=? AND turn_id=?""",
                (robot_id, turn_id),
            ).fetchone()
        return None if row is None else self._turn_row(row)

    def load_conversation(self, robot_id: str, chat_id: str, persona_id: str):
        with self.connect() as db:
            rows = db.execute(
                """SELECT id,turn_id,robot_id,room_id,chat_id,persona_id,persona_revision,
                session_epoch,generation,user_text,turn_source,input_mode,external_prompt,
                assistant_planned_text,assistant_generated_text,
                assistant_published_text,assistant_spoken_text,playback_evidence_status,status,
                interrupted,cancelled,cancel_source,created_ms,updated_ms
                FROM conversation_turns
                WHERE robot_id=? AND chat_id=? AND persona_id=?
                ORDER BY id""",
                (robot_id, chat_id, persona_id),
            ).fetchall()
        return [self._turn_row(row) for row in rows]

    def load_conversation_page(self, robot_id: str, chat_id: str, persona_id: str,
                               *, limit: int, before_id: int | None = None):
        """Read the latest bounded page, using the existing scope/id index."""
        where = "robot_id=? AND chat_id=? AND persona_id=?"
        args = [robot_id, chat_id, persona_id]
        if before_id is not None:
            where += " AND id<?"
            args.append(before_id)
        with self.connect() as db:
            rows = db.execute(
                """SELECT id,turn_id,robot_id,room_id,chat_id,persona_id,persona_revision,
                session_epoch,generation,user_text,turn_source,input_mode,external_prompt,
                assistant_planned_text,assistant_generated_text,
                assistant_published_text,assistant_spoken_text,playback_evidence_status,status,
                interrupted,cancelled,cancel_source,created_ms,updated_ms
                FROM conversation_turns WHERE """ + where + " ORDER BY id DESC LIMIT ?",
                (*args, limit + 1),
            ).fetchall()
        turns = [self._turn_row(row) for row in reversed(rows[:limit])]
        return {"turns": turns, "has_more": len(rows) > limit,
                "next_before_id": turns[0]["id"] if len(rows) > limit else None}

    def list_conversations(self, robot_id: str):
        with self.connect() as db:
            rows = db.execute(
                """SELECT c.chat_id,c.persona_id,COALESCE(t.count,0),
                       MAX(c.updated_ms,COALESCE(t.updated_ms,0)),c.persona_revision
                FROM conversation_chats c LEFT JOIN (
                  SELECT robot_id,persona_id,chat_id,COUNT(*) AS count,
                         MAX(updated_ms) AS updated_ms FROM conversation_turns
                  WHERE robot_id=? GROUP BY robot_id,persona_id,chat_id
                ) t ON t.robot_id=c.robot_id AND t.persona_id=c.persona_id
                      AND t.chat_id=c.chat_id
                WHERE c.robot_id=? ORDER BY MAX(c.updated_ms,COALESCE(t.updated_ms,0)) DESC""",
                (robot_id, robot_id),
            ).fetchall()
        return [
            {
                "chat_id": chat_id,
                "persona_id": persona_id,
                "turn_count": turn_count,
                "updated_ms": updated_ms,
                "persona_revision": persona_revision,
            }
            for chat_id, persona_id, turn_count, updated_ms, persona_revision in rows
        ]

    def recent_conversation_context(self, robot_id: str, chat_id: str, persona_id: str, limit: int):
        with self.connect() as db:
            rows = db.execute(
                """SELECT id,turn_id,robot_id,room_id,chat_id,persona_id,persona_revision,
                session_epoch,generation,user_text,turn_source,input_mode,external_prompt,
                assistant_planned_text,assistant_generated_text,
                assistant_published_text,assistant_spoken_text,playback_evidence_status,status,
                interrupted,cancelled,cancel_source,created_ms,updated_ms
                FROM conversation_turns
                WHERE robot_id=? AND chat_id=? AND persona_id=?
                  AND (
                    (status='completed' AND cancelled=0 AND interrupted=0)
                    OR (status='cancelled' AND cancel_source IN
                        ('stable_asr_prefix','valid_smart_turn','meaningful_ambient'))
                  )
                  AND turn_source='user' AND TRIM(user_text) <> ''
                ORDER BY id DESC LIMIT ?""",
                (robot_id, chat_id, persona_id, limit),
            ).fetchall()
        result = []
        for row in reversed(rows):
            item = self._turn_row(row)
            item["assistant_context_text"] = (
                item["assistant_spoken_text"]
                or item["assistant_published_text"]
                or item["assistant_generated_text"]
            )
            result.append(item)
        return result

    def delete_conversation(self, robot_id: str, chat_id: str, persona_id: str):
        with self.connect() as db:
            deleted_turns = db.execute(
                "DELETE FROM conversation_turns WHERE robot_id=? AND chat_id=? AND persona_id=?",
                (robot_id, chat_id, persona_id),
            ).rowcount
            db.execute(
                """DELETE FROM conversation_social_state
                WHERE robot_id=? AND chat_id=? AND persona_id=?""",
                (robot_id, chat_id, persona_id),
            )
            db.execute(
                """DELETE FROM conversation_chats
                WHERE robot_id=? AND chat_id=? AND persona_id=?""",
                (robot_id, chat_id, persona_id),
            )
            db.execute(
                """DELETE FROM chat_memories
                WHERE robot_id=? AND chat_id=? AND persona_id=?""",
                (robot_id, chat_id, persona_id),
            )
            delete_unowned_chat_language_preferences(db, robot_id, chat_id, persona_id)
        return deleted_turns

    def list_chat_memories(self, robot_id, persona_id, chat_id, include_deleted=False):
        now = time.time_ns() // 1_000_000
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                """SELECT * FROM chat_memories
                WHERE robot_id=? AND persona_id=? AND chat_id=?
                AND (? OR (status='active' AND (expires_at_ms IS NULL OR expires_at_ms>?)))
                ORDER BY updated_ms DESC LIMIT 128""",
                (robot_id, persona_id, chat_id, int(include_deleted), now),
            ).fetchall()
        return [dict(row) for row in rows]

    def put_chat_memory(self, robot_id, persona_id, chat_id, key, text,
                        source_turn_id, source_created_ms, expires_at_ms=None):
        now = time.time_ns() // 1_000_000
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            if source_turn_id is not None and db.execute(
                """SELECT 1 FROM conversation_turns WHERE robot_id=?
                AND persona_id=? AND chat_id=? AND turn_id=? AND status='completed'""",
                (robot_id, persona_id, chat_id, source_turn_id),
            ).fetchone() is None:
                return None  # Deleted Chat cannot be recreated by an old extraction.
            row = db.execute(
                """SELECT id,version,status,source_created_ms FROM chat_memories
                WHERE robot_id=? AND persona_id=? AND chat_id=? AND fact_key=?""",
                (robot_id, persona_id, chat_id, key),
            ).fetchone()
            if row is not None and (row["status"] == "deleted" or
                                    source_created_ms <= row["source_created_ms"]):
                return None
            if row is not None and source_turn_id is None:
                raise ValueError("chat memory key exists; use the correction endpoint")
            ident, version = (row["id"], row["version"] + 1) if row else (uuid4().hex, 1)
            db.execute(
                """INSERT INTO chat_memories
                (id,robot_id,persona_id,chat_id,fact_key,text,source_turn_id,
                 source_created_ms,version,status,expires_at_ms,updated_ms)
                VALUES(?,?,?,?,?,?,?,?,?,'active',?,?)
                ON CONFLICT(robot_id,persona_id,chat_id,fact_key) DO UPDATE SET
                 text=excluded.text,source_turn_id=excluded.source_turn_id,
                 source_created_ms=excluded.source_created_ms,version=excluded.version,
                 status='active',expires_at_ms=excluded.expires_at_ms,
                 updated_ms=excluded.updated_ms""",
                (ident, robot_id, persona_id, chat_id, key, text, source_turn_id,
                 source_created_ms, version, expires_at_ms, now),
            )
        return ident, version

    def change_chat_memory(self, robot_id, persona_id, chat_id, ident, version, text):
        now = time.time_ns() // 1_000_000
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT version,status,expires_at_ms FROM chat_memories
                WHERE id=? AND robot_id=? AND persona_id=? AND chat_id=?""",
                (ident, robot_id, persona_id, chat_id),
            ).fetchone()
            if row is None:
                raise KeyError(ident)
            if (row["version"] != version or row["status"] != "active" or
                    row["expires_at_ms"] is not None and row["expires_at_ms"] <= now):
                raise ValueError("chat memory version conflict or expired")
            db.execute(
                """UPDATE chat_memories SET text=?,status=?,version=version+1,
                 source_created_ms=?,updated_ms=? WHERE id=?""",
                (text or "", "active" if text else "deleted", now, now, ident),
            )
        return version + 1

    def upsert_social_state(
        self,
        robot_id: str,
        chat_id: str,
        persona_id: str,
        state_key: str,
        value,
        *,
        source: str,
        source_turn_id: str | None,
        generation: int,
        expires_at_ms: int | None,
        updated_ms: int,
    ):
        with self.connect() as db:
            db.execute(
                """INSERT INTO conversation_social_state
                (robot_id,chat_id,persona_id,state_key,value_json,source,source_turn_id,
                 generation,expires_at_ms,updated_ms)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(robot_id,chat_id,persona_id,state_key) DO UPDATE SET
                value_json=excluded.value_json,
                source=excluded.source,
                source_turn_id=excluded.source_turn_id,
                generation=excluded.generation,
                expires_at_ms=excluded.expires_at_ms,
                updated_ms=excluded.updated_ms""",
                (
                    robot_id,
                    chat_id,
                    persona_id,
                    state_key,
                    json.dumps(value, ensure_ascii=False),
                    source,
                    source_turn_id,
                    generation,
                    expires_at_ms,
                    updated_ms,
                ),
            )

    def load_social_state(self, robot_id: str, chat_id: str, persona_id: str, now_ms: int):
        with self.connect() as db:
            rows = db.execute(
                """SELECT state_key,value_json,source,source_turn_id,generation,expires_at_ms,updated_ms
                FROM conversation_social_state
                WHERE robot_id=? AND chat_id=? AND persona_id=?
                  AND (expires_at_ms IS NULL OR expires_at_ms>?)""",
                (robot_id, chat_id, persona_id, now_ms),
            ).fetchall()
        return {
            state_key: {
                "value": json.loads(value_json),
                "source": source,
                "source_turn_id": source_turn_id,
                "generation": generation,
                "expires_at_ms": expires_at_ms,
                "updated_ms": updated_ms,
            }
            for state_key, value_json, source, source_turn_id, generation, expires_at_ms, updated_ms in rows
        }
