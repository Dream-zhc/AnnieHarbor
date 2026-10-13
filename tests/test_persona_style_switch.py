"""In-chat personality switch: one explicit turn, same chat, no new provider session."""

import asyncio
from pathlib import Path

import pytest
from annie_adapters.realtime import session_update
from annie_brain.persona import load_personas, resolve_persona_style_request

from tests.test_phase7_runtime import ready, result_from_call, runtime

ROOT = Path(__file__).resolve().parents[1]
PROFILES = load_personas(ROOT / "config/personas.toml")


def test_style_tool_is_absent_until_labels_are_configured():
    names = [item["function"]["name"] for item in session_update("小安妮")["session"]["tools"]]
    assert "select_persona_style" not in names
    tools = session_update("小安妮", persona_labels=("自然陪伴", "机灵互怼"))["session"]["tools"]
    tool = next(item["function"] for item in tools if item["function"]["name"] == "select_persona_style")
    assert "机灵互怼" in tool["description"]
    assert "Do not create a new chat" in tool["description"]
    assert "needs_selection" in tool["description"]


def test_named_style_matches_and_unnamed_style_only_asks():
    style, options = resolve_persona_style_request("annie", PROFILES, {}, "机灵互怼", "换成机灵互怼")
    assert style == "annie-roast" and options is None
    style, options = resolve_persona_style_request("annie", PROFILES, {}, "", "用机灵互怼跟我说话")
    assert style == "annie-roast"
    style, options = resolve_persona_style_request("annie", PROFILES, {}, "", "切换性格")
    assert style is None and "机灵互怼" in options
    style, options = resolve_persona_style_request(
        "annie", PROFILES, {}, "", "机灵互怼还是怨种搭档，换成哪个")
    assert style is None and set(options) == {"机灵互怼", "怨种搭档"}
    style, options = resolve_persona_style_request("annie", PROFILES, {}, "", "换个性格")
    assert style is None
    assert "自然陪伴" not in options
    assert {"温柔知心", "活泼俏皮", "机灵互怼", "怨种搭档", "优雅乐子人"} <= set(options)


@pytest.mark.parametrize("text", [
    "换成英语", "用四川话", "你是不是机灵互怼", "不要换成机灵互怼",
    "换个话题，谈谈性格", "用户觉得机灵互怼怎么样",
])
def test_dialect_question_and_negation_do_not_select_a_style(text):
    with pytest.raises(ValueError):
        resolve_persona_style_request("annie", PROFILES, {}, "机灵互怼", text)


def test_model_guess_cannot_override_the_named_style():
    with pytest.raises(ValueError):
        resolve_persona_style_request("annie", PROFILES, {}, "活泼俏皮", "换成机灵互怼")


def test_named_style_switch_updates_this_chat_without_reconnecting(tmp_path):
    async def exercise():
        rt = runtime(tmp_path, persona_profiles=PROFILES)
        await ready(rt)
        client = rt.realtime["a"]
        stop = asyncio.Event()

        async def echo():
            seen = 0
            while not stop.is_set():
                outbound = list(client.socket.outbound)
                for event in outbound[seen:]:
                    if event.get("type") == "session.update" and event.get("session", {}).get("instructions"):
                        await client.socket.inbound.put({
                            "type": "session.updated",
                            "session": {
                                "id": client.session,
                                "modalities": ["text"],
                                "turn_detection": {"type": "smart_turn"},
                                "instructions": event["session"]["instructions"],
                            },
                        })
                seen = len(outbound)
                await asyncio.sleep(0.01)

        echo_task = asyncio.create_task(echo())
        try:
            await asyncio.sleep(0.05)
            opening = client.socket.outbound[0]["session"]
            assert "普通话、四川话、河南话、陕西话、粤语" in opening["instructions"]
            assert "整句外语必须跟随" in opening["instructions"]
            assert "select_persona_style" in [item["function"]["name"] for item in opening["tools"]]
            socket, session, chat = client.socket, client.session, client.robot.chat_id
            generation = client.robot.generation
            client._create_turn("换成机灵互怼")
            selected = await result_from_call(
                client, "style-1", "select_persona_style", {"style_name": "机灵互怼"})
            assert selected[0]["status"] == "selected"
            assert selected[0]["style_changed"] is True
            assert selected[0]["style_label"] == "机灵互怼"
            assert "不要念固定确认稿" in selected[0]["instruction"]
            assert client.followup_needed is True
            assert client.socket is socket and client.session == session and client.ready
            assert client.robot.chat_id == chat and client.robot.generation == generation
            assert not any(event["kind"] == "session.replaced" for event in client.events)
            assert client.persona_snapshot["style_id"] == "annie-roast"
            assert client.persona_snapshot["provider_session_id"] == session
            social = await rt.social_state("a")
            assert social["persona_style"]["value"] == "annie-roast"
            assert social["persona_style"]["source"] == "user_setting"
            assert "persona_style" not in await rt.social_state("b")
            output_at = next(index for index, event in enumerate(client.socket.outbound)
                             if event.get("type") == "conversation.item.create"
                             and event["item"].get("call_id") == "style-1")
            prompted = [index for index, event in enumerate(client.socket.outbound)
                        if event.get("type") == "session.update"
                        and "抓包现场" in event.get("session", {}).get("instructions", "")]
            assert prompted and prompted[0] < output_at

            client.robot.turn_cache[client.turn_id]["user_text"] = "换个性格"
            asked = await result_from_call(
                client, "style-2", "select_persona_style", {"style_name": ""})
            assert asked[0]["status"] == "needs_selection"
            assert asked[0]["style_changed"] is False
            assert "机灵互怼" not in asked[0]["instruction"]
            assert "自然陪伴" in asked[0]["instruction"]
            assert (await rt.social_state("a"))["persona_style"]["value"] == "annie-roast"

            client.robot.turn_cache[client.turn_id]["user_text"] = "换成机灵互怼"
            same = await result_from_call(
                client, "style-3", "select_persona_style", {"style_name": "机灵互怼"})
            assert same[0]["status"] == "already_selected"
            assert same[0]["style_changed"] is False
        finally:
            stop.set()
            echo_task.cancel()
            await asyncio.gather(echo_task, return_exceptions=True)
            await rt.close()

    asyncio.run(exercise())
