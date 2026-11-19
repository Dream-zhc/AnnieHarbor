"""Habituation (11-19): the same poke again soon gets a few words, then only a small motion.

11-16 16:20-16:26: fifteen approaches in six minutes got fifteen quips.
"""

import asyncio
from types import SimpleNamespace

from annie_brain.event_policy import BRIEF_AGAIN, EventPolicy
from annie_core import NormalizedEvent
from annie_runtime.events import EventService
from annie_runtime.motions import InteractionMotions

NOW = 1_000_000


def router(policy):
    def route(kind, payload, at):
        return policy.route(NormalizedEvent(kind, payload), robot_id="a",
                            transport="verified:a", identity={}, now_ms=NOW + at)
    return route


def test_the_same_spot_touched_again_soon_fades_to_a_motion():
    route = router(EventPolicy(habituation=True))
    first = route("sensor.touch", {"id": 4, "name": "肩膀拍打", "level": 1}, 0)
    assert first["decision"] == "need_response" and BRIEF_AGAIN not in first["prompt"]
    second = route("sensor.touch", {"id": 4, "level": 1}, 4_000)
    assert second["decision"] == "need_response" and second["prompt"].endswith(BRIEF_AGAIN)
    third = route("sensor.touch", {"id": 4, "level": 1}, 8_000)
    assert (third["decision"], third["reason"], third["prompt"], third["stimulus"]) == (
        "habituated", "habituated", None, "body")
    other = route("sensor.touch", {"id": 1, "name": "额头", "level": 1}, 9_000)
    assert other["decision"] == "need_response" and BRIEF_AGAIN not in other["prompt"]
    hard = route("sensor.touch", {"id": 4, "level": 5}, 12_000)
    assert hard["decision"] == "need_response" and hard["prompt"].endswith(BRIEF_AGAIN)
    assert route("sensor.touch", {"id": 4, "level": 1}, 16_000)["decision"] == "habituated"
    rested = route("sensor.touch", {"id": 4, "level": 1}, 16_000 + 120_000)
    assert rested["decision"] == "need_response" and BRIEF_AGAIN not in rested["prompt"]


def test_someone_staying_close_is_remarked_on_twice_then_only_seen():
    route = router(EventPolicy(habituation=True))
    packet = {"id": 5, "name": "距离过近"}
    replies = [route("sensor.radar", packet, at) for at in range(0, 6 * 20_000, 20_000)]
    assert [row["decision"] for row in replies] == [
        "need_response", "need_response", "habituated", "habituated", "habituated", "habituated"]
    assert replies[1]["prompt"].endswith(BRIEF_AGAIN) and replies[2]["stimulus"] == "radar"
    assert route("sensor.radar", packet, 100_000 + 5_000)["reason"] == "cooldown"
    assert route("sensor.radar", packet, 100_000 + 121_000)["decision"] == "need_response"


def test_a_lingering_smell_also_fades_and_without_the_option_nothing_changes():
    route = router(EventPolicy(habituation=True))
    assert [route("sensor.gas", {"level": 1}, at)["decision"] for at in (0, 11_000, 22_000)] == [
        "need_response", "need_response", "habituated"]
    plain = router(EventPolicy())
    assert [plain("sensor.touch", {"id": 4, "level": 1}, at)["decision"]
            for at in (0, 4_000, 8_000, 12_000)] == ["need_response"] * 4


class Client:
    def __init__(self):
        self.published = []

    def publish(self, kind, **fields):
        self.published.append((kind, fields))


class Motions:
    def __init__(self):
        self.calls = []

    def react_quietly(self, robot_id, client, stimulus):
        self.calls.append((robot_id, stimulus))
        return "shy"


def test_a_habituated_event_is_answered_with_a_motion_not_a_turn():
    identity = {"chat_id": "c", "persona_id": "annie", "session_epoch": 1, "generation": 1}
    inner = SimpleNamespace(seen=[], on_event=lambda robot_id, event: inner.seen.append(event.kind))
    rt = SimpleNamespace(robots={"a": SimpleNamespace(session_state=lambda: dict(identity))},
                         realtime={"a": Client()}, started=False, motions=Motions(),
                         inner_life=inner)
    service = EventService(rt, habituation=True)
    for _ in range(3):
        row = service.route("a", NormalizedEvent("sensor.touch", {"id": 4, "level": 1},
                                                 ts_ms=None), "verified:a")
        service.policies["a"].held.clear()
    assert row["decision"] == "habituated" and row["motion"] == "shy"
    assert rt.motions.calls == [("a", "body")]
    assert inner.seen == ["sensor.touch"] * 3
    assert not service.pending["a"]


class Actions:
    enabled = True
    allowlist = ("shy", "fast_double_blink")
    catalog = {"shy": {"name": "害羞"}, "fast_double_blink": {"name": "连眨两下"}}

    def __init__(self):
        self.performed = []

    async def perform_now(self, robot_id, call_id, conversation_id, generation, script, **_):
        self.performed.append(script)
        return {"status": "published_unknown"}


def test_the_quiet_reaction_is_allowed_awake_spaced_and_never_over_her_voice():
    async def run():
        rt = SimpleNamespace(robots={"a": None}, actions=Actions())
        motions = InteractionMotions(rt)
        client = SimpleNamespace(wake=None, social_state={}, speech=None,
                                 robot=SimpleNamespace(generation=1))
        assert motions.react_quietly("a", client, "body") in Actions.allowlist
        assert motions.react_quietly("a", client, "body") is None
        motions.last_quiet["a"] -= 10
        assert motions.react_quietly("a", client, "radar") == "fast_double_blink"
        motions.last_quiet["a"] -= 10
        client.speech = SimpleNamespace(turn_id="t1")
        assert motions.react_quietly("a", client, "body") is None
        client.speech, client.wake = None, SimpleNamespace(state="SLEEP")
        assert motions.react_quietly("a", client, "body") is None
        await asyncio.gather(*motions.tasks)
        assert len(rt.actions.performed) == 2 and motions.stats["a"]["quiet"] == 2
    asyncio.run(run())
