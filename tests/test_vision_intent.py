"""Look first, then answer (2026-10-28, docs/规范/视觉意图协议.md).

"你看看左边有什么": she turns at once (action_trigger offset_ms=-1), waits for the
embedded action.finished, asks the vision service on reb_main for a fresh view
(vision.intent), and only then lets Realtime answer.
"""

import asyncio

import pytest
from annie_adapters import RoomServerConfig
from annie_brain.decision_policy import asks_to_look
from annie_core import RobotConfig
from annie_runtime import HarborRuntime
from annie_runtime import realtime as realtime_module
from annie_runtime.vision_intent import VisionIntents

from tests.test_decision_model import EchoSocket
from tests.test_phase4b import FakeTts
from tests.test_phase7_execution import coordinator
from tests.test_phase7_runtime import ROOT, Publisher
from tests.test_wake_call_recovery import kinds, say, sent, woken


# ---- which questions make her look -------------------------------------------------
@pytest.mark.parametrize("text", [
    "你看看左边有什么",  # the user's own examples (10-28)
    "你看看周围。",
    "你看到了什么？",
    "你瞅瞅我拿的什么",
    "我手里拿的是什么？",
    "帮我看看这是什么",
    "你瞧瞧这是啥",
    "你能看到我吗？",
    "你看我穿的什么颜色",
    "你眼前有什么？",
    "小安妮，你看一下四周。",
    "上面有什么？",  # the 10-27 look questions still count
    "右边看。",
    "那你看看你的你的左手边。",
])
def test_questions_she_must_look_at_now(text):
    assert asks_to_look(text)


@pytest.mark.parametrize("text", [
    "你看这事怎么办",  # 你看 as a discourse marker
    "你看着办吧",
    "我看到一篇文章说",  # the user saw it
    "让我看看",
    "我看看周围",
    "看看天气怎么样",
    "你看我说的对不对",
    "你看过这部电影吗",
    "附近有什么好吃的",  # a place, but no look
    "周围有什么好玩的地方",
    "别看了",
    "你先别看左边",
    "左边看起来有点乱。",
    "看看你的左手。",
])
def test_other_talk_does_not(text):
    assert not asks_to_look(text)


def test_turn_your_head_counts_after_a_side_was_named():
    assert asks_to_look("那你现在转头看看。", ["那你看看你的你的左手边。"])
    assert not asks_to_look("那你现在转头看看。", ["你会数数吗？"])


# ---- the reb_main messages ----------------------------------------------------------
class Pub:
    def __init__(self, fail=False):
        self.messages, self.fail = [], fail

    async def publish(self, message):
        if self.fail:
            raise ConnectionError("room closed")
        self.messages.append(message)

    async def close(self):
        pass


def answer(intent, **data):
    return {"type": "vision.intent.result",
            "data": {"version": 1, "intent_id": intent["data"]["intent_id"],
                     "conversation_id": intent["data"]["conversation_id"], **data}}


async def until(check, seconds=3.0):
    for _ in range(int(seconds / .01)):
        if check():
            return
        await asyncio.sleep(.01)
    raise AssertionError("condition never became true")


def test_intent_carries_the_route_and_its_result_comes_back_by_id():
    async def run():
        pub = Pub()
        intents = VisionIntents({"a": pub}, route="cloud", deadline_ms=2000)
        task = asyncio.create_task(intents.ask(
            "a", "turn-1", "你看看左边有什么", fresh_frame=True,
            after_action={"script_id": "look_left", "receipt": "finished", "wait_ms": 1380}))
        await until(lambda: pub.messages)
        intent = pub.messages[0]
        data = intent["data"]
        assert intent["type"] == "vision.intent" and type(intent["ts"]) is int
        assert {key: data[key] for key in ("version", "conversation_id", "route", "question",
                                           "fresh_frame", "deadline_ms", "source")} == {
            "version": 1, "conversation_id": "turn-1", "route": "cloud",
            "question": "你看看左边有什么", "fresh_frame": True, "deadline_ms": 2000,
            "source": "robot_main_brain"}
        assert data["after_action"]["receipt"] == "finished"
        assert not intents.on_result("a", {"type": "vision.intent.result",
                                           "data": {"intent_id": "vi-other", "status": "ok",
                                                    "description": "别人的"}})
        assert intents.on_result("a", answer(intent, status="ok", route="cloud", model="m",
                                             description=" 左边有两盆绿植。 ", latency_ms=900,
                                             frame_after_request_ms=40))
        result = await task
        assert (result["status"], result["description"], result["route"],
                result["latency_ms"], result["frame_after_request_ms"]) == (
            "ok", "左边有两盆绿植。", "cloud", 900, 40)
        assert not intents.on_result("a", answer(intent, status="ok", description="迟到"))
        assert intents.snapshot("a")["stats"]["ok"] == 1
    asyncio.run(run())


def test_silence_failure_and_a_closed_room_never_hang():
    async def run():
        intents = VisionIntents({"a": Pub()}, route="local", deadline_ms=500)
        assert (await intents.ask("a", "t", "你看到了什么", fresh_frame=False))["status"] == "timeout"
        pub = intents.publishers["a"]
        task = asyncio.create_task(intents.ask("a", "t2", "你看到了什么", fresh_frame=False))
        await until(lambda: len(pub.messages) == 2)
        assert pub.messages[1]["data"]["route"] == "local"
        intents.on_result("a", answer(pub.messages[1], status="failed", error="no_frame"))
        assert (await task)["error"] == "no_frame"
        closed = VisionIntents({"a": Pub(fail=True)})
        assert (await closed.ask("a", "t3", "你看到了什么", fresh_frame=False))["status"] == "send_failed"
    asyncio.run(run())


def test_a_newer_question_replaces_the_one_still_waiting():
    async def run():
        pub = Pub()
        intents = VisionIntents({"a": pub}, deadline_ms=2000)
        first = asyncio.create_task(intents.ask("a", "t1", "你看到了什么", fresh_frame=False))
        await until(lambda: pub.messages)
        second = asyncio.create_task(intents.ask("a", "t2", "你看看周围", fresh_frame=False))
        assert (await first)["error"] == "superseded"
        await until(lambda: len(pub.messages) == 2)
        intents.on_result("a", answer(pub.messages[1], status="ok", description="一张桌子。"))
        assert (await second)["description"] == "一张桌子。"
    asyncio.run(run())


def test_receipt_watch_waits_for_finished_or_dropped_not_started(tmp_path):
    async def run():
        actions, _pubs, _events, _a, _b = coordinator(tmp_path)
        await actions.start()
        watch = actions.watch_receipt("A", "turn-1", "look_left")
        receipt = {"script_id": "look_left", "conversation_id": "turn-1"}
        actions.on_receipt("A", {"type": "action.started", "data": receipt})
        assert not watch.done()
        actions.on_receipt("A", {"type": "action.finished", "data": receipt})
        assert watch.result() == "finished"
        actions.unwatch_receipt("A", "turn-1", "look_left", watch)
        dropped = actions.watch_receipt("A", "turn-2", "look_left")
        actions.on_receipt("A", {"type": "action.dropped",
                                 "data": {**receipt, "conversation_id": "turn-2",
                                          "reason": "cooldown"}})
        assert dropped.result() == "dropped"
        await actions.close()
    asyncio.run(run())


# ---- the whole turn -----------------------------------------------------------------
def room_server():
    """The fake Room of tests.test_phase7_runtime plus a camera feed (visualllm)."""
    root = "ws://127.0.0.1:1/robot"
    channels = {name: f"reb_{name}" for name in
                ("main", "tts", "mic", "visualdata", "vision", "sam", "visualllm")}
    return RoomServerConfig(
        base_url=root + "/roomv2", room_root=root, channels=channels,
        default_urls={name: root + "/roomv2/" + path for name, path in channels.items()},
        connect_timeout_sec=.05, reconnect_min_sec=.05, reconnect_max_sec=.05)


def runtime(tmp_path, **options):
    return HarborRuntime(
        [RobotConfig("a", room_id="roomv2")], tmp_path / "vision.db", room_server(),
        realtime_key="fake", realtime_socket_factory=EchoSocket, tts_voice="fake-voice",
        tts_socket_factory=FakeTts, room_tts_factory=Publisher, room_action_factory=Publisher,
        action_catalog=ROOT / "config/action_scripts.json", actions_enabled=True,
        action_allowlist=("look_left", "look_right", "look_up", "agree_micro_nod"),
        **{"vision_intent_enabled": True, "vision_intent_deadline_ms": 600, **options})


def scenario(tmp_path, body, **options):
    async def run():
        rt = runtime(tmp_path, **options)
        await rt.start()
        client = rt.realtime["a"]
        await until(lambda: client.ready)
        try:
            await body(rt, client)
        finally:
            await rt.close()
    asyncio.run(run())


async def ask_held(client, item, text):
    """She was just woken; smart_turn answers before the final ASR (the reply is held)."""
    woken(client)
    g = client.robot.generation
    await client.handle({"type": "input_audio_buffer.speech_started", "item_id": item}, g)
    await client.handle({"type": "input_audio_buffer.speech_stopped", "item_id": item}, g)
    await client.handle({"type": "response.created", "response": {"id": "r-early"}}, g)
    await client.handle({"type": "response.text.delta", "response_id": "r-early",
                         "delta": "左边好像是"}, g)
    await client.handle({"type": "conversation.item.input_audio_transcription.completed",
                         "item_id": item, "transcript": text}, g)


def triggers(rt):
    return [m for m in rt.actions.main_publishers["a"].messages if m["type"] == "action_trigger"]


def intents(rt):
    return rt.vision_intents.publishers["a"].messages


def test_she_turns_first_then_looks_then_answers(tmp_path):
    async def body(rt, client):
        assert rt.health()["vision_intent"] == {"configured_robots": ["a"], "route": "cloud"}
        await ask_held(client, "u1", "你看看左边有什么")
        turn_id = client.turn_id
        await until(lambda: triggers(rt))
        trigger = triggers(rt)[0]["data"]
        assert (trigger["script_id"], trigger["offset_ms"], trigger["conversation_id"]) == (
            "look_left", -1, turn_id)  # at once, not with her first audio
        cancels = sent(client, "response.cancel")
        assert len(cancels) == 1  # her early, unseeing reply
        assert kinds(client, "text.delta") == []  # nothing of it reached TTS
        await asyncio.sleep(.05)
        assert intents(rt) == [] and sent(client, "response.create") == []
        rt._room_playback_receipt("a", {"type": "action.finished", "data": {
            "script_id": "look_left", "conversation_id": turn_id}})
        await until(lambda: intents(rt))
        intent = intents(rt)[0]
        data = intent["data"]
        assert (data["conversation_id"], data["route"], data["fresh_frame"], data["question"]) == (
            turn_id, "cloud", True, "你看看左边有什么")
        assert (data["after_action"]["script_id"], data["after_action"]["receipt"]) == (
            "look_left", "finished")
        rt._room_playback_receipt("a", answer(intent, status="ok", route="cloud", model="m",
                                              description="左边是一面白墙，墙边有两盆绿植。",
                                              latency_ms=900))
        await until(lambda: sent(client, "response.create"))
        outbound = client.socket.outbound
        create = outbound.index(sent(client, "response.create")[0])
        fresh = [i for i, event in enumerate(outbound) if event["type"] == "session.update"
                 and "两盆绿植" in event["session"].get("instructions", "")]
        assert fresh and fresh[-1] < create  # the fresh view is in the prompt first
        assert "转向左边后刚拍的" in outbound[fresh[-1]]["session"]["instructions"]
        assert client.turn_id == turn_id and len(sent(client, "response.create")) == 1
        done = kinds(client, "vision.intent.result")[-1]
        assert (done["status"], done["receipt"], done["route"]) == ("ok", "finished", "cloud")
        assert len(triggers(rt)) == 1  # the reply does not turn her a second time
    scenario(tmp_path, body)


def test_a_question_without_a_look_action_uses_the_latest_frame(tmp_path):
    async def body(rt, client):
        await ask_held(client, "u2", "你瞅瞅我拿的什么")
        await until(lambda: intents(rt))
        data = intents(rt)[0]["data"]
        assert (data["fresh_frame"], data["after_action"]) == (False, None)
        assert triggers(rt) == []
        rt._room_playback_receipt("a", answer(intents(rt)[0], status="ok",
                                              description="右手拿着一个白色杯子。"))
        await until(lambda: sent(client, "response.create"))
    scenario(tmp_path, body)


def test_a_silent_vision_service_only_delays_the_answer(tmp_path):
    async def body(rt, client):
        await ask_held(client, "u3", "你看到了什么？")
        await until(lambda: intents(rt))
        assert sent(client, "response.create") == []
        await until(lambda: sent(client, "response.create"), seconds=3)  # 600 ms + margin
        assert kinds(client, "vision.intent.result")[-1]["status"] == "timeout"
        assert "刚为用户这一问看到的画面" not in client._combined_instructions(None)
    scenario(tmp_path, body)


def test_a_dropped_look_still_gets_a_fresh_look_and_an_answer(tmp_path):
    async def body(rt, client):
        await ask_held(client, "u4", "你看看上面有什么")
        turn_id = client.turn_id
        await until(lambda: triggers(rt))
        rt._room_playback_receipt("a", {"type": "action.dropped", "data": {
            "script_id": "look_up", "conversation_id": turn_id, "reason": "cooldown"}})
        await until(lambda: intents(rt))
        assert intents(rt)[0]["data"]["after_action"]["receipt"] == "dropped"
        rt._room_playback_receipt("a", answer(intents(rt)[0], status="ok",
                                              description="一盏吸顶灯。"))
        await until(lambda: sent(client, "response.create"))
        # She did not get there: the prompt must not say she looked up.
        assert "抬头往上看时" not in client._combined_instructions(None)
    scenario(tmp_path, body)


def test_a_motion_still_moving_only_delays_the_look(tmp_path):
    async def body(rt, client):
        # She nodded while the user spoke; the device has not reported it done yet.
        await rt.actions.perform_now("a", "listen-1", "listen-1", client.robot.generation,
                                     "agree_micro_nod")
        await ask_held(client, "u9", "你看看左边有什么")
        await asyncio.sleep(.2)
        assert [t["data"]["script_id"] for t in triggers(rt)] == ["agree_micro_nod"]
        rt._room_playback_receipt("a", {"type": "action.finished", "data": {
            "script_id": "agree_micro_nod", "conversation_id": "listen-1"}})
        await until(lambda: len(triggers(rt)) == 2)
        assert triggers(rt)[1]["data"]["script_id"] == "look_left"
        assert kinds(client, "vision.intent.look_skipped") == []
    scenario(tmp_path, body)


def test_new_speech_while_she_looks_replaces_the_question(tmp_path):
    async def body(rt, client):
        await ask_held(client, "u5", "你看看左边有什么")
        turn_id = client.turn_id
        await until(lambda: triggers(rt))
        g = client.robot.generation
        await client.handle({"type": "input_audio_buffer.speech_started", "item_id": "u6"}, g)
        await client.handle({"type": "conversation.item.input_audio_transcription.delta",
                             "item_id": "u6", "text": "今天天气怎么样", "stash": ""}, g)
        assert client.turn_id != turn_id
        assert kinds(client, "vision.intent.abandoned")[-1]["turn_id"] == turn_id
        rt._room_playback_receipt("a", {"type": "action.finished", "data": {
            "script_id": "look_left", "conversation_id": turn_id}})
        await asyncio.sleep(.2)
        assert intents(rt) == [] and sent(client, "response.create") == []
        assert client._vision_hold is None
    scenario(tmp_path, body)


def test_a_call_smart_turn_dropped_is_looked_at_and_asked_again(tmp_path, monkeypatch):
    monkeypatch.setattr(realtime_module, "RECOVERY_SETTLE_SECONDS", 0.05)

    async def body(rt, client):
        woken(client)
        await say(client, "u7", draft="你看到了什么", invalid=True)
        await say(client, "u7", final="你看到了什么？", start=False)
        await until(lambda: intents(rt))
        assert sent(client, "response.create") == []
        rt._room_playback_receipt("a", answer(intents(rt)[0], status="ok",
                                              description="一位朋友坐在电脑前。"))
        await until(lambda: sent(client, "response.create"))
        asked = [event["item"]["content"][0]["text"]
                 for event in sent(client, "conversation.item.create")
                 if event["item"].get("role") == "user"]
        assert asked == ["你看到了什么？"]  # its audio item was not kept: ask in text
    scenario(tmp_path, body)


def test_switched_off_she_answers_as_before(tmp_path):
    async def body(rt, client):
        assert rt.vision_intents is None and client.vision_intents is None
        await ask_held(client, "u8", "你看看左边有什么")
        assert sent(client, "response.cancel") == []  # the early reply is released
        assert kinds(client, "vision.intent.started") == []
    scenario(tmp_path, body, vision_intent_enabled=False)


def test_the_fresh_view_leaves_the_prompt_after_a_while(tmp_path, monkeypatch):
    async def body(rt, client):
        client._vision_fresh = {"text": "左边有两盆绿植。", "script": "look_left",
                                "at": realtime_module.time.monotonic()}
        assert "左边有两盆绿植。" in client._combined_instructions(None)
        monkeypatch.setattr(realtime_module, "FRESH_VIEW_SECONDS", 0)
        await asyncio.sleep(.01)
        assert "左边有两盆绿植。" not in client._combined_instructions(None)
    scenario(tmp_path, body)
