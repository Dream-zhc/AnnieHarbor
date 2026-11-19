"""小头 (annie-small) runs beside 大头 in its own Room with its own robots.toml switches."""

import logging
from pathlib import Path

import pytest
from annie_adapters import RoomServerConfig, load_robots
from annie_core import RobotConfig
from annie_runtime import HarborRuntime
from annie_runtime.__main__ import cloud_aec_robots, vision_intent_options

from tests.test_phase5 import FakeSocket

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "config/action_scripts.json"
ROOM_ROOT = "ws://localhost:1/robot"
SMALL_ACTIONS = ("shake_head", "look_left", "look_right", "fast_double_blink", "wink_left", "wink_right")


def room_server():
    channels = {name: "reb_" + name for name in (
        "main", "tts", "mic", "visualdata", "vision", "sam", "visualllm", "voice")}
    return RoomServerConfig(base_url=ROOM_ROOT + "/roomv2", room_root=ROOM_ROOT, channels=channels,
                            default_urls={name: f"{ROOM_ROOT}/roomv2/{channel}"
                                          for name, channel in channels.items()})


def big_and_small():
    return [RobotConfig("annie-a", room_id="roomv2", listen_enabled=True, aec_enabled=True),
            RobotConfig("annie-small", room_id="roomv4", listen_enabled=True, wake_enabled=False,
                        aec_enabled=True, mouth_enabled=False, livekit_room_of="annie-a",
                        actions=SMALL_ACTIONS)]


class Publisher:
    def __init__(self, *_args, **_kwargs):
        self.packets = []

    async def publish(self, packet):
        self.packets.append(packet)


def runtime_for(tmp_path, **options):
    return HarborRuntime(big_and_small(), tmp_path / "small-head.db", room_server(),
                         realtime_key="fake", realtime_socket_factory=FakeSocket,
                         tts_voice="test", room_tts_factory=Publisher, wake_detector=object(),
                         cloud_aec_robots=("annie-a", "annie-small"), **options)


def test_robots_toml_gives_the_small_head_its_own_room_and_switches():
    robots = {robot.robot_id: robot for robot in load_robots(ROOT / "config/robots.toml")}
    assert {robot_id: robot.room_id for robot_id, robot in robots.items()} == {
        "annie-a": "roomv2", "annie-b": "roomv3", "annie-small": "roomv4"}
    small, big = robots["annie-small"], robots["annie-a"]
    assert small.enabled and big.enabled
    assert (small.wake_enabled, small.aec_enabled, small.mouth_enabled, small.camera_enabled) == (
        False, False, False, True)
    assert small.livekit_room_of == "annie-a"
    assert small.actions == SMALL_ACTIONS
    assert (big.wake_enabled, big.aec_enabled, big.mouth_enabled, big.camera_enabled) == (
        True, True, True, True)
    assert big.actions == () and big.livekit_room_of == ""
    assert robots["annie-b"].aec_enabled is False


def test_livekit_room_of_must_name_another_robot_in_the_same_file(tmp_path):
    with pytest.raises(ValueError, match="livekit_room_of"):
        RobotConfig("annie-small", room_id="roomv4", livekit_room_of="annie-small")
    path = tmp_path / "robots.toml"
    path.write_text('[[robots]]\nrobot_id = "annie-small"\nroom_id = "roomv4"\nlivekit_room_of = "annie-x"\n',
                    encoding="utf-8")
    with pytest.raises(ValueError, match="same file"):
        load_robots(path)


def test_health_reports_each_robots_switches_and_board(tmp_path):
    runtime = runtime_for(tmp_path, action_catalog=CATALOG, action_allowlist=("agree_micro_nod", "look_left"))
    assert runtime.realtime["annie-a"].wake is not None
    assert runtime.realtime["annie-small"].wake is None
    assert runtime.actions.allowed("annie-a") == ("agree_micro_nod", "look_left")
    assert runtime.actions.allowed("annie-small") == SMALL_ACTIONS

    health = runtime.health()
    robots = {item["robot_id"]: item for item in health["robots"]}
    assert robots["annie-a"]["switches"] == {"wake": True, "aec": True, "mouth": True, "camera": True,
                                              "actions": ["agree_micro_nod", "look_left"]}
    assert robots["annie-small"]["switches"] == {"wake": False, "aec": True, "mouth": False,
                                                  "camera": True, "actions": list(SMALL_ACTIONS)}
    assert health["actions"]["robot_allowlists"] == {"annie-small": list(SMALL_ACTIONS)}
    assert robots["annie-a"]["head"] is None and robots["annie-small"]["head"] is None

    runtime.audio_devices["annie-small"].report({"type": "robot.head.active", "data": {
        "sender": "room_audio", "instance_id": "small-1", "head_id": "small_head",
        "head_name": "小头机器人", "address": "10.0.0.8", "active_for_ms": 4000}})
    robots = {item["robot_id"]: item for item in runtime.health()["robots"]}
    assert robots["annie-a"]["head"] is None
    head = robots["annie-small"]["head"]
    assert (head["instance_id"], head["head_id"], head["head_name"]) == ("small-1", "small_head", "小头机器人")
    assert head["active_for_ms"] >= 4000


def test_the_small_heads_mouth_is_off_while_its_room_still_sees(tmp_path):
    runtime = runtime_for(tmp_path)
    channels = {robot_id: [item.channel for item in listeners]
                for robot_id, listeners in runtime.playback_listeners.items()}
    assert "sam" in channels["annie-a"] and "sam" not in channels["annie-small"]
    assert runtime.room_observations("annie-small")["viseme"]["state"] == "disabled"
    health = runtime.health()
    assert health["viseme"]["annie-small"]["state"] == "disabled"
    assert health["viseme"]["annie-a"]["state"] != "disabled"

    visual = next(item for item in runtime.playback_listeners["annie-small"] if item.channel == "visualllm")
    assert visual.url == ROOM_ROOT + "/roomv4/reb_visualllm"
    visual.connected = True
    runtime._room_visual_event("annie-small", {"type": "visualllm", "description": "桌上有一个杯子"})
    assert runtime.room_observations("annie-small")["visual"]["description"] == "桌上有一个杯子"
    assert runtime.room_observations("annie-a")["visual"]["description"] is None


def test_an_unknown_action_for_one_robot_stops_startup(tmp_path):
    with pytest.raises(ValueError, match="unknown or disabled"):
        HarborRuntime([RobotConfig("annie-small", room_id="roomv4", actions=("fly_away",))],
                      tmp_path / "unknown-action.db", action_catalog=CATALOG)


def test_cloud_aec_follows_robots_toml_under_the_machine_switch(caplog):
    robots = [*big_and_small(), RobotConfig("annie-b", room_id="roomv3"),
              RobotConfig("annie-off", room_id="roomv5", enabled=False, aec_enabled=True)]
    assert cloud_aec_robots(robots, {}) == ()
    on = {"ANNIE_CLOUD_AEC_ENABLED": "1"}
    with caplog.at_level(logging.WARNING, logger="annie_runtime"):
        assert cloud_aec_robots(robots, on) == ("annie-a", "annie-small")
        assert cloud_aec_robots(robots, {**on, "ANNIE_CLOUD_AEC_ROBOTS": "annie-small, annie-a"}) == (
            "annie-a", "annie-small")
    assert "ANNIE_CLOUD_AEC_ROBOTS" not in caplog.text
    with caplog.at_level(logging.WARNING, logger="annie_runtime"):
        assert cloud_aec_robots(robots, {**on, "ANNIE_CLOUD_AEC_ROBOTS": "annie-a"}) == (
            "annie-a", "annie-small")
    assert ("ANNIE_CLOUD_AEC_ROBOTS=annie-a is ignored; "
            "robots.toml aec_enabled selects annie-a,annie-small") in caplog.text


def test_the_small_heads_camera_reuses_the_big_heads_livekit_room():
    env = {"ANNIE_EXHIBIT_LIVEKIT_URL": "wss://livekit.example", "ANNIE_EXHIBIT_LIVEKIT_API_KEY": "test-key",
           "ANNIE_EXHIBIT_LIVEKIT_API_SECRET": "test-secret", "ANNIE_EXHIBIT_LIVEKIT_ROOM_ANNIE_A": "exhibit-a"}
    robot_ids = ("annie-a", "annie-b", "annie-small")
    room_of = {"annie-small": "annie-a"}
    assert vision_intent_options({}, env, robot_ids, room_of)["livekit"]["rooms"] == {
        "annie-a": "exhibit-a", "annie-small": "exhibit-a"}
    assert vision_intent_options({}, env, robot_ids)["livekit"]["rooms"] == {"annie-a": "exhibit-a"}
    env["ANNIE_EXHIBIT_LIVEKIT_ROOM_ANNIE_SMALL"] = "exhibit-small"
    assert vision_intent_options({}, env, robot_ids, room_of)["livekit"]["rooms"]["annie-small"] == "exhibit-small"
