"""Face registration by talking to Annie: "记住我，我叫小明" and "忘掉我"."""

import asyncio
import json
import time

import httpx
from annie_adapters.face_registry import FaceRegistry
from annie_adapters.realtime import session_update
from annie_brain.event_policy import EventPolicy
from annie_core import NormalizedEvent, RobotConfig
from annie_runtime import HarborRuntime
from annie_runtime.faces import face_name, heard_name, same_person, sounds_alike
from annie_runtime.realtime import (
    asked_to_confirm_forget,
    clean_utterance,
    forget_requested,
    introduces_other,
)

from tests.test_decision_model import EchoSocket, FakeDecision
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


def test_a_name_heard_with_other_characters_is_matched_by_sound():
    # 11-02: ASR wrote 康兴 as 康星 and 康欣; 海城 and 海超 are two names.
    assert sounds_alike("康兴", "康星") and sounds_alike("康兴", "康欣") and sounds_alike("张三", "章山")
    assert not sounds_alike("海城", "海超") and not sounds_alike("康兴", "康兴宇")
    assert heard_name("康兴", ["我叫康星"]) == "康星" and heard_name("小明", ["你好呀"]) is None
    assert heard_name("玉箫", ["你记住他叫玉香"]) is None
    assert heard_name("玉箫", ["你记住他叫玉香"], earlier=["她叫玉箫"]) == "玉香"
    assert same_person("康欣", ["张林", "康兴"]) == "康兴" and same_person("海超", ["海城"]) is None


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
        assert again["status"] == "recognized" and again["name"] == "张三"
        see(rt, [dict(ME, label="一位朋友", name=None, known=False)])  # filed, not recognised now
        filed = await say(client, "记住我，我叫章山", "k", "remember_face", {"name": "章山"})
        assert filed["status"] == "already_known" and filed["name"] == "张三"
        assert registry.calls == []
        crowded = [ME, person("person_7"), person("person_8")]
        see(rt, crowded)
        rt.faces.active["a"] = {"face_id": "f", "name": "小红", "state": "progress",
                                "saved": 3, "target_samples": 18}
        busy = await say(client, "记住我，我叫小明", "b", "remember_face", {"name": "小明"})
        assert busy["status"] == "busy"
        rt.faces.active.clear()

        stranger = await say(client, "把李四忘掉", "f1", "forget_face", {"name": "李四"})
        assert stranger["status"] == "not_known" and registry.calls == []
        asked = await say(client, "忘掉我的脸吧", "f2", "forget_face", {"name": "张三"})
        assert asked["status"] == "confirm" and asked["name"] == "张三" and registry.calls == []
        forgot = await say(client, "确定", "f3", "forget_face", {"name": "张三"})
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


def test_someone_recognised_at_the_side_is_the_speaker_and_a_misheard_name_still_counts(tmp_path):
    registry = FakeRegistry(people=("康兴", "张林"))
    kang = person("person_3", name="康兴", text="在左边")

    async def body(rt, client):
        see(rt, [person("person_9", gaze=True, text="在我面前"), kang])  # 11-02 19:03
        chat = await say(client, "今天好热啊", "r0", "remember_face", {"name": "海超"})
        assert chat["status"] == "rejected" and "不是在请你记住" in chat["instruction"]
        seen = await say(client, "记住我，我叫康星", "r1", "remember_face", {"name": "康兴"})
        assert seen["status"] == "recognized" and "画面里的康兴就是TA" in seen["instruction"]
        spelled = await say(client, "我叫康欣", "r2", "remember_face", {"name": "康欣"})
        assert spelled["name"] == "康兴" and "“康欣”是同音的写法" in spelled["instruction"]
        other = await say(client, "记住我，我叫海城", "r3", "remember_face", {"name": "海超"})
        assert other["status"] == "rejected" and "原话里的名字" in other["instruction"]
        assert registry.calls == []
        asked = await say(client, "把康星忘掉吧", "f1", "forget_face", {"name": "康星"})
        assert asked["status"] == "confirm" and "忘掉康兴的脸" in asked["instruction"]
        forgot = await say(client, "嗯，忘了吧", "f2", "forget_face", {"name": "康兴"})
        assert forgot["status"] == "requested" and registry.calls == [("delete", "康兴")]

    run_live(tmp_path, registry, body)


def test_in_a_crowd_a_face_she_knows_is_never_filed_under_the_speaker_s_name(tmp_path):
    registry = FakeRegistry(people=("康兴", "张林"))
    kang = person("person_3", name="康兴", gaze=True, text="在我面前")
    newcomer = person("person_9", text="在左边")

    async def body(rt, client):
        see(rt, [kang, newcomer])  # 11-02 19:07: 张林 spoke while she looked at 康兴
        filed = await say(client, "记住我，我叫张林", "z1", "remember_face", {"name": "张林"})
        assert filed["status"] == "already_known" and filed["name"] == "张林"
        first = await say(client, "记住我，我叫王五", "z2", "remember_face", {"name": "王五"})
        again = await say(client, "对，我就叫王五，记住我", "z3", "remember_face", {"name": "王五"})
        assert first["status"] == again["status"] == "known_as_other" and again["known_as"] == "康兴"
        assert "站到你正前方" in again["instruction"] and registry.calls == []
        see(rt, [dict(newcomer, is_gaze_target=True), dict(kang, is_gaze_target=False)])
        started = await say(client, "记住我，我叫王五", "z4", "remember_face", {"name": "王五"})
        assert started["status"] == "started" and registry.calls == [("register", "王五", 18)]

    run_live(tmp_path, registry, body)


def test_the_picture_s_name_stands_and_a_wrong_one_is_forgotten_then_filed_again(tmp_path):
    # 11-03 16:58: “我是X” filed a face the picture knew by another name. User rule: only a
    # face she does not know yet is filed; a wrong name is forgotten first, never renamed.
    tools = {tool["function"]["name"]: tool["function"]["description"]
             for tool in session_update("x", faces=True)["session"]["tools"]}
    assert "forgetting the face first" in tools["remember_face"]
    registry = FakeRegistry(people=("张三",))

    async def body(rt, client):
        setup = next(item for item in client.socket.outbound if item["type"] == "session.update")
        assert "已记的名字错了只能先忘掉再重新记" in setup["session"]["instructions"]
        see(rt, [dict(ME, is_gaze_target=False)])  # alone, the gaze flag lost on a missed frame
        claimed = await say(client, "我是李四", "w1", "remember_face", {"name": "李四"})
        assert claimed["status"] == "known_as_other" and claimed["known_as"] == "张三"
        assert "先让你忘掉TA的脸，再重新记" in claimed["instruction"]
        insisted = await say(client, "对，我就叫李四，记住我", "w2", "remember_face", {"name": "李四"})
        assert insisted["status"] == "known_as_other" and registry.calls == []
        see(rt, [ME])
        asked = await say(client, "忘掉我的脸吧", "w3", "forget_face", {"name": "张三"})
        forgot = await say(client, "确定", "w4", "forget_face", {"name": "张三"})
        assert asked["status"] == "confirm" and forgot["status"] == "requested"
        see(rt, [ME])  # the picture still names the face for a few observations
        filed = await say(client, "记住我，我叫李四", "w5", "remember_face", {"name": "李四"})
        assert filed["status"] == "started"
        assert registry.calls == [("delete", "张三"), ("register", "李四", 18)]

    run_live(tmp_path, registry, body)


class UnconfirmedDeletes(FakeRegistry):
    async def delete(self, name):
        self.calls.append(("delete", name))
        return {"ok": True, "face_id": "face_0"}  # accepted, yet the face stays filed


def test_a_forget_that_never_lands_leaves_the_picture_s_name_in_force(tmp_path, monkeypatch):
    monkeypatch.setattr("annie_runtime.faces.DELETE_CHECK_SECONDS", .05)
    registry = UnconfirmedDeletes(people=("张三",))

    async def body(rt, client):
        see(rt, [ME])
        await say(client, "忘掉我的脸吧", "u1", "forget_face", {"name": "张三"})
        forgot = await say(client, "确定", "u2", "forget_face", {"name": "张三"})
        assert forgot["status"] == "requested"
        for _ in range(100):
            if any(e["kind"] == "face.delete.unconfirmed" for e in client.events):
                break
            await asyncio.sleep(.01)
        assert any(e["kind"] == "face.delete.unconfirmed" for e in client.events)
        see(rt, [ME])
        claimed = await say(client, "我叫李四", "u3", "remember_face", {"name": "李四"})
        assert claimed["status"] == "known_as_other" and registry.calls == [("delete", "张三")]

    run_live(tmp_path, registry, body)


def test_other_wordings_of_forget_me_go_to_the_decision_model(tmp_path):
    registry = FakeRegistry(people=("张三",))

    async def body(rt, client):
        see(rt, [ME])
        client.decision = FakeDecision({"choice": "no", "confidence": .8},
                                       {"choice": "yes", "confidence": .9})
        joke = await say(client, "你会不会把我的脸弄丢啊", "g1", "forget_face", {"name": "张三"})
        assert joke["status"] == "rejected" and registry.calls == []
        asked = await say(client, "把我的脸弄掉吧", "g2", "forget_face", {"name": "张三"})
        assert asked["status"] == "confirm" and registry.calls == []
        state, questions = client.decision.calls[-1]
        assert state["当前这句话"] == "把我的脸弄掉吧"
        assert "忘掉“张三”的脸" in questions["intent"]["instructions"]
        forgot = await say(client, "好的", "g3", "forget_face", {"name": "张三"})
        assert forgot["status"] == "requested" and registry.calls == [("delete", "张三")]
        assert len(client.decision.calls) == 2  # a plain yes needs no model

    run_live(tmp_path, registry, body)


def test_phrase_rules_read_cleaned_asr_text():
    assert clean_utterance("忘， 了 我吧！") == "忘了我吧" and clean_utterance("OK") == "ok"
    assert forget_requested("忘了我吧") and forget_requested("别再认我了") and forget_requested("把我删了")
    assert not forget_requested("不要忘了我") and not forget_requested("别把我忘了")
    assert introduces_other("你记住他叫玉箫", "玉箫") and introduces_other("这是我朋友玉箫", "玉箫")
    assert introduces_other("记住她", "玉箫") and introduces_other("那个是玉箫", "玉箫")
    assert not introduces_other("记住我，我叫玉箫", "玉箫") and not introduces_other("这是我，玉箫", "玉箫")
    assert asked_to_confirm_forget("确定要我忘掉你吗？") and asked_to_confirm_forget("要我把你忘掉吗")
    assert not asked_to_confirm_forget("你不会忘了带伞吧？") and not asked_to_confirm_forget("好的，我会忘掉你。")


def test_nobody_is_registered_on_someone_else_s_behalf(tmp_path):
    registry = FakeRegistry()

    async def body(rt, client):
        see(rt, [person("person_5", gaze=True, text="在我面前")])
        client.decision = FakeDecision({"choice": "other", "confidence": .9})
        judged = await say(client, "这位就是玉箫", "o1", "remember_face", {"name": "玉箫"})
        assert judged["status"] == "rejected" and "不替别人记" in judged["instruction"]
        for call_id, text in (("o2", "你记住他叫玉箫"), ("o3", "这是我朋友玉箫，记住她")):
            other = await say(client, text, call_id, "remember_face", {"name": "玉箫"})
            assert other["status"] == "rejected" and "不替别人记" in other["instruction"]
        assert len(client.decision.calls) == 1 and registry.calls == []  # the rules saw those
        mine = await say(client, "记住我，我叫小明", "o4", "remember_face", {"name": "小明"})
        assert mine["status"] == "started" and registry.calls == [("register", "小明", 18)]

    run_live(tmp_path, registry, body)


def test_a_refused_face_call_never_stops_the_next_one(tmp_path):
    # 11-02 19:03: after one refusal said “不要重试”, three self-introductions in a row
    # got no remember_face call at all.
    tools = {tool["function"]["name"]: tool["function"]["description"]
             for tool in session_update("x", faces=True)["session"]["tools"]}
    assert "covers only that sentence" in tools["remember_face"]
    assert "as soon as their next words agree" in tools["forget_face"]
    registry = FakeRegistry()

    async def body(rt, client):
        setup = next(item for item in client.socket.outbound if item["type"] == "session.update")
        assert "画面里没认出TA也照样调用" in setup["session"]["instructions"]
        see(rt, [person("person_5", gaze=True, text="在我面前")])
        refused = await say(client, "今天天气不错", "r1", "remember_face", {"name": "小明"})
        assert refused["status"] == "rejected" and "照常调用remember_face" in refused["instruction"]
        assert "不要重试" not in refused["instruction"] and "记住我" not in refused["instruction"]
        other = await say(client, "你记住他叫玉箫", "r2", "remember_face", {"name": "玉箫"})
        assert other["status"] == "rejected" and "照常调用remember_face" in other["instruction"]
        mine = await say(client, "我叫小明", "r3", "remember_face", {"name": "小明"})
        assert mine["status"] == "started" and registry.calls == [("register", "小明", 18)]

    run_live(tmp_path, registry, body)


def test_forgetting_asks_once_and_a_no_keeps_the_face(tmp_path):
    registry = FakeRegistry(people=("张三",))

    async def body(rt, client):
        see(rt, [ME])
        client.decision = FakeDecision({"choice": "no", "confidence": .9},
                                       {"choice": "yes", "confidence": .8})
        keep = await say(client, "小安妮，不要忘了我哦", "n1", "forget_face", {"name": "张三"})
        assert keep["status"] == "rejected"  # the decision model: not a request
        asked = await say(client, "忘，了 我吧", "n2", "forget_face", {"name": "张三"})
        assert asked["status"] == "confirm" and len(client.decision.calls) == 1
        assert "嗯、对、确定、忘吧" in asked["instruction"]
        assert "马上再调用一次forget_face" in asked["instruction"]
        kept = await say(client, "算了，还是记着吧", "n3", "forget_face", {"name": "张三"})
        assert kept["status"] == "kept" and registry.calls == []
        again = await say(client, "忘掉我", "n4", "forget_face", {"name": "张三"})
        assert again["status"] == "confirm"  # a no closed the question
        name, _, turn = client._forget_asked
        client._forget_asked = (name, time.monotonic() - 61, turn)
        late = await say(client, "确定", "n5", "forget_face", {"name": "张三"})
        assert late["status"] == "confirm" and registry.calls == []  # asked too long ago
        unsure = await say(client, "行吧那就这样", "n6", "forget_face", {"name": "张三"})
        assert unsure["status"] == "requested" and registry.calls == [("delete", "张三")]
        state, questions = client.decision.calls[-1]
        assert "答应" in questions["intent"]["instructions"] and state["当前这句话"] == "行吧那就这样"

    run_live(tmp_path, registry, body)


def test_when_she_asked_herself_one_yes_forgets(tmp_path):
    registry = FakeRegistry(people=("张三",))

    async def body(rt, client):
        see(rt, [ME])
        client._create_turn("忘了我吧")  # she asked before calling forget_face
        client._last_reply_text = "确定要我忘掉你的脸吗？忘了就认不出你啦。"
        forgot = await say(client, "小安妮，嗯嗯，确定", "s1", "forget_face", {"name": "张三"})
        assert forgot["status"] == "requested" and registry.calls == [("delete", "张三")]

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
