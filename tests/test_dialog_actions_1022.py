"""10-22 live test: motions said in dialogue did not run, short motion commands were
judged side talk, and the mouth lagged the voice (docs/开发记录/2026-10-22/对话动作短句与嘴型同步.md)."""

import asyncio
import json
import time

import pytest
from annie_adapters.tts import RoomTtsPublisher
from annie_brain.decision_policy import explicit_motion
from annie_runtime import realtime as realtime_module
from annie_runtime.__main__ import DEFAULT_ACTION_ALLOWLIST
from websockets.protocol import State

from tests.test_action_speech_anchor import begin, reserve, speech_for
from tests.test_decision_model import FakeDecision, followup, utter
from tests.test_phase7_execution import coordinator, wait_for
from tests.test_phase7_runtime import ready, room_server, runtime
from tests.test_wake_call_recovery import say, settled


@pytest.mark.parametrize(("text", "script"), [
    ("小安妮摇摇头。", "shake_head"),
    ("小安妮，你看看上面有什么东西？", "look_up"),
    ("小安妮做一个惊。", "surprise"),  # ASR cut 惊讶 short; she heard it and said 惊讶？
    ("你做一个疑惑的表情。", "skeptical"),
    ("眨眨双眼", "fast_double_blink"),
    ("小安妮点点头。", "agree_micro_nod"),
    ("向左看。", "look_left"),
])
def test_motions_said_in_the_live_test_map_to_enabled_scripts(text, script):
    assert explicit_motion(text, DEFAULT_ACTION_ALLOWLIST) == script


@pytest.mark.parametrize("text", ["我有点疑惑", "做一个惊悚的故事", "别摇头了"])
def test_words_that_are_not_a_motion_request_stay_unmatched(text):
    assert explicit_motion(text, DEFAULT_ACTION_ALLOWLIST) is None


def test_word_action_goes_out_as_action_trigger_once_her_voice_starts(tmp_path):
    """The embedded player ignores action_prepare: 坏笑 never ran on 10-22."""
    async def run():
        actions, pubs, _events, robot, _b = coordinator(tmp_path)
        actions.allowlist = (*actions.allowlist, "smirk")
        speech = speech_for(robot, text="哈哈，被你发现了", frames=0)
        await actions.start()
        try:
            begin(actions, speech)
            result = await reserve(actions, speech, script="smirk", anchor="哈哈")
            assert result["status"] == "awaiting_text_anchor"
            await asyncio.sleep(.03)
            assert pubs["A"].sent == []  # the word exists, her voice has not started
            speech.frames = 1
            actions.on_first_pcm({"robot_id": "A", "turn_id": "turn", "generation": 1})
            await wait_for(lambda: len(pubs["A"].sent) == 1)
            packet = pubs["A"].sent[0]
            assert packet["type"] == "action_trigger"
            assert packet["data"] == {"script_id": "smirk", "offset_ms": 0,
                                      "conversation_id": "turn",
                                      "anchor_source": "robot_main_brain"}
            await wait_for(lambda: actions.store.get_action("A", "call")["status"] ==
                           "published_unknown")
        finally:
            await actions.close()
    asyncio.run(run())


def test_word_decided_after_her_voice_started_goes_out_at_once(tmp_path):
    async def run():
        actions, pubs, _events, robot, _b = coordinator(tmp_path)
        actions.allowlist = (*actions.allowlist, "surprise")
        speech = speech_for(robot, text="哇，五百万！", frames=3)
        await actions.start()
        try:
            begin(actions, speech)
            await reserve(actions, speech, script="surprise", anchor="五百")
            await wait_for(lambda: len(pubs["A"].sent) == 1)
            assert pubs["A"].sent[0]["type"] == "action_trigger"
        finally:
            await actions.close()
    asyncio.run(run())


class ClosingRoom:
    def __init__(self, sockets):
        self.sockets = sockets
        self.sent = []
        self.state = State.OPEN
        self.transport = self

    def abort(self):
        pass

    async def recv(self):
        self.sockets.append(self)
        return json.dumps({"type": "WELCOME", "ok": True, "clientId": "c",
                           "robotId": "reb_main", "room": "roomv2", "size": 1,
                           "subscriptions": [], "catalog": []})

    async def send(self, message):
        self.sent.append(message)


def test_room_channel_closed_while_idle_is_reopened_before_the_action(tmp_path):
    """10-22 15:18:35: 向右看 was lost to ConnectionClosedError on reb_main."""
    async def run():
        sockets = []

        async def connect(_url, **_kwargs):
            return ClosingRoom(sockets)

        publisher = RoomTtsPublisher(room_server(), "roomv2", socket_factory=connect,
                                     channel="main")
        await publisher.publish({"type": "action_trigger", "data": {"script_id": "look_left"}})
        sockets[0].state = State.CLOSED  # the Room dropped the idle connection
        await publisher.publish({"type": "action_trigger", "data": {"script_id": "look_right"}})
        assert len(sockets) == 2
        assert json.loads(sockets[1].sent[0])["data"]["script_id"] == "look_right"
        await publisher.close()
    asyncio.run(run())


def physical(tmp_path):
    rt = runtime(tmp_path)
    for client in rt.realtime.values():
        client.decision = FakeDecision()  # any question to it is recorded
        client.addressee_enabled = True
        client.auto_expression_enabled = True
    return rt


def test_her_prompt_names_only_the_motions_she_can_do(tmp_path):
    async def run():
        rt = physical(tmp_path)
        await ready(rt)
        try:
            instructions = rt.realtime["a"].socket.outbound[0]["session"]["instructions"]
            motion = instructions[instructions.index("【实体动作】"):].split("\n")[0]
            assert "附和小点头" in motion and "看左边" in motion and "惊讶" in motion
            assert "摇头" not in motion  # shake_head is not enabled in this runtime
            assert "做不了" in motion
        finally:
            await rt.close()
    asyncio.run(run())


def test_a_motion_command_in_the_followup_window_needs_no_addressee_check(tmp_path):
    async def run():
        rt = physical(tmp_path)
        await ready(rt)
        client = rt.realtime["a"]
        try:
            presence = followup(client)
            await utter(client, "m1", "向左看。", reply="好嘞，看我的～")
            assert client.decision.calls == []
            assert [e for e in client.events if e["kind"] == "turn.done"]
            assert presence.state != "SLEEP"
        finally:
            await rt.close()
    asyncio.run(run())


def test_a_dropped_motion_command_is_answered_without_asking_who_it_was_for(
        tmp_path, monkeypatch):
    """10-22 15:17:33: smart_turn dropped “摇摇头”, then qwen-flash called it side talk."""
    monkeypatch.setattr(realtime_module, "RECOVERY_SETTLE_SECONDS", 0.05)

    async def run():
        rt = physical(tmp_path)
        await ready(rt)
        client = rt.realtime["a"]
        try:
            followup(client)
            await say(client, "m2", draft="看看左边", invalid=True)
            await say(client, "m2", final="看看左边。", start=False)
            await settled(client)
            assert client.decision.calls == []
            asked = [e for e in client.socket.outbound if e["type"] == "conversation.item.create"
                     and e["item"].get("role") == "user"]
            assert asked and asked[-1]["item"]["content"][0]["text"] == "看看左边。"
        finally:
            await rt.close()
    asyncio.run(run())


def test_after_her_reply_she_keeps_listening_for_30_seconds_without_her_name():
    """10-22 15:10-15:21: re-woken 25, 28, 51 and 54 s after a reply; 20 s was short."""
    import tomllib
    from pathlib import Path

    from annie_runtime.presence import DialoguePresence

    duplex = tomllib.loads((Path(__file__).resolve().parents[1]
                            / "config/runtime_defaults.toml").read_text())["duplex"]
    now = [0.0]
    presence = DialoguePresence(clock=lambda: now[0], wake_wait_ms=duplex["wake_wait_ms"],
                                followup_ms=duplex["followup_ms"],
                                side_talk_listen_ms=duplex["side_talk_listen_ms"])
    presence.wake_detected()
    presence.speech_started("u1")
    assert presence.transcript_accepted("小安妮，向左看", item_id="u1")
    assert presence.speaker_drained(verified=True)
    now[0] = 28.0
    assert presence.permits_pcm() and presence.state == "FOLLOWUP"
    now[0] = 30.5
    assert not presence.permits_pcm()  # then she needs her name again


def test_room_gets_her_voice_in_provider_sized_packets_not_20ms_frames():
    """tts_viseme runs one ForcedAligner pass (~150-380 ms on Windows) per reb_tts
    message with >=120 ms of new audio. 20 ms packets meant ~66 passes for 8.4 s
    of speech and mouths 7.7 s late (10-22); the old brain's 50-800 ms chunks
    needed 6 passes for 3.3 s and kept up."""
    from annie_runtime.speech import ROOM_PCM_MAX_BYTES, SpeechOutput

    from tests.test_phase4b import FakePublisher, FakeRobot, FakeTts

    class ChunkyTts(FakeTts):
        async def send(self, message):
            self.sent.append(message)
            task = message["header"]["task_id"]
            if message["header"]["action"] == "run-task":
                await self.received.put({"header": {"event": "task-started", "task_id": task}})
            elif message["header"]["action"] == "finish-task":
                for _ in range(3):  # the provider's own 800 ms chunks
                    await self.received.put(bytes(38400))
                await self.received.put({"header": {"event": "task-finished", "task_id": task}})

    async def run():
        robot, publisher = FakeRobot(), FakePublisher()
        speech = SpeechOutput(robot, publisher, key="fake", voice="test-voice",
                              url="wss://dashscope.aliyuncs.com/api-ws/v1/inference",
                              socket_factory=ChunkyTts, event=lambda *_a, **_k: None,
                              first_wait_ms=5)
        try:
            await speech.feed("chunky", robot.session_state(), "你好呀，今天过得怎么样？")
            await speech.finish("chunky")
            await asyncio.wait_for(speech.worker, 2)
            audio = [m["data"] for m in publisher.messages if m["data"]["audio"]]
            sizes = [d["end_sample"] - d["start_sample"] for d in audio]
            assert sum(sizes) == 3 * 19200
            assert [d["start_sample"] for d in audio] == [sum(sizes[:i]) for i in range(len(sizes))]
            assert len(audio) <= 6  # 120 packets of 20 ms before
            assert max(sizes) <= ROOM_PCM_MAX_BYTES // 2 + 480
        finally:
            await speech.close()
    asyncio.run(run())


def test_the_mouth_service_gets_her_voice_well_ahead_of_the_speaker():
    """The same tts_viseme keeps mouths ~220 ms ahead for the old brain, which
    pushes PCM seconds ahead; 240 ms here left every mouth behind its sound.
    Bounded: the cloud AEC holds only 15 s of reference past the newest PCM."""
    from annie_runtime.cloud_aec import REFERENCE_MAX_FRAMES
    from annie_runtime.speech import PCM_LEAD_SECONDS

    from annie_runtime.speech import SpeechOutput

    from tests.test_phase4b import FakePublisher, FakeRobot, FakeTts

    async def run():
        robot = FakeRobot()
        speech = SpeechOutput(robot, FakePublisher(), key="fake", voice="test-voice",
                              url="wss://dashscope.aliyuncs.com/api-ws/v1/inference",
                              socket_factory=FakeTts, event=lambda *_a, **_k: None)
        try:
            identity = robot.session_state()
            await speech._begin("lead", identity)
            started = time.perf_counter()
            for _ in range(140):  # 2.8 s of 24 kHz audio
                await speech._publish_pcm(bytes(960), "长句", "part-1", "lead", identity,
                                          started)
            assert time.perf_counter() - started < .3
        finally:
            await speech.close()
    assert 3 <= PCM_LEAD_SECONDS <= REFERENCE_MAX_FRAMES / 100 / 3
    asyncio.run(run())
