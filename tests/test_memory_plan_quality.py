"""Context-grounded consolidation and cross-Chat ongoing plans."""

import asyncio
import json
import sqlite3
import time
from contextlib import closing

from annie_adapters import RuntimeStore
from annie_brain import memory as memory_module
from annie_brain.context import historical_user_evidence, select_restorable_context
from annie_brain.memory import MemoryService, ground_fondness, prepare_consolidated_fact

PLAN = "用户最近准备去看车"


def test_snapshot_bad_facts_do_not_pass():
    for kind, text in (("chat", "用户提及地点为北京。"), ("chat", "用户要求1分钟后提醒去喂猫。"),
                       ("semantic", "用户不喜欢带。"), ("episodes", "用户今天工作挺累的。")):
        assert prepare_consolidated_fact(kind, text) is None, text
    fake_love = {"op": "add", "kind": "persona", "text": "用户对AI有正面好感。", "importance": 0.5,
                 "target": None}
    assert ground_fondness([fake_love], "请你眨两下眼")[0]["ungrounded"]
    assert prepare_consolidated_fact("semantic", "用户居住在北京。") == "用户居住在北京。"


def test_unknown_legacy_turn_is_safe_but_old_car_topic_is_available():
    old = {"turn_id": "old", "user_text": "我最近去准备去看车",
           "assistant_generated_text": "你打算看新车还是二手车？",
           "input_mode": "unknown", "playback_evidence_status": "unknown",
           "status": "completed", "turn_source": "user",
           "created_ms": 1}
    packet = select_restorable_context([old], speech_enabled=True)
    assert packet["turns"][0]["assistant_text"] == ""
    evidence = historical_user_evidence(packet)
    assert "看车" in evidence and "近期话题线索" in evidence
    assert "不主动执行旧任务" in evidence


class Chat:
    """Records each request; `plan(payload, alias_of)` returns the ops."""

    def __init__(self, plan=None):
        self.plan = plan or (lambda payload, alias_of: [])
        self.requests = []

    async def close(self):
        pass

    async def complete(self, _system, user, *, max_tokens):
        payload = json.loads(user)
        self.requests.append(payload)
        alias_of = {item["内容"]: item["编号"] for item in payload["已经记得的事"]}
        return json.dumps({"ops": self.plan(payload, alias_of)}, ensure_ascii=False)


def turn(store, turn_id, text, *, reply=None):
    now = time.time_ns() // 1_000_000
    store.create_conversation_turn({
        "turn_id": turn_id, "robot_id": "annie-a", "room_id": "roomv2", "chat_id": "chat",
        "persona_id": "annie", "persona_revision": 1, "session_epoch": 1, "generation": 1,
        "turn_source": "user", "input_mode": "text", "user_text": text, "status": "running",
        "created_ms": now, "updated_ms": now,
    })
    store.finish_conversation_turn("annie-a", turn_id, status="completed", generated_text=reply,
                                   updated_ms=now + 1)


def progress(store):
    with closing(sqlite3.connect(store.path)) as db:
        row = db.execute("SELECT turn_row_id FROM memory_progress WHERE robot_id='annie-a'").fetchone()
    return None if row is None else row[0]


async def wait_for(check, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not check():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.01)


def test_consolidation_reads_her_question_with_the_short_answer(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_module, "POLL_SECONDS", 0.01)

    async def run():
        store = RuntimeStore(tmp_path / "context.db")
        store.migrate()
        chat = Chat()
        service = MemoryService(store, chat, ("annie-a",), idle_seconds=60, max_turns=2)
        await service.start()
        try:
            await wait_for(lambda: progress(store) is not None)
            turn(store, "prev", "今天天气怎么样", reply="你在哪座城市？")
            turn(store, "current", "北京的")
            await wait_for(lambda: progress(store) == 2)
        finally:
            await service.close()
        assert len(chat.requests) == 1
        dialogue = json.dumps(chat.requests[0]["对话"], ensure_ascii=False)
        assert "今天天气怎么样" in dialogue and "你在哪座城市" in dialogue and "北京的" in dialogue

    asyncio.run(run())


def test_plan_is_cross_chat_scoped_to_the_persona_and_ends_when_done(tmp_path):
    def plan(payload, alias_of):
        if "买" in payload["对话"][-1]["用户"]:
            return [{"op": "outdated", "id": alias_of[PLAN], "kind": "episodes",
                     "text": "用户已经买了车", "importance": 0.6}]
        return [{"op": "add", "kind": "episodes", "text": PLAN, "importance": 0.6}]

    async def run():
        store = RuntimeStore(tmp_path / "plans.db")
        store.migrate()
        service = MemoryService(store, Chat(plan), ("annie-a",))

        async def consolidate(said, chat):
            now = memory_module.now_ms()
            await service._consolidate("annie-a", "annie", chat, [(now, said, "嗯")],
                                       {"turn_id": f"t-{chat}", "created_ms": now})

        await consolidate("我最近准备去看车", "chat-a")
        assert "看车" in await service.bootstrap("annie-a", "annie", "chat-b")
        assert "看车" not in await service.bootstrap("annie-a", "other", "chat-b")
        await consolidate("之前说的看车，我已经买了", "chat-b")
        opening = await service.bootstrap("annie-a", "annie", "chat-a")
        assert "准备去看车" not in opening and "已经买了车" in opening
        old = next(row for row in await service.list("annie-a", "annie", True) if row["text"] == PLAN)
        assert old["status"] == "outdated" and old["superseded_by"] is not None
        await service.close()

    asyncio.run(run())
