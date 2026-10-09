"""SQLite upgrade must distinguish new typed turns without fabricating legacy audio evidence."""

import sqlite3

from annie_adapters import RuntimeStore
from annie_brain.context import select_restorable_context


def test_legacy_turns_remain_unknown_after_lossless_schema_upgrade(tmp_path):
    path = tmp_path / "legacy.db"
    store = RuntimeStore(path)
    store.migrate()
    store.create_conversation_turn({
        "turn_id": "old", "robot_id": "annie-a", "room_id": "roomv2",
        "chat_id": "daily", "persona_id": "annie", "persona_revision": 1,
        "session_epoch": 1, "generation": 1, "user_text": "之前问了什么",
        "input_mode": "text", "status": "completed", "created_ms": 1,
        "updated_ms": 1,
    })
    store.finish_conversation_turn(
        "annie-a", "old", status="completed", generated_text="你平时喜欢怎么放松？",
        updated_ms=2,
    )
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE conversation_turns DROP COLUMN input_mode")
        db.execute("PRAGMA user_version=8")
    store.preflight()
    store.migrate()
    store.migrate()  # repeated startup is also safe
    restored = store.recent_conversation_context("annie-a", "daily", "annie", 8)
    assert len(restored) == 1
    assert restored[0]["input_mode"] == "unknown"
    assert restored[0]["assistant_generated_text"] == "你平时喜欢怎么放松？"
    assert select_restorable_context(restored, speech_enabled=True)["turns"][0][
        "assistant_text"] == ""
    store.create_conversation_turn({
        "turn_id": "new", "robot_id": "annie-a", "room_id": "roomv2",
        "chat_id": "daily", "persona_id": "annie", "persona_revision": 1,
        "session_epoch": 2, "generation": 2, "user_text": "你猜",
        "input_mode": "text", "status": "completed", "created_ms": 3,
        "updated_ms": 3,
    })
    store.finish_conversation_turn(
        "annie-a", "new", status="completed", generated_text="我猜你想先好好睡一觉。",
        updated_ms=4,
    )
    rows = store.recent_conversation_context("annie-a", "daily", "annie", 8)
    assert [r["input_mode"] for r in rows] == ["unknown", "text"]
    packet = select_restorable_context(rows, speech_enabled=True)
    assert packet["turns"][-1]["assistant_text"] == "我猜你想先好好睡一觉。"
