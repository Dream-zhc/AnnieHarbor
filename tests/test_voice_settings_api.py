"""Settings API exercises no actual clone provider or Room audio."""

import asyncio
import io
import wave
from pathlib import Path

import httpx
from annie_web import create_app

from tests.test_phase7_runtime import ready, runtime


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


def source_wav():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * 82000)
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
