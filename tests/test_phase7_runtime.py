"""End-to-end Realtime Function Call -> SQLite -> room wire (fake transport).

No external device or cloud success is inferred from this test.
"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from annie_adapters import RoomServerConfig
from annie_core import RobotConfig
from annie_runtime import HarborRuntime
from annie_web import create_app

from tests.test_phase5 import FakeSocket

ROOT = Path(__file__).resolve().parents[1]


class Publisher:
    instances = {}

    def __init__(self, config, room_id, *, channel="tts"):
        self.room_id, self.channel = room_id, channel
        self.messages = []
        self.__class__.instances[room_id, channel] = self

    async def publish(self, value):
        self.messages.append(value)

    async def close(self):
        pass


def room_server():
    root = "ws://127.0.0.1:1/robot"
    channels = {name: f"reb_{name}" for name in
                ("main", "tts", "mic", "visualdata", "vision", "sam")}
    return RoomServerConfig(
        base_url=root + "/roomv2",
        room_root=root,
        channels=channels,
        default_urls={name: root + "/roomv2/" + path
                      for name, path in channels.items()},
        connect_timeout_sec=.05, reconnect_min_sec=.05, reconnect_max_sec=.05,
    )


def runtime(tmp_path, weather_client=None, persona_profiles=None):
    Publisher.instances.clear()
    FakeSocket.instances.clear()
    return HarborRuntime(
        [RobotConfig("a", room_id="roomv2"), RobotConfig("b", room_id="roomv3")],
        tmp_path / "runtime.db",
        room_server(),
        realtime_key="fake-key",
        realtime_socket_factory=FakeSocket,
        persona_profiles=persona_profiles,
        tts_voice="fake-voice",
        room_tts_factory=Publisher,
        room_action_factory=Publisher,
        action_catalog=ROOT / "config/action_scripts.json",
        actions_enabled=True,
        action_allowlist=("agree_micro_nod", "look_left",
                          "look_right", "fast_double_blink",
                          "surprise", "smirk", "proud", "skeptical", "shy"),
        weather_key="fake-map-key" if weather_client else None,
        weather_client=weather_client,
    )


async def ready(rt):
    await rt.start()
    for _ in range(100):
        if all(client.ready for client in rt.realtime.values()):
            return
        await asyncio.sleep(.01)
    raise AssertionError("Realtime test clients not ready")


async def result_from_call(client, call_id, name, arguments):
    await client._tool_call({
        "call_id": call_id, "name": name,
        "arguments": json.dumps(arguments, ensure_ascii=False),
    })
    outputs = [item for item in client.socket.outbound
               if item["type"] == "conversation.item.create"
               and item["item"]["type"] == "function_call_output"
               and item["item"]["call_id"] == call_id]
    if name in {"perform_action", "set_expression"}:
        # Transport-facing statuses live in Runtime evidence; Provider output
        # deliberately contains only a neutral async-action acknowledgement.
        return [item["result"] for item in client.events
                if item["kind"] == "tool.result" and item.get("call_id") == call_id]
    return [json.loads(item["item"]["output"]) for item in outputs]


def test_realtime_tool_action_and_expression_only_a_changes(tmp_path):
    async def exercise():
        rt = runtime(tmp_path)
        await ready(rt)
        try:
            a, b = rt.realtime["a"], rt.realtime["b"]
            tools = a.socket.outbound[0]["session"]["tools"]
            spoken = a.socket.outbound[0]["session"]["instructions"]
            assert "不要把‘动作已发出/已发送/指令已下发/动作已执行’当作给用户的回复" in spoken
            assert "好嘞，眨两下～" in spoken
            names = [item["function"]["name"] for item in tools]
            assert "perform_action" in names and "set_expression" in names
            assert "get_weather" not in names
            assert "preview_action" not in names
            ids = next(x["function"]["parameters"]["properties"]["script_id"]["enum"]
                       for x in tools if x["function"]["name"] == "perform_action")
            assert "invented_wave" not in ids
            a._create_turn("你点下头")
            tid = a.turn_id
            first = await result_from_call(a, "motion", "perform_action",
                                           {"script_id": "agree_micro_nod"})
            assert first[0]["status"] == "awaiting_audio_anchor"
            provider_output = next(item for item in a.socket.outbound
                                   if item["type"] == "conversation.item.create"
                                   and item["item"].get("call_id") == "motion")
            ack = json.loads(provider_output["item"]["output"])
            assert ack == {"status": "accepted"}
            assert "awaiting_audio_anchor" not in provider_output["item"]["output"]
            # The model must not convert an unneeded device ACK into
            # hedging language. The Room ledger retains the actual state.
            assert "physical_execution" not in provider_output["item"]["output"]
            repeat = await result_from_call(a, "motion", "perform_action",
                                            {"script_id": "agree_micro_nod"})
            assert repeat == [first[0]]
            assert Publisher.instances["roomv2", "main"].messages == []
            assert Publisher.instances["roomv3", "main"].messages == []
            a.publish("tts.first_published", turn_id=tid,
                      robot_id="a", generation=1)
            for _ in range(100):
                if Publisher.instances["roomv2", "main"].messages:
                    break
                await asyncio.sleep(.01)
            assert len(Publisher.instances["roomv2", "main"].messages) == 1
            assert Publisher.instances["roomv3", "main"].messages == []
            for _ in range(100):
                if (await rt.action_list("a"))["requests"][0]["status"] == "published_unknown":
                    break
                await asyncio.sleep(.01)
            assert (await rt.action_list("a"))["requests"][0]["status"] == "published_unknown"
            assert (await rt.action_list("b"))["requests"] == []
            a._finish("completed")
            a._create_turn("你给我做个开心表情")
            face = await result_from_call(a, "face", "set_expression", {"expression": "喜"})
            assert face[0]["status"] == "queued"
            for _ in range(100):
                if Publisher.instances["roomv2", "tts"].messages:
                    break
                await asyncio.sleep(.01)
            assert Publisher.instances["roomv2", "tts"].messages[0]["type"] == "expression"
            assert Publisher.instances["roomv3", "tts"].messages == []
            b._create_turn("你看左边")
            blocked = await result_from_call(b, "invalid", "perform_action",
                                             {"script_id": "invented_wave"})
            assert blocked[0]["status"] == "rejected"
            assert Publisher.instances["roomv3", "main"].messages == []
        finally:
            await rt.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(("script_id", "duration_ms"), [
    ("surprise", 1600), ("smirk", 1500), ("proud", 1950),
    ("skeptical", 1600), ("shy", 2000),
])
def test_expressive_script_sends_indexed_intent_before_alignment(tmp_path, script_id, duration_ms):
    async def exercise():
        rt = runtime(tmp_path)
        await ready(rt)
        try:
            a = rt.realtime["a"]
            session = a.socket.outbound[0]["session"]
            functions = {tool["function"]["name"]: tool["function"]
                         for tool in session["tools"]}
            assert script_id in functions["perform_action"]["parameters"][
                "properties"]["script_id"]["enum"]
            assert script_id in functions["perform_action"]["description"]
            assert script_id in session["instructions"]
            a._create_turn("刚才真是意想不到，太有意思了")
            turn_id = a.turn_id
            missing = await result_from_call(
                a, "missing-anchor", "perform_action", {"script_id": script_id},
            )
            assert missing[0]["status"] == "rejected"
            assert "requires anchor_text" in missing[0]["reason"]
            result = await result_from_call(
                a, "expressive", "perform_action",
                {"script_id": script_id, "anchor_text": "意想不到"},
            )
            assert result[0]["status"] == "awaiting_text_anchor"
            ack = next(item for item in a.socket.outbound
                       if item["type"] == "conversation.item.create"
                       and item["item"].get("call_id") == "expressive")
            assert json.loads(ack["item"]["output"]) == {"status": "accepted"}
            assert "awaiting_text_anchor" not in ack["item"]["output"]
            assert Publisher.instances["roomv2", "main"].messages == []
            assert Publisher.instances["roomv3", "main"].messages == []
            a.publish("tts.first_published", turn_id=turn_id,
                      robot_id="a", generation=a.robot.generation)
            await asyncio.sleep(.02)
            # Legacy first PCM is never sufficient for expressive scripts.
            assert Publisher.instances["roomv2", "main"].messages == []
            assert Publisher.instances["roomv3", "main"].messages == []
            # The generated text, not the ForcedAligner event, releases the
            # single Room reservation. No PCM/sample estimate is supplied.
            speech = a.speech
            speech.turn_id = turn_id
            speech.identity = dict(a.turn_identity)
            speech.stream_epoch += 1
            speech.generated_text = "哇，意想不到！"
            a.actions.on_text_ready({"robot_id": "a", "turn_id": turn_id})
            for _ in range(70):
                if Publisher.instances["roomv2", "main"].messages:
                    break
                await asyncio.sleep(.01)
            messages = Publisher.instances["roomv2", "main"].messages
            assert len(messages) == 1
            assert messages[0]["type"] == "action_prepare"
            assert messages[0]["data"]["script_id"] == script_id
            assert messages[0]["data"]["anchor_char_index"] == 2
            assert messages[0]["data"]["timing_mode"] == "phoneme_anchor_v1"
            assert "at_sample" not in messages[0]["data"]
            assert Publisher.instances["roomv3", "main"].messages == []
            assert (await rt.action_list("a"))["requests"][0]["duration_ms"] == duration_ms
        finally:
            await rt.close()

    asyncio.run(exercise())


def test_actual_realtime_delta_releases_one_intent_without_waiting_for_pcm(tmp_path):
    async def exercise():
        rt = runtime(tmp_path)
        await ready(rt)
        try:
            client = rt.realtime["a"]
            client._create_turn("来一个惊讶")

            async def block_synthesis(_turn_id, _identity):
                await asyncio.Event().wait()

            client.speech._synthesize_stream = block_synthesis
            result = await result_from_call(client, "call-live", "perform_action",
                                            {"script_id": "surprise", "anchor_text": "意想不到"})
            assert result[0]["status"] == "awaiting_text_anchor"
            await client.handle({"type": "response.text.delta", "delta": "哇，意想"},
                                client.robot.generation)
            await asyncio.sleep(.02)
            assert Publisher.instances["roomv2", "main"].messages == []
            await client.handle({"type": "response.text.delta", "delta": "不到！"},
                                client.robot.generation)
            for _ in range(70):
                if Publisher.instances["roomv2", "main"].messages:
                    break
                await asyncio.sleep(.01)
            events = Publisher.instances["roomv2", "main"].messages
            assert len(events) == 1
            assert events[0]["type"] == "action_prepare"
            assert events[0]["data"]["anchor_char_index"] == 2
            assert "at_sample" not in events[0]["data"]
            assert client.speech.frames == 0
            assert Publisher.instances["roomv3", "main"].messages == []
        finally:
            await rt.close()
    asyncio.run(exercise())


def test_cancel_before_audio_and_api_scope(tmp_path):
    async def exercise():
        rt = runtime(tmp_path)
        await ready(rt)
        try:
            a = rt.realtime["a"]
            a._create_turn("请你眨两下眼")
            turn = a.turn_id
            await result_from_call(a, "cancel-me", "perform_action",
                                   {"script_id": "fast_double_blink"})
            await a.cancel()
            a.publish("tts.first_published", turn_id=turn,
                      robot_id="a", generation=1)
            await asyncio.sleep(.03)
            assert Publisher.instances["roomv2", "main"].messages == []
            assert (await rt.action_list("a"))["requests"][0]["status"] == (
                "cancelled_before_send"
            )
            app = create_app(rt, ROOT)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                assert (await client.get("/api/robots/a/actions")).status_code == 200
                assert (await client.get("/api/robots/a/actions")).json()["requests"][0][
                    "status"
                ] == "cancelled_before_send"
                assert (await client.get("/api/robots/b/actions")).json()["requests"] == []
                assert (await client.get("/api/robots/unknown/actions")).status_code == 404
        finally:
            await rt.close()

    asyncio.run(exercise())


def test_live_weather_tool_uses_current_user_city_and_true_provider_data(tmp_path):
    async def exercise():
        def handler(request):
            return httpx.Response(200, json={"status": "1", "lives": [{
                "city": "上海", "adcode": "310000", "weather": "多云",
                "temperature": "24", "reporttime": "2026-09-22 09:00:00",
            }]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            rt = runtime(tmp_path, weather_client=client)
            await ready(rt)
            try:
                a = rt.realtime["a"]
                a._create_turn("上海天气怎么样")
                result = await result_from_call(a, "weather", "get_weather",
                                                {"city": "上海"})
                assert result[0]["source"] == "amap"
                assert result[0]["temperature_c"] == "24"
                assert "fake-map-key" not in str(result)
                rejected = await result_from_call(a, "wrongcity", "get_weather",
                                                  {"city": "../上海"})
                assert rejected[0]["status"] == "rejected"
            finally:
                await rt.close()

    asyncio.run(exercise())


def test_live_weather_accepts_city_suffix_but_not_different_district(tmp_path):
    async def exercise():
        def handler(request):
            assert request.url.params["city"] == "西安市"
            return httpx.Response(200, json={"status": "1", "lives": [{
                "city": "西安市", "adcode": "610100", "weather": "晴",
                "temperature": "25", "reporttime": "2026-09-24 14:00:00",
            }]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            rt = runtime(tmp_path, weather_client=client)
            await ready(rt)
            try:
                a = rt.realtime["a"]
                a._create_turn("西安的天气怎么样")
                result = await result_from_call(a, "city-suffix", "get_weather",
                                                {"city": "西安市"})
                assert result[0]["status"] == "ok"
                assert result[0]["adcode"] == "610100"
                rejected = await result_from_call(a, "wrong-district", "get_weather",
                                                  {"city": "西安区"})
                assert rejected[0]["status"] == "rejected"
            finally:
                await rt.close()

    asyncio.run(exercise())


def test_smallbrain_action_prep_requires_matching_asr_identity(tmp_path):
    async def exercise():
        rt = runtime(tmp_path)
        await ready(rt)
        try:
            a, b = rt.realtime["a"], rt.realtime["b"]
            a._create_turn("你看左边")
            a_turn = a.turn_id
            b._create_turn("你看右边")
            b_turn = b.turn_id
            await result_from_call(a, "action-a", "perform_action",
                                   {"script_id": "look_left"})
            await result_from_call(b, "action-b", "perform_action",
                                   {"script_id": "look_right"})
            rt._room_playback_receipt("a", {
                "type": "asr", "text": "你看左边", "from": "smallbrain",
                "event_id": "asr-event-a", "ts": 1789960000000,
            })
            rt._room_playback_receipt("a", {
                "type": "cancel", "reason": "action_prep",
                "asr_event_id": "wrong-event",
            })
            await asyncio.sleep(.01)
            assert a.turn_id == a_turn
            assert b.turn_id == b_turn
            rt._room_playback_receipt("a", {
                "type": "cancel", "reason": "action_prep",
                "asr_event_id": "asr-event-a",
            })
            for _ in range(80):
                if a.turn_id is None:
                    break
                await asyncio.sleep(.01)
            assert a.turn_id is None
            assert b.turn_id == b_turn
            assert (await rt.action_list("a"))["requests"][0][
                "status"
            ] == "cancelled_before_send"
            assert (await rt.action_list("b"))["requests"][0][
                "status"
            ] == "awaiting_audio_anchor"
            a.publish("tts.first_published", turn_id=a_turn,
                      robot_id="a", generation=1)
            await asyncio.sleep(.03)
            assert Publisher.instances["roomv2", "main"].messages == []
            assert Publisher.instances["roomv3", "main"].messages == []
            events = a.since(0)["events"]
            assert any(e["kind"] == "smallbrain.cancel.accepted" for e in events)
            assert any(e["kind"] == "smallbrain.cancel.ignored" for e in events)
        finally:
            await rt.close()

    asyncio.run(exercise())


def test_function_call_before_final_asr_returns_rejection_not_silent_hang(tmp_path):
    async def exercise():
        rt = runtime(tmp_path)
        await ready(rt)
        try:
            client = rt.realtime["a"]
            assert client.turn_id is None
            output = await result_from_call(
                client, "premature-action", "perform_action",
                {"script_id": "agree_micro_nod"},
            )
            assert output == [{
                "status": "rejected", "reason": "unconfirmed_current_turn",
                "real_device_executed": False,
            }]
            assert (await rt.action_list("a"))["requests"] == []
            await client.handle({
                "type": "response.done",
                "response": {"status": "completed"},
            }, client.robot.generation)
            assert client.socket.outbound[-1]["type"] == "response.create"
        finally:
            await rt.close()

    asyncio.run(exercise())


def test_only_one_physical_action_tool_is_exposed(tmp_path):
    async def exercise():
        rt = runtime(tmp_path)
        await ready(rt)
        try:
            a = rt.realtime["a"]
            functions = {
                item["function"]["name"]: item["function"]
                for item in a.socket.outbound[0]["session"]["tools"]
            }
            assert "perform_social_action" not in functions
            ids = functions["perform_action"][
                "parameters"]["properties"]["script_id"]["enum"]
            assert "agree_micro_nod" in ids
            assert "fast_double_blink" in ids
            assert "look_left" in ids and "look_right" in ids
            a._create_turn("你看右边")
            refused = await result_from_call(
                a, "unsupported-tool", "perform_social_action",
                {"script_id": "look_left"},
            )
            assert refused[0]["status"] == "rejected"
            assert (await rt.action_list("a"))["requests"] == []
        finally:
            await rt.close()

    asyncio.run(exercise())


def test_expression_tool_result_and_followup_do_not_wait_for_room_network(tmp_path):
    async def exercise():
        rt = runtime(tmp_path)
        await ready(rt)
        client = rt.realtime["a"]
        speaker = Publisher.instances["roomv2", "tts"]
        started, release = asyncio.Event(), asyncio.Event()
        original_publish = speaker.publish

        async def blocked_publish(packet):
            started.set()
            await release.wait()
            await original_publish(packet)

        speaker.publish = blocked_publish
        try:
            client._create_turn("你给我做个开心表情")
            output = await asyncio.wait_for(
                result_from_call(client, "slow-expression", "set_expression",
                                 {"expression": "喜"}),
                .5,
            )
            assert output[0]["status"] == "queued"
            assert output[0]["real_device_executed"] is False
            await asyncio.wait_for(started.wait(), .5)
            assert speaker.messages == []
            await asyncio.wait_for(
                client.handle({"type": "response.done", "response": {
                    "status": "completed",
                }}, client.robot.generation),
                .5,
            )
            assert client.socket.outbound[-1]["type"] == "response.create"
            assert (await rt.action_list("a"))["requests"][0]["status"] == "send_unknown"
            release.set()
            for _ in range(100):
                if (await rt.action_list("a"))["requests"][0]["status"] == "published_unknown":
                    break
                await asyncio.sleep(.01)
            assert (await rt.action_list("a"))["requests"][0]["status"] == "published_unknown"
            assert speaker.messages[0]["type"] == "expression"
        finally:
            release.set()
            await rt.close()

    asyncio.run(exercise())
