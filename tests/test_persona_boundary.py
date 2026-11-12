"""11-04: once the personality is switched, each reply shows which one said it, what an
old one said is history rather than a voice to keep, and her first line after a switch
in the workbench shows off the new personality or voice instead of announcing it."""

import asyncio
from pathlib import Path

from annie_brain.context import select_restorable_context
from annie_brain.persona import load_personas
from annie_runtime import realtime

from tests.test_phase7_runtime import ready, runtime

ROOT = Path(__file__).resolve().parents[1]
PROFILES = load_personas(ROOT / "config/personas.toml")


def styled(client, style_id):
    client.social_state["persona_style"] = {"value": style_id, "source": "user_setting",
                                           "generation": client.robot.generation}


def test_each_turn_keeps_the_personality_it_was_said_in(tmp_path):
    async def exercise():
        rt = runtime(tmp_path, persona_profiles=PROFILES)
        await ready(rt)
        try:
            client = rt.realtime["a"]
            turns = []
            for style, text in (("annie-calm", "今天好累"), ("annie-savage", "你变了")):
                styled(client, style)
                client._create_turn(text)
                turns.append(client.turn_id)
                client.text = "嗯。"
                client._finish("completed")
            await rt.ledger.flush()
            assert [rt.store.get_conversation_turn("a", turn)["persona_style"] for turn in turns] == [
                "annie-calm", "annie-savage"]
            assert client.robot.turn_cache[turns[1]]["persona_style"] == "annie-savage"
            packet = select_restorable_context(list(client.robot.context_cache), speech_enabled=False)
            assert [turn["persona_style"] for turn in packet["turns"]] == ["annie-calm", "annie-savage"]
        finally:
            await rt.close()

    asyncio.run(exercise())


def test_replies_from_another_personality_are_restored_as_content_only(tmp_path):
    async def exercise():
        rt = runtime(tmp_path, persona_profiles=PROFILES)
        await ready(rt)
        try:
            client = rt.realtime["a"]
            styled(client, "annie-savage")
            sent = len(client.socket.outbound)
            await client._restore_context({"chars_used": 20, "turns": [
                {"turn_id": "t1", "user_text": "今天好累", "assistant_text": "抱抱你，辛苦啦。",
                 "persona_style": "annie-calm", "created_ms": None},
                {"turn_id": "t2", "user_text": "你变了", "assistant_text": "少废话。",
                 "persona_style": "annie-savage", "created_ms": None}]})
            said = [item["item"]["content"][0]["text"] for item in client.socket.outbound[sent:]
                    if item["type"] == "conversation.item.create" and item["item"]["role"] == "user"]
            assert said == ["【这轮你还是「温柔」性格，回复只参考内容，别学那个口吻】今天好累", "你变了"]
        finally:
            await rt.close()

    asyncio.run(exercise())


def test_a_switch_in_the_workbench_opens_with_the_new_personality(tmp_path):
    async def exercise():
        rt = runtime(tmp_path, persona_profiles=PROFILES)
        await ready(rt)
        try:
            client = rt.realtime["a"]
            await rt.update_social_state("a", {"persona_style": "annie-savage"}, source="user_setting",
                                         generation=client.robot.generation)
            for _ in range(100):
                if client.ready:
                    break
                await asyncio.sleep(.01)
            note = client._once_notes["switch"][0]
            assert "你刚从「机灵」换成「毒舌」" in note and "别说“已切换”" in note
            assert note in client._combined_instructions(client._fresh_visual())
            # Spent once she has said that first line.
            client._create_turn("在吗")
            client.text = "在呢，憋了一肚子槽等你。"
            client._finish("completed")
            assert "switch" not in client._once_notes
            client.note_switch(voice="御姐音")
            assert "你刚换成「御姐音」的声音" in client._once_notes["switch"][0]
        finally:
            await rt.close()

    asyncio.run(exercise())


def test_a_switch_in_the_workbench_is_heard_at_once_unless_someone_is_talking(tmp_path, monkeypatch):
    monkeypatch.setattr(realtime, "SWITCH_OPENER_SETTLE_SECONDS", .05)

    async def exercise():
        rt = runtime(tmp_path, persona_profiles=PROFILES)
        await ready(rt)
        client = rt.realtime["a"]
        said, outcomes, publish = [], [], client.publish

        async def submit_text(text, *, source="user", **_):
            said.append(source)
            return {"turn_id": f"opener-{len(said)}"}

        def record(kind, **fields):
            if kind == "switch.opener":
                outcomes.append((fields["status"], fields["reason"]))
            return publish(kind, **fields)

        client.submit_text, client.publish = submit_text, record
        try:
            await rt.update_social_state("a", {"persona_style": "annie-savage"}, source="user_setting",
                                         generation=client.robot.generation)
            for _ in range(300):
                if said:
                    break
                await asyncio.sleep(.01)
            assert said == ["external"]
            # Clicking through voices opens once, for the last one.
            for voice in ("御姐音", "萝莉音"):
                client.note_switch(voice=voice)
                client.open_after_switch()
            await asyncio.sleep(.3)
            assert said == ["external"] * 2
            # Someone talking: the note waits for her next reply instead.
            client.speech_active = True
            client.note_switch(voice="邻家姐姐")
            client.open_after_switch()
            await asyncio.sleep(.3)
            assert len(said) == 2 and "邻家姐姐" in client._once_notes["switch"][0]
            assert outcomes == [("spoken", None), ("spoken", None), ("skipped", "busy")]
        finally:
            client.speech_active = False
            await rt.close()

    asyncio.run(exercise())
