"""In-chat personality switch: one explicit turn, same chat, no new provider session."""

import asyncio
from pathlib import Path

import pytest
from annie_adapters.realtime import session_update
from annie_brain.persona import (
    UnclearPersonaRequest,
    load_personas,
    refuses_switch,
    resolve_persona_style_request,
)

from tests.test_decision_model import FakeDecision
from tests.test_phase7_runtime import ready, result_from_call, runtime

ROOT = Path(__file__).resolve().parents[1]
PROFILES = load_personas(ROOT / "config/personas.toml")


async def acknowledge_instructions(client, stop):
    """Stands in for the provider confirming each instructions update."""
    seen = 0
    while not stop.is_set():
        outbound = list(client.socket.outbound)
        for event in outbound[seen:]:
            if event.get("type") == "session.update" and event.get("session", {}).get("instructions"):
                await client.socket.inbound.put({"type": "session.updated", "session": {
                    "id": client.session, "modalities": ["text"],
                    "turn_detection": {"type": "smart_turn"},
                    "instructions": event["session"]["instructions"]}})
        seen = len(outbound)
        await asyncio.sleep(0.01)


def test_style_tool_is_absent_until_labels_are_configured():
    names = [item["function"]["name"] for item in session_update("小安妮")["session"]["tools"]]
    assert "select_persona_style" not in names
    tools = session_update("小安妮", persona_labels=("机灵", "毒舌"))["session"]["tools"]
    tool = next(item["function"] for item in tools if item["function"]["name"] == "select_persona_style")
    assert "毒舌" in tool["description"]
    assert "Do not create a new chat" in tool["description"]
    assert "needs_selection" in tool["description"]


def test_named_style_matches_and_unnamed_style_only_asks():
    style, options = resolve_persona_style_request("annie", PROFILES, {}, "毒舌", "换成毒舌")
    assert style == "annie-savage" and options is None
    style, options = resolve_persona_style_request("annie", PROFILES, {}, "", "用毒舌跟我说话")
    assert style == "annie-savage"
    style, options = resolve_persona_style_request("annie", PROFILES, {}, "", "切换性格")
    assert style is None and "毒舌" in options
    style, options = resolve_persona_style_request(
        "annie", PROFILES, {}, "", "毒舌还是高冷，换成哪个")
    assert style is None and set(options) == {"毒舌", "高冷"}
    style, options = resolve_persona_style_request("annie", PROFILES, {}, "", "换个性格")
    assert style is None
    assert "机灵" not in options
    assert {"温柔", "毒舌", "高冷"} <= set(options)


@pytest.mark.parametrize("text", [
    "换成英语", "用四川话", "你是不是毒舌", "不要换成毒舌",
    "换个话题，谈谈性格", "用户觉得毒舌怎么样",
])
def test_dialect_question_and_negation_do_not_select_a_style(text):
    with pytest.raises(ValueError):
        resolve_persona_style_request("annie", PROFILES, {}, "毒舌", text)


def test_model_guess_cannot_override_the_named_style():
    with pytest.raises(ValueError):
        resolve_persona_style_request("annie", PROFILES, {}, "温柔", "换成毒舌")


@pytest.mark.parametrize(("text", "expected"), [
    ("切换温柔性格", "annie-calm"), ("切到温柔", "annie-calm"), ("换回机灵吧", "annie"),
    ("来个毒舌的", "annie-savage"), ("你太毒舌了，换个温柔的", "annie-calm"),
    ("把毒舌换成高冷", "annie-deadpan"), ("试试高冷性格", "annie-deadpan"),
    ("不要这么毒舌了，换个温柔的", "annie-calm"),
])
def test_a_switch_verb_selects_the_style_it_governs(text, expected):
    style, options = resolve_persona_style_request("annie-savage", PROFILES, {}, "", text)
    assert style == expected and options is None
    style, _ = resolve_persona_style_request(
        "annie-savage", PROFILES, {}, PROFILES[expected]["label"] + "性格", text)
    assert style == expected  # the model may append "性格" to the label


@pytest.mark.parametrize("text", ["性格换一下", "换回原来的性格", "风格切换一下吧"])
def test_unnamed_switch_phrasings_still_ask(text):
    style, options = resolve_persona_style_request("annie", PROFILES, {}, "", text)
    assert style is None and "毒舌" in options


def test_only_bare_mentions_are_left_to_the_decision_model():
    with pytest.raises(UnclearPersonaRequest):
        resolve_persona_style_request("annie", PROFILES, {}, "毒舌", "你是不是毒舌")
    for text in ("不要换成毒舌", "性格不用改", "别切了，毒舌挺好", "别再用毒舌了"):
        with pytest.raises(ValueError) as refused:
            resolve_persona_style_request("annie", PROFILES, {}, "毒舌", text)
        assert not isinstance(refused.value, UnclearPersonaRequest), text


def test_only_turning_down_the_switch_or_the_named_option_refuses_it():
    for text in ("别换声音，这个挺好", "不要换了", "不用再换", "我不想换", "算了不换了", "不切换",
                 "别用毒舌", "不要那个毒舌"):
        assert refuses_switch(text, "毒舌"), text
    for text in ("不要这么凶了，换个温柔的", "不要这个声音了，换一个", "要不要换个温柔的",
                 "别用这个声音了，换成毒舌", "特别想换个温柔的"):
        assert not refuses_switch(text, "毒舌"), text


def test_named_style_switch_updates_this_chat_without_reconnecting(tmp_path):
    async def exercise():
        rt = runtime(tmp_path, persona_profiles=PROFILES)
        await ready(rt)
        client = rt.realtime["a"]
        stop = asyncio.Event()
        echo_task = asyncio.create_task(acknowledge_instructions(client, stop))
        try:
            await asyncio.sleep(0.05)
            opening = client.socket.outbound[0]["session"]
            assert "支持的中文方言：普通话、广东话（粤语）" in opening["instructions"]
            assert "整句外语必须跟随" in opening["instructions"]
            assert "select_persona_style" in [item["function"]["name"] for item in opening["tools"]]
            socket, session, chat = client.socket, client.session, client.robot.chat_id
            generation = client.robot.generation
            client._create_turn("换成毒舌")
            selected = await result_from_call(
                client, "style-1", "select_persona_style", {"style_name": "毒舌"})
            assert selected[0]["status"] == "selected"
            assert selected[0]["style_changed"] is True
            assert selected[0]["style_label"] == "毒舌"
            assert "不要念固定确认稿" in selected[0]["instruction"]
            assert client.followup_needed is True
            assert client.socket is socket and client.session == session and client.ready
            assert client.robot.chat_id == chat and client.robot.generation == generation
            assert not any(event["kind"] == "session.replaced" for event in client.events)
            assert client.persona_snapshot["style_id"] == "annie-savage"
            assert client.persona_snapshot["provider_session_id"] == session
            social = await rt.social_state("a")
            assert social["persona_style"]["value"] == "annie-savage"
            assert social["persona_style"]["source"] == "user_setting"
            assert "persona_style" not in await rt.social_state("b")
            output_at = next(index for index, event in enumerate(client.socket.outbound)
                             if event.get("type") == "conversation.item.create"
                             and event["item"].get("call_id") == "style-1")
            prompted = [index for index, event in enumerate(client.socket.outbound)
                        if event.get("type") == "session.update"
                        and "嘴比刀快" in event.get("session", {}).get("instructions", "")]
            assert prompted and prompted[0] < output_at

            client.robot.turn_cache[client.turn_id]["user_text"] = "换个性格"
            asked = await result_from_call(
                client, "style-2", "select_persona_style", {"style_name": ""})
            assert asked[0]["status"] == "needs_selection"
            assert asked[0]["style_changed"] is False
            assert "毒舌" not in asked[0]["instruction"]
            assert "机灵" in asked[0]["instruction"]
            assert (await rt.social_state("a"))["persona_style"]["value"] == "annie-savage"

            client.robot.turn_cache[client.turn_id]["user_text"] = "换成毒舌"
            same = await result_from_call(
                client, "style-3", "select_persona_style", {"style_name": "毒舌"})
            assert same[0]["status"] == "already_selected"
            assert same[0]["style_changed"] is False
        finally:
            stop.set()
            echo_task.cancel()
            await asyncio.gather(echo_task, return_exceptions=True)
            await rt.close()

    asyncio.run(exercise())


def test_user_can_hand_the_choice_back_after_being_asked():
    offered = ["annie-calm", "annie-savage", "annie-deadpan"]
    for reply in ("你选一个。", "随便，你定吧", "那你换吧。", "好", "就它吧"):
        style, options = resolve_persona_style_request(
            "annie", PROFILES, {}, "毒舌", reply, offered=offered)
        assert style == "annie-savage" and options is None, reply
    style, _ = resolve_persona_style_request(
        "annie", PROFILES, {}, "温柔", "那就温柔吧", offered=offered)
    assert style == "annie-calm"


@pytest.mark.parametrize(("reply", "requested", "offered"), [
    ("你选一个。", "毒舌", None),                       # nobody asked: no delegation
    ("你选一个。", "高冷", ["annie-calm", "annie-savage"]),  # not among the offer
    ("别换了，你选的不行", "毒舌", ["annie-savage"]),        # negated
    ("那就温柔吧", "毒舌", ["annie-calm", "annie-savage"]),  # disagrees with user
])
def test_delegation_never_overrides_the_offer_or_a_refusal(reply, requested, offered):
    with pytest.raises(ValueError):
        resolve_persona_style_request("annie", PROFILES, {}, requested, reply, offered=offered)


def test_switch_after_asking_then_delegating_in_a_live_chat(tmp_path):
    """Regression: 换一个性格 → 你选一个 → 那你换吧 used to be rejected every time."""
    async def exercise():
        rt = runtime(tmp_path, persona_profiles=PROFILES)
        await ready(rt)
        client = rt.realtime["a"]
        stop = asyncio.Event()
        echo_task = asyncio.create_task(acknowledge_instructions(client, stop))
        try:
            client._create_turn("换一个性格")
            asked = await result_from_call(client, "ask", "select_persona_style", {"style_name": ""})
            assert asked[0]["status"] == "needs_selection"
            assert "让你来选" in asked[0]["instruction"]
            client._finish("completed")
            client._create_turn("你选一个。")
            picked = await result_from_call(client, "pick", "select_persona_style",
                                            {"style_name": "毒舌"})
            assert picked[0]["status"] == "selected"
            assert client.persona_snapshot["style_id"] == "annie-savage"
            assert "annie-savage" not in client._persona_offer["options"]  # follow-ups may delegate again
            client._finish("completed")
            client._create_turn("那你换吧。")
            same = await result_from_call(client, "same", "select_persona_style",
                                          {"style_name": "毒舌"})
            assert same[0]["status"] == "already_selected"  # never a refusal for a no-op
        finally:
            stop.set()
            await echo_task
            await rt.close()
    asyncio.run(exercise())


def test_unnamed_switch_accepts_the_models_pick_of_another_style():
    style, options = resolve_persona_style_request("annie-deadpan", PROFILES, {}, "温柔", "换一个性格")
    assert style == "annie-calm" and options is None
    style, options = resolve_persona_style_request("annie-deadpan", PROFILES, {}, "高冷", "换一个性格")
    assert style is None and "高冷" not in options  # picking the current one still asks


def test_a_bare_mention_switches_only_when_the_decision_model_says_so(tmp_path):
    async def exercise():
        rt = runtime(tmp_path, persona_profiles=PROFILES)
        await ready(rt)
        client = rt.realtime["a"]
        stop = asyncio.Event()
        echo_task = asyncio.create_task(acknowledge_instructions(client, stop))
        try:
            client.decision = FakeDecision({"choice": "yes", "confidence": .9})
            client._create_turn("毒舌模式走起")
            picked = await result_from_call(client, "confirmed", "select_persona_style",
                                            {"style_name": "毒舌"})
            assert picked[0]["status"] == "selected"
            assert client.persona_snapshot["style_id"] == "annie-savage"
            assert "开场手法：刚换过来的第一句：直接开骂" in picked[0]["instruction"]
            assert len(client.decision.calls) == 1
            client._finish("completed")
            client._persona_offer = None  # past the window where naming another style answers her
            client.decision = FakeDecision({"choice": "no", "confidence": .8})
            client._create_turn("你是不是温柔不起来")
            refused = await result_from_call(client, "unsure", "select_persona_style",
                                             {"style_name": "温柔"})
            assert refused[0]["status"] == "rejected"
            assert client.persona_snapshot["style_id"] == "annie-savage"
            assert "annie-calm" in client._persona_offer["options"]  # so the answer to her question counts
        finally:
            stop.set()
            await echo_task
            await rt.close()
    asyncio.run(exercise())
