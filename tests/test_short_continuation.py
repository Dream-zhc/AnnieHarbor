"""Private short continuation: physical receipts, opt-in and interrupt safety."""

import asyncio
import time
from types import SimpleNamespace

from annie_runtime.idle import ShortContinuation
from annie_brain.idle_plan import build_idle_graph


def identity(rid):
    return {"robot_id": rid, "chat_id": "chat", "persona_id": "annie",
            "session_epoch": 1, "generation": 1}


class Planner:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {
            "decision": "SPEAK", "topic": "继续聊喜欢读的书", "new_value": False}
        self.gate = None

    async def plan(self, state):
        self.calls.append(dict(state))
        if self.gate:
            await self.gate.wait()
        return self.result


class Client:
    def __init__(self):
        self.ready = True
        self.speech_active = False
        self.turn_id = None
        self.response_id = None
        self.speech = object()
        self.robot = SimpleNamespace(turn_cache={})
        self._turn_timelines = {}
        self._last_user_turn_id = None
        self.persona_snapshot = {"question_pressure": "active"}
        self._last_user_turn_at = time.monotonic() - 2
        self._last_provider_speech_started_t = None
        self.messages = []

    async def submit_text(self, text, *, source, expected_identity, text_only=False):
        self.messages.append((text, source, dict(expected_identity), text_only))
        return {"turn_id": "out", "status": "accepted"}


def build(*, enabled=True, delay=0, window=35, robots=("a", "b")):
    planner = Planner()
    clients = {rid: Client() for rid in robots}
    identities = {rid: identity(rid) for rid in robots}
    runtime = SimpleNamespace(
        started=True, realtime=clients,
        robots={rid: SimpleNamespace(coordinator=SimpleNamespace(playback_state="idle"))
                for rid in robots},
        reminders=SimpleNamespace(_speech_blocks=lambda _: False),
        events=SimpleNamespace(pending={}),
        session_state=lambda rid: dict(identities[rid]),
    )
    service = ShortContinuation(runtime, build_idle_graph(planner),
                                enabled=enabled, delay=delay, window=window)
    return service, clients, identities, planner


def drain(service, client, rid="a", *, source="speaker", text=None,
          turn_id="turn-a", user_text="我今天读了一本挺有意思的书，越看越想看。"):
    client.robot.turn_cache[turn_id] = {
        "turn_id": turn_id, "turn_source": "user", "status": "completed",
        "user_text": user_text,
        "assistant_generated_text": text or "听起来故事挺吸引人的。",
    }
    service.on_event(rid, {"kind": "tts.playback.drained",
                           "turn_id": turn_id, "source": source, **identity(rid)})


async def settle(service):
    await service.tick()
    for task in list(service.tasks.values()):
        await task


def test_no_continuation_without_speaker_receipt_and_user_opt_in():
    async def run():
        service, clients, _, model = build()
        await settle(service)
        drain(service, clients["a"], source="browser_test")
        await settle(service)
        assert model.calls == []
        drain(service, clients["a"])
        clients["a"].persona_snapshot["question_pressure"] = "normal"
        await settle(service)
        assert model.calls == []
        assert service.snapshot("a")["reason"] == "not_opted_in"
        clients["a"].persona_snapshot["question_pressure"] = "active"
        await settle(service)
        assert len(model.calls) == 1
        assert model.calls[0]["mode"] == "continuation"
        assert clients["a"].messages[0][1] == "external"
        assert clients["b"].messages == []
        await settle(service)
        assert len(model.calls) == 1
        drain(service, clients["a"])
        await settle(service)
        assert len(model.calls) == 1
        await service.close()

    asyncio.run(run())


def test_no_chasing_an_existing_question_or_stop_request():
    async def run():
        service, clients, _, planner = build(robots=("a",))
        drain(service, clients["a"], text="喜欢哪一部分呢？")
        await settle(service)
        assert service.snapshot("a")["status"] == "submitted_unknown"
        service.on_event("a", {"kind": "turn.started", "turn_source": "user"})
        clients["a"]._last_user_turn_at = time.monotonic()
        drain(service, clients["a"], text="这本书听着真不错。",
              turn_id="turn-b", user_text="小安妮退下，别说了")
        await settle(service)
        assert service.snapshot("a")["reason"] == "no_open_topic"
        assert len(planner.calls) == 1
        await service.close()

    asyncio.run(run())


def test_user_interruption_and_session_switch_discard_inflight_result():
    async def run():
        service, clients, identities, planner = build(robots=("a",))
        drain(service, clients["a"])
        planner.gate = asyncio.Event()
        await service.tick()
        await asyncio.sleep(0)
        service.on_event("a", {"kind": "speech.started", **identity("a")})
        planner.gate.set()
        await service.tasks["a"]
        assert clients["a"].messages == []
        drain(service, clients["a"])
        planner.gate = asyncio.Event()
        await service.tick()
        await asyncio.sleep(0)
        identities["a"]["chat_id"] = "other-chat"
        planner.gate.set()
        await service.tasks["a"]
        assert clients["a"].messages == []
        await service.close()

    asyncio.run(run())


def test_cooldown_expiration_busy_and_no_speak_are_bounded():
    async def run():
        service, clients, _, planner = build(robots=("a",), delay=15, window=35)
        drain(service, clients["a"])
        await settle(service)
        assert service.snapshot("a")["reason"] == "cooldown"
        service.rows["a"]["drained_at"] -= 17
        clients["a"].speech_active = True
        await settle(service)
        assert service.snapshot("a")["reason"] == "conversation_busy"
        clients["a"].speech_active = False
        planner.result = {"decision": "NO_SPEAK", "topic": "", "new_value": False}
        await settle(service)
        assert service.snapshot("a")["status"] == "model_no_speak"
        evidence = service.snapshot("a")["recent"]
        assert len(evidence) == 1
        assert evidence[0]["source_turn_id"] == "turn-a"
        assert evidence[0]["mode"] == "short_continuation"
        assert evidence[0]["decision"] == "NO_SPEAK"
        assert evidence[0]["model_ms"] is not None
        await settle(service)
        assert len(planner.calls) == 1
        service.on_event("a", {"kind": "turn.started", "turn_source": "user"})
        clients["a"]._last_user_turn_at = time.monotonic()
        drain(service, clients["a"], turn_id="turn-b")
        service.rows["a"]["drained_at"] -= 40
        await settle(service)
        assert service.snapshot("a")["reason"] == "window_expired"
        assert len(planner.calls) == 1
        await service.close()

    asyncio.run(run())


def test_disabled_short_continuation_is_inert():
    async def run():
        service, clients, _, planner = build(enabled=False, robots=("a",))
        drain(service, clients["a"])
        await settle(service)
        assert not planner.calls
        assert service.snapshot("a")["enabled"] is False
        await service.close()

    asyncio.run(run())


def test_visible_web_text_can_trigger_silent_continuation_without_speaker_receipt():
    async def run():
        service, clients, identities, planner = build(robots=("a",), delay=15)
        client = clients["a"]
        client._last_user_turn_id = "web-turn"
        client._turn_timelines["web-turn"] = {"input_mode": "text"}
        client.robot.turn_cache["web-turn"] = {
            "turn_id": "web-turn", "turn_source": "user",
            "user_text": "我昨天看了个特别有意思的电影",
            "assistant_generated_text": "听起来挺有趣，里面哪一段最打动你？",
            "updated_ms": time.time_ns() // 1_000_000, "status": "completed",
        }
        assert not service.viewed_text("a", "web-turn", {**identities["a"], "chat_id": "bad"})
        assert service.viewed_text("a", "web-turn", dict(identities["a"]))
        assert service.snapshot("a")["channel"] == "text"
        await settle(service)
        assert not planner.calls
        assert service.rows["a"]["reason"] == "cooldown"
        service.rows["a"]["drained_at"] -= 16
        assert service.viewed_text("a", "web-turn", dict(identities["a"]))
        await settle(service)
        assert len(planner.calls) == 1
        assert len(client.messages) == 1
        assert client.messages[0][-1] is True
        await settle(service)
        assert len(planner.calls) == 1
        await service.close()

    asyncio.run(run())


def test_hidden_webpage_and_new_user_preempt_web_continuation():
    async def run():
        service, clients, identities, planner = build(robots=("a",))
        client = clients["a"]
        client._last_user_turn_id = "web-turn"
        client._turn_timelines["web-turn"] = {"input_mode": "text"}
        client.robot.turn_cache["web-turn"] = {
            "turn_id": "web-turn", "turn_source": "user",
            "user_text": "今天工作太累了真的想聊聊",
            "assistant_generated_text": "听起来不轻松，最近挺辛苦。",
            "updated_ms": time.time_ns() // 1_000_000, "status": "completed",
        }
        assert service.viewed_text("a", "web-turn", dict(identities["a"]))
        service.rows["a"]["drained_at"] -= 17
        service.rows["a"]["visible_at"] -= 9
        await settle(service)
        assert service.snapshot("a")["reason"] == "web_not_visible"
        assert not planner.calls
        assert service.viewed_text("a", "web-turn", dict(identities["a"]))
        planner.gate = asyncio.Event()
        await service.tick()
        await asyncio.sleep(0)
        service.on_event("a", {"kind": "turn.started", "turn_source": "user"})
        client._last_user_turn_at = time.monotonic()
        planner.gate.set()
        await service.tasks["a"]
        assert client.messages == []
        await service.close()

    asyncio.run(run())
