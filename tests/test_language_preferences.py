"""Explicit language preferences persist by robot/Chat, never by Persona."""

import sqlite3

import pytest
from annie_adapters import RuntimeStore


@pytest.fixture
def store(tmp_path):
    instance = RuntimeStore(tmp_path / "runtime.db")
    instance.migrate()
    return instance


def put(store, *, robot_id="a", chat_id="daily", scope="chat", language="zh",
        dialect="sichuan", call_id="call-1", updated_ms=10, source="explicit_user",
        source_turn_id="turn-1"):
    return store.set_language_preference(
        robot_id, chat_id, scope=scope, language=language, dialect=dialect,
        source_turn_id=source_turn_id, call_id=call_id, updated_ms=updated_ms, source=source,
    )


def clear(store, *, robot_id="a", chat_id="daily", scope="chat", call_id="clear-1"):
    return store.clear_language_preference(
        robot_id, chat_id, scope=scope, source_turn_id="turn-2", call_id=call_id,
        updated_ms=20,
    )


def session(store, persona_id, *, robot_id="a", chat_id="daily"):
    store.save_conversation_session({
        "robot_id": robot_id, "room_id": "roomv2", "chat_id": chat_id,
        "persona_id": persona_id, "persona_revision": 1, "session_epoch": 1,
        "generation": 1, "updated_ms": 1,
    })


def test_robot_chat_preferences_restart_persona_and_isolation(store):
    assert store.load_language_preferences("a", "daily") == {"robot": None, "chat": None}
    robot = put(store, scope="robot", language="en", dialect="unknown")
    chat = put(store)
    session(store, "first")
    session(store, "second")
    reopened = RuntimeStore(store.path)
    reopened.preflight()
    reopened.migrate()
    assert reopened.load_language_preferences("a", "daily") == {"robot": robot, "chat": chat}
    assert reopened.load_language_preferences("a", "other") == {"robot": robot, "chat": None}
    assert reopened.load_language_preferences("b", "daily") == {"robot": None, "chat": None}
    assert chat["scope"] == "chat"
    assert chat["source"] == "explicit_user"
    assert chat["revision"] == 1
    assert chat["source_turn_id"] == "turn-1"


def test_revisions_clear_and_retry_preserve_history(store):
    first = put(store)
    # Replaying an old call never promotes its value above a later committed revision.
    second = put(store, language="yue", dialect="cantonese", call_id="call-2", updated_ms=1)
    assert second["revision"] == 2
    assert put(store) == first
    assert store.load_language_preferences("a", "daily")["chat"] == second
    tombstone = clear(store)
    assert tombstone["status"] == "cleared"
    assert tombstone["revision"] == 3
    assert store.load_language_preferences("a", "daily")["chat"] is None
    assert clear(store) == tombstone
    assert put(store) == first
    assert store.load_language_preferences("a", "daily")["chat"] is None
    third = put(store, language="ja", dialect="unknown", call_id="call-3")
    assert third["revision"] == 4
    with sqlite3.connect(store.path) as db:
        rows = db.execute(
            "SELECT revision,language,status FROM chat_language_preferences "
            "WHERE robot_id='a' AND chat_id='daily' ORDER BY revision"
        ).fetchall()
    assert rows == [(1, "zh", "set"), (2, "yue", "set"), (3, None, "cleared"), (4, "ja", "set")]
    with pytest.raises(ValueError, match="call_id"):
        put(store, language="en", dialect="unknown")
    assert store.load_language_preferences("a", "daily")["chat"] == third


def test_clear_robot_does_not_clear_chat_and_clear_empty_is_revision(store):
    robot = put(store, scope="robot")
    chat = put(store)
    tombstone = clear(store, scope="robot")
    assert tombstone["revision"] == robot["revision"] + 1
    assert store.load_language_preferences("a", "daily") == {"robot": None, "chat": chat}
    empty_clear = clear(store, robot_id="b")
    assert empty_clear["revision"] == 1
    assert store.load_language_preferences("b", "daily")["chat"] is None


@pytest.mark.parametrize("override", [
    {"source": "auto"}, {"source": "inferred"}, {"source": "asr"},
    {"source": True}, {"scope": "persona"}, {"scope": True},
    {"language": "ms"}, {"language": "auto"}, {"language": True},
    {"language": "en", "dialect": "sichuan"},
    {"language": "zh", "dialect": "cantonese"},
    {"language": "yue", "dialect": "mandarin"}, {"dialect": "auto"},
    {"robot_id": ""}, {"chat_id": " "}, {"call_id": ""},
    {"call_id": "x" * 257}, {"source_turn_id": "x" * 257},
    {"source_turn_id": True}, {"updated_ms": True}, {"updated_ms": -1},
])
def test_invalid_or_inferred_preferences_never_persist(store, override):
    with pytest.raises(ValueError):
        put(store, **override)
    assert store.load_language_preferences("a", "daily") == {"robot": None, "chat": None}


@pytest.mark.parametrize("language", [
    "zh", "yue", "en", "ja", "de", "ko", "ru", "fr", "pt", "ar", "it", "es",
    "hi", "id", "th", "tr", "uk", "vi",
])
def test_all_supported_language_codes_with_unknown_dialect(store, language):
    assert put(store, language=language, dialect="unknown")["language"] == language


@pytest.mark.parametrize("dialect", ["mandarin", "sichuan", "henan", "shaanxi"])
def test_supported_chinese_dialects(store, dialect):
    assert put(store, dialect=dialect)["dialect"] == dialect


def test_failed_insert_leaves_current_revision_and_history_intact(store):
    previous = put(store)
    with sqlite3.connect(store.path) as db:
        db.execute("""CREATE TRIGGER preference_failure BEFORE INSERT ON chat_language_preferences
                    WHEN NEW.call_id='fail' BEGIN SELECT RAISE(ABORT, 'injected failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        put(store, call_id="fail", language="ja", dialect="unknown")
    assert store.load_language_preferences("a", "daily")["chat"] == previous
    next_record = put(store, call_id="success")
    assert next_record["revision"] == 2


def test_delete_one_persona_preserves_shared_chat_preference(store):
    robot = put(store, scope="robot")
    chat = put(store)
    session(store, "first")
    session(store, "second")
    store.delete_conversation("a", "daily", "first")
    assert store.load_language_preferences("a", "daily") == {"robot": robot, "chat": chat}
    store.delete_conversation("a", "daily", "second")
    assert store.load_language_preferences("a", "daily") == {"robot": robot, "chat": None}
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM chat_language_preferences").fetchone()[0] == 0


def test_other_persona_turn_without_chat_row_still_owns_preference(store):
    chat = put(store)
    store.create_conversation_turn({
        "turn_id": "orphan", "robot_id": "a", "room_id": "roomv2", "chat_id": "daily",
        "persona_id": "second", "persona_revision": 1, "session_epoch": 1,
        "generation": 1, "user_text": "你好", "status": "completed", "created_ms": 1,
        "updated_ms": 1,
    })
    store.delete_conversation("a", "daily", "first")
    assert store.load_language_preferences("a", "daily")["chat"] == chat
    store.delete_conversation("a", "daily", "second")
    assert store.load_language_preferences("a", "daily")["chat"] is None


def test_additive_migration_keeps_existing_version_and_rows(store):
    session(store, "first")
    with sqlite3.connect(store.path) as db:
        previous_version = db.execute("PRAGMA user_version").fetchone()[0]
        db.execute("DROP TABLE robot_language_preferences")
        db.execute("DROP TABLE chat_language_preferences")
    store.preflight()
    store.migrate()
    store.migrate()
    assert store.load_language_preferences("a", "daily") == {"robot": None, "chat": None}
    with sqlite3.connect(store.path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == previous_version
        assert db.execute("SELECT persona_id FROM conversation_chats").fetchone()[0] == "first"


def test_external_ids_are_bound_sql_parameters(store):
    malicious_id = "daily'); DROP TABLE conversation_turns; --"
    record = put(store, chat_id=malicious_id)
    assert store.load_language_preferences("a", malicious_id)["chat"] == record
    assert store.load_language_preferences("a", "daily")["chat"] is None
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM conversation_turns").fetchone()[0] == 0


def test_load_returns_consistent_two_scope_snapshot_during_concurrent_write(store):
    from contextlib import contextmanager

    robot = put(store, scope="robot")
    chat = put(store)
    writer = RuntimeStore(store.path)
    original_connect = store.connect
    updated = []

    @contextmanager
    def observing_connect():
        with original_connect() as db:
            def interleave(statement):
                if "SELECT * FROM chat_language_preferences" in statement and not updated:
                    updated.append(put(writer, language="ja", dialect="unknown", call_id="new"))
            db.set_trace_callback(interleave)
            yield db

    # Real WAL connections reproduce another writer committing between the two
    # scope reads, without replacing SQLite or faking preference behavior.
    store.connect = observing_connect
    assert store.load_language_preferences("a", "daily") == {"robot": robot, "chat": chat}
    assert updated
    store.connect = original_connect
    assert store.load_language_preferences("a", "daily")["chat"] == updated[0]


@pytest.mark.parametrize("operation", ["set", "clear"])
@pytest.mark.parametrize("checkpoint", [1, 2, 3])
def test_cancelled_preference_transaction_rolls_back_at_each_checkpoint(store, operation, checkpoint):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    previous = put(store)
    entered = Event()
    resume = Event()
    cancelled = Event()
    calls = []

    def validate_current():
        calls.append(len(calls) + 1)
        if len(calls) == checkpoint:
            entered.set()
            assert resume.wait(5), "test did not release validation checkpoint"
        if cancelled.is_set():
            raise ValueError("stale language tool")

    def write():
        arguments = dict(scope="chat", source_turn_id="new-turn", call_id="cancelled",
                         updated_ms=30, validate_current=validate_current)
        if operation == "set":
            return store.set_language_preference(
                "a", "daily", language="ja", dialect="unknown", **arguments,
            )
        return store.clear_language_preference("a", "daily", **arguments)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(write)
        try:
            assert entered.wait(2), pending.exception() if pending.done() else "checkpoint missed"
            cancelled.set()
        finally:
            resume.set()
        with pytest.raises(ValueError, match="stale language tool"):
            pending.result(timeout=5)
    assert calls == list(range(1, checkpoint + 1))
    assert store.load_language_preferences("a", "daily")["chat"] == previous
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM chat_language_preferences").fetchone()[0] == 1


def test_cancelled_idempotent_retry_is_validated_before_return(store):
    previous = put(store)
    calls = []

    def validate_current():
        calls.append(True)
        if len(calls) == 2:
            raise ValueError("stale retry")

    with pytest.raises(ValueError, match="stale retry"):
        store.set_language_preference(
            "a", "daily", scope="chat", language="zh", dialect="sichuan",
            source_turn_id="turn-1", call_id="call-1", updated_ms=10,
            validate_current=validate_current,
        )
    assert len(calls) == 2
    assert store.load_language_preferences("a", "daily")["chat"] == previous


@pytest.mark.parametrize("operation", ["set", "clear"])
def test_queued_tool_cancelled_before_sqlite_transaction_never_writes(store, operation):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    from threading import Event

    previous = put(store)
    entered = Event()
    resume = Event()
    cancelled = Event()
    original_connect = store.connect

    @contextmanager
    def queued_connect():
        entered.set()
        assert resume.wait(5), "test did not release queued database write"
        with original_connect() as db:
            yield db

    def validate_current():
        if cancelled.is_set():
            raise ValueError("queued tool cancelled")

    def write():
        arguments = dict(scope="chat", source_turn_id="queued-turn", call_id="queued",
                         updated_ms=40, validate_current=validate_current)
        if operation == "set":
            return store.set_language_preference(
                "a", "daily", language="en", dialect="unknown", **arguments,
            )
        return store.clear_language_preference("a", "daily", **arguments)

    store.connect = queued_connect
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(write)
            try:
                assert entered.wait(2)
                cancelled.set()
            finally:
                resume.set()
            with pytest.raises(ValueError, match="queued tool cancelled"):
                pending.result(timeout=5)
    finally:
        store.connect = original_connect
    assert store.load_language_preferences("a", "daily")["chat"] == previous
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM chat_language_preferences").fetchone()[0] == 1


def test_valid_current_callback_commits_set_clear_and_idempotent_retry(store):
    calls = []

    def validate_current():
        calls.append(True)

    arguments = dict(scope="chat", source_turn_id="turn", call_id="validated-set",
                     updated_ms=50, validate_current=validate_current)
    record = store.set_language_preference(
        "a", "daily", language="ja", dialect="unknown", **arguments,
    )
    assert len(calls) == 3
    assert store.load_language_preferences("a", "daily")["chat"] == record
    assert store.set_language_preference(
        "a", "daily", language="ja", dialect="unknown", **arguments,
    ) == record
    assert len(calls) == 5
    store.clear_language_preference("a", "daily", **{**arguments, "call_id": "validated-clear"})
    assert len(calls) == 8
    assert store.load_language_preferences("a", "daily")["chat"] is None
