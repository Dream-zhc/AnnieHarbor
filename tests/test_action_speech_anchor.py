"""Word actions: sent as action_trigger once the word is in the reply and her voice started.

The embedded player ignores action_prepare/phoneme_anchor_v1 (2026-10-22).
"""

import asyncio
from types import SimpleNamespace

import pytest

from tests.test_phase7_execution import (
    BlockingPublisher, FakePublisher, coordinator, wait_for,
)
from tests.test_phase7_runtime import ready, result_from_call, runtime


def speech_for(robot, *, text="", turn="turn", epoch=3, frames=1):
    identity = {"robot_id": robot.config.robot_id, "generation": robot.generation,
                "session_id": "session", "persona_id": "default"}
    robot.session_state = lambda: dict(identity)
    return SimpleNamespace(
        turn_id=turn, frames=frames, inflight_end=0, stream_epoch=epoch,
        generated_text=text, submitted_text="", published_text="", samples=0,
        cancelled=False, identity=identity, receipt_tracks={},
        publisher=FakePublisher(),
    )


def begin(actions, speech, robot_id="A"):
    actions.on_turn_started({**speech.identity, "robot_id": robot_id,
                             "turn_id": speech.turn_id})


async def reserve(actions, speech, *, call="call", anchor="点头", occurrence=1,
                  script="agree_micro_nod", turn="turn"):
    return await actions.request(
        "A", call, turn, 1, "action", script, speech=speech,
        user_text="你选一个动作", anchor_text=anchor,
        anchor_occurrence=occurrence,
    )


@pytest.mark.parametrize("script", ("surprise", "smirk", "proud", "skeptical", "shy"))
def test_expressive_action_is_one_action_trigger_not_acoustic_wait(tmp_path, script):
    async def run():
        actions, pubs, _events, robot, _b = coordinator(tmp_path)
        actions.allowlist = (*actions.allowlist, script)
        speech = speech_for(robot, text="好家伙，真有意思")
        await actions.start()
        try:
            begin(actions, speech)
            with pytest.raises(ValueError, match="requires anchor_text"):
                await actions.request("A", "invalid", "turn", 1, "action", script,
                                      speech=speech, user_text="选一个")
            result = await reserve(actions, speech, script=script, anchor="好家伙")
            assert result["status"] == "awaiting_text_anchor"
            await wait_for(lambda: len(pubs["A"].sent) == 1)
            packet = pubs["A"].sent[0]
            assert packet["type"] == "action_trigger"
            assert packet["data"] == {"script_id": script, "offset_ms": 0,
                                      "conversation_id": "turn",
                                      "anchor_source": "robot_main_brain"}
            assert pubs["B"].sent == []
            await wait_for(lambda: actions.store.get_action("A", "call")["status"] ==
                           "published_unknown")
        finally:
            await actions.close()
    asyncio.run(run())


def test_no_unindexed_reservation_and_dispatch_on_first_matching_text_delta(tmp_path):
    async def run():
        actions, pubs, _events, robot, _b = coordinator(tmp_path)
        speech = speech_for(robot)
        await actions.start()
        try:
            begin(actions, speech)
            assert (await reserve(actions, speech))["status"] == "awaiting_text_anchor"
            assert not pubs["A"].sent
            actions.on_first_pcm({"robot_id": "A", "turn_id": "turn", "generation": 1})
            await asyncio.sleep(.02)
            assert not pubs["A"].sent
            speech.generated_text = "好，点"
            actions.on_text_ready({"robot_id": "A", "turn_id": "turn"})
            await asyncio.sleep(.02)
            assert not pubs["A"].sent
            speech.generated_text += "头吧。"
            actions.on_text_ready({"robot_id": "A", "turn_id": "turn"})
            actions.on_text_ready({"robot_id": "A", "turn_id": "turn"})
            await wait_for(lambda: len(pubs["A"].sent) == 1)
            assert pubs["A"].sent[0]["type"] == "action_trigger"
        finally:
            await actions.close()
    asyncio.run(run())


@pytest.mark.parametrize("occurrence,sent", [(1, True), (2, True), (3, False)])
def test_the_requested_occurrence_must_be_in_the_reply(tmp_path, occurrence, sent):
    async def run():
        actions, pubs, _events, robot, _b = coordinator(tmp_path)
        speech = speech_for(robot, text="好，好！ 点头吧。")
        await actions.start()
        try:
            begin(actions, speech)
            await reserve(actions, speech, anchor="好", occurrence=occurrence)
            await asyncio.sleep(.05)
            assert len(pubs["A"].sent) == int(sent)
        finally:
            await actions.close()
    asyncio.run(run())


def test_word_in_the_reply_waits_for_her_first_pcm(tmp_path):
    async def run():
        actions, pubs, _events, robot, _b = coordinator(tmp_path)
        speech = speech_for(robot, text="点头吧。", frames=0)
        await actions.start()
        try:
            begin(actions, speech)
            assert (await reserve(actions, speech))["status"] == "awaiting_text_anchor"
            actions.on_text_ready({"robot_id": "A", "turn_id": "turn"})
            await asyncio.sleep(.03)
            assert not pubs["A"].sent
            speech.frames = 1
            actions.on_first_pcm({"robot_id": "A", "turn_id": "turn", "generation": 1})
            await wait_for(lambda: len(pubs["A"].sent) == 1)
        finally:
            await actions.close()
    asyncio.run(run())


def test_generated_text_not_submitted_text_is_the_earliest_authority(tmp_path):
    async def run():
        actions, pubs, _events, robot, _b = coordinator(tmp_path)
        speech = speech_for(robot)
        speech.submitted_text = "点头"
        speech.published_text = "点头"
        await actions.start()
        try:
            begin(actions, speech)
            await reserve(actions, speech)
            await asyncio.sleep(.02)
            assert not pubs["A"].sent
            speech.generated_text = "点头"
            actions.on_text_ready({"robot_id": "A", "turn_id": "turn"})
            await wait_for(lambda: len(pubs["A"].sent) == 1)
        finally:
            await actions.close()
    asyncio.run(run())


def test_no_target_text_expires_without_sending_empty_or_guessed_command(tmp_path, monkeypatch):
    async def run():
        import annie_runtime.actions as module
        monkeypatch.setattr(module, "ANCHOR_TIMEOUT", .035)
        actions, pubs, _events, robot, _b = coordinator(tmp_path)
        speech = speech_for(robot, text="这段话没有动作目标")
        await actions.start()
        try:
            begin(actions, speech)
            await reserve(actions, speech)
            await wait_for(lambda: not actions.pending)
            assert not pubs["A"].sent
            assert actions.store.get_action("A", "call")["status"] == "expired_no_text_anchor"
        finally:
            await actions.close()
    asyncio.run(run())


@pytest.mark.parametrize("failure", ("cancel", "epoch", "generation", "turn", "speaker_drained"))
def test_stale_or_cancelled_intent_is_not_published(tmp_path, failure):
    async def run():
        actions, pubs, _events, robot, _b = coordinator(tmp_path)
        speech = speech_for(robot)
        await actions.start()
        try:
            begin(actions, speech)
            await reserve(actions, speech)
            if failure == "cancel":
                await actions.cancel_pending("A")
            elif failure == "epoch":
                speech.stream_epoch += 1
            elif failure == "generation":
                robot.generation += 1
            elif failure == "turn":
                actions.on_turn_started({**speech.identity, "turn_id": "replacement"})
            else:
                speech.receipt_tracks["speaker"] = {"played": 24000, "state": "drained"}
            speech.generated_text = "点头"
            actions.on_text_ready({"robot_id": "A", "turn_id": "turn"})
            await asyncio.sleep(.03)
            assert not pubs["A"].sent
        finally:
            await actions.close()
    asyncio.run(run())


def test_pending_intent_survives_until_speech_stream_begins_with_next_epoch(tmp_path):
    async def run():
        actions, pubs, _events, robot, _b = coordinator(tmp_path)
        speech = speech_for(robot, turn=None, epoch=2)
        await actions.start()
        try:
            actions.on_turn_started({**speech.identity, "turn_id": "turn"})
            await reserve(actions, speech)
            speech.turn_id = "turn"
            speech.stream_epoch = 3
            speech.generated_text = "点头"
            actions.on_text_ready({"robot_id": "A", "turn_id": "turn"})
            await wait_for(lambda: len(pubs["A"].sent) == 1)
            assert pubs["A"].sent[0]["type"] == "action_trigger"
        finally:
            await actions.close()
    asyncio.run(run())


def test_no_replay_on_repeat_tool_call_or_extra_text_delta(tmp_path):
    async def run():
        actions, pubs, _events, robot, _b = coordinator(tmp_path)
        speech = speech_for(robot, text="点头")
        await actions.start()
        try:
            begin(actions, speech)
            await reserve(actions, speech)
            await wait_for(lambda: len(pubs["A"].sent) == 1)
            assert (await reserve(actions, speech))["status"].startswith("duplicate_")
            for _ in range(8):
                actions.on_text_ready({"robot_id": "A", "turn_id": "turn"})
            await asyncio.sleep(.02)
            assert len(pubs["A"].sent) == 1
        finally:
            await actions.close()
    asyncio.run(run())


def test_unknown_network_delivery_never_retries(tmp_path):
    async def run():
        actions, pubs, _events, robot, _b = coordinator(tmp_path)
        pubs["A"].fail = True
        speech = speech_for(robot, text="点头")
        await actions.start()
        try:
            begin(actions, speech)
            await reserve(actions, speech)
            await wait_for(lambda: actions.store.get_action("A", "call")["status"] ==
                           "send_unknown")
            assert not pubs["A"].sent
            assert (await reserve(actions, speech))["status"] == "duplicate_send_unknown"
        finally:
            await actions.close()
    asyncio.run(run())


def test_network_wait_does_not_hold_robot_cancel_lock(tmp_path):
    async def run():
        actions, pubs, _events, robot, _b = coordinator(tmp_path)
        blocked = pubs["A"] = BlockingPublisher()
        speech = speech_for(robot, text="点头")
        await actions.start()
        try:
            begin(actions, speech)
            await reserve(actions, speech)
            await asyncio.wait_for(blocked.started.wait(), .5)
            await asyncio.wait_for(actions.cancel_pending("A"), .2)
            blocked.release.set()
            await wait_for(lambda: len(blocked.sent) == 1)
            assert actions.store.get_action("A", "call")["status"] == "published_unknown"
        finally:
            await actions.close()
    asyncio.run(run())


def test_ordinary_legacy_action_remains_first_pcm_trigger(tmp_path):
    async def run():
        actions, pubs, _events, robot, _b = coordinator(tmp_path)
        speech = speech_for(robot, text="点头", frames=0)
        await actions.start()
        try:
            begin(actions, speech)
            result = await actions.request("A", "legacy", "turn", 1, "action",
                                           "agree_micro_nod", speech=speech,
                                           user_text="你点头")
            assert result["status"] == "awaiting_audio_anchor"
            actions.on_text_ready({"robot_id": "A", "turn_id": "turn"})
            await asyncio.sleep(.02)
            assert not pubs["A"].sent
            actions.on_first_pcm({"robot_id": "A", "turn_id": "turn", "generation": 1})
            await wait_for(lambda: len(pubs["A"].sent) == 1)
            assert pubs["A"].sent[0]["type"] == "action_trigger"
        finally:
            await actions.close()
    asyncio.run(run())


def test_realtime_tool_response_remains_neutral_and_uses_new_intent(tmp_path):
    async def run():
        rt = runtime(tmp_path)
        await ready(rt)
        try:
            client = rt.realtime["a"]
            client._create_turn("你选一个")
            result = await result_from_call(client, "early", "perform_action",
                                            {"script_id": "surprise", "anchor_text": "好家伙"})
            assert result[0]["status"] == "awaiting_text_anchor"
            assert client.speech is not None
            # No acoustic result is required. Simulate the genuine streaming text.
            speech = client.speech
            speech.turn_id = client.turn_id
            speech.identity = client.robot.session_state()
            speech.generated_text = "好家伙，试试"
            speech.stream_epoch = rt.realtime["a"].speech.stream_epoch + 1
            client.actions.on_text_ready({"robot_id": "a", "turn_id": client.turn_id})
            assert client.calls
        finally:
            await rt.close()
    asyncio.run(run())
