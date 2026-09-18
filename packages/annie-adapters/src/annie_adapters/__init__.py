"""Local TOML and SQLite only; Phase 1 imports no Provider/sidecar SDK."""

import json
import sqlite3
import tomllib
from contextlib import contextmanager
from pathlib import Path

from annie_core import RobotConfig


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
    SCHEMA_VERSION = 1

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
                ts_ms INTEGER NOT NULL, elapsed_ms REAL, error TEXT)""")
            db.execute(f"PRAGMA user_version={self.SCHEMA_VERSION}")

    def write_batch(self, records):
        with self.connect() as db:
            for event, snapshot in records:
                db.execute(
                    """INSERT INTO runtime_events
                    (robot_id,room_id,kind,request_id,ts_ms,elapsed_ms,error) VALUES (?,?,?,?,?,?,?)""",
                    (
                        event.robot_id,
                        event.room_id,
                        event.kind,
                        event.request_id,
                        event.ts_ms,
                        event.elapsed_ms,
                        event.error,
                    ),
                )
                db.execute(
                    """INSERT INTO runtime_snapshots VALUES (?,?,?)
                    ON CONFLICT(robot_id) DO UPDATE SET snapshot=excluded.snapshot,updated_ms=excluded.updated_ms""",
                    (event.robot_id, json.dumps(snapshot, ensure_ascii=False), event.ts_ms),
                )
