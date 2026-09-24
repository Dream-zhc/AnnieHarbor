"""Live ASGI memory operations; server—not browser—owns persona scope."""

from annie_core import RobotConfig
from annie_runtime import HarborRuntime
from annie_web import create_app
from fastapi.testclient import TestClient


class FakeCloud:
    async def close(self):
        pass

    async def embed(self, _):
        return [1.0, 0.0]

    async def extract(self, user, existing):
        return []


def test_memory_endpoints_use_current_server_persona(tmp_path):
    runtime = HarborRuntime(
        [RobotConfig("a", room_id="roomv2", fake_delay_ms=0)],
        tmp_path / "http.db",
        memory_model=FakeCloud(),
    )
    with TestClient(create_app(runtime, tmp_path)) as client:
        base = "/api/robots/a/memories"
        created = client.post(base, json={
            "kind": "persona", "key": "friendship", "text": "用户喜欢开玩笑",
        })
        assert created.status_code == 200, created.text
        ident, version = created.json()["id"], created.json()["version"]
        listed = client.get(base).json()["memories"]
        assert len(listed) == 1
        assert listed[0]["persona_id"] == "annie"
        assert client.patch(base + "/" + ident, json={
            "version": 88, "text": "旧版本",
        }).status_code == 409
        corrected = client.patch(base + "/" + ident, json={
            "version": version, "text": "用户不喜欢开玩笑",
        })
        assert corrected.status_code == 200
        assert client.post("/api/robots/a/session", json={
            "chat_id": "new-chat", "persona_id": "different",
        }).status_code == 200
        assert client.get(base).json()["memories"] == []
        assert client.request("DELETE", base + "/" + ident, json={"version": 2}).status_code == 404
        assert client.post("/api/robots/a/session", json={
            "chat_id": "new-chat", "persona_id": "annie",
        }).status_code == 200
        forgotten = client.request("DELETE", base + "/" + ident, json={"version": 2})
        assert forgotten.status_code == 200
        assert forgotten.json()["conversation_history_deleted"] is False
        assert client.get(base).json()["memories"] == []
        assert client.get(base + "?include_deleted=true").json()["memories"][0]["status"] == "deleted"


def test_sensitive_diagnostics_are_scoped_and_not_in_health(tmp_path):
    from tests.test_phase4_realtime import FakeSocket

    runtime = HarborRuntime(
        [RobotConfig("a", room_id="roomv2", fake_delay_ms=0)],
        tmp_path / "diagnostics.db",
        realtime_key="fake", realtime_socket_factory=FakeSocket,
    )
    with TestClient(create_app(runtime, tmp_path)) as client:
        health = client.get("/health")
        assert "prompt_snapshot" not in health.text
        assert "tool_history" not in health.text
        diagnostics = client.get("/api/robots/a/diagnostics")
        assert diagnostics.status_code == 200
        scoped = diagnostics.json()
        assert scoped["robot_id"] == "a"
        assert scoped["chat_id"] == "default"
        assert "prompt_snapshot" in scoped
        assert "tool_history" in scoped
        assert client.get("/api/robots/not-a-robot/diagnostics").status_code == 404
