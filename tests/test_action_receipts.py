"""Embedded reb_main action.started/finished/dropped receipts close the action loop."""

import asyncio
from types import SimpleNamespace

from annie_runtime import HarborRuntime

from tests.test_phase7_execution import FakePublisher, coordinator, wait_for


def receipt(kind, cid="turn-1", script="agree_micro_nod", **extra):
    # Wire shape as given by the embedded team (2026-10-22).
    return {"type": kind, "ts": 1787727448905,
            "data": {"script_id": script, "conversation_id": cid, "offset_ms": 200,
                     "tag": script, "duration_ms": 850, **extra}}


async def published(actions, pubs, a, turn="turn-1", call="call-1"):
    speech = SimpleNamespace(turn_id=None, frames=0, publisher=FakePublisher())
    await actions.request("A", call, turn, a.generation, "action", "agree_micro_nod",
                          speech=speech, user_text="你点下头")
    actions.on_first_pcm({"robot_id": "A", "turn_id": turn, "generation": 1})
    await wait_for(lambda: len(pubs["A"].sent) == 1)


def states(events):
    return [(data["status"], data["real_device_executed"], data["device_completion"])
            for _robot, kind, data in events if kind == "action.state"]


def test_started_then_finished_marks_the_published_action_done(tmp_path):
    async def run():
        actions, pubs, events, a, _b = coordinator(tmp_path)
        await actions.start()
        await published(actions, pubs, a)
        assert actions.on_receipt("A", receipt("action.started"))
        await wait_for(lambda: actions.receipts["applied"] == 1)
        assert actions.on_receipt("A", receipt("action.finished"))
        await wait_for(lambda: actions.receipts["applied"] == 2)
        assert (await actions.list("A"))[0]["status"] == "device_finished"
        assert states(events)[-2:] == [("device_started", True, "started"),
                                       ("device_finished", True, "finished")]
        finished = [data for _r, kind, data in events if kind == "action.state"][-1]
        assert (finished["device_offset_ms"], finished["device_duration_ms"],
                finished["device_ts_ms"]) == (200, 850, 1787727448905)
        # A late duplicate/out-of-order receipt never moves the action backwards.
        actions.on_receipt("A", receipt("action.started"))
        await wait_for(lambda: actions.receipts["ignored"] == 1)
        assert (await actions.list("A"))[0]["status"] == "device_finished"
        await actions.close()

    asyncio.run(run())


def test_dropped_keeps_the_device_reason_and_whether_motion_had_started(tmp_path):
    async def run():
        actions, pubs, events, a, _b = coordinator(tmp_path)
        await actions.start()
        await published(actions, pubs, a)
        actions.on_receipt("A", receipt("action.dropped", reason="cooldown"))
        await wait_for(lambda: actions.receipts["applied"] == 1)
        row = (await actions.list("A"))[0]
        assert (row["status"], row["error"]) == ("device_dropped", "cooldown")
        assert states(events)[-1] == ("device_dropped", False, "dropped")
        await actions.close()

        actions, pubs, events, a, _b = coordinator(tmp_path / "second")
        await actions.start()
        await published(actions, pubs, a, turn="turn-2", call="call-2")
        actions.on_receipt("A", receipt("action.started", cid="turn-2"))
        actions.on_receipt("A", receipt("action.dropped", cid="turn-2", reason="segment_change"))
        await wait_for(lambda: actions.receipts["applied"] == 2)
        assert states(events)[-1] == ("device_dropped", True, "dropped")
        await actions.close()

    (tmp_path / "second").mkdir()
    asyncio.run(run())


def test_receipts_match_only_this_robot_turn_and_script(tmp_path):
    async def run():
        actions, pubs, events, a, _b = coordinator(tmp_path)
        await actions.start()
        await published(actions, pubs, a)
        actions.on_receipt("B", receipt("action.finished"))  # other robot's Room
        actions.on_receipt("A", receipt("action.finished", cid="turn-0"))
        actions.on_receipt("A", receipt("action.finished", script="look_left"))
        await wait_for(lambda: actions.receipts["unmatched"] == 3)
        assert (await actions.list("A"))[0]["status"] == "published_unknown"
        # A and B receipts take separate locks: their order is not defined.
        assert sorted(data["action_value"] for _r, kind, data in events
                      if kind == "action.receipt_unmatched") == [
            "agree_micro_nod", "agree_micro_nod", "look_left"]
        for bad in ({"type": "action.finished"},
                    {"type": "action.finished", "data": {"script_id": "x" * 65,
                                                         "conversation_id": "turn-1"}},
                    {"type": "action.finished", "data": {"script_id": "agree_micro_nod"}}):
            assert not actions.on_receipt("A", bad)
        assert not actions.on_receipt("A", receipt("action.paused"))
        assert actions.receipts["invalid"] == 3
        await actions.close()

    asyncio.run(run())


def test_reb_main_routes_action_receipts_before_playback_handling():
    seen = []
    fake = SimpleNamespace(
        _room_external_event=lambda *args: None,
        actions=SimpleNamespace(on_receipt=lambda robot_id, message: seen.append(
            (robot_id, message["type"]))),
        realtime={},
    )
    HarborRuntime._room_playback_receipt(fake, "annie-a", receipt("action.finished"))
    HarborRuntime._room_playback_receipt(fake, "annie-a", {"type": "tts.playback.started"})
    assert seen == [("annie-a", "action.finished")]
