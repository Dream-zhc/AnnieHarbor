"""Local config/storage plus the verified Phase 0 Room protocol boundary."""

import json
import sqlite3
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from annie_core import AudioFormat, NormalizedEvent, RobotConfig

VERIFIED_MIC_FORMAT = AudioFormat("pcm_s16le", 16000, 1, 20, 640)


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
        data = message.get("data") if isinstance(message.get("data"), dict) else {}
        level = data.get("level", message.get("level"))
        if type(level) is not int:
            return None
        if message_type == "pressure" and not 0 <= level <= 5:
            return None
        if message_type in {"touch", "gas"}:
            level = int(level > 0)
        payload = {"level": level}
        for key in ("id", "sensor_type", "text"):
            value = data.get(key, message.get(key))
            if value is not None:
                payload[key] = value
        source = data.get("source", message.get("from", message.get("source")))
        ts_ms = data.get("ts", message.get("ts"))
        return NormalizedEvent(
            kind=f"sensor.{message_type}",
            payload=payload,
            source=source if isinstance(source, str) else None,
            ts_ms=ts_ms if type(ts_ms) is int else None,
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
    SCHEMA_VERSION = 2

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
            db.execute(f"PRAGMA user_version={self.SCHEMA_VERSION}")

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
