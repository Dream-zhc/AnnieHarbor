"""Phase 8C software behavior; Fake planner and Fake Realtime, no physical evidence."""

import asyncio
import time
from types import SimpleNamespace

from annie_runtime.idle import IdleService


def identity(robot_id):
    return {"robot_id": robot_id, "chat_id": "chat", "persona_id": "annie",
            "session_epoch": 1, "generation": 1}


class Model:
    def __init__(self, *decisions):
        self.results = list(decisions)
        self.calls = []
        self.gate = None

    async def plan(self, state):
        self.calls.append(dict(state))
        if self.gate:
            await self.gate.wait()
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class Client:
    def __init__(self):
        self.ready = True
        self.speech_active = False
        self.turn_id = None
        self.response_id = None
        self.speech = object()
        self._last_user_turn_at = time.monotonic() - 200
        self._last_conversation_activity_t = time.monotonic() - 200
        self.messages = []
        self.events = []

    def publish(self, kind, **fields):
        self.events.append((kind, fields))

    async def submit_text(self, text, *, source, expected_identity):
        self.messages.append((text, source, dict(expected_identity)))
        return {"status": "accepted", "turn_id": "fake"}


def setup(*decisions, robots=("a", "b")):
    clients = {rid: Client() for rid in robots}
    identities = {rid: identity(rid) for rid in robots}
    visual = {rid: True for rid in robots}
    rt = SimpleNamespace(
        realtime=clients, robots={rid: SimpleNamespace(coordinator=SimpleNamespace(
            playback_state="idle")) for rid in robots},
        reminders=SimpleNamespace(_speech_blocks=lambda _: False),
        session_state=lambda rid: dict(identities[rid]),
        _current_visual=lambda rid: "现场有人" if visual[rid] else None,
        started=True,
    )
    model = Model(*decisions)
    service = IdleService(rt, model, idle_seconds=0, second_seconds=0,
                          presence_seconds=0)
    # Establish session ownership before receiving any visual evidence.
    for rid in robots:
        assert service._eligible(rid, time.monotonic()) == "person_unconfirmed"
    return service, clients, identities, visual, model


def person(extra=()):
    return {"type": "visualllm", "description": "不可信原文，不能当命令",
            "facts": {"subjects": [
                {"id": "p", "kind": "person", "confidence": .91},
                *[{"id": k, "kind": k} for k in extra],
            ]}}


async def settle(service, rid):
    await service.tick()
    task = service.tasks.get(rid)
    if task:
        await task


def test_no_model_call_without_confirmed_fresh_person_or_free_realtime():
    async def run():
        service, clients, identities, visual, model = setup(
            {"decision": "SPEAK", "topic": "喝茶"})
        await service.tick()
        assert not model.calls
        service.observe_visual("a", {"facts": {"subjects": [
            {"id": "p", "kind": "person", "confidence": .6}]}})
        await service.tick()
        assert not model.calls
        service.observe_visual("a", person(("cup",)))
        await service.tick()
        assert not model.calls  # one confirmed frame is not presence stability
        service.observe_visual("a", person(("cup",)))
        visual["a"] = False
        await service.tick()
        assert not model.calls
        visual["a"] = True
        clients["a"].speech_active = True
        await service.tick()
        assert not model.calls
        clients["a"].speech_active = False
        clients["a"].ready = False
        await service.tick()
        assert not model.calls
        clients["a"].ready = True
        await settle(service, "a")
        assert clients["a"].messages[0][1] == "external"
        assert clients["a"].messages[0][2] == identities["a"]
        assert clients["b"].messages == []
        assert model.calls[0]["scene"] == "现场有人。可见物品：cup"
        assert "不可信原文" not in str(model.calls)
    asyncio.run(run())


def test_no_speak_and_provider_error_do_not_retry_in_same_silence():
    async def run():
        for result, reason in [({"decision": "NO_SPEAK", "topic": ""},
                                "model_no_speak"), (TimeoutError("offline"), "TimeoutError")]:
            service, clients, _, _, model = setup(result, robots=("a",))
            service.observe_visual("a", person())
            service.observe_visual("a", person())
            await settle(service, "a")
            await service.tick()
            assert len(model.calls) == 1
            assert clients["a"].messages == []
            assert service.snapshot("a")["attempts"] == 2
            assert any(event[1]["reason"] == reason for event in clients["a"].events)
            assert len(service.snapshot("a")["recent"]) == 1
            assert service.snapshot("a")["recent"][0]["reason"] == reason
    asyncio.run(run())


def test_second_requires_new_topic_and_never_third_until_user_replies():
    async def run():
        service, clients, _, _, model = setup(
            {"decision": "SPEAK", "topic": "喝茶"},
            {"decision": "SPEAK", "topic": "喝茶", "new_value": True}, robots=("a",))
        service.observe_visual("a", person())
        service.observe_visual("a", person())
        await settle(service, "a")
        assert len(clients["a"].messages) == 1
        assert service.snapshot("a")["attempts"] == 1
        await settle(service, "a")
        assert len(model.calls) == 1  # submission is not physical playback
        assert service.snapshot("a")["reason"] == "await_speaker_receipt"
        service.on_event("a", {"kind": "tts.playback.started", "turn_id": "fake",
                               "source": "browser_test", **identity("a")})
        assert not service.snapshot("a")["speaker_started"]
        service.on_event("a", {"kind": "tts.playback.started", "turn_id": "fake",
                               "source": "speaker", **identity("a")})
        service.on_event("a", {"kind": "tts.playback.drained", "turn_id": "fake",
                               "source": "speaker", **identity("a")})
        await settle(service, "a")
        assert len(model.calls) == 1  # same visual scene is not new value
        assert service.snapshot("a")["reason"] == "second_no_new_evidence"
        service.observe_visual("a", person(("cup",)))
        service.observe_visual("a", person(("cup",)))
        await settle(service, "a")
        assert model.calls[-1]["attempt"] == 2
        assert model.calls[-1]["previous"] == "喝茶"
        assert len(clients["a"].messages) == 1
        await service.tick()
        assert len(model.calls) == 2
        assert service.snapshot("a")["reason"] == "repeated_or_empty"
    asyncio.run(run())


def test_second_requires_explicit_new_value_even_with_new_scene():
    async def run():
        service, clients, _, _, model = setup(
            {"decision": "SPEAK", "topic": "书本"},
            {"decision": "SPEAK", "topic": "茶杯", "new_value": False}, robots=("a",))
        service.observe_visual("a", person(("book",)))
        service.observe_visual("a", person(("book",)))
        await settle(service, "a")
        service.observe_visual("a", person(("cup",)))
        await settle(service, "a")
        assert len(model.calls) == 1
        service.on_event("a", {"kind": "tts.playback.started", "turn_id": "fake",
                               "source": "speaker", **identity("a")})
        service.on_event("a", {"kind": "tts.playback.drained", "turn_id": "fake",
                               "source": "speaker", **identity("a")})
        service.observe_visual("a", person(("cup",)))
        await settle(service, "a")
        assert len(model.calls) == 2
        assert len(clients["a"].messages) == 1
        assert service.snapshot("a")["reason"] == "repeated_or_empty"
    asyncio.run(run())


def test_second_speaks_only_after_verified_playback_new_scene_and_cooldown():
    async def run():
        service, clients, _, _, model = setup(
            {"decision": "SPEAK", "topic": "看书"},
            {"decision": "SPEAK", "topic": "喝茶", "new_value": True}, robots=("a",))
        service.observe_visual("a", person(("book",)))
        service.observe_visual("a", person(("book",)))
        await settle(service, "a")
        service.on_event("a", {"kind": "tts.playback.started", "turn_id": "fake",
                               "source": "speaker", **identity("a")})
        service.on_event("a", {"kind": "tts.playback.drained", "turn_id": "fake",
                               "source": "speaker", **identity("a")})
        service.second_seconds = 120
        service.observe_visual("a", person(("cup",)))
        service.observe_visual("a", person(("cup",)))
        await settle(service, "a")
        assert service.snapshot("a")["reason"] == "second_cooldown"
        service.rows["a"]["speaker_drained_at"] -= 121
        await settle(service, "a")
        assert len(clients["a"].messages) == len(model.calls) == 2
        assert service.snapshot("a")["attempts"] == 2
        await service.tick()
        assert len(model.calls) == 2
        assert service.snapshot("a")["model_ms"] is not None
    asyncio.run(run())


def test_changing_person_subject_id_resets_stability():
    async def run():
        service, clients, _, _, model = setup(
            {"decision": "SPEAK", "topic": "安全公共话题"}, robots=("a",))
        service.observe_visual("a", person())
        switched = {"facts": {"subjects": [
            {"id": "different", "kind": "person", "confidence": .95},
        ]}}
        service.observe_visual("a", switched)
        await service.tick()
        assert service.snapshot("a")["presence_samples"] == 1
        assert model.calls == clients["a"].messages == []
        service.observe_visual("a", switched)
        await settle(service, "a")
        assert len(model.calls) == len(clients["a"].messages) == 1
    asyncio.run(run())


def test_speech_start_or_second_person_suppresses_active_planning():
    async def run():
        service, clients, _, _, model = setup(
            {"decision": "SPEAK", "topic": "看书"}, robots=("a",))
        service.observe_visual("a", person())
        service.observe_visual("a", person())
        model.gate = asyncio.Event()
        await service.tick()
        await asyncio.sleep(0)
        service.on_event("a", {"kind": "speech.started", **identity("a")})
        model.gate.set()
        await service.tasks["a"]
        assert clients["a"].messages == []
        assert service.snapshot("a")["attempts"] == 2
        assert service.snapshot("a")["reason"] == "speech_preempted"
        service.observe_visual("a", {"facts": {"subjects": [
            {"id": "a", "kind": "person", "confidence": .99},
            {"id": "b", "kind": "person", "confidence": .99},
        ]}})
        await service.tick()
        assert service.snapshot("a")["presence_samples"] == 0
        assert len(model.calls) == 1
    asyncio.run(run())


def test_two_heads_have_independent_qualified_graph_calls():
    async def run():
        service, clients, _, _, model = setup(
            {"decision": "SPEAK", "topic": "公共内容一"},
            {"decision": "SPEAK", "topic": "公共内容二"})
        for rid in ("a", "b"):
            service.observe_visual(rid, person())
            service.observe_visual(rid, person())
        await service.tick()
        await asyncio.gather(*service.tasks.values())
        assert len(model.calls) == 2
        assert len(clients["a"].messages) == len(clients["b"].messages) == 1
        assert clients["a"].messages[0][2]["robot_id"] == "a"
        assert clients["b"].messages[0][2]["robot_id"] == "b"
    asyncio.run(run())


def test_late_plan_cannot_speak_after_user_interrupts_or_session_switches():
    async def run():
        service, clients, identities, _, model = setup(
            {"decision": "SPEAK", "topic": "公共话题"}, robots=("a",))
        service.observe_visual("a", person())
        service.observe_visual("a", person())
        model.gate = asyncio.Event()
        await service.tick()
        await asyncio.sleep(0)
        clients["a"]._last_user_turn_at = time.monotonic()
        clients["a"].speech_active = True
        model.gate.set()
        await service.tasks["a"]
        assert clients["a"].messages == []
        clients["a"].speech_active = True  # no second candidate during new user speech
        clients["a"]._last_user_turn_at -= 200
        await service.tick()
        assert service.snapshot("a")["attempts"] == 0
        identities["a"]["chat_id"] = "different"
        await service.tick()
        assert service.snapshot("a")["presence_samples"] == 0
        assert clients["a"].messages == []
    asyncio.run(run())


def test_disabled_has_no_worker_or_provider_calls():
    async def run():
        service, clients, _, _, model = setup(robots=("a",))
        service.graph = None
        await service.start()
        await service.tick()
        assert service.worker is None
        assert model.calls == clients["a"].messages == []
        assert not service.snapshot("a")["enabled"]
        await service.close()
    asyncio.run(run())


def test_enabled_idle_subscribes_each_head_own_visual_room(tmp_path):
    from annie_adapters import RoomServerConfig
    from annie_core import RobotConfig
    from annie_runtime import HarborRuntime

    from tests.test_phase5 import FakeSocket

    root = "ws://localhost:1/robot"
    channels = {name: "reb_" + name for name in (
        "main", "tts", "mic", "visualdata", "vision", "sam", "visualllm")}
    room = RoomServerConfig(
        base_url=root + "/roomv2", room_root=root, channels=channels,
        default_urls={name: root + "/roomv2/" + channel
                      for name, channel in channels.items()},
    )
    rt = HarborRuntime(
        [RobotConfig("a", room_id="roomv2"), RobotConfig("b", room_id="roomv3")],
        tmp_path / "idle-rooms.db", room, realtime_key="fake",
        realtime_socket_factory=FakeSocket, idle_model=Model(),
    )
    assert next(item.url for item in rt.playback_listeners["a"]
                if item.channel == "visualllm") == root + "/roomv2/reb_visualllm"
    assert next(item.url for item in rt.playback_listeners["b"]
                if item.channel == "visualllm") == root + "/roomv3/reb_visualllm"


def test_chat_switch_needs_new_chat_user_turn_before_any_planning():
    async def run():
        service, clients, identities, _, model = setup(
            {"decision": "SPEAK", "topic": "公共话题"}, robots=("a",))
        clients["a"]._last_user_turn_identity = dict(identities["a"])
        identities["a"]["chat_id"] = "new-chat"
        service.observe_visual("a", person())
        service.observe_visual("a", person())
        await service.tick()
        assert len(model.calls) == 0
        assert service.snapshot("a")["reason"] == "waiting_user_turn"
        clients["a"]._last_user_turn_identity = dict(identities["a"])
        clients["a"]._last_user_turn_at = time.monotonic() - 200
        clients["a"]._last_conversation_activity_t = clients["a"]._last_user_turn_at
        await service.tick()
        service.observe_visual("a", person())
        service.observe_visual("a", person())
        await settle(service, "a")
        assert len(model.calls) == 1
        assert clients["a"].messages[0][2]["chat_id"] == "new-chat"
    asyncio.run(run())


def test_runtime_idle_submission_is_internal_and_scoped_to_own_robot(tmp_path):
    from annie_core import RobotConfig
    from annie_runtime import HarborRuntime

    from tests.test_phase5 import FakeSocket
    from tests.test_phase7_runtime import Publisher, room_server

    async def run():
        model = Model({"decision": "SPEAK", "topic": "桌上有一本书"})
        rt = HarborRuntime(
            [RobotConfig("a", room_id="roomv2"), RobotConfig("b", room_id="roomv3")],
            tmp_path / "idle.db", room_server(), realtime_key="fake",
            realtime_socket_factory=FakeSocket, tts_voice="fake",
            room_tts_factory=Publisher, idle_model=model, idle_seconds=0,
        )
        rt.idle.presence_seconds = 0
        await rt.start()
        try:
            for _ in range(100):
                if all(client.ready for client in rt.realtime.values()):
                    break
                await asyncio.sleep(.01)
            assert all(client.ready for client in rt.realtime.values())
            rt._current_visual = lambda robot_id: "现场有人" if robot_id == "a" else None
            a, b = rt.realtime["a"], rt.realtime["b"]
            a._last_user_turn_at = time.monotonic() - 120
            a._last_user_turn_identity = a.robot.session_state()
            a._last_conversation_activity_t = a._last_user_turn_at
            await rt.idle.tick()  # synchronize session ownership first
            rt.idle.observe_visual("a", person(("book",)))
            rt.idle.observe_visual("a", person(("book",)))
            await rt.idle.tick()
            await rt.idle.tasks["a"]
            assert len(model.calls) == 1
            assert a.turn_id is not None
            turn = a.robot.cached_turn(a.turn_id)
            assert turn["turn_source"] == "external"
            assert turn["user_text"] == ""
            assert "桌上有一本书" in turn["external_prompt"]
            assert "不透露任何聊天或私人记忆" in turn["external_prompt"]
            assert not any(packet["type"] == "response.create"
                           for packet in b.socket.outbound)
            assert rt.external_events("a")["idle"]["status"] == "submitted_unknown"
            assert rt.external_events("b")["idle"]["attempts"] == 0
        finally:
            await rt.close()

    asyncio.run(run())


def test_missing_or_invalid_person_id_cannot_count_as_presence():
    async def run():
        service, clients, _, _, model = setup(
            {"decision": "SPEAK", "topic": "公共话题"}, robots=("a",))
        for bad_id in (None, "", "  ", "x" * 129):
            bad = person()
            bad["facts"]["subjects"][0]["id"] = bad_id
            service.observe_visual("a", bad)
            service.observe_visual("a", bad)
            await service.tick()
            assert service.snapshot("a")["presence_samples"] == 0
        assert model.calls == clients["a"].messages == []
    asyncio.run(run())


def test_presence_requires_spaced_observations_not_only_idle_time():
    async def run():
        service, clients, _, _, model = setup(
            {"decision": "SPEAK", "topic": "公共话题"}, robots=("a",))
        service.presence_seconds = 1
        service.observe_visual("a", person(("book",)))
        service.observe_visual("a", person(("book",)))
        row = service.rows["a"]
        row["presence_since"] = row["presence_at"] - .05
        await service.tick()
        assert service.snapshot("a")["reason"] == "person_unconfirmed"
        assert model.calls == []
        row["presence_since"] = row["presence_at"] - 1.1
        await settle(service, "a")
        assert len(clients["a"].messages) == 1
    asyncio.run(run())


def test_second_scene_change_requires_two_spaced_observations():
    async def run():
        service, clients, _, _, model = setup(
            {"decision": "SPEAK", "topic": "看书"},
            {"decision": "SPEAK", "topic": "喝茶", "new_value": True}, robots=("a",))
        service.presence_seconds = 1
        service.observe_visual("a", person(("book",)))
        service.observe_visual("a", person(("book",)))
        row = service.rows["a"]
        row["presence_since"] = row["presence_at"] - 1.1
        row["scene_since"] = row["presence_at"] - 1.1
        await settle(service, "a")
        service.on_event("a", {"kind": "tts.playback.started", "turn_id": "fake",
                               "source": "speaker", **identity("a")})
        service.on_event("a", {"kind": "tts.playback.drained", "turn_id": "fake",
                               "source": "speaker", **identity("a")})
        service.observe_visual("a", person(("cup",)))
        await settle(service, "a")
        assert service.snapshot("a")["reason"] == "second_no_new_evidence"
        service.observe_visual("a", person(("cup",)))
        await settle(service, "a")
        assert service.snapshot("a")["reason"] == "second_no_new_evidence"
        row["scene_since"] = row["presence_at"] - 1.1
        await settle(service, "a")
        assert len(model.calls) == len(clients["a"].messages) == 2
    asyncio.run(run())


def test_speech_preemption_during_submit_does_not_rearm_followup():
    async def run():
        service, clients, _, _, model = setup(
            {"decision": "SPEAK", "topic": "看书"}, robots=("a",))
        client = clients["a"]
        gate = asyncio.Event()
        submitted = asyncio.Event()

        async def slow_submit(text, *, source, expected_identity):
            submitted.set()
            await gate.wait()
            return {"status": "accepted", "turn_id": "fake"}

        client.submit_text = slow_submit
        service.observe_visual("a", person())
        service.observe_visual("a", person())
        await service.tick()
        await submitted.wait()
        service.on_event("a", {"kind": "speech.started", **identity("a")})
        gate.set()
        await service.tasks["a"]
        assert service.snapshot("a")["attempts"] == 2
        assert service.snapshot("a")["reason"] == "speech_preempted"
        await service.tick()
        assert len(model.calls) == 1
    asyncio.run(run())
