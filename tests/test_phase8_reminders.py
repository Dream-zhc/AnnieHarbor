"""8B: real SQLite task state + LangGraph nodes; fake Realtime, never fake hardware."""

import asyncio

import httpx
from annie_core import RobotConfig
from annie_runtime import HarborRuntime
from annie_web import create_app

from tests.test_phase5 import FakeSocket


def build(tmp_path):
    return HarborRuntime(
        [RobotConfig("a", room_id="roomv2"), RobotConfig("b", room_id="roomv3")],
        tmp_path / "runtime.db", realtime_key="fake-key",
        realtime_socket_factory=FakeSocket,
    )


async def ready(rt):
    await rt.start()
    for _ in range(100):
        if all(client.ready for client in rt.realtime.values()):
            return
        await asyncio.sleep(.01)
    raise AssertionError("Realtime not ready")


async def create(rt, key="unique", robot_id="a"):
    return await rt.reminder_create(robot_id, "喝水", 5000, key)


def test_reminder_control_due_dedupe_and_scope(tmp_path):
    async def exercise():
        rt = build(tmp_path)
        await ready(rt)
        try:
            task = await create(rt)
            assert task["checkpoint"]["steps"] == ["validated", "scheduled"]
            task_id = task["task_id"]
            assert (await create(rt))["task_id"] == task_id
            assert len(await rt.reminder_list("a")) == 1
            assert await rt.reminder_change("a", task_id, "pause")
            assert (await rt.reminders.dispatch("a", now_ms=task["due_ms"]+1)) is None
            await rt.reminder_change("a", task_id, "resume")
            a, b = rt.realtime["a"], rt.realtime["b"]
            a.speech_active = True
            blocked = await rt.reminders.dispatch("a", now_ms=task["due_ms"]+1)
            assert blocked["status"] == "scheduled"
            a.speech_active = False
            first, second = await asyncio.gather(
                rt.reminders.dispatch("a", now_ms=task["due_ms"]+1),
                rt.reminders.dispatch("a", now_ms=task["due_ms"]+1))
            assert {first["status"], second["status"]} <= {
                "submitted_unknown", "submitting"}
            saved = await asyncio.to_thread(rt.reminders.store.get, "a", task_id)
            assert saved["status"] == "submitted_unknown"
            assert saved["checkpoint"]["steps"] == [
                "validated", "scheduled", "due_checked", "result_prepared"]
            assert saved["result"]["text"] == "喝水"
            assert saved["result"]["device_executed"] is False
            assert saved["turn_id"] == a.turn_id
            assert sum(x["type"] == "response.create" for x in a.socket.outbound) == 1
            assert not any(x["type"] == "response.create" for x in b.socket.outbound)
            assert await rt.reminders.dispatch("a", now_ms=task["due_ms"]+1) is None
            try:
                await rt.reminder_change("a", task_id, "cancel")
            except ValueError as exc:
                assert "already dispatched" in str(exc)
            else:
                raise AssertionError("false cancellation of submitted speech")
            assert not rt.reminders.graph.get_graph().nodes.get("weather")
        finally:
            await rt.close()

    asyncio.run(exercise())


def test_restart_adopts_only_same_session_and_unknown_send_not_replayed(tmp_path):
    async def exercise():
        first = build(tmp_path)
        await ready(first)
        survive = await create(first, "survive")
        lost = await create(first, "unknown")
        await asyncio.to_thread(first.reminders.store.change, "a", lost["task_id"],
            "scheduled", "submitting", first.reminders._now())
        cancelled = await create(first, "cancel")
        await first.reminder_change("a", cancelled["task_id"], "cancel")
        await first.close()

        second = build(tmp_path)
        await ready(second)
        try:
            adopted = await asyncio.to_thread(second.reminders.store.get,
                                              "a", survive["task_id"])
            assert adopted["session_epoch"] == second.session_state("a")["session_epoch"]
            unknown = await asyncio.to_thread(second.reminders.store.get,
                                              "a", lost["task_id"])
            assert unknown["status"] == "send_unknown"
            cancelled_again = await asyncio.to_thread(second.reminders.store.get,
                                              "a", cancelled["task_id"])
            assert cancelled_again["status"] == "cancelled"
            await second.reminders.dispatch("a", now_ms=survive["due_ms"]+1)
            assert sum(x["type"] == "response.create" for x in
                       second.realtime["a"].socket.outbound) == 1
        finally:
            await second.close()

    asyncio.run(exercise())


def test_chat_switch_stale_graph_failure_and_normal_chat(tmp_path):
    async def exercise():
        rt = build(tmp_path)
        await ready(rt)
        try:
            stale = await create(rt, "stale")
            await rt.switch_session("a", chat_id="other", persona_id="annie")
            row = await asyncio.to_thread(rt.reminders.store.get, "a", stale["task_id"])
            assert row["status"] == "stale_session"
            assert await rt.reminders.dispatch("a", now_ms=stale["due_ms"]+1) is None
            assert not any(x["type"] == "response.create" for x in
                           rt.realtime["a"].socket.outbound)
            task = await create(rt, "fail")
            class BrokenGraph:
                async def ainvoke(self, state):
                    raise RuntimeError("graph offline")

            rt.reminders.graph = BrokenGraph()
            await rt.reminders.dispatch("a", now_ms=task["due_ms"]+1)
            row = await asyncio.to_thread(rt.reminders.store.get, "a", task["task_id"])
            assert row["status"] == "failed"
            assert row["reason"] == "RuntimeError"
            assert (await rt.realtime_text("a", "正常聊天"))["status"] == "accepted"
        finally:
            await rt.close()

    asyncio.run(exercise())


def test_reminder_http_contract_and_per_robot_history(tmp_path):
    async def exercise():
        rt = build(tmp_path)
        await ready(rt)
        try:
            app = create_app(rt, tmp_path)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                base = "/api/robots/a/reminders"
                assert (await client.post(base, json={
                    "text": "喝水", "delay_ms": 5000, "request_key": "http"})).status_code == 200
                tasks = (await client.get(base)).json()["tasks"]
                assert len(tasks) == 1
                task_id = tasks[0]["task_id"]
                assert (await client.get("/api/robots/b/reminders")).json()["tasks"] == []
                path = base + "/" + task_id + "/control"
                assert (await client.post(path, json={"action":"pause"})).json()["status"] == "paused"
                assert (await client.post(path, json={"action":"resume"})).json()["status"] == "scheduled"
                assert (await client.post(path, json={"action":"cancel"})).json()["status"] == "cancelled"
                assert (await client.post(path, json={"action":"cancel"})).json()["status"] == "cancelled"
                assert (await client.post(path, json={"action":"invented"})).status_code == 422
                assert (await client.post("/api/robots/b/reminders/"+task_id+"/control",
                    json={"action":"pause"})).status_code == 404
                assert (await client.post(base, json={
                    "text":"错误内容", "delay_ms":5000, "request_key":"http"})).status_code == 409
        finally:
            await rt.close()

    asyncio.run(exercise())


def test_automatic_due_waits_for_microphone_and_no_idle_graph(tmp_path):
    async def exercise():
        rt = build(tmp_path)
        await ready(rt)
        try:
            assert rt.reminders.graph is None
            assert not await asyncio.to_thread(rt.reminders.store.exists)
            assert await rt.reminder_list("a") == []
            a = rt.realtime["a"]
            a.speech_active = True
            item = await rt.reminder_create("a", "倒杯水", 1000, "auto")
            await asyncio.sleep(1.15)
            blocked = await asyncio.to_thread(rt.reminders.store.get, "a", item["task_id"])
            assert blocked["status"] == "scheduled"
            assert not any(x["type"] == "response.create" for x in a.socket.outbound)
            a.speech_active = False
            a.publish("speech.stopped")
            for _ in range(100):
                saved = await asyncio.to_thread(rt.reminders.store.get, "a", item["task_id"])
                if saved["status"] == "submitted_unknown":
                    break
                await asyncio.sleep(.01)
            assert saved["status"] == "submitted_unknown"
            assert sum(x["type"] == "response.create" for x in a.socket.outbound) == 1
        finally:
            await rt.close()

    asyncio.run(exercise())


def test_graph_completion_after_session_switch_never_speaks(tmp_path):
    async def exercise():
        rt = build(tmp_path)
        await ready(rt)
        try:
            item = await create(rt, "graph-race")
            graph = rt.reminders.graph
            started, release = asyncio.Event(), asyncio.Event()

            class DelayedGraph:
                async def ainvoke(self, state):
                    started.set()
                    await release.wait()
                    return await graph.ainvoke(state)

            rt.reminders.graph = DelayedGraph()
            runner = asyncio.create_task(rt.reminders.dispatch("a", now_ms=item["due_ms"]+1))
            await started.wait()
            await rt.switch_session("a", chat_id="new", persona_id="annie")
            release.set()
            outcome = await runner
            assert outcome["status"] == "stale_session"
            assert not any(x["type"] == "response.create" for x in
                           rt.realtime["a"].socket.outbound)
        finally:
            await rt.close()

    asyncio.run(exercise())
