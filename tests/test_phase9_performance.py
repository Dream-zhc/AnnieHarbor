"""Phase 9 bounded diagnostics / resource sampler; no cloud or hardware calls."""

import asyncio
from types import SimpleNamespace

from annie_core import RobotConfig
from annie_observability import ProcessProbe
from annie_runtime import HarborRuntime
from annie_runtime.realtime import RealtimeRobot


def client_for(robot_id):
    identity = {"robot_id": robot_id, "chat_id": "test", "persona_id": "annie",
                "session_epoch": 1, "generation": 1}
    robot = SimpleNamespace(
        config=SimpleNamespace(robot_id=robot_id), turn_cache={},
        chat_id="test", persona_id="annie", generation=1,
        session_state=lambda: dict(identity),
    )
    return RealtimeRobot(robot, url="wss://example.test/realtime", key="fake")


def emit_turn(client, turn_id, *, mode="text", status="completed", speaker=False):
    audio = mode == "audio"
    client.publish("turn.started", turn_id=turn_id, user_text="test",
                   turn_source="external" if mode == "external" else "user",
                   speech_started_ms=1000 if audio else None,
                   speech_stopped_ms=1500 if audio else None,
                   asr_final_ms=1650 if audio else None)
    client.publish("llm.request_started", turn_id=turn_id)
    client.publish("text.delta", turn_id=turn_id, text="test")
    client.publish("tts.text_submitted", turn_id=turn_id)
    client.publish("tts.first_audio", turn_id=turn_id)
    client.publish("tts.first_published", turn_id=turn_id)
    if speaker:
        client.publish("tts.playback.started", turn_id=turn_id, source="speaker")
        client.publish("tts.playback.drained", turn_id=turn_id, source="speaker")
    else:
        client.publish("tts.playback.started", turn_id=turn_id, source="browser_test")
    client.publish("turn.done", turn_id=turn_id, status=status)


def test_128_turns_bounded_and_failures_do_not_disappear():
    a, b = client_for("a"), client_for("b")
    a.speech = SimpleNamespace(snapshot=lambda: {"state": "idle"})
    for index in range(160):
        status = "failed" if index % 10 == 0 else "cancelled" if index % 10 == 1 else "completed"
        emit_turn(a, f"a-{index}", mode="audio" if index % 2 else "text",
                  status=status, speaker=index % 3 == 0)
    emit_turn(b, "b-1", mode="text", speaker=False)
    window = a.diagnostic_snapshot()["latency_window"]
    assert window["turns"] == 128
    assert len(a._turn_timelines) == len(a._timeline_order) == 128
    assert sum(window["statuses"].values()) == 128
    assert window["statuses"]["failed"] > 0
    assert window["statuses"]["cancelled"] > 0
    assert window["speaker_receipt_missing"] > 0
    assert window["by_input"] == {"audio": 64, "text": 64, "external": 0}
    percentiles = a.snapshot()["latency_percentiles"]
    assert percentiles["endpoint_to_asr_final_ms"]["samples"] == 64
    assert percentiles["endpoint_to_asr_final_ms"]["missing"] == 0
    assert percentiles["room_to_speaker_drained_ms"]["missing"] > 0
    assert percentiles["llm_request_to_first_text_ms"]["p95_ms"] is not None
    assert b.snapshot()["latency_window"]["turns"] == 1
    assert all(not item.startswith("a-") for item in b._timeline_order)


def test_unknown_and_browser_receipts_never_invent_device_latency():
    client = client_for("a")
    emit_turn(client, "text-only", mode="text", speaker=False)
    timeline = client.snapshot()["last_turn_timeline"]
    assert timeline["durations"]["endpoint_to_asr_final_ms"] is None
    assert timeline["durations"]["room_to_speaker_start_ms"] is None
    assert timeline["durations"]["room_to_speaker_drained_ms"] is None
    assert timeline["duration_sources"]["room_to_speaker_start_ms"] == "unobserved"
    assert timeline["durations"]["llm_request_to_first_text_ms"] >= 0
    assert timeline["duration_sources"]["llm_request_to_first_text_ms"] == "local_monotonic"
    stats = client.snapshot()["latency_percentiles"]
    assert stats["endpoint_to_speaker_start_ms"]["samples"] == 0
    assert stats["llm_request_to_first_text_ms"]["samples"] == 1


def test_process_probe_only_samples_when_started_and_closes_cleanly():
    async def run():
        probe = ProcessProbe(interval=.01)
        assert probe.snapshot()["samples"] == 0
        probe.start()
        await asyncio.sleep(.06)
        metrics = probe.snapshot()
        assert metrics["samples"] >= 1
        assert metrics["cpu_percent"] is not None
        assert metrics["loop_lag_ms"] >= 0
        assert metrics["loop_lag_max_ms"] >= metrics["loop_lag_ms"]
        await probe.close()
        assert probe.task is None
        assert probe.snapshot()["samples"] == metrics["samples"]

    asyncio.run(run())


def test_runtime_health_reports_real_sampling_and_queue_capacity(tmp_path):
    async def run():
        runtime = HarborRuntime([RobotConfig("a", room_id="roomv2")],
                                tmp_path / "phase9.sqlite")
        runtime.process_probe.interval = .01
        await runtime.start()
        try:
            await asyncio.sleep(.05)
            health = runtime.health()
            performance = health["performance"]
            assert performance["samples"] >= 1
            assert isinstance(performance["cpu_percent"], float)
            assert performance["loop_lag_ms"] >= 0
            assert health["storage"]["trace_queue_capacity"] > 0
            assert health["storage"]["ledger_queue_capacity"] > 0
            assert health["storage"]["trace_dropped"] == 0
        finally:
            await runtime.close()
        assert runtime.process_probe.task is None

    asyncio.run(run())
