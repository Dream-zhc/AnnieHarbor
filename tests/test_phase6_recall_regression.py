"""A consolidated fact about the visitor must reach the actual Realtime session in another Chat."""

import asyncio
import json

from annie_adapters import RuntimeStore
from annie_brain import memory as memory_module
from annie_brain.memory import BOOTSTRAP_BYTES, BOOTSTRAP_HEADER, MemoryService, prepare_consolidated_fact
from annie_core import RobotConfig
from annie_runtime import HarborRuntime

from tests.test_phase4_realtime import FakeSocket

VISIT = "用户说11月12日有朋友来找他"
CANCELLED = "用户说朋友不来找他了"


class Chat:
    def __init__(self, plan=None):
        self.plan = plan or (lambda payload, alias_of: [])

    async def close(self):
        pass

    async def complete(self, _system, user, *, max_tokens):
        payload = json.loads(user)
        if "滑出的对话" in payload:
            return json.dumps({"recap": ""})
        alias_of = {item["内容"]: item["编号"] for item in payload["已经记得的事"]}
        return json.dumps({"ops": self.plan(payload, alias_of)}, ensure_ascii=False)


def visit_plan(payload, alias_of):
    if "不来" in payload["对话"][-1]["用户"]:
        return [{"op": "outdated", "id": alias_of[VISIT], "kind": "episodes", "text": CANCELLED}]
    return [{"op": "add", "kind": "episodes", "text": VISIT, "importance": 0.7}]


async def consolidate(memory, said, *, chat="测试", persona="annie-lively"):
    now = memory_module.now_ms()
    await memory._consolidate("a", persona, chat, [(now, said, "好呀")],
                              {"turn_id": f"t{now}", "created_ms": now})
    return [item["result"] for item in memory.consolidations["a"][0]["results"]]


def test_consolidated_visit_reaches_the_realtime_session_of_another_chat(tmp_path):
    async def run():
        database = tmp_path / "runtime.db"
        store = RuntimeStore(database)
        store.migrate()
        memory = MemoryService(store, Chat(visit_plan), ("a",))
        assert await consolidate(memory, "下周三我朋友来找我") == ["added"]
        await memory.close()

        FakeSocket.instances.clear()
        rt = HarborRuntime(
            [RobotConfig("a", room_id="roomv2", fake_delay_ms=0)],
            database, realtime_key="fake", realtime_socket_factory=FakeSocket,
            memory_chat=Chat(),
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
            assert VISIT in instructions
            assert BOOTSTRAP_HEADER.strip() in instructions
            assert "recall_memory" in [t["function"]["name"]
                                       for t in updates[0]["session"]["tools"]]
            assert await rt.memory.bootstrap("a", "other-persona", "测试 a") == ""
        finally:
            await rt.close()

    asyncio.run(run())


def test_bootstrap_is_bounded_scoped_to_the_persona_and_skips_questions(tmp_path):
    async def run():
        store = RuntimeStore(tmp_path / "runtime.db")
        store.migrate()
        memory = MemoryService(store, Chat(), ("a",))
        for text in ("用户询问下周有没有人找他", "用户问过如果下周朋友来找他怎么办"):
            assert prepare_consolidated_fact("episodes", text) is None, text
        for number in range(40):
            await memory.put("a", "annie-lively", "episodes", f"trip_{number}",
                             f"用户去过第{number}个城市旅行，玩得很开心")
        await memory.put("a", "other", "episodes", "hike", "用户和朋友约好去爬山")
        opening = await memory.bootstrap("a", "annie-lively", "测试 a")
        assert len(opening.encode()) <= len(BOOTSTRAP_HEADER.encode()) + BOOTSTRAP_BYTES
        assert 0 < opening.count("旅行") <= memory_module.BOOTSTRAP_LONG_FACTS
        assert "爬山" not in opening
        await memory.close()

    asyncio.run(run())


def test_cancellation_or_forget_takes_the_plan_out_of_the_session(tmp_path):
    async def run():
        store = RuntimeStore(tmp_path / "runtime.db")
        store.migrate()
        memory = MemoryService(store, Chat(visit_plan), ("a",))
        assert await consolidate(memory, "下周三我朋友来找我") == ["added"]
        assert VISIT in await memory.bootstrap("a", "annie-lively", "new")
        assert await consolidate(memory, "下周三朋友不来找我了", chat="new") == ["outdated"]
        opening = await memory.bootstrap("a", "annie-lively", "new")
        assert VISIT not in opening and CANCELLED in opening

        assert await consolidate(memory, "下周三我朋友来找我", persona="annie-calm") == ["added"]
        row = (await memory.list("a", "annie-calm"))[0]
        await memory.change("a", "annie-calm", row["id"], row["version"])
        assert VISIT not in await memory.bootstrap("a", "annie-calm", "new")
        assert await consolidate(memory, "下周三我朋友来找我", persona="annie-calm") == ["blocked"]
        assert VISIT not in await memory.bootstrap("a", "annie-calm", "new")
        await memory.close()

    asyncio.run(run())
