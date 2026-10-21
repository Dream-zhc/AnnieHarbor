"""10/21 evening: the brain host's desktop proxy broke Aliyun sockets, and a
missing item on deletion reset the whole Realtime session."""

import asyncio
import time

import pytest
from annie_adapters import acoustic, net, realtime, tts


def test_aliyun_sockets_go_direct_unless_a_proxy_is_asked_for(monkeypatch):
    monkeypatch.delenv("ANNIE_CLOUD_PROXY", raising=False)
    assert net.cloud_proxy() is None
    monkeypatch.setenv("ANNIE_CLOUD_PROXY", "http://127.0.0.1:7890")
    assert net.cloud_proxy() == "http://127.0.0.1:7890"
    assert net.room_connect.keywords == {"proxy": None}  # the Room is on the LAN


@pytest.mark.parametrize("module,factory", [
    (realtime, lambda: realtime.QwenRealtimeSocket(
        "wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=qwen-audio-3.1-realtime-plus", "k")),
    (tts, lambda: tts.QwenTtsSocket("wss://dashscope.aliyuncs.com/api-ws/v1/inference", "k")),
    (acoustic, lambda: acoustic.QwenASRSocket("wss://dashscope.aliyuncs.com/api-ws/v1/realtime", "k")),
])
def test_each_aliyun_socket_passes_the_proxy_choice(monkeypatch, module, factory):
    seen = {}

    async def fake_connect(url, **kwargs):
        seen.update(kwargs)
        return object()

    monkeypatch.delenv("ANNIE_CLOUD_PROXY", raising=False)
    monkeypatch.setattr(module, "connect", fake_connect)
    asyncio.run(factory().open())
    assert "proxy" in seen and seen["proxy"] is None


def test_a_missing_item_we_deleted_does_not_reset_the_session(tmp_path):
    from annie_core import RobotConfig
    from annie_runtime import HarborRuntime

    from tests.test_decision_model import EchoSocket

    async def run():
        rt = HarborRuntime([RobotConfig("a", room_id="roomv2")], tmp_path / "rt.db",
                           realtime_key="fake", realtime_socket_factory=EchoSocket)
        await rt.start()
        client = rt.realtime["a"]
        try:
            for _ in range(150):
                if client.ready:
                    break
                await asyncio.sleep(.01)
            await client._delete_unheard_items({"item_A"})
            gone = {"type": "error", "error": {"code": "invalid_value",
                                               "message": "Cannot find item with id: item_A"}}
            await client.handle(gone, client.robot.generation)  # no raise
            assert any(e["kind"] == "context.delete_missing_ignored" for e in client.events)
            other = {"type": "error", "error": {"code": "invalid_value",
                                                "message": "Cannot find item with id: item_B"}}
            with pytest.raises(RuntimeError):
                await client.handle(other, client.robot.generation)
            client._pending_deletes["item_C"] = time.monotonic() - 60  # long ago: real error
            stale = {"type": "error", "error": {"code": "invalid_value",
                                                "message": "Cannot find item with id: item_C"}}
            with pytest.raises(RuntimeError):
                await client.handle(stale, client.robot.generation)
        finally:
            await rt.close()

    asyncio.run(run())
