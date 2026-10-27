"""Her mood, energy and own day (2026-10-26 first version, docs/开发记录/2026-10-26/)."""

import asyncio
import json
import time
from datetime import datetime
from types import SimpleNamespace

from annie_adapters import RuntimeStore
from annie_adapters import inner_life_store as rows
from annie_brain.inner_life import (
    APPRAISAL_QUESTION,
    BASE_VALENCE,
    BEIJING,
    LONELY_AFTER_MS,
    Mood,
    appraisal_feeling,
    diary_hint,
    diary_lines,
    event_feeling,
    feel,
    mood_hint,
    mood_label,
    mood_text,
    parse_summary,
    public_day,
    repeats,
    settle,
    summary_request,
)
from annie_core import NormalizedEvent, RobotConfig
from annie_runtime import HarborRuntime
from annie_runtime.inner_life import InnerLifeService

from tests.test_decision_model import EchoSocket, ready, utter

# 2026-10-26 10:00 Beijing: daytime, energy baseline 0.8.
MORNING = int(datetime(2026, 10, 26, 10, 0, tzinfo=BEIJING).timestamp() * 1000)
MINUTE = 60_000


def touch(level=2, sensor=1):
    return NormalizedEvent(kind="sensor.touch", source="embedded",
                           payload={"id": sensor, "level": level},
                           ts_ms=time.time_ns() // 1_000_000, ttl_ms=10_000)


# ---- pure state ----------------------------------------------------------------------
def test_a_gentle_touch_lifts_her_mood_with_the_real_reason_and_then_it_fades():
    feeling = event_feeling("sensor.touch", {"id": 1, "level": 2})
    assert feeling["diary"] == "有人轻轻按了你的额头" and feeling["diary_kind"] == "touch"
    mood = feel(Mood(updated_ms=MORNING), MORNING, valence=feeling["valence"],
                tone=feeling["tone"], reason=feeling["reason"], interaction=True)
    assert mood_label(mood, MORNING) == "心情不错"
    hint = mood_hint(mood, MORNING)
    assert "【你此刻的状态】心情不错（原因：有人轻轻按了你的额头，刚才）；精神很好。" in hint
    moved = feel(Mood(updated_ms=MORNING), MORNING, valence=.5, tone="moved", reason="对方关心了你")
    assert mood_text(moved, MORNING).startswith("心里暖暖的（原因：对方关心了你")
    assert mood_text(Mood(updated_ms=MORNING), MORNING) == "心情平静；精神很好"
    assert "别编" in hint and "开心可以带进语气里" in hint
    hurt = feel(Mood(updated_ms=MORNING), MORNING, valence=-0.5, tone="hurt", reason="对方说了几句重话")
    assert "先带点小委屈说出来，再大方原谅" in mood_hint(hurt, MORNING)
    # Nothing going on: no state block at all (replay 2026-10-27).
    assert mood_hint(Mood(updated_ms=MORNING), MORNING) == ""
    tired = feel(Mood(updated_ms=MORNING), MORNING, energy=-0.5)
    assert "有点累就答得简短些" in mood_hint(tired, MORNING)
    later = settle(mood, MORNING + 40 * MINUTE)  # two half-lives, before loneliness
    assert abs(later.valence - BASE_VALENCE) < 0.05
    assert mood_label(later, MORNING + 40 * MINUTE) == "平静"
    assert "（原因：" not in mood_hint(later, MORNING + 40 * MINUTE)


def test_a_hard_press_startles_her_and_a_release_changes_nothing():
    hard = event_feeling("sensor.pressure", {"id": 4, "level": 5})
    assert hard["tone"] == "startled" and hard["diary"] == "有人很用力拍了你的肩膀"
    mood = feel(Mood(updated_ms=MORNING), MORNING, valence=hard["valence"],
                tone=hard["tone"], reason=hard["reason"])
    assert mood_label(mood, MORNING) == "有点被吓到"
    assert event_feeling("sensor.touch", {"id": 1, "level": 0}) is None
    assert event_feeling("vision.gesture", {}) is None


def test_hours_without_anyone_make_her_bored_until_someone_comes():
    mood = feel(Mood(updated_ms=MORNING), MORNING, interaction=True)
    lonely = settle(mood, MORNING + LONELY_AFTER_MS + 30 * MINUTE)
    assert lonely.tone == "bored" and lonely.valence < BASE_VALENCE
    assert "好久没人跟你说话了" in mood_text(lonely, lonely.updated_ms)  # the card shows it
    assert mood_hint(lonely, lonely.updated_ms) == ""  # but the prompt stays quiet about it
    back = feel(lonely, lonely.updated_ms, interaction=True)
    assert back.reason == "" and back.tone == "neutral"


def test_energy_follows_the_clock_and_the_amount_of_talk():
    night = int(datetime(2026, 10, 26, 23, 30, tzinfo=BEIJING).timestamp() * 1000)
    assert settle(Mood(energy=0.8, updated_ms=night - 4 * 60 * MINUTE), night).energy < 0.4
    tired = Mood(updated_ms=MORNING)
    for _ in range(30):
        tired = feel(tired, MORNING, energy=-0.012, interaction=True)
    assert tired.energy < 0.4


def test_the_decision_model_reading_maps_to_a_small_bounded_change():
    assert APPRAISAL_QUESTION["type"] == "choice" and "hurt" in APPRAISAL_QUESTION["criteria"]
    assert appraisal_feeling({"choice": "hurt", "confidence": .8}, "张三") == (
        -0.25, "hurt", "张三说了几句重话")
    assert appraisal_feeling({"choice": "moved", "confidence": .9})[2] == "对方关心了你"
    assert appraisal_feeling({"choice": "hurt", "confidence": .2}) is None  # unsure: no change
    assert appraisal_feeling({"choice": "neutral", "confidence": .99}) is None
    assert appraisal_feeling(None) is None
    mood = Mood(updated_ms=MORNING)
    for _ in range(2):
        mood = feel(mood, MORNING, valence=-0.25, tone="hurt", reason="张三说了几句重话")
    assert mood_label(mood, MORNING) == "有点委屈"


def test_her_day_is_today_only_in_order_and_strangers_hear_no_names_or_talk():
    def row(kind, text, minutes, person=None, count=1):
        at = MORNING + minutes * MINUTE
        return {"kind": kind, "text": text, "person": person, "chat_id": None, "count": count,
                "created_ms": at, "updated_ms": at}
    day = [row("chat", "和张三聊了周末去爬山", 30, "张三"),
           row("touch", "有人轻轻按了你的额头", 20, count=3),
           row("arrival", "张三来了", 10, "张三"),
           row("touch", "昨天的事", -24 * 60)]
    assert diary_lines(day, MORNING + 40 * MINUTE) == [
        "10:10 张三来了", "10:20 有人轻轻按了你的额头（共3次）", "10:30 和张三聊了周末去爬山"]
    hint = diary_hint(day, MORNING + 40 * MINUTE)
    assert "只能讲这些，不要编别的" in hint
    # Someone else's talk: others only learn that it happened (2026-10-26 replay).
    assert "10:30 和张三聊了一会儿" in hint and "爬山" not in hint
    own = diary_hint(day, MORNING + 40 * MINUTE, "张三")
    assert "10:30 和张三聊了周末去爬山" in own and "眼前跟你说话的就是张三" in own
    public = public_day(day, MORNING + 40 * MINUTE)
    assert public == ["10:20 有人轻轻按了你的额头（共3次）", "今天有认识的人来看过你"]
    assert diary_hint([], MORNING) == ""
    assert repeats(day[1], "touch", "有人轻轻按了你的额头", MORNING + 25 * MINUTE)
    assert not repeats(day[1], "touch", "有人轻轻按了你的额头", MORNING + 40 * MINUTE)
    assert not repeats(day[0], "chat", day[0]["text"], MORNING + 31 * MINUTE)


def test_the_diary_summary_is_one_short_line_or_nothing():
    system, user = summary_request([("我下周要去面试", "加油，你准备得怎么样")], "张三")
    assert "和张三聊了" in system and json.loads(user)["对方"] == "张三"
    assert summary_request([("hi", "")], None)[1].count("一位没认出的人") == 1
    assert parse_summary('{"skip": false, "summary": "和张三聊了下周的面试"}')["summary"] == "和张三聊了下周的面试"
    assert parse_summary('{"skip": true, "summary": "调音量"}')["summary"] is None
    assert parse_summary(json.dumps({"skip": False, "summary": "长" * 61}))["summary"] is None
    parsed = parse_summary(json.dumps({"skip": True, "threads": [{"text": "下周去面试", "follow_up": "问问面试结果"}],
                                       "resolved": [3, 99], "asked": [3]}), [3])
    assert parsed["threads"] == [{"text": "下周去面试", "follow_up": "问问面试结果"}]
    assert parsed["resolved"] == [3] and parsed["asked"] == [3]  # unknown ids are ignored


# ---- SQLite rows ----------------------------------------------------------------------
def test_rows_survive_a_restart_and_forget_by_chat_or_person(tmp_path):
    store = RuntimeStore(tmp_path / "d.db")
    store.migrate()
    rows.save_mood(store, "a", Mood(valence=.4, updated_ms=MORNING).to_row())
    assert Mood.from_row(rows.load_mood(store, "a")).valence == .4
    assert rows.load_mood(store, "b") is None
    old = rows.add_experience(store, "a", "touch", "很久以前", now_ms=MORNING - 9 * 86_400_000,
                              keep_after_ms=0)
    first = rows.add_experience(store, "a", "chat", "和张三聊了爬山", now_ms=MORNING,
                                keep_after_ms=MORNING - 7 * 86_400_000,
                                person="张三", chat_id="c1")
    rows.add_experience(store, "a", "touch", "有人摸了你的头顶", now_ms=MORNING + 1,
                        keep_after_ms=MORNING - 7 * 86_400_000)
    rows.bump_experience(store, "a", first["id"], now_ms=MORNING + 2)
    recent = rows.recent_experiences(store, "a", since_ms=0)
    assert [row["text"] for row in recent] == ["有人摸了你的头顶", "和张三聊了爬山"]
    assert recent[1]["count"] == 2 and old["id"] not in {row["id"] for row in recent}
    assert rows.forget_person(store, "a", "张三") == 1
    rows.add_experience(store, "a", "chat", "和李四聊了天气", now_ms=MORNING + 3,
                        keep_after_ms=0, chat_id="c2")
    assert rows.forget_chat(store, "a", "c2") == 1


# ---- service --------------------------------------------------------------------------
class FakeClient:
    def __init__(self, answer=None, error=None):
        self.answer, self.error = answer, error
        self.changed = 0
        self.events = []
        self.asked = []
        self.speakers = SimpleNamespace(public=lambda: {"status": "matched", "name": "张三"})

    def inner_changed(self):
        self.changed += 1

    def publish(self, kind, **fields):
        self.events.append({"kind": kind, **fields})

    async def ask_decision(self, state, questions):
        self.asked.append((state, questions))
        if self.error is not None:
            raise self.error
        return {"mood": self.answer}, "d3-test"


class FakeSummarizer:
    model = "fake-flash"

    def __init__(self, reply='{"skip": false, "summary": "和张三聊了下周的面试"}'):
        self.reply = reply
        self.calls = []

    async def complete(self, system, user):
        self.calls.append((system, user))
        return self.reply


def service_with(tmp_path, client, summarizer=None, now=None):
    store = RuntimeStore(tmp_path / "d.db")
    store.migrate()
    clock = {"now": now or MORNING}
    runtime = SimpleNamespace(store=store, robots={"a": SimpleNamespace(chat_id="c1")},
                              realtime={"a": client}, room_monitors={})
    service = InnerLifeService(runtime, summarizer=summarizer, close_seconds=120,
                               clock=lambda: clock["now"])
    return service, clock


async def settled(service):
    for _ in range(100):
        if not service.tasks:
            return
        await asyncio.gather(*list(service.tasks), return_exceptions=True)


def test_a_touch_changes_mood_and_day_once_and_repeats_count_on_one_line(tmp_path):
    async def run():
        client = FakeClient()
        service, clock = service_with(tmp_path, client)
        await service.start()
        try:
            service.on_event("a", touch())
            assert client.changed == 1
            assert "有人轻轻按了你的额头" in service.hint("a")
            clock["now"] += MINUTE
            service.on_event("a", touch())
            await settled(service)
            assert service.day["a"][0]["count"] == 2 and len(service.day["a"]) == 1
            stored = rows.recent_experiences(service.store, "a", since_ms=0)
            assert stored[0]["count"] == 2
            snapshot = service.snapshot("a")
            assert snapshot["mood"]["label"] == "心情不错"
            assert snapshot["today"][0]["text"] == "有人轻轻按了你的额头"
            assert snapshot["hint_in_session"] is False  # nothing was sent to a live session
            assert any(e["kind"] == "inner_life.changed" for e in client.events)
        finally:
            await service.close()
    asyncio.run(run())


def test_each_turn_tires_her_and_the_decision_model_reading_moves_her_mood(tmp_path):
    async def run():
        client = FakeClient({"choice": "moved", "confidence": .9})
        service, _clock = service_with(tmp_path, client)
        await service.start()
        try:
            before = settle(service.moods["a"], MORNING).energy
            service.on_turn("a", chat_id="c1", user_text="今天辛苦你了", reply_text="不辛苦！",
                            input_mode="audio", client=client)
            await settled(service)
            assert client.asked[0][1] == {"mood": APPRAISAL_QUESTION}
            assert client.asked[0][0] == {"用户": "今天辛苦你了", "小安妮回复": "不辛苦！"}
            assert service.moods["a"].energy < before
            assert service.moods["a"].reason == "张三关心了你"
            assert "张三关心了你" in service.hint("a")
            assert service.stats["a"]["last_appraisal"]["applied"] is True
        finally:
            await service.close()
    asyncio.run(run())


def test_no_decision_model_or_a_failure_leaves_her_mood_alone(tmp_path):
    async def run():
        for error, failures in ((LookupError("none up"), 0), (RuntimeError("boom"), 1)):
            client = FakeClient(error=error)
            service, _clock = service_with(tmp_path, client)
            await service.start()
            try:
                service.on_turn("a", chat_id="c1", user_text="你好", reply_text="你好呀",
                                input_mode="text", client=client)
                await settled(service)
                assert service.moods["a"].reason == ""
                assert service.stats["a"]["appraisal_failures"] == failures
            finally:
                await service.close()
    asyncio.run(run())


def test_a_finished_conversation_becomes_one_named_diary_line(tmp_path):
    async def run():
        client = FakeClient({"choice": "neutral", "confidence": .9})
        summarizer = FakeSummarizer()
        service, clock = service_with(tmp_path, client, summarizer)
        await service.start()
        try:
            for text in ("我下周要去面试", "有点紧张"):
                service.on_turn("a", chat_id="c1", user_text=text, reply_text="嗯嗯",
                                input_mode="audio", client=client)
            assert service.snapshot("a")["conversation"]["turns"] == 2
            clock["now"] += 3 * MINUTE
            service._close_stretch("a")
            await settled(service)
            system, user = summarizer.calls[0]
            assert "和张三聊了" in system and "有点紧张" in user
            line = service.day["a"][0]
            assert (line["kind"], line["text"], line["person"], line["chat_id"]) == (
                "chat", "和张三聊了下周的面试", "张三", "c1")
            assert line["created_ms"] == MORNING  # when they talked, not when it was written
            assert "10:00 和张三聊了下周的面试" in service.my_day("a")["today"]
            assert "和张三聊了" not in service.hint("a")  # her day is looked up, not always shown
            # Deleting the Chat takes the line, also from SQLite.
            service.on_chat_deleted("a", "c1")
            await settled(service)
            assert service.day["a"] == []
            assert rows.recent_experiences(service.store, "a", since_ms=0) == []
        finally:
            await service.close()
    asyncio.run(run())


def test_typed_turns_are_not_put_on_a_voice_and_short_chit_chat_without_summary_is_dropped(tmp_path):
    async def run():
        client = FakeClient({"choice": "neutral", "confidence": .9})
        service, _clock = service_with(tmp_path, client)  # no summarizer
        await service.start()
        try:
            service.on_turn("a", chat_id="c1", user_text="几点了", reply_text="十点",
                            input_mode="text", client=client)
            assert service.stretch["a"]["who"] is None
            service._close_stretch("a")
            await settled(service)
            assert service.day["a"] == []  # one turn, no summarizer: nothing worth a line
        finally:
            await service.close()
    asyncio.run(run())


def test_forget_me_removes_her_lines_about_that_person(tmp_path):
    async def run():
        client = FakeClient()
        service, _clock = service_with(tmp_path, client)
        await service.start()
        try:
            service.on_event("a", NormalizedEvent(
                kind="vision.known_arrival", source="vision", payload={"name": "张三"},
                ts_ms=time.time_ns() // 1_000_000, ttl_ms=10_000))
            service.on_event("a", touch())
            await settled(service)
            assert "10:00 张三来了" in service.my_day("a")["today"]
            service.on_person_forgotten("a", "张三")
            await settled(service)
            assert not any("张三" in line for line in service.my_day("a")["today"])
            assert [row["text"] for row in rows.recent_experiences(
                service.store, "a", since_ms=0)] == ["有人轻轻按了你的额头"]
        finally:
            await service.close()
    asyncio.run(run())


def test_mood_and_day_come_back_after_a_restart(tmp_path):
    async def run():
        client = FakeClient()
        service, clock = service_with(tmp_path, client)
        await service.start()
        service.on_event("a", touch())
        await settled(service)
        await service.close()
        again, _ = service_with(tmp_path, client, now=clock["now"] + MINUTE)
        await again.start()
        try:
            assert again.moods["a"].reason == "有人轻轻按了你的额头"
            assert again.day["a"][0]["text"] == "有人轻轻按了你的额头"
        finally:
            await again.close()
    asyncio.run(run())


def test_idle_planner_material_keeps_names_and_talk_away_from_strangers(tmp_path):
    async def run():
        client = FakeClient()
        service, _clock = service_with(tmp_path, client)
        await service.start()
        try:
            service.on_event("a", NormalizedEvent(
                kind="vision.known_arrival", source="vision", payload={"name": "张三"},
                ts_ms=time.time_ns() // 1_000_000, ttl_ms=10_000))
            public = service.idle_material("a", public=True)
            assert "张三" not in public["own_day"] and "认识的人来看过你" in public["own_day"]
            assert public["mood"] == "心情不错；精神很好"  # the reason names 张三: left out
            private = service.idle_material("a", public=False)
            assert private["own_day"] == "" and "张三来了" in private["mood"]
        finally:
            await service.close()
    asyncio.run(run())


# ---- runtime: her state reaches the Realtime session -------------------------------
class ByQuestion:
    """Decision model answering each question by its key."""

    def __init__(self, mood):
        self.mood = mood
        self.calls = []

    async def ask(self, state, questions):
        self.calls.append((state, questions))
        answers = {"to": {"choice": "robot", "confidence": .95},
                   "face": {"choice": "none", "confidence": .9}, "mood": self.mood}
        return {key: answers.get(key, {"choice": "none", "confidence": .5}) for key in questions}

    async def close(self):
        pass


def test_touch_and_conversation_reach_the_session_instructions(tmp_path):
    decision = ByQuestion({"choice": "happy", "confidence": .9})

    async def run():
        rt = HarborRuntime([RobotConfig("a", room_id="roomv2")], tmp_path / "d.db",
                           realtime_key="fake", realtime_socket_factory=EchoSocket,
                           decision_client=decision, addressee_enabled=True,
                           inner_life_enabled=True)
        client = await ready(rt)
        try:
            sent = [p["session"]["instructions"] for p in client.socket.outbound
                    if p["type"] == "session.update"]
            assert "【你此刻的状态】" not in sent[0]  # calm: nothing to say about it
            tools = next(p for p in client.socket.outbound if p["type"] == "session.update"
                         and "tools" in p["session"])["session"]["tools"]
            assert any(tool["function"]["name"] == "recall_my_day" for tool in tools)
            await utter(client, "u1", "你真厉害")
            for _ in range(100):
                if rt.inner_life.moods["a"].reason == "和对方聊得挺开心":
                    break
                await asyncio.sleep(.02)
            assert any("mood" in questions for _state, questions in decision.calls)
            assert rt.inner_life.moods["a"].reason == "和对方聊得挺开心"
            assert rt.inner_life.snapshot("a")["conversation"]["turns"] == 1
            rt.events.route("a", touch(), "room:roomv2:main-event")
            for _ in range(200):
                sent = [p["session"]["instructions"] for p in client.socket.outbound
                        if p["type"] == "session.update"]
                if "有人轻轻按了你的额头" in sent[-1]:
                    break
                await asyncio.sleep(.02)
            assert "【你今天的真实经历" not in sent[-1]
            await client._tool_call({"call_id": "d1", "name": "recall_my_day", "arguments": "{}"})
            result = next(e for e in client.events if e["kind"] == "tool.result"
                          and e.get("call_id") == "d1")["result"]
            assert result["status"] == "ok" and result["today"][-1].endswith("有人轻轻按了你的额头")
            assert "心情" in sent[-1] and "原因：有人轻轻按了你的额头" in sent[-1]
            for _ in range(100):
                if rt.inner_life.snapshot("a")["hint_in_session"]:
                    break
                await asyncio.sleep(.02)
            assert rt.inner_life.snapshot("a")["hint_in_session"] is True
        finally:
            await rt.close()
    asyncio.run(run())


def test_inner_life_is_off_unless_configured(tmp_path):
    rt = HarborRuntime([RobotConfig("a", room_id="roomv2")], tmp_path / "d.db")
    assert rt.inner_life is None


# ---- idle planner: her mood and bodily day as material -----------------------------
def test_idle_planner_gets_her_mood_and_bodily_day_but_no_names():
    import httpx
    from annie_adapters.idle import QwenIdleModel
    from annie_brain.idle_plan import build_idle_graph

    async def run():
        seen = []

        def respond(request):
            payload = json.loads(request.content)
            seen.append((payload["messages"][0]["content"],
                         json.loads(payload["messages"][1]["content"])))
            return httpx.Response(200, json={"choices": [{"message": {
                "content": '{"decision":"SPEAK","topic":"说说刚才被摸头","new_value":true}'}}]})

        model = QwenIdleModel("fake", "https://example.test/v1",
                              transport=httpx.MockTransport(respond))
        try:
            graph = build_idle_graph(model)
            await graph.ainvoke({"attempt": 1, "scene": "有人站在桌边", "previous": "",
                                 "mood": "心情不错", "own_day": "10:20 有人摸了你的头顶"})
            await graph.ainvoke({"mode": "continuation", "user_text": "我先走了",
                                 "assistant_text": "好，路上小心", "mood": "有点累了"})
        finally:
            await model.close()
        system, user = seen[0]
        assert user["own_day"] == "10:20 有人摸了你的头顶" and user["mood"] == "心情不错"
        assert "own_day里小安妮自己今天真实经历过的事" in system and "不编造" in system
        system, user = seen[1]
        assert user["mood"] == "有点累了" and "own_day" not in user
        assert "mood是小安妮自己此刻真实的心情" in system

    asyncio.run(run())
