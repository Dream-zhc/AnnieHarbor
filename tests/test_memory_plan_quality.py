"""Context-grounded memory extraction and cross-Chat ongoing plans."""

import asyncio
import sqlite3
import time

from annie_adapters import RuntimeStore
from annie_brain.context import historical_user_evidence, select_restorable_context
from annie_brain.memory import (
    CloudMemoryModel, MemoryService, prepare_extracted_fact, same_extracted_fact,
)


def test_snapshot_bad_facts_do_not_pass_and_car_plan_has_stable_identity():
    cases = [
        ("chat", "location", "用户提及地点为北京。", "北京的"),
        ("chat", "reminder", "用户要求1分钟后提醒去喂猫。", "1分钟后提醒我去喂猫"),
        ("semantic", "no_band", "用户不喜欢带。", "不喜欢带。"),
        ("persona", "fake_love", "用户对AI有正面好感。", "请你眨两下眼"),
        ("episodes", "daily_tired", "用户今天工作挺累的。", "今天工作挺累的"),
    ]
    for kind, key, value, original in cases:
        assert prepare_extracted_fact(
            {"kind": kind, "key": key, "text": value}, original,
        ) is None
    result = prepare_extracted_fact({
        "kind": "chat", "key": "chat_04", "text": "用户最近准备去看车。",
    }, "我最近去准备去看车")
    assert result == (
        "episodes", "ongoing_plan_car_viewing",
        "用户最近准备去看车，进展尚未确认。",
    )
    assert same_extracted_fact([
        {"kind": "episodes", "fact_key": result[1], "text": result[2]},
    ], *result)
    assert prepare_extracted_fact({
        "kind": "episodes", "key": result[1], "text": result[2],
    }, "我已经买车了") is None
    assert prepare_extracted_fact({
        "kind": "semantic", "key": "city", "text": "用户居住在北京。",
    }, "我住在北京") == ("semantic", "city", "用户居住在北京。")


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


def test_context_is_a_disambiguation_hint_not_a_second_fact_source(tmp_path):
    async def run():
        store = RuntimeStore(tmp_path / "context.db")
        store.migrate()
        now = time.time_ns() // 1_000_000
        store.create_conversation_turn({
            "turn_id": "prev", "robot_id": "annie-a", "room_id": "roomv2",
            "chat_id": "chat", "persona_id": "annie", "persona_revision": 1,
            "session_epoch": 1, "generation": 1, "user_text": "今天天气怎么样",
            "turn_source": "user", "status": "completed",
            "created_ms": now, "updated_ms": now,
        })
        store.finish_conversation_turn(
            "annie-a", "prev", status="completed",
            generated_text="你在哪座城市？", updated_ms=now,
        )
        store.create_conversation_turn({
            "turn_id": "current", "robot_id": "annie-a", "room_id": "roomv2",
            "chat_id": "chat", "persona_id": "annie", "persona_revision": 1,
            "session_epoch": 1, "generation": 1, "user_text": "北京的",
            "turn_source": "user", "status": "completed",
            "created_ms": now + 1, "updated_ms": now + 1,
        })
        service = MemoryService(store, EmptyExtractor(), ("annie-a",))
        with sqlite3.connect(store.path) as db:
            db.row_factory = sqlite3.Row
            row = dict(db.execute(
                "SELECT * FROM conversation_turns WHERE turn_id='current'",
            ).fetchone())
        history = service._nearby_dialogue(row)
        assert history == [{"user": "今天天气怎么样", "assistant": "你在哪座城市？"}]

        class Capture:
            async def ainvoke(self, value):
                assert "current_user" in value["messages"][0]["content"]
                assert "北京的" in value["messages"][0]["content"]
                assert "你在哪座城市" in value["messages"][0]["content"]
                return []

        cloud = object.__new__(CloudMemoryModel)
        cloud.manager = Capture()
        assert await cloud.extract("北京的", [], context=history) == []
        await service.close()

    asyncio.run(run())


class EmptyExtractor:
    async def extract(self, _text, _existing):
        return []

    async def embed(self, _text):
        return [1., 0.]

    async def close(self):
        pass


def test_plan_is_cross_chat_but_expires_and_a_completion_stops_it(tmp_path):
    async def run():
        store = RuntimeStore(tmp_path / "plans.db")
        store.migrate()
        service = MemoryService(store, EmptyExtractor(), ("annie-a",))
        await service.start()
        try:
            await asyncio.sleep(.1)

            def turn(turn_id, text, chat):
                now = time.time_ns() // 1_000_000
                store.create_conversation_turn({
                    "turn_id": turn_id, "robot_id": "annie-a",
                    "room_id": "roomv2", "chat_id": chat, "persona_id": "annie",
                    "persona_revision": 1, "session_epoch": 1,
                    "generation": 1, "turn_source": "user", "input_mode": "text",
                    "user_text": text, "status": "completed",
                    "created_ms": now, "updated_ms": now,
                })

            async def wait_for_row(turn_id):
                for _ in range(55):
                    with sqlite3.connect(store.path) as db:
                        mark = db.execute(
                            "SELECT turn_row_id FROM memory_progress WHERE robot_id='annie-a'"
                        ).fetchone()[0]
                        row = db.execute(
                            "SELECT id FROM conversation_turns WHERE turn_id=?", (turn_id,)
                        ).fetchone()[0]
                    if mark >= row:
                        return
                    await asyncio.sleep(.1)
                raise AssertionError("memory worker didn't advance")

            turn("t1", "我最近去准备去看车", "chat-a")
            await wait_for_row("t1")
            rows = await service.list("annie-a", "annie")
            assert len(rows) == 1 and rows[0]["fact_key"] == "ongoing_plan_car_viewing"
            assert "看车" in await service.bootstrap("annie-a", "annie", "chat-b")
            assert "看车" not in await service.bootstrap("annie-a", "other", "chat-b")
            turn("t2", "我已经买车了", "chat-b")
            await wait_for_row("t2")
            assert "跨Chat近期计划" not in await service.bootstrap("annie-a", "annie", "chat-a")
            rows = await service.list("annie-a", "annie")
            assert "已结束" in rows[0]["text"]
        finally:
            await service.close()

    asyncio.run(run())
