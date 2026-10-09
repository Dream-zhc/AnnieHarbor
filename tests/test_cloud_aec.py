"""Cloud AEC: real APM core and exact Room/physical-speaker invariants."""

import asyncio
import base64
import time

from annie_runtime.cloud_aec import CloudAec, ReferencePool, ReferenceTimeline, WebRtcAecBackend


def _tts(pcm, *, epoch=1, start=0, conversation_id="turn-a"):
    return {"type": "tts", "data": {
        "format": "pcm_s16le", "sample_rate": 24000,
        "audio": base64.b64encode(pcm).decode(), "start_sample": start,
        "conversation_id": conversation_id, "stream_epoch": epoch,
    }}


def _receipt(kind="started", *, cursor=0, epoch=1, source="speaker", seq=1):
    return {"type": f"tts.playback.{kind}", "data": {
        "source": source, "conversation_id": "turn-a", "stream_epoch": epoch,
        "seq": seq, "played_sample_count": cursor, "playback_id": "speaker-a",
    }}


def test_webrtc_binding_processes_two_real_10ms_native_frames():
    backend = WebRtcAecBackend(delay_ms=0)
    for _ in range(6):
        result = backend.process(bytes(320), bytes(320))
        assert isinstance(result, bytes) and len(result) == 320


def test_reference_needs_actual_speaker_receipt_and_bounded_age():
    timeline = ReferenceTimeline(offset_ms=0, max_receipt_age_ms=850)
    # Actual 24kHz TTS samples, 100 * 10ms frames, not the player output.
    timeline.render(_tts(bytes(480 * 100)))
    assert timeline.next_frame == 100
    assert timeline.receipt(_receipt(source="browser_test"), 10.0) is False
    assert timeline.receipt(_receipt(epoch=99), 10.0) is False
    assert timeline.reference(10.0) is None
    assert timeline.receipt(_receipt("progress", cursor=12000), 10.0) is True
    aligned = timeline.reference(10.02)
    assert aligned is not None and [len(part) for part in aligned] == [320, 320]
    assert timeline.reference(10.9) is None  # stale device acknowledgement
    assert timeline.receipt(_receipt("held", cursor=12500, seq=2), 10.9) is True
    assert timeline.reference(10.91) is None  # held speaker does not emit new reference
    timeline.reset()
    assert timeline.reference(11.0) is None


def test_new_tts_publish_never_displaces_previous_physical_playback_early():
    pool = ReferencePool(offset_ms=0)
    pool.render(_tts(bytes(480 * 100), epoch=1))
    assert pool.receipt(_receipt("started", cursor=10000, epoch=1), 10.0)
    assert pool.active == ("turn-a", 1)
    pool.render(_tts(bytes(480 * 100), epoch=2))  # network output is NOT a physical start
    assert pool.active == ("turn-a", 1)
    assert pool.reference(10.02) is not None
    assert pool.receipt(_receipt("started", cursor=10000, epoch=2), 10.03)
    assert pool.active == ("turn-a", 2)
    # A delayed stop for the old stream cannot silence the new speaker.
    assert pool.receipt(_receipt("stopped", cursor=10240, epoch=1, seq=2), 10.04)
    assert pool.active == ("turn-a", 2)
    assert pool.reference(10.05) is not None


def test_reference_tracks_single_transaction_and_bounds_memory():
    timeline = ReferenceTimeline()
    timeline.render(_tts(bytes(480 * 2)))
    assert timeline.next_frame == 2
    timeline.render(_tts(bytes(480 * 2)))  # duplicated PCM packet
    assert timeline.next_frame == 2
    timeline.render(_tts(bytes(480 * 2), start=480))
    assert timeline.next_frame == 4
    timeline.render(_tts(b"", epoch=2))  # empty stop/final must not steal identity
    assert timeline.identity == ("turn-a", 1)
    timeline.render(_tts(bytes(480 * 3), epoch=2))
    assert timeline.next_frame == 3
    assert timeline.identity == ("turn-a", 2)
    assert timeline.receipt(_receipt(epoch=1), 10.0) is False


def test_cloud_aec_worker_returns_exactly_one_processed_pcm_per_input():
    class FakeAec:
        calls = 0

        def process(self, reference, capture):
            assert len(reference) == 320 and len(capture) == 320
            self.calls += 1
            return b"\x11" * 320

    async def run():
        loop = asyncio.get_running_loop()
        output = []
        fake = FakeAec()
        aec = CloudAec(loop, lambda pcm, at: output.append((pcm, at)),
                       sync_offset_ms=0, backend_factory=lambda: fake)
        aec.start()
        try:
            aec.feed_tts(_tts(bytes(480 * 100)))
            aec.feed_receipt(_receipt("started", cursor=10000))
            assert aec.feed_mic(b"\x22" * 640)
            for _ in range(100):
                if output:
                    break
                await asyncio.sleep(.002)
            assert len(output) == 1
            assert output[0][0] == b"\x11" * 640
            assert fake.calls == 2
            assert aec.snapshot()["processed"] == 1
            aec.reset()
            aec.feed_mic(b"\x33" * 640)
            for _ in range(100):
                if len(output) > 1:
                    break
                await asyncio.sleep(.002)
            assert len(output) == 2
            assert output[-1][0] == b"\x33" * 640
            assert aec.snapshot()["bypassed"] == 1
        finally:
            aec.close()

    asyncio.run(run())


def test_inactive_aec_does_not_create_a_second_microphone_publisher():
    async def run():
        emitted = []
        aec = CloudAec(asyncio.get_running_loop(), lambda p, t: emitted.append(p))
        assert aec.feed_mic(bytes(640)) is False
        assert emitted == []
        assert aec.snapshot()["worker"] is False
        assert abs(time.monotonic()) > 0

    asyncio.run(run())
