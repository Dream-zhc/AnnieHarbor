"""Phase 6: real SQLite persistence, scope, correction and deletion invariants."""

import asyncio

import pytest
from annie_adapters import RuntimeStore
from annie_brain.memory import MemoryService


class FakeCloud:
    def __init__(self):
        self.delay = 0
        self.closed = False

    async def close(self):
        self.closed = True

    async def embed(self, value):
        if self.delay:
            await asyncio.sleep(self.delay)
        return [1.0, 0.0] if "茶" in value else [0.0, 1.0]

    async def extract(self, user, existing):
        if "咖啡" in user:
            return [{"key": "drink_preference", "kind": "semantic", "text": "用户喜欢咖啡"}]
        if "茶" in user:
            return [{"key": "drink_preference", "kind": "semantic", "text": "用户喜欢茶"}]
        return []


def test_crud_corrections_tombstone_and_restart(tmp_path):
    async def exercise():
        store = RuntimeStore(tmp_path / "memory.db")
        store.migrate()
        cloud = FakeCloud()
        service = MemoryService(store, cloud, ("annie-a", "annie-b"))
        result = await service.put(
            "annie-a", "warm", "semantic", "drink_preference", "用户喜欢茶",
            source_turn_id="t1", source_created_ms=1000,
        )
        ident, version = result
        assert version == 1
        assert len(await service.list("annie-a", "warm")) == 1
        assert await service.list("annie-b", "warm") == []
        assert len((await service.recall("annie-a", "warm", "茶"))["evidence"]) == 1
        assert await service.list("annie-a", "cold")
        restarted = MemoryService(store, cloud, ("annie-a", "annie-b"))
        assert await service.list("annie-a", "warm") == await restarted.list("annie-a", "warm")
        assert await service.put(
            "annie-a", "warm", "semantic", "drink_preference", "用户喜欢咖啡",
            source_turn_id="t2", source_created_ms=2000,
        ) == (ident, 2)
        assert await service.put(
            "annie-a", "warm", "semantic", "drink_preference", "用户喜欢茶",
            source_turn_id="t1", source_created_ms=1000,
        ) is None
        with pytest.raises(ValueError, match="version conflict"):
            await service.change("annie-a", "warm", ident, 1, "旧版本")
        forgotten = await service.change("annie-a", "warm", ident, 2)
        assert forgotten["conversation_history_deleted"] is False
        assert await service.list("annie-a", "warm") == []
        assert (await service.recall("annie-a", "warm", "咖啡"))["evidence"] == []
        assert await service.put(
            "annie-a", "warm", "semantic", "drink_preference", "用户喜欢茶",
            source_turn_id="t1", source_created_ms=1000,
        ) is None
        assert (await service.list("annie-a", "warm", True))[0]["status"] == "deleted"
        await service.close()
        assert cloud.closed

    asyncio.run(exercise())


def test_persona_scope_and_no_arbitrary_recall(tmp_path):
    async def exercise():
        store = RuntimeStore(tmp_path / "memory.db")
        store.migrate()
        service = MemoryService(store, FakeCloud(), ("a", "b"))
        await service.put("a", "warm", "persona", "style", "喜欢讲笑话")
        await service.put("a", "warm", "episodes", "trip", "用户去过北京")
        assert len(await service.list("a", "warm")) == 2
        assert await service.list("a", "cold") == []
        assert await service.list("b", "warm") == []
        assert (await service.recall("a", "cold", "北京"))["evidence"] == []

    asyncio.run(exercise())
