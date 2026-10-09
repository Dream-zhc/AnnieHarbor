"""内网直用、机器人隔离、视觉新鲜度与视频订阅回归。"""

import asyncio
import base64
import json
import runpy
from pathlib import Path

import httpx
import pytest
from annie_core import RobotConfig
from annie_runtime import HarborRuntime
from annie_web import create_app


def test_local_exhibit_provision_keeps_original_environment(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "初始化展厅访问.py"
    provision = runpy.run_path(str(script))["provision"]
    env = tmp_path / ".env"
    env.write_text("DASHSCOPE_API_KEY=protected\nANNIE_EXHIBIT_VIEWER_TOKEN=\n", encoding="utf-8")
    credentials = provision(env)
    assert len(credentials["ANNIE_EXHIBIT_OPERATOR_TOKEN"]) >= 24
    assert len(credentials["ANNIE_EXHIBIT_VIEWER_TOKEN"]) >= 24
    assert credentials["ANNIE_EXHIBIT_OPERATOR_TOKEN"] != credentials["ANNIE_EXHIBIT_VIEWER_TOKEN"]
    assert env.read_text(encoding="utf-8").count("DASHSCOPE_API_KEY=protected") == 1
    assert provision(env) == credentials


def test_existing_local_workbench_remains_usable_in_testclient(tmp_path, monkeypatch):
    monkeypatch.delenv("ANNIE_EXHIBIT_OPERATOR_TOKEN", raising=False)
    monkeypatch.delenv("ANNIE_EXHIBIT_VIEWER_TOKEN", raising=False)

    async def run():
        runtime = HarborRuntime([RobotConfig(robot_id="annie-a", room_id="roomv2",
                                            persona_id="annie-default")], tmp_path / "workbench.db")
        app = create_app(runtime, tmp_path)
        transport = httpx.ASGITransport(app=app, client=("testclient", 50123))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            assert (await client.get("/api/robots/annie-a/realtime")).status_code == 200
            assert (await client.get("/api/robots/annie-a/session")).status_code == 200

    asyncio.run(run())


def test_wakeword_status_endpoint_tracks_real_presence_without_history(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from annie_runtime.wakeword import WakeAdmission

    monkeypatch.delenv("ANNIE_EXHIBIT_OPERATOR_TOKEN", raising=False)
    monkeypatch.delenv("ANNIE_EXHIBIT_VIEWER_TOKEN", raising=False)

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


def test_recent_actions_are_chronological_and_generation_scoped():
    from annie_web.exhibit import _recent_actions

    newest_first = [
        {"call_id": f"call-{n}", "turn_id": f"turn-{n}",
         "generation": n, "kind": "action", "value": "agree_micro_nod",
         "status": "published_unknown"}
        for n in range(15, 0, -1)
    ]
    visible = _recent_actions({"requests": newest_first})
    assert len(visible) == 8
    assert [item["generation"] for item in visible] == list(range(8, 16))
    assert visible[-1]["call_id"] == "call-15"
    assert all(item["real_device_executed"] is False for item in visible)


@pytest.mark.parametrize("configured", [False, True])
def test_intranet_http_access_without_login_or_consent(tmp_path, monkeypatch, configured):
    for key in ("ANNIE_EXHIBIT_OPERATOR_TOKEN", "ANNIE_EXHIBIT_VIEWER_TOKEN"):
        if configured:
            monkeypatch.setenv(key, key + "-old-token-for-regression")
        else:
            monkeypatch.delenv(key, raising=False)

    async def run():
        robots = [RobotConfig(robot_id=name, room_id=room, persona_id="annie-default")
                  for name, room in (("annie-a", "roomv2"), ("annie-b", "roomv3"))]
        runtime = HarborRuntime(robots, tmp_path / "intranet.db")
        runtime.robots["annie-a"].snapshot.last_input = "北京天气怎么样"
        runtime.robots["annie-a"].snapshot.last_reply = "正在查询北京天气"
        app = create_app(runtime, tmp_path)
        transport = httpx.ASGITransport(app=app, client=("192.168.1.77", 12345))
        async with httpx.AsyncClient(transport=transport, base_url="http://192.168.1.10:8780") as client:
            session = await client.get("/api/exhibit/session")
            assert session.status_code == 200
            assert session.json()["access_mode"] == "intranet"
            assert (await client.get("/api/robots/annie-a/realtime")).status_code == 200
            assert (await client.get("/api/robots/annie-a/wakeword")).status_code == 200
            a = await client.get("/api/exhibit/robots/annie-a/snapshot")
            assert a.status_code == 200
            assert a.json()["realtime"]["user_text"] == "北京天气怎么样"
            assert a.json()["realtime"]["assistant_text"] == "正在查询北京天气"
            assert "playback_state" in a.json()["realtime"]
            assert "playback_source" in a.json()["realtime"]
            b = (await client.get("/api/exhibit/robots/annie-b/snapshot")).json()
            assert b["robot"]["room_id"] == "roomv3"
            assert b["realtime"]["user_text"] != "北京天气怎么样"
            assert (await client.get("/api/exhibit/robots/unknown/snapshot")).status_code == 404
            assert (await client.get("/api/dashboard/snapshot")).status_code == 404
            assert (await client.get("/api/robots/annie-a/cockpit")).status_code == 404
            # Normal same-origin mutations continue to work over LAN HTTP.
            response = await client.post("/api/robots/annie-a/cancel", headers={
                "Origin": "http://192.168.1.10:8780"})
            assert response.status_code == 200
            assert (await client.post("/api/robots/annie-a/cancel", headers={
                "Origin": "http://other-host"})).status_code == 403

    asyncio.run(run())


def test_visual_observation_keeps_freshness_without_consent():
    from annie_web.exhibit import _visible_visual

    now_ms = 200_000
    visual = {"state": "online", "description": "最近一帧画面", "age_ms": 3000}
    assert _visible_visual(visual, now_ms) == ("最近一帧画面", now_ms - 3000)
    for bad in ({**visual, "state": "stale"}, {**visual, "age_ms": 12000},
                {**visual, "age_ms": -1}, {**visual, "age_ms": None}):
        assert _visible_visual(bad, now_ms) == (None, None)


@pytest.mark.parametrize("server", ["ws://192.168.1.10:7880", "wss://video.example.test"])
def test_livekit_subscription_without_login_or_consent(tmp_path, monkeypatch, server):
    monkeypatch.delenv("ANNIE_EXHIBIT_OPERATOR_TOKEN", raising=False)
    monkeypatch.delenv("ANNIE_EXHIBIT_VIEWER_TOKEN", raising=False)
    monkeypatch.delenv("ANNIE_EXHIBIT_ALLOW_INSECURE_LIVEKIT", raising=False)
    monkeypatch.setenv("ANNIE_EXHIBIT_LIVEKIT_URL", server)
    monkeypatch.setenv("ANNIE_EXHIBIT_LIVEKIT_API_KEY", "lk_key")
    monkeypatch.setenv("ANNIE_EXHIBIT_LIVEKIT_API_SECRET", "lk-secret")
    monkeypatch.setenv("ANNIE_EXHIBIT_LIVEKIT_ROOM_ANNIE_A", "robot-test")
    monkeypatch.delenv("ANNIE_EXHIBIT_LIVEKIT_ROOM_ANNIE_B", raising=False)

    async def run():
        robots = [RobotConfig(robot_id=name, room_id=room, persona_id="annie-default")
                  for name, room in (("annie-a", "roomv2"), ("annie-b", "roomv3"))]
        runtime = HarborRuntime(robots, tmp_path / "video.db")
        app = create_app(runtime, tmp_path)
        transport = httpx.ASGITransport(app=app, client=("192.168.1.77", 12345))
        async with httpx.AsyncClient(transport=transport, base_url="http://192.168.1.10:8780") as client:
            response = await client.get("/api/exhibit/robots/annie-a/video-config")
            assert response.status_code == 200
            video = response.json()
            claims = json.loads(base64.urlsafe_b64decode(video["token"].split(".")[1] + "=="))
            assert video["server"] == server
            assert claims["video"]["room"] == "robot-test"
            assert claims["video"]["canPublish"] is False
            assert claims["video"]["canSubscribe"] is True
            assert claims["exp"] > claims["iat"]
            assert "secret" not in video
            assert (await client.get("/api/exhibit/robots/annie-b/video-config")).status_code == 503

    asyncio.run(run())
