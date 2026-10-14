"""Per-robot streaming sherpa KWS and Qwen audio admission."""

import asyncio
import time

from annie_runtime.presence import DialoguePresence

SAMPLE_RATE = 16000
FRAME_BYTES = SAMPLE_RATE * 2 // 50  # Room contract: 20ms s16le mono
WINDOW_BYTES = SAMPLE_RATE * 2 * 2  # simulation test window; not sherpa requirement
# Sherpa reports a keyword hit, not the sample where the keyword ended.
# Its confirmation can lag behind the start of a same-breath question.
# Retain 1.6s of bounded PCM: 800ms could omit early question words.
PRE_ROLL_BYTES = FRAME_BYTES * 80  # 1600ms accommodates late keyword confirmation
WAKE_WAIT_SECONDS = 5.0
FOLLOWUP_SECONDS = 8.0
STREAM_BATCH_BYTES = FRAME_BYTES * 2     # 40ms input reduces detection batch wait
STREAM_BACKLOG_LIMIT_BYTES = FRAME_BYTES * 25  # 500ms fail-closed backlog
WAKE_ADDRESS_INSTRUCTIONS = (
    "\n【本地唤醒称呼】语音入口使用本地唤醒词“小安妮”。"
    "语音句首的“小安妮”及其近音转写“小爱你/小安尼/小安你/小爱妮/小阿妮”，"
    "在紧接问候或请求时只是呼叫称呼，不是表白或独立话题。"
    "直接回应后面的问候或请求，不要围绕称呼打趣、复述“小爱你”或推断爱意，"
    "也不要把这个近音当成用户的姓名，"
    "也不要沿用历史中由这些误识别引出的表白梗。"
    "仅有唤醒称呼不构成聊天问题，等待后续提问。"
    "用户明确讨论词义或真实表达“我爱你”等情感时保留原意；"
    "本规则只解释语音呼叫，不改写文字输入或用户的真实表达。"
)


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
        self.last_completed_decode_ms = None
        self.last_detected_keyword = None
        self.last_keyword_tokens = []
        self.last_keyword_token_times_ms = []
        self._batch_started_at = None

    @property
    def state(self):
        return self.presence.state

    @property
    def expirations(self):
        return self.presence.expirations

    @property
    def inference_age_ms(self):
        return (max(0, (self.clock() - self._batch_started_at) * 1000)
                if self._batch_started_at is not None else 0)

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
            completed = self.complete_inference()
            if completed:
                return completed
            self._start_inference()
            return ()
        # Legacy 2s fake detector is retained only for current synthetic tests.
        now = self.clock()
        if len(self.recent) < WINDOW_BYTES or now < self.next_inference_at:
            return ()
        self.next_inference_at = now + 0.5
        if await self.detector.detected(bytes(self.recent)):
            return self._admit_wake()
        return ()

    def _start_inference(self):
        if len(self.pending) >= STREAM_BACKLOG_LIMIT_BYTES:
            self.error = "stream_backlog"
            raise RuntimeError("sherpa KWS stream backlog exceeded")
        if self.task is None and len(self.pending) >= STREAM_BATCH_BYTES:
            batch = bytes(self.pending)
            self.pending.clear()
            self._batch_started_at = self.clock()
            self.task = asyncio.create_task(self.detector.detected(batch))

    def complete_inference(self, *, discard=False) -> tuple[bytes, ...]:
        """Consume one finished native job, independently of future PCM arrival.

        The audio pump owns delivery and validates its input/generation identity.
        frames() also uses this path for existing direct gate consumers.
        """
        if self.task is None or not self.task.done():
            return ()
        task, self.task = self.task, None
        batch_at, self._batch_started_at = self._batch_started_at, None
        if task.cancelled():
            return ()
        try:
            triggered = task.result()
        except Exception as exc:
            self.error = type(exc).__name__
            raise RuntimeError(f"sherpa KWS failed: {self.error}") from exc
        if discard:
            # Retrieve native errors before retiring an overage success; never
            # briefly admit stale audio or increment the wake counter.
            self.reset()
            return ()
        decoded_ms = getattr(self.detector, "last_decode_ms", None)
        if decoded_ms is not None:
            self.last_completed_decode_ms = decoded_ms
        if triggered:
            self.last_detected_keyword = getattr(self.detector, "last_keyword", None)
            self.last_keyword_tokens = list(getattr(self.detector, "last_keyword_tokens", []))
            self.last_keyword_token_times_ms = list(getattr(
                self.detector, "last_keyword_token_times_ms", []
            ))
            self.last_trigger_lag_ms = (round((self.clock() - batch_at) * 1000, 1)
                                       if batch_at is not None else None)
            return self._admit_wake()
        # PCM can arrive while native decoding runs. Drain its complete batch
        # even if the sender becomes silent before the previous job finishes.
        self._start_inference()
        return ()

    def _admit_wake(self):
        if not self.presence.wake_detected():
            return ()
        self.wakes += 1
        self.last_wake_at_ms = int(time.time() * 1000)
        self._was_awake = True
        # Sherpa does not expose an exact keyword-end sample. The overlap may
        # contain some wake word; the response gate must reject wake-only ASR.
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
            "last_keyword": (getattr(self.detector, "last_keyword", None)
                             or self.last_detected_keyword),
            "last_keyword_tokens": list(self.last_keyword_tokens),
            "last_keyword_token_times_ms": list(self.last_keyword_token_times_ms),
            "keyword_time_basis": "decoder_relative_token_emission",
            "keyword_end_sample": None,
            "preroll_policy": "preserve_question_unverified_keyword_end",
            "keywords_file": getattr(self.factory, "keywords_name", None),
            "global_threshold": getattr(self.factory, "threshold", None),
            "batch_audio_ms": STREAM_BATCH_BYTES // FRAME_BYTES * 20,
            "preroll_ms": PRE_ROLL_BYTES // FRAME_BYTES * 20,
            "last_wake_at_ms": self.last_wake_at_ms,
            "last_trigger_lag_ms": self.last_trigger_lag_ms,
            "last_decode_ms": (getattr(self.detector, "last_decode_ms", None)
                               if getattr(self.detector, "last_decode_ms", None) is not None
                               else self.last_completed_decode_ms),
        }
