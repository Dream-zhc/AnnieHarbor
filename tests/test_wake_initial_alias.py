"""Observed wake-name mistranscription is scoped to the first KWS utterance."""

import asyncio

import pytest
from annie_core import RobotConfig
from annie_runtime import HarborRuntime
from annie_runtime.presence import DialoguePresence
from annie_runtime.wakeword import WakeAdmission


def finish(policy, text, item_id="first"):
    assert policy.speech_started(item_id)
    assert policy.speech_stopped(item_id=item_id)
    return policy.transcript_accepted(text, item_id=item_id)


@pytest.mark.parametrize("text", ["想爱你。", "想爱你！", " 想 爱 你 "])
def test_observed_alias_is_rejected_only_as_first_kws_utterance(text):
    policy = DialoguePresence()
    policy.wake_detected()
    assert not finish(policy, text)
    assert policy.state == "WAKE_WAIT"
    assert not policy.snapshot()["audio_reply_allowed"]
    assert finish(policy, "想爱你。", "second")


@pytest.mark.parametrize("text", ["小安妮你好呀", "小安妮，我想爱你", "我爱你", "想爱你，怎么办"])
def test_same_breath_greeting_or_question_is_not_filtered(text):
    policy = DialoguePresence()
    policy.wake_detected()
    assert finish(policy, text)
    assert policy.state == "ACTIVE"


def test_alias_does_not_change_general_text_or_followup_semantics():
    assert not DialoguePresence.is_wake_only("想爱你。")
    assert DialoguePresence.is_substantive("想爱你。")
    policy = DialoguePresence()
    policy.wake_detected()
    assert not finish(policy, "小安妮")
    assert finish(policy, "想爱你。", "affection")
    assert policy.speaker_drained(verified=True)
    assert finish(policy, "想爱你。", "followup")


def test_stale_final_does_not_consume_initial_kws_guard():
    policy = DialoguePresence()
    policy.wake_detected()
    policy.speech_started("current")
    assert not policy.transcript_accepted("想爱你", item_id="stale")
    assert not policy.transcript_accepted("想爱你", item_id="current")
    policy.close()
    policy.wake_detected()
    assert not finish(policy, "想爱你", "new-wake")


def test_stale_invalid_stop_does_not_consume_initial_kws_guard():
    policy = DialoguePresence()
    policy.wake_detected()
    policy.speech_started("current")
    assert not policy.speech_stopped(item_id="stale", invalid=True)
    assert not policy.transcript_accepted("想爱你", item_id="current")


def test_duplicate_candidate_events_do_not_consume_initial_kws_guard():
    policy = DialoguePresence()
    policy.wake_detected()
    policy.speech_started("current")
    policy.speech_started("current")
    policy.speech_stopped(item_id="current")
    policy.speech_stopped(item_id="current")
    assert not policy.transcript_accepted("想爱你", item_id="current")


def test_invalid_first_candidate_does_not_filter_second_real_utterance():
    policy = DialoguePresence()
    policy.wake_detected()
    policy.speech_started("invalid")
    policy.speech_stopped(item_id="invalid", invalid=True)
    assert finish(policy, "想爱你。", "second")


def test_replaced_first_candidate_does_not_filter_second_real_utterance():
    policy = DialoguePresence()
    policy.wake_detected()
    policy.speech_started("replaced")
    policy.speech_started("new")
    assert policy.transcript_accepted("想爱你。", item_id="new")


@pytest.mark.parametrize("first_item_id, second_item_id", [
    ("first", None), (None, "second"), (None, None),
])
def test_unknown_item_replacement_does_not_filter_second_real_utterance(
    first_item_id, second_item_id,
):
    policy = DialoguePresence()
    policy.wake_detected()
    assert policy.speech_started(first_item_id)
    assert policy.speech_started(second_item_id)
    assert policy.transcript_accepted("想爱你。", item_id=second_item_id)


class FakeSocket:
    def __init__(self, _url, _key):
        self.outbound = []
        self.inbound = asyncio.Queue()

    async def open(self):
        return self

    async def send(self, packet):
        self.outbound.append(packet)
        if packet["type"] == "session.update":
            await self.inbound.put({
                "type": "session.updated",
                "session": {"id": "wake-alias-regression", **packet["session"]},
            })

    async def recv(self):
        return await self.inbound.get()

    async def close(self):
        pass


@pytest.mark.parametrize("enabled", [False, True])
def test_provider_receives_wake_address_rule_only_with_kws_enabled(tmp_path, enabled):
    async def run():
        runtime = HarborRuntime(
            [RobotConfig("annie-a", room_id="roomv2", listen_enabled=True)],
            tmp_path / "address-rule.db", realtime_key="fake-test-key",
            realtime_socket_factory=FakeSocket,
            wake_detector=object() if enabled else None,
        )
        await runtime.start()
        try:
            client = runtime.realtime["annie-a"]
            for _ in range(100):
                if client.ready:
                    break
                await asyncio.sleep(.01)
            assert client.ready
            packets = [p for p in client.socket.outbound if p["type"] == "session.update"]
            instructions = packets[0]["session"]["instructions"]
            assert ("【本地唤醒称呼】" in instructions) == enabled
            if enabled:
                assert instructions.count("【本地唤醒称呼】") == 1
                assert "小爱你" in instructions
                assert "我爱你" in instructions
                assert "不要围绕称呼打趣" in instructions
                assert "也不要把这个近音当成用户的姓名" in instructions
                assert "明确讨论词义" in instructions
        finally:
            await runtime.close()
    asyncio.run(run())


def test_runtime_rejects_speculative_output_and_deletes_false_user_context(tmp_path):
    async def run():
        runtime = HarborRuntime(
            [RobotConfig("annie-a", room_id="roomv2", listen_enabled=True)],
            tmp_path / "runtime.db", realtime_key="fake-test-key",
            realtime_socket_factory=FakeSocket,
        )
        await runtime.start()
        try:
            client = runtime.realtime["annie-a"]
            for _ in range(100):
                if client.ready:
                    break
                await asyncio.sleep(.01)
            assert client.ready
            client.wake = WakeAdmission(object())
            client.wake.presence.wake_detected()
            gen = client.robot.generation
            for event in (
                {"type": "input_audio_buffer.speech_started", "item_id": "wake"},
                {"type": "response.created", "response": {"id": "unwanted"}},
                {"type": "response.text.delta", "response_id": "unwanted", "delta": "这话太甜了"},
                {"type": "conversation.item.input_audio_transcription.completed",
                 "item_id": "wake", "transcript": "想爱你。"},
            ):
                await client.handle(event, gen)
            assert client.turn_id is None
            assert client.robot.recent_context() == []
            assert client.wake.state == "WAKE_WAIT"
            assert {"type": "conversation.item.delete", "item_id": "wake"} in client.socket.outbound
            assert any(p["type"] == "response.cancel" for p in client.socket.outbound)
            assert not any(e["kind"] == "text.delta" for e in client.events)

            # The next utterance, even the same words, is genuine user input.
            await client.handle({"type": "input_audio_buffer.speech_started",
                                 "item_id": "question"}, gen)
            await client.handle({"type": "conversation.item.input_audio_transcription.completed",
                                 "item_id": "question", "transcript": "想爱你。"}, gen)
            assert client.robot.turn_cache[client.turn_id]["user_text"] == "想爱你。"
        finally:
            await runtime.close()

    asyncio.run(run())


def test_ambient_first_candidate_is_retired_before_next_real_utterance(tmp_path):
    async def run():
        runtime = HarborRuntime(
            [RobotConfig("annie-a", room_id="roomv2", listen_enabled=True)],
            tmp_path / "ambient.db", realtime_key="fake-test-key",
            realtime_socket_factory=FakeSocket,
        )
        await runtime.start()
        try:
            client = runtime.realtime["annie-a"]
            for _ in range(100):
                if client.ready:
                    break
                await asyncio.sleep(.01)
            assert client.ready
            client.wake = WakeAdmission(object())
            client.wake.presence.wake_detected()
            gen = client.robot.generation
            await client.handle({"type": "input_audio_buffer.speech_started",
                                 "item_id": "ambient"}, gen)
            await client.handle({"type": "conversation.item.ambient_audio_transcription.completed",
                                 "item_id": "ambient", "transcript": "环境声"}, gen)
            assert not client.wake.snapshot()["candidate_speech"]
            assert client.wake.snapshot()["deadline_remaining_ms"] is not None
            await client.handle({"type": "input_audio_buffer.speech_started",
                                 "item_id": "real"}, gen)
            await client.handle({"type": "conversation.item.input_audio_transcription.completed",
                                 "item_id": "real", "transcript": "想爱你。"}, gen)
            assert client.robot.turn_cache[client.turn_id]["user_text"] == "想爱你。"
        finally:
            await runtime.close()

    asyncio.run(run())
