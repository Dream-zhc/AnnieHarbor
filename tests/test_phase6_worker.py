"""Completed Ledger facts are extracted only in background and survive restart."""

import asyncio
import time

from annie_adapters import RuntimeStore
from annie_brain.memory import MemoryService


class FakeCloud:
    async def close(self):
        pass

    async def embed(self, text):
        return [1.0, 0.0] if "茶" in text else [0.0, 1.0]

    async def extract(self, text, existing):
        return [{"key": "drink", "kind": "semantic", "text": "用户喜欢茶"}] if "茶" in text else []


def test_completed_turn_background_extraction_and_restart_cursor(tmp_path):
    async def run():
        store = RuntimeStore(tmp_path / "memory.db")
        store.migrate()
        memory = MemoryService(store, FakeCloud(), ("a",))
        await memory.start()
        try:
            # Establish the first-boot cursor before a new turn enters the ledger.
            await asyncio.sleep(0.15)
            now = time.time_ns() // 1_000_000
            store.create_conversation_turn({
                "turn_id": "turn1", "robot_id": "a", "room_id": "roomv2",
                "chat_id": "chat-old", "persona_id": "warm", "persona_revision": 1,
                "session_epoch": 1, "generation": 1,
                "user_text": "我喜欢茶", "status": "running",
                "created_ms": now, "updated_ms": now,
            })
            store.finish_conversation_turn("a", "turn1", status="completed", updated_ms=now + 1)
            for _ in range(30):
                if await memory.list("a", "warm"):
                    break
                await asyncio.sleep(0.1)
            items = await memory.list("a", "warm")
            assert len(items) == 1
            assert items[0]["source_turn_id"] == "turn1"
        finally:
            await memory.close()
        restarted = MemoryService(store, FakeCloud(), ("a",))
        await restarted.start()
        try:
            assert len(await restarted.list("a", "other-chat-persona")) == 1
            assert store.get_conversation_turn("a", "turn1")["user_text"] == "我喜欢茶"
        finally:
            await restarted.close()

    asyncio.run(run())


def test_late_vector_index_cannot_revive_forgotten_fact(tmp_path):
    class SlowCloud(FakeCloud):
        async def embed(self, text):
            await asyncio.sleep(0.2)
            return [1.0, 0.0]

    async def run():
        store = RuntimeStore(tmp_path / "memory.db")
        store.migrate()
        memory = MemoryService(store, SlowCloud(), ("a",))
        task = asyncio.create_task(memory.put("a", "warm", "semantic", "drink", "用户喜欢茶"))
        ident = None
        for _ in range(20):
            rows = await memory.list("a", "warm")
            if rows:
                ident = rows[0]["id"]
                break
            await asyncio.sleep(0.01)
        assert ident is not None
        await memory.change("a", "warm", ident, 1)
        await task
        assert await memory.list("a", "warm") == []
        assert (await memory.list("a", "warm", True))[0]["index_state"] == "deleted"

    asyncio.run(run())


def test_external_event_does_not_block_next_real_user_memory(tmp_path):
    async def run():
        store = RuntimeStore(tmp_path / "memory.db")
        store.migrate()
        memory = MemoryService(store, FakeCloud(), ("a",))
        await memory.start()
        try:
            await asyncio.sleep(.15)
            now = time.time_ns() // 1_000_000
            for turn_id, source, user, prompt in (
                ("internal", "external", "", "【真实到期的单次提醒】喝茶"),
                ("real", "user", "我喜欢茶", None),
            ):
                store.create_conversation_turn({
                    "turn_id": turn_id, "robot_id": "a", "room_id": "roomv2",
                    "chat_id": "chat", "persona_id": "warm", "persona_revision": 1,
                    "session_epoch": 1, "generation": 1, "user_text": user,
                    "turn_source": source, "external_prompt": prompt,
                    "status": "completed", "created_ms": now, "updated_ms": now,
                })
            for _ in range(30):
                if await memory.list("a", "warm"):
                    break
                await asyncio.sleep(.1)
            items = await memory.list("a", "warm")
            assert len(items) == 1 and items[0]["source_turn_id"] == "real"
        finally:
            await memory.close()

    asyncio.run(run())
