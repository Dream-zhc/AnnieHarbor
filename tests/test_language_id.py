"""Language/dialect from the Windows FireRedLID sidecar (user 11-20), shown on the workbench."""

import asyncio
import importlib.util
import json
import math
import random
import struct
from pathlib import Path

from annie_adapters import decode_room_binary_frame
from annie_runtime.room_monitor import RoomMonitor

ROOT = Path(__file__).resolve().parents[1]
SIDECAR = ROOT / "sidecars/annie-dialect-lid"


def sidecar():
    spec = importlib.util.spec_from_file_location("lid_service", SIDECAR / "lid_service.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tone(db, frames):
    amplitude = 32768 * 10 ** (db / 20)
    return [struct.pack("<320h", *(int(amplitude * math.sin(2 * math.pi * 220 * (i + 320 * f) / 16000))
                                   for i in range(320))) for f in range(frames)]


def test_the_room_shows_the_latest_language_and_ignores_other_rooms():
    clock = [100.0]
    room = RoomMonitor("roomv2", clock=lambda: clock[0])
    view = lambda: room.snapshot(sam_connected=True, visual_connected=False,
                                 visual_configured=False)["language"]
    assert view()["state"] == "waiting" and view()["latest"] is None
    room.on_sam({"type": "language_id_health", "room": "roomv2", "status": "ready",
                 "mic_connected": True, "device": "cuda", "queue": 0})
    room.on_sam({"type": "language_id", "room": "roomv3", "label": "zh mandarin"})  # robot B's
    room.on_sam({"type": "language_id", "room": "roomv2", "robot_id": "annie-a", "label": "zh yue",
                 "lang": "zh", "dialect": "yue", "name": "粤语", "confidence": 0.999,
                 "duration_s": 3.1, "latency_ms": 420, "seq": 7})
    clock[0] += 3
    state = view()
    assert state["state"] == "online" and state["device"] == "cuda" and state["count"] == 1
    assert state["latest"]["name"] == "粤语" and state["latest_age_ms"] == 3000
    room.on_sam({"type": "language_id_health", "room": "roomv2", "status": "ready",
                 "mic_connected": False})
    assert view()["state"] == "no_mic"
    clock[0] += 11
    assert view()["state"] == "offline"
    assert room.snapshot(sam_connected=False, visual_connected=False,
                         visual_configured=False)["language"]["state"] == "disconnected"


def test_the_sidecar_cuts_one_sentence_and_never_wakes_on_digital_silence():
    lid = sidecar()
    cut = lid.Segmenter(json.loads((SIDECAR / "config.json").read_text())["vad"])
    out = [cut.feed(frame) for frame in [bytes(640)] * 100]  # all-zero frames: never speech
    out += [cut.feed(frame) for frame in tone(-62, 50) + tone(-20, 100) + tone(-62, 40)]
    sentences = [s for s in out if s]
    assert len(sentences) == 1 and 2.0 <= len(sentences[0]) / 32000 <= 2.4  # 2 s + preroll
    assert cut.feed(bytes(640)) is None


def test_the_sidecar_message_names_the_dialect_and_keeps_the_robot():
    lid = sidecar()
    robot = {"robot_id": "annie-b", "room": "roomv3"}
    message = lid.result_message(robot, 4, {"lang": "zh xinan", "confidence": 0.9971},
                                 duration_s=3.81, latency_ms=310, ended_at_ms=1)
    assert message["room"] == "roomv3" and message["robot_id"] == "annie-b"
    assert (message["lang"], message["dialect"], message["confidence"]) == ("zh", "xinan", 0.997)
    assert message["name"].startswith("西南官话")
    assert lid.result_message(robot, 5, {"lang": "en", "confidence": 1}, duration_s=1,
                              latency_ms=1, ended_at_ms=1)["dialect"] is None


def test_the_sidecar_config_matches_one_room_per_robot():
    config = sidecar().load_config(SIDECAR / "config.json")
    rooms = {robot["robot_id"]: robot["room"] for robot in config["robots"]}
    assert rooms == {"annie-a": "roomv2", "annie-b": "roomv3"}
    # the private self-test room (no robot, brain or speaker in it) stays off unless someone turns it on
    declared = json.loads((SIDECAR / "config.json").read_text())["robots"]
    assert {r["room"]: r["enabled"] for r in declared if r["robot_id"] == "annie-test"} == {"lidtest": False}


def room_message(pcm):
    """What the real Room delivers for one microphone frame (annie_adapters.decode_room_binary_frame)."""
    return len(b"MIC_AUDIO").to_bytes(2, "big") + b"MIC_AUDIO" + pcm


def test_the_sidecar_unwraps_the_room_envelope_exactly_like_the_brain():
    lid = sidecar()
    pcm = bytes(range(256)) * 2 + bytes(128)
    rng = random.Random(7)
    messages = [room_message(pcm), room_message(b""), room_message(bytes(640)),
                len(b"OTHER").to_bytes(2, "big") + b"OTHER" + pcm,  # another binary type
                b"", b"\x00", b"\x00\x09MIC_AU",  # truncated
                (0).to_bytes(2, "big") + pcm,  # zero-length type
                *(bytes(rng.randrange(256) for _ in range(rng.randrange(0, 40))) for _ in range(200))]
    for message in messages:
        decoded = decode_room_binary_frame(message)
        expected = decoded[1] if decoded and decoded[0] == "MIC_AUDIO" else None
        assert (lid.mic_payload(message) or None) == (expected or None), message[:12]  # b"" = no sound
    assert lid.mic_payload(room_message(pcm)) == pcm


def test_a_silent_robot_microphone_in_the_room_envelope_never_reaches_the_model():
    """11-23 repro: read as raw PCM, the 11 header bytes of every message looked like speech."""
    lid = sidecar()
    vad = json.loads((SIDECAR / "config.json").read_text())["vad"]
    cut, found = lid.Segmenter(vad), []
    for _ in range(1100):  # 22 s of digital silence, as the board's AGC microphone sends in a quiet room
        pcm = lid.mic_payload(room_message(bytes(640)))
        found.append(cut.feed(pcm))
    assert not any(found)
    sentence = tone(-20, 100)  # 2 s of speech between two quiet stretches, same envelope
    stream = [bytes(640)] * 50 + sentence + [bytes(640)] * 50
    found = [cut.feed(lid.mic_payload(room_message(frame))) for frame in stream]
    heard = [speech for speech in found if speech]
    assert len(heard) == 1 and 2.0 <= len(heard[0]) / 32000 <= 2.4


def test_her_own_voice_is_audible_back_to_back_and_a_stop_ends_it():
    lid = sidecar()
    now = [100.0]
    playing = lid.Playing(clock=lambda: now[0])
    tts = lambda samples: {"type": "tts", "data": {"audio": "", "sample_rate": 24000, "start_sample": 0,
                                                   "end_sample": samples}}
    playing.feed(tts(24000 * 2))  # 2 s of audio arrives at t=100
    now[0] = 100.5
    playing.feed(tts(24000 * 3))  # arrives while the first still plays: 3 s more, back to back
    assert [[round(a, 2), round(b, 2)] for a, b in playing.spans] == [[100.0, 105.0]]
    now[0] = 103.0
    playing.feed({"type": "tts", "data": {"stop": True, "audio": ""}})
    assert round(playing.spans[-1][1], 2) == 103.0
    playing.feed({"type": "tts.playback.hold", "data": {}})  # other types and junk change nothing
    playing.feed({"type": "tts", "data": "x"})
    playing.feed("x")
    assert len(playing.spans) == 1
    assert lid.tts_seconds({"audio": base64_audio(2400), "sample_rate": 24000}) == 0.1  # fallback: audio length
    assert lid.tts_seconds({}) == 0.0


def base64_audio(samples):
    import base64

    return base64.b64encode(bytes(samples * 2)).decode()


def test_an_utterance_inside_her_voice_is_echo_but_a_quick_reply_is_not():
    lid = sidecar()
    playing = lid.Playing(clock=lambda: 0.0)
    playing.chunk(3.0, now=100.0)  # she is audible 100..103 (+ the margins)
    assert playing.share(100.4, 103.2) >= lid.ECHO_SHARE  # the mic hearing her: echo
    assert playing.share(98.0, 99.7) == 0.0 and playing.share(98.0, 99.9) < 0.05  # before she started
    assert playing.share(103.5, 106.5) < 0.2  # the user answers right after her last word
    assert playing.share(102.0, 107.0) < lid.ECHO_SHARE  # starting over her last second: still the user
    assert playing.share(5.0, 5.0) == 0.0


def test_the_service_skips_echo_and_counts_it_but_queues_the_users_sentence():
    lid = sidecar()
    config = {"robots": [{"robot_id": "annie-a", "room": "roomv2"}, {"robot_id": "annie-b", "room": "roomv3"}],
              "queue_max": 4}
    robot_a, robot_b = config["robots"]

    async def run():
        clock = [100.0]
        service = lid.Service(config, clock=lambda: clock[0])
        service.playing["roomv2"].chunk(3.0, now=100.0)
        speech = bytes(32000 * 2)
        service.enqueue(robot_a, speech, 100.2, 102.2)  # annie-a's own voice
        service.enqueue(robot_b, speech, 100.2, 102.2)  # annie-b was silent: its user is heard
        service.enqueue(robot_a, speech, 104.5, 106.5)  # annie-a's user, after she finished
        assert service.echo_skipped == 1 and service.queue.qsize() == 2
        assert [item[0]["robot_id"] for item in (service.queue.get_nowait(), service.queue.get_nowait())] == [
            "annie-b", "annie-a"]

    asyncio.run(run())


def test_the_heartbeat_reports_the_microphone_rate_and_skipped_echo():
    lid = sidecar()
    robot = {"robot_id": "annie-a", "room": "roomv2"}

    async def run():
        service = lid.Service({"robots": [robot], "queue_max": 4}, clock=lambda: 0.0)
        first = service.health_message(robot, now=10.0)
        assert first["mic_fps"] is None and first["mic_connected"] is False and first["status"] == "loading"
        service.mic_messages["roomv2"] += 100  # 100 frames in 2 s
        service.echo_skipped = 2
        second = service.health_message(robot, now=12.0)
        assert second["mic_fps"] == 50.0 and second["echo_skipped"] == 2 and second["room"] == "roomv2"
        assert service.health_message(robot, now=14.0)["mic_fps"] == 0.0  # the microphone went quiet

    asyncio.run(run())


def test_the_room_view_carries_the_microphone_rate_for_the_workbench():
    clock = [100.0]
    room = RoomMonitor("roomv2", clock=lambda: clock[0])
    room.on_sam({"type": "language_id_health", "room": "roomv2", "status": "ready", "mic_connected": True,
                 "mic_fps": 49.9, "echo_skipped": 4, "device": "cuda", "queue": 0})
    view = room.snapshot(sam_connected=True, visual_connected=False, visual_configured=False)["language"]
    assert (view["state"], view["mic_fps"], view["echo_skipped"]) == ("online", 49.9, 4)


def test_the_heartbeat_loop_publishes_for_every_robot_and_logs_the_stats_line(caplog):
    """The loop is shared by all robots: one exception in it would stop the whole service."""
    import logging

    lid = sidecar()
    config = {"robots": [{"robot_id": "annie-a", "room": "roomv2"}, {"robot_id": "annie-b", "room": "roomv3"}],
              "queue_max": 4, "health_interval_s": 60, "vad": json.loads((SIDECAR / "config.json").read_text())["vad"]}

    async def run():
        service = lid.Service(config)
        service.segmenters["roomv2"] = lid.Segmenter(config["vad"])  # roomv3's microphone is not connected yet
        sent = []

        async def publish(robot, message):
            sent.append((robot["robot_id"], message))

        service.publish = publish
        task = asyncio.create_task(service.health())
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return sent

    with caplog.at_level(logging.INFO, logger="lid"):
        sent = asyncio.run(run())
    assert [(robot, message["room"], message["type"]) for robot, message in sent] == [
        ("annie-a", "roomv2", "language_id_health"), ("annie-b", "roomv3", "language_id_health")]
    stats = [record.getMessage() for record in caplog.records if record.getMessage().startswith("stats ")]
    assert len(stats) == 2 and "vad_floor=-60dB" in stats[0] and "vad_floor=-" in stats[1]
    assert "mic_fps=None" in stats[0]  # the first heartbeat has no rate yet
