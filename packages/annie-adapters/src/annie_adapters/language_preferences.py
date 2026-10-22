"""SQLite explicit preferences: append-only revisions, last successful write wins.

Persona is deliberately absent from this boundary. A retry returns its original
revision and never changes which revision is current. A clear appends a tombstone
instead of deleting the idempotency/history evidence. Deleting the last owner of
an entire Chat is the sole operation that purges its preference history.
"""

import sqlite3
from collections.abc import Callable

SUPPORTED_LANGUAGES = frozenset({
    "zh", "yue", "en", "ja", "de", "ko", "ru", "fr", "pt", "ar", "it", "es",
    "hi", "id", "th", "tr", "uk", "vi", "ms", "fil",
})
DIALECTS_BY_LANGUAGE = {
    "zh": frozenset({"unknown", "mandarin", "sichuan", "henan", "shaanxi"}),
    "yue": frozenset({"unknown", "cantonese"}),
}
_TABLES = {"robot": "robot_language_preferences", "chat": "chat_language_preferences"}


def _identifier(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError(f"{name} must be a non-empty string of at most 256 characters")
    return value


def validate_language_pair(language, dialect):
    if not isinstance(language, str) or language not in SUPPORTED_LANGUAGES:
        raise ValueError("unsupported language")
    if not isinstance(dialect, str) or dialect not in DIALECTS_BY_LANGUAGE.get(
        language, frozenset({"unknown"}),
    ):
        raise ValueError("unsupported language/dialect combination")


def migrate_language_preferences(db):
    """Pure additive extension; user_version 9 remains backwards-compatible."""
    for scope, table in _TABLES.items():
        chat_column = "chat_id TEXT NOT NULL," if scope == "chat" else ""
        key = "robot_id,chat_id" if scope == "chat" else "robot_id"
        # Table names and SQL fragments are private, fixed constants, never input.
        db.execute(f"""CREATE TABLE IF NOT EXISTS {table} (
            robot_id TEXT NOT NULL, {chat_column}
            revision INTEGER NOT NULL CHECK(revision > 0),
            language TEXT, dialect TEXT,
            status TEXT NOT NULL CHECK(status IN ('set','cleared')),
            source TEXT NOT NULL CHECK(source='explicit_user'),
            source_turn_id TEXT, call_id TEXT NOT NULL,
            updated_ms INTEGER NOT NULL CHECK(updated_ms >= 0),
            PRIMARY KEY({key},revision), UNIQUE({key},call_id),
            CHECK((status='set' AND language IS NOT NULL AND dialect IS NOT NULL)
               OR (status='cleared' AND language IS NULL AND dialect IS NULL)))""")


def _scope(robot_id, chat_id, scope):
    _identifier(robot_id, "robot_id")
    _identifier(chat_id, "chat_id")
    if not isinstance(scope, str) or scope not in _TABLES:
        raise ValueError("scope must be robot or chat")
    if scope == "robot":
        return _TABLES[scope], "robot_id=?", (robot_id,)
    return _TABLES[scope], "robot_id=? AND chat_id=?", (robot_id, chat_id)


def _record(row, scope):
    if row is None:
        return None
    return {"scope": scope, "chat_id": None, **dict(row)}


def load_language_preferences(store, robot_id, chat_id):
    """Return latest non-cleared preference per scope, without inferred fallback."""
    result = {}
    with store.connect() as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")  # The two scopes form one consistent read snapshot.
        for scope in _TABLES:
            table, where, params = _scope(robot_id, chat_id, scope)
            row = db.execute(
                f"SELECT * FROM {table} WHERE {where} ORDER BY revision DESC LIMIT 1", params,
            ).fetchone()
            result[scope] = _record(row, scope) if row and row["status"] == "set" else None
    return result


def _write(store, robot_id, chat_id, *, scope, language, dialect, status,
           source_turn_id, call_id, updated_ms, source, validate_current):
    table, where, params = _scope(robot_id, chat_id, scope)
    _identifier(call_id, "call_id")
    if source_turn_id is not None:
        _identifier(source_turn_id, "source_turn_id")
    if source != "explicit_user":
        raise ValueError("only explicit_user preferences may be persisted")
    if type(updated_ms) is not int or not 0 <= updated_ms <= 2**63 - 1:
        raise ValueError("updated_ms must be a non-negative SQLite integer")
    with store.connect() as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        if validate_current is not None:
            validate_current()
        previous = db.execute(
            f"SELECT * FROM {table} WHERE {where} AND call_id=?", (*params, call_id),
        ).fetchone()
        if previous is not None:
            # A retry may acquire a later wall-clock timestamp, but its intent
            # and provenance must remain identical. Never replay its effect.
            intended = (language, dialect, status, source, source_turn_id)
            actual = tuple(previous[key] for key in (
                "language", "dialect", "status", "source", "source_turn_id",
            ))
            if intended != actual:
                raise ValueError("call_id already used for a different language preference")
            if validate_current is not None:
                validate_current()
            return _record(previous, scope)
        revision = db.execute(
            f"SELECT COALESCE(MAX(revision),0)+1 FROM {table} WHERE {where}", params,
        ).fetchone()[0]
        columns = "robot_id,chat_id" if scope == "chat" else "robot_id"
        fields = (revision, language, dialect, status, source, source_turn_id, call_id, updated_ms)
        values = (*params, *fields)
        placeholders = ",".join("?" for _ in values)
        if validate_current is not None:
            validate_current()
        db.execute(
            f"""INSERT INTO {table} ({columns},revision,language,dialect,status,source,
                source_turn_id,call_id,updated_ms) VALUES ({placeholders})""", values,
        )
        row = db.execute(
            f"SELECT * FROM {table} WHERE {where} AND revision=?", (*params, revision),
        ).fetchone()
        if validate_current is not None:
            validate_current()
        return _record(row, scope)


def set_language_preference(store, robot_id, chat_id, *, scope, language, dialect,
                            source_turn_id, call_id, updated_ms, source="explicit_user",
                            validate_current: Callable[[], None] | None = None):
    """Persist one explicit set; validation raises to roll back stale callers.

    The optional zero-argument validator runs after acquiring the write
    transaction, immediately before INSERT, and immediately before returning
    through the commit boundary. An idempotent retry also validates before return.
    """
    validate_language_pair(language, dialect)
    return _write(
        store, robot_id, chat_id, scope=scope, language=language, dialect=dialect,
        status="set", source_turn_id=source_turn_id, call_id=call_id,
        updated_ms=updated_ms, source=source, validate_current=validate_current,
    )


def clear_language_preference(store, robot_id, chat_id, *, scope, source_turn_id,
                              call_id, updated_ms, source="explicit_user",
                              validate_current: Callable[[], None] | None = None):
    """Append an explicit clear, using the same transactional validator as set."""
    return _write(
        store, robot_id, chat_id, scope=scope, language=None, dialect=None,
        status="cleared", source_turn_id=source_turn_id, call_id=call_id,
        updated_ms=updated_ms, source=source, validate_current=validate_current,
    )


def delete_unowned_chat_language_preferences(db, robot_id, chat_id, persona_id):
    """Purge only after no surviving Persona Chat, turn, or session owns it."""
    owned = db.execute(
        """SELECT 1 FROM conversation_chats WHERE robot_id=? AND chat_id=?
           UNION ALL SELECT 1 FROM conversation_turns WHERE robot_id=? AND chat_id=?
           UNION ALL SELECT 1 FROM conversation_sessions
               WHERE robot_id=? AND chat_id=? AND persona_id<>? LIMIT 1""",
        (robot_id, chat_id, robot_id, chat_id, robot_id, chat_id, persona_id),
    ).fetchone()
    if owned is None:
        db.execute(
            "DELETE FROM chat_language_preferences WHERE robot_id=? AND chat_id=?",
            (robot_id, chat_id),
        )
