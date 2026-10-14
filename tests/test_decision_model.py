"""Decision model: addressee gate, automatic expressions and post-conversation listening."""

import asyncio
import json
import time

import httpx
import pytest
from annie_adapters.decision import DecisionClient
from annie_adapters.realtime import session_update
from annie_brain.decision_policy import (
    addressee_state,
    addressee_verdict,
    explicit_expression,
    explicit_motion,
    expression_choice,
    expression_question,
    pick_anchor,
)
from annie_core import RobotConfig
from annie_runtime import HarborRuntime
from annie_runtime.ambient import HANGOVER_FRAMES, AmbientListener
from annie_runtime.wakeword import WakeAdmission

URL = "https://ws.example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/systemone"


class FakeDecision:
    def __init__(self, *answers, fail=False):
        self.answers = list(answers)
        self.fail = fail
        self.calls = []

    async def ask(self, state, questions):
        self.calls.append((state, questions))
        if self.fail:
            raise httpx.ConnectError("down")
        return {key: self.answers.pop(0) for key in questions}

    async def close(self):
        pass


class EchoSocket:
    def __init__(self, _url, _key):
        self.outbound = []
        self.inbound = asyncio.Queue()

    async def open(self):
        return self

    async def send(self, packet):
        self.outbound.append(packet)
        if packet["type"] == "session.update":
            await self.inbound.put({"type": "session.updated",
                                    "session": {"id": "decision-test", "modalities": ["text"],
                                                "turn_detection": {"type": "smart_turn"},
                                                **packet["session"]}})

    async def recv(self):
        return await self.inbound.get()

    async def close(self):
        pass


# ---- adapter and policy ---------------------------------------------------------------
def test_client_posts_typed_questions_and_requires_every_answer():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content), auth=request.headers["Authorization"])
        return httpx.Response(200, json={"answers": {"to": {"type": "choice", "choice": "robot",
                                                            "confidence": .9}}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                     headers={"Authorization": "Bearer k"}) as http:
            client = DecisionClient(URL, "k", client=http)
            answers = await client.ask({"当前这句话": "你好"},
                                       {"to": {"type": "choice", "criteria": {"robot": "r"}}})
            assert answers["to"]["choice"] == "robot"
            with pytest.raises(ValueError):
                await client.ask({}, {"a": {"type": "choice"}, "b": {"type": "noul"}})
            with pytest.raises(ValueError):
                await client.ask({}, {"a": {"type": "free_text"}})
    asyncio.run(run())
    assert seen["model"] == "decision-model-preview" and seen["auth"] == "Bearer k"
    for bad in ("http://x/compatible-mode/v1/systemone", "https://x/v1/chat",
                URL + "?key=1"):
        with pytest.raises(ValueError):
            DecisionClient(bad, "k")


def test_verdicts_need_confidence_and_concrete_choices():
    assert addressee_verdict({"choice": "robot", "confidence": .8}) == "robot"
    assert addressee_verdict({"choice": "person", "confidence": .9}) == "not_robot"
    assert addressee_verdict({"choice": "self_or_media", "confidence": .7}) == "not_robot"
    assert addressee_verdict({"choice": "robot", "confidence": .18}) == "robot"  # live-calibrated
    assert addressee_verdict({"choice": "unclear", "confidence": .99}) == "unclear"
    assert addressee_verdict(None) == "unclear"
    assert expression_choice({"choice": "surprise", "confidence": .7}) == "surprise"
    assert expression_choice({"choice": "none", "confidence": .99}) is None
    assert expression_choice({"choice": "surprise", "confidence": .3}) is None
    assert set(expression_question(["shy"])["criteria"]) == {"none", "shy"}
    assert pick_anchor("五百万！恭喜你。") == ("五百", 1) and pick_anchor("……") == (None, None)
    assert pick_anchor("哈哈，你好。哈哈哈", start=6) == ("哈哈", 2)  # lands on the later copy
    assert explicit_expression("做个害羞的表情") == "shy"
    assert explicit_expression("我很惊讶") is None and explicit_expression("别做惊讶的表情") is None
    allowed = ("agree_micro_nod", "look_left", "fast_double_blink", "shy")
    assert explicit_motion("点个头", allowed) == "agree_micro_nod"
    assert explicit_motion("往左边看看", allowed) == "look_left"
    assert explicit_motion("眨两下眼睛", allowed) == "fast_double_blink"
    assert explicit_motion("做个害羞的表情", allowed) == "shy"
    assert explicit_motion("别点头了", allowed) is None
    assert explicit_motion("摇个头", allowed) is None  # not enabled
    state = addressee_state(utterance="x" * 900, recent_lines=[str(i) for i in range(20)],
                            seconds_since_reply=3.7, robot_asked=True)
    assert len(state["当前这句话"]) == 300 and len(state["最近对话"]) == 6
    assert state["距机器人上一句"] == "3秒，上一句是在提问"


# ---- runtime: addressee gate --------------------------------------------------------
def runtime_with(tmp_path, decision, **kwargs):
    return HarborRuntime([RobotConfig("a", room_id="roomv2")], tmp_path / "d.db",
                         realtime_key="fake", realtime_socket_factory=EchoSocket,
                         decision_client=decision, addressee_enabled=True, **kwargs)


async def ready(rt):
    await rt.start()
    for _ in range(150):
        if rt.realtime["a"].ready:
            return rt.realtime["a"]
        await asyncio.sleep(.01)
    raise AssertionError("not ready")


async def utter(client, item, text, reply="好的。"):
    for event in (
        {"type": "input_audio_buffer.speech_started", "item_id": item},
        {"type": "input_audio_buffer.speech_stopped", "item_id": item},
        {"type": "response.created", "response": {"id": "r-" + item}},
        {"type": "response.output_item.added", "response_id": "r-" + item,
         "item": {"id": "m-" + item, "type": "message", "role": "assistant"}},
        {"type": "response.text.delta", "response_id": "r-" + item, "delta": reply},
        {"type": "response.done", "response": {"id": "r-" + item, "status": "completed",
                                               "output": [{"id": "m-" + item, "type": "message",
                                                           "role": "assistant"}]}},
        {"type": "conversation.item.input_audio_transcription.completed",
         "item_id": item, "transcript": text},
    ):
        await client.socket.inbound.put(event)
    for _ in range(200):
        if any(e["kind"] in {"turn.done", "transcript.ambient"} for e in client.events
               if e.get("item_id") in (None, item)) and not client._decision_tasks:
            break
        await asyncio.sleep(.01)
    await asyncio.sleep(.05)


def test_reply_to_the_robot_is_released_after_the_verdict(tmp_path):
    decision = FakeDecision({"choice": "robot", "confidence": .93})

    async def run():
        rt = runtime_with(tmp_path, decision)
        client = await ready(rt)
        try:
            await utter(client, "u1", "帮我想个周末去哪玩")
            done = [e for e in client.events if e["kind"] == "turn.done"]
            assert done and done[-1]["text"] == "好的。"
            assert rt.robots["a"].turn_cache[done[-1]["turn_id"]]["user_text"] == "帮我想个周末去哪玩"
            assert decision.calls[0][0]["当前这句话"] == "帮我想个周末去哪玩"
            assert any(e["kind"] == "addressee.decided" and e["verdict"] == "robot"
                       for e in client.events)
        finally:
            await rt.close()
    asyncio.run(run())


def test_talk_to_someone_else_is_not_answered_but_kept_as_context(tmp_path):
    decision = FakeDecision({"choice": "person", "confidence": .95})

    async def run():
        rt = runtime_with(tmp_path, decision)
        client = await ready(rt)
        try:
            await utter(client, "u2", "妈，晚饭吃什么？", reply="晚饭可以吃面。")
            assert not [e for e in client.events if e["kind"] == "turn.done"]
            assert not rt.robots["a"].turn_cache
            deleted = {e["item_id"] for e in client.socket.outbound
                       if e["type"] == "conversation.item.delete"}
            assert {"u2", "m-u2"} <= deleted  # neither the question nor the held reply stay
            ambient = [e for e in client.events if e["kind"] == "transcript.ambient"]
            assert ambient[-1]["reason"] == "not_addressed"
            assert client._overheard_recent(60) == ["妈，晚饭吃什么？"]
            assert client.decision_stats["declined"] == 1
        finally:
            await rt.close()
    asyncio.run(run())


def test_side_talk_is_fetched_on_demand_never_put_in_the_instructions(tmp_path):
    from tests.test_phase7_runtime import result_from_call

    decision = FakeDecision({"choice": "person", "confidence": .95})

    async def run():
        rt = runtime_with(tmp_path, decision)
        client = await ready(rt)
        try:
            tools = [t["function"]["name"] for t in client.socket.outbound[0]["session"]["tools"]]
            assert "recall_overheard" in tools
            await utter(client, "u2", "我们这周末去爬山吧？")
            sent = [e["session"].get("instructions", "") for e in client.socket.outbound
                    if e["type"] == "session.update"]
            # Live: lines in the instructions made smart_turn drop later questions.
            assert not any("爬山" in text for text in sent)
            client._create_turn("小安妮，我们刚才说周末要去干嘛？")
            result = (await result_from_call(client, "c1", "recall_overheard", {}))[0]
            assert result == {"status": "ok", "lines": ["我们这周末去爬山吧？"]}
            client._forget_overheard("sleep_command")
            result = (await result_from_call(client, "c2", "recall_overheard", {}))[0]
            assert result == {"status": "empty", "lines": []}
        finally:
            await rt.close()
    asyncio.run(run())


def test_calling_her_name_skips_the_check_and_an_outage_fails_open(tmp_path):
    async def run():
        named = FakeDecision()
        rt = runtime_with(tmp_path, named)
        client = await ready(rt)
        try:
            await utter(client, "u3", "小安妮，今天几号？")
            assert named.calls == []
            assert [e for e in client.events if e["kind"] == "turn.done"]
        finally:
            await rt.close()
        down = FakeDecision(fail=True)
        rt = runtime_with(tmp_path, down)
        client = await ready(rt)
        try:
            await utter(client, "u4", "讲个笑话")
            assert [e for e in client.events if e["kind"] == "turn.done"]
            assert client.decision_stats["failures"] == 1
        finally:
            await rt.close()
    asyncio.run(run())


def test_followup_window_survives_a_side_conversation(tmp_path):
    decision = FakeDecision({"choice": "person", "confidence": .9})

    async def run():
        rt = runtime_with(tmp_path, decision)
        client = await ready(rt)
        try:
            class Detector:
                async def detected(self, _):
                    return False
            client.wake = WakeAdmission(Detector())
            presence = client.wake.presence
            presence.state = "FOLLOWUP"
            presence.deadline = presence._window_deadline = time.monotonic() + 8
            await utter(client, "u5", "老王你把遥控器递给我")
            assert presence.state == "FOLLOWUP"  # the window stays open for the user
            assert not [e for e in client.events if e["kind"] == "turn.done"]
            assert decision.calls
        finally:
            await rt.close()
    asyncio.run(run())


# ---- automatic expressions --------------------------------------------------------
def test_expression_is_chosen_by_the_decision_model_not_the_llm(tmp_path):
    from tests.test_phase7_runtime import ready as ready7
    from tests.test_phase7_runtime import runtime

    async def run():
        rt = runtime(tmp_path)
        await ready7(rt)
        client = rt.realtime["a"]
        client.decision = FakeDecision({"choice": "surprise", "confidence": .9},
                                       {"choice": "surprise", "confidence": .9})
        client.auto_expression_enabled = True
        try:
            client._create_turn("我中了五百万彩票")
            await client.handle({"type": "response.created", "response": {"id": "r1"}},
                                client.robot.generation)
            await client.handle({"type": "response.text.delta", "response_id": "r1",
                                 "delta": "五百万！恭喜你。"}, client.robot.generation)
            for _ in range(100):
                if any(e["kind"] == "action.auto" for e in client.events):
                    break
                await asyncio.sleep(.01)
            auto = next(e for e in client.events if e["kind"] == "action.auto")
            assert auto["script"] == "surprise" and auto["anchor"] in "五百万！恭喜你。"
            state = client.decision.calls[0][0]
            assert state["用户"] == "我中了五百万彩票" and state["机器人回复开头"] == "五百万！"
            # cooldown: the next turn right away does not ask again
            client._finish("completed")
            client._create_turn("哈哈")
            await client.handle({"type": "response.text.delta", "response_id": None,
                                 "delta": "确实好笑。"}, client.robot.generation)
            await asyncio.sleep(.05)
            assert len(client.decision.calls) == 1
        finally:
            await rt.close()
    asyncio.run(run())
    tool = next(t["function"] for t in session_update(
        "x", actions=("surprise",), spontaneous_expressions=False)["session"]["tools"]
        if t["function"]["name"] == "perform_action")
    assert "never add one yourself" in tool["description"]


# ---- post-conversation listening ------------------------------------------------------
class FakeSidecar:
    def __init__(self):
        self.offers, self.invalidated = [], []

    def offer(self, pcm, **kwargs):
        self.offers.append(pcm)
        return True

    def invalidate(self, reason):
        self.invalidated.append(reason)

    def snapshot(self):
        return {}


def test_ambient_listening_is_windowed_energy_gated_and_memory_only():
    now = [100.0]
    lines = []
    sidecar = FakeSidecar()
    listener = AmbientListener(sidecar, lines.append, clock=lambda: now[0])
    loud = (b"\x00\x40" * 320)  # about -6 dBFS
    quiet = bytes(640)
    kwargs = {"identity": {}, "stream_epoch": 1, "source_id": "s", "received_at": 0}
    assert listener.offer(loud, **kwargs) is False  # no window yet: nothing leaves
    listener.open_window()
    assert listener.offer(loud, **kwargs) is True
    for _ in range(HANGOVER_FRAMES):  # the ASR VAD needs trailing silence to finish
        assert listener.offer(quiet, **kwargs) is True
    assert listener.offer(quiet, **kwargs) is False  # silence is not uploaded
    assert HANGOVER_FRAMES * 20 > 800  # more than the ASR server VAD silence_duration_ms
    listener.observe({"kind": "partial", "text": "我们"})
    listener.observe({"kind": "completed", "text": "我们周末去爬山吧"})
    assert lines == ["我们周末去爬山吧"]
    now[0] += 181
    assert listener.offer(loud, **kwargs) is False and "window_closed" in sidecar.invalidated


def test_llm_no_longer_calls_motions_when_the_runtime_performs_them(tmp_path):
    from tests.test_phase7_runtime import ready as ready7
    from tests.test_phase7_runtime import runtime

    async def run():
        rt = runtime(tmp_path)
        for client in rt.realtime.values():
            client.decision = FakeDecision()
            client.auto_expression_enabled = True
        await ready7(rt)
        try:
            update = rt.realtime["a"].socket.outbound[0]["session"]
            names = [t["function"]["name"] for t in update["tools"]]
            assert "perform_action" not in names  # no tool-first round trip for motions
            assert "preview_action" not in names
            assert "set_expression" not in names
            assert "动作与表情由系统根据对话自动执行" in update["instructions"]
        finally:
            await rt.close()
    asyncio.run(run())


def test_requested_motion_starts_with_the_first_words_even_in_a_short_reply(tmp_path):
    from tests.test_phase7_runtime import ready as ready7
    from tests.test_phase7_runtime import runtime

    async def run():
        rt = runtime(tmp_path)
        await ready7(rt)
        client = rt.realtime["a"]
        client.decision = FakeDecision()
        client.auto_expression_enabled = True
        try:
            client._create_turn("点个头")
            await client.handle({"type": "response.created", "response": {"id": "r1"}},
                                client.robot.generation)
            await client.handle({"type": "response.text.delta", "response_id": "r1",
                                 "delta": "好嘞，点头～"}, client.robot.generation)
            for _ in range(100):
                if any(e["kind"] == "action.auto" for e in client.events):
                    break
                await asyncio.sleep(.01)
            auto = next(e for e in client.events if e["kind"] == "action.auto")
            # an ordinary requested motion starts with the first audio, no word anchor
            assert auto["script"] == "agree_micro_nod" and auto["anchor"] is None
            assert auto["status"] == "awaiting_audio_anchor"
            assert client.decision.calls == []  # a named action needs no model call
        finally:
            await rt.close()
    asyncio.run(run())
