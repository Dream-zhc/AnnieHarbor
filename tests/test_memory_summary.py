import asyncio
import json

from annie_adapters import RuntimeStore
from annie_brain.memory import MemoryService, is_meta_memory, prepare_consolidated_fact
from annie_brain.memory_consolidate import EPISODE_IMPORTANCE, parse_ops
from annie_brain.memory_summary import parse_summary, summary_fit


class SummaryChat:
    def __init__(self):
        self.calls = 0

    async def complete(self, _system, _user, *, max_tokens):
        self.calls += 1
        return json.dumps({"summary": "常聊天气，喜欢吃辣"}, ensure_ascii=False)


def memory_at(tmp_path):
    store = RuntimeStore(tmp_path / "memory.db")
    store.migrate()
    return MemoryService(store, SummaryChat(), ("a",))


def consolidated(memory, reply, turn, aliases=None):
    return memory._apply("a", "annie", "c1", parse_ops(json.dumps(reply, ensure_ascii=False), aliases or {}),
                         {"turn_id": turn, "created_ms": 1_000})


async def episodes(memory):
    return sorted(row["text"] for row in await memory.list("a", "annie") if row["kind"] == "episodes")


def test_conversation_episode_becomes_a_fading_memory():
    ops = parse_ops('{"episode": "11月18日下午用户和小安妮聊了杭州的天气", "ops": []}', {})
    assert ops == [{"op": "add", "kind": "episodes", "text": "11月18日下午用户和小安妮聊了杭州的天气",
                    "importance": EPISODE_IMPORTANCE, "target": None, "conversation": True}]
    assert parse_ops('{"episode": "", "ops": []}', {}) == []


def test_prompt_episode_example_is_kept():
    text = "11月18日下午用户和小安妮聊了杭州的天气，还请她跳了段舞，她答应下次学个新动作"
    assert prepare_consolidated_fact("episodes", text) == text


def test_consolidator_verdict_is_not_a_memory():
    assert is_meta_memory("", "这段对话属于闲聊，不提取为长期记忆。")
    assert not is_meta_memory("", "用户喜欢吃辣")


def test_summary_is_usable_until_a_summarized_fact_changes():
    rows = [{"id": 1, "version": 1, "updated_ms": 100}, {"id": 2, "version": 1, "updated_ms": 300}]
    summary = {"text": "她记得的事", "facts": {1: 1}, "facts_ms": 200}
    assert summary_fit(summary, rows) == (True, [rows[1]])
    assert summary_fit(summary, [{"id": 1, "version": 2, "updated_ms": 400}]) == (False, [])
    assert summary_fit(summary, rows[1:]) == (False, [])
    assert summary_fit(None, rows) == (False, [])
    assert parse_summary('{"summary": " 常聊\\n天气 "}') == "常聊 天气"


def test_each_conversation_of_an_afternoon_keeps_its_own_episode(tmp_path):
    async def run():
        memory = memory_at(tmp_path)
        afternoon = ["11月18日下午用户和小安妮聊了杭州的天气", "11月18日下午用户和小安妮聊了跳舞",
                     "11月18日下午用户和小安妮打了招呼，问她叫什么"]
        results = [consolidated(memory, {"episode": text, "ops": []}, f"t{n}")[0]["result"]
                   for n, text in enumerate(afternoon)]
        assert results == ["added", "added", "added"]
        assert consolidated(memory, {"episode": afternoon[1], "ops": []}, "t3")[0]["result"] == "known"
        assert await episodes(memory) == sorted(afternoon)

    asyncio.run(run())


def test_a_forgotten_episode_stays_forgotten_without_blocking_other_conversations(tmp_path):
    async def run():
        memory = memory_at(tmp_path)
        weather = "11月18日下午用户和小安妮聊了杭州的天气"
        consolidated(memory, {"episode": weather, "ops": []}, "t1")
        row = next(row for row in await memory.list("a", "annie") if row["kind"] == "episodes")
        await memory.change("a", "annie", row["id"], row["version"])
        assert consolidated(memory, {"episode": weather, "ops": []}, "t2")[0]["result"] == "blocked"
        other = "11月18日下午用户和小安妮聊了北京的天气"
        assert consolidated(memory, {"episode": other, "ops": []}, "t3")[0]["result"] == "added"
        assert await episodes(memory) == [other]

    asyncio.run(run())


def test_consolidation_cannot_rewrite_or_retire_an_episode(tmp_path):
    async def run():
        memory = memory_at(tmp_path)
        weather = "11月17日下午用户和小安妮聊了杭州的天气"
        ident = consolidated(memory, {"episode": weather, "ops": []}, "t1")[0]["id"]
        for op in ("update", "outdated"):
            result = consolidated(memory, {"episode": "", "ops": [
                {"op": op, "id": "m1", "kind": "semantic", "text": "用户喜欢杭州的天气"}]},
                f"t-{op}", {"m1": ident})
            assert result[0]["op"] == op and result[0]["result"] in {"added", "known"}
        assert await episodes(memory) == [weather]

    asyncio.run(run())


def test_sessions_open_with_the_summary_until_one_of_its_facts_is_forgotten(tmp_path):
    async def run():
        memory = memory_at(tmp_path)
        ident, version = await memory.put("a", "annie", "semantic", "food", "用户喜欢吃辣")
        assert (await memory.summary("a", "annie"))["state"] == "missing"
        await memory.refresh_summary("a", "annie")
        assert (await memory.summary("a", "annie"))["state"] == "current"
        assert "常聊天气，喜欢吃辣" in await memory.bootstrap("a", "annie", "c1")
        await memory.put("a", "annie", "semantic", "city", "用户住在杭州")
        opening = await memory.bootstrap("a", "annie", "c1")
        assert "常聊天气，喜欢吃辣" in opening and "用户住在杭州" in opening
        assert (await memory.summary("a", "annie"))["state"] == "behind"
        restarted = MemoryService(memory.store, SummaryChat(), ("a",))
        assert (await restarted.summary("a", "annie"))["text"] == "常聊天气，喜欢吃辣"
        await memory.change("a", "annie", ident, version)
        assert (await memory.summary("a", "annie"))["state"] == "stale"
        opening = await memory.bootstrap("a", "annie", "c1")
        assert "喜欢吃辣" not in opening and "用户住在杭州" in opening

    asyncio.run(run())
