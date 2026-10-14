"""Chat preferences changed by talking: closed enums, explicit requests, live prompt."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from annie_adapters.realtime import session_update
from annie_brain.persona import REPLY_LENGTH_HINTS, load_personas, session_instructions

from tests.test_phase7_runtime import ready, result_from_call, runtime

ROOT = Path(__file__).resolve().parents[1]
PROFILES = load_personas(ROOT / "config/personas.toml")


async def _echo_updates(client, stop):
    seen = 0
    while not stop.is_set():
        outbound = list(client.socket.outbound)
        for event in outbound[seen:]:
            if event.get("type") == "session.update" and event.get("session", {}).get("instructions"):
                await client.socket.inbound.put({"type": "session.updated", "session": {
                    "id": client.session, "modalities": ["text"],
                    "turn_detection": {"type": "smart_turn"},
                    "instructions": event["session"]["instructions"]}})
        seen = len(outbound)
        await asyncio.sleep(0.01)


def run_live(tmp_path, body, weather_handler=None):
    async def exercise():
        http = (httpx.AsyncClient(transport=httpx.MockTransport(weather_handler))
                if weather_handler else None)
        rt = runtime(tmp_path, weather_client=http, persona_profiles=PROFILES)
        await ready(rt)
        client = rt.realtime["a"]
        stop = asyncio.Event()
        echo = asyncio.create_task(_echo_updates(client, stop))
        try:
            await body(rt, client)
        finally:
            stop.set()
            await echo
            await rt.close()
            if http is not None:
                await http.aclose()
    asyncio.run(exercise())


async def say(client, text, call_id, name, args):
    if client.turn_id:
        client._finish("completed")
    client._create_turn(text)
    return (await result_from_call(client, call_id, name, args))[0]


def test_tool_lists_every_setting_as_a_closed_enum():
    tool = next(t["function"] for t in session_update("x", preferences=True)["session"]["tools"]
                if t["function"]["name"] == "update_chat_preference")
    assert set(tool["parameters"]["properties"]["setting"]["enum"]) == {
        "question_pressure", "humor_level", "reply_length", "speech_rate",
        "speech_volume", "spontaneous_motions", "home_city"}
    assert "update_chat_preference" not in json.dumps(session_update("x")["session"]["tools"])


def test_reply_length_and_motion_preferences_shape_the_prompt():
    social = {"reply_length": {"value": "short", "source": "user_setting"},
              "spontaneous_motions": {"value": "off", "source": "user_setting"}}
    text, snapshot = session_instructions("annie", PROFILES, social, physical_actions=True)
    assert REPLY_LENGTH_HINTS["short"] in text
    assert "用户关闭了自发表情动作" in text
    assert snapshot["preferences"] == {"reply_length": "short", "spontaneous_motions": "off"}
    observed = {"reply_length": {"value": "short", "source": "llm_observation"}}
    assert REPLY_LENGTH_HINTS["short"] not in session_instructions("annie", PROFILES, observed)[0]


def test_spoken_preferences_apply_live_and_need_an_explicit_request(tmp_path):
    async def body(rt, client):
        result = await say(client, "以后回答简短一点", "len", "update_chat_preference",
                           {"setting": "reply_length", "value": "short"})
        assert result["status"] == "set"
        assert REPLY_LENGTH_HINTS["short"] in client._persona_text
        assert any(REPLY_LENGTH_HINTS["short"] in e.get("session", {}).get("instructions", "")
                   for e in client.socket.outbound if e["type"] == "session.update")
        assert (await rt.social_state("a"))["reply_length"]["value"] == "short"
        again = await say(client, "回答简短点就行", "len2", "update_chat_preference",
                          {"setting": "reply_length", "value": "short"})
        assert again["status"] == "unchanged"
        unasked = await say(client, "今天天气怎么样", "humor", "update_chat_preference",
                            {"setting": "humor_level", "value": "high"})
        assert unasked["status"] == "rejected"
        bad = await say(client, "多开点玩笑", "bad", "update_chat_preference",
                        {"setting": "humor_level", "value": "extreme"})
        assert bad["status"] == "rejected"
        slow = await say(client, "说慢一点", "rate", "update_chat_preference",
                         {"setting": "speech_rate", "value": "slower"})
        assert slow["status"] == "set"
        style = client.turn_speech_style(client.turn_id, client.robot.session_state())
        assert style.rate == pytest.approx(.92)
    run_live(tmp_path, body)


def test_motions_off_blocks_unprompted_expressions_but_not_explicit_ones(tmp_path):
    async def body(rt, client):
        off = await say(client, "别老做表情动作了", "off", "update_chat_preference",
                        {"setting": "spontaneous_motions", "value": "off"})
        assert off["status"] == "set"
        client._finish("completed")
        client._create_turn("我中了五百万")
        client.text = "五百万！恭喜你。"
        await client._tool_call({"call_id": "auto", "name": "perform_action",
                                 "arguments": json.dumps({"script_id": "surprise",
                                                          "anchor_text": "五百万"})})
        auto = next(e for e in client.events if e["kind"] == "tool.result" and e["call_id"] == "auto")
        assert auto["result"]["status"] == "rejected"
        client._finish("completed")
        client._create_turn("做个惊讶的表情")
        client.text = "好，惊讶一下。"
        await client._tool_call({"call_id": "asked", "name": "perform_action",
                                 "arguments": json.dumps({"script_id": "surprise",
                                                          "anchor_text": "惊讶"})})
        asked = next(e for e in client.events if e["kind"] == "tool.result" and e["call_id"] == "asked")
        assert asked["result"]["status"] != "rejected"
    run_live(tmp_path, body)


def test_home_city_answers_weather_without_a_named_city(tmp_path):
    def handler(request):
        assert request.url.params["city"] == "上海市"
        return httpx.Response(200, json={"status": "1", "lives": [{
            "city": "上海市", "adcode": "310000", "weather": "小雨",
            "temperature": "20", "reporttime": "2026-10-14 10:00:00"}]})

    async def body(rt, client):
        city = await say(client, "我在上海", "city", "update_chat_preference",
                         {"setting": "home_city", "value": "上海"})
        assert city["status"] == "set"
        weather = await say(client, "今天天气怎么样", "w", "get_weather", {})
        assert weather["city"] == "上海市" and weather["default_city"] is True
        named = await say(client, "今天天气怎么样", "w2", "get_weather", {"city": "上海"})
        assert named["status"] == "ok"  # the model may spell out the user's own home city
        other = await say(client, "今天天气怎么样", "w3", "get_weather", {"city": "广州"})
        assert other["status"] == "rejected"

    run_live(tmp_path, body, handler)
