"""Meta-extractions never become memory; repeat utterances don't multiply facts."""

import asyncio
import json
import sqlite3

import pytest
from annie_adapters import RuntimeStore
from annie_brain import memory as memory_module
from annie_brain.memory import (
    FONDNESS, MemoryService, ground_fondness, is_meta_memory, prepare_consolidated_fact,
)

EMPTY = "未提取到用户显式持久偏好或有意义经历。"


def add(text, kind="persona"):
    return {"op": "add", "kind": kind, "text": text, "importance": 0.5, "target": None}


def test_extraction_is_a_fact_not_the_models_analysis():
    assert prepare_consolidated_fact("semantic", EMPTY + "用户询问下周是否有朋友联系。") is None
    assert prepare_consolidated_fact("chat", "placeholder") is None
    assert prepare_consolidated_fact("persona", "用户曾询问当前AI助手是否具备自行安装软件的能力。") is None
    assert prepare_consolidated_fact("persona", "用户曾要求当前AI助手点下头，测试其物理动作能力。") is None
    assert prepare_consolidated_fact("episodes", "用户今天工作挺累的。") is None
    assert is_meta_memory("no_extraction", "placeholder")
    assert is_meta_memory("nod_test", "用户曾要求当前AI助手点下头，测试其物理动作能力。")
    assert prepare_consolidated_fact("semantic", "用户喜欢茶") == "用户喜欢茶"
    assert prepare_consolidated_fact("episodes", "用户今天生日，很开心") == "用户今天生日，很开心"


def test_liking_her_is_the_users_words_not_the_models_verdict():
    assert ground_fondness([add("用户对AI有永久好感"), add("用户喜欢茶", "semantic")], "我喜欢你") == [
        add(FONDNESS), add("用户喜欢茶", "semantic")]
    assert ground_fondness([add("用户喜欢上小安妮了", "semantic")], "小安妮，我好喜欢你啊") == [add(FONDNESS)]
    for text, words in (("用户对AI有正面好感。", "请你眨两下眼"), ("用户对小安妮有好感", "我不再喜欢你了"),
                        ("用户说不再喜欢小安妮了", "我不再喜欢你了"), ("用户对小安妮有好感", "你好可爱你知道吗")):
        assert ground_fondness([add(text)], words)[0]["ungrounded"], (text, words)
    assert ground_fondness([add("用户喜欢小安妮的声音")], "") == [add("用户喜欢小安妮的声音")]


class Chat:
    """Answers every conversation with an empty result and two wordings of liking her."""

    async def close(self):
        pass

    async def complete(self, _system, _user, *, max_tokens):
        return json.dumps({"ops": [
            {"op": "add", "kind": "semantic", "text": EMPTY},
            {"op": "add", "kind": "persona", "text": "用户曾对AI表达喜欢（我喜欢你），永远有好感。"},
            {"op": "add", "kind": "persona", "text": "用户曾向当前AI助手表示喜欢（我喜欢你）。"},
        ]}, ensure_ascii=False)


def test_legacy_empty_result_stays_visible_to_user_but_never_recalled(tmp_path):
    async def run():
        store = RuntimeStore(tmp_path / "memory.db")
        store.migrate()
        with sqlite3.connect(store.path) as db:
            db.execute("""INSERT INTO long_term_memories
                (id,robot_id,persona_id,kind,fact_key,text,source_created_ms,
                 version,status,updated_ms,index_state)
                VALUES('legacy','a','','semantic','no-extractable-content',?,1,1,
                       'active',1,'pending')""", (EMPTY,))
        memory = MemoryService(store, Chat(), ("a",))
        assert len(await memory.list("a", "customer1")) == 1  # User can forget it.
        assert EMPTY not in await memory.bootstrap("a", "customer1", "chat1")
        assert (await memory.recall("a", "customer1", "未提取到"))["evidence"] == []
        with pytest.raises(ValueError, match="not a fact about the user"):
            await memory.put("a", "customer1", "semantic", "no-extractable-content", EMPTY)
        await memory.close()

    asyncio.run(run())


def test_repeat_wordings_make_one_fact_and_a_forgotten_one_stays_forgotten(tmp_path):
    async def run():
        store = RuntimeStore(tmp_path / "memory.db")
        store.migrate()
        memory = MemoryService(store, Chat(), ("a",))

        async def consolidate(said):
            now = memory_module.now_ms()
            await memory._consolidate("a", "customer1", "chat1", [(now, said, "嗯")],
                                      {"turn_id": f"turn{now}", "created_ms": now})
            return [item["result"] for item in memory.consolidations["a"][0]["results"]]

        assert await consolidate("下周有朋友来吗") == ["rejected"] * 3
        assert await memory.list("a", "customer1") == []
        assert await consolidate("我喜欢你") == ["rejected", "added", "known"]
        assert await consolidate("我喜欢你") == ["rejected", "known", "known"]
        rows = await memory.list("a", "customer1")
        assert [(row["kind"], row["text"]) for row in rows] == [("persona", FONDNESS)]
        assert await memory.list("a", "other-persona") == []
        await memory.change("a", "customer1", rows[0]["id"], rows[0]["version"])
        assert await consolidate("我喜欢你") == ["rejected", "blocked", "blocked"]
        assert await memory.list("a", "customer1") == []
        await memory.close()

    asyncio.run(run())
