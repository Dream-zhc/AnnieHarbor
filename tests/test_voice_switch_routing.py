"""Voice switching regression from the 19:13 '换一个音色' incident."""

import asyncio

from tests.test_phase7_runtime import ready, result_from_call, runtime
from tests.test_voice_profiles import FakeVoiceProvider, wav


def test_unnamed_voice_switch_with_one_alternative_uses_actual_choice(tmp_path):
    async def exercise():
        rt = runtime(tmp_path)
        rt.voices.provider = FakeVoiceProvider()
        await ready(rt)
        try:
            voice = (await rt.clone_voice("a", "半藏森林", "voice.wav", wav()))["voice_id"]
            client = rt.realtime["a"]
            client._create_turn("换一个音色。")
            accepted = await result_from_call(client, "vague-choice", "select_voice",
                                              {"voice_name": "半藏森林", "scope": "chat"})
            assert accepted[0]["status"] == "selected"
            assert accepted[0]["voice_changed"] is True
            assert accepted[0]["voice_name"] == "半藏森林"
            assert rt.voices.resolved(client.turn_identity) == voice
            assert client.speech.voice_provider(client.turn_identity) == voice
            assert client.speech.locked_voice is None
        finally:
            await rt.close()
    asyncio.run(exercise())


def test_unnamed_voice_switch_with_multiple_alternatives_must_ask(tmp_path):
    async def exercise():
        rt = runtime(tmp_path)
        rt.voices.provider = FakeVoiceProvider()
        await ready(rt)
        try:
            await rt.clone_voice("a", "半藏森林", "voice.wav", wav())
            await rt.clone_voice("a", "温柔女声", "voice.wav", wav())
            client = rt.realtime["a"]
            client._create_turn("换一个音色。")
            proposed = await result_from_call(client, "vague-choice", "select_voice",
                                              {"voice_name": "半藏森林", "scope": "chat"})
            assert proposed[0]["status"] == "needs_selection"
            assert proposed[0]["voice_changed"] is False
            assert "温柔女声" in proposed[0]["instruction"]
            assert "半藏森林" in proposed[0]["instruction"]
            assert rt.voices.resolved(client.turn_identity) == rt.voices.default_voice
            assert not rt.voices.selections
        finally:
            await rt.close()
    asyncio.run(exercise())


def test_default_selected_again_is_not_reported_as_a_change(tmp_path):
    async def exercise():
        rt = runtime(tmp_path)
        rt.voices.provider = FakeVoiceProvider()
        await ready(rt)
        try:
            client = rt.realtime["a"]
            await rt.clone_voice("a", "半藏森林", "voice.wav", wav())
            client._create_turn("换回默认音色。")
            reply = await result_from_call(client, "default-already", "select_voice",
                                           {"voice_name": "默认音色", "scope": "chat"})
            assert reply[0]["status"] == "already_selected"
            assert reply[0]["voice_changed"] is False
            assert rt.voices.resolved(client.turn_identity) == rt.voices.default_voice
        finally:
            await rt.close()
    asyncio.run(exercise())
