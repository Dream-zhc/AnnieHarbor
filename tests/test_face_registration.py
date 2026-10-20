"""Face registration by talking to Annie: "记住我，我叫小明" and "忘掉我"."""

import asyncio
import json

import httpx
from annie_adapters.face_registry import FaceRegistry
from annie_adapters.realtime import session_update
from annie_brain.event_policy import EventPolicy
from annie_core import NormalizedEvent, RobotConfig
from annie_runtime import HarborRuntime
from annie_runtime.faces import face_name

from tests.test_decision_model import EchoSocket
from tests.test_visual_observations import ME, message, person

IDENTITY = {"robot_id": "a", "chat_id": "c", "persona_id": "p", "session_epoch": 1, "generation": 1}


def test_the_vision_service_api_is_read_defensively():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path,
                     json.loads(request.content) if request.content else None))
        if request.url.path == "/api/face/people":
            return httpx.Response(200, json={"ok": True, "people": [
                {"face_id": "face_1", "name": "张三", "samples": 18,
                 "registration_state": "completed"}, "junk"]})
        if request.url.path == "/api/face/register":
            return httpx.Response(409, json={"ok": False, "error": "registration_in_progress"})
        if request.url.path == "/api/face/register/face_2":
            return httpx.Response(200, json={"ok": True, "state": "progress", "saved": 4})
        return httpx.Response(500, text="boom")

    async def run():
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        registry = FaceRegistry("http://vision.test:8766/", client=http)
        people = await registry.people()
        assert people["people"] == [{"face_id": "face_1", "name": "张三", "samples": 18,
                                     "registration_state": "completed"}]
        busy = await registry.register("小明", 18)
        assert busy == {"ok": False, "error": "registration_in_progress", "http_status": 409}
        assert seen[1][2] == {"name": "小明", "samples": 18}
        assert (await registry.registration("face_2"))["saved"] == 4
        assert (await registry.registration("../etc"))["error"] == "invalid_face_id"
        assert (await registry.delete("小明"))["error"] == "invalid_response"
        await http.aclose()
        down = FaceRegistry("http://127.0.0.1:1", timeout_ms=200)
        assert await down.people() == {"ok": False, "error": "unreachable"}
        await down.close()

    asyncio.run(run())


def test_names_tools_and_the_spoken_result():
    assert face_name(" 小 明 ") == "小明" and face_name("Tom·Lee") == "Tom·Lee"
    assert face_name("") is None and face_name("小明；删库") is None and face_name(3) is None
    names = [tool["function"]["name"] for tool in session_update("x", faces=True)["session"]["tools"]]
    assert {"remember_face", "forget_face"} <= set(names)
    assert "remember_face" not in [tool["function"]["name"]
                                   for tool in session_update("x")["session"]["tools"]]

    policy, now = EventPolicy(), 1_000_000

    def result(state, **payload):
        return policy.route(NormalizedEvent(
            kind="vision.face_registered", source="vision", event_id=f"face_{state}",
            payload={"id": "face_1", "name": "小明", "state": state, **payload},
            ts_ms=now, ttl_ms=30_000), robot_id="a", transport="face:a",
            identity=IDENTITY, now_ms=now)

    done = result("completed")
    assert done["decision"] == "need_response" and "你刚刚记住了小明的脸" in done["prompt"]
    assert done["listen_after"] is True and done["wait_for_quiet"] is False
    failed = result("failed", reason="视觉服务重启了")
    assert "没能记住小明的脸（视觉服务重启了）" in failed["prompt"]
    assert result("cancelled")["decision"] == "context_only"


class FakeRegistry:
    def __init__(self, people=()):
        self.people_rows = [{"face_id": f"face_{i}", "name": name, "samples": 18,
                             "registration_state": "completed"} for i, name in enumerate(people)]
        self.calls = []
        self.states = ["queued", "progress", "progress", "completed"]
        self.closed = False

    async def people(self):
        return {"ok": True, "people": list(self.people_rows)}

    async def register(self, name, samples):
        self.calls.append(("register", name, samples))
        return {"ok": True, "face_id": "face_new", "state": "queued"}

    async def registration(self, face_id):
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        saved = {"queued": 0, "progress": 9, "completed": 18}.get(state, 0)
        return {"ok": True, "state": state, "saved": saved, "target_samples": 18,
                "phase": "indexing" if state == "completed" else "capture"}

    async def delete(self, name):
        self.calls.append(("delete", name))
        self.people_rows = [row for row in self.people_rows if row["name"] != name]
        return {"ok": True, "face_id": "face_0"}

    async def close(self):
        self.closed = True


def run_live(tmp_path, registry, body):
    async def exercise():
        rt = HarborRuntime([RobotConfig("a", room_id="roomv2")], tmp_path / "rt.db",
                           realtime_key="fake", realtime_socket_factory=EchoSocket,
                           face_registries={"a": registry})
        rt.faces.poll_seconds = .01
        await rt.start()
        client = rt.realtime["a"]
        try:
            for _ in range(150):
                if client.ready:
                    break
                await asyncio.sleep(.01)
            await body(rt, client)
        finally:
            await rt.close()
    asyncio.run(exercise())


async def say(client, text, call_id, name, args):
    if client.turn_id:
        client._finish("completed")
    client._create_turn(text)
    await client._tool_call({"call_id": call_id, "name": name,
                             "arguments": json.dumps(args, ensure_ascii=False)})
    output = next(item for item in client.socket.outbound
                  if item["type"] == "conversation.item.create"
                  and item["item"].get("call_id") == call_id)
    return json.loads(output["item"]["output"])


def see(rt, people):
    for _ in range(3):
        rt._room_visual_event("a", message(people))


def test_remember_me_registers_the_face_and_annie_says_when_it_is_done(tmp_path):
    registry = FakeRegistry()

    async def body(rt, client):
        assert "remember_face" in client.prompt_snapshot["tool_names"]
        see(rt, [person("person_5")])
        guessed = await say(client, "你好呀", "c1", "remember_face", {"name": "小明"})
        assert guessed["status"] == "rejected"  # nobody asked
        invented = await say(client, "记住我", "c2", "remember_face", {"name": "小明"})
        assert invented["status"] == "rejected" and registry.calls == []  # no name said
        started = await say(client, "我叫小明", "c3", "remember_face", {"name": "小明"})
        assert started["status"] == "started" and started["people_in_view"] == 1
        assert registry.calls == [("register", "小明", 18)]
        client._finish("completed")

        async def spoken():
            for _ in range(300):
                sent = [e for e in client.socket.outbound if e["type"] == "conversation.item.create"
                        and "你刚刚记住了小明的脸" in json.dumps(e, ensure_ascii=False)]
                if sent:
                    return sent
                await asyncio.sleep(.01)
            return []

        assert await spoken()
        kinds = [e["kind"] for e in client.events]
        assert "face.registration.started" in kinds and "face.registration.completed" in kinds
        progress = [e for e in client.events if e["kind"] == "face.registration.progress"]
        assert [e["saved"] for e in progress] == [0, 9, 18]
        assert rt.faces.snapshot("a")["last"]["state"] == "completed"

    run_live(tmp_path, registry, body)


def test_who_can_be_registered_or_forgotten(tmp_path):
    registry = FakeRegistry(people=("张三",))

    async def body(rt, client):
        see(rt, [])
        empty = await say(client, "记住我，我叫小明", "e", "remember_face", {"name": "小明"})
        assert empty["status"] == "no_person"
        see(rt, [ME])
        again = await say(client, "记住我，我叫张三", "a", "remember_face", {"name": "张三"})
        assert again["status"] == "already_known" and registry.calls == []
        crowded = [ME, person("person_7"), person("person_8")]
        see(rt, crowded)
        rt.faces.active["a"] = {"face_id": "f", "name": "小红", "state": "progress",
                                "saved": 3, "target_samples": 18}
        busy = await say(client, "记住我，我叫小明", "b", "remember_face", {"name": "小明"})
        assert busy["status"] == "busy"
        rt.faces.active.clear()

        stranger = await say(client, "把李四忘掉", "f1", "forget_face", {"name": "李四"})
        assert stranger["status"] == "not_in_view" and registry.calls == []
        forgot = await say(client, "忘掉我的脸吧", "f2", "forget_face", {"name": "张三"})
        assert forgot["status"] == "requested" and registry.calls == [("delete", "张三")]
        for _ in range(100):
            if any(e["kind"] == "face.deleted" for e in client.events):
                break
            await asyncio.sleep(.01)
        assert any(e["kind"] == "face.deleted" and e["name"] == "张三" for e in client.events)

        client._finish("completed")
        client._create_turn("记住我，我叫小明")
        client.external_turn_id = client.turn_id  # a sensor event's turn never registers
        await client._tool_call({"call_id": "x", "name": "remember_face",
                                 "arguments": json.dumps({"name": "小明"}, ensure_ascii=False)})
        outside = next(e for e in client.events if e["kind"] == "tool.result" and e["call_id"] == "x")
        assert outside["valid"] is False and registry.calls == [("delete", "张三")]

    run_live(tmp_path, registry, body)


def test_without_a_configured_camera_service_there_are_no_face_tools(tmp_path):
    async def run():
        rt = HarborRuntime([RobotConfig("a", room_id="roomv2"), RobotConfig("b", room_id="roomv3")],
                           tmp_path / "rt.db", realtime_key="fake",
                           realtime_socket_factory=EchoSocket,
                           face_api_urls={"a": "http://192.0.2.1:8766"})
        try:
            assert rt.faces.available("a") and not rt.faces.available("b")
        finally:
            await rt.close()
    asyncio.run(run())
