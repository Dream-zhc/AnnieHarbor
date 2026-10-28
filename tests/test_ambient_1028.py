"""10-28 188 big head: most speech went to the provider's "ambient" bucket and one
sentence came back as two (docs/开发记录/2026-10-28/环境音误判与一句拆两句.md)."""

import asyncio

import pytest
from annie_runtime import realtime as realtime_module

from tests.test_decision_model import FakeDecision
from tests.test_wake_call_recovery import kinds, ready, runtime, sent, settled, woken


@pytest.fixture(autouse=True)
def quick_settle(monkeypatch):
    monkeypatch.setattr(realtime_module, "RECOVERY_SETTLE_SECONDS", 0.05)


def answered(client):
    return [e["item"]["content"][0]["text"] for e in sent(client, "conversation.item.create")
            if e["item"].get("role") == "user"]


def in_state(client, state):
    presence = woken(client)
    presence.state = state
    presence._first_wake_transcript = False
    if state != "WAKE_WAIT":
        presence.deadline = presence._window_deadline = None
    return presence


async def ambient(client, item, text):
    """What the provider sends when smart_turn files speech as background."""
    g = client.robot.generation
    await client.handle({"type": "input_audio_buffer.speech_started", "item_id": item}, g)
    await client.handle({"type": "conversation.item.ambient_audio_transcription.completed",
                         "item_id": item, "transcript": text}, g)


def demo(tmp_path, body, decision=None):
    async def run():
        rt = runtime(tmp_path)
        client = await ready(rt)
        client.decision = decision or FakeDecision()
        client.addressee_enabled = True
        try:
            await body(client)
        finally:
            await rt.close()
    asyncio.run(run())


def test_speech_filed_as_ambient_after_her_answer_is_judged_and_answered(tmp_path):
    async def body(client):
        in_state(client, "FOLLOWUP")
        await ambient(client, "h1", "你能介绍一下自己吗？")
        assert kinds(client, "transcript.final_preview")[-1]["text"] == "你能介绍一下自己吗？"
        await settled(client)
        assert answered(client) == ["你能介绍一下自己吗？"]
        assert len(client.decision.calls) == 1
    demo(tmp_path, body, FakeDecision({"choice": "robot", "confidence": .9}))


def test_side_talk_filed_as_ambient_stays_unanswered(tmp_path):
    async def body(client):
        in_state(client, "FOLLOWUP")
        await ambient(client, "h2", "播放的通道被某个进程占用了。")
        await settled(client)
        assert answered(client) == []
        assert kinds(client, "transcript.ambient")[-1]["reason"] == "not_addressed"
    demo(tmp_path, body, FakeDecision({"choice": "person", "confidence": .7}))


def test_speech_filed_as_ambient_right_after_a_wake_is_judged_too(tmp_path):
    """Ambient is a stronger "not for her" than turn_invalid: even just woken, the
    judge decides (background right after a wake stays ignored, see
    test_wake_initial_alias)."""
    async def body(client):
        in_state(client, "WAKE_WAIT")
        await ambient(client, "h3", "简单的介绍一下你自己。")
        await settled(client)
        assert len(client.decision.calls) == 1
        assert answered(client) == ["简单的介绍一下你自己。"]
    demo(tmp_path, body, FakeDecision({"choice": "robot", "confidence": .9}))


def test_her_own_voice_filed_as_ambient_is_not_judged_or_answered(tmp_path):
    import time

    from tests.test_demo_issues_1021 import Playing

    async def body(client):
        in_state(client, "ACTIVE")
        client._last_reply_text = "故宫、颐和园、天坛，这三个是必去的。"
        client._last_reply_t = time.monotonic()
        real, client.speech = client.speech, Playing()
        try:
            await ambient(client, "e8", "颐和园天坛。")  # 10-21 echo sample
            await settled(client)
            assert client.decision.calls == [] and answered(client) == []
            assert kinds(client, "transcript.ambient")[-1]["reason"] == "ambient_audio"
        finally:
            client.speech = real
    demo(tmp_path, body)


def test_a_call_by_name_filed_as_ambient_needs_no_judge(tmp_path):
    async def body(client):
        in_state(client, "FOLLOWUP")
        await ambient(client, "h7", "小安妮，那你介绍一下北京。")
        await settled(client)
        assert client.decision.calls == []
        assert answered(client) == ["小安妮，那你介绍一下北京。"]
    demo(tmp_path, body)


@pytest.mark.parametrize(("state", "text"), [("SLEEP", "你能介绍一下自己吗？"),
                                             ("FOLLOWUP", "嗯嗯。")])
def test_ambient_while_asleep_or_mere_filler_is_still_ignored(tmp_path, state, text):
    async def body(client):
        if state == "SLEEP":
            woken(client).close()  # asleep: provider speech never reopens the gate
        else:
            in_state(client, state)
        await ambient(client, "h4", text)
        await settled(client)
        assert answered(client) == [] and client.decision.calls == []
        if state != "SLEEP":
            assert kinds(client, "transcript.ambient")[-1]["reason"] == "ambient_audio"
    demo(tmp_path, body)


def test_a_sentence_cut_in_two_is_answered_once_as_a_whole(tmp_path):
    """11:03:08: head “简单的介绍” went to ambient, tail “一下自己吗？” was answered alone."""
    async def body(client):
        in_state(client, "FOLLOWUP")
        g = client.robot.generation
        await ambient(client, "h5", "你能简单的介绍")
        # She keeps talking before the quiet wait ends: the tail is a valid turn
        # and the provider starts a speculative reply to the tail alone.
        await client.handle({"type": "input_audio_buffer.speech_started", "item_id": "t5"}, g)
        await client.handle({"type": "input_audio_buffer.speech_stopped", "item_id": "t5"}, g)
        await client.handle({"type": "response.created", "response": {"id": "r-tail"}}, g)
        await client.handle({"type": "conversation.item.input_audio_transcription.completed",
                             "item_id": "t5", "transcript": "一下自己吗？"}, g)
        await settled(client)
        assert answered(client) == ["你能简单的介绍一下自己吗？"]
        assert "r-tail" in client._cancelled_response_ids  # no answer to the tail alone
    demo(tmp_path, body, FakeDecision({"choice": "robot", "confidence": .9}))


def test_a_valid_tail_without_a_dropped_head_is_answered_as_usual(tmp_path):
    async def body(client):
        in_state(client, "FOLLOWUP")
        g = client.robot.generation
        await client.handle({"type": "input_audio_buffer.speech_started", "item_id": "v6"}, g)
        await client.handle({"type": "input_audio_buffer.speech_stopped", "item_id": "v6"}, g)
        await client.handle({"type": "conversation.item.input_audio_transcription.completed",
                             "item_id": "v6", "transcript": "今天天气怎么样？"}, g)
        await settled(client)
        assert answered(client) == []  # the provider's own reply, nothing re-asked
        assert kinds(client, "transcript.final_preview")[-1]["text"] == "今天天气怎么样？"
    demo(tmp_path, body, FakeDecision({"choice": "robot", "confidence": .9}))
