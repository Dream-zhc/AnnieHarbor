"""Clone/preview/selection tests never contact DashScope or a physical Room."""

import asyncio
import io
import wave

import pytest
from annie_adapters import RuntimeStore
from annie_adapters.tts import command
from annie_runtime.voice_profiles import (
    AUTO_PREVIEWS,
    DEFAULT,
    PRESETS,
    VOICE_ID,
    VoiceProfiles,
    check_audio,
)

from tests.test_phase7_runtime import ready, result_from_call, runtime


def wav(duration=5.1):
    target = io.BytesIO()
    with wave.open(target, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * round(duration * 16000))
    return target.getvalue()


class FakeVoiceProvider:
    def __init__(self):
        self.calls = []
        self.counter = 0

    async def clone(self, filename, audio, prefix, language):
        check_audio(filename, audio)
        self.counter += 1
        return f"qwen-audio-3.0-tts-flash-{prefix}-{self.counter}"

    async def synthesize(self, voice, preset):
        self.calls.append((voice, preset[0]))
        await asyncio.sleep(.001)
        return wav(.2)


def test_invalid_audio_is_rejected_before_provider(tmp_path):
    assert len(PRESETS) == 15
    assert len(AUTO_PREVIEWS) == 15
    for filename, content in (("bad.exe", wav()), ("bad.wav", b"garbage"*200),
                              ("bad.mp3", b"garbage"*200), ("bad.m4a", b"garbage"*200),
                              ("short.wav", wav(2))):
        with pytest.raises(ValueError):
            check_audio(filename, content)
    check_audio("voice.wav", wav())


def test_realistic_dashscope_voice_identifier_accepts_model_version_dots():
    assert VOICE_ID.fullmatch("qwen-audio-3.0-tts-flash-annie-9417629004fa4493a2c172b24d3e764a")
    assert not VOICE_ID.fullmatch("../../private")
    assert not VOICE_ID.fullmatch("voice/id")


def test_preview_wire_supports_language_hints_and_full_rate_range():
    packet = command("run-task", "preview-1", voice="cloned-voice",
                     language_hint="ru", rate=.65, pitch=.75,
                     instruction="请用俄语表达。")
    options = packet["payload"]["parameters"]
    assert options["language_hints"] == ["ru"]
    assert options["rate"] == .65
    assert options["pitch"] == .75
    with pytest.raises(ValueError):
        command("run-task", "preview-1", voice="cloned-voice", language_hint="invalid")


def test_persisted_voice_library_chat_and_robot_scope_without_extra_llm(tmp_path):
    async def exercise():
        store = RuntimeStore(tmp_path / "var" / "runtime.db")
        store.migrate()
        provider = FakeVoiceProvider()
        profiles = VoiceProfiles(store, provider, "original")
        await profiles.start()
        try:
            clone = await profiles.clone("温柔女声", "voice.wav", wav())
            voice_id = clone["voice_id"]
            identity = {"robot_id": "a", "chat_id": "daily", "persona_id": "annie"}
            other = {**identity, "chat_id": "work"}
            robot_b = {**identity, "robot_id": "b"}
            assert profiles.resolved(identity) == "original"
            assert profiles.match("温柔女声") == voice_id
            assert (await profiles.select(identity, voice_id, "chat"))["status"] == "selected"
            assert profiles.resolved(identity) == voice_id
            assert profiles.resolved(other) == "original"
            await profiles.select(identity, voice_id, "robot")
            assert profiles.resolved(other) == voice_id
            assert profiles.resolved(robot_b) == "original"
            await profiles.select(identity, DEFAULT, "chat")
            assert profiles.resolved(identity) == voice_id
            preview = await profiles.preview(voice_id, "mandarin")
            assert preview.read_bytes().startswith(b"RIFF")
            assert await profiles.rename(voice_id, "声音甲")
            assert profiles.match("声音甲") == voice_id
        finally:
            await profiles.close()

        second = VoiceProfiles(store, provider, "original")
        await second.start()
        try:
            assert second.match("声音甲") == voice_id
            assert second.resolved(other) == voice_id
            assert await second.delete(voice_id) == {"status": "deleted_locally"}
            assert second.resolved(other) == "original"
        finally:
            await second.close()
    asyncio.run(exercise())


def test_realtime_voice_switch_calls_current_chat_and_keeps_tts_task_identity(tmp_path):
    async def exercise():
        rt = runtime(tmp_path)
        provider = FakeVoiceProvider()
        rt.voices.provider = provider
        await ready(rt)
        try:
            voice = (await rt.clone_voice("a", "温柔女声", "voice.wav", wav()))["voice_id"]
            client = rt.realtime["a"]
            tools = client.socket.outbound[0]["session"]["tools"]
            assert "select_voice" in {row["function"]["name"] for row in tools}
            client._create_turn("现在换成温柔女声说话")
            result = await result_from_call(client, "voice-fc", "select_voice",
                                            {"voice_name": "温柔女声", "scope": "chat"})
            assert result[0]["status"] == "selected"
            assert client.speech.voice_provider(client.turn_identity) == voice
            assert client.speech.locked_voice is None  # current synthesized PCM untouched
            invalid = await result_from_call(client, "voice-bad", "select_voice",
                                             {"voice_name": "不存在", "scope": "chat"})
            assert invalid[0]["status"] == "rejected"
            other = dict(client.turn_identity)
            other["chat_id"] = "other"
            assert rt.voices.resolved(other) == rt.voices.default_voice
        finally:
            await rt.close()
    asyncio.run(exercise())
