"""Cross-Chat user-utterance recall must reach the actual Realtime session."""

import asyncio
import time

from annie_adapters import RuntimeStore
from annie_brain.memory import MemoryService, prepare_extracted_fact
from annie_core import RobotConfig
from annie_runtime import HarborRuntime

from tests.test_phase4_realtime import FakeSocket


class FakeCloud:
    async def close(self):
        pass

    async def embed(self, _text):
        return [1.0, 0.0]

    async def extract(self, _text, _existing):
        return []


def add_turn(store, ident, text, persona="annie-lively", chat="测试", age_days=0):
    now = time.time_ns() // 1_000_000 - age_days * 86_400_000
    store.create_conversation_turn({
        "turn_id": ident, "robot_id": "a", "room_id": "roomv2",
        "chat_id": chat, "persona_id": persona, "persona_revision": 1,
        "session_epoch": 1, "generation": 1, "user_text": text,
        "status": "running", "created_ms": now, "updated_ms": now,
    })
    store.finish_conversation_turn("a", ident, status="completed", updated_ms=now + 1)


def test_realtime_recovers_visitor_utterance_from_other_chat_without_cloud(tmp_path):
    async def run():
        database = tmp_path / "runtime.db"
        store = RuntimeStore(database)
        store.migrate()
        add_turn(store, "friend", "下周我朋友来找我")
        add_turn(store, "hello", "你好呀")
        add_turn(store, "question", "下周有人找我吗", chat="测试 a")
        # Legacy model claims must not contaminate answers when sourced to hello.
        memory = MemoryService(store, FakeCloud(), ("a",))
        await memory.put("a", "annie-lively", "episodes", "fake_visit",
                         "用户提到下周有朋友来找他。", source_turn_id="hello")
        legacy = await memory.list("a", "annie-lively")
        assert legacy[0]["source_valid"] is False
        assert (await memory.recall("a", "annie-lively", "下周朋友"))["evidence"] == []
        assert prepare_extracted_fact({
            "kind": "episodes", "key": "fabricated",
            "text": "用户提到下周有朋友来找他。",
        }, "你好呀") is None

        FakeSocket.instances.clear()
        rt = HarborRuntime(
            [RobotConfig("a", room_id="roomv2", fake_delay_ms=0)],
            database, realtime_key="fake", realtime_socket_factory=FakeSocket,
            memory_model=FakeCloud(),
        )
        await rt.start()
        try:
            await rt.switch_session("a", chat_id="测试 a", persona_id="annie-lively")
            for _ in range(150):
                if rt.realtime["a"].ready and rt.robots["a"].chat_id == "测试 a":
                    break
                await asyncio.sleep(.01)
            assert rt.realtime["a"].ready
            updates = [e for e in rt.realtime["a"].socket.outbound
                       if e["type"] == "session.update"]
            assert len(updates) == 1
            instructions = updates[0]["session"]["instructions"]
            assert "下周我朋友来找我" in instructions
            assert "UTC" in instructions
            assert "用户提到下周有朋友来找他" not in instructions
            assert "相对日期相对于原话时间" in instructions
            assert "recall_memory" in [t["function"]["name"]
                                       for t in updates[0]["session"]["tools"]]
            assert await rt.memory.bootstrap("a", "other-persona", "测试 a") == ""
        finally:
            await rt.close()

    asyncio.run(run())


def test_historical_visit_is_bounded_and_not_invented_from_questions(tmp_path):
    async def run():
        store = RuntimeStore(tmp_path / "runtime.db")
        store.migrate()
        add_turn(store, "question", "下周有人找我吗")
        add_turn(store, "hypothetical", "如果下周朋友来找我怎么办")
        add_turn(store, "cancelled", "下周朋友不来找我了")
        add_turn(store, "old", "下周我朋友来找我", age_days=22)
        memory = MemoryService(store, FakeCloud(), ("a",))
        assert await memory.bootstrap("a", "annie-lively", "测试 a") == ""
        add_turn(store, "current", "下周我朋友来找我", chat="测试 a")
        assert "下周我朋友来找我" in await memory.bootstrap("a", "annie-lively", "测试 a")
        add_turn(store, "other-role", "明天我朋友来找我", persona="other")
        assert "明天我朋友来找我" not in await memory.bootstrap("a", "annie-lively", "测试 a")
        await memory.close()

    asyncio.run(run())


def test_newer_correction_or_forget_defeats_raw_cross_chat_evidence(tmp_path):
    async def run():
        store = RuntimeStore(tmp_path / "runtime.db")
        store.migrate()
        add_turn(store, "friend", "下周我朋友来找我")
        memory = MemoryService(store, FakeCloud(), ("a",))
        assert "下周我朋友来找我" in await memory.bootstrap("a", "annie-lively", "new")
        add_turn(store, "cancel", "下周朋友不来找我了", chat="new")
        assert await memory.bootstrap("a", "annie-lively", "new") == ""
        add_turn(store, "again", "下周我朋友来找我", chat="new")
        assert "下周我朋友来找我" in await memory.bootstrap("a", "annie-lively", "new")
        add_turn(store, "forget", "忘记下周朋友来找我的事", chat="new")
        assert await memory.bootstrap("a", "annie-lively", "new") == ""
        await memory.close()

    asyncio.run(run())
