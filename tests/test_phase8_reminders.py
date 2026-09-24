"""8B: real SQLite task state + LangGraph nodes; fake Realtime, never fake hardware."""

import asyncio
import json
import sqlite3
import time
from types import SimpleNamespace

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


def test_realtime_reminder_tool_creates_real_task_and_rejects_external(tmp_path):
    async def exercise():
        rt = build(tmp_path)
        await ready(rt)
        try:
            a = rt.realtime["a"]
            update = next(event for event in a.socket.outbound
                          if event["type"] == "session.update")
            tools = {tool["function"]["name"] for tool in update["session"]["tools"]}
            assert "create_reminder" in tools
            assert "只有工具返回status=scheduled及task_id后" in update["session"]["instructions"]
            await a.submit_text("两小时后提醒我取外卖")
            event = {"call_id": "reminder-call", "name": "create_reminder",
                     "arguments": json.dumps({"text": "取外卖", "delay_seconds": 7200})}
            await a._tool_call(event)
            row, = await rt.reminder_list("a")
            assert row["text"] == "取外卖"
            assert row["status"] == "scheduled"
            assert row["due_ms"] - row["updated_ms"] >= 7200_000 - 1000
            outputs = [e for e in a.socket.outbound if
                       e.get("item", {}).get("type") == "function_call_output"]
            assert json.loads(outputs[-1]["item"]["output"])["task_id"] == row["task_id"]
            await a._tool_call(event)
            assert len(await rt.reminder_list("a")) == 1
            assert await rt.reminder_list("b") == []
            a.external_turn_id = a.turn_id
            await a._tool_call({**event, "call_id": "untrusted-event"})
            assert json.loads(a.socket.outbound[-1]["item"]["output"])["reason"] == (
                "external_event_no_reminder")
            assert len(await rt.reminder_list("a")) == 1
        finally:
            await rt.close()

    asyncio.run(exercise())


def test_restart_batches_missed_reminders(tmp_path):
    async def exercise():
        first = build(tmp_path)
        await ready(first)
        tasks = [await first.reminder_create("a", text, 86_400_000, text)
                 for text in ("接水", "喝水", "取外卖")]
        unknown = await first.reminder_create("a", "不能重播", 86_400_000, "unknown")
        await asyncio.to_thread(first.reminders.store.change, "a", unknown["task_id"],
                                "scheduled", "submitting", first.reminders._now())
        await first.close()
        now = first.reminders._now()
        with sqlite3.connect(tmp_path / "runtime.db") as db:
            for item, delay in zip(tasks, (120_000, 120_000, 3600_000), strict=True):
                db.execute("UPDATE phase8_reminders SET due_ms=? WHERE task_id=?",
                           (now-delay, item["task_id"]))
            db.execute("""UPDATE phase8_reminders SET status='expired',
                reason='late_reminder' WHERE task_id=?""", (tasks[1]["task_id"],))
        second = build(tmp_path)
        await ready(second)
        try:
            for _ in range(200):
                rows = await second.reminder_list("a")
                if sum(row["status"] == "submitted_unknown" for row in rows) == 3:
                    break
                await asyncio.sleep(.01)
            assert sum(row["status"] == "submitted_unknown" for row in rows) == 3
            assert next(row for row in rows if row["text"] == "不能重播")[
                "status"] == "send_unknown"
            outbound = second.realtime["a"].socket.outbound
            assert sum(x["type"] == "response.create" for x in outbound) == 1
            message = next(x for x in outbound if x.get("type") == "conversation.item.create")
            prompt = message["item"]["content"][0]["text"]
            assert all(text in prompt for text in ("接水", "喝水", "取外卖"))
            assert "晚了约2分钟" in prompt and "项目重启" in prompt
            assert "还需要提醒吗" in prompt
            assert next(row for row in rows if row["text"] == "取外卖")[
                "reason"] == "missed_confirmation"
            await second.reminders.dispatch("a")
            assert sum(x["type"] == "response.create" for x in outbound) == 1
        finally:
            await second.close()

    asyncio.run(exercise())


def test_overdue_offline_uses_bounded_retry_and_ready_wakeup(tmp_path, monkeypatch):
    from annie_runtime import reminders

    monkeypatch.setattr(reminders, "RETRY_SECONDS", .03)

    async def exercise():
        rt = build(tmp_path)
        await ready(rt)
        try:
            task = await rt.reminder_create("a", "接水", 86_400_000, "offline")
            client = rt.realtime["a"]
            client.ready = False
            now = rt.reminders._now()
            with sqlite3.connect(tmp_path / "runtime.db") as db:
                db.execute("""UPDATE phase8_reminders SET due_ms=?, expires_ms=?
                    WHERE task_id=?""", (now-120_000, now-1, task["task_id"]))
            scans = 0
            original = rt.reminders.store.next_due

            def counted(robot_id):
                nonlocal scans
                scans += 1
                return original(robot_id)

            rt.reminders.store.next_due = counted
            rt.reminders.wake("a")
            await asyncio.sleep(.16)
            saved = await asyncio.to_thread(rt.reminders.store.get, "a", task["task_id"])
            assert saved["status"] == "scheduled"
            assert scans < 30  # no 1ms busy-loop after legacy expiration
            client.ready = True
            client.publish("session.ready")
            for _ in range(100):
                saved = await asyncio.to_thread(rt.reminders.store.get,
                                                "a", task["task_id"])
                if saved["status"] == "submitted_unknown":
                    break
                await asyncio.sleep(.01)
            assert saved["status"] == "submitted_unknown"
        finally:
            await rt.close()

    asyncio.run(exercise())


def test_two_robots_batch_only_their_own_reminders(tmp_path):
    async def exercise():
        rt = build(tmp_path)
        await ready(rt)
        try:
            a = await rt.reminder_create("a", "A喝水", 86_400_000, "same")
            b = await rt.reminder_create("b", "B接水", 86_400_000, "same")
            now = rt.reminders._now()
            with sqlite3.connect(tmp_path / "runtime.db") as db:
                for item in (a, b):
                    db.execute("UPDATE phase8_reminders SET due_ms=? WHERE task_id=?",
                               (now-120_000, item["task_id"]))
            await asyncio.gather(rt.reminders.dispatch("a"), rt.reminders.dispatch("b"))
            for robot_id, own, other in (("a", "A喝水", "B接水"),
                                         ("b", "B接水", "A喝水")):
                sent = rt.realtime[robot_id].socket.outbound
                prompt = next(x for x in sent if x["type"] ==
                              "conversation.item.create")["item"]["content"][0]["text"]
                assert own in prompt and other not in prompt
                assert sum(x["type"] == "response.create" for x in sent) == 1
        finally:
            await rt.close()

    asyncio.run(exercise())


def test_worker_survives_transient_sqlite_read_failure(tmp_path, monkeypatch):
    from annie_runtime import reminders

    monkeypatch.setattr(reminders, "RETRY_SECONDS", .03)

    async def exercise():
        rt = build(tmp_path)
        await ready(rt)
        try:
            task = await rt.reminder_create("a", "恢复检查", 1000, "fault")
            original = rt.reminders.store.next_due
            failures = 0

            def flaky(robot_id):
                nonlocal failures
                failures += 1
                if failures == 1:
                    raise sqlite3.OperationalError("temporary database error")
                return original(robot_id)

            rt.reminders.store.next_due = flaky
            for _ in range(150):
                saved = await asyncio.to_thread(rt.reminders.store.get,
                                                "a", task["task_id"])
                if saved["status"] == "submitted_unknown":
                    break
                await asyncio.sleep(.01)
            assert failures >= 2
            assert saved["status"] == "submitted_unknown"
        finally:
            await rt.close()

    asyncio.run(exercise())


def test_completed_tts_without_receipt_does_not_strand_reminder(tmp_path):
    async def exercise():
        rt = build(tmp_path)
        await ready(rt)
        try:
            row = await rt.reminder_create("a", "关电脑", 86_400_000, "tts-done")
            client = rt.realtime["a"]
            original = client.speech
            finished = asyncio.create_task(asyncio.sleep(0))
            await finished
            client.speech = SimpleNamespace(
                turn_id="old-tts", worker=finished, synthesized_complete=True,
                local_output_completed_at=time.monotonic(), receipt_tracks={})
            now = row["due_ms"] + 1
            assert (await rt.reminders.dispatch("a", now_ms=now))["status"] == "scheduled"
            client.speech.local_output_completed_at -= 3
            assert (await rt.reminders.dispatch("a", now_ms=now))[
                "status"] == "submitted_unknown"
            assert sum(x["type"] == "response.create" for x in client.socket.outbound) == 1
            client.speech = original
        finally:
            await rt.close()

    asyncio.run(exercise())


def test_real_speaker_progress_still_blocks_reminder(tmp_path):
    async def exercise():
        rt = build(tmp_path)
        await ready(rt)
        try:
            row = await rt.reminder_create("a", "关电脑", 86_400_000, "speaker")
            client = rt.realtime["a"]
            original = client.speech
            finished = asyncio.create_task(asyncio.sleep(0))
            await finished
            client.speech = SimpleNamespace(
                turn_id="old-tts", worker=finished, synthesized_complete=True,
                local_output_completed_at=time.monotonic() - 100,
                receipt_tracks={"speaker": {"state": "progress"}})
            assert (await rt.reminders.dispatch("a", now_ms=row["due_ms"] + 1))[
                "status"] == "scheduled"
            assert not any(x["type"] == "response.create" for x in client.socket.outbound)
            client.speech = original
        finally:
            await rt.close()

    asyncio.run(exercise())


def test_reminder_prompt_is_not_user_history(tmp_path):
    async def exercise():
        rt = build(tmp_path)
        await ready(rt)
        try:
            task = await rt.reminder_create("a", "吃饭", 86_400_000, "internal")
            result = await rt.reminders.dispatch("a", now_ms=task["due_ms"] + 1)
            client = rt.realtime["a"]
            turn_id = result["turn_id"]
            cached = client.robot.cached_turn(turn_id)
            assert cached["user_text"] == "" and cached["turn_source"] == "external"
            assert "现在提醒：吃饭" in cached["external_prompt"]
            started = [e for e in client.events if e["kind"] == "turn.started"][-1]
            assert started["user_text"] == "" and started["turn_source"] == "external"
            assert any("现在提醒：吃饭" in e["item"]["content"][0]["text"]
                       for e in client.socket.outbound if e.get("item", {}).get("role") == "user")
            await client.handle({"type": "response.text.done", "text": "吃饭啦"},
                                client.robot.generation)
            await client.handle({"type": "response.done", "response": {"status": "completed"}},
                                client.robot.generation)
            await rt.ledger.flush()
            saved = rt.store.get_conversation_turn("a", turn_id)
            assert saved["user_text"] == "" and saved["turn_source"] == "external"
            assert "现在提醒：吃饭" in saved["external_prompt"]
            assert saved["assistant_generated_text"] == "吃饭啦"
            assert all(item["turn_id"] != turn_id for item in client.robot.recent_context())
            assert all(item["turn_id"] != turn_id for item in
                       rt.store.recent_conversation_context("a", saved["chat_id"],
                                                            saved["persona_id"], 20))
        finally:
            await rt.close()

    asyncio.run(exercise())


def test_legacy_prompt_migration_retains_diagnostic_text(tmp_path):
    async def exercise():
        rt = build(tmp_path)
        await ready(rt)
        try:
            await rt.realtime["a"].submit_text("我要吃饭")
            await rt.ledger.flush()
            await rt.realtime["a"].handle(
                {"type": "response.done", "response": {"status": "completed"}},
                rt.robots["a"].generation)
            await rt.realtime["a"].submit_text("我喜欢喝茶")
            await rt.ledger.flush()
        finally:
            await rt.close()
        path = tmp_path / "runtime.db"
        with sqlite3.connect(path) as db:
            turn_id = db.execute("SELECT turn_id FROM conversation_turns WHERE user_text='我要吃饭'").fetchone()[0]
            db.execute("UPDATE conversation_turns SET user_text=? WHERE turn_id=?",
                       ("【真实到期的单次提醒，非用户当前发言。】现在提醒：吃饭", turn_id))
            db.execute("PRAGMA user_version=7")
        rt.store.migrate()
        saved = rt.store.get_conversation_turn("a", turn_id)
        assert saved["turn_source"] == "external" and saved["user_text"] == ""
        assert "现在提醒：吃饭" in saved["external_prompt"]
        with sqlite3.connect(path) as db:
            assert db.execute("""SELECT turn_source,user_text FROM conversation_turns
                WHERE user_text='我喜欢喝茶'""").fetchone() == ("user", "我喜欢喝茶")
        rt.store.migrate()
        assert rt.store.get_conversation_turn("a", turn_id) == saved

    asyncio.run(exercise())
