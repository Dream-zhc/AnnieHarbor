"""SQLite rows for her mood and her own day (annie_brain.inner_life).

Pure additive tables: user_version stays unchanged. One mood row per robot;
the day is a bounded, append-mostly list pruned by age on every insert.
"""

import json


def migrate_inner_life(db):
    db.execute("""CREATE TABLE IF NOT EXISTS robot_inner_state (
        robot_id TEXT PRIMARY KEY,
        valence REAL NOT NULL, energy REAL NOT NULL,
        tone TEXT NOT NULL, reason TEXT NOT NULL, reason_ms INTEGER,
        last_interaction_ms INTEGER, updated_ms INTEGER NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS robot_experiences (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        robot_id TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
        person TEXT, chat_id TEXT,
        count INTEGER NOT NULL DEFAULT 1 CHECK(count > 0),
        created_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL)""")
    db.execute("""CREATE INDEX IF NOT EXISTS robot_experiences_time_idx
        ON robot_experiences(robot_id, created_ms)""")
    # Things someone said they would do and that she may ask about later.
    # person is a recognised name; without one the thread stays in its Chat.
    db.execute("""CREATE TABLE IF NOT EXISTS robot_open_threads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        robot_id TEXT NOT NULL, person TEXT, chat_id TEXT,
        text TEXT NOT NULL, follow_up TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL CHECK(status IN ('open','resolved')),
        asks INTEGER NOT NULL DEFAULT 0, asked_ms INTEGER,
        created_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL)""")
    db.execute("""CREATE INDEX IF NOT EXISTS robot_open_threads_idx
        ON robot_open_threads(robot_id, status, created_ms)""")
    # One nightly look back per robot and day; people carries the merged
    # impressions so far, chat_ids the Chats its day lines came from.
    db.execute("""CREATE TABLE IF NOT EXISTS robot_reflections (
        robot_id TEXT NOT NULL, day TEXT NOT NULL,
        people_json TEXT NOT NULL, self_text TEXT NOT NULL,
        chat_ids_json TEXT NOT NULL, created_ms INTEGER NOT NULL,
        PRIMARY KEY(robot_id, day))""")


def _rows(cursor):
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def load_mood(store, robot_id):
    with store.connect() as db:
        rows = _rows(db.execute(
            """SELECT valence,energy,tone,reason,reason_ms,last_interaction_ms,updated_ms
               FROM robot_inner_state WHERE robot_id=?""", (robot_id,)))
    return rows[0] if rows else None


def save_mood(store, robot_id, row):
    with store.connect() as db:
        db.execute(
            """INSERT INTO robot_inner_state
               (robot_id,valence,energy,tone,reason,reason_ms,last_interaction_ms,updated_ms)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(robot_id) DO UPDATE SET valence=excluded.valence,
               energy=excluded.energy, tone=excluded.tone, reason=excluded.reason,
               reason_ms=excluded.reason_ms, last_interaction_ms=excluded.last_interaction_ms,
               updated_ms=excluded.updated_ms""",
            (robot_id, row["valence"], row["energy"], row["tone"], row["reason"],
             row["reason_ms"], row["last_interaction_ms"], row["updated_ms"]))


def add_experience(store, robot_id, kind, text, *, now_ms, keep_after_ms,
                   person=None, chat_id=None):
    with store.connect() as db:
        db.execute("DELETE FROM robot_experiences WHERE robot_id=? AND created_ms<?",
                   (robot_id, keep_after_ms))
        cursor = db.execute(
            """INSERT INTO robot_experiences
               (robot_id,kind,text,person,chat_id,count,created_ms,updated_ms)
               VALUES (?,?,?,?,?,1,?,?)""",
            (robot_id, kind, text, person, chat_id, now_ms, now_ms))
        row_id = cursor.lastrowid
    return {"id": row_id, "kind": kind, "text": text, "person": person, "chat_id": chat_id,
            "count": 1, "created_ms": now_ms, "updated_ms": now_ms}


def bump_experience(store, robot_id, row_id, *, now_ms):
    with store.connect() as db:
        db.execute("""UPDATE robot_experiences SET count=count+1, updated_ms=?
                      WHERE robot_id=? AND id=?""", (now_ms, robot_id, row_id))


def recent_experiences(store, robot_id, *, since_ms, until_ms=1 << 62, limit=40):
    """Newest first."""
    with store.connect() as db:
        return _rows(db.execute(
            """SELECT id,kind,text,person,chat_id,count,created_ms,updated_ms
               FROM robot_experiences WHERE robot_id=? AND created_ms>=? AND created_ms<?
               ORDER BY created_ms DESC, id DESC LIMIT ?""",
            (robot_id, since_ms, until_ms, limit)))


def forget_chat(store, robot_id, chat_id):
    """A deleted Chat takes the day lines summarised from it."""
    with store.connect() as db:
        return db.execute("DELETE FROM robot_experiences WHERE robot_id=? AND chat_id=?",
                          (robot_id, chat_id)).rowcount


def forget_person(store, robot_id, name):
    """“忘掉我”: every line that names this person goes too."""
    with store.connect() as db:
        return db.execute(
            """DELETE FROM robot_experiences WHERE robot_id=?
               AND (person=? OR instr(text, ?) > 0)""", (robot_id, name, name)).rowcount


def open_threads(store, robot_id, *, since_ms):
    with store.connect() as db:
        return _rows(db.execute(
            """SELECT id,person,chat_id,text,follow_up,status,asks,asked_ms,created_ms,updated_ms
               FROM robot_open_threads WHERE robot_id=? AND status='open' AND created_ms>=?
               ORDER BY created_ms DESC, id DESC LIMIT 100""", (robot_id, since_ms)))


def add_thread(store, robot_id, text, follow_up, *, person, chat_id, now_ms):
    with store.connect() as db:
        cursor = db.execute(
            """INSERT INTO robot_open_threads
               (robot_id,person,chat_id,text,follow_up,status,asks,asked_ms,created_ms,updated_ms)
               VALUES (?,?,?,?,?,'open',0,NULL,?,?)""",
            (robot_id, person, chat_id, text, follow_up, now_ms, now_ms))
        return cursor.lastrowid


def resolve_threads(store, robot_id, ids, *, now_ms):
    with store.connect() as db:
        db.executemany("""UPDATE robot_open_threads SET status='resolved', updated_ms=?
                          WHERE robot_id=? AND id=?""", [(now_ms, robot_id, i) for i in ids])


def mark_threads_asked(store, robot_id, ids, *, now_ms):
    with store.connect() as db:
        db.executemany("""UPDATE robot_open_threads SET asks=asks+1, asked_ms=?, updated_ms=?
                          WHERE robot_id=? AND id=?""",
                       [(now_ms, now_ms, robot_id, i) for i in ids])


def forget_threads(store, robot_id, *, chat_id=None, person=None):
    with store.connect() as db:
        if chat_id is not None:
            return db.execute("DELETE FROM robot_open_threads WHERE robot_id=? AND chat_id=?",
                              (robot_id, chat_id)).rowcount
        return db.execute(
            """DELETE FROM robot_open_threads WHERE robot_id=?
               AND (person=? OR instr(text, ?) > 0)""", (robot_id, person, person)).rowcount


def latest_reflection(store, robot_id):
    with store.connect() as db:
        rows = _rows(db.execute(
            """SELECT day,people_json,self_text,chat_ids_json,created_ms FROM robot_reflections
               WHERE robot_id=? ORDER BY day DESC LIMIT 1""", (robot_id,)))
    if not rows:
        return None
    row = rows[0]
    return {"day": row["day"], "people": json.loads(row["people_json"]),
            "self": row["self_text"], "chat_ids": json.loads(row["chat_ids_json"]),
            "created_ms": row["created_ms"]}


def save_reflection(store, robot_id, reflection):
    with store.connect() as db:
        db.execute(
            """INSERT INTO robot_reflections
               (robot_id,day,people_json,self_text,chat_ids_json,created_ms)
               VALUES (?,?,?,?,?,?) ON CONFLICT(robot_id,day) DO UPDATE SET
               people_json=excluded.people_json, self_text=excluded.self_text,
               chat_ids_json=excluded.chat_ids_json, created_ms=excluded.created_ms""",
            (robot_id, reflection["day"], json.dumps(reflection["people"], ensure_ascii=False),
             reflection["self"], json.dumps(reflection["chat_ids"], ensure_ascii=False),
             reflection["created_ms"]))


def forget_reflection(store, robot_id, *, person=None, chat_id=None):
    """“忘掉我” drops that impression; a deleted Chat drops every impression built on it."""
    with store.connect() as db:
        rows = _rows(db.execute(
            "SELECT day,people_json,chat_ids_json FROM robot_reflections WHERE robot_id=?",
            (robot_id,)))
        for row in rows:
            people = json.loads(row["people_json"])
            chats = json.loads(row["chat_ids_json"])
            if person is not None:
                people = {name: text for name, text in people.items()
                          if name != person and person not in text}
            elif chat_id in chats:
                people = {}
                chats = [item for item in chats if item != chat_id]
            db.execute("""UPDATE robot_reflections SET people_json=?, chat_ids_json=?
                          WHERE robot_id=? AND day=?""",
                       (json.dumps(people, ensure_ascii=False),
                        json.dumps(chats, ensure_ascii=False), robot_id, row["day"]))
