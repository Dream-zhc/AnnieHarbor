"""Actual local WebSocket subscription -> camera fact -> existing Qwen socket."""

import asyncio
import json

from annie_adapters import RoomServerConfig
from annie_core import RobotConfig
from annie_runtime import HarborRuntime
from websockets.asyncio.server import serve

from tests.test_phase4_realtime import FakeSocket
from tests.test_visual_llm_context import until


def test_websocket_visual_packet_reaches_realtime_instructions(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setattr("annie_runtime.realtime.VISUAL_CONTEXT_MIN_INTERVAL_SECONDS", .02)
        monkeypatch.setattr("annie_runtime.realtime.VISUAL_CONTEXT_POLL_SECONDS", .02)
        connected = []

        async def handler(ws):
            room_id, channel = ws.request.path.split("/")[-2:]
            connected.append((room_id, channel))
            await ws.send(json.dumps({
                "type": "WELCOME", "ok": True, "clientId": "local-test",
                "robotId": channel, "room": room_id, "size": 1,
                "subscriptions": [],
                "catalog": [{"type": "MIC_AUDIO"}] if channel == "reb_mic" else [],
            }))
            subscribe = json.loads(await ws.recv())
            assert subscribe == {"type": "SUBSCRIBE", "data": {"types": ["*"]}}
            await ws.send(json.dumps({"type": "ACK", "data": {
                "what": "subscribed", "subscriptions": ["*"]}}))
            if room_id == "roomv2" and channel == "reb_visualllm":
                await ws.send(json.dumps({
                    "type": "visualllm", "schema_version": 2,
                    "description": "测试房间实际 WS：右边有一只猫",
                    "facts": {"subjects": [{"id": "cat_1", "kind": "cat",
                                           "position": "右边"}]},
                }, ensure_ascii=False))
            await ws.wait_closed()

        server = await serve(handler, "127.0.0.1", 0)
        root = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/robot"
        channels = {name: "reb_" + name for name in (
            "main", "tts", "mic", "visualdata", "vision", "sam", "visualllm"
        )}
        room = RoomServerConfig(
            base_url=root + "/roomv2", room_root=root, channels=channels,
            default_urls={name: root + "/roomv2/" + channel
                          for name, channel in channels.items()},
            connect_timeout_sec=.5, reconnect_min_sec=.02, reconnect_max_sec=.1,
        )
        rt = HarborRuntime([RobotConfig("a", room_id="roomv2")],
                           tmp_path / "network.db", room, realtime_key="fake",
                           realtime_socket_factory=FakeSocket)
        try:
            await rt.start()
            a = rt.realtime["a"]
            await until(lambda: a.visual_context_snapshot()["state"] == "confirmed")
            assert ("roomv2", "reb_visualllm") in connected
            instructions = a.prompt_snapshot["instructions"]
            assert "右边有一只猫" in instructions
            assert "cat_1" in instructions
            assert len(a.robot.turn_cache) == 0
            await a.submit_text("你看右边有什么？")
            assert a.socket.outbound[-1]["type"] == "response.create"
        finally:
            await rt.close()
            server.close()
            await server.wait_closed()

    asyncio.run(run())
