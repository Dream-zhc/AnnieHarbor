"""Realtime companion regressions: fake cloud, real async receive loop and SQLite."""

import asyncio
import json
import time
from functools import wraps

from annie_core import RobotConfig
from annie_runtime import HarborRuntime

from tests.test_phase4_realtime import FakeSocket


def async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return run


class Cloud:
    async def embed(self, text):
        return [1., 0.]

    async def extract(self, text, existing):
        return []

    async def close(self):
        pass


class SlowWeather:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def current(self, city):
        self.started.set()
        await self.release.wait()
        return {"status": "ok", "city": city}


async def wait_until(predicate, timeout=2):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.005)


def make_runtime(tmp_path, *, memory=False):
    return HarborRuntime(
        [RobotConfig("a", room_id="roomv2"), RobotConfig("b", room_id="roomv3")],
        tmp_path / "runtime.db", realtime_key="fake",
        realtime_socket_factory=FakeSocket,
        memory_model=Cloud() if memory else None,
    )


async def ready(rt):
    await rt.start()
    await wait_until(lambda: all(c.ready for c in rt.realtime.values()))


async def start_weather(c):
    c.weather = SlowWeather()
    await c.submit_text("北京天气")
    await c.socket.inbound.put({"type": "response.created", "response": {"id": "weather-response"}})
    await c.socket.inbound.put({
        "type": "response.function_call_arguments.done", "response_id": "weather-response",
        "call_id": "weather-call", "name": "get_weather",
        "arguments": json.dumps({"city": "北京"}),
    })
    await asyncio.wait_for(c.weather.started.wait(), 1)


def tool_outputs(c):
    return [e for e in c.socket.outbound if e["type"] == "conversation.item.create"
            and e["item"]["type"] == "function_call_output"]


@async_test
async def test_receive_loop_keeps_consuming_speech_while_weather_waits(tmp_path):
    rt = make_runtime(tmp_path)
    await ready(rt)
    c = rt.realtime["a"]
    try:
        await start_weather(c)
        await c.socket.inbound.put({"type": "input_audio_buffer.speech_started", "item_id": "new"})
        await asyncio.sleep(.06)
        assert c.speech_started_count == 1
        assert not c.weather.release.is_set()
        # Another head's ordinary chat is also unaffected.
        b = rt.realtime["b"]
        await b.submit_text("你好")
        await b.socket.inbound.put({"type": "response.text.delta", "delta": "你好呀"})
        await wait_until(lambda: b.text == "你好呀")
    finally:
        c.weather.release.set()
        await asyncio.wait_for(rt.close(), 3)


@async_test
async def test_response_done_waits_for_tool_and_follows_up_exactly_once(tmp_path):
    rt = make_runtime(tmp_path)
    await ready(rt)
    c = rt.realtime["a"]
    try:
        await start_weather(c)
        owner = c.turn_id
        await c.socket.inbound.put({"type": "response.done", "response": {
            "id": "weather-response", "status": "completed"}})
        await c.socket.inbound.put({"type": "input_audio_buffer.speech_started", "item_id": "candidate"})
        await asyncio.sleep(.04)
        assert c.speech_started_count == 1
        assert c.turn_id == owner
        assert len([e for e in c.socket.outbound if e["type"] == "response.create"]) == 1
        c.weather.release.set()
        await wait_until(lambda: any(e["kind"] == "tool.followup" for e in c.events))
        assert len(tool_outputs(c)) == 1
        assert len([e for e in c.socket.outbound if e["type"] == "response.create"]) == 2
        assert c.turn_id == owner
    finally:
        c.weather.release.set()
        await asyncio.wait_for(rt.close(), 3)


@async_test
async def test_cancelled_tool_cannot_write_to_next_turn(tmp_path):
    rt = make_runtime(tmp_path)
    await ready(rt)
    c = rt.realtime["a"]
    try:
        await start_weather(c)
        await c.cancel()
        await c.submit_text("换个话题")
        owner = c.turn_id
        c.weather.release.set()
        await asyncio.sleep(.04)
        assert tool_outputs(c) == []
        assert c.turn_id == owner
        assert not c.followup_needed
    finally:
        c.weather.release.set()
        await asyncio.wait_for(rt.close(), 3)


@async_test
async def test_deleted_bootstrap_fact_is_replaced_in_same_provider_session(tmp_path):
    rt = make_runtime(tmp_path, memory=True)
    rt.store.migrate()
    ident, version = await rt.memory.put("a", "annie", "semantic", "drink", "用户喜欢乌龙茶")
    await ready(rt)
    c = rt.realtime["a"]
    try:
        assert "用户喜欢乌龙茶" in c.prompt_snapshot["instructions"]
        session = c.session
        await rt.memory_change("a", "annie", ident, version)
        await asyncio.sleep(.08)
        assert "用户喜欢乌龙茶" not in c._combined_instructions(c._fresh_visual())
        await wait_until(lambda: "用户喜欢乌龙茶" not in c.prompt_snapshot["instructions"])
        assert c.ready and c.session == session
        assert await rt.memory.list("a", "annie") == []
    finally:
        await asyncio.wait_for(rt.close(), 3)


@async_test
async def test_new_chat_constraint_and_expiry_refresh_without_restart(tmp_path):
    rt = make_runtime(tmp_path, memory=True)
    await ready(rt)
    c = rt.realtime["a"]
    try:
        session = c.session
        identity = c.robot.session_state()
        await rt.chat_memory_put("a", "tone", "今天少开玩笑", int(time.time()*1000)+250,
            chat_id=identity["chat_id"], persona_id=identity["persona_id"], generation=identity["generation"])
        await asyncio.sleep(.08)
        assert "今天少开玩笑" in c._combined_instructions(c._fresh_visual())
        await asyncio.sleep(.35)
        assert "今天少开玩笑" not in c._combined_instructions(c._fresh_visual())
        assert c.session == session and c.ready
    finally:
        await asyncio.wait_for(rt.close(), 3)


@async_test
async def test_social_hint_updates_and_expires_without_stopping_chat(tmp_path):
    rt = make_runtime(tmp_path)
    await ready(rt)
    c = rt.realtime["a"]
    try:
        session = c.session
        await rt.update_social_state("a", {"conversation_mode": "emotional_support", "advice_readiness": False},
            source="observed", generation=c.robot.generation, ttl_ms=200)
        hint = "当前用户请求优先；以下只是可随当前话题调整的交谈提示"
        assert hint in c._combined_instructions(c._fresh_visual())
        await asyncio.sleep(.25)
        assert hint not in c._combined_instructions(c._fresh_visual())
        assert c.session == session and c.ready
    finally:
        await asyncio.wait_for(rt.close(), 3)


def test_existing_tts_parameters_are_passed_to_speech(tmp_path):
    from tests.test_phase7_runtime import Publisher, room_server
    rt = HarborRuntime([RobotConfig("a", room_id="roomv2")], tmp_path/"runtime.db", room_server(),
        realtime_key="fake", realtime_socket_factory=FakeSocket, tts_voice="fake-voice",
        room_tts_factory=Publisher, tts_first_chars=2, tts_min_chars=4, tts_max_chars=30)
    speech = rt.realtime["a"].speech
    assert (speech.first_chars, speech.min_chars, speech.max_chars) == (2, 4, 30)


@async_test
async def test_provider_cancel_does_not_wait_for_slow_tool(tmp_path):
    rt = make_runtime(tmp_path)
    await ready(rt)
    c = rt.realtime["a"]
    try:
        await start_weather(c)
        await c.socket.inbound.put({"type": "response.done", "response": {
            "id": "weather-response", "status": "cancelled"}})
        await asyncio.sleep(.06)
        assert c.turn_id is None
        assert tool_outputs(c) == []
        c.weather.release.set()
        await asyncio.sleep(.02)
        assert not c.followup_needed
    finally:
        c.weather.release.set()
        await asyncio.wait_for(rt.close(), 3)


@async_test
async def test_queued_unowned_tool_is_not_authorized_by_later_turn(tmp_path):
    rt = make_runtime(tmp_path)
    await ready(rt)
    c = rt.realtime["a"]
    try:
        # Queue without yielding to the worker, then create a later user turn.
        await c.handle({"type": "response.function_call_arguments.done", "call_id": "early",
            "name": "preview_action", "arguments": json.dumps({"description": "点头"})},
            c.robot.generation, background_tools=True)
        c._create_turn("聊点别的")
        await asyncio.sleep(.04)
        outputs = tool_outputs(c)
        assert outputs
        assert json.loads(outputs[0]["item"]["output"])["status"] == "rejected"
    finally:
        await asyncio.wait_for(rt.close(), 3)


@async_test
async def test_memory_correction_refreshes_without_cloud_index_delay(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path, memory=True)
    rt.store.migrate()
    ident, version = await rt.memory.put("a", "annie", "semantic", "drink", "用户喜欢茶")
    await ready(rt)
    c = rt.realtime["a"]
    async def slow_embed(text):
        await asyncio.sleep(2)
        return [1., 0.]
    monkeypatch.setattr(rt.memory.model, "embed", slow_embed)
    monkeypatch.setattr("annie_brain.memory.MEMORY_REINDEX_DEADLINE_SEC", .03)
    try:
        started = time.monotonic()
        result = await rt.memory_change("a", "annie", ident, version, "用户喜欢咖啡")
        assert time.monotonic() - started < .3
        assert result["status"] == "active"
        await wait_until(lambda: "用户喜欢咖啡" in c._combined_instructions(c._fresh_visual()))
        assert "用户喜欢茶" not in c._combined_instructions(c._fresh_visual())
        assert (await rt.memory.list("a", "annie"))[0]["index_state"] == "failed"
    finally:
        await asyncio.wait_for(rt.close(), 3)


@async_test
async def test_new_confirmed_voice_preempts_tool_before_any_tts_exists(tmp_path):
    rt = make_runtime(tmp_path)
    await ready(rt)
    c = rt.realtime["a"]
    try:
        await start_weather(c)
        old = c.turn_id
        await c.socket.inbound.put({"type": "input_audio_buffer.speech_started", "item_id": "next-user"})
        await c.socket.inbound.put({"type": "conversation.item.input_audio_transcription.completed",
            "item_id": "next-user", "transcript": "不查了，聊点别的"})
        await asyncio.sleep(.06)
        assert c.turn_id != old
        assert c.robot.turn_cache[old]["status"] == "cancelled"
        assert c.robot.turn_cache[c.turn_id]["user_text"] == "不查了，聊点别的"
        c.weather.release.set()
        await asyncio.sleep(.02)
        assert tool_outputs(c) == []
    finally:
        c.weather.release.set()
        await asyncio.wait_for(rt.close(), 3)


@async_test
async def test_followup_send_stays_owned_and_cancelled(tmp_path):
    rt = make_runtime(tmp_path)
    await ready(rt)
    c = rt.realtime["a"]
    try:
        await start_weather(c)
        sending, release = asyncio.Event(), asyncio.Event()
        original_send = c.socket.send
        async def delayed_send(event):
            if event["type"] == "response.create":
                sending.set()
                await release.wait()
            await original_send(event)
        c.socket.send = delayed_send
        await c.socket.inbound.put({"type": "response.done", "response": {
            "id": "weather-response", "status": "completed"}})
        c.weather.release.set()
        await asyncio.wait_for(sending.wait(), 1)
        assert c._tool_worker is not None
        await c.cancel()
        release.set()
        await asyncio.sleep(.03)
        assert len([e for e in c.socket.outbound if e["type"] == "response.create"]) == 1
    finally:
        c.weather.release.set()
        await asyncio.wait_for(rt.close(), 3)


@async_test
async def test_old_cancelled_done_does_not_cancel_new_tool(tmp_path):
    rt = make_runtime(tmp_path)
    await ready(rt)
    c = rt.realtime["a"]
    try:
        await c.submit_text("旧话题")
        await c.handle({"type": "response.created", "response": {"id": "old-response"}}, c.robot.generation)
        await c.cancel()
        await start_weather(c)
        await c.socket.inbound.put({"type": "response.done", "response": {
            "id": "old-response", "status": "cancelled"}})
        await asyncio.sleep(.04)
        assert "weather-call" in c._pending_tool_ids
        c.weather.release.set()
        await wait_until(lambda: bool(tool_outputs(c)))
    finally:
        c.weather.release.set()
        await asyncio.wait_for(rt.close(), 3)


@async_test
async def test_delete_during_session_handshake_cannot_restore_stale_fact(tmp_path):
    class HeldSocket(FakeSocket):
        gate = asyncio.Event()
        configured = asyncio.Event()
        async def send(self, event):
            if event["type"] == "session.update" and not self.configured.is_set():
                self.configured.set()
                await self.gate.wait()
            await super().send(event)
    rt = make_runtime(tmp_path, memory=True)
    rt.realtime["a"].socket_factory = HeldSocket
    rt.store.migrate()
    ident, version = await rt.memory.put("a", "annie", "semantic", "drink", "用户喜欢茶")
    try:
        await rt.start()
        await asyncio.wait_for(HeldSocket.configured.wait(), 1)
        await rt.memory_change("a", "annie", ident, version)
        HeldSocket.gate.set()
        c = rt.realtime["a"]
        await wait_until(lambda: c.ready)
        await wait_until(lambda: "用户喜欢茶" not in c.prompt_snapshot["instructions"])
        assert await rt.memory.list("a", "annie") == []
    finally:
        HeldSocket.gate.set()
        await asyncio.wait_for(rt.close(), 3)


@async_test
async def test_cancelled_memory_thread_still_invalidates_live_bootstrap(tmp_path):
    import threading

    rt = make_runtime(tmp_path, memory=True)
    rt.store.migrate()
    ident, version = await rt.memory.put("a", "annie", "semantic", "drink", "用户喜欢乌龙茶")
    await ready(rt)
    c = rt.realtime["a"]
    entered, release, committed = threading.Event(), threading.Event(), threading.Event()
    original = rt.memory._change

    def delayed(*args):
        entered.set()
        assert release.wait(2)
        result = original(*args)
        committed.set()
        return result

    rt.memory._change = delayed
    task = asyncio.create_task(rt.memory.change("a", "annie", ident, version))
    try:
        await asyncio.wait_for(asyncio.to_thread(entered.wait), 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        release.set()
        await asyncio.wait_for(asyncio.to_thread(committed.wait), 1)
        await wait_until(lambda: "用户喜欢乌龙茶" not in c.prompt_snapshot["instructions"])
        assert await rt.memory.list("a", "annie") == []
        assert c.ready
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.wait_for(rt.close(), 3)


@async_test
async def test_local_cancel_retires_response_before_tool_cleanup_await(tmp_path):
    rt = make_runtime(tmp_path)
    await ready(rt)
    c = rt.realtime["a"]
    cleanup = asyncio.Event()
    release = asyncio.Event()

    class Weather(SlowWeather):
        async def current(self, city):
            self.started.set()
            try:
                await self.release.wait()
            finally:
                cleanup.set()
                await release.wait()

    c.weather = Weather()
    try:
        await c.submit_text("北京天气")
        owner = c.turn_id
        await c.handle({"type": "response.created", "response": {"id": "slow"}}, c.robot.generation)
        await c.handle({"type": "response.function_call_arguments.done", "response_id": "slow",
                        "call_id": "slow-tool", "name": "get_weather", "arguments": '{"city":"北京"}'},
                       c.robot.generation, background_tools=True)
        await asyncio.wait_for(c.weather.started.wait(), 1)
        task = asyncio.create_task(c.cancel())
        await asyncio.wait_for(cleanup.wait(), 1)
        await c.handle({"type": "response.done", "response": {"id": "slow", "status": "completed"}},
                       c.robot.generation, background_tools=True)
        release.set()
        await asyncio.wait_for(task, 1)
        assert c.robot.turn_cache[owner]["status"] == "cancelled"
        assert c.robot.turn_cache[owner]["cancelled"] is True
    finally:
        release.set()
        await asyncio.wait_for(rt.close(), 3)


@async_test
async def test_social_update_cannot_write_second_field_into_new_chat(tmp_path):
    import threading

    rt = make_runtime(tmp_path)
    rt.store.migrate()
    robot = rt.robots["a"]
    entered, release = threading.Event(), threading.Event()
    original = rt.store.upsert_social_state
    calls = 0

    def delayed(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = original(*args, **kwargs)
        if calls == 1:
            entered.set()
            assert release.wait(2)
        return result

    rt.store.upsert_social_state = delayed
    task = asyncio.create_task(rt.update_social_state(
        "a", {"conversation_mode": "emotional_support", "humor_level": "none"},
        source="observed", generation=robot.generation))
    try:
        await asyncio.wait_for(asyncio.to_thread(entered.wait), 1)
        await robot.switch_session(chat_id="new-chat", persona_id="annie")
        release.set()
        result = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(result[0], ValueError)
        assert await rt.social_state("a") == {}
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.wait_for(rt.close(), 3)
