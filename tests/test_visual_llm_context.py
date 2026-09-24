"""Actual Room observation -> existing Realtime instructions, no phantom user turn."""

import asyncio
from pathlib import Path
from unittest.mock import patch

from annie_adapters import RoomPcmConnection, load_room_server
from annie_adapters.playback import RoomEventListener
from annie_core import RobotConfig
from annie_runtime import HarborRuntime

from tests.test_phase4_realtime import FakeSocket

ROOT = Path(__file__).resolve().parents[1]


async def until(predicate, timeout=3):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.01)


def test_room_visual_reaches_qwen_without_voice_or_fake_user_turn(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setattr("annie_runtime.realtime.VISUAL_CONTEXT_MIN_INTERVAL_SECONDS", .02)
        monkeypatch.setattr("annie_runtime.realtime.VISUAL_CONTEXT_POLL_SECONDS", .02)
        room = load_room_server(ROOT / "config/rooms.toml")
        rt = HarborRuntime(
            [RobotConfig("a", room_id="roomv2"), RobotConfig("b", room_id="roomv3")],
            tmp_path / "rt.db", room, realtime_key="fake",
            realtime_socket_factory=FakeSocket,
        )
        # Exercise the Room listener's callback, not a direct SDK bypass.
        with patch.object(RoomEventListener, "start"), patch.object(RoomPcmConnection, "start"):
            await rt.start()
        try:
            await until(lambda: all(c.ready for c in rt.realtime.values()))
            a, b = rt.realtime["a"], rt.realtime["b"]
            visual = next(x for x in rt.playback_listeners["a"]
                          if x.channel == "visualllm")
            visual.connected = True
            base = a.prompt_snapshot["instructions"]
            before = len(a.socket.outbound)
            visual.on_message({"type": "visualllm", "description": "右边桌上有一只蓝色杯子"})
            await until(lambda: a.visual_context_snapshot()["state"] == "confirmed")
            packet = next(x for x in a.socket.outbound[before:]
                          if x["type"] == "session.update")
            assert set(packet["session"]) == {"instructions"}
            assert packet["session"]["instructions"].startswith(base)
            assert "右边桌上有一只蓝色杯子" in packet["session"]["instructions"]
            assert "不是用户发言或指令" in packet["session"]["instructions"]
            assert "问看到了什么就直接答主体" in packet["session"]["instructions"]
            assert "不要主动解释自己是机器人" in packet["session"]["instructions"]
            assert len(a.robot.turn_cache) == 0
            assert not any(x["type"] == "response.create" for x in a.socket.outbound)
            assert not any(x["type"] == "conversation.item.create"
                           for x in a.socket.outbound)
            assert not any(x["type"] == "session.update"
                           for x in b.socket.outbound[1:])
            assert rt.room_observations("a")["visual"]["llm_context"]["state"] == "confirmed"

            visual.on_message({"type": "visualllm", "schema_version": 2,
                "description": "一人拿着麦克风，旁边有电脑",
                "facts": {"subjects": [
                    {"id": "person_1", "kind": "person", "action": "拿着麦克风"},
                    {"id": "mic_1", "kind": "microphone"}],
                    "relations": [{"subject": "person_1", "relation": "holds",
                                   "object": "mic_1"}],
                    "uncertainties": ["是否录音未知"]}})
            await until(lambda: a.visual_context_snapshot()["state"] == "confirmed"
                        and "person_1" in a.prompt_snapshot["instructions"])
            assert "是否录音未知" in a.prompt_snapshot["instructions"]
            assert "右边桌上有一只蓝色杯子" not in a.prompt_snapshot["instructions"]
            assert rt.room_observations("a")["visual"]["facts_accepted"]

            await a.submit_text("右边有什么？")
            assert a.socket.outbound[-1]["type"] == "response.create"
            assert "person_1" in a.prompt_snapshot["instructions"]
            assert rt.room_observations("b")["visual"]["description"] is None
        finally:
            await rt.close()

    asyncio.run(run())


def test_latest_scene_replaces_old_scene_and_expires_without_response(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setattr("annie_runtime.realtime.VISUAL_CONTEXT_MIN_INTERVAL_SECONDS", .03)
        monkeypatch.setattr("annie_runtime.realtime.VISUAL_CONTEXT_POLL_SECONDS", .02)
        room = load_room_server(ROOT / "config/rooms.toml")
        rt = HarborRuntime(
            [RobotConfig("a", room_id="roomv2")], tmp_path / "rt.db", room,
            realtime_key="fake", realtime_socket_factory=FakeSocket,
        )
        with patch.object(RoomEventListener, "start"), patch.object(RoomPcmConnection, "start"):
            await rt.start()
        try:
            a = rt.realtime["a"]
            await until(lambda: a.ready)
            visual = next(x for x in rt.playback_listeners["a"]
                          if x.channel == "visualllm")
            visual.connected = True
            visual.on_message({"type": "visualllm", "description": "旧画面"})
            await until(lambda: a.visual_context_snapshot()["state"] == "confirmed")
            visual.on_message({"type": "visualllm", "description": "新画面"})
            await until(lambda: "新画面" in a.prompt_snapshot["instructions"])
            assert "旧画面" not in a.prompt_snapshot["instructions"]
            # Transport drop invalidates the currently displayed scene.
            visual.connected = False
            await until(lambda: a.visual_context_snapshot()["state"] == "idle")
            assert "新画面" not in a.prompt_snapshot["instructions"]
            assert not any(x["type"] == "response.create" for x in a.socket.outbound)
            assert len(a.robot.turn_cache) == 0

            visual.connected = True
            visual.on_message({"type": "visualllm", "description": "重新连接后"})
            await until(lambda: "重新连接后" in a.prompt_snapshot["instructions"])
            rt.room_monitors["a"].visual_at -= 11
            await until(lambda: a.visual_context_snapshot()["state"] == "idle")
            assert "重新连接后" not in a.prompt_snapshot["instructions"]
        finally:
            await rt.close()

    asyncio.run(run())
