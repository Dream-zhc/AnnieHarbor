"""Open threads, nightly reflection, her own wish to speak and interaction motions (2026-10-26)."""

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from annie_adapters import RuntimeStore
from annie_adapters import inner_life_store as rows
from annie_brain.action_catalog import load_action_catalog
from annie_brain.decision_policy import (
    INNER_MOTIONS,
    LISTEN_MOTIONS,
    SPEECH_MOTIONS,
    motion_pick,
    sentences,
    speech_motion_question,
)
from annie_brain.event_policy import EventPolicy
from annie_brain.inner_life import (
    BEIJING,
    THREAD_MAX_ASKS,
    THREAD_REASK_MS,
    THREAD_TTL_MS,
    moment_now,
    parse_reflection,
    reflection_hint,
    reflection_request,
    thought_prompt,
    thread_visible,
    threads_hint,
)
from annie_core import NormalizedEvent, RobotConfig
from annie_runtime import motions as motions_module
from annie_runtime.actions import ActionCoordinator
from annie_runtime.inner_life import InnerLifeService
from annie_runtime.motions import InteractionMotions, played_sample, target_sample

ROOT = Path(__file__).resolve().parents[1]
CATALOG = load_action_catalog(ROOT / "config/action_scripts.json")
MORNING = int(datetime(2026, 10, 26, 10, 0, tzinfo=BEIJING).timestamp() * 1000)
MINUTE = 60_000


def thread(**fields):
    return {"id": 1, "person": "张三", "chat_id": "c1", "text": "下周去面试",
            "follow_up": "问问面试结果", "status": "open", "asks": 0, "asked_ms": None,
            "created_ms": MORNING, "updated_ms": MORNING, **fields}


# ---- pure rules ----------------------------------------------------------------------
def test_an_open_thread_is_raised_rarely_and_then_dropped():
    assert thread_visible(thread(), MORNING + MINUTE)
    assert not thread_visible(thread(status="resolved"), MORNING)
    assert not thread_visible(thread(), MORNING + THREAD_TTL_MS + 1)
    asked = thread(asks=1, asked_ms=MORNING)
    assert not thread_visible(asked, MORNING + MINUTE)
    assert thread_visible(asked, MORNING + THREAD_REASK_MS)
    assert not thread_visible(thread(asks=THREAD_MAX_ASKS, asked_ms=MORNING),
                              MORNING + THREAD_TTL_MS - 1)
    hint = threads_hint([thread()], "张三")
    assert "张三之前说过" in hint and "10-26 下周去面试（可以问问面试结果）" in hint
    assert "问过就别追问" in hint and threads_hint([], "张三") == ""


def test_reflection_keeps_only_people_of_her_day_and_no_names_in_her_own_line():
    day = [{"created_ms": MORNING, "text": "张三来了", "count": 1, "person": "张三"}]
    system, user = reflection_request(day, [thread()], {"张三": "在找工作"})
    assert "只依据输入" in system and "原有印象" in user and "下周去面试" in user
    parsed = parse_reflection(json.dumps({
        "people": [{"name": "张三", "impression": "最近在准备面试，有点紧张"},
                   {"name": "李四", "impression": "编出来的人"}],
        "self": "你最近总惦记着张三的面试"}), {"张三"})
    assert parsed == {"people": {"张三": "最近在准备面试，有点紧张"}, "self": ""}
    hint = reflection_hint({"people": parsed["people"], "self": "你最近挺爱聊吃的"}, "张三")
    assert "【你对张三的印象】最近在准备面试" in hint and "【你最近的状态】你最近挺爱聊吃的" in hint
    assert "张三" not in reflection_hint({"people": parsed["people"], "self": "x"}, None)


def test_moments_and_her_wish_to_speak_never_name_a_stranger():
    noon = int(datetime(2026, 10, 26, 12, 0, tzinfo=BEIJING).timestamp() * 1000)
    assert moment_now(noon)[0] == "lunch" and moment_now(MORNING) is None
    public = thought_prompt("到饭点了", mood="心情平静；精力还行")
    assert "不提任何人的名字" in public and "不要追问" in public
    private = thought_prompt("想问问面试", mood="心情不错", person="张三")
    assert "你认出眼前是张三" in private and "不提任何人的名字" not in private


def test_a_reply_is_cut_into_finished_sentences_and_motions_need_confidence():
    assert sentences("好呀！今天怎么样？还没说完") == [(0, 3), (3, 9)]
    assert sentences("……嗯。") == [(2, 4)]  # a leading ellipsis is not a sentence
    assert sentences("…！") == []
    question = speech_motion_question({"agree_micro_nod": "同意时点头"})
    assert set(question["criteria"]) == {"none", "agree_micro_nod"}
    allowed = ["agree_micro_nod"]
    assert motion_pick({"choice": "agree_micro_nod", "confidence": .8}, allowed, threshold=.45)
    assert motion_pick({"choice": "agree_micro_nod", "confidence": .3}, allowed, threshold=.45) is None
    assert motion_pick({"choice": "none", "confidence": .9}, allowed, threshold=.45) is None
    assert motion_pick({"choice": "shake_head", "confidence": .9}, allowed, threshold=.45) is None
    assert all(script in CATALOG for script in (*LISTEN_MOTIONS, *SPEECH_MOTIONS, *INNER_MOTIONS))


def test_an_inner_thought_only_comes_from_the_runtime_never_from_a_room():
    policy = EventPolicy()
    event = NormalizedEvent(kind="inner.thought", source="inner",
                            payload={"prompt": "现场情况：你心里有句话"},
                            ts_ms=MORNING, ttl_ms=10_000)
    forged = policy.route(event, robot_id="a", transport="room:roomv2:main-event",
                          identity={}, now_ms=MORNING)
    assert (forged["decision"], forged["reason"]) == ("dropped", "untrusted_inner_thought")
    own = policy.route(NormalizedEvent(kind="inner.thought", source="inner",
                                       payload={"prompt": "现场情况：你心里有句话"},
                                       ts_ms=MORNING, ttl_ms=10_000),
                       robot_id="a", transport="inner:a", identity={}, now_ms=MORNING)
    assert own["decision"] == "need_response" and own["prompt"] == "现场情况：你心里有句话"
    assert own["listen_after"] and own["wait_for_quiet"]


# ---- motion timing ---------------------------------------------------------------------
def fake_speech(**fields):
    base = {"turn_id": "t1", "stream_epoch": 1, "cancelled": False, "receipt_tracks": {},
            "played_samples": 0, "pcm_marks": [], "samples": 0, "alignment_points": {},
            "generated_text": "", "output_active": lambda: False}
    return SimpleNamespace(**{**base, **fields})


def test_motion_timing_prefers_the_aligner_then_the_speaker_then_the_clock():
    speech = fake_speech(alignment_points={10: {"start_sample": 48000}})
    assert target_sample(speech, 10) == 48000
    assert target_sample(speech, 14) == 48000 + int(4 * 24000 / 4.3)
    assert target_sample(fake_speech(), 43) == 240000
    playing = fake_speech(receipt_tracks={"speaker": {"state": "progress"}}, played_samples=7000)
    assert played_sample(playing) == 7000
    now = time.monotonic()
    clock = fake_speech(pcm_marks=[(480, now - 1.18)], samples=96000)
    assert abs(played_sample(clock, now) - 24000) < 50
    assert played_sample(fake_speech()) is None


# ---- service: threads, reflection, thoughts ------------------------------------------
class FakeSummarizer:
    model = "fake-flash"

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    async def complete(self, system, user, *, max_tokens=240):
        self.calls.append((system, json.loads(user)))
        return self.replies.pop(0)


class Speakers:
    def __init__(self, name=None):
        self.name = name

    def public(self):
        return {"status": "matched" if self.name else "idle", "name": self.name}


class Client:
    def __init__(self, name=None):
        self.speakers = Speakers(name)
        self.events = []
        self.changed = 0
        self.ready = True
        self.speech = fake_speech(turn_id=None)
        self.speech_active = False
        self.turn_id = self.response_id = None
        self._last_conversation_activity_t = time.monotonic() - 3600
        self._asked_quiet_at = None

    def inner_changed(self):
        self.changed += 1

    def publish(self, kind, **fields):
        self.events.append({"kind": kind, **fields})

    async def ask_decision(self, state, questions):
        return {key: {"choice": "neutral", "confidence": .9} for key in questions}, "fake"


class Monitor:
    def __init__(self, count=0, names=()):
        self.view = {"count": count, "names": list(names), "gaze_name": None}

    def people_view(self):
        return self.view if self.view["count"] else None


class Events:
    def __init__(self):
        self.routed = []
        self.pending = {"a": []}

    def route(self, robot_id, event, transport):
        row = EventPolicy().route(event, robot_id=robot_id, transport=transport,
                                  identity={}, now_ms=event.ts_ms)
        row["status"] = "waiting" if row["decision"] == "need_response" else row["status"]
        self.routed.append((robot_id, event, transport, row))
        return row


def service_with(tmp_path, *, summarizer=None, name=None, view=None, now=MORNING):
    store = RuntimeStore(tmp_path / "d.db")
    store.migrate()
    clock = {"now": now}
    client = Client(name)
    runtime = SimpleNamespace(store=store, robots={"a": SimpleNamespace(chat_id="c1")},
                              realtime={"a": client},
                              room_monitors={"a": view or Monitor()}, events=Events())
    service = InnerLifeService(runtime, summarizer=summarizer, close_seconds=120,
                               clock=lambda: clock["now"])
    return service, clock, client, runtime


async def settled(service):
    for _ in range(100):
        if not service.tasks:
            return
        await asyncio.gather(*list(service.tasks), return_exceptions=True)


def summary(**fields):
    return json.dumps({"skip": False, "summary": "和张三聊了面试", "threads": [],
                       "resolved": [], "asked": [], **fields}, ensure_ascii=False)


def test_a_conversation_leaves_an_open_thread_that_only_its_person_hears_about(tmp_path):
    async def run():
        summarizer = FakeSummarizer(summary(threads=[{"text": "下周去面试", "follow_up": "问问结果"}]))
        service, clock, client, runtime = service_with(tmp_path, summarizer=summarizer, name="张三")
        await service.start()
        try:
            service.on_turn("a", chat_id="c1", user_text="我下周要去面试", reply_text="加油",
                            input_mode="audio", client=client)
            service._close_stretch("a")
            await settled(service)
            stored = rows.open_threads(service.store, "a", since_ms=0)
            assert [(row["person"], row["text"]) for row in stored] == [("张三", "下周去面试")]
            assert "【你记得的待续话题（张三之前说过" in service.hint("a")
            client.speakers.name = "李四"  # someone else in front of her
            assert "下周去面试" not in service.hint("a")
            client.speakers.name = None  # unknown voice: only this Chat's anonymous threads
            assert "下周去面试" not in service.hint("a")
        finally:
            await service.close()
    asyncio.run(run())


def test_the_next_conversation_resolves_or_counts_the_thread(tmp_path):
    async def run():
        summarizer = FakeSummarizer(
            summary(threads=[{"text": "下周去面试", "follow_up": ""}]),
            summary(asked=[1]), summary(resolved=[1]))
        service, clock, client, _rt = service_with(tmp_path, summarizer=summarizer, name="张三")
        await service.start()
        try:
            for text in ("我下周要去面试", "还没消息呢", "面试过了！"):
                service.on_turn("a", chat_id="c1", user_text=text, reply_text="嗯",
                                input_mode="audio", client=client)
                service._close_stretch("a")
                await settled(service)
                clock["now"] += 10 * MINUTE
            second = summarizer.calls[1][1]
            assert second["existing_threads"] == [{"id": 1, "text": "下周去面试"}]
            assert service.threads["a"] == []  # resolved
            assert rows.open_threads(service.store, "a", since_ms=0) == []
        finally:
            await service.close()
    asyncio.run(run())


def test_unknown_people_keep_their_threads_inside_the_chat(tmp_path):
    async def run():
        summarizer = FakeSummarizer(summary(threads=[{"text": "明天考驾照", "follow_up": ""}]))
        service, _clock, client, runtime = service_with(tmp_path, summarizer=summarizer)
        await service.start()
        try:
            service.on_turn("a", chat_id="c1", user_text="我明天考驾照", reply_text="稳住",
                            input_mode="text", client=client)
            service._close_stretch("a")
            await settled(service)
            assert service.threads["a"][0]["person"] is None
            assert "明天考驾照" in service.hint("a")
            runtime.robots["a"].chat_id = "c2"
            assert "明天考驾照" not in service.hint("a")
            service.on_chat_deleted("a", "c1")
            await settled(service)
            assert rows.open_threads(service.store, "a", since_ms=0) == []
        finally:
            await service.close()
    asyncio.run(run())


def test_greeting_a_known_face_alone_brings_up_one_thread_once(tmp_path):
    async def run():
        service, _clock, _client, runtime = service_with(tmp_path, view=Monitor(1, ["张三"]))
        await service.start()
        try:
            service.threads["a"] = [thread()]
            extra = service.greeting_extra("a", "张三")
            assert "下周去面试" in extra and "只问一句" in extra
            assert service.greeting_extra("a", "张三") == ""  # asked: waits days now
            service.threads["a"] = [thread(id=2)]
            runtime.room_monitors["a"].view["count"] = 2
            assert service.greeting_extra("a", "张三") == ""  # not in front of others
        finally:
            await service.close()
    asyncio.run(run())


def test_each_night_she_looks_back_and_forgets_on_request(tmp_path):
    async def run():
        reply = json.dumps({"people": [{"name": "张三", "impression": "最近在准备面试"}],
                            "self": "你最近总被摸头，挺开心"}, ensure_ascii=False)
        summarizer = FakeSummarizer(reply)
        service, clock, client, _rt = service_with(tmp_path, summarizer=summarizer, name="张三")
        await service.start()
        try:
            rows.add_experience(service.store, "a", "arrival", "张三来了", now_ms=MORNING,
                                keep_after_ms=0, person="张三", chat_id="c1")
            clock["now"] = MORNING + 14 * 60 * MINUTE  # next day 00:00 Beijing
            service._maybe_reflect("a", clock["now"])
            await settled(service)
            assert summarizer.calls == []  # before 03:00
            clock["now"] = MORNING + 18 * 60 * MINUTE  # next day 04:00
            service._maybe_reflect("a", clock["now"])
            await settled(service)
            service._maybe_reflect("a", clock["now"])  # once a day
            await settled(service)
            assert len(summarizer.calls) == 1
            assert summarizer.calls[0][1]["昨天的经历"][0]["事"] == "张三来了"
            hint = service.hint("a")
            assert "【你对张三的印象】最近在准备面试" in hint and "你最近总被摸头" in hint
            assert rows.latest_reflection(service.store, "a")["day"] == "2026-10-26"
            service.on_person_forgotten("a", "张三")
            await settled(service)
            assert "张三" not in service.hint("a")
            assert rows.latest_reflection(service.store, "a")["people"] == {}
        finally:
            await service.close()
    asyncio.run(run())


def test_she_speaks_her_mind_only_to_a_face_in_a_quiet_moment_and_rarely(tmp_path):
    async def run():
        service, clock, client, runtime = service_with(tmp_path, view=Monitor(1, ["张三"]))
        await service.start()
        try:
            service.threads["a"] = [thread()]
            service._consider_thoughts("a", clock["now"])
            routed = runtime.events.routed
            assert len(routed) == 1 and routed[0][2] == "inner:a"
            assert "你认出眼前是张三" in routed[0][1].payload["prompt"]
            assert "下周去面试" in routed[0][1].payload["prompt"]
            assert service.threads["a"][0]["asks"] == 1
            assert service.snapshot("a")["thought"]["status"] == "queued"
            # Five minutes between remarks, four an hour, shared with 8C.
            clock["now"] += MINUTE
            service._consider_thoughts("a", clock["now"])
            assert service.thought_state["a"]["status"] == "proactive_gap"
            for _ in range(3):
                service.note_proactive("a")
            clock["now"] += 6 * MINUTE
            assert service.proactive_block("a") == "proactive_hourly_cap"
        finally:
            await service.close()
    asyncio.run(run())


@pytest.mark.parametrize("change,reason", [
    (lambda c, m: m.view.update(count=0), "no_face_in_view"),
    (lambda c, m: setattr(c, "_last_conversation_activity_t", time.monotonic()), "conversation_recent"),
    (lambda c, m: setattr(c, "_asked_quiet_at", time.monotonic()), "asked_to_be_quiet"),
    (lambda c, m: setattr(c, "turn_id", "t9"), "conversation_busy"),
])
def test_her_wish_to_speak_waits_for_the_right_moment(tmp_path, change, reason):
    async def run():
        service, clock, client, runtime = service_with(tmp_path, view=Monitor(1, []))
        await service.start()
        try:
            noon = int(datetime(2026, 10, 26, 12, 0, tzinfo=BEIJING).timestamp() * 1000)
            clock["now"] = noon
            change(client, runtime.room_monitors["a"])
            service._consider_thoughts("a", noon)
            assert service.thought_state["a"]["status"] == reason
            assert runtime.events.routed == []
        finally:
            await service.close()
    asyncio.run(run())


def test_a_stranger_at_lunchtime_hears_no_names_and_nothing_twice(tmp_path):
    async def run():
        service, clock, _client, runtime = service_with(tmp_path, view=Monitor(2, ["张三"]))
        await service.start()
        try:
            noon = int(datetime(2026, 10, 26, 12, 0, tzinfo=BEIJING).timestamp() * 1000)
            service.threads["a"] = [thread()]  # 张三 is not alone: no thread
            service._consider_thoughts("a", noon)
            prompt = runtime.events.routed[0][1].payload["prompt"]
            assert "饭点" in prompt and "张三" not in prompt and "面试" not in prompt
            service._consider_thoughts("a", noon + 10 * MINUTE)
            assert len(runtime.events.routed) == 1  # lunch already said today
        finally:
            await service.close()
    asyncio.run(run())


# ---- actions: perform_now and the interaction motions ----------------------------------
class FakePublisher:
    def __init__(self):
        self.sent = []

    async def publish(self, packet):
        self.sent.append(packet)


def coordinator(tmp_path, allowlist=(*LISTEN_MOTIONS, *SPEECH_MOTIONS, *INNER_MOTIONS)):
    robot = SimpleNamespace(config=RobotConfig("a", room_id="roomv2"), generation=1,
                            turn_cache={}, session_state=lambda: {})
    store = RuntimeStore(tmp_path / "actions.db")
    store.migrate()
    publisher = FakePublisher()
    actions = ActionCoordinator(store, CATALOG, {"a": robot}, {"a": publisher},
                                lambda *_a, **_k: None, enabled=True,
                                allowlist=tuple(dict.fromkeys(allowlist)))
    return actions, publisher, robot


def test_perform_now_uses_the_same_gates_as_every_action(tmp_path):
    async def run():
        actions, publisher, _robot = coordinator(tmp_path, allowlist=("listen_tilt",))
        result = await actions.perform_now("a", "listen-1", "listen-1", 1, "listen_tilt")
        assert result["status"] == "published_unknown"
        data = publisher.sent[0]["data"]
        assert publisher.sent[0]["type"] == "action_trigger"
        assert (data["script_id"], data["conversation_id"], data["offset_ms"]) == (
            "listen_tilt", "listen-1", 0)
        busy = await actions.perform_now("a", "listen-2", "listen-2", 1, "listen_tilt")
        assert busy["status"] == "conflict_or_cooldown"  # the first is still moving
        with pytest.raises(ValueError):
            await actions.perform_now("a", "x", "x", 1, "shake_head")  # not allowed
        with pytest.raises(ValueError):
            await actions.perform_now("a", "y", "y", 1, "listen_tilt", user_text="别动")
        with pytest.raises(ValueError):
            await actions.perform_now("a", "z", "z", 2, "listen_tilt")  # stale generation
    asyncio.run(run())


class Wake:
    def __init__(self, state="ACTIVE"):
        self.state = state


class MotionClient:
    def __init__(self, robot, *, decision=None, wake="ACTIVE"):
        self.robot = robot
        self.wake = Wake(wake)
        self.speech_active = True
        self.speech = fake_speech()
        self.social_state = {}
        self.turn_id = None
        self.transcript = "我今天面试过了"
        self._external_text_only = False
        self.decision = decision or {}
        self.asked = []

    async def ask_decision(self, state, questions):
        self.asked.append(state)
        sentence = state.get("小安妮这句话") or state.get("小安妮的感受")
        return {"motion": self.decision.get(sentence, {"choice": "none", "confidence": .9})}, "d3"


def motions_with(tmp_path, monkeypatch, **client_fields):
    monkeypatch.setattr(motions_module, "LISTEN_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(motions_module, "POLL_SECONDS", 0.005)
    actions, publisher, robot = coordinator(tmp_path)
    client = MotionClient(robot, **client_fields)
    runtime = SimpleNamespace(actions=actions, robots={"a": robot}, realtime={"a": client})
    return InteractionMotions(runtime), client, publisher


async def drain(motions):
    for _ in range(200):
        if not motions.tasks:
            return
        await asyncio.sleep(0.01)


def test_she_looks_like_she_is_listening_only_while_awake_and_quiet(tmp_path, monkeypatch):
    async def run():
        motions, client, publisher = motions_with(tmp_path, monkeypatch)
        motions.on_speech_started("a", client)
        await drain(motions)
        assert [packet["data"]["script_id"] for packet in publisher.sent] == ["listen_tilt"]
        motions.on_speech_started("a", client)  # within the cooldown
        await drain(motions)
        assert len(publisher.sent) == 1
        for setup in (lambda: setattr(client.wake, "state", "SLEEP"),
                      lambda: setattr(client.speech, "output_active", lambda: True),
                      lambda: client.social_state.update(spontaneous_motions={
                          "value": "off", "source": "user_setting"})):
            motions.last_listen["a"] = -1e9
            client.wake.state, client.speech.output_active, client.social_state = (
                "ACTIVE", lambda: False, {})
            setup()
            motions.on_speech_started("a", client)
            await drain(motions)
            assert len(publisher.sent) == 1
        motions.last_listen["a"] = -1e9
        client.wake.state = "ACTIVE"
        client.speech_active = False  # a cough: over before the delay
        motions.on_speech_started("a", client)
        await drain(motions)
        assert len(publisher.sent) == 1
    asyncio.run(run())


def test_each_sentence_gets_its_gesture_when_its_words_are_played(tmp_path, monkeypatch):
    async def run():
        decision = {"我觉得这事能成。": {"choice": "agree_micro_nod", "confidence": .8},
                    "不过别太紧张！": {"choice": "micro_tilt", "confidence": .9}}
        motions, client, publisher = motions_with(tmp_path, monkeypatch, decision=decision)
        client.turn_id = "t1"
        speech = client.speech
        speech.generated_text = "恭喜你。我觉得这事能成。不过别太紧张！"
        speech.receipt_tracks = {"speaker": {"state": "progress"}}
        speech.played_samples = 0
        motions.on_reply_text("a", client)
        await asyncio.sleep(0.05)
        assert publisher.sent == []  # the words are not played yet
        assert len(client.asked) == 3 and client.asked[1]["小安妮这句话"] == "我觉得这事能成。"
        speech.played_samples = target_sample(speech, 4)  # “我觉得……” is about to play
        await asyncio.sleep(0.05)
        assert [p["data"]["script_id"] for p in publisher.sent] == ["agree_micro_nod"]
        assert publisher.sent[0]["data"]["conversation_id"] == "t1"
        # The nod lasts 850 ms; the next gesture waits for the speaker to move on.
        motions.runtime.actions.store.set_action_status(
            "a", "say-t1-4", "published_unknown", "device_finished")
        speech.played_samples = target_sample(speech, 12)
        await drain(motions)
        assert [p["data"]["script_id"] for p in publisher.sent] == ["agree_micro_nod",
                                                                     "micro_tilt"]
        motions.on_reply_text("a", client)  # the same sentences never ask twice
        assert len(client.asked) == 3
    asyncio.run(run())


def test_a_stopped_reply_drops_its_pending_gestures(tmp_path, monkeypatch):
    async def run():
        decision = {"第二句。": {"choice": "think_tilt", "confidence": .9}}
        motions, client, publisher = motions_with(tmp_path, monkeypatch, decision=decision)
        client.turn_id = "t1"
        client.speech.generated_text = "第一句。第二句。"
        client.speech.receipt_tracks = {"speaker": {"state": "progress"}}
        motions.on_reply_text("a", client)
        await asyncio.sleep(0.03)
        client.speech.receipt_tracks["speaker"]["state"] = "stopped"  # interrupted
        await drain(motions)
        assert publisher.sent == []
        assert motions.recent["a"][-1]["status"] == "too_late"
    asyncio.run(run())


def test_after_speaking_she_may_show_how_the_exchange_made_her_feel(tmp_path, monkeypatch):
    async def run():
        decision = {"张三关心了你": {"choice": "shy_retract", "confidence": .8}}
        motions, client, publisher = motions_with(tmp_path, monkeypatch, decision=decision)
        client.speech_active = False
        motions.on_feeling("a", "moved", "张三关心了你")
        await drain(motions)
        assert [p["data"]["script_id"] for p in publisher.sent] == ["shy_retract"]
        motions.on_feeling("a", "moved", "张三关心了你")  # at most once in 90 s
        await drain(motions)
        assert len(publisher.sent) == 1
    asyncio.run(run())


# ---- runtime: the real Realtime client drives the motions ------------------------------
def test_the_live_client_listens_and_gestures_through_the_room(tmp_path, monkeypatch):
    from annie_runtime import HarborRuntime

    from tests.test_phase5 import FakeSocket
    from tests.test_phase7_runtime import Publisher, room_server

    monkeypatch.setattr(motions_module, "LISTEN_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(motions_module, "POLL_SECONDS", 0.005)

    class Decision:
        async def ask(self, state, questions):
            sentence = state.get("小安妮这句话", "")
            pick = "agree_micro_nod" if sentence.startswith("我觉得") else "none"
            return {key: {"choice": pick, "confidence": .9} for key in questions}

        async def close(self):
            pass

    async def run():
        Publisher.instances.clear()
        rt = HarborRuntime(
            [RobotConfig("a", room_id="roomv2")], tmp_path / "runtime.db", room_server(),
            realtime_key="fake-key", realtime_socket_factory=FakeSocket,
            tts_voice="fake-voice", room_tts_factory=Publisher, room_action_factory=Publisher,
            action_catalog=ROOT / "config/action_scripts.json", actions_enabled=True,
            action_allowlist=("listen_tilt", "agree_micro_nod", "micro_tilt"),
            decision_client=Decision(),
            motion_options={"listening": True, "speaking": True, "reactions": True,
                            "per_reply": 3})
        await rt.start()
        client = rt.realtime["a"]
        for _ in range(100):
            if client.ready:
                break
            await asyncio.sleep(.01)
        main = Publisher.instances["roomv2", "main"]
        try:
            await client.handle({"type": "input_audio_buffer.speech_started", "item_id": "i1"},
                                client.robot.generation)
            for _ in range(100):
                if main.messages:
                    break
                await asyncio.sleep(.01)
            assert main.messages[0]["data"]["script_id"] == "listen_tilt"
            assert rt.motions.snapshot("a")["stats"]["listen"] == 1
            client.speech_active = False
            rt.actions.store.set_action_status(
                "a", main.messages[0]["data"]["conversation_id"], "published_unknown",
                "device_finished")
            client._create_turn("帮我看看这事行不行")
            await client.handle({"type": "response.created", "response": {"id": "r1"}},
                                client.robot.generation)
            await client.handle({"type": "response.text.delta", "response_id": "r1",
                                 "delta": "嗯。我觉得能行。"}, client.robot.generation)
            assert client.speech.generated_text == "嗯。我觉得能行。"
            # No audio has played yet: the gesture waits for its words.
            await asyncio.sleep(.05)
            assert len(main.messages) == 1
            client.speech.pcm_marks.append((24000, time.monotonic() - 2))
            client.speech.samples = 72000
            for _ in range(200):
                if len(main.messages) == 2:
                    break
                await asyncio.sleep(.01)
            assert main.messages[1]["data"]["script_id"] == "agree_micro_nod"
            assert main.messages[1]["data"]["conversation_id"] == client.turn_id
        finally:
            await rt.close()
    asyncio.run(run())


def test_diary_names_only_who_she_recognised_and_threads_are_not_repeated():
    from annie_brain.inner_life import same_thread, summary_line

    assert summary_line("和张林聊了名字和天气。", None) == "和一位没认出的人聊了名字和天气"
    assert summary_line("和一位没认出的人聊了，对方让讲故事。", "张三") == "和张三聊了对方让讲故事"
    assert summary_line("聊了天气。", None) == "聊了天气"
    assert same_thread("下周三去面试", "下周三去面试！") and same_thread("面试", "下周三去面试")
    assert not same_thread("明天考科目二", "妈妈下周做手术")
