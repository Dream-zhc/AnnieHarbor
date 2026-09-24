"""Phase 7: actual SQLite ownership and wire publication, fake transport only."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from annie_adapters import RuntimeStore
from annie_adapters.weather import AmapWeather
from annie_brain.action_catalog import load_action_catalog
from annie_core import RobotConfig
from annie_runtime.actions import ActionCoordinator, current_action_not_vetoed

ROOT = Path(__file__).resolve().parents[1]
CATALOG = load_action_catalog(ROOT / "config/action_scripts.json")


class FakePublisher:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail
        self.closed = False

    async def publish(self, event):
        if self.fail:
            raise TimeoutError("delivery uncertain")
        self.sent.append(event)

    async def close(self):
        self.closed = True


class BlockingPublisher(FakePublisher):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def publish(self, event):
        self.started.set()
        await self.release.wait()
        await super().publish(event)


def coordinator(tmp_path, *, enabled=True):
    a, b = SimpleNamespace(config=RobotConfig("A", room_id="roomv2"), generation=1), (
        SimpleNamespace(config=RobotConfig("B", room_id="roomv3"), generation=1)
    )
    publishers = {"A": FakePublisher(), "B": FakePublisher()}
    events = []
    store = RuntimeStore(tmp_path / "actions.db")
    store.migrate()
    actions = ActionCoordinator(
        store,
        CATALOG, {"A": a, "B": b}, publishers,
        lambda robot_id, kind, **data: events.append((robot_id, kind, data)),
        enabled=enabled, allowlist=("agree_micro_nod", "look_left",
                                     "look_right", "fast_double_blink"),
    )
    return actions, publishers, events, a, b


async def wait_for(condition):
    for _ in range(80):
        if condition():
            return
        await asyncio.sleep(.01)
    raise AssertionError("async action did not complete")


def test_two_room_action_sends_once_after_first_pcm_and_survives_restart(tmp_path):
    async def run():
        actions, pubs, events, a, _b = coordinator(tmp_path)
        await actions.start()
        speech = SimpleNamespace(turn_id=None, frames=0, publisher=FakePublisher())
        result = await actions.request(
            "A", "call-1", "turn-1", a.generation, "action", "agree_micro_nod",
            speech=speech, user_text="你点下头",
        )
        assert result["status"] == "awaiting_audio_anchor"
        assert pubs["A"].sent == pubs["B"].sent == []
        actions.on_first_pcm({"robot_id": "A", "turn_id": "turn-1", "generation": 1})
        await wait_for(lambda: len(pubs["A"].sent) == 1)
        assert pubs["B"].sent == []
        assert pubs["A"].sent[0]["type"] == "action_trigger"
        assert pubs["A"].sent[0]["data"] == {
            "script_id": "agree_micro_nod", "offset_ms": 0,
            "conversation_id": "turn-1", "anchor_source": "robot_main_brain",
        }
        duplicate = await actions.request(
            "A", "call-1", "turn-1", 1, "action", "agree_micro_nod",
            speech=speech, user_text="你点下头",
        )
        assert duplicate["status"] == "duplicate_published_unknown"
        actions.on_first_pcm({"robot_id": "A", "turn_id": "turn-1", "generation": 1})
        await asyncio.sleep(.03)
        assert len(pubs["A"].sent) == 1
        assert (await actions.list("A"))[0]["status"] == "published_unknown"
        assert any(event[2].get("device_completion") == "unknown" for event in events)
        await actions.close()

        reopened, pubs2, _events2, _a, _b = coordinator(tmp_path)
        await reopened.start()
        assert (await reopened.list("A"))[0]["status"] == "published_unknown"
        retried = await reopened.request(
            "A", "call-1", "turn-1", 1, "action", "agree_micro_nod",
            speech=speech, user_text="你点下头",
        )
        assert retried["status"] == "duplicate_published_unknown"
        assert pubs2["A"].sent == []
        await reopened.close()

    asyncio.run(run())


def test_network_stall_does_not_hold_robot_lock_or_claim_cancelled_motion(tmp_path):
    async def run():
        actions, pubs, events, _a, _b = coordinator(tmp_path)
        blocked = pubs["A"] = BlockingPublisher()
        await actions.start()
        speech = SimpleNamespace(turn_id=None, frames=0, publisher=FakePublisher())
        await actions.request(
            "A", "slow", "turn-slow", 1, "action", "look_left",
            speech=speech, user_text="你看左边",
        )
        actions.on_first_pcm({
            "robot_id": "A", "turn_id": "turn-slow", "generation": 1,
        })
        await asyncio.wait_for(blocked.started.wait(), 1)
        # No motor cancellation wire exists. This is already send_unknown,
        # even though the socket has not yet finished the send.
        await asyncio.wait_for(actions.cancel_pending("A"), .3)
        assert (await actions.list("A"))[0]["status"] == "send_unknown"
        assert not any(e[2].get("status") == "cancelled_before_send" for e in events
                       if e[2].get("call_id") == "slow")
        # B's unrelated action remains free to reserve and publish.
        assert (await actions.request(
            "B", "b-call", "b-turn", 1, "action", "look_right",
            speech=speech, user_text="你看右边",
        ))["status"] == "awaiting_audio_anchor"
        actions.on_first_pcm({"robot_id": "B", "turn_id": "b-turn", "generation": 1})
        await wait_for(lambda: len(pubs["B"].sent) == 1)
        blocked.release.set()
        await wait_for(lambda: len(blocked.sent) == 1)
        await wait_for(lambda: next(iter(
            [row["status"] for row in actions.store.list_actions("A")]), ""
        ) == "published_unknown")
        await actions.close()

    asyncio.run(run())


def test_slow_expression_does_not_lock_other_calls_for_same_robot(tmp_path):
    async def run():
        actions, _pubs, _events, _a, _b = coordinator(tmp_path)
        await actions.start()
        blocked = BlockingPublisher()
        speech = SimpleNamespace(turn_id=None, frames=0, publisher=blocked)
        in_flight = asyncio.create_task(actions.request(
            "A", "face-slow", "face-turn", 1, "expression", "喜",
            speech=speech, user_text="给我一个开心表情",
        ))
        await asyncio.wait_for(blocked.started.wait(), 1)
        await asyncio.wait_for(actions.cancel_pending("A"), .3)
        new = await asyncio.wait_for(actions.request(
            "A", "motion-after-face", "next-turn", 1, "action", "look_left",
            speech=speech, user_text="你看左边",
        ), .3)
        assert new["status"] == "awaiting_audio_anchor"
        blocked.release.set()
        assert (await in_flight)["status"] == "queued"
        await wait_for(
            lambda: any(
                row["call_id"] == "face-slow" and row["status"] == "published_unknown"
                for row in actions.store.list_actions("A")
            )
        )
        await actions.close()

    asyncio.run(run())


def test_cancel_old_generation_invalid_parameter_and_cooldown(tmp_path):
    async def run():
        actions, pubs, events, a, _b = coordinator(tmp_path)
        await actions.start()
        speech = SimpleNamespace(turn_id=None, frames=0, publisher=FakePublisher())
        with pytest.raises(ValueError):
            await actions.request("A", "bad", "t1", 1, "action", "invented_wave",
                                  speech=speech, user_text="给我挥挥手")
        with pytest.raises(ValueError):
            await actions.request("A", "unsafe", "t1", 1, "action", "look_left",
                                  speech=speech, user_text="你别看左边")
        assert (await actions.request(
            "A", "one", "t1", 1, "action", "look_left",
            speech=speech, user_text="你看左边",
        ))["status"] == "awaiting_audio_anchor"
        assert (await actions.request(
            "A", "two", "t2", 1, "action", "look_right",
            speech=speech, user_text="你看右边",
        ))["status"] == "conflict_or_cooldown"
        await actions.cancel_pending("A", "t1")
        actions.on_first_pcm({"robot_id": "A", "turn_id": "t1", "generation": 1})
        assert pubs["A"].sent == []
        assert (await actions.list("A"))[0]["status"] == "cancelled_before_send"
        a.generation = 2
        with pytest.raises(ValueError, match="stale_generation"):
            await actions.request("A", "stale", "t3", 1, "action", "look_left",
                                  speech=speech, user_text="你看左边")
        assert events[-1][0] == "A"
        await actions.close()

    asyncio.run(run())


def test_expression_preserves_tts_channel_and_unknown_device_result(tmp_path):
    async def run():
        actions, _pubs, _events, _a, _b = coordinator(tmp_path)
        await actions.start()
        tts = FakePublisher()
        speech = SimpleNamespace(turn_id=None, frames=0, publisher=tts)
        result = await actions.request("A", "face-1", "t-face", 1,
                                       "expression", "喜", speech=speech,
                                       user_text="你给我做个开心表情")
        assert result["status"] == "queued"
        await wait_for(lambda: len(tts.sent) == 1)
        assert result["real_device_executed"] is False
        assert tts.sent[0]["type"] == "expression"
        assert tts.sent[0]["data"] == {
            "expression": "喜", "turn_id": "t-face", "source": "robot_main_brain",
        }
        with pytest.raises(ValueError):
            await actions.request("A", "face-bad", "t-face", 1, "expression",
                                  "happy", speech=speech, user_text="你给我做个表情")
        await actions.close()

    asyncio.run(run())


def test_uncertain_transport_never_retries_or_claims_hardware_completion(tmp_path):
    async def run():
        actions, pubs, _events, _a, _b = coordinator(tmp_path)
        pubs["A"].fail = True
        await actions.start()
        speech = SimpleNamespace(turn_id=None, frames=0, publisher=FakePublisher())
        await actions.request("A", "uncertain", "turn", 1, "action", "look_left",
                              speech=speech, user_text="你看左边")
        actions.on_first_pcm({"robot_id": "A", "turn_id": "turn", "generation": 1})
        await wait_for(lambda: not actions.pending)
        await asyncio.sleep(.03)
        assert (await actions.list("A"))[0]["status"] == "send_unknown"
        assert (await actions.list("A"))[0]["error"] == "TimeoutError"
        await actions.close()

    asyncio.run(run())


@pytest.mark.parametrize("text, expected", [
    ("你点下头", True), ("来眨两下眼", True), ("看左边", True),
    ("我看见你点头了", True), ("你别看左边", False),
    ("我不想你点头", False), ("你不用给我眨眼", False),
    ("他刚才点头了", True), ("你好", True),
    ("你看左边的天气怎么样", True),
    ("你现在点头了吗", True),
    ("你刚刚看左边了吗", True),
    ("请你给我讲讲点头有什么含义", True),
    ("你做过哪些表情呀", True),
    ("你能往左看一下吗", True),
    ("你给我做个开心表情", True),
])
def test_physical_intent_gate(text, expected):
    assert current_action_not_vetoed(text) is expected


def test_runtime_veto_does_not_reimplement_qwen_semantics(tmp_path):
    async def run():
        actions, pubs, _events, _a, _b = coordinator(tmp_path)
        await actions.start()
        speech = SimpleNamespace(turn_id=None, frames=0, publisher=FakePublisher())
        for kind, value, user_text in (
            ("action", "look_left", "你别看左边"),
            ("action", "agree_micro_nod", "不要点头"),
            ("expression", "喜", "取消这个表情"),
        ):
            with pytest.raises(ValueError, match="vetoed"):
                await actions.request(
                    "A", "rejected-" + value, "turn", 1, kind, value,
                    speech=speech, user_text=user_text,
                )
        with pytest.raises(ValueError):
            await actions.request(
                "A", "invented", "turn", 1, "action", "invented_wave",
                speech=speech, user_text="你做个动作",
            )
        assert (await actions.list("A")) == []
        assert pubs["A"].sent == pubs["B"].sent == []
        await actions.close()

    asyncio.run(run())


def test_already_started_tts_does_not_create_an_unfulfillable_action_anchor(tmp_path):
    async def run():
        actions, pubs, _events, _a, _b = coordinator(tmp_path)
        await actions.start()
        speech = SimpleNamespace(turn_id="current", frames=1,
                                 publisher=FakePublisher())
        late = await actions.request(
            "A", "late-motion", "current", 1, "action", "look_left",
            speech=speech, user_text="你看左边",
        )
        assert late["status"] == "rejected_late"
        assert actions.pending == {}
        assert (await actions.list("A"))[0]["status"] == "rejected_late"
        actions.on_first_pcm({"robot_id": "A", "turn_id": "current", "generation": 1})
        await asyncio.sleep(.02)
        assert pubs["A"].sent == []
        await actions.close()

    asyncio.run(run())


def test_first_pcm_in_flight_rejects_late_motion_anchor(tmp_path):
    async def run():
        actions, pubs, _events, _a, _b = coordinator(tmp_path)
        await actions.start()
        speech = SimpleNamespace(turn_id="current", frames=0,
                                 inflight_end=480, publisher=FakePublisher())
        late = await actions.request(
            "A", "late-inflight", "current", 1, "action", "look_left",
            speech=speech, user_text="你看左边",
        )
        assert late["status"] == "rejected_late"
        actions.on_first_pcm({"robot_id": "A", "turn_id": "current",
                              "generation": 1})
        await asyncio.sleep(.02)
        assert pubs["A"].sent == []
        await actions.close()

    asyncio.run(run())


def test_old_tts_cannot_receive_new_turn_expression(tmp_path):
    async def run():
        actions, _pubs, _events, _a, _b = coordinator(tmp_path)
        await actions.start()
        worker = SimpleNamespace(done=lambda: False)
        tts = FakePublisher()
        speech = SimpleNamespace(turn_id="old-turn", frames=80,
                                 worker=worker, publisher=tts)
        reply = await actions.request(
            "A", "face-late", "new-turn", 1, "expression", "喜",
            speech=speech, user_text="你给我做个开心表情",
        )
        assert reply["status"] == "rejected_late"
        assert tts.sent == []
        await actions.close()

    asyncio.run(run())


def test_completed_previous_tts_allows_next_turn_expression(tmp_path):
    async def run():
        actions, _pubs, _events, _a, _b = coordinator(tmp_path)
        await actions.start()
        tts = FakePublisher()
        speech = SimpleNamespace(
            turn_id="finished-turn", frames=80, inflight_end=9600,
            worker=SimpleNamespace(done=lambda: True), publisher=tts,
        )
        result = await actions.request(
            "A", "next-face", "next-turn", 1, "expression", "喜",
            speech=speech, user_text="你给我做个开心表情",
        )
        assert result["status"] == "queued"
        await wait_for(lambda: len(tts.sent) == 1)
        assert tts.sent[0]["data"]["turn_id"] == "next-turn"
        await actions.close()

    asyncio.run(run())


def test_first_pcm_in_flight_rejects_expression_before_frames_increment(tmp_path):
    async def run():
        actions, _pubs, _events, _a, _b = coordinator(tmp_path)
        await actions.start()
        tts = FakePublisher()
        # SpeechOutput sets inflight_end BEFORE waiting for the room WebSocket.
        # frames is incremented only AFTER the WebSocket send returns.
        speech = SimpleNamespace(turn_id="face-turn", frames=0,
                                 inflight_end=480, publisher=tts)
        result = await actions.request(
            "A", "face-race", "face-turn", 1, "expression", "喜",
            speech=speech, user_text="请给我一个开心表情",
        )
        assert result["status"] == "rejected_late"
        assert tts.sent == []
        assert (await actions.list("A"))[0]["status"] == "rejected_late"
        await actions.close()

    asyncio.run(run())


def test_crash_retires_orphaned_sqlite_requests_without_replaying_motion(tmp_path):
    async def run():
        first, pubs, _events, _a, _b = coordinator(tmp_path)
        await first.start()
        speech = SimpleNamespace(turn_id=None, frames=0, publisher=FakePublisher())
        await first.request(
            "A", "old-call", "old-turn", 1, "action", "agree_micro_nod",
            speech=speech, user_text="你点下头",
        )
        # Deliberately do not close the first process; emulate hard termination
        # by reopening its DB before any graceful cancel runs.
        recovered, new_pubs, _events, _a, _b = coordinator(tmp_path)
        await recovered.start()
        old = (await recovered.list("A"))[0]
        assert old["status"] == "expired_no_pcm"
        assert old["error"] == "process_restarted"
        accepted = await recovered.request(
            "A", "new-call", "new-turn", 1, "action", "look_right",
            speech=speech, user_text="你看右边",
        )
        assert accepted["status"] == "awaiting_audio_anchor"
        assert pubs["A"].sent == new_pubs["A"].sent == []
        await first.close()
        await recovered.close()

    asyncio.run(run())


def test_amap_weather_uses_provider_data_without_secret_leakage():
    async def run():
        urls = []

        def handler(request):
            urls.append(request.url)
            return httpx.Response(200, json={"status": "1", "lives": [{
                "city": "北京", "adcode": "110000", "weather": "晴",
                "temperature": "22", "humidity": "32", "winddirection": "北",
                "windpower": "2", "reporttime": "2026-09-22 09:00:00",
            }]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            tool = AmapWeather("test-secret", client=client)
            result = await tool.current("北京")
            assert result["temperature_c"] == "22"
            assert result["city"] == "北京"
            assert "test-secret" not in str(result)
            assert urls[0].params["city"] == "北京市"
            assert urls[0].params["extensions"] == "base"
            assert urls[0].host == "restapi.amap.com"
            with pytest.raises(ValueError):
                await tool.current("../北京")

    asyncio.run(run())


def test_amap_weather_failure_explains_provider_rejection_without_key():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={
                "status": "0", "infocode": "10001", "info": "INVALID_USER_KEY",
            })
        )) as client:
            tool = AmapWeather("test-secret", client=client)
            result = await tool.current("西安")
            assert result == {"status": "unavailable", "source": "amap",
                              "reason": "provider_rejected", "infocode": "10001"}
            assert "test-secret" not in str(result)

    asyncio.run(run())
