"""SQLite checkpoints and compare-and-swap side-effect reservations for Phase 8B."""

import json
import sqlite3
from pathlib import Path


class ReminderStore:
    def __init__(self, path: Path):
        self.path = path

    def exists(self):
        if not self.path.exists():
            return False
        with sqlite3.connect(self.path, timeout=5) as db:
            return db.execute("""SELECT 1 FROM sqlite_master
                   WHERE type='table' AND name='phase8_reminders'""").fetchone() is not None

    def initialize(self):
        with sqlite3.connect(self.path, timeout=5) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS phase8_reminders (
                task_id TEXT PRIMARY KEY, robot_id TEXT NOT NULL,
                chat_id TEXT NOT NULL, persona_id TEXT NOT NULL,
                session_epoch INTEGER NOT NULL, generation INTEGER NOT NULL,
                request_key TEXT NOT NULL, text TEXT NOT NULL,
                due_ms INTEGER NOT NULL, expires_ms INTEGER NOT NULL,
                status TEXT NOT NULL, version INTEGER NOT NULL,
                checkpoint TEXT NOT NULL, result TEXT,
                turn_id TEXT, reason TEXT, updated_ms INTEGER NOT NULL,
                UNIQUE (robot_id, chat_id, persona_id, request_key)
            )""")
            db.execute("""CREATE INDEX IF NOT EXISTS phase8_reminders_due
                          ON phase8_reminders(robot_id, status, due_ms)""")

    @staticmethod
    def _row(cursor):
        columns = [item[0] for item in cursor.description]
        result = cursor.fetchone()
        if result is None:
            return None
        data = dict(zip(columns, result, strict=True))
        data["checkpoint"] = json.loads(data["checkpoint"])
        data["result"] = json.loads(data["result"]) if data["result"] else None
        return data

    def create(self, task):
        with sqlite3.connect(self.path, timeout=5) as db:
            db.execute("""INSERT OR IGNORE INTO phase8_reminders
                 (task_id, robot_id, chat_id, persona_id, session_epoch,
                  generation, request_key, text, due_ms, expires_ms, status,
                  version, checkpoint, updated_ms)
                 VALUES (:task_id,:robot_id,:chat_id,:persona_id,:session_epoch,
                  :generation,:request_key,:text,:due_ms,:expires_ms,:status,
                  1,:checkpoint,:updated_ms)""", {**task,
                      "checkpoint": json.dumps(task["checkpoint"], ensure_ascii=False)})
            return self._row(db.execute(
                """SELECT * FROM phase8_reminders WHERE robot_id=? AND chat_id=?
                   AND persona_id=? AND request_key=?""",
                (task["robot_id"], task["chat_id"], task["persona_id"], task["request_key"]),
            ))

    def get(self, robot_id, task_id):
        with sqlite3.connect(self.path, timeout=5) as db:
            return self._row(db.execute("""SELECT * FROM phase8_reminders
                WHERE robot_id=? AND task_id=?""", (robot_id, task_id)))

    def list(self, robot_id, chat_id, persona_id):
        with sqlite3.connect(self.path, timeout=5) as db:
            cursor = db.execute("""SELECT * FROM phase8_reminders WHERE robot_id=?
                AND chat_id=? AND persona_id=? ORDER BY updated_ms DESC LIMIT 64""",
                (robot_id, chat_id, persona_id))
            return [self._row(db.execute("SELECT * FROM phase8_reminders WHERE task_id=?",
                                   (item[0],))) for item in cursor.fetchall()]

    def list_robot(self, robot_id):
        with sqlite3.connect(self.path, timeout=5) as db:
            ids = db.execute("SELECT task_id FROM phase8_reminders WHERE robot_id=?",
                             (robot_id,)).fetchall()
            return [self._row(db.execute("SELECT * FROM phase8_reminders WHERE task_id=?",
                                   (item[0],))) for item in ids]

    def next_due(self, robot_id):
        # Pending reminders remain durable until claimed or explicitly cancelled.
        with sqlite3.connect(self.path, timeout=5) as db:
            return self._row(db.execute("""SELECT * FROM phase8_reminders
                WHERE robot_id=? AND status='scheduled'
                ORDER BY due_ms LIMIT 1""", (robot_id,)))

    def due_batch(self, robot_id, now_ms):
        with sqlite3.connect(self.path, timeout=5) as db:
            ids = db.execute("""SELECT task_id FROM phase8_reminders
                WHERE robot_id=? AND status='scheduled' AND due_ms<=?
                ORDER BY due_ms""", (robot_id, now_ms)).fetchall()
            return [self._row(db.execute(
                "SELECT * FROM phase8_reminders WHERE task_id=?", (task_id,)))
                for (task_id,) in ids]

    def claim_batch(self, robot_id, checkpoints, now_ms):
        if not checkpoints:
            return False
        with sqlite3.connect(self.path, timeout=5) as db:
            db.execute("BEGIN IMMEDIATE")
            for task_id in checkpoints:
                row = db.execute("SELECT status FROM phase8_reminders WHERE robot_id=? AND task_id=?",
                                 (robot_id, task_id)).fetchone()
                if row != ("scheduled",):
                    return False
            for task_id, checkpoint in checkpoints.items():
                db.execute("""UPDATE phase8_reminders SET status='submitting',
                    version=version+1, updated_ms=?, checkpoint=?, result=?
                    WHERE robot_id=? AND task_id=?""", (now_ms,
                    json.dumps(checkpoint, ensure_ascii=False),
                    json.dumps(checkpoint["result"], ensure_ascii=False),
                    robot_id, task_id))
            return True

    def adopt_restart(self, identity, now_ms):
        # Repeated restarts must not strand an earlier pending task. Explicit
        # Chat/Persona switches already invalidate their previous tasks.
        with sqlite3.connect(self.path, timeout=5) as db:
            db.execute("""UPDATE phase8_reminders SET session_epoch=?, generation=?,
                version=version+1, updated_ms=?,
                status=CASE WHEN status='expired' THEN 'scheduled' ELSE status END,
                reason=CASE WHEN due_ms<=? THEN 'restart_recovered' ELSE reason END
                WHERE robot_id=? AND chat_id=? AND persona_id=?
                AND (status IN ('scheduled', 'paused') OR
                     (status='expired' AND reason='late_reminder'))""",
                (identity["session_epoch"], identity["generation"], now_ms, now_ms,
                 identity["robot_id"], identity["chat_id"], identity["persona_id"]))

    def invalidate_owner(self, identity, now_ms):
        with sqlite3.connect(self.path, timeout=5) as db:
            return db.execute("""UPDATE phase8_reminders SET status='stale_session',
                reason='session_changed', version=version+1, updated_ms=?
                WHERE robot_id=? AND chat_id=? AND persona_id=?
                AND session_epoch=? AND generation=?
                AND status IN ('scheduled','paused')""",
                (now_ms, identity["robot_id"], identity["chat_id"],
                 identity["persona_id"], identity["session_epoch"],
                 identity["generation"])).rowcount

    def change(self, robot_id, task_id, old, new, now_ms, *, checkpoint=None,
               result=None, turn_id=None, reason=None):
        updates = ["status=?", "version=version+1", "updated_ms=?", "reason=?"]
        args = [new, now_ms, reason]
        if checkpoint is not None:
            updates.append("checkpoint=?")
            args.append(json.dumps(checkpoint, ensure_ascii=False))
        if result is not None:
            updates.append("result=?")
            args.append(json.dumps(result, ensure_ascii=False))
        if turn_id is not None:
            updates.append("turn_id=?")
            args.append(turn_id)
        args.extend((robot_id, task_id, old))
        with sqlite3.connect(self.path, timeout=5) as db:
            cursor = db.execute("UPDATE phase8_reminders SET " + ",".join(updates) +
                                " WHERE robot_id=? AND task_id=? AND status=?", args)
            row = self._row(db.execute("""SELECT * FROM phase8_reminders
                WHERE robot_id=? AND task_id=?""", (robot_id, task_id)))
            return bool(cursor.rowcount), row
