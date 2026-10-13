"""Phase 8A event facts and actual Realtime submission (fake provider, no device)."""

import asyncio
import json
from types import SimpleNamespace

import httpx
from annie_brain.event_policy import EventPolicy
from annie_core import NormalizedEvent, RobotConfig
from annie_runtime import HarborRuntime
from annie_web import create_app

from tests.test_phase5 import FakeSocket


def touch(event_id="touch-1", *, sensor="head", level=1):
    return {"type": "robot_event", "data": {"event_type": "sensor.touch",
            "event_id": event_id, "source": "claimed-identity",
            "ttl_ms": 7000, "payload": {"id": sensor, "level": level}}}


def test_policy_context_dedupe_and_expiry():
    policy = EventPolicy()
    identity = {"chat_id": "x", "generation": 1}
    now = 1_000_000
    pressure = NormalizedEvent("sensor.pressure", {"level": 4}, ttl_ms=500)
    first = policy.route(pressure, robot_id="a", transport="verified:a",
                         identity=identity, now_ms=now)
    assert first["decision"] == "context_only"
    assert policy.snapshot(now)["facts"][0]["summary"]["level"] == 4
    assert not policy.snapshot(now + 501)["facts"]
    head = NormalizedEvent("sensor.touch", {"id": "head", "level": 1},
                           event_id="same", ttl_ms=1000)
    assert policy.route(head, robot_id="a", transport="verified:a",
                        identity=identity, now_ms=now)["decision"] == "need_response"
    repeat = policy.route(head, robot_id="a", transport="verified:a",
                          identity=identity, now_ms=now)
    assert repeat["reason"] == "duplicate_event_id"
    assert policy.route(NormalizedEvent("sensor.touch", {"id": "head", "level": 1},
                                        event_id="other"), robot_id="a", transport="verified:a",
                        identity=identity, now_ms=now)["reason"] == "cooldown"
    assert policy.route(NormalizedEvent("sensor.touch", {"id": "arm", "level": 1}),
                        robot_id="a", transport="verified:a", identity=identity,
                        now_ms=now)["decision"] == "context_only"
    assert policy.route(NormalizedEvent("sensor.touch", {"id": "head", "level": 1},
                                        ts_ms=now-100, ttl_ms=50),
                        robot_id="a", transport="verified:a", identity=identity,
                        now_ms=now)["reason"] == "stale_or_invalid_time"
    assert len(policy.snapshot(now)["events"]) == 6


def test_event_realtime_isolation_and_waiting(tmp_path):
    async def exercise():
        FakeSocket.instances.clear()
        rt = HarborRuntime(
            [RobotConfig("a", room_id="roomv2"), RobotConfig("b", room_id="roomv3")],
            tmp_path / "rt.db", realtime_key="fake", realtime_socket_factory=FakeSocket,
        )
        rt.bind_input_source("room:roomv2:main-event", ["a"])
        rt.bind_input_source("room:roomv3:main-event", ["b"])
        await rt.start()
        try:
            for _ in range(100):
                if all(client.ready for client in rt.realtime.values()):
                    break
                await asyncio.sleep(.01)
            assert all(client.ready for client in rt.realtime.values())
            a, b = rt.realtime["a"], rt.realtime["b"]
            a.speech_active = True
            rt.dispatch_room_event("room:roomv2:main-event", touch())
            await asyncio.sleep(.02)
            rows = rt.external_events("a")["events"]
            assert rows[-1]["decision"] == "need_response"
            assert rows[-1]["status"] == "waiting"
            assert rows[-1]["reason"] == "model_busy"
            assert not any(x["type"] == "response.create" for x in a.socket.outbound)
            assert not rt.external_events("b")["events"]
            a.speech_active = False
            a.publish("speech.stopped")
            for _ in range(100):
                if rows[-1]["status"] == "submitted_unknown":
                    break
                await asyncio.sleep(.01)
            assert rows[-1]["status"] == "submitted_unknown"
            assert rows[-1]["turn_id"] == a.turn_id
            assert sum(x["type"] == "response.create" for x in a.socket.outbound) == 1
            assert not any(x["type"] == "response.create" for x in b.socket.outbound)
            assert a.robot.cached_turn(a.turn_id)["user_text"] == ""
            assert a.robot.cached_turn(a.turn_id)["turn_source"] == "external"
            assert "非用户发言" in a.robot.cached_turn(a.turn_id)["external_prompt"]
            async def never_dispatch(*args, **kwargs):
                raise AssertionError("external event reached the motor")
            a.actions = SimpleNamespace(enabled=True, request=never_dispatch)
            await a._tool_call({"call_id": "external-motion", "name": "perform_action",
                                "arguments": json.dumps({"script_id": "agree_micro_nod"})})
            output = next(x for x in a.socket.outbound if x["type"] == "conversation.item.create"
                          and x["item"].get("call_id") == "external-motion")
            assert json.loads(output["item"]["output"])["reason"] == "external_event_no_motion"
            await a._tool_call({"call_id": "external-weather", "name": "get_weather",
                                "arguments": json.dumps({"city": "西安"})})
            weather_result = next(x for x in a.socket.outbound
                                  if x.get("item", {}).get("call_id") == "external-weather")
            assert json.loads(weather_result["item"]["output"])["reason"] == (
                "external_event_no_tools")
            a.actions = None
            rt.dispatch_room_event("room:roomv2:main-event", touch())
            await asyncio.sleep(.02)
            assert sum(x["type"] == "response.create" for x in a.socket.outbound) == 1
            assert rt.external_events("a")["events"][-1]["reason"] == "duplicate_event_id"
            # A claimed target in payload never overrides the verified Room binding.
            try:
                rt.dispatch_room_event("room:roomv2:main-event", touch("2"), target_robot_id="b")
            except Exception as exc:
                assert str(exc) == "target_not_bound"
            else:
                raise AssertionError("cross-room event accepted")
            app = create_app(rt, tmp_path)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/api/robots/a/external-events")
                assert response.status_code == 200
                assert response.json()["events"][-1]["reason"] == "duplicate_event_id"
                assert (await client.get("/api/robots/unknown/external-events")).status_code == 404
        finally:
            await rt.close()

    asyncio.run(exercise())


def test_radar_too_close_replies_once_then_cools_down(tmp_path):
    packet = {
        "type": "pressure", "sensor_type": "pressure", "id": 5,
        "name": "距离过近", "distance": 148.0,
        "from": "anonymous-04ad4b04", "room": "main",
    }

    async def exercise():
        FakeSocket.instances.clear()
        rt = HarborRuntime(
            [RobotConfig("a", room_id="roomv2")],
            tmp_path / "radar.db", realtime_key="fake",
            realtime_socket_factory=FakeSocket,
        )
        rt.bind_input_source("room:roomv2:main-event", ["a"])
        await rt.start()
        try:
            for _ in range(100):
                if rt.realtime["a"].ready:
                    break
                await asyncio.sleep(.01)
            assert rt.realtime["a"].ready
            rt.dispatch_room_event("room:roomv2:main-event", packet)
            rows = rt.external_events("a")["events"]
            for _ in range(100):
                if rows[-1]["status"] == "submitted_unknown":
                    break
                await asyncio.sleep(.01)
            assert rows[-1]["kind"] == "sensor.radar"
            assert rows[-1]["decision"] == "need_response"
            assert rows[-1]["status"] == "submitted_unknown"
            assert rows[-1]["summary"]["distance"] == 148.0
            client = rt.realtime["a"]
            assert sum(item["type"] == "response.create" for item in client.socket.outbound) == 1
            prompt = client.robot.cached_turn(client.turn_id)["external_prompt"]
            assert "靠得很近" in prompt
            assert "当前的性格" in prompt
            assert "正在进行的对话" in prompt
            assert "固定套话" in prompt
            assert "请对方稍微退开" not in prompt
            assert "非用户发言" in prompt
            rt.dispatch_room_event("room:roomv2:main-event", packet)
            await asyncio.sleep(.02)
            assert rt.external_events("a")["events"][-1]["reason"] == "cooldown"
            assert sum(item["type"] == "response.create" for item in client.socket.outbound) == 1
        finally:
            await rt.close()

    asyncio.run(exercise())


def test_policy_bounded_and_no_raw_vision():
    policy = EventPolicy()
    for i in range(90):
        policy.route(NormalizedEvent("vision.face", {"id": str(i),
            "tracks": [{"image": "secret", "expression": "中性"}]}),
            robot_id="a", transport="visualdata", identity={}, now_ms=1000)
    data = policy.snapshot(1000)
    assert len(data["events"]) == 80
    assert len(data["facts"]) == 64
    assert "secret" not in str(data)
