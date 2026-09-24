"""Phase 6: forgotten facts and cloud vector failures cannot violate scope."""

import asyncio

from annie_adapters import RuntimeStore
from annie_brain.memory import MemoryService


class Cloud:
    async def close(self):
        pass

    async def embed(self, text):
        if text == "用户喜欢茶":
            raise RuntimeError("provider_down")
        return [1.0, 0.0]

    async def extract(self, text, existing):
        return []


def test_permanent_tombstone_even_after_newer_background_turn(tmp_path):
    async def exercise():
        store = RuntimeStore(tmp_path / "memory.db")
        store.migrate()
        service = MemoryService(store, Cloud(), ("a",))
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


def test_embedding_failure_visible_without_hiding_canonical_fact(tmp_path):
    async def exercise():
        store = RuntimeStore(tmp_path / "memory.db")
        store.migrate()
        service = MemoryService(store, Cloud(), ("a",))
        await service.put("a", "warm", "semantic", "drink", "用户喜欢茶")
        rows = await service.list("a", "warm")
        assert rows[0]["index_state"] == "failed"
        assert service.errors["a"] == "embedding_failed"
        assert len((await service.recall("a", "warm", "喜欢茶"))["evidence"]) == 1

    asyncio.run(exercise())
