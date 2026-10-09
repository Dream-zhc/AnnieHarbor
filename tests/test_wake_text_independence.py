"""Text input remains usable while the microphone's KWS gate sleeps."""

import asyncio
import time

from annie_core import RobotConfig
from annie_runtime import HarborRuntime
from annie_runtime.wakeword import WakeAdmission
from annie_web import create_app
from fastapi.testclient import TestClient


class FakeRealtimeSocket:
    def __init__(self, _url, _key):
        self.outbound = []
        self.inbound = asyncio.Queue()

    async def open(self):
        return self

    async def send(self, packet):
        self.outbound.append(packet)
        if packet["type"] == "session.update":
            await self.inbound.put({
                "type": "session.updated",
                "session": {"id": "wake-text-test", **packet["session"]},
            })

    async def recv(self):
        return await self.inbound.get()

    async def close(self):
        pass


def test_sleeping_kws_does_not_block_explicit_text_or_open_pcm(tmp_path):
    async def run():
        runtime = HarborRuntime(
            [RobotConfig("annie-a", room_id="roomv2", listen_enabled=True)],
            tmp_path / "runtime.db",
            realtime_key="fake-test-key",
            realtime_socket_factory=FakeRealtimeSocket,
        )
        await runtime.start()
        try:
            client = runtime.realtime["annie-a"]
            for _ in range(100):
                if client.ready:
                    break
                await asyncio.sleep(.01)
            assert client.ready

            class Detector:
                async def detected(self, _):
                    return False

            client.wake = WakeAdmission(Detector())
            assert client.wake.state == "SLEEP"
            response = await client.submit_text("北京今天什么天气？")
            assert response["status"] == "accepted"
            assert client.robot.turn_cache[client.turn_id]["user_text"] == "北京今天什么天气？"
            assert client.wake.state == "SLEEP"
            assert not client.wake.presence.permits_pcm()
            assert any(packet["type"] == "response.create" for packet in client.socket.outbound)

            generation = client.robot.generation
            await client.handle({"type": "response.created",
                                 "response": {"id": "typed-response"}}, generation)
            await client.handle({"type": "response.text.delta", "response_id": "typed-response",
                                 "delta": "晴天"}, generation)
            assert client.text == "晴天"
            assert client.wake.state == "SLEEP"
        finally:
            await runtime.close()

    asyncio.run(run())


def test_workbench_text_http_endpoint_returns_accepted_while_sleeping(tmp_path):
    runtime = HarborRuntime(
        [RobotConfig("annie-a", room_id="roomv2", listen_enabled=True)],
        tmp_path / "http.db",
        realtime_key="fake-test-key",
        realtime_socket_factory=FakeRealtimeSocket,
    )

    class Detector:
        async def detected(self, _):
            return False

    with TestClient(create_app(runtime, tmp_path)) as client:
        for _ in range(100):
            if runtime.realtime["annie-a"].ready:
                break
            time.sleep(.01)
        assert runtime.realtime["annie-a"].ready
        runtime.realtime["annie-a"].wake = WakeAdmission(Detector())
        result = client.post("/api/robots/annie-a/realtime/text",
                             json={"text": "不唤醒也能发送文字"})
        assert result.status_code == 200, result.text
        assert result.json()["status"] == "accepted"
        assert runtime.realtime["annie-a"].wake.state == "SLEEP"


def test_workbench_text_view_receipt_is_scoped_not_speaker_evidence(tmp_path):
    class Planner:
        async def plan(self, _state):
            return {"decision": "NO_SPEAK", "topic": "", "new_value": False}

    runtime = HarborRuntime(
        [RobotConfig("annie-a", room_id="roomv2", listen_enabled=True)],
        tmp_path / "visible.db",
        realtime_key="fake-test-key", realtime_socket_factory=FakeRealtimeSocket,
        idle_model=Planner(), continuation_enabled=True,
    )
    with TestClient(create_app(runtime, tmp_path)) as web:
        client = runtime.realtime["annie-a"]
        for _ in range(100):
            if client.ready:
                break
            time.sleep(.01)
        assert client.ready
        turn_id = "visible-web-turn"
        identity = runtime.session_state("annie-a")
        client._last_user_turn_id = turn_id
        client._last_user_turn_at = time.monotonic()
        client._turn_timelines[turn_id] = {"input_mode": "text"}
        client.robot.turn_cache[turn_id] = {
            "turn_id": turn_id, "turn_source": "user", "status": "completed",
            "user_text": "今天工作好累，特别想放松一下",
            "assistant_generated_text": "那你先休息一下，别太拼了。",
            "updated_ms": time.time_ns() // 1_000_000,
        }
        route = f"/api/robots/annie-a/turns/{turn_id}/viewed"
        body = {k: identity[k] for k in
                ("chat_id", "persona_id", "session_epoch", "generation")}
        result = web.post(route, json={**body, "generation": body["generation"] + 1})
        assert result.status_code == 200 and result.json()["accepted"] is False
        assert web.post(route, json=body).json() == {
            "accepted": True, "evidence": "web_text_visible_only",
        }
        snap = runtime.idle.short.snapshot("annie-a")
        assert snap["channel"] == "text"
        assert runtime.idle.snapshot("annie-a")["speaker_drained"] is False
        client._turn_timelines[turn_id]["input_mode"] = "audio"
        client._last_user_turn_id = "newer-turn"
        assert web.post(route, json=body).json()["accepted"] is False
