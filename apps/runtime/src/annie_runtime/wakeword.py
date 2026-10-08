"""Per-robot streaming sherpa KWS and Qwen audio admission."""

import asyncio
import time

from annie_runtime.presence import DialoguePresence

SAMPLE_RATE = 16000
FRAME_BYTES = SAMPLE_RATE * 2 // 50  # Room contract: 20ms s16le mono
WINDOW_BYTES = SAMPLE_RATE * 2 * 2  # simulation test window; not sherpa requirement
PRE_ROLL_BYTES = FRAME_BYTES * 4   # 80ms overlap; do not replay the whole wake phrase
WAKE_WAIT_SECONDS = 5.0
FOLLOWUP_SECONDS = 8.0
STREAM_BATCH_BYTES = FRAME_BYTES * 2     # 40ms input reduces detection batch wait
STREAM_BACKLOG_LIMIT_BYTES = FRAME_BYTES * 25  # 500ms fail-closed backlog


class WakeAdmission:
    """Never await native inference in Room's 20ms PCM audio pump."""

    def __init__(self, detector, *, clock=time.monotonic,
                 wake_wait_ms=5000, followup_ms=8000):
        self.clock = clock
        self.presence = DialoguePresence(clock=clock, wake_wait_ms=wake_wait_ms,
                                         followup_ms=followup_ms)
        self.factory = detector
        self.streaming = hasattr(detector, "new_stream")
        self.detector = detector.new_stream() if self.streaming else detector
        self.recent = bytearray()
        self.pending = bytearray()
        self.task = None
        self.next_inference_at = 0.0
        self.wakes = 0
        self.rejected_frames = 0
        self.error = None
        self.authorized_turn_id = None
        self._was_awake = False
        self.last_wake_at_ms = None
        self.last_trigger_lag_ms = None
        self._batch_started_at = None

    @property
    def state(self):
        return self.presence.state

    @property
    def expirations(self):
        return self.presence.expirations

    def reset(self):
        self.presence.close()
        if self.task and not self.task.done():
            self.task.cancel()
        self.task = None
        # A cancelled to_thread decoder may still finish on its OLD stream.
        self.detector = self.factory.new_stream() if self.streaming else self.factory
        self.recent.clear()
        self.pending.clear()
        self.next_inference_at = 0.0
        self.authorized_turn_id = None
        self.error = None
        self._was_awake = False
        self._batch_started_at = None

    async def frames(self, pcm: bytes) -> tuple[bytes, ...]:
        """Only locally buffer sleep audio; sherpa decoding runs in a task."""
        if len(pcm) != FRAME_BYTES:
            raise ValueError("wake PCM must contain one 20ms/640B frame")
        if self.error is not None:
            raise RuntimeError(f"sherpa KWS failed: {self.error}")
        if self.presence.permits_pcm():
            self._was_awake = True
            return (pcm,)
        if self._was_awake:
            # Sleep window expired: reset pre-wake decoder caches.
            self.detector = self.factory.new_stream() if self.streaming else self.factory
            self.recent.clear()
            self.pending.clear()
            self._was_awake = False
        self.rejected_frames += 1
        self.recent.extend(pcm)
        if len(self.recent) > WINDOW_BYTES:
            del self.recent[:-WINDOW_BYTES]
        if self.streaming:
            self.pending.extend(pcm)
            if self.task is not None and self.task.done():
                task, self.task = self.task, None
                batch_at, self._batch_started_at = self._batch_started_at, None
                try:
                    triggered = task.result()
                except Exception as exc:
                    self.error = type(exc).__name__
                    raise RuntimeError(f"sherpa KWS failed: {self.error}") from exc
                if triggered:
                    self.last_trigger_lag_ms = (round((self.clock() - batch_at) * 1000, 1)
                                                if batch_at is not None else None)
                    return self._admit_wake()
            if len(self.pending) >= STREAM_BACKLOG_LIMIT_BYTES:
                self.error = "stream_backlog"
                raise RuntimeError("sherpa KWS stream backlog exceeded")
            if self.task is None and len(self.pending) >= STREAM_BATCH_BYTES:
                batch = bytes(self.pending)
                self.pending.clear()
                self._batch_started_at = self.clock()
                self.task = asyncio.create_task(self.detector.detected(batch))
            return ()
        # Legacy 2s fake detector is retained only for current synthetic tests.
        now = self.clock()
        if len(self.recent) < WINDOW_BYTES or now < self.next_inference_at:
            return ()
        self.next_inference_at = now + 0.5
        if await self.detector.detected(bytes(self.recent)):
            return self._admit_wake()
        return ()

    def _admit_wake(self):
        if not self.presence.wake_detected():
            return ()
        self.wakes += 1
        self.last_wake_at_ms = int(time.time() * 1000)
        self._was_awake = True
        # A tiny overlap protects the start of same-breath questions. Sherpa
        # hits approximate keyword end, not a sample-accurate boundary.
        tail = bytes(self.recent[-PRE_ROLL_BYTES:])
        self.recent.clear()
        self.pending.clear()
        return tuple(tail[i:i + FRAME_BYTES]
                     for i in range(0, len(tail), FRAME_BYTES))

    def speech_started(self, item_id=None):
        return self.presence.speech_started(item_id)

    def speech_stopped(self, item_id=None, *, invalid=False):
        return self.presence.speech_stopped(item_id=item_id, invalid=invalid)

    def transcript_accepted(self, text, item_id=None):
        return self.presence.transcript_accepted(text, item_id=item_id)

    def speaker_drained(self, turn_id=None):
        if turn_id and turn_id == self.authorized_turn_id:
            return self.presence.speaker_drained(verified=True)
        return False

    def reply_allowed(self):
        return self.presence.permits_audio_response(self.presence.epoch)

    def reply_finished(self, *, has_speaker):
        if self.presence.state != "ACTIVE":
            return
        if not has_speaker:
            # Without a speaker there can be no trustworthy drained receipt.
            self.reset()
        else:
            # Safety recovery only if physical playback never reports drained.
            # Starts after model completion, not while the user is speaking.
            self.presence.deadline = self.clock() + 120.0

    def snapshot(self):
        return {
            "enabled": True, "backend": "sherpa-onnx" if self.streaming else "fake",
            **self.presence.snapshot(), "wakes": self.wakes,
            "sleep_frames_rejected": self.rejected_frames, "error": self.error,
            "decoded_batches": getattr(self.detector, "decoded_batches", None),
            "input_dbfs": getattr(self.detector, "input_dbfs", None),
            "near_silence": getattr(self.detector, "quiet_batches", 0) >= 5,
            "last_keyword": getattr(self.detector, "last_keyword", None),
            "keywords_file": getattr(self.factory, "keywords_name", None),
            "global_threshold": getattr(self.factory, "threshold", None),
            "batch_audio_ms": STREAM_BATCH_BYTES // FRAME_BYTES * 20,
            "preroll_ms": PRE_ROLL_BYTES // FRAME_BYTES * 20,
            "last_wake_at_ms": self.last_wake_at_ms,
            "last_trigger_lag_ms": self.last_trigger_lag_ms,
            "last_decode_ms": getattr(self.detector, "last_decode_ms", None),
        }
