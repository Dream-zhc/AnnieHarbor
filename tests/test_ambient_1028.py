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


# ---- 10-28 evening: the provider's real order (docs/开发记录/2026-10-28/环境音补答与拆句合并.md)
async def background(client, item, text, *, delta=True):
    """Background speech as the provider sends it: no speech_started, and its delta
    and final carry a standalone temporary id (Qwen-Audio realtime server events)."""
    g = client.robot.generation
    if delta:
        await client.handle({"type": "conversation.item.ambient_audio_transcription.delta",
                             "item_id": item, "text": "", "stash": text[:2]}, g)
    await client.handle({"type": "conversation.item.ambient_audio_transcription.completed",
                         "item_id": item, "transcript": text}, g)


def judged(client):
    return [state["当前这句话"] for state, _ in client.decision.calls]


def test_background_speech_judged_for_her_is_answered(tmp_path):
    """19:56:04 “今天天气怎么样？” was judged for her (0.91) and never answered: the
    wake gate admitted only speech it saw start, and background speech never starts."""
    async def body(client):
        in_state(client, "FOLLOWUP")
        await background(client, "amb_1", "今天天气怎么样？")
        await settled(client)
        assert answered(client) == ["今天天气怎么样？"]
        assert client.wake.state == "ACTIVE"
    demo(tmp_path, body, FakeDecision({"choice": "robot", "confidence": .9}))


def test_background_call_by_name_is_answered_without_a_judge(tmp_path):
    """15:47:09 “小安妮，你看看左边。” was dropped the same way."""
    async def body(client):
        in_state(client, "WAKE_WAIT")
        await background(client, "amb_2", "小安妮，那你介绍一下北京。")
        await settled(client)
        assert client.decision.calls == []
        assert answered(client) == ["小安妮，那你介绍一下北京。"]
    demo(tmp_path, body)


def test_background_speech_judged_side_talk_still_gets_no_answer(tmp_path):
    async def body(client):
        in_state(client, "FOLLOWUP")
        await background(client, "amb_3", "哎，有个简单的办法。")
        await settled(client)
        assert answered(client) == [] and client.wake.state == "FOLLOWUP"
        assert kinds(client, "transcript.ambient")[-1]["reason"] == "not_addressed"
    demo(tmp_path, body, FakeDecision({"choice": "person", "confidence": .7}))


def test_background_sentence_cut_in_two_is_joined_and_answered_once(tmp_path):
    """19:55:46 “你今天天气。” + “怎么样呀？”: both halves went to ambient and were judged
    apart; the tail alone was taken for side talk."""
    async def body(client):
        in_state(client, "FOLLOWUP")
        g = client.robot.generation
        await background(client, "amb_h", "你今天天气。")
        # The user goes on before the head's quiet wait ends: only a delta shows it.
        await client.handle({"type": "conversation.item.ambient_audio_transcription.delta",
                             "item_id": "amb_t", "text": "", "stash": "怎么"}, g)
        await asyncio.sleep(0.15)  # longer than the (patched) quiet wait
        assert client.decision.calls == [] and answered(client) == []
        await client.handle({"type": "conversation.item.ambient_audio_transcription.completed",
                             "item_id": "amb_t", "transcript": "怎么样呀？"}, g)
        await settled(client)
        assert judged(client) == ["你今天天气怎么样呀？"]
        assert answered(client) == ["你今天天气怎么样呀？"]
    demo(tmp_path, body, FakeDecision({"choice": "robot", "confidence": .9}))


def test_a_second_background_final_joins_a_head_still_waiting(tmp_path):
    """19:54:08 “你把你的声音。” + “到最大。”: the second final replaced the first."""
    async def body(client):
        in_state(client, "FOLLOWUP")
        await background(client, "amb_a", "你把你的声音。", delta=False)
        await background(client, "amb_b", "到最大。", delta=False)
        await settled(client)
        assert answered(client) == ["你把你的声音到最大。"]
    demo(tmp_path, body, FakeDecision({"choice": "robot", "confidence": .9}))


def test_an_answer_refused_while_the_user_speaks_again_waits_and_joins(tmp_path):
    """19:51:50: response.create for a recovered sentence was refused (“Cannot create
    response while user is speaking.”) and the error closed the whole session."""
    async def body(client):
        in_state(client, "FOLLOWUP")
        g = client.robot.generation
        await background(client, "amb_r", "你对你现在的样子满意吗？")
        await settled(client)
        assert answered(client) == ["你对你现在的样子满意吗？"]
        asked = sent(client, "response.create")[-1]
        await client.handle({"type": "error", "error": {
            "type": "invalid_request_error", "code": "invalid_value",
            "message": "Cannot create response while user is speaking.",
            "param": "response.create", "event_id": asked.get("event_id")}}, g)
        assert client.ready and client.turn_id is None  # no RuntimeError, no reconnect
        await background(client, "amb_r2", "说实话。")
        await settled(client)
        assert answered(client)[-1] == "你对你现在的样子满意吗说实话。"
        assert len(client.decision.calls) == 1  # already judged for her; not asked again
    demo(tmp_path, body, FakeDecision({"choice": "robot", "confidence": .9}))


def test_background_speech_being_judged_keeps_a_closing_window_open(tmp_path):
    """Background speech starts no candidate, so speech_stopped cannot hold the
    window for its verdict: just woken, the gate would close during the judge."""
    async def body(client):
        presence = in_state(client, "WAKE_WAIT")
        presence.deadline = presence._window_deadline = presence.clock() + 0.02
        await background(client, "amb_w", "简单的介绍一下你自己。")
        await asyncio.sleep(0.05)
        await settled(client)
        assert answered(client) == ["简单的介绍一下你自己。"]
    demo(tmp_path, body, FakeDecision({"choice": "robot", "confidence": .9}))
