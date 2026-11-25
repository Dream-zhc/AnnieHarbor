"""小头 (annie-small) runs beside 大头 in its own Room with its own robots.toml switches."""

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from annie_adapters import RoomServerConfig, RuntimeStore, load_robots
from annie_brain.action_catalog import load_action_catalog, resolve_action
from annie_core import RobotConfig
from annie_runtime import HarborRuntime
from annie_runtime.__main__ import cloud_aec_robots, vision_intent_options
from annie_runtime.actions import ActionCoordinator

from tests.test_decision_model import FakeDecision
from tests.test_phase5 import FakeSocket
from tests.test_phase7_runtime import Publisher as ActionPublisher
from tests.test_phase7_runtime import ready

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "config/action_scripts.json"  # the big head's and B's table
SMALL_CATALOG = ROOT / "config/action_scripts_small.json"  # the small head's own table
BIG_ALLOWLIST = ("agree_micro_nod", "look_left")  # the shared allowlist these tests give the big head
ROOM_ROOT = "ws://localhost:1/robot"
# Legacy path, still supported: a robot without its own table picks a subset of the shared one.
SMALL_ACTIONS = ("shake_head", "look_left", "look_right", "fast_double_blink", "wink_left", "wink_right")
# What the embedded team listed for the small head (script_id -> duration_ms, in their order).
SMALL_HEAD_ACTIONS = {
    "shake_head": 3700, "nod": 2200, "tilt_head_right": 1800, "tilt_head_left": 1800, "tilt": 1800,
    "tilt_left": 1800, "look_left": 1200, "look_right": 1200, "look_up": 1800, "look_down": 1800,
    "think_tilt": 2800, "fast_double_blink": 550, "wink_left": 520, "wink_right": 520, "smile": 2000,
    "cry": 2000, "sad": 2000, "smile_nod": 2200, "cry_look_down": 1800, "sad_look_down": 1800,
    "shake_cry": 2500, "nod_look_down": 2200, "nod_serious": 2200,
}


def room_server():
    channels = {name: "reb_" + name for name in (
        "main", "tts", "mic", "visualdata", "vision", "sam", "visualllm", "voice")}
    return RoomServerConfig(base_url=ROOM_ROOT + "/roomv2", room_root=ROOM_ROOT, channels=channels,
                            default_urls={name: f"{ROOM_ROOT}/roomv2/{channel}"
                                          for name, channel in channels.items()},
                            connect_timeout_sec=.05, reconnect_min_sec=.05, reconnect_max_sec=.05)


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


def real_runtime(tmp_path, **options):
    """robots.toml and both action tables exactly as shipped; only the Room and the clouds are fake."""
    return HarborRuntime(load_robots(ROOT / "config/robots.toml"), tmp_path / "real-config.db",
                         room_server(), realtime_key="fake", realtime_socket_factory=FakeSocket,
                         tts_voice="test", room_tts_factory=ActionPublisher,
                         room_action_factory=ActionPublisher, action_catalog=CATALOG,
                         action_allowlist=BIG_ALLOWLIST, **options)


def test_robots_toml_gives_the_small_head_its_own_room_and_switches():
    robots = {robot.robot_id: robot for robot in load_robots(ROOT / "config/robots.toml")}
    assert {robot_id: robot.room_id for robot_id, robot in robots.items()} == {
        "annie-a": "roomv2", "annie-b": "roomv3", "annie-small": "roomv4"}
    small, big = robots["annie-small"], robots["annie-a"]
    assert small.enabled and big.enabled
    assert (small.wake_enabled, small.aec_enabled, small.mouth_enabled, small.camera_enabled) == (
        False, False, False, True)
    assert small.livekit_room_of == "annie-a"
    # Its own action table; the allowlist is then every enabled script in it.
    assert (small.action_catalog, small.actions) == ("action_scripts_small.json", ())
    assert (big.wake_enabled, big.aec_enabled, big.mouth_enabled, big.camera_enabled) == (
        True, True, True, True)
    assert (big.action_catalog, big.actions, big.livekit_room_of) == ("", (), "")
    assert (robots["annie-b"].action_catalog, robots["annie-b"].actions) == ("", ())
    assert robots["annie-b"].aec_enabled is False


# ---- the small head's own action table: not shared with the big head or B -------------
def test_the_small_heads_action_table_is_exactly_the_embedded_list():
    table = load_action_catalog(SMALL_CATALOG)
    assert list(table) == list(SMALL_HEAD_ACTIONS)  # same ids, same order
    assert {script: entry["duration_ms"] for script, entry in table.items()} == SMALL_HEAD_ACTIONS
    assert all(entry["enabled"] for entry in table.values())


@pytest.mark.parametrize(("alias", "script_id"), [
    ("tilt", "tilt_head_right"), ("tilt_left", "tilt_head_left"), ("sad", "cry"),
    ("sad_look_down", "cry_look_down"), ("nod_serious", "nod_look_down"),
])
def test_the_small_heads_aliases_read_like_their_script_so_the_prompt_names_one(alias, script_id):
    table = load_action_catalog(SMALL_CATALOG)
    for field in ("name", "description", "duration_ms", "category"):
        assert table[alias][field] == table[script_id][field]


def test_the_big_heads_table_is_untouched_by_the_small_heads():
    big = load_action_catalog(CATALOG)
    assert len(big) == 39
    assert {script: big[script]["duration_ms"] for script in ("look_left", "look_right", "look_up",
                                                              "think_tilt")} == {
        "look_left": 1300, "look_right": 1300, "look_up": 1300, "think_tilt": 3000}
    assert not {"nod", "tilt", "look_down", "smile", "cry", "sad", "shake_cry"} & set(big)


def test_each_head_has_its_own_table_and_cannot_do_the_others_actions(tmp_path):
    actions = real_runtime(tmp_path).actions
    big, small = actions.catalog_for("annie-a"), actions.catalog_for("annie-small")
    assert actions.catalog_for("annie-b") is big  # B has no table of its own: the shared one
    assert small is not big
    # One id on both heads, two durations: the small head's never reach the big head's.
    durations = {script: (big[script]["duration_ms"], small[script]["duration_ms"])
                 for script in ("look_left", "look_right", "look_up", "think_tilt")}
    assert durations == {"look_left": (1300, 1200), "look_right": (1300, 1200),
                         "look_up": (1300, 1800), "think_tilt": (3000, 2800)}
    # The small head runs exactly its own table and none of the big head's other scripts...
    assert actions.allowed("annie-small") == tuple(SMALL_HEAD_ACTIONS)
    big_only = set(big) - set(small)
    assert {"agree_micro_nod", "listen_tilt", "surprise", "sniff_forward"} <= big_only
    for script in big_only:
        assert script not in actions.allowed("annie-small")
        with pytest.raises(ValueError, match="unknown or disabled"):
            resolve_action(small, script)
    # ...and neither the big head nor B ever gets one that only the small head has.
    small_only = set(small) - set(big)
    assert small_only == {"nod", "tilt_head_right", "tilt", "tilt_left", "look_down", "smile", "cry",
                          "sad", "smile_nod", "cry_look_down", "sad_look_down", "shake_cry",
                          "nod_look_down", "nod_serious"}
    for robot_id in ("annie-a", "annie-b"):
        assert actions.allowed(robot_id) == BIG_ALLOWLIST
        assert not small_only & set(actions.allowed(robot_id))


def two_head_coordinator(tmp_path, name):
    class Sent:
        def __init__(self):
            self.sent = []

        async def publish(self, packet):
            self.sent.append(packet)

    def robot(robot_id, room_id, **config):
        return SimpleNamespace(config=RobotConfig(robot_id, room_id=room_id, **config), generation=1,
                               turn_cache={}, session_state=lambda: {})

    robots = {"big": robot("big", "roomv2"),
              "small": robot("small", "roomv4", action_catalog="action_scripts_small.json")}
    store = RuntimeStore(tmp_path / f"{name}.db")
    store.migrate()
    publishers = {"big": Sent(), "small": Sent()}
    actions = ActionCoordinator(
        store, load_action_catalog(CATALOG), robots, publishers, lambda *_a, **_k: None,
        enabled=True, allowlist=BIG_ALLOWLIST, robot_allowlists={"small": tuple(SMALL_HEAD_ACTIONS)},
        robot_catalogs={"small": load_action_catalog(SMALL_CATALOG)})
    return actions, publishers, store


def test_each_head_sends_its_own_scripts_with_its_own_durations(tmp_path):
    async def run():
        # look_left is on both heads: each reserves its own duration for its cooldown.
        actions, _publishers, store = two_head_coordinator(tmp_path, "look")
        for robot_id in ("big", "small"):
            result = await actions.perform_now(robot_id, f"look-{robot_id}", f"turn-{robot_id}", 1,
                                               "look_left")
            assert result["status"] == "published_unknown"
        assert store.get_action("big", "look-big")["duration_ms"] == 1300
        assert store.get_action("small", "look-small")["duration_ms"] == 1200

        # smile only exists on the small head, agree_micro_nod only on the big one.
        actions, publishers, store = two_head_coordinator(tmp_path, "own")
        assert (await actions.perform_now("small", "s1", "turn-s", 1, "smile"))["status"] == (
            "published_unknown")
        assert (await actions.perform_now("big", "b1", "turn-b", 1, "agree_micro_nod"))["status"] == (
            "published_unknown")
        assert publishers["small"].sent[0]["data"]["script_id"] == "smile"
        assert publishers["big"].sent[0]["data"]["script_id"] == "agree_micro_nod"
        assert store.get_action("small", "s1")["duration_ms"] == 2000
        with pytest.raises(ValueError, match="not enabled"):
            await actions.perform_now("big", "b2", "turn-b2", 1, "smile")
        with pytest.raises(ValueError, match="not enabled"):
            await actions.perform_now("small", "s2", "turn-s2", 1, "agree_micro_nod")
        assert len(publishers["big"].sent) == 1 and len(publishers["small"].sent) == 1
    asyncio.run(run())


def test_the_actions_view_lists_each_robots_own_table_and_allowlist(tmp_path):
    runtime = real_runtime(tmp_path)
    runtime.store.migrate()
    small = asyncio.run(runtime.action_list("annie-small"))
    assert [item["id"] for item in small["actions"]] == list(SMALL_HEAD_ACTIONS)
    assert small["allowlist"] == list(SMALL_HEAD_ACTIONS)
    big = asyncio.run(runtime.action_list("annie-a"))
    assert len(big["actions"]) == 39 and big["allowlist"] == list(BIG_ALLOWLIST)


def test_the_health_view_lists_the_small_heads_whole_table_as_its_actions(tmp_path):
    runtime = real_runtime(tmp_path)
    health = runtime.health()
    robots = {item["robot_id"]: item for item in health["robots"]}
    assert robots["annie-small"]["switches"]["actions"] == list(SMALL_HEAD_ACTIONS)
    assert robots["annie-a"]["switches"]["actions"] == list(BIG_ALLOWLIST)
    assert health["actions"]["robot_allowlists"] == {"annie-small": list(SMALL_HEAD_ACTIONS)}


def test_a_robot_with_its_own_table_never_falls_back_to_the_shared_allowlist(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    (config / "action_scripts.json").write_text(CATALOG.read_text(encoding="utf-8"), encoding="utf-8")
    document = json.loads(SMALL_CATALOG.read_text(encoding="utf-8"))
    for entry in document["scripts"]:
        entry["enabled"] = False
    (config / "all_off.json").write_text(json.dumps(document), encoding="utf-8")
    runtime = HarborRuntime([RobotConfig("annie-x", room_id="roomv9", action_catalog="all_off.json"),
                             RobotConfig("annie-y", room_id="roomv8")],
                            tmp_path / "all-off.db", action_catalog=config / "action_scripts.json",
                            action_allowlist=BIG_ALLOWLIST)
    assert runtime.actions.allowed("annie-x") == ()  # its table has nothing enabled: nothing to run
    assert runtime.actions.allowed("annie-y") == BIG_ALLOWLIST


def test_an_action_missing_from_a_robots_own_table_stops_startup(tmp_path):
    # agree_micro_nod is a big-head script: the shared table knows it, the small table does not.
    with pytest.raises(ValueError, match="unknown or disabled"):
        HarborRuntime([RobotConfig("annie-small", room_id="roomv4",
                                   action_catalog="action_scripts_small.json",
                                   actions=("agree_micro_nod",))],
                      tmp_path / "foreign-action.db", action_catalog=CATALOG)


@pytest.mark.parametrize("name", ["../x.json", "sub/x.json", "sub\\x.json", "x.txt", ".json",
                                  ".hidden.json", "a b.json", 5])
def test_action_catalog_must_be_a_plain_json_file_name(name):
    with pytest.raises(ValueError, match="action_catalog"):
        RobotConfig("annie-small", room_id="roomv4", action_catalog=name)


def test_action_catalog_takes_a_plain_json_file_name_or_nothing():
    assert RobotConfig("x", action_catalog="action_scripts_small.json").action_catalog == (
        "action_scripts_small.json")
    assert RobotConfig("x").action_catalog == ""


def started(tmp_path, *, decision):
    """The shipped robots with real actions on and the Realtime fakes ready."""
    runtime = real_runtime(tmp_path, actions_enabled=True)
    for client in runtime.realtime.values():
        if decision:
            client.decision = FakeDecision()
            client.addressee_enabled = True
            client.auto_expression_enabled = True
    return runtime


def test_each_heads_prompt_names_only_its_own_motions(tmp_path):
    async def run():
        runtime = started(tmp_path, decision=True)
        await ready(runtime)
        try:
            def motion_line(robot_id):
                instructions = runtime.realtime[robot_id].socket.outbound[0]["session"]["instructions"]
                return instructions[instructions.index("【实体动作】"):].split("\n")[0]

            small, big = motion_line("annie-small"), motion_line("annie-a")
            names = small.split("只有：")[1].split("。")[0].split("、")
            assert len(names) == len(set(names)) == 18  # aliases share a name: listed once
            assert {"点头", "抬头", "低头", "微笑", "伤心", "快速双眨", "微低头点头"} <= set(names)
            assert "附和小点头" not in small and "惊讶" not in small and "倾听" not in small
            assert "附和小点头" in big and "微笑" not in big and "抬头" not in big
        finally:
            await runtime.close()
    asyncio.run(run())


def test_each_heads_action_tool_lists_only_its_own_scripts(tmp_path):
    async def run():
        runtime = started(tmp_path, decision=False)
        await ready(runtime)
        try:
            def perform_action(robot_id):
                tools = runtime.realtime[robot_id].socket.outbound[0]["session"]["tools"]
                return next(tool["function"] for tool in tools
                            if tool["function"]["name"] == "perform_action")

            small, big = perform_action("annie-small"), perform_action("annie-a")
            assert small["parameters"]["properties"]["script_id"]["enum"] == list(SMALL_HEAD_ACTIONS)
            assert "smile: 开心、友好" in small["description"]
            assert ("think_tilt: 正在思考、权衡、回忆、准备给判断时使用；动作自带快速双眨"
                    in small["description"])
            assert big["parameters"]["properties"]["script_id"]["enum"] == list(BIG_ALLOWLIST)
            assert "agree_micro_nod: 轻微附和" in big["description"] and "smile" not in big["description"]
        finally:
            await runtime.close()
    asyncio.run(run())


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
