"""Global voiceprints persist like memory and reach the current reply prompt."""

import asyncio
import io
import time
import wave
from pathlib import Path

import httpx
import numpy as np
from annie_adapters import RuntimeStore
from annie_adapters.speakers import match_gallery, unit_vector
from annie_runtime.realtime import RealtimeRobot
from annie_runtime.speakers import SpeakerService, SpeakerTracker, wav_to_enrollment
from annie_web import create_app

from tests.test_phase7_runtime import ready, runtime


class BucketEmbedder:
    model_id = "fake-speaker"

    def embed(self, samples):
        vector = np.zeros(4, np.float32)
        vector[0 if float(np.mean(samples)) < 0.4 else 1] = 1
        return vector


def tone_wav(rate, seconds, amplitude):
    count = int(rate * seconds)
    pcm = np.full(count, int(amplitude * 32767), np.int16)
    if rate != 16000:
        # Stereo at the source rate exercises resampling, not the matcher.
        pcm = np.column_stack((pcm, pcm)).reshape(-1)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(2 if rate != 16000 else 1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm.astype("<i2").tobytes())
    return buffer.getvalue()


def pcm_from_amplitude(amplitude, seconds=1.2):
    return np.full(int(16000 * seconds), int(amplitude * 32767), np.int16).tobytes()


def service(tmp_path, embedder=None):
    store = RuntimeStore(tmp_path / "runtime.db")
    store.migrate()
    book = SpeakerService(store, embedder or BucketEmbedder(), threshold=0.5, margin=0.08)
    book.reload()
    return book


def test_match_requires_margin_and_rejects_unknown():
    ming = {"speaker_id": "ming", "name": "小明", "stale": False,
            "vector": unit_vector([1, 0, 0, 0])}
    hong = {"speaker_id": "hong", "name": "小红", "stale": False,
            "vector": unit_vector([0, 1, 0, 0])}
    assert match_gallery([1, 0, 0, 0], [ming, hong], threshold=0.5, margin=0.08)["name"] == "小明"
    assert match_gallery([0, 0, 1, 0], [ming, hong], threshold=0.5, margin=0.08)["status"] == "unknown"
    close = match_gallery([0.72, 0.70, 0, 0], [ming, hong], threshold=0.5, margin=0.08)
    assert close["status"] == "ambiguous"
    assert "name" not in close


def test_enrollment_is_global_persistent_and_discards_audio(tmp_path):
    book = service(tmp_path)
    created = book.enroll("  小明  ", tone_wav(16000, 3.2, 0.22))
    assert created["status"] == "created"
    updated = book.enroll("小明", tone_wav(48000, 4, 0.22))
    assert updated["status"] == "updated"
    assert updated["speaker_id"] == created["speaker_id"]
    again = SpeakerService(book.store, BucketEmbedder())
    again.reload()
    assert [row["name"] for row in again.catalog()["speakers"]] == ["小明"]
    raw = Path(book.store.path).read_bytes()
    assert b"RIFF" not in raw
    known = book.identify(pcm_from_amplitude(0.22))
    stranger = book.identify(pcm_from_amplitude(0.7))
    assert known["name"] == "小明"
    assert stranger["status"] == "unknown"
    assert book.delete(created["speaker_id"])["name"] == "小明"
    again.reload()
    assert again.catalog()["speakers"] == []


def test_short_quiet_and_missing_model_are_explicit(tmp_path):
    book = service(tmp_path)
    try:
        wav_to_enrollment(tone_wav(16000, 1.0, 0.22))
        raise AssertionError("short clip should fail")
    except ValueError as exc:
        assert "3" in str(exc)
    try:
        wav_to_enrollment(tone_wav(16000, 3.2, 0.0))
        raise AssertionError("silence should fail")
    except ValueError as exc:
        assert "太轻" in str(exc)
    empty = SpeakerService(book.store, None)
    try:
        empty.enroll("小明", tone_wav(16000, 3.2, 0.22))
        raise AssertionError("missing model should fail")
    except RuntimeError as exc:
        assert "下载声纹模型" in str(exc)


def test_tracker_names_the_current_utterance_and_carries_only_a_short_followup(tmp_path):
    async def exercise():
        book = service(tmp_path)
        book.enroll("小明", tone_wav(16000, 3.2, 0.22))
        sent = []

        async def publish():
            sent.append(object())

        now = {"t": 100.0}
        tracker = SpeakerTracker(book, publish, clock=lambda: now["t"])
        tracker.begin()
        tracker.observe(pcm_from_amplitude(0.22, 0.4))
        assert tracker.task is None
        slow = BucketEmbedder.embed

        def delayed(self, samples):
            time.sleep(0.2)
            return slow(self, samples)

        BucketEmbedder.embed = delayed
        try:
            began = time.perf_counter()
            tracker.observe(pcm_from_amplitude(0.22, 1.2))
            assert time.perf_counter() - began < 0.1
            await tracker.task
        finally:
            BucketEmbedder.embed = slow
        assert "小明" in tracker.instruction_hint()
        assert "不要每句都说认出了谁" in tracker.instruction_hint()
        assert sent
        tracker.begin()
        await tracker.finish()
        assert tracker.public()["status"] == "carried"
        assert tracker.public()["name"] == "小明"
        tracker.begin()
        tracker.observe(pcm_from_amplitude(0.7, 1.2))
        await tracker.finish()
        assert tracker.public()["status"] == "unknown"
        assert "小明" not in tracker.instruction_hint()
        now["t"] = 130
        assert tracker.instruction_hint() == ""

    asyncio.run(exercise())


def test_prompt_update_uses_the_matched_name_before_the_reply(tmp_path):
    async def exercise():
        book = service(tmp_path)
        book.enroll("小明", tone_wav(16000, 3.2, 0.22))

        class Socket:
            def __init__(self):
                self.sent = []

            async def send(self, event):
                self.sent.append(event)

        robot = type("Robot", (), {})()
        robot.config = type("Config", (), {"robot_id": "a"})()
        robot.generation = 1
        robot.turn_cache = {}
        robot.session_state = lambda: {"robot_id": "a", "chat_id": "one"}
        client = RealtimeRobot(robot, url="", key="")
        client.ready = True
        client.socket = Socket()
        client._visual_base_instructions = "你是小安妮。"
        client.speakers = SpeakerTracker(book, client.note_speaker)
        client.speakers.begin()
        client.speakers.observe(pcm_from_amplitude(0.22, 1.2))
        await client.speakers.finish()
        assert client.socket.sent
        instructions = client.socket.sent[-1]["session"]["instructions"]
        assert "这句话是「小明」说的" in instructions
        assert "不要每句都说认出了谁" in instructions
        assert client.snapshot()["speaker"]["name"] == "小明"

    asyncio.run(exercise())


def test_speaker_api_is_global_and_reports_a_missing_model(tmp_path):
    async def exercise():
        rt = runtime(tmp_path)
        await ready(rt)
        app = create_app(rt, Path(__file__).resolve().parents[1] / "web" / "dist")
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://testserver") as client:
                missing = await client.post("/api/speakers", params={"name": "小明"},
                                            content=tone_wav(16000, 3.2, 0.22))
                assert missing.status_code == 503
                assert "下载声纹模型" in missing.json()["detail"]
                rt.speakers.embedder = BucketEmbedder()
                created = await client.post("/api/speakers", params={"name": "小明"},
                                            content=tone_wav(16000, 3.2, 0.22))
                assert created.status_code == 200, created.text
                listed = await client.get("/api/speakers")
                assert listed.json()["speakers"][0]["name"] == "小明"
                assert "embedding" not in listed.text
                removed = await client.delete("/api/speakers/" + created.json()["speaker_id"])
                assert removed.status_code == 200
                assert (await client.get("/api/speakers")).json()["speakers"] == []
        finally:
            await rt.close()

    asyncio.run(exercise())
