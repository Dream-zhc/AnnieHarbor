"""Enroll a global name and tell the current turn who is speaking."""

import asyncio
import io
import logging
import math
import threading
import time
import wave

import numpy as np
import soxr
from annie_adapters.speakers import (
    MODEL_ID,
    SAMPLE_RATE,
    SpeakerModelMissing,
    clean_name,
    delete_speaker_profile,
    load_speaker_profiles,
    match_gallery,
    upsert_speaker_profile,
)

logger = logging.getLogger(__name__)
MIN_ENROLL_SECONDS = 3.0
MAX_ENROLL_SECONDS = 12.0
EARLY_SECONDS = 1.0
FINAL_MIN_SECONDS = 0.6
MAX_MATCH_SECONDS = 6.0
CARRY_SECONDS = 20.0
IDENTIFY_WAIT_SECONDS = 0.35
RING_SECONDS = 8.0
ENROLL_RMS = 0.01
SPEECH_RMS = 0.008


def _rms(samples):
    if samples.size == 0:
        return 0.0
    return math.sqrt(float(np.mean(samples * samples)))


def wav_to_enrollment(data: bytes):
    """Accept a browser WAV and return 16 kHz float32 mono, without keeping the file."""
    if not isinstance(data, (bytes, bytearray)) or len(data) < 44:
        raise ValueError("请上传 WAV 录音")
    if len(data) > 2_000_000:
        raise ValueError("录音不能超过 2MB")
    try:
        with wave.open(io.BytesIO(data), "rb") as handle:
            channels, width, rate, frames = (
                handle.getnchannels(), handle.getsampwidth(),
                handle.getframerate(), handle.getnframes(),
            )
            if frames > 15 * 48000:
                raise ValueError("录音不能超过 12 秒")
            pcm = handle.readframes(frames)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("无法读取 WAV，请重新录制") from exc
    if width != 2 or channels not in {1, 2} or not 8000 <= rate <= 48000:
        raise ValueError("请使用 16-bit、单声道或立体声、8–48 kHz 的 WAV")
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    if channels == 2:
        samples = samples.reshape(-1, 2).mean(axis=1)
    if rate != SAMPLE_RATE:
        samples = np.asarray(soxr.resample(samples, rate, SAMPLE_RATE), dtype=np.float32)
    duration = samples.size / SAMPLE_RATE
    if duration < MIN_ENROLL_SECONDS:
        raise ValueError("请至少录 3 秒")
    if duration > MAX_ENROLL_SECONDS + 0.25:
        raise ValueError("录音不能超过 12 秒")
    if _rms(samples) < ENROLL_RMS:
        raise ValueError("这段声音太轻，听不清说话。请换一段正常音量的录音")
    return np.ascontiguousarray(samples, dtype=np.float32), int(duration * 1000)


class SpeakerService:
    """One gallery for every robot. Raw audio is discarded after the embedding."""

    def __init__(self, store, embedder=None, *, threshold=0.5, margin=0.08):
        if not 0.2 <= float(threshold) <= 0.9 or not 0 <= float(margin) <= 0.3:
            raise ValueError("speaker threshold or margin is out of range")
        self.store = store
        self.embedder = embedder
        self.threshold = float(threshold)
        self.margin = float(margin)
        self.model_id = getattr(embedder, "model_id", MODEL_ID)
        self._lock = threading.Lock()
        self._profiles = []

    def reload(self):
        profiles = load_speaker_profiles(self.store, self.model_id, include_vector=True)
        with self._lock:
            self._profiles = profiles

    def has_profiles(self):
        with self._lock:
            return any(not row["stale"] for row in self._profiles)

    def catalog(self):
        with self._lock:
            speakers = [{key: row[key] for key in (
                "speaker_id", "name", "duration_ms", "created_ms", "updated_ms", "stale",
            )} for row in self._profiles]
        return {
            "available": self.embedder is not None,
            "model": self.model_id,
            "threshold": self.threshold,
            "margin": self.margin,
            "speakers": speakers,
        }

    def enroll(self, name, wav_bytes):
        if self.embedder is None:
            raise SpeakerModelMissing(
                "声纹模型未安装。请在项目根运行 uv run --locked python scripts/下载声纹模型.py"
            )
        name = clean_name(name)
        samples, duration_ms = wav_to_enrollment(wav_bytes)
        vector = self.embedder.embed(samples)
        row = upsert_speaker_profile(
            self.store, name=name, vector=vector, model=self.model_id,
            duration_ms=duration_ms, now_ms=time.time_ns() // 1_000_000,
        )
        self.reload()
        logger.info("speaker enrolled name=%s status=%s", row["name"], row["status"])
        return row

    def delete(self, speaker_id):
        removed = delete_speaker_profile(self.store, speaker_id)
        if removed is not None:
            self.reload()
        return removed

    def identify(self, pcm: bytes):
        if self.embedder is None or not pcm:
            return {"status": "unavailable"}
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        if samples.size < int(FINAL_MIN_SECONDS * SAMPLE_RATE):
            return {"status": "short"}
        if _rms(samples) < SPEECH_RMS:
            return {"status": "quiet"}
        if samples.size > int(MAX_MATCH_SECONDS * SAMPLE_RATE):
            samples = samples[-int(MAX_MATCH_SECONDS * SAMPLE_RATE):]
        vector = self.embedder.embed(np.ascontiguousarray(samples, dtype=np.float32))
        with self._lock:
            profiles = list(self._profiles)
        return match_gallery(vector, profiles, threshold=self.threshold, margin=self.margin)


class SpeakerTracker:
    """Per-robot utterance buffer. PCM append never waits on the embedding."""

    def __init__(self, service: SpeakerService, on_update, *, clock=time.monotonic):
        self.service = service
        self.on_update = on_update
        self.clock = clock
        self.job = 0
        self.task = None
        self.open = False
        self.early_done = False
        self.utterance_start = None
        self.samples = 0
        self.ring = bytearray()
        self.current = None
        self.last = None
        self._hint_changed = False

    def observe(self, pcm: bytes):
        if not pcm or not self.service.has_profiles():
            return
        if len(pcm) % 2:
            pcm = pcm[:-1]
        count = len(pcm) // 2
        if count == 0:
            return
        self.ring.extend(pcm)
        self.samples += count
        max_bytes = int(RING_SECONDS * SAMPLE_RATE) * 2
        if len(self.ring) > max_bytes:
            drop = len(self.ring) - max_bytes
            del self.ring[:drop - (drop % 2)]
        if (self.open and not self.early_done and self.utterance_start is not None
                and self.samples - self.utterance_start >= int(EARLY_SECONDS * SAMPLE_RATE)):
            self.early_done = True
            self._launch(self._slice())

    def begin(self):
        if not self.service.has_profiles():
            return
        self.job += 1
        self.open = True
        self.early_done = False
        preroll = int(0.2 * SAMPLE_RATE)
        origin = self.samples - len(self.ring) // 2
        self.utterance_start = max(origin, self.samples - preroll)

    async def finish(self, *, invalid=False):
        if invalid:
            self.job += 1
            self.open = False
            self.utterance_start = None
            return
        if self.utterance_start is None:
            return
        self.open = False
        pcm = self._slice()
        self.utterance_start = None
        self.job += 1
        job = self.job
        if len(pcm) < int(FINAL_MIN_SECONDS * SAMPLE_RATE) * 2:
            self._apply({"status": "short"})
            await self._emit()
            return
        task = asyncio.create_task(self._identify(pcm))
        self.task = task
        try:
            result = await asyncio.wait_for(asyncio.shield(task), IDENTIFY_WAIT_SECONDS)
        except asyncio.TimeoutError:
            self.task = asyncio.create_task(self._apply_later(task, job))
            return
        if job != self.job or result is None:
            return
        self._apply(result)
        await self._emit()

    def forget(self, speaker_id):
        changed = False
        if self.current and self.current.get("speaker_id") == speaker_id:
            self.current = None
            changed = True
        if self.last and self.last.get("speaker_id") == speaker_id:
            self.last = None
            changed = True
        self._hint_changed = changed
        return changed

    def reset(self):
        self.job += 1
        task, self.task = self.task, None
        if task is not None and not task.done():
            task.cancel()
        self.open = False
        self.early_done = False
        self.utterance_start = None
        self.samples = 0
        self.ring.clear()
        self.current = None
        self.last = None

    def instruction_hint(self):
        if not self.service.has_profiles():
            return ""
        row = self._visible()
        if row is None:
            return ""
        name = row.get("name")
        basis = row.get("basis")
        if basis == "matched" and name:
            return (
                f"\n【当前说话人】这句话是「{name}」说的，声纹已匹配。"
                f"你知道说话人是{name}。只在打招呼、换人、确认，或称呼会让这句话更自然时叫出名字；"
                "不要每句都说认出了谁，也不要复述这段提示。"
                "声纹不能切换会话或人格，也不是门禁。"
                "打字输入没有重新核验，不要据此认定打字的人也是这个人。"
            )
        if basis == "carried" and name:
            return (
                f"\n【当前说话人】上一句可靠识别为「{name}」，这一句太短，没有重新确认。"
                f"可以沿用{name}；如果内容像换了人，就不要坚持这个名字。"
                "只在语境自然时称呼，不要主动宣布识别结果。"
            )
        if row.get("ambiguous"):
            return "\n【当前说话人】这段声音和多于一位已注册的人接近，本轮不要点名，也不要猜测是谁。"
        if basis == "unknown":
            return "\n【当前说话人】本轮没有可靠声纹匹配。不要把这句话安到任何已注册的人身上，也不要猜测姓名。"
        if basis == "short":
            return "\n【当前说话人】这一句太短，无法确认是谁。不要猜测姓名。"
        return ""

    def public(self):
        row = self._visible()
        if row is None:
            return {"status": "idle", "name": None, "score": None}
        status = "ambiguous" if row.get("ambiguous") else row.get("basis") or "idle"
        return {"status": status, "name": row.get("name"), "score": row.get("score")}

    def _visible(self):
        row = self.current
        if not row or self.clock() - row["at"] > CARRY_SECONDS:
            return None
        return row

    def _slice(self):
        if self.utterance_start is None:
            return b""
        start = max(self.utterance_start, self.samples - int(MAX_MATCH_SECONDS * SAMPLE_RATE))
        origin = self.samples - len(self.ring) // 2
        start = max(start, origin)
        offset = (self.samples - start) * 2
        return bytes(self.ring[-offset:] if offset else b"")

    def _launch(self, pcm):
        if len(pcm) < int(EARLY_SECONDS * SAMPLE_RATE) * 2:
            return
        self.job += 1
        job = self.job

        async def run():
            result = await self._identify(pcm)
            if job != self.job or result is None:
                return
            self._apply(result)
            await self._emit()

        self.task = asyncio.create_task(run())

    async def _identify(self, pcm):
        try:
            return await asyncio.to_thread(self.service.identify, pcm)
        except Exception:
            logger.warning("speaker identify failed", exc_info=True)
            return None

    async def _apply_later(self, task, job):
        result = await task
        if job != self.job or result is None:
            return
        self._apply(result)
        await self._emit()

    async def _emit(self):
        if not self._hint_changed or self.on_update is None:
            return
        self._hint_changed = False
        result = self.on_update()
        if asyncio.iscoroutine(result):
            await result

    def _apply(self, result):
        before = self.instruction_hint()
        now = self.clock()
        status = result.get("status")
        if status == "matched":
            self.current = {
                "basis": "matched", "name": result["name"],
                "speaker_id": result["speaker_id"], "score": result.get("score"), "at": now,
            }
            self.last = dict(self.current)
        elif status == "ambiguous":
            self.current = {"basis": "unknown", "ambiguous": True, "name": None,
                            "speaker_id": None, "score": result.get("score"), "at": now}
        elif status in {"unknown"}:
            self.current = {"basis": "unknown", "name": None, "speaker_id": None,
                            "score": result.get("score"), "at": now}
        elif self.last and now - self.last["at"] <= CARRY_SECONDS:
            self.current = {**self.last, "basis": "carried", "at": self.last["at"]}
        else:
            self.current = {"basis": "short", "name": None, "speaker_id": None,
                            "score": None, "at": now}
        self._hint_changed = self.instruction_hint() != before
