"""Clone/preview/selection tests never contact DashScope or a physical Room."""

import asyncio
import io
import struct
import wave

import httpx
import numpy as np
import pytest
from annie_adapters import RuntimeStore
from annie_adapters.tts import command
from annie_runtime.voice_profiles import (
    AUTO_PREVIEWS,
    DEFAULT,
    PRESETS,
    SYSTEM,
    VOICE_ID,
    DashscopeVoiceProvider,
    VoiceCloneError,
    VoiceProfiles,
    check_audio,
    prepare_audio,
)

from tests.test_decision_model import FakeDecision
from tests.test_phase7_runtime import ready, result_from_call, runtime


def wav(duration=5.1, rate=16000):
    target = io.BytesIO()
    with wave.open(target, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(b"\0\0" * round(duration * rate))
    return target.getvalue()


def extensible_wav(left, right, *, rate=44100, width=3, tag=1):
    """Stereo WAVE_FORMAT_EXTENSIBLE as editors export it; Python's wave cannot read it."""
    samples = np.stack([left, right], axis=1).reshape(-1)
    if tag == 3:
        data = samples.astype("<f4" if width == 4 else "<f8").tobytes()
    else:
        data = samples.astype("<i4").view(np.uint8).reshape(-1, 4)[:, :width].tobytes()
    fmt = (struct.pack("<HHIIHHHHIH", 0xFFFE, 2, rate, rate * 2 * width, 2 * width,
                       8 * width, 22, 8 * width, 3, tag)
           + bytes.fromhex("000000001000800000aa00389b71"))
    return (b"RIFF" + struct.pack("<I", 20 + len(fmt) + len(data)) + b"WAVE"
            + b"fmt " + struct.pack("<I", len(fmt)) + fmt
            + b"data" + struct.pack("<I", len(data)) + data)


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


def test_wav_of_any_depth_or_layout_is_uploaded_as_16bit_mono():
    """11-05: a 24-bit EXTENSIBLE editor export was refused before reaching the provider."""
    ramp = (np.arange(242550) % 2000 - 1000) * 7
    pcm24 = extensible_wav(ramp * 512, np.zeros_like(ramp))
    floats = extensible_wav(np.full(88000, .25), np.full(88000, .25), rate=16000, width=4, tag=3)
    with pytest.raises(ValueError):
        check_audio("voice.wav", pcm24)
    for audio, rate, expected in ((pcm24, 44100, ramp), (floats, 16000, np.full(88000, 8192))):
        with wave.open(io.BytesIO(prepare_audio("voice.wav", audio))) as sent:
            assert (sent.getnchannels(), sent.getsampwidth(), sent.getframerate()) == (1, 2, rate)
            assert np.array_equal(np.frombuffer(sent.readframes(sent.getnframes()), "<i2"),
                                  expected)
    assert prepare_audio("voice.wav", wav()) == wav()


def test_unusable_reference_audio_says_what_is_wrong():
    with pytest.raises(ValueError, match="当前 4.2 秒"):
        prepare_audio("voice.wav", wav(4.2))
    with pytest.raises(ValueError, match="当前 8000Hz"):
        prepare_audio("voice.wav", wav(6, rate=8000))
    adpcm = extensible_wav(np.zeros(1000), np.zeros(1000), width=2, tag=2)
    with pytest.raises(ValueError, match="不支持的 WAV 编码"):
        prepare_audio("voice.wav", adpcm)


def test_cloud_refusal_names_the_step_and_reason_without_upload_secrets(monkeypatch):
    def reply(request):
        if request.url.path.endswith("/uploads"):
            return httpx.Response(200, json={"data": {
                "upload_host": "https://bucket.oss-cn-beijing.aliyuncs.com",
                "upload_dir": "voices", "oss_access_key_id": "STS.upload-id",
                "signature": "signed", "policy": "policy", "x_oss_object_acl": "private",
                "x_oss_forbid_overwrite": "true"}})
        return httpx.Response(403, text=(
            "<Error><Code>AccessDenied</Code><Message>Invalid according to Policy: "
            "Policy expired.</Message><OSSAccessKeyId>STS.upload-id</OSSAccessKeyId></Error>"))
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: client(transport=httpx.MockTransport(reply), **kw))
    provider = DashscopeVoiceProvider("test-key", "wss://dashscope.aliyuncs.com/api-ws/v1/inference")
    with pytest.raises(VoiceCloneError) as refused:
        asyncio.run(provider.clone("voice.wav", wav(), "annie12345", "zh"))
    assert refused.value.reason == ("参考音频上传云端失败：Invalid according to Policy: "
                                    "Policy expired.（AccessDenied）")
    assert "STS.upload-id" not in str(refused.value)
    arrears = httpx.Response(400, json={"code": "Arrearage", "message": "Access denied."})
    assert (VoiceCloneError.from_response("enroll", arrears).reason
            == "云端创建音色失败：阿里云账户欠费或余额不足（Arrearage）")


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


def test_personality_voice_sits_between_chat_and_robot_choices(tmp_path):
    async def exercise():
        store = RuntimeStore(tmp_path / "var" / "runtime.db")
        store.migrate()
        profiles = VoiceProfiles(store, FakeVoiceProvider(), "original")
        await profiles.start()
        try:
            bound = (await profiles.clone("毒舌女声", "voice.wav", wav()))["voice_id"]
            chosen = (await profiles.clone("温柔女声", "voice.wav", wav()))["voice_id"]
            identity = {"robot_id": "a", "chat_id": "daily", "persona_id": "annie"}
            wanted = {"voice": None}
            profiles.bound_voice = lambda _identity: wanted["voice"]
            await profiles.select(identity, chosen, "robot")
            assert profiles.resolved(identity) == chosen
            wanted["voice"] = "毒舌女声"  # bound by name
            assert profiles.resolved(identity) == bound
            assert profiles.snapshot(identity)["persona_voice_id"] == bound
            wanted["voice"] = bound  # or by ID
            assert profiles.resolved(identity) == bound
            wanted["voice"] = "还没克隆的声音"  # an unknown voice falls through
            assert profiles.resolved(identity) == chosen
            wanted["voice"] = "毒舌女声"
            await profiles.select(identity, chosen, "chat")
            assert profiles.resolved(identity) == chosen  # the chat's own choice wins
        finally:
            await profiles.close()
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


def test_system_default_beats_the_personality_voice_and_the_original_voice_returns(tmp_path):
    async def exercise():
        store = RuntimeStore(tmp_path / "var" / "runtime.db")
        store.migrate()
        profiles = VoiceProfiles(store, FakeVoiceProvider(), "original")
        profiles.bound_voice = lambda _identity: "毒舌女声"
        identity = {"robot_id": "a", "chat_id": "daily", "persona_id": "annie"}
        await profiles.start()
        try:
            bound = (await profiles.clone("毒舌女声", "voice.wav", wav()))["voice_id"]
            await profiles.clone("温柔女声", "voice.wav", wav())
            assert profiles.resolved(identity) == bound
            assert profiles.resolve_request(identity, "系统默认", "换回系统默认的声音") == (SYSTEM, None)
            assert (await profiles.select(identity, SYSTEM, "chat"))["voice_name"] == "系统默认"
            assert profiles.resolved(identity) == "original"
            assert profiles.snapshot(identity)["chat_voice_id"] == SYSTEM
            assert profiles.resolve_request(identity, "", "换个声音") == (None, ["毒舌女声", "温柔女声"])
            assert profiles.resolve_request(identity, "原来的声音", "还是用原来的声音吧") == (DEFAULT, None)
            await profiles.select(identity, DEFAULT, "chat")
            assert profiles.resolved(identity) == bound
            assert profiles.resolve_request(identity, "", "换个声音") == (None, ["系统默认", "温柔女声"])
            assert profiles.resolve_request(identity, "温柔女声", "系统默认还是温柔女声")[0] is None
            await profiles.select(identity, SYSTEM, "chat")
        finally:
            await profiles.close()

        second = VoiceProfiles(store, FakeVoiceProvider(), "original")
        second.bound_voice = lambda _identity: "毒舌女声"
        await second.start()
        try:
            assert second.resolved(identity) == "original"
        finally:
            await second.close()
    asyncio.run(exercise())


def test_realtime_voice_switch_follows_casual_requests_refusals_and_handed_back_choices(tmp_path):
    async def exercise():
        rt = runtime(tmp_path)
        rt.voices.provider = FakeVoiceProvider()
        await ready(rt)
        try:
            gentle = (await rt.clone_voice("a", "温柔女声", "voice.wav", wav()))["voice_id"]
            sharp = (await rt.clone_voice("a", "毒舌女声", "voice.wav", wav()))["voice_id"]
            client = rt.realtime["a"]
            gentle_call = {"voice_name": "温柔女声", "scope": "chat"}

            client._create_turn("别换声音，这个挺好")
            refused = (await result_from_call(client, "negated", "select_voice", gentle_call))[0]
            assert refused["status"] == "rejected" and "问一句" not in refused["instruction"]
            client._finish("completed")

            client._create_turn("这个声音有点吵")  # no rule places it and no decision model is up
            asked = (await result_from_call(client, "vague", "select_voice", gentle_call))[0]
            assert asked["status"] == "rejected" and "问一句" in asked["instruction"]
            assert set(client._voice_offer["options"]) == {"系统默认", "毒舌女声", "温柔女声"}
            client._finish("completed")

            client._create_turn("你定吧")
            picked = (await result_from_call(client, "handed-back", "select_voice", gentle_call))[0]
            assert picked["status"] == "selected" and picked["effective"] == "this_reply"
            assert "接下来说的这句已经是「温柔女声」" in picked["instruction"]
            assert client.speech.voice_provider(client.turn_identity) == gentle
            assert client._voice_offer is None
            client._finish("completed")

            client._create_turn("试试毒舌女声")
            client.speech.turn_id, client.speech.locked_voice = client.turn_id, gentle  # audio already out
            late = (await result_from_call(client, "late", "select_voice",
                                           {"voice_name": "毒舌女声", "scope": "chat"}))[0]
            assert late["status"] == "selected" and late["effective"] == "next_tts_task"
            assert "下一轮起才是「毒舌女声」" in late["instruction"]
            assert client.speech.voice_provider(client.turn_identity) == sharp
            client._finish("completed")

            client._create_turn("不要这个声音了，换成温柔女声")  # turns the current one down, not the switch
            back = (await result_from_call(client, "not-this-one", "select_voice", gentle_call))[0]
            assert back["status"] == "selected"
            assert client.speech.voice_provider(client.turn_identity) == gentle
        finally:
            await rt.close()
    asyncio.run(exercise())


def test_decision_model_can_confirm_a_voice_the_words_only_hint_at(tmp_path):
    async def exercise():
        rt = runtime(tmp_path)
        rt.voices.provider = FakeVoiceProvider()
        await ready(rt)
        try:
            gentle = (await rt.clone_voice("a", "温柔女声", "voice.wav", wav()))["voice_id"]
            client = rt.realtime["a"]
            client.decision = FakeDecision({"choice": "yes", "confidence": .9})
            client._create_turn("嗯，温柔一点的那个好听")
            result = (await result_from_call(client, "hinted", "select_voice",
                                             {"voice_name": "温柔女声", "scope": "chat"}))[0]
            assert result["status"] == "selected"
            assert client.speech.voice_provider(client.turn_identity) == gentle
            assert len(client.decision.calls) == 1
        finally:
            await rt.close()
    asyncio.run(exercise())
