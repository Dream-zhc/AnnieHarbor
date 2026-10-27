"""10-27 demo on the 188 big head: speech she ignored, look actions she never made,
and live ASR that showed late (docs/开发记录/2026-10-27/演示忽略用户与看向动作.md)."""

import asyncio
from types import SimpleNamespace

import pytest
from annie_brain.decision_policy import (
    ADDRESSEE_FOLLOWUP_QUESTION,
    ADDRESSEE_SYSTEM_FOLLOWUP,
    explicit_motion,
    head_turn_motion,
)
from annie_runtime import realtime as realtime_module
from annie_runtime.__main__ import DEFAULT_ACTION_ALLOWLIST

from tests.test_decision_model import FakeDecision
from tests.test_wake_call_recovery import kinds, ready, runtime, say, sent, settled, woken


# ---- look actions for "what is on your left / above" ---------------------------------
@pytest.mark.parametrize(("text", "script"), [
    ("左手边有有啥？你看到了什么画面？", "look_left"),  # 11:22:20, ASR stutter kept
    ("那你看看你的你的左手边。", "look_left"),  # 11:22:51
    ("上面有什么？", "look_up"),  # 11:17:35
    ("你看看右手边有什么东西？", "look_right"),
    ("小安妮，左边是什么？", "look_left"),
    ("你右侧有没有人？", "look_right"),
    ("看看左边有什么？", "look_left"),  # matched before, still does
    ("右边看。", "look_right"),  # 11:16:38, side first
    ("往左手边看看", "look_left"),
])
def test_questions_about_a_side_turn_her_head_that_way(text, script):
    assert explicit_motion(text, DEFAULT_ACTION_ALLOWLIST) == script


@pytest.mark.parametrize("text", [
    "你的左边没有饮水镜。",  # a statement about the left, not a request (11:22:41)
    "你左右看那你的饮水机在你的什么位置？",  # both sides: no single script
    "桌子上面有什么？",  # the top of a table, not above her
    "看看你的左手。",  # her hand, not her left side
    "别往左边看了",
    "左边看起来有点乱。",
])
def test_other_mentions_of_a_side_do_not_move_her(text):
    assert explicit_motion(text, DEFAULT_ACTION_ALLOWLIST) is None


def test_turn_your_head_takes_the_side_the_user_just_pointed_at():
    previous = ["你的左边没有饮水镜。", "那你看看你的你的左手边。", "旋转头了吗？你看见。"]
    allowed = DEFAULT_ACTION_ALLOWLIST
    assert head_turn_motion("那你现在转头看看。", previous, allowed) == "look_left"  # 11:23:18
    assert head_turn_motion("旋转头了吗？你看见。", previous, allowed) is None  # a question
    assert head_turn_motion("那你现在转头看看。", ["你会数数吗？"], allowed) is None  # no side
    assert head_turn_motion("别转头看了", previous, allowed) is None


# ---- smart_turn invalid while she speaks or right after a wake -----------------------
@pytest.fixture(autouse=True)
def quick_settle(monkeypatch):
    monkeypatch.setattr(realtime_module, "RECOVERY_SETTLE_SECONDS", 0.05)


def speaking(client):
    """She is answering (ACTIVE): a reply plays until the speaker reports drained."""
    presence = woken(client)
    presence.state = "ACTIVE"
    presence.deadline = None
    presence._first_wake_transcript = False
    return presence


def answered(client):
    created = [e for e in sent(client, "conversation.item.create")
               if e["item"].get("role") == "user"]
    return [e["item"]["content"][0]["text"] for e in created]


def demo(tmp_path, body):
    async def run():
        rt = runtime(tmp_path)
        client = await ready(rt)
        try:
            await body(client)
        finally:
            await rt.close()
    asyncio.run(run())


def test_a_request_smart_turn_drops_while_she_speaks_is_judged_and_answered(tmp_path):
    """11:16:38 "右边看", 11:18:50 "你介绍的不对": dropped with recover=False before."""
    async def body(client):
        client.decision = FakeDecision({"choice": "robot", "confidence": .9})
        client.addressee_enabled = True
        speaking(client)
        await say(client, "a1", draft="你介绍的不对", invalid=True)
        await say(client, "a1", final="你介绍的不对，重新介绍一下。", start=False)
        await settled(client)
        assert answered(client) == ["你介绍的不对，重新介绍一下。"]
        assert len(client.decision.calls) == 1
    demo(tmp_path, body)


def test_side_talk_smart_turn_drops_while_she_speaks_stays_unanswered(tmp_path):
    async def body(client):
        client.decision = FakeDecision({"choice": "person", "confidence": .8})
        client.addressee_enabled = True
        speaking(client)
        await say(client, "a2", draft="电锯还是啥没通", invalid=True)
        await say(client, "a2", final="电锯还是啥没通，你看。", start=False)
        await settled(client)
        assert answered(client) == []
        assert kinds(client, "transcript.ambient")[-1]["reason"] == "not_addressed"
    demo(tmp_path, body)


def test_a_motion_command_dropped_while_she_speaks_needs_no_judge(tmp_path):
    from tests.test_phase7_runtime import ready as ready7
    from tests.test_phase7_runtime import runtime as runtime7

    async def run():
        rt = runtime7(tmp_path)  # real ActionCoordinator, look_right enabled
        await ready7(rt)
        client = rt.realtime["a"]
        try:
            client.decision = FakeDecision()  # any question to it would be recorded
            client.addressee_enabled = True
            speaking(client)
            await say(client, "a3", draft="右边看", invalid=True)  # 11:16:38
            await say(client, "a3", final="右边看。", start=False)
            await settled(client)
            assert client.decision.calls == []
            assert answered(client) == ["右边看。"]
        finally:
            await rt.close()
    asyncio.run(run())


def test_an_unnamed_request_right_after_a_wake_is_answered(tmp_path):
    """13:52:37 / 13:54:42: unnamed WAKE_WAIT utterances smart_turn dropped."""
    async def body(client):
        client.decision = FakeDecision()
        client.addressee_enabled = True
        presence = woken(client)
        presence._first_wake_transcript = False  # the call itself was the first utterance
        await say(client, "w1", draft="你认为自己是个机器人", invalid=True)
        await say(client, "w1", final="你认为自己是个机器人吗？", start=False)
        await settled(client)
        assert client.decision.calls == []  # just woken by name: said to her
        assert answered(client) == ["你认为自己是个机器人吗？"]
    demo(tmp_path, body)


def test_the_held_final_is_shown_at_once_not_after_the_judge(tmp_path):
    """Live ASR stalled, then the whole sentence appeared: the final waited ~1 s."""
    async def body(client):
        client.decision = FakeDecision({"choice": "robot", "confidence": .9})
        client.addressee_enabled = True
        presence = woken(client)
        presence.state = "FOLLOWUP"
        presence._first_wake_transcript = False
        await say(client, "f1", draft="小爱您", invalid=True)
        await say(client, "f1", final="你说话的语速慢一点。", start=False)
        preview = kinds(client, "transcript.final_preview")
        assert preview and preview[-1]["text"] == "你说话的语速慢一点。"
        assert client.decision.calls == []  # still settling: shown before the judge ran
        await settled(client)
        assert answered(client) == ["你说话的语速慢一点。"]
    demo(tmp_path, body)


def test_the_judge_asks_the_follow_up_question_while_she_speaks(tmp_path):
    """The follow-up wording scored 87% on d3 (10-21) against 72% for the idle one."""
    class D3:
        def __init__(self):
            self.questions = []

        async def ask(self, _state, questions):
            self.questions.append(questions["to"])
            return {"to": {"choice": "robot", "confidence": .9}}

    class Judge:
        def __init__(self):
            self.systems = []

        async def judge(self, system, _user):
            self.systems.append(system)
            return "robot", .8

    async def body(client):
        client.wake = SimpleNamespace(state="ACTIVE")
        d3 = D3()
        client.d3 = SimpleNamespace(ready=True, client=d3, model="d3-NVFP4",
                                    failed=lambda _error: None)
        assert await client._decide_addressee("你介绍的不对", "q1") == "robot"
        assert d3.questions == [ADDRESSEE_FOLLOWUP_QUESTION]
        client.d3 = None
        judge = Judge()
        client.addressee_judge = judge
        assert await client._decide_addressee("你介绍的不对", "q2") == "robot"
        assert judge.systems == [ADDRESSEE_SYSTEM_FOLLOWUP]
        client.wake = None
    demo(tmp_path, body)
