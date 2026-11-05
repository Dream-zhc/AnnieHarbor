"""Settings API exercises no actual clone provider or Room audio."""

import asyncio
import io
import wave
from pathlib import Path

import httpx
import numpy as np
from annie_runtime.voice_profiles import VoiceCloneError
from annie_web import create_app

from tests.test_phase7_runtime import ready, runtime
from tests.test_voice_profiles import extensible_wav


class FakeProvider:
    async def clone(self, filename, audio, prefix, language):
        return "qwen-audio-3.0-tts-flash-annie-fakeid"

    async def synthesize(self, voice, preset):
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(b"\x00\x00" * 2400)
        return buffer.getvalue()


class RecordingProvider(FakeProvider):
    def __init__(self):
        self.uploads, self.refusal = [], None

    async def clone(self, filename, audio, prefix, language):
        if self.refusal:
            raise self.refusal
        self.uploads.append(audio)
        return "qwen-audio-3.0-tts-flash-" + prefix


def source_wav(frames=82000):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * frames)
    return buffer.getvalue()


def test_settings_api_upload_preview_rename_and_stale_switch(tmp_path):
    async def exercise():
        rt = runtime(tmp_path)
        rt.voices.provider = FakeProvider()
        await ready(rt)
        app = create_app(rt, Path(__file__).resolve().parents[1] / "web" / "dist")
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://testserver") as client:
                base = "/api/robots/a/voices"
                invalid = await client.post(base + "/clone?name=声音甲&filename=bad.wav",
                                            content=b"broken")
                assert invalid.status_code == 422
                uploaded = await client.post(base + "/clone?name=声音甲&filename=voice.wav",
                                             content=source_wav())
                assert uploaded.status_code == 200, uploaded.text
                voice_id = uploaded.json()["voice_id"]
                listing = await client.get(base)
                assert listing.json()["voices"][0]["name"] == "声音甲"
                wrong = await client.put(base + "/selection", json={
                    "chat_id": "default", "persona_id": "annie",
                    "session_epoch": 12345, "generation": 12345,
                    "voice_id": voice_id, "scope": "chat",
                })
                assert wrong.status_code == 409
                identity = rt.session_state("a")
                switched = await client.put(base + "/selection", json={
                    "chat_id": identity["chat_id"], "persona_id": identity["persona_id"],
                    "session_epoch": identity["session_epoch"],
                    "generation": identity["generation"],
                    "voice_id": voice_id, "scope": "chat",
                })
                assert switched.status_code == 200, switched.text
                assert (await client.get(base)).json()["active_voice_id"] == voice_id
                preview = await client.post(base + "/" + voice_id + "/previews/mandarin")
                assert preview.status_code == 200, preview.text
                audio = await client.get(base + "/" + voice_id + "/previews/mandarin")
                assert audio.content.startswith(b"RIFF")
                renamed = await client.patch(base + "/" + voice_id, json={"name": "温柔"})
                assert renamed.status_code == 200
                removed = await client.delete(base + "/" + voice_id)
                assert removed.json()["status"] == "deleted_locally"
                assert (await client.get(base)).json()["active_voice_id"] == rt.voices.default_voice
                assert (await client.get(base + "/" + voice_id + "/previews/mandarin")).status_code == 404
        finally:
            await rt.close()
    asyncio.run(exercise())


def test_clone_api_converts_editor_exports_and_explains_refusals(tmp_path):
    """11-05: a 24-bit EXTENSIBLE export got 422 and cloud refusals read only "失败"."""
    async def exercise():
        rt = runtime(tmp_path)
        provider = rt.voices.provider = RecordingProvider()
        await ready(rt)
        app = create_app(rt, Path(__file__).resolve().parents[1] / "web" / "dist")
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://testserver") as client:
                base = "/api/robots/a/voices/clone?filename=voice.wav&name="
                ramp = (np.arange(242550) % 2000 - 1000) * 256
                uploaded = await client.post(base + "可爱萝莉", content=extensible_wav(ramp, ramp))
                assert uploaded.status_code == 200, uploaded.text
                with wave.open(io.BytesIO(provider.uploads[0])) as sent:
                    assert (sent.getnchannels(), sent.getsampwidth()) == (1, 2)
                short = await client.post(base + "本座", content=source_wav(67200))
                assert (short.status_code, short.json()["detail"]) == (
                    422, "参考音频须为5至60秒，当前 4.2 秒")
                provider.refusal = VoiceCloneError("enroll", 400, "Arrearage", "Access denied.")
                refused = await client.post(base + "御姐", content=source_wav())
                assert (refused.status_code, refused.json()["detail"]) == (
                    502, "云端创建音色失败：阿里云账户欠费或余额不足（Arrearage）")
        finally:
            await rt.close()
    asyncio.run(exercise())
