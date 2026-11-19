"""判停时序 (11-19): when the user's voice ended, against the provider's speech_stopped."""

import asyncio
import logging
import time
from array import array

import pytest
from annie_runtime.voice_timing import VoiceTiming, ms_between

from tests.test_wake_call_recovery import ready, runtime

LOUD = array("h", [8000, -8000] * 160).tobytes()
QUIET = array("h", [30, -30] * 160).tobytes()


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def feed(timing, clock, frames, start_t):
    """20 ms chunks sent back to back from start_t, numbered from sample 0."""
    sample = 0
    for index, pcm in enumerate(frames):
        clock.t = start_t + index * .02
        timing.sent(pcm, sample)
        sample += len(pcm) // 2


def speech(timing, clock, start_t):
    """0.2 s room, 1.0 s voice (last voiced chunk at +1.18 s), 0.8 s room."""
    feed(timing, clock, [QUIET] * 10 + [LOUD] * 50 + [QUIET] * 40, start_t)


def test_provider_offsets_map_back_to_the_moment_that_audio_left():
    clock = Clock(100.0)
    timing = VoiceTiming(clock=clock)
    speech(timing, clock, 100.0)
    assert timing.sent_at(0) == 100.0
    assert timing.sent_at(200) == pytest.approx(100.2)
    assert timing.sent_at(1990) == pytest.approx(101.98)
    assert timing.sent_at(2000) == pytest.approx(101.98)
    assert timing.sent_at(2001) is None
    for bad in (None, -1, 1.5, "200"):
        assert timing.sent_at(bad) is None
    timing.reset()
    assert timing.sent_at(0) is None


def test_the_local_ear_finds_where_the_voice_ended():
    clock = Clock(100.0)
    timing = VoiceTiming(clock=clock)
    speech(timing, clock, 100.0)
    assert timing.voice_end(100.0) == pytest.approx(101.18)
    assert timing.voice_end(101.5) is None
    assert timing.floor_dbfs() == pytest.approx(-60.8, abs=.2)
    assert ms_between(1.0, 1.25) == 250
    assert ms_between(None, 1.0) is None and ms_between(2.0, 1.0) is None


def test_each_turn_logs_how_long_the_provider_took_to_call_it_over(tmp_path, caplog):
    async def run():
        rt = runtime(tmp_path)
        client = await ready(rt)
        try:
            now = time.monotonic()
            clock = Clock(now)
            client.voice_timing = timing = VoiceTiming(clock=clock)
            speech(timing, clock, now - 2.0)
            g = client.robot.generation
            with caplog.at_level(logging.INFO, logger="annie_runtime.realtime"):
                await client.handle({"type": "input_audio_buffer.speech_started",
                                     "item_id": "u1", "audio_start_ms": 180}, g)
                await client.handle({"type": "input_audio_buffer.speech_stopped",
                                     "item_id": "u1", "audio_end_ms": 1600}, g)
                await client.handle({"type": "conversation.item.input_audio_transcription.completed",
                                     "item_id": "u1", "transcript": "你好呀"}, g)
            lines = [record.getMessage() for record in caplog.records
                     if record.getMessage().startswith("speech_vad ")]
            started, stopped = lines
            values = dict(part.split("=", 1) for part in stopped.split()[1:])
            assert "event=started" in started and "event=stopped" in stopped
            assert int(dict(part.split("=", 1) for part in started.split()[1:])[
                "start_lag_ms"]) == pytest.approx(1820, abs=80)
            assert values["invalid"] == "False" and values["speech_ms"] == "1420"
            assert int(values["endpoint_ms"]) == pytest.approx(400, abs=80)
            assert int(values["voice_end_to_stopped_ms"]) == pytest.approx(820, abs=80)
            assert int(values["audio_end_after_voice_ms"]) == pytest.approx(420, abs=5)
            snapshot = client._timeline_snapshot(client.turn_id)
            durations = snapshot["durations"]
            assert durations["voice_end_to_endpoint_ms"] == pytest.approx(820, abs=80)
            assert durations["provider_endpoint_ms"] == pytest.approx(400, abs=80)
            assert durations["voice_end_to_speaker_start_ms"] is None
            assert type(snapshot["times"]["voice_end_ms"]) is int
            assert "voice_end_to_endpoint_ms" in client._timeline_percentiles()
        finally:
            await rt.close()
    asyncio.run(run())


def test_a_dropped_utterance_leaves_no_voice_end_for_the_next_turn(tmp_path):
    async def run():
        rt = runtime(tmp_path)
        client = await ready(rt)
        try:
            now = time.monotonic()
            clock = Clock(now)
            client.voice_timing = timing = VoiceTiming(clock=clock)
            speech(timing, clock, now - 2.0)
            g = client.robot.generation
            await client.handle({"type": "input_audio_buffer.speech_started",
                                 "item_id": "u1", "audio_start_ms": 180}, g)
            await client.handle({"type": "input_audio_buffer.speech_stopped", "item_id": "u1",
                                 "audio_end_ms": 1600, "reason": "turn_invalid"}, g)
            assert client._last_voice_end_t is None and client._last_audio_end_sent_t is None
        finally:
            await rt.close()
    asyncio.run(run())
