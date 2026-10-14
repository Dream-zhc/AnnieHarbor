"""Realtime model selection, clock context, TTS connection reuse and weather default city."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from annie_adapters.realtime import (
    MODEL,
    QwenRealtimeSocket,
    realtime_url,
    session_update,
    validate_model,
)
from annie_core import RobotConfig
from annie_runtime import HarborRuntime
from annie_runtime.realtime import clock_instructions
from annie_runtime.speech import WARM_SOCKET_MAX_IDLE_SECONDS, SpeechOutput

from tests.test_phase4b import FakePublisher, FakeRobot, FakeTts
from tests.test_phase5 import FakeSocket

ROOT = Path(__file__).resolve().parents[1]


def test_realtime_model_is_selectable_within_the_same_wire_family():
    url = realtime_url("wss://ws.example/api-ws/v1/realtime", "qwen-audio-3.1-realtime-plus")
    assert url.endswith("model=qwen-audio-3.1-realtime-plus")
    assert QwenRealtimeSocket(url, "k").url == url  # pinned model survives the socket
    assert QwenRealtimeSocket("wss://ws.example/api-ws/v1/realtime", "k").url.endswith(MODEL)
    for bad in ("qwen3-asr-flash-realtime", "qwen-audio-3.1-realtime-plus;x", "", None):
        with pytest.raises(ValueError):
            validate_model(bad)
    with pytest.raises(ValueError):
        QwenRealtimeSocket("wss://ws.example/api-ws/v1/realtime?model=qwen3-asr-flash-realtime", "k")


def test_runtime_pins_configured_model_and_host(tmp_path):
    rt = HarborRuntime([RobotConfig("a", room_id="roomv2")], tmp_path / "rt.db",
                       realtime_key="fake", realtime_socket_factory=FakeSocket,
                       realtime_url="wss://llm-ws.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime",
                       realtime_model="qwen-audio-3.1-realtime-plus")
    client = rt.realtime["a"]
    assert client.model == "qwen-audio-3.1-realtime-plus"
    assert client.url.endswith("?model=qwen-audio-3.1-realtime-plus")
    snapshot = client.snapshot()
    assert snapshot["model"] == "qwen-audio-3.1-realtime-plus"
    assert snapshot["endpoint_host"] == "llm-ws.cn-beijing.maas.aliyuncs.com"
    with pytest.raises(ValueError):
        HarborRuntime([RobotConfig("a", room_id="roomv2")], tmp_path / "bad.db",
                      realtime_key="fake", realtime_model="gpt-realtime")


def test_clock_is_beijing_time_and_reaches_session_instructions(tmp_path):
    # 2026-10-13 07:05 UTC == 15:05 Beijing, a Tuesday.
    text = clock_instructions(1791875100000)
    assert "北京时间2026年10月13日 星期二 15:05" in text

    async def run():
        FakeSocket.instances.clear()
        rt = HarborRuntime([RobotConfig("a", room_id="roomv2", fake_delay_ms=0)],
                           tmp_path / "clock.db", realtime_key="fake",
                           realtime_socket_factory=FakeSocket)
        await rt.start()
        try:
            for _ in range(150):
                if rt.realtime["a"].ready:
                    break
                await asyncio.sleep(.01)
            update = next(e for e in FakeSocket.instances[-1].outbound
                          if e["type"] == "session.update")
            assert "【当前时间】北京时间" in update["session"]["instructions"]
            assert "【语音输出】第一句短而直接" in update["session"]["instructions"]
        finally:
            await rt.close()
    asyncio.run(run())


def make_output(factory=FakeTts):
    events = []
    output = SpeechOutput(FakeRobot(), FakePublisher(),
                          url="wss://example.test/api-ws/v1/inference", key="fake",
                          voice="keep", socket_factory=factory, first_chars=2,
                          event=lambda kind, **fields: events.append((kind, fields)))
    return output, events


async def speak(output, turn, text):
    identity = output.robot.session_state()
    await output.feed(turn, identity, text)
    await output.finish(turn)
    await asyncio.wait_for(output.worker, 2)


def test_second_reply_reuses_the_finished_tts_connection():
    async def run():
        FakeTts.instances.clear()
        output, events = make_output()
        try:
            await speak(output, "t1", "你好。")
            await speak(output, "t2", "再见。")
            assert len(FakeTts.instances) == 1  # one handshake for two replies
            runs = [m for m in FakeTts.instances[0].sent if m["header"]["action"] == "run-task"]
            assert len(runs) == 2 and runs[0]["header"]["task_id"] != runs[1]["header"]["task_id"]
            origins = [fields["origin"] for kind, fields in events if kind == "tts.connection"]
            assert origins == ["fresh", "reused"]
            assert output.snapshot()["connection"]["reused"] == 1
        finally:
            await output.close()
        assert FakeTts.instances[0].closed
    asyncio.run(run())


def test_dead_or_stale_warm_connection_falls_back_before_any_text():
    class DeadAfterFirstTask(FakeTts):
        async def send(self, message):
            if (message["header"]["action"] == "run-task"
                    and any(m["header"]["action"] == "run-task" for m in self.sent)):
                raise ConnectionError("provider closed idle connection")
            await super().send(message)

    async def run():
        FakeTts.instances.clear()
        output, events = make_output(DeadAfterFirstTask)
        try:
            await speak(output, "t1", "你好。")
            await speak(output, "t2", "还在吗。")
            assert len(FakeTts.instances) == 2
            texts = [m["payload"]["input"]["text"] for m in FakeTts.instances[1].sent
                     if m["header"]["action"] == "continue-task"]
            assert "".join(texts) == "还在吗。"  # nothing was lost to the dead socket
            assert output.snapshot()["connection"]["reuse_failed"] == 1
            assert output.error is None
            # A warm socket older than the trust window is replaced, not used.
            output._warm_at -= WARM_SOCKET_MAX_IDLE_SECONDS + 1
            await speak(output, "t3", "好的。")
            assert len(FakeTts.instances) == 3
            assert output.snapshot()["connection"]["stale_dropped"] == 1
        finally:
            await output.close()
    asyncio.run(run())


def test_prewarm_opens_once_and_interrupt_prewarms_the_next_reply():
    async def run():
        FakeTts.instances.clear()
        output, events = make_output()
        try:
            output.prewarm()
            output.prewarm()  # coalesced while the first opener runs
            await asyncio.wait_for(output._warm_task, 1)
            assert len(FakeTts.instances) == 1
            await speak(output, "t1", "你好。")
            origins = [fields["origin"] for kind, fields in events if kind == "tts.connection"]
            assert origins == ["prewarmed"] and len(FakeTts.instances) == 1
            identity = output.robot.session_state()
            await output.feed("t2", identity, "一段很长的话，")
            await output.cancel("valid_smart_turn")
            await asyncio.wait_for(output._warm_task, 1)
            assert output.snapshot()["connection"]["warm_ready"]
        finally:
            await output.close()
    asyncio.run(run())


def test_weather_without_city_uses_configured_home_city(tmp_path):
    from tests.test_phase7_runtime import ready, result_from_call, room_server

    async def run():
        def handler(request):
            assert request.url.params["city"] == "北京市"
            return httpx.Response(200, json={"status": "1", "lives": [{
                "city": "北京市", "adcode": "110000", "weather": "晴",
                "temperature": "18", "reporttime": "2026-10-13 15:00:00"}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            rt = HarborRuntime([RobotConfig("a", room_id="roomv2")], tmp_path / "w.db",
                               room_server(), realtime_key="fake",
                               realtime_socket_factory=FakeSocket,
                               weather_key="fake-map-key", weather_client=http,
                               weather_default_city="北京市")
            await ready(rt)
            try:
                a = rt.realtime["a"]
                a._create_turn("今天天气怎么样")
                ok = await result_from_call(a, "home", "get_weather", {})
                assert ok[0]["status"] == "ok" and ok[0]["default_city"] is True
                assert ok[0]["city"] == "北京市"
                a._finish("completed")
                a._create_turn("给我讲个笑话")
                rejected = await result_from_call(a, "unasked", "get_weather", {})
                assert rejected[0]["status"] == "rejected"
            finally:
                await rt.close()
    asyncio.run(run())

    tool = next(t for t in session_update("x", weather=True)["session"]["tools"]
                if t["function"]["name"] == "get_weather")
    assert tool["function"]["parameters"]["required"] == []
    assert "omit city" in json.dumps(tool, ensure_ascii=False)


def test_tool_called_before_final_asr_waits_for_the_confirmed_utterance(tmp_path):
    """smart_turn may emit a function call before ASR final; never reject it as unowned."""
    from tests.test_phase7_runtime import ready

    async def run():
        def handler(request):
            return httpx.Response(200, json={"status": "1", "lives": [{
                "city": "北京市", "adcode": "110000", "weather": "晴",
                "temperature": "18", "reporttime": "2026-10-13 15:00:00"}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            FakeSocket.instances.clear()
            rt = HarborRuntime([RobotConfig("a", room_id="roomv2")], tmp_path / "early.db",
                               realtime_key="fake", realtime_socket_factory=FakeSocket,
                               weather_key="fake-map-key", weather_client=http,
                               weather_default_city="北京市")
            await ready(rt)
            try:
                a = rt.realtime["a"]
                a.transcript = "上一句的旧识别"  # a late final ASR of the previous utterance
                sock = a.socket
                for event in (
                    {"type": "input_audio_buffer.speech_started", "item_id": "u1"},
                    {"type": "input_audio_buffer.speech_stopped", "item_id": "u1"},
                    {"type": "response.created", "response": {"id": "r1"}},
                    {"type": "response.function_call_arguments.done", "response_id": "r1",
                     "call_id": "early", "name": "get_weather", "arguments": "{}"},
                ):
                    await sock.inbound.put(event)
                await asyncio.sleep(.1)  # the call is waiting, not rejected
                assert not [e for e in sock.outbound if e["type"] == "conversation.item.create"]
                await sock.inbound.put({
                    "type": "conversation.item.input_audio_transcription.completed",
                    "item_id": "u1", "transcript": "今天天气怎么样"})
                await sock.inbound.put({"type": "response.done", "response": {
                    "id": "r1", "status": "completed", "output": []}})
                for _ in range(100):
                    outputs = [e for e in sock.outbound if e["type"] == "conversation.item.create"
                               and e["item"]["type"] == "function_call_output"]
                    if outputs and any(e["type"] == "response.create" for e in sock.outbound):
                        break
                    await asyncio.sleep(.01)
                result = json.loads(outputs[0]["item"]["output"])
                assert result["status"] == "ok" and result["default_city"] is True
                turn = rt.robots["a"].turn_cache[a.turn_id]
                assert turn["user_text"] == "今天天气怎么样"
            finally:
                await rt.close()
    asyncio.run(run())


def test_tool_first_reply_survives_previous_tts_turn_and_early_call(tmp_path):
    """Without wake gating, a new utterance's tool-first reply was cancelled as a barge-in."""
    from tests.test_phase7_runtime import Publisher, ready, room_server

    async def run():
        def handler(request):
            return httpx.Response(200, json={"status": "1", "lives": [{
                "city": "北京市", "adcode": "110000", "weather": "晴",
                "temperature": "18", "reporttime": "2026-10-13 15:00:00"}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            FakeSocket.instances.clear()
            rt = HarborRuntime([RobotConfig("a", room_id="roomv2")], tmp_path / "barge.db",
                               room_server(), realtime_key="fake",
                               realtime_socket_factory=FakeSocket, tts_voice="fake-voice",
                               tts_socket_factory=FakeTts, room_tts_factory=Publisher,
                               weather_key="fake-map-key", weather_client=http,
                               weather_default_city="北京市")
            await ready(rt)
            try:
                a = rt.realtime["a"]
                sock = a.socket

                async def feed(*events):
                    for event in events:
                        await sock.inbound.put(event)

                await feed(
                    {"type": "input_audio_buffer.speech_started", "item_id": "u0"},
                    {"type": "input_audio_buffer.speech_stopped", "item_id": "u0"},
                    {"type": "conversation.item.input_audio_transcription.completed",
                     "item_id": "u0", "transcript": "你好"},
                    {"type": "response.created", "response": {"id": "r0"}},
                    {"type": "response.text.delta", "response_id": "r0", "delta": "你好呀。"},
                    {"type": "response.done", "response": {"id": "r0", "status": "completed",
                                                           "output": []}},
                )
                for _ in range(200):
                    if a.speech.turn_id and a.speech.synthesized_complete and not a.turn_id:
                        break
                    await asyncio.sleep(.01)
                assert a.speech.turn_id and not a.turn_id  # finished reply, TTS owner lingers
                await feed(
                    {"type": "input_audio_buffer.speech_started", "item_id": "u1"},
                    {"type": "input_audio_buffer.speech_stopped", "item_id": "u1"},
                    {"type": "response.created", "response": {"id": "r1"}},
                    {"type": "response.function_call_arguments.done", "response_id": "r1",
                     "call_id": "w1", "name": "get_weather", "arguments": "{}"},
                    {"type": "conversation.item.input_audio_transcription.completed",
                     "item_id": "u1", "transcript": "今天天气怎么样"},
                    {"type": "response.done", "response": {"id": "r1", "status": "completed",
                                                           "output": []}},
                )
                outputs = []
                for _ in range(200):
                    outputs = [e for e in sock.outbound if e["type"] == "conversation.item.create"
                               and e["item"]["type"] == "function_call_output"]
                    creates = [e for e in sock.outbound if e["type"] == "response.create"]
                    if outputs and creates:
                        break
                    await asyncio.sleep(.01)
                assert "r1" not in a._cancelled_response_ids
                assert json.loads(outputs[0]["item"]["output"])["status"] == "ok"
                assert creates, "tool result must be followed by the spoken answer"
            finally:
                await rt.close()
    asyncio.run(run())


def test_early_call_from_an_abandoned_utterance_is_never_run_for_the_next_one(tmp_path):
    from tests.test_phase7_runtime import ready

    async def run():
        FakeSocket.instances.clear()
        rt = HarborRuntime([RobotConfig("a", room_id="roomv2")], tmp_path / "next.db",
                           realtime_key="fake", realtime_socket_factory=FakeSocket)
        await ready(rt)
        try:
            a = rt.realtime["a"]
            sock = a.socket
            for event in (
                {"type": "input_audio_buffer.speech_started", "item_id": "u1"},
                {"type": "response.created", "response": {"id": "r1"}},
                {"type": "response.function_call_arguments.done", "response_id": "r1",
                 "call_id": "old", "name": "create_reminder",
                 "arguments": json.dumps({"text": "关火", "delay_seconds": 600})},
                {"type": "input_audio_buffer.speech_started", "item_id": "u2"},
                {"type": "conversation.item.input_audio_transcription.completed",
                 "item_id": "u2", "transcript": "十分钟后提醒我关火"},
            ):
                await sock.inbound.put(event)
            for _ in range(100):
                outputs = [e for e in sock.outbound if e["type"] == "conversation.item.create"
                           and e["item"]["type"] == "function_call_output"]
                if outputs:
                    break
                await asyncio.sleep(.01)
            result = json.loads(outputs[0]["item"]["output"])
            assert result["status"] == "rejected"  # u1's call is not executed as u2's request
            assert not [e for e in a.events if e["kind"] == "tool.result"
                        and (e.get("result") or {}).get("status") == "scheduled"]
        finally:
            await rt.close()
    asyncio.run(run())


def test_motion_called_after_speaking_needs_no_second_model_round(tmp_path):
    from tests.test_phase7_runtime import ready, result_from_call, runtime

    async def run():
        rt = runtime(tmp_path)
        await ready(rt)
        try:
            a = rt.realtime["a"]
            a._create_turn("你这招真行")
            a.text = "好家伙，这招厉害。"  # the reply is already speaking
            await result_from_call(a, "spoken-first", "perform_action",
                                   {"script_id": "surprise", "anchor_text": "好家伙"})
            assert a.followup_needed is False
            a._finish("completed")
            a._create_turn("点个头")
            await result_from_call(a, "silent-first", "perform_action",
                                   {"script_id": "agree_micro_nod"})
            assert a.followup_needed is True  # nothing spoken yet: the model must still answer
        finally:
            await rt.close()
    asyncio.run(run())
