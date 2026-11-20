"""Language/dialect from the Windows FireRedLID sidecar (user 11-20), shown on the workbench."""

import importlib.util
import json
import math
import struct
from pathlib import Path

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
