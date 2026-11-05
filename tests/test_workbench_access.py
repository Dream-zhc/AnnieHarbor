"""工作台接口的内网直用与唤醒状态回归。"""

import asyncio

import httpx
from annie_core import RobotConfig
from annie_runtime import HarborRuntime
from annie_web import create_app


def test_existing_local_workbench_remains_usable_in_testclient(tmp_path):
    async def run():
        runtime = HarborRuntime([RobotConfig(robot_id="annie-a", room_id="roomv2",
                                            persona_id="annie-default")], tmp_path / "workbench.db")
        app = create_app(runtime, tmp_path)
        transport = httpx.ASGITransport(app=app, client=("testclient", 50123))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            assert (await client.get("/api/robots/annie-a/realtime")).status_code == 200
            assert (await client.get("/api/robots/annie-a/session")).status_code == 200

    asyncio.run(run())


def test_wakeword_status_endpoint_tracks_real_presence_without_history(tmp_path):
    from types import SimpleNamespace

    from annie_runtime.wakeword import WakeAdmission

    async def run():
        runtime = HarborRuntime([RobotConfig(robot_id="annie-a", room_id="roomv2",
                                            persona_id="annie-default")], tmp_path / "wake.db")
        current = [100.0]
        wake = WakeAdmission(object(), clock=lambda: current[0],
                             wake_wait_ms=5000, followup_ms=8000)
        runtime.realtime["annie-a"] = SimpleNamespace(ready=True, wake=wake)
        app = create_app(runtime, tmp_path)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            url = "/api/robots/annie-a/wakeword"
            asleep = (await client.get(url)).json()
            assert asleep["robot_id"] == "annie-a"
            assert asleep["wakeword"]["enabled"] is True
            assert asleep["wakeword"]["state"] == "SLEEP"
            assert asleep["wakeword"]["pcm_to_qwen"] is False
            assert "instructions" not in str(asleep)

            wake.presence.wake_detected()
            awake = (await client.get(url)).json()
            assert awake["wakeword"]["state"] == "WAKE_WAIT"
            assert awake["wakeword"]["deadline_remaining_ms"] == 5000
            assert awake["wakeword"]["pcm_to_qwen"] is True
            current[0] += 5.1
            assert (await client.get(url)).json()["wakeword"]["state"] == "SLEEP"
            assert (await client.get("/api/robots/unknown/wakeword")).status_code == 404

    asyncio.run(run())
