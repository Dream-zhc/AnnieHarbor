"""Meta-extractions never become memory; repeat utterances don't multiply facts."""

import asyncio
import sqlite3
import time

import pytest
from annie_adapters import RuntimeStore
from annie_brain.memory import MemoryService, is_meta_memory, prepare_extracted_fact, same_extracted_fact


def test_extraction_is_a_fact_not_the_models_analysis():
    screenshot = {
        "kind": "semantic", "key": "no-extractable-content",
        "text": "未提取到用户显式持久偏好或有意义经历。用户询问下周是否有朋友联系。",
    }
    assert prepare_extracted_fact(screenshot, "下周有朋友找我吗") is None
    assert prepare_extracted_fact({**screenshot, "key": "other"}, "下周有朋友找我吗") is None
    assert prepare_extracted_fact({"kind": "chat", "key": "no_extraction",
                                  "text": "placeholder"}, "你好呀") is None
    assert prepare_extracted_fact({"kind": "persona", "key": "capability",
                                  "text": "用户曾询问当前AI助手是否具备自行安装软件的能力。"},
                                 "你能自己安装软件吗") is None
    assert prepare_extracted_fact({"kind": "persona", "key": "nod_test",
                                  "text": "用户曾要求当前AI助手点下头，测试其物理动作能力。"},
                                 "点下头") is None
    assert is_meta_memory("no_extraction", "placeholder")
    assert is_meta_memory("nod_test", "用户曾要求当前AI助手点下头，测试其物理动作能力。")
    assert prepare_extracted_fact({"kind": "semantic", "key": "tea",
                                  "text": "用户喜欢茶"}, "我喜欢茶") == (
                                      "semantic", "tea", "用户喜欢茶")
    assert prepare_extracted_fact({"kind": "persona", "key": "persona_feedback_01",
                                  "text": "用户对AI有永久好感"}, "我喜欢你") == (
                                      "persona", "user_expressed_liking",
                                      "用户曾向当前 Persona 表达喜欢。")
    assert same_extracted_fact([
        {"kind": "persona", "fact_key": "persona_feedback_02",
         "text": "用户曾对当前AI助手表示喜欢（我喜欢你），对该机器人 persona 有正面好感。"},
    ], "persona", "user_expressed_liking", "用户曾向当前 Persona 表达喜欢。")
    assert not same_extracted_fact([
        {"kind": "persona", "fact_key": "x", "text": "用户喜欢茶"},
    ], "semantic", "tea", "用户喜欢茶")


def test_legacy_empty_result_stays_visible_to_user_but_never_recalled(tmp_path):
    async def run():
        store = RuntimeStore(tmp_path / "memory.db")
        store.migrate()
        old = "未提取到用户显式持久偏好或有意义经历。"
        with sqlite3.connect(store.path) as db:
            db.execute("""INSERT INTO long_term_memories
                (id,robot_id,persona_id,kind,fact_key,text,source_created_ms,
                 version,status,updated_ms,index_state)
                VALUES('legacy','a','','semantic','no-extractable-content',?,1,1,
                       'active',1,'pending')""", (old,))
        cloud = BadCloud()
        memory = MemoryService(store, cloud, ("a",))
        assert len(await memory.list("a", "customer1")) == 1  # User can forget it.
        assert old not in await memory.bootstrap("a", "customer1", "chat1")
        assert (await memory.recall("a", "customer1", "未提取到"))["evidence"] == []
        with pytest.raises(ValueError, match="not a memory fact"):
            await memory.put("a", "customer1", "semantic", "no-extractable-content", old)
        await memory.close()

    asyncio.run(run())


class BadCloud:
    def __init__(self):
        self.embeds = 0

    async def close(self):
        pass

    async def embed(self, _text):
        self.embeds += 1
        return [1.0, 0.0]

    async def extract(self, user_text, _existing):
        if "朋友" in user_text:
            return [{"kind": "semantic", "key": "no-extractable-content",
                     "text": "未提取到用户显式持久偏好或有意义经历。"}]
        return [
            {"kind": "persona", "key": "persona_feedback_01",
             "text": "用户曾对AI表达喜欢（我喜欢你），永远有好感。"},
            {"kind": "persona", "key": "persona-liked",
             "text": "用户曾向当前AI助手表示喜欢（我喜欢你）。"},
        ]


def test_worker_skips_empty_facts_repeat_aliases_and_forgotten_memory(tmp_path):
    async def run():
        store = RuntimeStore(tmp_path / "memory.db")
        store.migrate()
        cloud = BadCloud()
        memory = MemoryService(store, cloud, ("a",))

        def complete_turn(number, text):
            now = time.time_ns() // 1_000_000
            turn_id = f"turn{number}"
            store.create_conversation_turn({
                "turn_id": turn_id, "robot_id": "a", "room_id": "roomv2",
                "chat_id": "chat1", "persona_id": "customer1", "persona_revision": 1,
                "session_epoch": 1, "generation": 1,
                "user_text": text, "status": "running",
                "created_ms": now, "updated_ms": now,
            })
            store.finish_conversation_turn("a", turn_id, status="completed",
                                           updated_ms=now + 1)

        async def wait_progress(number):
            for _ in range(50):
                with sqlite3.connect(store.path) as db:
                    cursor = db.execute(
                        "SELECT turn_row_id FROM memory_progress WHERE robot_id='a'"
                    ).fetchone()
                if cursor is not None and cursor[0] >= number:
                    return
                await asyncio.sleep(.1)
            raise AssertionError(f"worker did not process turn {number}")

        await memory.start()
        try:
            await asyncio.sleep(.15)  # First boot cursor, before the first turn.
            complete_turn(1, "下周有朋友来吗")
            complete_turn(2, "我喜欢你")
            complete_turn(3, "我喜欢你")
            await wait_progress(3)
            rows = await memory.list("a", "customer1")
            assert len(rows) == 1
            assert rows[0]["fact_key"] == "user_expressed_liking"
            assert rows[0]["text"] == "用户曾向当前 Persona 表达喜欢。"
            assert cloud.embeds == 1
            assert await memory.list("a", "other-persona") == []
            await memory.change("a", "customer1", rows[0]["id"], 1)
            complete_turn(4, "我喜欢你")
            await wait_progress(4)
            assert await memory.list("a", "customer1") == []
            assert cloud.embeds == 1
        finally:
            await memory.close()

    asyncio.run(run())
