"""Per-robot cloud-side wake admission using one shared LiveKit ONNX model.

This is not speaker identification. Only PCM that passed wake admission may
reach the automatic Qwen smart_turn session.
"""

import asyncio
import time
from pathlib import Path

SAMPLE_RATE = 16000
WINDOW_BYTES = SAMPLE_RATE * 2 * 2  # LiveKit classifier requires ~2 seconds
FRAME_BYTES = SAMPLE_RATE * 2 // 50  # existing Qwen 20 ms append contract
PRE_ROLL_BYTES = FRAME_BYTES * 12  # keep 240 ms at detection boundary
WAKE_WAIT_SECONDS = 5.0
FOLLOWUP_SECONDS = 8.0
ACTIVE_WATCHDOG_SECONDS = 30.0
INFER_INTERVAL_SECONDS = 0.5


class SharedWakeModel:
    """Load ONNX once; serialize access to the shared feature extractor."""

    def __init__(self, model_path: Path, *, threshold: float = 0.5):
        if not 0 < threshold <= 1:
            raise ValueError("wakeword threshold must be in (0, 1]")
        if not model_path.is_file() or model_path.suffix.lower() != ".onnx":
            raise ValueError(f"wakeword ONNX model missing: {model_path}")
        try:
            import numpy as np
            from livekit.wakeword import WakeWordModel
        except ImportError as exc:
            raise RuntimeError("enable wakeword requires: uv pip install livekit-wakeword") from exc
        self.numpy = np
        self.model = WakeWordModel(models=[str(model_path)])
        self.threshold = threshold
        self.lock = asyncio.Lock()

    async def detected(self, pcm: bytes) -> bool:
        async with self.lock:
            # ONNX inference is blocking; it must not run in the event loop.
            scores = await asyncio.to_thread(
                self.model.predict, self.numpy.frombuffer(pcm, dtype=self.numpy.int16)
            )
        return bool(scores and max(scores.values()) >= self.threshold)


class WakeAdmission:
    """Independent state and bounded 16kHz mono PCM cache for one robot."""

    def __init__(self, detector, *, clock=time.monotonic,
                 wake_wait_ms=5000, followup_ms=8000):
        if (type(wake_wait_ms) is not int or not 1000 <= wake_wait_ms <= 30000 or
                type(followup_ms) is not int or not 1000 <= followup_ms <= 30000):
            raise ValueError("wakeword wait windows must be integer milliseconds (1000..30000)")
        self.detector = detector
        self.clock = clock
        self.wake_wait_seconds = wake_wait_ms / 1000
        self.followup_seconds = followup_ms / 1000
        self.state = "SLEEP"
        self.deadline = None
        self.recent = bytearray()
        self.next_inference_at = 0.0
        self.wakes = 0
        self.expirations = 0
        self.rejected_frames = 0

    def reset(self):
        self.state = "SLEEP"
        self.deadline = None
        self.recent.clear()
        self.next_inference_at = 0.0

    def _expire(self, now):
        if self.deadline is not None and now >= self.deadline:
            self.expirations += 1
            self.reset()

    async def frames(self, pcm: bytes) -> tuple[bytes, ...]:
        """Drop sleep PCM before the provider; infer at most twice per second."""
        now = self.clock()
        self._expire(now)
        if self.state != "SLEEP":
            return (pcm,)
        self.rejected_frames += 1
        self.recent.extend(pcm)
        if len(self.recent) > WINDOW_BYTES:
            del self.recent[:-WINDOW_BYTES]
        if len(self.recent) < WINDOW_BYTES or now < self.next_inference_at:
            return ()
        self.next_inference_at = now + INFER_INTERVAL_SECONDS
        if not await self.detector.detected(bytes(self.recent)):
            return ()
        self.wakes += 1
        self.state = "WAKE_WAIT"
        self.deadline = self.clock() + self.wake_wait_seconds
        # The model returns no word boundary. This short pre-roll reduces
        # clipping for same-breath questions without replaying all wake speech.
        tail = bytes(self.recent[-PRE_ROLL_BYTES:])
        self.recent.clear()
        return tuple(tail[offset:offset + FRAME_BYTES]
                     for offset in range(0, len(tail), FRAME_BYTES))

    def speech_started(self):
        now = self.clock()
        self._expire(now)
        if self.state in {"WAKE_WAIT", "FOLLOWUP"}:
            self.state = "ACTIVE"
            self.deadline = now + ACTIVE_WATCHDOG_SECONDS

    def speaker_drained(self):
        now = self.clock()
        self._expire(now)
        if self.state == "ACTIVE":
            self.state = "FOLLOWUP"
            self.deadline = now + self.followup_seconds

    def snapshot(self):
        self._expire(self.clock())
        return {"enabled": True, "state": self.state, "wakes": self.wakes,
                "expirations": self.expirations,
                "sleep_frames_rejected": self.rejected_frames,
                "deadline_remaining_ms": (max(0, int((self.deadline - self.clock()) * 1000))
                                          if self.deadline is not None else None)}
