"""Completed Ledger turns are consolidated only in background, and survive restart."""

import asyncio
import json
import sqlite3
import time
from contextlib import closing

from annie_adapters import RuntimeStore
from annie_brain import memory as memory_module
from annie_brain.memory import MemoryService


class FakeChat:
    """Learns 用户喜欢茶 whenever the user mentioned tea."""

    def __init__(self, delay=0.0):
        self.delay = delay
        self.requests = []

    async def close(self):
        pass

    async def complete(self, _system, user, *, max_tokens):
        payload = json.loads(user)
        self.requests.append(payload)
        await asyncio.sleep(self.delay)
        said = "".join(turn["用户"] for turn in payload["对话"])
        ops = ([{"op": "add", "kind": "semantic", "text": "用户喜欢茶", "importance": 0.6}]
               if "茶" in said else [])
        return json.dumps({"ops": ops}, ensure_ascii=False)


def finish_turn(store, turn_id, text, **extra):
    now = time.time_ns() // 1_000_000
    store.create_conversation_turn({
        "turn_id": turn_id, "robot_id": "a", "room_id": "roomv2", "chat_id": "chat-old",
        "persona_id": "warm", "persona_revision": 1, "session_epoch": 1, "generation": 1,
        "user_text": text, "turn_source": "user", "status": "running",
        "created_ms": now, "updated_ms": now, **extra,
    })
    store.finish_conversation_turn("a", turn_id, status="completed", updated_ms=now + 1)


def progress(store):
    with closing(sqlite3.connect(store.path)) as db:
        row = db.execute("SELECT turn_row_id FROM memory_progress WHERE robot_id='a'").fetchone()
    return None if row is None else row[0]


async def wait_for(check, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not check():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.01)


def test_completed_turn_background_consolidation_and_restart_cursor(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_module, "POLL_SECONDS", 0.01)

    async def run():
        store = RuntimeStore(tmp_path / "memory.db")
        store.migrate()
        memory = MemoryService(store, FakeChat(), ("a",), idle_seconds=0)
        await memory.start()
        try:
            # Establish the first-boot cursor before a new turn enters the ledger.
            await wait_for(lambda: progress(store) is not None)
            finish_turn(store, "turn1", "我喜欢茶")
            await wait_for(lambda: progress(store) == 1)
            items = await memory.list("a", "warm")
            assert [(item["text"], item["source_turn_id"]) for item in items] == [
                ("用户喜欢茶", "turn1")]
        finally:
            await memory.close()
        chat = FakeChat()
        restarted = MemoryService(store, chat, ("a",), idle_seconds=0)
        await restarted.start()
        try:
            await asyncio.sleep(0.1)
            assert chat.requests == []  # the cursor survived: no second model call
            assert len(await restarted.list("a", "other-chat-persona")) == 1
            assert store.get_conversation_turn("a", "turn1")["user_text"] == "我喜欢茶"
        finally:
            await restarted.close()

    asyncio.run(run())


def test_slow_consolidation_cannot_revive_forgotten_fact(tmp_path):
    async def run():
        store = RuntimeStore(tmp_path / "memory.db")
        store.migrate()
        chat = FakeChat(delay=0.2)
        memory = MemoryService(store, chat, ("a",))
        ident, version = await memory.put("a", "warm", "semantic", "drink", "用户喜欢茶")
        now = memory_module.now_ms()
        task = asyncio.create_task(memory._consolidate(
            "a", "warm", "chat", [(now, "我喜欢茶", "好")], {"turn_id": "t1", "created_ms": now}))
        await wait_for(lambda: chat.requests)
        await memory.change("a", "warm", ident, version)
        await task
        assert await memory.list("a", "warm") == []
        assert (await memory.list("a", "warm", True))[0]["index_state"] == "deleted"
        assert [item["result"] for item in memory.consolidations["a"][0]["results"]] == ["blocked"]

    asyncio.run(run())


def test_external_event_does_not_block_next_real_user_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_module, "POLL_SECONDS", 0.01)

    async def run():
        store = RuntimeStore(tmp_path / "memory.db")
        store.migrate()
        chat = FakeChat()
        memory = MemoryService(store, chat, ("a",), idle_seconds=0)
        await memory.start()
        try:
            await wait_for(lambda: progress(store) is not None)
            finish_turn(store, "internal", "", turn_source="external",
                        external_prompt="【真实到期的单次提醒】喝茶")
            finish_turn(store, "real", "我喜欢茶")
            await wait_for(lambda: progress(store) == 2)
            items = await memory.list("a", "warm")
            assert len(items) == 1 and items[0]["source_turn_id"] == "real"
            assert "提醒" not in json.dumps(chat.requests, ensure_ascii=False)
        finally:
            await memory.close()

    asyncio.run(run())
