"""Memory service: canonical rows, per-turn recall, consolidation after the talk, and the recap."""

import asyncio
import json
import sqlite3

import pytest
from annie_adapters import RuntimeStore
from annie_brain import memory
from annie_brain.memory import (
    BOOTSTRAP_HEADER, MemoryService, conversations, is_meta_memory, prepare_consolidated_fact,
    ready_batch,
)

ROBOT = "annie-a"
DAY_MS = 86_400_000


class FakeChat:
    """Answers like the consolidation and recap model; `plan(payload, alias_of)` returns the ops."""

    def __init__(self, plan=None):
        self.plan = plan or (lambda payload, alias_of: [])
        self.requests = []
        self.closed = False

    async def complete(self, system, user, *, max_tokens):
        payload = json.loads(user)
        self.requests.append(payload)
        if "滑出的对话" in payload:
            return json.dumps({"recap": f"前情{len(payload['滑出的对话'])}轮"}, ensure_ascii=False)
        alias_of = {item["内容"]: item["编号"] for item in payload["已经记得的事"]}
        return json.dumps({"ops": self.plan(payload, alias_of)}, ensure_ascii=False)

    async def close(self):
        self.closed = True


def new_service(tmp_path, plan=None, **options):
    store = RuntimeStore(tmp_path / "memory.db")
    store.migrate()
    return store, MemoryService(store, FakeChat(plan), (ROBOT, "annie-b"), **options)


def say(store, n, text, created, *, reply="好的", source="user", chat="chat-1"):
    store.create_conversation_turn({
        "turn_id": f"t{n}", "robot_id": ROBOT, "room_id": "room", "chat_id": chat,
        "persona_id": "warm", "persona_revision": 1, "session_epoch": 1, "generation": 1,
        "user_text": text, "turn_source": source, "status": "running",
        "created_ms": created, "updated_ms": created,
    })
    store.finish_conversation_turn(ROBOT, f"t{n}", status="completed", generated_text=reply,
                                   updated_ms=created + 500)


def sql(store, query, params=()):
    db = sqlite3.connect(store.path)
    try:
        rows = db.execute(query, params).fetchall()
        db.commit()
        return rows
    finally:
        db.close()


async def wait_for(check, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not check():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.01)


async def consolidate(store, service, said, plan):
    """One finished conversation through the consolidator, as the worker would hand it over."""
    service.chat.plan = plan
    n = 100 + len(service.chat.requests)
    now = memory.now_ms()
    say(store, n, said, now, reply="嗯")
    await service._consolidate(ROBOT, "warm", "chat-1", [(now, said, "嗯")],
                               {"turn_id": f"t{n}", "created_ms": now})
    return [item["result"] for item in service.consolidations[ROBOT][0]["results"]]


def test_only_facts_about_the_user_can_become_memories():
    assert prepare_consolidated_fact("semantic", "  用户  喜欢吃辣 ") == "用户 喜欢吃辣"
    for text in ("你喜欢吃辣", "我是小安妮", "小安妮会跳舞", "用户的名字是张三", "用户称呼小安妮为宝贝",
                 "用户要求小安妮闭嘴", "用户询问天气", "用户不喜欢。", "用户想测试她的唱歌能力"):
        assert prepare_consolidated_fact("semantic", text) is None, text
    assert prepare_consolidated_fact("unknown", "用户喜欢吃辣") is None
    assert is_meta_memory("no_extraction", "placeholder")
    assert not is_meta_memory("auto_1", "用户不喜欢吃香菜")


def test_versioned_crud_scope_and_key_tombstone_survive_restart(tmp_path):
    async def exercise():
        store, service = new_service(tmp_path)
        now = memory.now_ms()
        ident, version = await service.put(ROBOT, "warm", "semantic", "drink", "用户喜欢喝茶",
                                           source_turn_id="t1", source_created_ms=now - 2000)
        assert version == 1
        assert [row["text"] for row in await service.list(ROBOT, "warm")] == ["用户喜欢喝茶"]
        assert await service.list("annie-b", "warm") == []
        assert await service.list(ROBOT, "cold")  # facts about the user are shared by every persona
        restarted = MemoryService(store, FakeChat(), (ROBOT,))
        assert await restarted.list(ROBOT, "warm") == await service.list(ROBOT, "warm")
        assert await service.put(ROBOT, "warm", "semantic", "drink", "用户喜欢喝咖啡",
                                 source_turn_id="t2", source_created_ms=now - 1000) == (ident, 2)
        assert await service.put(ROBOT, "warm", "semantic", "drink", "用户喜欢喝茶",
                                 source_turn_id="t1", source_created_ms=now - 2000) is None
        with pytest.raises(ValueError, match="memory key exists"):
            await service.put(ROBOT, "warm", "semantic", "drink", "用户喜欢喝水")
        with pytest.raises(ValueError, match="version conflict"):
            await service.change(ROBOT, "warm", ident, 1, "旧版本")
        forgotten = await service.change(ROBOT, "warm", ident, 2)
        assert forgotten["conversation_history_deleted"] is False
        assert await service.list(ROBOT, "warm") == []
        assert await service.put(ROBOT, "warm", "semantic", "drink", "用户喜欢喝茶",
                                 source_turn_id="t3", source_created_ms=now + DAY_MS) is None
        deleted, = await service.list(ROBOT, "warm", True)
        assert (deleted["status"], deleted["text"]) == ("deleted", "")
        await service.put(ROBOT, "warm", "persona", "style", "用户喜欢听冷笑话")
        await service.put(ROBOT, "warm", "episodes", "trip", "用户去过北京")
        assert len(await service.list(ROBOT, "warm")) == 2
        assert await service.list(ROBOT, "cold") == []
        with pytest.raises(ValueError, match="not a fact about the user"):
            await service.put(ROBOT, "warm", "semantic", "name", "用户的名字是张三")
        await service.close()
        assert service.chat.closed

    asyncio.run(exercise())


def test_consolidation_learns_retires_and_never_revises_what_people_wrote(tmp_path):
    async def exercise():
        store, service = new_service(tmp_path)
        now = memory.now_ms()
        auto = {"source_turn_id": "t0", "source_created_ms": now - DAY_MS}
        await service.put(ROBOT, "warm", "semantic", "drink", "用户喜欢喝茶", **auto)
        await service.put(ROBOT, "warm", "semantic", "spicy", "用户喜欢吃辣", **auto)
        await service.put(ROBOT, "warm", "semantic", "city", "用户在杭州上班")
        herb, version = await service.put(ROBOT, "warm", "semantic", "herb", "用户喜欢吃香菜", **auto)
        await service.change(ROBOT, "warm", herb, version)

        def plan(payload, alias_of):
            return [
                {"op": "outdated", "id": alias_of["用户喜欢喝茶"], "kind": "semantic",
                 "text": "用户最近改喝咖啡，不喝茶了", "importance": 0.6},
                {"op": "add", "kind": "semantic", "text": "用户喜欢吃辣", "importance": 0.8},
                {"op": "update", "id": alias_of["用户在杭州上班"], "kind": "semantic",
                 "text": "用户在杭州的公司上班", "importance": 0.5},
                {"op": "add", "kind": "semantic", "text": "用户很喜欢吃香菜", "importance": 0.5},
                {"op": "add", "kind": "episodes", "text": "用户下周三要去面试", "importance": 0.8},
                {"op": "add", "kind": "semantic", "text": "用户的名字是张三", "importance": 0.9},
                {"op": "add", "kind": "chat", "text": "这次聊天用户想练习面试", "importance": 0.5},
            ]

        said = "我以前喜欢喝茶，最近改喝咖啡了。我在杭州上班，下周三要去面试，想练习一下。"
        assert await consolidate(store, service, said, plan) == [
            "outdated", "known", "added", "blocked", "added", "rejected", "added"]
        request = json.dumps(service.chat.requests[-1], ensure_ascii=False)
        assert "用户喜欢吃香菜" not in request  # forgotten facts never go back to the model

        rows = {row["text"]: row for row in await service.list(ROBOT, "warm", True)}
        tea, coffee = rows["用户喜欢喝茶"], rows["用户最近改喝咖啡，不喝茶了"]
        assert (tea["status"], tea["superseded_by"]) == ("outdated", coffee["id"])
        assert (coffee["origin"], coffee["source_turn_id"]) == ("auto", "t100")
        assert rows["用户喜欢吃辣"]["importance"] == 0.8
        city = rows["用户在杭州上班"]
        assert (city["version"], city["origin"], city["status"]) == (1, "manual", "active")
        assert "用户在杭州的公司上班" in rows
        interview = rows["用户下周三要去面试"]
        assert (interview["kind"], interview["persona_id"]) == ("episodes", "warm")
        assert not any("张三" in text or "很喜欢吃香菜" in text for text in rows)
        chats = await service.chat_list(ROBOT, "warm", "chat-1")
        assert [row["text"] for row in chats] == ["这次聊天用户想练习面试"]

        # A close restatement of an automatic fact replaces its wording in place.
        assert await consolidate(store, service, "我真的很能吃辣", lambda payload, alias_of: [
            {"op": "add", "kind": "semantic", "text": "用户喜欢吃辣的", "importance": 0.5}]) == ["merged"]
        rows = {row["text"]: row for row in await service.list(ROBOT, "warm")}
        assert "用户喜欢吃辣" not in rows
        assert (rows["用户喜欢吃辣的"]["version"], rows["用户喜欢吃辣的"]["importance"]) == (2, 0.8)

        # A wrong retirement can be undone; a forgotten fact stays forgotten.
        with pytest.raises(ValueError, match="version conflict"):
            await service.restore(ROBOT, "warm", tea["id"], tea["version"] - 1)
        assert (await service.restore(ROBOT, "warm", tea["id"], tea["version"]))["status"] == "active"
        tea = next(row for row in await service.list(ROBOT, "warm") if row["id"] == tea["id"])
        assert tea["superseded_by"] is None and tea["last_hit_ms"]
        await service.change(ROBOT, "warm", tea["id"], tea["version"])
        assert sql(store, "SELECT text,status FROM long_term_memories WHERE id=?", (tea["id"],)) == [
            ("用户喜欢喝茶", "deleted")]
        with pytest.raises(ValueError):
            await service.restore(ROBOT, "warm", tea["id"], tea["version"] + 1)

    asyncio.run(exercise())


def test_a_roasting_ban_stays_in_this_chat_unless_it_should_last(tmp_path):
    async def exercise():
        store, service = new_service(tmp_path)
        assert await consolidate(store, service, "别骂我了，以后也别骂", lambda payload, alias_of: [
            {"op": "add", "kind": "persona", "text": "用户要求小安妮不要骂他", "importance": 0.8},
            {"op": "add", "kind": "semantic", "text": "用户要求以后一直不要骂他", "importance": 0.8},
        ]) == ["added", "added"]
        assert [row["text"] for row in await service.list(ROBOT, "warm")] == ["用户要求以后一直不要骂他"]
        assert [row["text"] for row in await service.chat_list(ROBOT, "warm", "chat-1")] == [
            "用户要求小安妮不要骂他"]

    asyncio.run(exercise())


def test_every_turn_recall_counts_hits_without_a_change_event(tmp_path):
    async def exercise():
        store, service = new_service(tmp_path)
        now = memory.now_ms()
        for kind, key, text in (("episodes", "interview", "用户下周三要去杭州面试"),
                                ("semantic", "cat", "用户养了一只猫叫团子"),
                                ("semantic", "spicy", "用户喜欢吃辣")):
            await service.put(ROBOT, "warm", kind, key, text,
                              source_turn_id="t0", source_created_ms=now - DAY_MS)
        await asyncio.sleep(0.01)
        changes = []
        service.on_change = changes.append

        hint = await service.recall_hint(ROBOT, "warm", "我下周三要去面试了，有点紧张")
        assert "【你记得的相关旧事" in hint and "[昨天] 用户下周三要去杭州面试" in hint
        assert "团子" not in hint
        assert await service.recall_hint(ROBOT, "warm", "今天天气不错") == ""
        row = next(row for row in await service.list(ROBOT, "warm") if row["fact_key"] == "interview")
        assert (row["hits"], row["hit_days"], row["version"]) == (1, 1, 1) and row["last_hit_ms"] >= now

        found = await service.recall(ROBOT, "warm", "面试")
        assert [(item["fact_key"], item["when"]) for item in found["evidence"]] == [("interview", "昨天")]
        row = next(row for row in await service.list(ROBOT, "warm") if row["fact_key"] == "interview")
        assert (row["hits"], row["hit_days"]) == (2, 1)  # the same day counts once
        await asyncio.sleep(0.01)
        assert changes == []

        insight = service.insight(ROBOT)
        assert insight["recall_turns"] == 2
        assert [entry["source"] for entry in insight["recalls"]] == ["tool", "turn"]
        assert insight["recalls"][1]["hits"][0]["why"]

    asyncio.run(exercise())


def test_bootstrap_puts_this_chat_first_then_the_weightiest_facts(tmp_path):
    async def exercise():
        store, service = new_service(tmp_path)
        now = memory.now_ms()
        expires = now + 3_600_000
        await service.chat_put(ROBOT, "warm", "chat-1", "no_jokes", "这次聊天别讲冷笑话",
                               expires_at_ms=expires)
        await service.put(ROBOT, "warm", "semantic", "blue", "用户随口提过喜欢蓝色", importance=0.2)
        await service.put(ROBOT, "warm", "semantic", "peanut", "用户对花生过敏", importance=0.9)
        await service.put(ROBOT, "warm", "semantic", "fish", "用户以前养过鱼", importance=0.3,
                          source_turn_id="t0", source_created_ms=now - 400 * DAY_MS)
        snapshot = await service.bootstrap_snapshot(ROBOT, "warm", "chat-1")
        assert snapshot["text"].startswith(BOOTSTRAP_HEADER)
        assert snapshot["text"][len(BOOTSTRAP_HEADER):].split("\n") == [
            "[这次聊天·今天] 这次聊天别讲冷笑话", "[今天] 用户对花生过敏", "[今天] 用户随口提过喜欢蓝色"]
        assert snapshot["expires_at_ms"] == expires
        fish = next(row for row in await service.list(ROBOT, "warm") if row["fact_key"] == "fish")
        assert fish["faded"] is True  # still listed and searchable, never opens a session
        assert (await service.recall(ROBOT, "warm", "养过鱼"))["evidence"]
        empty, _ = new_service(tmp_path / "empty")
        assert await MemoryService(empty, FakeChat(), (ROBOT,)).bootstrap(ROBOT, "warm", "chat-1") == ""

    asyncio.run(exercise())


def row(ident, *, said="你好", status="completed", updated=0, source="user", chat="c1", reply="嗯"):
    return {"id": ident, "turn_id": f"t{ident}", "chat_id": chat, "persona_id": "warm",
            "user_text": said, "assistant_generated_text": reply, "assistant_spoken_text": None,
            "turn_source": source, "status": status, "created_ms": updated, "updated_ms": updated}


def test_batches_wait_for_quiet_or_a_long_talk():
    now = 10_000_000
    quiet = [row(1, updated=now - 200_000), row(2, updated=now - 130_000)]
    assert ready_batch(quiet, now, 120_000, 24) == (quiet, 2)
    assert ready_batch([row(1, updated=now - 200_000), row(2, updated=now - 10_000)],
                       now, 120_000, 24) is None
    long_talk = [row(n, updated=now - 1000) for n in range(1, 5)] + [row(5, status="running", updated=now)]
    assert ready_batch(long_talk, now, 120_000, 3) == (long_talk[:3], 3)
    assert ready_batch([row(1, updated=now - 200_000), row(2, status="running", updated=now - 1000)],
                       now, 120_000, 24) is None
    abandoned = [row(1, updated=now - 900_000), row(2, status="running", updated=now - 600_000)]
    assert ready_batch(abandoned, now, 120_000, 24) == (abandoned, 2)
    assert ready_batch([], now, 120_000, 24) is None


def test_conversations_group_by_chat_and_keep_her_own_lines():
    rows = [
        row(1, said="我下周去面试", reply="加油"),
        row(2, said="", source="proactive", reply="今天也要开心"),
        row(3, said="在吗", chat="c2", status="running"),
        row(4, said="", source="proactive", chat="c3", reply="有人吗"),
        row(5, said="谢谢", reply=""),
    ]
    rows[4]["assistant_spoken_text"] = "不客气"
    assert [(persona, chat, [turn[1:] for turn in turns], source["turn_id"])
            for persona, chat, turns, source in conversations(rows)] == [
        ("warm", "c1", [("我下周去面试", "加油"), ("", "今天也要开心"), ("谢谢", "不客气")], "t5")]


def test_worker_waits_for_quiet_then_consolidates_once(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "POLL_SECONDS", 0.01)

    async def exercise():
        def plan(payload, alias_of):
            return [{"op": "add", "kind": "episodes", "text": "用户下周三要去杭州面试", "importance": 0.8}]

        store, service = new_service(tmp_path, plan, idle_seconds=0.3)
        say(store, 1, "很早以前的一句话", memory.now_ms() - 60_000)
        changes = []
        service.on_change = changes.append
        await service.start()
        # Enabling memory starts after the existing dialogue: old talk is never billed.
        await wait_for(lambda: sql(store, "SELECT turn_row_id FROM memory_progress WHERE robot_id=?",
                                   (ROBOT,)) == [(1,)])
        now = memory.now_ms()
        say(store, 2, "我下周三要去杭州面试", now)
        say(store, 3, "", now + 10, source="proactive", reply="祝你顺利！")
        await wait_for(lambda: service.pending.get(ROBOT))
        assert service.pending[ROBOT]["turns"] == 1
        assert not service.consolidations[ROBOT]
        await wait_for(lambda: service.consolidations[ROBOT])
        await wait_for(lambda: service.pending.get(ROBOT) is None)
        await asyncio.sleep(0.05)

        entry = service.consolidations[ROBOT][0]
        assert entry["turns"] == 1 and [item["result"] for item in entry["results"]] == ["added"]
        assert len(service.chat.requests) == 1
        assert [(turn["用户"], turn["小安妮"]) for turn in service.chat.requests[0]["对话"]] == [
            ("我下周三要去杭州面试", "好的"), ("", "祝你顺利！")]
        assert changes == [ROBOT]
        assert sql(store, "SELECT turn_row_id FROM memory_progress WHERE robot_id=?", (ROBOT,)) == [(3,)]
        learned, = await service.list(ROBOT, "warm")
        assert (learned["text"], learned["source_turn_id"]) == ("用户下周三要去杭州面试", "t2")
        assert service.insight(ROBOT)["error"] is None
        await service.close()

    asyncio.run(exercise())


def test_a_batch_the_model_keeps_failing_on_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "POLL_SECONDS", 0.01)

    async def exercise():
        def plan(payload, alias_of):
            raise RuntimeError("model down")

        store, service = new_service(tmp_path, plan, idle_seconds=0)
        await asyncio.to_thread(service._pending, ROBOT)
        say(store, 1, "我下周三要去面试", memory.now_ms() - 60_000)
        await service.start()
        await wait_for(lambda: service.consolidations[ROBOT])
        assert service.consolidations[ROBOT][0]["error"] == "RuntimeError"
        assert len(service.chat.requests) == memory.CONSOLIDATE_ATTEMPTS
        assert service.insight(ROBOT)["error"] == "RuntimeError"
        await wait_for(lambda: sql(store, "SELECT turn_row_id FROM memory_progress WHERE robot_id=?",
                                   (ROBOT,)) == [(1,)])
        await asyncio.sleep(0.1)
        assert len(service.chat.requests) == memory.CONSOLIDATE_ATTEMPTS
        await service.close()

    asyncio.run(exercise())


def test_recap_folds_turns_and_ends_with_the_conversation(tmp_path):
    async def exercise():
        store, service = new_service(tmp_path)
        assert await service.recap(ROBOT, "", [("我下周去面试", "加油"), ("有点紧张", "正常的")]) == "前情2轮"
        assert service.insight(ROBOT)["recap"]["folded"] == 2
        assert await service.recap(ROBOT, "前情2轮", [("谢谢", "不客气")]) == "前情1轮"
        assert service.chat.requests[-1]["之前的前情"] == "前情2轮"
        assert service.insight(ROBOT)["recap"] | {"updated_ms": 0} == {
            "text": "前情1轮", "folded": 3, "updated_ms": 0}
        service.end_recap(ROBOT)
        assert service.insight(ROBOT)["recap"] is None

    asyncio.run(exercise())


def test_migration_adds_curve_columns_and_backfills_origin(tmp_path):
    if sqlite3.sqlite_version_info < (3, 35):
        pytest.skip("needs ALTER TABLE DROP COLUMN")
    store = RuntimeStore(tmp_path / "memory.db")
    store.migrate()
    for name in ("importance", "hits", "hit_days", "last_hit_ms", "superseded_by", "origin"):
        sql(store, f"ALTER TABLE long_term_memories DROP COLUMN {name}")
    for ident, source in (("a", "t1"), ("b", None)):
        sql(store, """INSERT INTO long_term_memories (id,robot_id,persona_id,kind,fact_key,text,
                      source_turn_id,source_created_ms,version,status,updated_ms,index_state)
                      VALUES (?,?,'','semantic',?,'用户喜欢喝茶',?,1,1,'active',1,'ready')""",
            (ident, ROBOT, ident, source))
    store.migrate()
    assert sql(store, """SELECT id,importance,hits,hit_days,last_hit_ms,superseded_by,origin
                         FROM long_term_memories ORDER BY id""") == [
        ("a", 0.5, 0, 0, None, None, "auto"), ("b", 0.5, 0, 0, None, None, "manual")]
