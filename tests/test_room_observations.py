"""Room service heartbeat and external camera descriptions are scoped observations."""

import asyncio
from unittest.mock import patch

import httpx
from annie_adapters import RoomServerConfig
from annie_core import RobotConfig
from annie_runtime import HarborRuntime
from annie_runtime.room_monitor import RoomMonitor
from annie_web import create_app

from tests.test_phase5 import FakeSocket


def test_health_is_receive_time_based_not_last_mouth_or_remote_clock():
    with patch("annie_runtime.room_monitor.time.monotonic", return_value=100):
        state = RoomMonitor("roomv2")
        state.on_sam({"type": "mouth_script", "seq": 1,
                      "char": "你", "viseme": "i", "ts": 9999999999999})
        state.on_visual({"type": "visualllm", "description": "看见一个杯子"})
    with patch("annie_runtime.room_monitor.time.monotonic", return_value=101):
        assert state.snapshot(sam_connected=True, visual_connected=True,
                              visual_configured=True)["viseme"]["state"] == "waiting"
        state.on_sam({"type": "viseme_health", "status": "ready",
                      "sam_connected": True, "tts_connected": True,
                      "sam_subscribed": True, "tts_subscribed": True,
                      "ts": 1})
        assert state.snapshot(sam_connected=True, visual_connected=True,
                              visual_configured=True)["viseme"]["state"] == "online"
    with patch("annie_runtime.room_monitor.time.monotonic", return_value=107):
        snap = state.snapshot(sam_connected=True, visual_connected=True,
                              visual_configured=True)
        assert snap["viseme"]["state"] == "offline"
        assert snap["visual"]["description"] == "看见一个杯子"
        assert snap["viseme"]["mouth_count"] == 1
    with patch("annie_runtime.room_monitor.time.monotonic", return_value=112):
        assert state.snapshot(sam_connected=False, visual_connected=True,
                              visual_configured=True)["visual"]["state"] == "stale"


def test_invalid_data_cannot_fake_online_or_replace_description():
    monitor = RoomMonitor("roomv2")
    monitor.on_sam({"type": "mouth_script", "seq": "1", "viseme": "i"})
    monitor.on_sam({"type": "viseme_health", "status": "ready",
                    "sam_connected": True, "tts_connected": False,
                    "sam_subscribed": True, "tts_subscribed": True})
    monitor.on_visual({"type": "visualllm", "description": "  "})
    monitor.on_visual({"type": "other", "description": "错误消息"})
    snap = monitor.snapshot(sam_connected=True, visual_connected=True,
                            visual_configured=True)
    assert snap["viseme"]["state"] == "degraded"
    assert snap["viseme"]["mouth_count"] == 0
    assert snap["visual"]["description"] is None


def test_structured_visual_preserves_subject_relations_and_reports_budget():
    monitor = RoomMonitor("roomv2")
    facts = {
        "subjects": [
            {"id": "person_1", "kind": "person", "action": "拿着麦克风",
             "position": "桌子前", "confidence": .91},
            {"id": "mic_1", "kind": "microphone", "position": "person_1手中"},
        ],
        "relations": [{"subject": "person_1", "relation": "holds", "object": "mic_1"}],
        "uncertainties": ["是否录音不能确定"], "changes": [],
    }
    assert monitor.on_visual({"type": "visualllm", "schema_version": 2,
                              "description": "画面里一人拿着麦克风，旁边有电脑。",
                              "facts": facts})
    text = monitor.current_visual(connected=True)
    assert "person_1" in text and "mic_1" in text and "holds" in text
    assert "是否录音不能确定" in text
    snap = monitor.snapshot(sam_connected=None, visual_connected=True,
                            visual_configured=True)["visual"]
    assert snap["facts_accepted"] and not snap["context_truncated"]
    assert snap["description"] == "画面里一人拿着麦克风，旁边有电脑。"

    # Extra prose does not remove the structured subject/relationship evidence.
    assert monitor.on_visual({"type": "visualllm", "description": "细节" * 2500,
                              "facts": facts})
    assert "person_1" in monitor.current_visual(connected=True)
    snap = monitor.snapshot(sam_connected=None, visual_connected=True,
                            visual_configured=True)["visual"]
    assert snap["context_truncated"]
    assert len(monitor.current_visual(connected=True)) <= 2000
    assert len(snap["description"]) == 4000
    assert not monitor.current_visual(connected=False)


def test_oversized_facts_fall_back_to_caption_with_explicit_diagnostic():
    monitor = RoomMonitor("roomv2")
    assert monitor.on_visual({"type": "visualllm", "description": "桌上有杯子",
                              "facts": {"changes": ["变化" * 1200]}})
    assert monitor.current_visual(connected=True) == "桌上有杯子"
    snap = monitor.snapshot(sam_connected=None, visual_connected=True,
                            visual_configured=True)["visual"]
    assert snap["context_truncated"] and not snap["facts_accepted"]
    assert not monitor.on_visual({"type": "visualllm", "description": " "})
    assert monitor.current_visual(connected=True) == "桌上有杯子"


def test_roomv2_subscriptions_and_http_do_not_leak_to_roomv3(tmp_path):
    async def run():
        channels = {name: "reb_" + name for name in (
            "main", "tts", "mic", "visualdata", "vision", "sam", "visualllm"
        )}
        root = "ws://localhost:1/robot"
        room = RoomServerConfig(
            base_url=root + "/roomv2", room_root=root, channels=channels,
            default_urls={name: root + "/roomv2/" + channel
                          for name, channel in channels.items()},
        )
        runtime = HarborRuntime(
            [RobotConfig("a", room_id="roomv2"), RobotConfig("b", room_id="roomv3")],
            tmp_path / "room.db", room, realtime_key="fake",
            realtime_socket_factory=FakeSocket,
        )
        a = runtime.playback_listeners["a"]
        b = runtime.playback_listeners["b"]
        assert [item.channel for item in a] == ["sam", "main", "visualdata", "visualllm"]
        assert [item.channel for item in b] == ["sam", "main", "visualdata"]
        assert a[-1].url == root + "/roomv2/reb_visualllm"
        a[-1].connected = True
        runtime._room_visual_event("a", {"type": "visualllm", "description": "桌上有杯子"})
        app = create_app(runtime, tmp_path)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url="http://test") as client:
            desc = (await client.get("/api/robots/a/room-observations")).json()
            assert desc["visual"]["description"] == "桌上有杯子"
            assert desc["visual"]["state"] == "online"
            other = (await client.get("/api/robots/b/room-observations")).json()
            assert other["visual"]["description"] is None
            assert other["visual"]["state"] == "not_configured"
            assert (await client.get("/api/robots/unknown/room-observations")).status_code == 404

    asyncio.run(run())


def test_independent_viseme_status_does_not_require_tts_or_realtime(tmp_path):
    root = "ws://localhost:1/robot"
    channels = {name: "reb_" + name for name in (
        "main", "tts", "mic", "visualdata", "vision", "sam", "visualllm"
    )}
    room = RoomServerConfig(
        base_url=root + "/roomv2", room_root=root, channels=channels,
        default_urls={name: root + "/roomv2/" + channel
                      for name, channel in channels.items()},
    )
    runtime = HarborRuntime(
        [RobotConfig("a", room_id="roomv2")], tmp_path / "local.db", room,
    )
    assert [listener.channel for listener in runtime.playback_listeners["a"]] == [
        "sam", "visualllm"
    ]
    assert runtime.room_observations("a")["viseme"]["state"] == "disconnected"
    runtime.playback_listeners["a"][0].connected = True
    runtime._room_sam_event("a", {"type": "viseme_health", "status": "ready",
                                  "sam_connected": True, "tts_connected": True,
                                  "sam_subscribed": True, "tts_subscribed": True})
    assert runtime.room_observations("a")["viseme"]["state"] == "online"
