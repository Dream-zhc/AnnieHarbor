"""A recognised person who arrives is greeted once; waving never triggers a reply."""

import asyncio
import time

from annie_brain.event_policy import EventPolicy
from annie_core import NormalizedEvent
from annie_runtime.room_monitor import GREET_WINDOW_MESSAGES, PeopleSmoother

from tests.test_visual_observations import ME, message, person

IDENTITY = {"robot_id": "a", "chat_id": "c", "persona_id": "p", "session_epoch": 1, "generation": 1}


def arrival(name, now_ms):
    return NormalizedEvent(kind="vision.known_arrival", source="vision",
                           payload={"id": "person_1", "name": name}, ts_ms=now_ms, ttl_ms=10_000)


def test_policy_greets_each_known_person_once_per_cooldown_and_ignores_waving():
    policy, now = EventPolicy(greet_cooldown_ms=60_000), 1_000_000
    route = lambda event, at=now: policy.route(  # noqa: E731
        event, robot_id="a", transport="room:roomv2:visualllm", identity=IDENTITY, now_ms=at)
    row = route(arrival("张三", now))
    assert row["decision"] == "need_response" and row["listen_after"] is True
    assert "你认出张三刚来到你面前" in row["prompt"] and "摄像头" in row["prompt"]
    assert route(arrival("张三", now + 1000), now + 1000)["reason"] == "cooldown"
    assert route(arrival("李四", now + 1000), now + 1000)["decision"] == "need_response"
    assert route(arrival("张三", now + 61_000), now + 61_000)["decision"] == "need_response"
    assert route(arrival("", now))["decision"] == "context_only"
    wave = NormalizedEvent(kind="vision.gesture", source="vision", ts_ms=now,
                           payload={"gesture": {"state": "active", "name": "wave"}})
    waved = route(wave)
    assert waved["decision"] == "context_only" and waved["prompt"] is None
    assert waved["listen_after"] is False


def test_only_a_settled_newcomer_with_a_name_counts_as_arrived():
    from annie_runtime.room_monitor import observation_people

    def step(smoother, people):
        smoother.update(observation_people(message(people)["observations"]))
        return smoother.known_arrivals

    already = PeopleSmoother()
    assert [step(already, [ME]) for _ in range(6)] == [[]] * 6  # there at start: no greeting

    smoother = PeopleSmoother()
    for _ in range(4):
        step(smoother, [])  # an empty room, past warm-up
    seen = [step(smoother, [ME]) for _ in range(6)]
    assert seen[1] == [{"subject_id": "person_1", "name": "张三"}]
    assert sum(len(item) for item in seen) == 1  # staying is not arriving again

    stranger = PeopleSmoother()
    for _ in range(4):
        step(stranger, [])
    assert all(step(stranger, [person("person_7")]) == [] for _ in range(5))

    late = PeopleSmoother()
    for _ in range(4):
        step(late, [])
    unnamed = dict(ME, name=None, label="一位朋友", known=False)
    for _ in range(GREET_WINDOW_MESSAGES + 1):
        assert step(late, [unnamed]) == []
    assert [step(late, [ME]) for _ in range(3)] == [[]] * 3  # recognised mid-stay: no greeting


def test_arrival_is_greeted_through_the_event_path_and_then_she_listens(tmp_path):
    from annie_core import RobotConfig
    from annie_runtime import HarborRuntime
    from annie_runtime.wakeword import WakeAdmission

    from tests.test_decision_model import EchoSocket

    class Silent:
        async def detected(self, _pcm):
            return False

    async def run():
        rt = HarborRuntime([RobotConfig("a", room_id="roomv2")], tmp_path / "rt.db",
                           realtime_key="fake", realtime_socket_factory=EchoSocket,
                           greet_known_cooldown_minutes=30)
        await rt.start()
        try:
            client = rt.realtime["a"]
            for _ in range(150):
                if client.ready:
                    break
                await asyncio.sleep(.01)
            client.wake = WakeAdmission(Silent())
            client._last_conversation_activity_t = time.monotonic() - 60  # a quiet room

            async def greeted():
                for _ in range(100):
                    items = [e for e in client.socket.outbound if e["type"] == "conversation.item.create"
                             and "你认出张三" in str(e)]
                    if items:
                        return items
                    await asyncio.sleep(.01)
                return []

            for _ in range(4):
                rt._room_visual_event("a", message([]))
            for _ in range(2):
                rt._room_visual_event("a", message([ME]))
            assert await greeted()
            assert client.wake.state == "ACTIVE"
            assert client.wake.authorized_turn_id == client.turn_id
            turn = client.turn_id
            # With a physical speaker: the reply completes, then its drain receipt arrives.
            client.wake.reply_finished(has_speaker=True)
            assert client.wake.speaker_drained(turn)
            assert client.wake.state == "FOLLOWUP"  # the reply needs no wake word

            client._finish("completed")  # the greeting turn is over
            rt.events.policies["a"].cooldown.clear()
            client.wake.reset()
            client._last_conversation_activity_t = time.monotonic()  # someone is talking
            for _ in range(GREET_WINDOW_MESSAGES):
                rt._room_visual_event("a", message([]))
            for _ in range(2):
                rt._room_visual_event("a", message([dict(ME, subject_id="person_3")]))
            await asyncio.sleep(.1)
            waiting = [row for row in rt.events.snapshot("a")["events"]
                       if row["kind"] == "vision.known_arrival"]
            assert waiting[-1]["reason"] == "conversation_active"
        finally:
            await rt.close()
    asyncio.run(run())
