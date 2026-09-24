"""Persona is a durable role; Chat memory is a separate, bounded scope."""

import asyncio
import sqlite3
import time

import pytest
from annie_adapters import RuntimeStore
from annie_brain.memory import MemoryService, safe_extracted_scope
from annie_brain.persona import session_instructions
from annie_core import RobotConfig
from annie_runtime import HarborRuntime
from annie_web import create_app
from fastapi.testclient import TestClient


class FakeCloud:
    async def close(self):
        pass

    async def embed(self, _text):
        return [1., 0.]

    async def extract(self, user, existing):
        if "别骂" in user:
            return [{"kind": "chat", "key": "current_tone", "text": "当前聊天不要骂我"}]
        return []


def test_temporary_prohibition_is_not_automatically_promoted_to_long_term():
    assert safe_extracted_scope("persona", "你不能骂我") == "chat"
    assert safe_extracted_scope("semantic", "不要骂我") == "chat"
    assert safe_extracted_scope("episodes", "下周有朋友来") == "chat"
    assert safe_extracted_scope("persona", "我喜欢你") == "persona"
    assert safe_extracted_scope("semantic", "以后一直不要骂我") == "semantic"


def test_chat_and_long_term_scope_correction_expiry_and_restart(tmp_path):
    async def exercise():
        db = tmp_path / "runtime.db"
        store = RuntimeStore(db)
        store.migrate()
        memory = MemoryService(store, FakeCloud(), ("a", "b"))
        await memory.put("a", "user-role", "persona", "friend", "用户曾说喜欢小安妮")
        await memory.put("a", "user-role", "semantic", "tea", "用户喜欢茶")
        temporary = await memory.chat_put("a", "user-role", "chat-1", "tone", "不要骂我")
        assert temporary is not None
        assert len(await memory.chat_list("a", "user-role", "chat-1")) == 1
        assert await memory.chat_list("a", "user-role", "chat-2") == []
        assert await memory.chat_list("a", "other-role", "chat-1") == []
        assert await memory.chat_list("b", "user-role", "chat-1") == []
        assert len(await memory.list("a", "user-role")) == 2
        assert "不要骂我" in await memory.bootstrap("a", "user-role", "chat-1")
        assert "不要骂我" not in await memory.bootstrap("a", "user-role", "chat-2")
        assert "用户喜欢茶" in await memory.bootstrap("a", "user-role", "chat-2")
        assert "用户曾说喜欢小安妮" not in await memory.bootstrap("a", "other-role", "chat-2")
        ident, version = temporary
        assert (await memory.chat_change("a", "user-role", "chat-1", ident, version,
                                        "当前聊天少开玩笑"))["version"] == 2
        with pytest.raises(ValueError, match="version conflict"):
            await memory.chat_change("a", "user-role", "chat-1", ident, 1, "旧内容")
        await memory.chat_change("a", "user-role", "chat-1", ident, 2)
        assert await memory.chat_list("a", "user-role", "chat-1") == []
        assert await memory.chat_put("a", "user-role", "chat-1", "tone", "复活") is None
        assert len(await memory.list("a", "user-role")) == 2
        expires = int(time.time() * 1000) + 30
        await memory.chat_put("a", "user-role", "chat-1", "temp", "短暂有效",
                              expires_at_ms=expires)
        await asyncio.sleep(.05)
        assert await memory.chat_list("a", "user-role", "chat-1") == []
        assert len(await memory.chat_list("a", "user-role", "chat-1", True)) == 2
        store.migrate()
        assert len(await memory.list("a", "user-role")) == 2
        await memory.close()

    asyncio.run(exercise())


def test_upgrade_from_existing_v6_preserves_long_term_rows(tmp_path):
    store = RuntimeStore(tmp_path / "legacy.db")
    store.migrate()
    with sqlite3.connect(store.path) as db:
        db.execute("DROP TABLE chat_memories")
        db.execute("PRAGMA user_version=6")
        db.execute("""INSERT INTO long_term_memories
            (id,robot_id,persona_id,kind,fact_key,text,source_created_ms,
             version,status,updated_ms,index_state)
            VALUES('old','a','annie-lively','persona','old-key','旧记忆',1,1,'active',1,'ready')""")
    store.migrate()
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT text FROM long_term_memories WHERE id='old'").fetchone()[0] == "旧记忆"
        assert db.execute("PRAGMA user_version").fetchone()[0] == RuntimeStore.SCHEMA_VERSION
        assert db.execute("SELECT COUNT(*) FROM chat_memories").fetchone()[0] == 0
        assert db.execute("SELECT persona_id FROM persona_instances").fetchone()[0] == "annie-lively"


def test_activation_and_chat_memory_api_are_bound_to_server_session(tmp_path):
    rt = HarborRuntime([RobotConfig("a", room_id="roomv2", fake_delay_ms=0)],
                       tmp_path / "live.db", memory_model=FakeCloud(),
                       persona_profiles={"annie": {"label": "自然", "style": "自然说话"},
                                         "annie-lively": {"label": "活泼", "style": "俏皮说话"}})
    with TestClient(create_app(rt, tmp_path)) as client:
        before = client.get("/api/robots/a/session").json()
        created = client.post("/api/robots/a/activate", json={
            "persona_id": "customer-1", "chat_id": "chat-1",
            "style_id": "annie-lively",
        })
        assert created.status_code == 200, created.text
        active = created.json()
        assert active["persona_id"] == "customer-1"
        assert client.post("/api/robots/a/persona-instances", json={
            "persona_id": "customer-1", "display_name": "演示小安妮",
        }).status_code == 200
        assert any(role["display_name"] == "演示小安妮" for role in
                   client.get("/api/robots/a/persona-instances").json())
        assert any(chat["chat_id"] == "chat-1" and chat["turn_count"] == 0 for chat in
                   client.get("/api/robots/a/conversations").json())
        social = client.get("/api/robots/a/social-state").json()
        assert social["persona_style"]["value"] == "annie-lively"
        instructions, snapshot = session_instructions("customer-1", rt.persona_profiles, social)
        assert snapshot["style_id"] == "annie-lively"
        assert "当前Persona标识：customer-1" in instructions
        base = "/api/robots/a/chat-memories"
        payload = {"key": "tone", "text": "今天不要骂我", "chat_id": "chat-1",
                   "persona_id": "customer-1", "generation": active["generation"]}
        assert client.post(base, json={**payload, "generation": before["generation"]}).status_code == 409
        put = client.post(base, json=payload)
        assert put.status_code == 200, put.text
        ident = put.json()["id"]
        assert len(client.get(base).json()["memories"]) == 1
        assert client.post("/api/robots/a/activate", json={
            "chat_id": "chat-2", "persona_id": "customer-1",
        }).status_code == 200
        assert client.get(base).json()["memories"] == []
        assert any(chat["chat_id"] == "chat-1" for chat in
                   client.get("/api/robots/a/conversations").json())
        assert client.request("DELETE", base + "/" + ident,
                              json={"version": 1, "chat_id": payload["chat_id"],
                                    "persona_id": payload["persona_id"],
                                    "generation": payload["generation"]}).status_code == 409
        assert client.post("/api/robots/a/activate", json={
            "chat_id": "chat-1", "persona_id": "customer-1",
        }).status_code == 200
        assert client.get(base).json()["memories"][0]["text"] == "今天不要骂我"
