"""Unified per-turn intent batching, slimmed language fast-path, and serious safety valve."""

import asyncio
import json

import pytest
from annie_adapters.decision import D3_MODEL
from annie_brain.decision_policy import SERIOUS_CONTEXT_QUESTION, serious_cue
from annie_core import RobotConfig
from annie_runtime import HarborRuntime
from annie_runtime.decision_route import D3Route
from annie_runtime.language import (
    ReplyLanguageState,
    explicit_language_request,
    unplaced_language_mention,
)

from tests.test_decision_model import EchoSocket, FakeDecision
from tests.test_face_registration import FakeRegistry
from tests.test_vision_intent import ask_held, intents, scenario, until
from tests.test_vision_look_decision import Frames, SeeingD3
from tests.test_wake_call_recovery import say, woken

IDENTITY = {
    "robot_id": "a", "room_id": "r", "chat_id": "c", "persona_id": "p",
    "generation": 1, "session_epoch": 1,
}


class KeyedD3(SeeingD3):
    """d3 fake answering each batched question by its key."""

    def __init__(self, responses, **kwargs):
        super().__init__(**kwargs)
        self._responses = dict(responses)

    async def ask(self, state, questions, *, images=None):
        self.images.append(images)
        self.calls.append((state, questions))
        return {key: self._responses[key] for key in questions}


def test_long_narratives_and_unrelated_quotes_route_to_decision_model():
    narrative = (
        "来的时候，我们有一个非常核心的能力，它叫同声传译。"
        "比如说我在说汉语，小安你退一下，变成各种语言推过去啊。"
    )
    assert explicit_language_request(narrative) is None
    assert unplaced_language_mention(narrative) == ("zh", "unknown")

    quoted_unrelated = (
        "国家刚出的法律，对于那个叫“陪陪陪护”都定了一定的标准。"
        "对，小安妮，你用四川话介绍一下自己。"
    )
    assert explicit_language_request(quoted_unrelated) is None
    assert unplaced_language_mention(quoted_unrelated) == ("zh", "sichuan")

    # Quotes actually wrapping the language name still stay blocked.
    for quoted_lang in ("请翻译‘以后你都用英语’", "别翻译“说粤语”", '他说"用四川话"'):
        assert explicit_language_request(quoted_lang) is None
        assert unplaced_language_mention(quoted_lang) is None


def test_one_shot_select_reply_style_clears_after_turn_finished():
    s = ReplyLanguageState()
    s.reset(IDENTITY)
    s.user_request("你能用陕西话夸一夸我吗？")
    s.reply_intent(language="zh", dialect="shaanxi", emotion="playful",
                   transcript="你能用陕西话夸一夸我吗？", confirmed=True)
    assert s.selection()["dialect"] == "shaanxi"
    s.response_started()
    s.first_text("t1")
    s.response_finished()
    assert s.intent is None
    assert s.selection()["dialect"] == "unknown"


def test_sleep_look_and_language_batch_into_one_decision_call(tmp_path):
    """When sleep + look + language all qualify on one turn, only ONE ask() call is issued."""
    d3 = KeyedD3({
        "sleep": {"choice": "no", "confidence": 0.9},
        "look": {"choice": "front", "confidence": 0.92},
        "language": {"choice": "from_now", "confidence": 0.91},
    })
    route = D3Route(d3, model=D3_MODEL)
    route._mark(True, None)

    async def body(rt, client):
        await ask_held(client, "u-batch-1", "先停一下看看前面，整点河南话")
        await until(lambda: intents(rt))
        # All three questions went out in a SINGLE HTTP request!
        assert len(d3.calls) == 1
        state, questions = d3.calls[0]
        assert set(questions) == {"sleep", "look", "language"}
        assert state["当前这句话"] == "先停一下看看前面，整点河南话"
        assert d3.images and d3.images[0] is not None
        await until(lambda: client.language.preferences["chat"])
        assert rt.store.load_language_preferences("a", client.robot.chat_id)["chat"]["dialect"] == "henan"
        # Re-checking select_reply_style reuses the seeded decision with 0 extra ask() calls.
        await client._tool_call({
            "call_id": "lang-reuse",
            "name": "select_reply_style",
            "arguments": json.dumps({"language": "zh", "dialect": "henan", "emotion": "playful"}),
        })
        assert len(d3.calls) == 1

    scenario(tmp_path, body, d3_route=route, camera_frames={"a": Frames()},
             vision_look_decision={"enabled": True})


def test_sleep_winning_in_batch_skips_look_and_language_side_effects(tmp_path):
    d3 = KeyedD3({
        "sleep": {"choice": "yes", "confidence": 0.95},
        "look": {"choice": "front", "confidence": 0.9},
        "language": {"choice": "from_now", "confidence": 0.9},
    })
    route = D3Route(d3, model=D3_MODEL)
    route._mark(True, None)

    async def body(rt, client):
        woken(client)
        await say(client, "u-sleep-win", final="先停一下看看前面，整点河南话")
        assert client.wake.state == "SLEEP"
        assert len(d3.calls) == 1
        assert set(d3.calls[0][1]) == {"sleep", "look", "language"}
        assert intents(rt) == []
        assert rt.store.load_language_preferences("a", client.robot.chat_id)["chat"] is None

    scenario(tmp_path, body, d3_route=route, camera_frames={"a": Frames()},
             vision_look_decision={"enabled": True})


def test_background_intents_batch_language_absent_and_serious_without_blocking(tmp_path):
    d3 = KeyedD3({
        "language": {"choice": "this_reply", "confidence": 0.88},
        "absent": {"choice": "absent", "confidence": 0.91},
        "serious": {"choice": "serious", "confidence": 0.94},
    })
    route = D3Route(d3, model=D3_MODEL)
    route._mark(True, None)

    async def run():
        rt = HarborRuntime(
            [RobotConfig("a", room_id="roomv2")], tmp_path / "rt.db",
            realtime_key="fake", realtime_socket_factory=EchoSocket,
            d3_route=route, face_registries={"a": FakeRegistry(["小明"])},
            tts_sounds_enabled=True,
        )
        await rt.start()
        client = rt.realtime["a"]
        try:
            await until(lambda: client.ready)
            client.sounds_enabled = True
            client._refresh_known_people()
            await asyncio.sleep(0.05)
            await client.submit_text("我今天失业了太难受了，整点河南话安慰下")
            await until(lambda: len(d3.calls) == 1 and client.language.serious_context)
            assert len(d3.calls) == 1
            assert set(d3.calls[0][1]) == {"language", "absent", "serious"}
            assert client.language.explicit["dialect"] == "henan"
            assert client.language.explicit["scope"] == "turn"
            assert client._absent_asked[client.turn_id] is True
            assert client.sounds is not None and client.sounds.serious is True
            assert "【严肃倾诉】" in client.language.instruction_hint()
        finally:
            await rt.close()

    asyncio.run(run())


def test_d3_language_confidence_floor_rejects_unsure_mention_while_dashscope_keeps_fallback(tmp_path):
    # d3 at 0.47 ("你普通话没有换成功吧") is rejected under the 0.6 floor.
    d3 = KeyedD3({"intent": {"choice": "from_now", "confidence": 0.47}})
    route = D3Route(d3, model=D3_MODEL)
    route._mark(True, None)

    async def run_d3():
        rt = HarborRuntime(
            [RobotConfig("a", room_id="roomv2")], tmp_path / "d3.db",
            realtime_key="fake", realtime_socket_factory=EchoSocket, d3_route=route,
        )
        await rt.start()
        client = rt.realtime["a"]
        try:
            await until(lambda: client.ready)
            await client.submit_text("你普通话没有换成功吧。")
            await until(lambda: len(d3.calls) == 1)
            await asyncio.sleep(0.03)
            assert rt.store.load_language_preferences("a", client.robot.chat_id)["chat"] is None
            assert client.language.explicit is None
        finally:
            await rt.close()

    asyncio.run(run_d3())

    # DashScope fallback (uncalibrated confidence) still adopts 0.47.
    ali = FakeDecision({"choice": "from_now", "confidence": 0.47})

    async def run_ali():
        rt = HarborRuntime(
            [RobotConfig("a", room_id="roomv2")], tmp_path / "ali.db",
            realtime_key="fake", realtime_socket_factory=EchoSocket, decision_client=ali,
        )
        await rt.start()
        client = rt.realtime["a"]
        try:
            await until(lambda: client.ready)
            await client.submit_text("粤语聊天吧")
            await until(lambda: rt.store.load_language_preferences("a", client.robot.chat_id)["chat"])
            assert rt.store.load_language_preferences("a", client.robot.chat_id)["chat"]["dialect"] == "cantonese"
        finally:
            await rt.close()

    asyncio.run(run_ali())


def test_serious_safety_valve_uses_d3_only_and_respects_threshold(tmp_path):
    assert serious_cue("我失业了不知道该怎么办")
    assert not serious_cue("今天天气怎么样")
    assert set(SERIOUS_CONTEXT_QUESTION["criteria"]) == {"serious", "light"}

    # DashScope alone never asks SERIOUS_CONTEXT_QUESTION (too many FPs in 11-25 eval).
    ali = FakeDecision({"choice": "serious", "confidence": 0.99})

    async def run_ali():
        rt = HarborRuntime(
            [RobotConfig("a", room_id="roomv2")], tmp_path / "s-ali.db",
            realtime_key="fake", realtime_socket_factory=EchoSocket,
            decision_client=ali, tts_sounds_enabled=True,
        )
        await rt.start()
        client = rt.realtime["a"]
        try:
            await until(lambda: client.ready)
            await client.submit_text("我妈住院了")
            await asyncio.sleep(0.03)
            assert ali.calls == []
            assert not client.language.serious_context
        finally:
            await rt.close()

    asyncio.run(run_ali())

    # d3 low-confidence serious (0.32 on light banter) does not activate serious mode.
    d3 = KeyedD3({"serious": {"choice": "serious", "confidence": 0.32}})
    route = D3Route(d3, model=D3_MODEL)
    route._mark(True, None)

    async def run_low():
        rt = HarborRuntime(
            [RobotConfig("a", room_id="roomv2")], tmp_path / "s-low.db",
            realtime_key="fake", realtime_socket_factory=EchoSocket,
            d3_route=route, tts_sounds_enabled=True,
        )
        await rt.start()
        client = rt.realtime["a"]
        try:
            await until(lambda: client.ready)
            client.sounds_enabled = True
            await client.submit_text("今天好累啊不想上班")
            await until(lambda: len(d3.calls) == 1)
            await asyncio.sleep(0.03)
            assert not client.language.serious_context
            assert client.sounds is not None and not client.sounds.serious
        finally:
            await rt.close()

    asyncio.run(run_low())


@pytest.mark.parametrize(("text", "expected_clear"), [
    ("取消这个会话的俄语设置", "chat"),
    ("清除机器人的语言设置", "robot"),
    ("恢复默认语言", "any"),
    ("不要清除这个会话的语言设置", None),
])
def test_clear_language_preference_tool_uses_shared_parser(tmp_path, text, expected_clear):
    from annie_runtime.language import explicit_language_clear
    assert explicit_language_clear(text) == expected_clear

    async def run():
        rt = HarborRuntime(
            [RobotConfig("a", room_id="roomv2")], tmp_path / "clr.db",
            realtime_key="fake", realtime_socket_factory=EchoSocket,
        )
        await rt.start()
        client = rt.realtime["a"]
        try:
            await until(lambda: client.ready)
            client._create_turn(text)
            await client._tool_call({
                "call_id": "c1",
                "name": "clear_language_preference",
                "arguments": json.dumps({"scope": "chat"}),
            })
            res = next(e for e in client.events if e["kind"] == "tool.result" and e["call_id"] == "c1")
            assert res["valid"] is (expected_clear == "chat")
        finally:
            await rt.close()

    asyncio.run(run())
