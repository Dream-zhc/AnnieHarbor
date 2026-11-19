"""Vision questions (11-19): a short line of her own at once, then the answer from the view.

The look took 4-6 s of silence after smart_turn (head turn ~2.1 s, cloud view ~2 s);
a third of the answers then called look_closely again and got the older 1 Hz view.
"""

import asyncio

from annie_runtime import realtime as realtime_module
from annie_runtime.realtime import LOOK_CLOSELY_RULE, visual_instructions

from tests.test_live_issues_1020 import live
from tests.test_vision_intent import answer, ask_held, intents, scenario, triggers, until
from tests.test_wake_call_recovery import kinds, say, sent, woken


def updates(client):
    return [event["session"].get("instructions", "") for event in sent(client, "session.update")]


async def early_reply_gone(client):
    await client.handle({"type": "response.done", "response": {
        "id": "r-early", "status": "cancelled", "output": []}}, client.robot.generation)


async def reply(client, response_id, text):
    g = client.robot.generation
    await client.handle({"type": "response.created", "response": {"id": response_id}}, g)
    await client.handle({"type": "response.text.delta", "response_id": response_id,
                         "delta": text}, g)
    await client.handle({"type": "response.text.done", "response_id": response_id,
                         "text": text}, g)
    await client.handle({"type": "response.done", "response": {
        "id": response_id, "status": "completed", "output": []}}, g)


def test_she_says_a_short_line_at_once_then_answers_from_the_view(tmp_path):
    async def body(rt, client):
        await ask_held(client, "u1", "你看看左边有什么")
        turn_id = client.turn_id
        await until(lambda: triggers(rt))
        await early_reply_gone(client)
        await until(lambda: sent(client, "response.create"))
        noted = [text for text in updates(client) if "【先应一声】" in text]
        assert noted and "正转头往左边看" in noted[-1] and "你看看左边有什么" in noted[-1]
        assert intents(rt) == []
        await reply(client, "r-ack", "我瞅瞅哈。")
        assert kinds(client, "vision.ack.done")[-1]["status"] == "spoken"
        assert client.turn_id == turn_id and kinds(client, "turn.done") == []
        assert [e["delta"] for e in kinds(client, "text.delta")] == ["我瞅瞅哈。"]
        rt._room_playback_receipt("a", {"type": "action.finished", "data": {
            "script_id": "look_left", "conversation_id": turn_id}})
        await until(lambda: intents(rt))
        rt._room_playback_receipt("a", answer(intents(rt)[0], status="ok", route="cloud",
                                              model="m", description="左边是一面白墙，墙边有两盆绿植。",
                                              latency_ms=900))
        await until(lambda: len(sent(client, "response.create")) == 2)
        last = updates(client)[-1]
        assert "两盆绿植" in last and "【先应一声】" not in last
        assert "刚才已经应过一声" in last and "我瞅瞅哈" in last
        await reply(client, "r-answer", "左边就一面白墙，配了两盆绿植。")
        assert kinds(client, "turn.done")[-1]["status"] == "completed"
        assert "look_acked" not in client._once_notes and "look_ack" not in client._once_notes
    scenario(tmp_path, body, vision_intent_ack=True)


def test_no_line_while_another_reply_is_still_going(tmp_path, monkeypatch):
    monkeypatch.setattr(realtime_module, "VISION_ACK_WAIT_SECONDS", .05)

    async def body(rt, client):
        await ask_held(client, "u2", "你瞅瞅我拿的什么")
        client._provider_active_response_id = "r-other"
        await until(lambda: intents(rt))
        await asyncio.sleep(.15)
        assert sent(client, "response.create") == []
        rt._room_playback_receipt("a", answer(intents(rt)[0], status="ok",
                                              description="右手拿着一个白色杯子。"))
        await until(lambda: sent(client, "response.create"))
        assert "【先应一声】" not in updates(client)[-1]
        assert kinds(client, "vision.ack.done") == []
    scenario(tmp_path, body, vision_intent_ack=True)


def test_speaking_over_her_line_drops_the_look_and_its_note(tmp_path):
    async def body(rt, client):
        await ask_held(client, "u3", "你看看左边有什么")
        turn_id = client.turn_id
        await early_reply_gone(client)
        await until(lambda: sent(client, "response.create"))
        g = client.robot.generation
        await client.handle({"type": "response.created", "response": {"id": "r-ack"}}, g)
        await client.handle({"type": "response.text.delta", "response_id": "r-ack",
                             "delta": "我看看"}, g)
        await client.handle({"type": "input_audio_buffer.speech_started", "item_id": "u4"}, g)
        await client.handle({"type": "conversation.item.input_audio_transcription.delta",
                             "item_id": "u4", "text": "算了不用看了", "stash": ""}, g)
        assert client.turn_id != turn_id and client._vision_hold is None
        await until(lambda: "【先应一声】" not in updates(client)[-1])
        assert "look_ack" not in client._once_notes
    scenario(tmp_path, body, vision_intent_ack=True)


def test_a_dropped_call_gets_its_line_and_is_asked_only_once(tmp_path, monkeypatch):
    monkeypatch.setattr(realtime_module, "RECOVERY_SETTLE_SECONDS", 0.05)

    async def body(rt, client):
        woken(client)
        await say(client, "u7", draft="你看到了什么", invalid=True)
        await say(client, "u7", final="你看到了什么？", start=False)
        await until(lambda: sent(client, "response.create"))
        await reply(client, "r-ack", "等我瞧一眼。")
        await until(lambda: intents(rt))
        rt._room_playback_receipt("a", answer(intents(rt)[0], status="ok",
                                              description="一位朋友坐在电脑前。"))
        await until(lambda: len(sent(client, "response.create")) == 2)
        asked = [event["item"]["content"][0]["text"]
                 for event in sent(client, "conversation.item.create")
                 if event["item"].get("role") == "user"]
        assert asked == ["你看到了什么？"]
    scenario(tmp_path, body, vision_intent_ack=True)


def test_with_a_fresh_view_she_neither_looks_again_nor_lists_everything(tmp_path):
    scene = "【眼前画面】你正看着一位朋友。"
    assert LOOK_CLOSELY_RULE in visual_instructions("base", scene)
    assert LOOK_CLOSELY_RULE not in visual_instructions("base", scene, fresh=True)

    async def body(_rt, client):
        client.visual_details = lambda: {"seconds_ago": .5, "people": [], "scene": "一台显示器",
                                         "around": "", "objects": []}
        client._create_turn("你看看左边有什么")
        client._vision_fresh = {"text": "左边是一扇木门。", "script": "look_left",
                                "at": realtime_module.time.monotonic()}
        prompt = client._combined_instructions(scene)
        assert LOOK_CLOSELY_RULE not in prompt and "不要再调用look_closely" in prompt
        assert "最显眼的一两样" in prompt and "不要一样样列出来" in prompt
        await client._tool_call({"call_id": "l1", "name": "look_closely", "arguments": "{}"})
        result = next(e for e in client.events if e["kind"] == "tool.result"
                      and e["call_id"] == "l1")["result"]
        assert result["fresh_view"] == "左边是一扇木门。" and "以 fresh_view 为准" in result["instruction"]
        assert result["scene"] == "一台显示器"
    live(tmp_path, body)
