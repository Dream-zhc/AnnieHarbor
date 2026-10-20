"""One microphone and one speaker per Room: observed from reports, frame rate and receipts."""

import asyncio

from annie_runtime.audio_devices import DEVICE_TTL_SECONDS, RoomAudioDevices


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def devices():
    clock, changes = Clock(), []
    registry = RoomAudioDevices(clock=clock, on_change=lambda status, snap: changes.append(status))
    return registry, clock, changes


def mic_report(instance, kind="progress", **extra):
    return {"type": "mic.capture." + kind, "data": {
        "sender": "microphone", "instance_id": instance, "host": "rdk", "device_name": "阵列",
        **extra}}


def presence(instance, sender="speaker", state="idle"):
    return {"type": "speaker.presence", "data": {"sender": sender, "instance_id": instance,
                                                 "state": state}}


def receipt(playback_id, source="speaker", cid="turn-1", epoch=1):
    return {"type": "tts.playback.progress", "data": {
        "protocol_version": 1, "conversation_id": cid, "stream_epoch": epoch,
        "played_sample_count": 2400, "sample_rate": 24000, "seq": 1, "source": source,
        "sender": source, "playback_id": playback_id}}


def frames(registry, clock, per_second, seconds):
    for _ in range(seconds):
        for index in range(per_second):
            clock.now += 1 / per_second
            registry.mic_frame()


def test_two_reporting_microphones_warn_until_one_stops():
    registry, clock, changes = devices()
    assert registry.report(mic_report("microphone-a"))
    assert registry.snapshot()["status"] == "ok"
    registry.report(mic_report("browser-microphone-x"))
    snap = registry.snapshot()
    assert snap["conflicts"] == [{"role": "microphone", "reason": "multiple_devices", "count": 2}]
    assert {row["kind"] for row in snap["microphones"]} == {"device", "browser"}
    registry.report(mic_report("browser-microphone-x", "stopped"))
    assert registry.snapshot()["status"] == "ok"
    assert changes == ["conflict", "ok"]


def test_a_device_that_stops_reporting_expires():
    registry, clock, _ = devices()
    registry.report(presence("speaker-a"))
    registry.report(presence("speaker-b", state="playing"))
    assert registry.snapshot()["status"] == "conflict"
    clock.now += DEVICE_TTL_SECONDS + .1
    assert registry.snapshot() == {"status": "ok", "conflicts": [], "microphones": [],
                                   "speakers": [], "mic_frames_per_second": None,
                                   "head": {"current": None, "heads": [], "handover": None}}


def test_old_microphones_without_reports_are_caught_by_frame_rate():
    registry, clock, changes = devices()
    frames(registry, clock, 50, 4)
    assert registry.snapshot()["status"] == "ok"  # one 20 ms stream
    frames(registry, clock, 100, 4)
    snap = registry.snapshot()
    assert snap["conflicts"][0]["reason"] == "frame_rate"
    assert snap["mic_frames_per_second"] >= 95
    clock.now += 3  # audio stopped: an old rate is not a second microphone
    assert registry.snapshot()["status"] == "ok"


def test_two_players_of_one_reply_are_two_speakers_including_the_web_audition():
    registry, clock, _ = devices()
    registry.playback_receipt(receipt("board-1"))
    assert registry.snapshot()["status"] == "ok"
    registry.playback_receipt(receipt("tab-1", source="browser_test"))
    conflict = registry.snapshot()["conflicts"][0]
    assert conflict == {"role": "speaker", "reason": "multiple_playbacks", "count": 2,
                        "sources": ["browser_test", "speaker"]}
    clock.now += 6
    registry.playback_receipt(receipt("board-1", cid="turn-2"))  # the next reply, one player
    assert registry.snapshot()["status"] == "ok"
    registry.report(presence("browser-speaker-y", sender="browser_test"))
    registry.report(presence("speaker-a"))
    assert registry.snapshot()["conflicts"][0]["reason"] == "multiple_devices"


def test_unrelated_voice_traffic_is_ignored_and_warnings_never_cost_audio():
    def boom(*_):
        raise RuntimeError("screen unavailable")
    registry = RoomAudioDevices(clock=Clock(), on_change=boom)
    assert not registry.report({"type": "voice.interaction", "data": {"sender": "brain"}})
    assert not registry.report({"type": "mic.capture.progress", "data": {"sender": "brain"}})
    registry.report(mic_report("a"))
    registry.report(mic_report("b"))  # on_change raises; report and mic_frame must not
    registry.mic_frame()
    assert registry.snapshot()["status"] == "conflict"


def test_inputs_and_big_screen_carry_the_device_state(tmp_path):
    from annie_core import RobotConfig
    from annie_runtime import HarborRuntime
    from annie_web.exhibit import exhibit_snapshot

    async def run():
        rt = HarborRuntime([RobotConfig("a", room_id="roomv2")], tmp_path / "rt.db")
        await rt.start()
        try:
            rt._room_voice_event("a", mic_report("microphone-a"))
            rt._room_voice_event("a", mic_report("microphone-b"))
            assert rt.input_stats("a")["audio_devices"]["status"] == "conflict"
            screen = await exhibit_snapshot(rt, "a")
            assert screen["audio"]["devices"]["conflicts"][0]["reason"] == "multiple_devices"
        finally:
            await rt.close()
    asyncio.run(run())
