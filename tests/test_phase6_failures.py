"""Phase 6: forgotten facts and a failing memory model cannot violate scope."""

import asyncio
import sqlite3
import time
from contextlib import closing

from annie_adapters import RuntimeStore
from annie_brain import memory
from annie_brain.memory import MemoryService


class DownChat:
    async def close(self):
        pass

    async def complete(self, _system, _user, *, max_tokens):
        raise RuntimeError("provider_down")


def test_permanent_tombstone_even_after_newer_background_turn(tmp_path):
    async def exercise():
        store = RuntimeStore(tmp_path / "memory.db")
        store.migrate()
        service = MemoryService(store, DownChat(), ("a",))
        ident, version = await service.put(
            "a", "warm", "semantic", "drink", "用户喜欢茶",
            source_turn_id="old", source_created_ms=1000,
        )
        await service.change("a", "warm", ident, version)
        assert await service.put(
            "a", "warm", "semantic", "drink", "用户喜欢茶",
            source_turn_id="new", source_created_ms=9999999999999,
        ) is None
        assert await service.list("a", "warm") == []
        assert (await service.list("a", "warm", True))[0]["status"] == "deleted"

    asyncio.run(exercise())


def test_model_failure_visible_without_hiding_canonical_fact(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "POLL_SECONDS", 0.01)

    async def exercise():
        store = RuntimeStore(tmp_path / "memory.db")
        store.migrate()
        service = MemoryService(store, DownChat(), ("a",), idle_seconds=0)
        await service.put("a", "warm", "semantic", "drink", "用户喜欢茶")
        await service.start()
        try:
            await wait_for(lambda: progress(store) is not None)
            now = time.time_ns() // 1_000_000
            store.create_conversation_turn({
                "turn_id": "turn1", "robot_id": "a", "room_id": "roomv2", "chat_id": "chat",
                "persona_id": "warm", "persona_revision": 1, "session_epoch": 1,
                "generation": 1, "user_text": "我在杭州上班", "turn_source": "user",
                "status": "running", "created_ms": now, "updated_ms": now,
            })
            store.finish_conversation_turn("a", "turn1", status="completed", updated_ms=now + 1)
            await wait_for(lambda: "a" in service.errors)
            assert service.insight("a")["error"] == "RuntimeError"
            assert [row["text"] for row in await service.list("a", "warm")] == ["用户喜欢茶"]
            assert len((await service.recall("a", "warm", "喜欢茶"))["evidence"]) == 1
        finally:
            await service.close()

    asyncio.run(exercise())


def progress(store):
    with closing(sqlite3.connect(store.path)) as db:
        row = db.execute("SELECT turn_row_id FROM memory_progress WHERE robot_id='a'").fetchone()
    return None if row is None else row[0]


async def wait_for(check, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not check():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.01)
