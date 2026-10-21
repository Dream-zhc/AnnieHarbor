"""Two robot heads in one Room: the one started later takes over, the older one stops."""

import asyncio
import os
import re
import subprocess

import pytest

from annie_runtime.audio_devices import HANDOVER_GRACE_SECONDS, HEAD_TTL_SECONDS, RoomAudioDevices
from tests.test_room_microphone import ROOT, FakeRoom, sine, speaker

head_module = speaker.head_module()  # the module serve() itself uses


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def test_the_older_head_yields_to_a_newer_one_only():
    clock = Clock()
    doll = head_module.Head("obj_doll", "obj 娃娃小机器人", address="192.168.1.143", clock=clock)
    clock.now += 30
    big = head_module.Head("big_head", "大头机器人", address="192.168.1.188", clock=clock)
    clock.now += 1
    assert not big.hear(doll.active_message())  # older: it will hear us and stop
    assert not doll.hear(doll.active_message())  # its own echo
    assert not doll.hear({"type": "robot.head.active", "data": {"sender": "speaker"}})
    announced = big.active_message()
    assert announced["data"]["active_for_ms"] == 1000 and "microphone" not in announced["data"]
    assert doll.hear(announced)
    assert doll.taken_over_by == {"instance_id": big.instance_id, "head_id": "big_head",
                                  "head_name": "大头机器人", "host": big.host,
                                  "address": "192.168.1.188"}
    released = doll.released_message("taken_over")
    assert released["type"] == "robot.head.released"
    assert released["data"]["taken_over_by"]["head_name"] == "大头机器人"
    assert released["description"] == "obj 娃娃小机器人已断开：大头机器人接管"
    assert doll.identity() == {"head_instance": doll.instance_id, "head_id": "obj_doll",
                               "head_name": "obj 娃娃小机器人"}


def test_head_options_are_opt_in_and_validated(monkeypatch):
    assert speaker.parse_args([]).head == ""  # a Windows test speaker never takes over
    monkeypatch.setenv("ROBOT_HEAD", "big_head")
    monkeypatch.setenv("ROBOT_HEAD_NAME", "大头机器人")
    args = speaker.parse_args(["--mic"])
    assert (args.head, args.head_name) == ("big_head", "大头机器人")
    with pytest.raises(SystemExit):
        speaker.parse_args(["--head", "大头"])
    monkeypatch.delenv("ROBOT_HEAD")
    with pytest.raises(SystemExit):
        speaker.parse_args([])  # a name without a head id


def test_the_service_keeps_a_board_that_was_taken_over_stopped():
    unit = (ROOT / "room-speaker" / "install-service.sh").read_text()
    assert re.search(r"^Restart=always$", unit, re.M)
    assert f"RestartPreventExitStatus={head_module.EXIT_TAKEN_OVER}" in unit
    assert f"SuccessExitStatus={head_module.EXIT_TAKEN_OVER}" in unit  # inactive, not failed


def test_the_launcher_for_the_robot_main_project_drives_the_same_service():
    """annie-audio.sh is what the embedded main project calls on the board."""
    script = ROOT / "room-speaker" / "annie-audio.sh"
    text = script.read_text()
    unit = (ROOT / "room-speaker" / "install-service.sh").read_text()
    assert 'UNIT="${ANNIE_AUDIO_UNIT:-annie-room-audio}"' in text
    assert "/etc/systemd/system/annie-room-audio.service" in unit
    # The started program must not inherit the lock, or the next call waits forever.
    assert "exec flock -o " in text
    assert subprocess.run(["sh", "-n", str(script)]).returncode == 0
    wrong = subprocess.run(["sh", str(script), "bogus"], capture_output=True, text=True,
                           env={**os.environ, "ANNIE_AUDIO_LOCKED": "1"})
    assert wrong.returncode == 2 and "用法" in wrong.stderr


def head_message(kind, instance, name, address, **data):
    return {"type": kind, "data": {"sender": "room_audio", "instance_id": instance,
                                   "head_name": name, "address": address, **data}}


def mic_report(instance, head_instance):
    return {"type": "mic.capture.progress", "data": {
        "sender": "microphone", "instance_id": instance, "head_instance": head_instance,
        "device_name": "USB 麦克风"}}


def test_brain_follows_the_newest_head_and_a_handover_is_not_a_conflict():
    clock = Clock()
    notices, changes = [], []
    devices = RoomAudioDevices(clock=clock, on_head=lambda kind, details: notices.append(
        (kind, details)), on_change=lambda status, _snapshot: changes.append(status))
    devices.report(head_message("robot.head.active", "head-doll", "obj 娃娃小机器人",
                                "192.168.1.143", active_for_ms=600_000))
    devices.report(mic_report("microphone-doll", "head-doll"))
    assert devices.snapshot()["head"]["current"]["head_name"] == "obj 娃娃小机器人"
    assert notices[-1][0] == "online"

    clock.now += 1
    devices.report(head_message("robot.head.active", "head-big", "大头机器人",
                                "192.168.1.188", active_for_ms=300))
    devices.report(mic_report("microphone-big", "head-big"))
    devices.report(mic_report("microphone-doll", "head-doll"))  # still in flight
    snapshot = devices.snapshot()
    assert snapshot["head"]["current"]["head_name"] == "大头机器人"
    assert [row["head_name"] for row in snapshot["head"]["heads"]] == ["大头机器人", "obj 娃娃小机器人"]
    assert snapshot["status"] == "ok" and changes == []

    devices.report(head_message("robot.head.released", "head-doll", "obj 娃娃小机器人",
                                "192.168.1.143", reason="taken_over", taken_over_by={
                                    "instance_id": "head-big", "head_name": "大头机器人",
                                    "address": "192.168.1.188"}))
    kind, details = notices[-1]
    assert kind == "handover"
    assert details["from"]["head_name"] == "obj 娃娃小机器人"
    assert details["to"]["address"] == "192.168.1.188"
    snapshot = devices.snapshot()
    assert [row["instance_id"] for row in snapshot["microphones"]] == ["microphone-big"]
    assert snapshot["head"]["handover"]["from"]["address"] == "192.168.1.143"

    clock.now += HEAD_TTL_SECONDS + 1  # the big head is unplugged
    assert devices.snapshot()["head"]["current"] is None
    assert notices[-1] == ("offline", {"instance_id": "head-big", "head_id": None,
                                       "head_name": "大头机器人", "host": None,
                                       "address": "192.168.1.188"})


def test_an_older_head_that_does_not_stop_is_a_conflict_after_the_grace():
    clock = Clock()
    devices = RoomAudioDevices(clock=clock)
    devices.report(head_message("robot.head.active", "head-doll", "娃娃", None,
                                active_for_ms=60_000))
    devices.report(head_message("robot.head.active", "head-big", "大头", None, active_for_ms=0))
    devices.report({"type": "mic.capture.started", "data": {
        "sender": "microphone", "instance_id": "browser-microphone-x"}})
    devices.report(mic_report("microphone-doll", "head-doll"))
    devices.report(mic_report("microphone-big", "head-big"))
    # The web microphone is not part of any handover.
    assert devices.conflicts()[0]["count"] == 2
    devices.report({"type": "mic.capture.stopped", "data": {
        "sender": "microphone", "instance_id": "browser-microphone-x"}})
    assert devices.conflicts() == []
    for _ in range(int(HANDOVER_GRACE_SECONDS) + 1):  # an old program that ignores heads
        clock.now += 1
        for instance in ("head-doll", "head-big"):
            devices.report(head_message("robot.head.active", instance, None, None,
                                        active_for_ms=0))
        devices.report(mic_report("microphone-doll", "head-doll"))
        devices.report(mic_report("microphone-big", "head-big"))
    assert devices.conflicts() == [{"role": "microphone", "reason": "multiple_devices", "count": 2}]


def test_the_head_started_later_takes_over_end_to_end(tmp_path):
    from annie_adapters import RoomServerConfig
    from annie_core import RobotConfig
    from annie_runtime import HarborRuntime
    from websockets.asyncio.server import serve

    from tests.test_decision_model import EchoSocket

    mic = speaker.mic_module()

    async def feed(capture, stop):
        tone = sine(16000, 440, .02, .3)
        while not stop.is_set():
            capture.feed(tone)
            await asyncio.sleep(.02)

    async def run():
        room = FakeRoom()
        server = await serve(room.handler, "127.0.0.1", 0)
        root = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/robot"
        channels = {name: "reb_" + name for name in
                    ("main", "tts", "mic", "visualdata", "vision", "sam", "voice")}
        config = RoomServerConfig(base_url=root + "/roomv2", room_root=root, channels=channels,
                                  default_urls={n: f"{root}/roomv2/{c}" for n, c in channels.items()},
                                  reconnect_min_sec=.01, reconnect_max_sec=.02,
                                  connect_timeout_sec=.5)
        rt = HarborRuntime([RobotConfig("a", room_id="roomv2", listen_enabled=True)],
                           tmp_path / "rt.db", config, realtime_key="fake",
                           realtime_socket_factory=EchoSocket)
        await rt.start()
        client = rt.realtime["a"]
        tasks = []
        stop = asyncio.Event()

        def board(head_id, name, address):
            head = head_module.Head(head_id, name, address=address, microphone="USB 麦克风")
            capture = mic.MicCapture(16000)
            identity = {**mic.device_identity("microphone", "USB 麦克风"), **head.identity()}
            task = asyncio.create_task(speaker.serve(
                "", f"{root}/roomv2/reb_voice", "roomv2", None, None,
                mic=(f"{root}/roomv2/reb_mic", capture, identity), head=head))
            tasks.append(task)
            tasks.append(asyncio.create_task(feed(capture, stop)))
            return head, task, capture

        def heads():
            return rt.input_stats("a")["audio_devices"]["head"]

        async def until(check, seconds=4):
            async with asyncio.timeout(seconds):
                while not check():
                    await asyncio.sleep(.02)

        try:
            doll, doll_task, doll_capture = board("obj_doll", "obj 娃娃小机器人", "192.168.1.143")
            await until(lambda: rt.input_stats("a")["accepted"] >= 10
                        and rt.input_stats("a")["audio_devices"]["microphones"])
            assert heads()["current"]["head_name"] == "obj 娃娃小机器人"
            microphone = rt.input_stats("a")["audio_devices"]["microphones"][0]
            assert microphone["head_name"] == "obj 娃娃小机器人"

            big, big_task, _ = board("big_head", "大头机器人", "192.168.1.188")
            assert await asyncio.wait_for(doll_task, 4) == "taken_over"
            assert not big_task.done()
            await until(lambda: heads()["handover"] is not None
                        and len(rt.input_stats("a")["audio_devices"]["microphones"]) == 1)
            state = heads()
            assert state["current"]["instance_id"] == big.instance_id
            assert state["handover"]["from"]["head_name"] == "obj 娃娃小机器人"
            assert state["handover"]["to"]["address"] == "192.168.1.188"
            devices = rt.input_stats("a")["audio_devices"]
            assert devices["microphones"][0]["head_name"] == "大头机器人"
            sent = doll_capture.sent_frames
            kinds = [event["kind"] for event in client.events]
            assert "robot.head.handover" in kinds and "robot.head.online" in kinds
            assert "audio.devices.conflict" not in kinds  # a handover is not two devices
            handover = next(e for e in client.events if e["kind"] == "robot.head.handover")
            assert (handover["from_name"], handover["to_name"]) == ("obj 娃娃小机器人", "大头机器人")
            await asyncio.sleep(.2)
            assert doll_capture.sent_frames == sent  # the older board sends nothing more
        finally:
            stop.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await rt.close()
            server.close()
            await server.wait_closed()

    asyncio.run(run())
