"""One-robot cloud AEC, adapted from the user's aec_room_bridge core.

AEC is deliberately downstream of the one Room MIC_AUDIO subscription and
upstream of the existing audio admission gate. It is not a second microphone
publisher. TTS and verified physical speaker reports are the render clock.
The native 10 ms DSP and resampling never run on the asyncio event loop.
"""

from __future__ import annotations

import base64
import logging
import queue
import threading
import time
from collections import deque
from importlib.metadata import PackageNotFoundError, version

import numpy as np
import soxr

MIC_RATE = 16000
TTS_RATE = 24000
MIC_FRAME_BYTES = 640  # 20 ms s16le
RENDER_CHUNK_BYTES = 480  # 10 ms s16le at 24 kHz
CAPTURE_CHUNK_BYTES = 320
REFERENCE_MAX_FRAMES = 1500  # 15 seconds of bounded reference history
STATUS_LOG_SECONDS = 2.0  # one console line per status change, at most this often
logger = logging.getLogger(__name__)


class WebRtcAecBackend:
    """Small WebRTC APM binding carried over from aec_room_bridge/aec_backend.py."""

    def __init__(self, *, delay_ms=0, enable_ns=False, enable_agc=False):
        from aec_audio_processing import AudioProcessor

        try:
            self.version = version("aec-audio-processing")
        except PackageNotFoundError:
            self.version = "unknown"
        self.processor = AudioProcessor(
            enable_aec=True, enable_ns=enable_ns, enable_agc=enable_agc, enable_vad=False,
        )
        try:
            self.processor.set_stream_format(
                sample_rate_in=MIC_RATE, channel_count_in=1,
                sample_rate_out=MIC_RATE, channel_count_out=1,
            )
        except TypeError:
            self.processor.set_stream_format(MIC_RATE, 1)
        self.processor.set_reverse_stream_format(MIC_RATE, 1)
        self.processor.set_stream_delay(delay_ms)
        self.reverse = next((getattr(self.processor, name)
                             for name in ("process_reverse_stream", "process_reverse",
                                          "process_render_stream")
                             if callable(getattr(self.processor, name, None))), None)
        if self.reverse is None:
            raise RuntimeError("AEC backend does not provide a reverse-stream processor")

    def process(self, reference: bytes, capture: bytes) -> bytes:
        if len(reference) != CAPTURE_CHUNK_BYTES or len(capture) != CAPTURE_CHUNK_BYTES:
            raise ValueError("WebRTC APM requires 10ms 16k mono int16 frames")
        self.reverse(reference)
        result = self.processor.process_stream(capture)
        output = bytes(result) if result is not None else capture
        if len(output) != CAPTURE_CHUNK_BYTES:
            raise RuntimeError("AEC returned an invalid sample count")
        return output


class RenderResampler:
    """Stateful Quick Quality libsoxr conversion from old aec_room_bridge."""

    def __init__(self):
        self.stream = soxr.ResampleStream(TTS_RATE, MIC_RATE, 1, dtype="int16", quality="QQ")
        self.pending: deque[np.ndarray] = deque()
        self.pending_samples = 0

    def process(self, pcm: bytes) -> bytes:
        if len(pcm) != RENDER_CHUNK_BYTES:
            raise ValueError("render reference must be 10ms 24k mono int16")
        samples = self.stream.resample_chunk(np.frombuffer(pcm, dtype="<i2"), last=False)
        if samples.size:
            chunk = np.asarray(samples, dtype="<i2")
            self.pending.append(chunk)
            self.pending_samples += int(chunk.size)
        if self.pending_samples < MIC_RATE // 100:
            return bytes(CAPTURE_CHUNK_BYTES)
        output = np.empty(MIC_RATE // 100, dtype="<i2")
        written = 0
        while written < output.size:
            chunk = self.pending[0]
            count = min(output.size - written, chunk.size)
            output[written:written + count] = chunk[:count]
            self.pending_samples -= count
            written += count
            if count == chunk.size:
                self.pending.popleft()
            else:
                self.pending[0] = chunk[count:]
        return output.tobytes()


class ReferenceTimeline:
    """Bounded playback reference using current stream_epoch and actual speaker cursor.

    A remote time estimate is approximate; APM's adaptive filter compensates
    residual delay, but loss of physical receipts must never masquerade as AEC.
    """

    def __init__(self, *, offset_ms=80, max_receipt_age_ms=850):
        if not -300 <= offset_ms <= 500:
            raise ValueError("AEC sync offset must be -300..500ms")
        self.offset_ms = offset_ms
        self.max_receipt_age_ms = max_receipt_age_ms
        self.identity = None
        self.frames: deque[bytes] = deque()
        self.first_frame = 0
        self.next_frame = 0
        self.resampler = RenderResampler()
        self.cursor = None
        self.report_time = None
        self.playing = False
        self.playback_id = None
        self.read_head = None
        self.last_receipt_seq = 0

    def reset(self):
        self.identity = None
        self.frames.clear()
        self.first_frame = self.next_frame = 0
        self.resampler = RenderResampler()
        self.cursor = self.report_time = self.playback_id = self.read_head = None
        self.playing = False
        self.last_receipt_seq = 0

    def render(self, packet):
        data = packet.get("data")
        if not isinstance(data, dict):
            return
        identity = (data.get("conversation_id"), data.get("stream_epoch"))
        if (not isinstance(identity[0], str) or type(identity[1]) is not int or
                data.get("sample_rate") != TTS_RATE or
                data.get("format") != "pcm_s16le"):
            return
        pcm = base64.b64decode(data.get("audio") or "", validate=True)
        if not pcm:
            # Empty stream-final/stop packets may arrive during a new turn.
            # Only a *physical* speaker receipt decides when playback ends.
            return
        start = data.get("start_sample")
        if type(start) is not int or start < 0 or start % (TTS_RATE // 100):
            return
        if self.identity != identity:
            self.reset()
            self.identity = identity
            self.first_frame = self.next_frame = start // (TTS_RATE // 100)
        if len(pcm) % RENDER_CHUNK_BYTES:
            raise ValueError("unaligned TTS render PCM")
        start_frame = start // (TTS_RATE // 100)
        if start_frame != self.next_frame:
            # A gap, retry, or out-of-order packet invalidates the filter state.
            # Never quietly paste non-contiguous reference into its timeline.
            if start_frame < self.next_frame:
                return  # duplicate output packet
            self.reset()
            self.identity = identity
            self.first_frame = self.next_frame = start_frame
        for i in range(0, len(pcm), RENDER_CHUNK_BYTES):
            self.frames.append(self.resampler.process(pcm[i:i + RENDER_CHUNK_BYTES]))
            self.next_frame += 1
            if len(self.frames) > REFERENCE_MAX_FRAMES:
                self.frames.popleft()
                self.first_frame += 1

    def receipt(self, message, at):
        data = message.get("data")
        if not isinstance(data, dict) or data.get("source") != "speaker":
            return False
        if (data.get("conversation_id"), data.get("stream_epoch")) != self.identity:
            return False
        seq = data.get("seq")
        cursor = data.get("played_sample_count")
        kind = message.get("type", "").removeprefix("tts.playback.")
        if (type(seq) is not int or seq <= self.last_receipt_seq or
                type(cursor) is not int or cursor < 0 or cursor > self.next_frame * 240 or
                kind not in {"started", "progress", "resumed", "held", "stopped", "drained"}):
            return False
        self.last_receipt_seq = seq
        self.cursor = cursor
        self.report_time = at
        self.playback_id = data.get("playback_id")
        self.playing = kind in {"started", "progress", "resumed"}
        if not self.playing:
            self.read_head = None
        return True

    def reference(self, at):
        if (self.cursor is None or self.report_time is None or
                not self.playing or
                1000 * (at - self.report_time) > self.max_receipt_age_ms):
            self.read_head = None
            return None
        # The remote speaker cursor is observed at receipt arrival. Compensate
        # approximately for microphone uplink and the separate speaker path.
        elapsed = max(0.0, at - self.report_time)
        cursor_samples = self.cursor + int(elapsed * TTS_RATE)
        cursor_samples -= int(self.offset_ms * TTS_RATE / 1000)
        target = cursor_samples // (TTS_RATE // 100) - 2
        if self.read_head is None or abs(target - self.read_head) > 20:
            self.read_head = target
        else:
            # Gentle ±10ms corrections per 20ms avoids time-jump artifacts.
            self.read_head += max(-1, min(3, target - self.read_head))
        index = self.read_head
        if index < self.first_frame or index + 1 >= self.next_frame:
            return None
        relative = index - self.first_frame
        return self.frames[relative], self.frames[relative + 1]


class ReferencePool:
    """Keep the previous speech reference until the actual speaker changes.

    Publishing a new TTS stream doesn't mean the physical speaker has stopped
    playing an earlier buffered one. Two bounded timelines cover handoff.
    """

    def __init__(self, *, offset_ms=80, max_receipt_age_ms=850):
        self.offset_ms = offset_ms
        self.max_receipt_age_ms = max_receipt_age_ms
        self.timelines = {}
        self.active = None

    def reset(self):
        self.timelines.clear()
        self.active = None

    @property
    def identity(self):
        return self.active or (next(reversed(self.timelines)) if self.timelines else None)

    @property
    def active_timeline(self):
        return self.timelines.get(self.active) if self.active else None

    @property
    def report_time(self):
        return self.active_timeline.report_time if self.active_timeline else None

    @property
    def cursor(self):
        return self.active_timeline.cursor if self.active_timeline else None

    @property
    def playing(self):
        return bool(self.active_timeline and self.active_timeline.playing)

    def render(self, packet):
        data = packet.get("data")
        if not isinstance(data, dict) or not data.get("audio"):
            return
        identity = (data.get("conversation_id"), data.get("stream_epoch"))
        if identity not in self.timelines:
            self.timelines[identity] = ReferenceTimeline(
                offset_ms=self.offset_ms, max_receipt_age_ms=self.max_receipt_age_ms)
        self.timelines[identity].render(packet)
        while len(self.timelines) > 2:
            # Prefer to retain the physical playback's timeline.
            evict = next((item for item in self.timelines if item != self.active), None)
            if evict is None:
                break
            self.timelines.pop(evict)

    def receipt(self, message, at):
        data = message.get("data")
        if not isinstance(data, dict):
            return False
        identity = (data.get("conversation_id"), data.get("stream_epoch"))
        timeline = self.timelines.get(identity)
        if timeline is None or not timeline.receipt(message, at):
            return False
        if timeline.playing:
            self.active = identity
        elif self.active == identity:
            self.active = None
        return True

    def reference(self, at):
        timeline = self.active_timeline
        return timeline.reference(at) if timeline else None


class CloudAec:
    """Bounded one-robot worker; all DSP stays off the Room/Realtime event loop."""

    def __init__(self, loop, output, *, sync_offset_ms=80, delay_ms=0,
                 max_receipt_age_ms=850, backend_factory=None):
        self.loop = loop
        self.output = output
        self.timeline = ReferencePool(offset_ms=sync_offset_ms,
                                      max_receipt_age_ms=max_receipt_age_ms)
        self.backend_factory = backend_factory or (lambda: WebRtcAecBackend(delay_ms=delay_ms))
        self.jobs = queue.Queue(maxsize=32)
        self.running = False
        self.worker = None
        self.generation = 0
        self.stats = {"received": 0, "processed": 0, "bypassed": 0,
                      "drops": 0, "errors": 0, "receipts": 0, "missing_reference": 0}
        self.process_times = deque(maxlen=128)
        self.last_status = "waiting_for_speaker"
        self._logged_status = (None, 0.0)

    def start(self):
        if self.running:
            return
        # Fail at startup, not mid-conversation, if the native binding is absent.
        backend = self.backend_factory()
        self.running = True
        self.worker = threading.Thread(target=self._run, args=(backend,),
                                       name="annie-cloud-aec", daemon=True)
        self.worker.start()

    def _offer(self, job):
        if not self.running:
            return False
        try:
            self.jobs.put_nowait(job)
        except queue.Full:
            self.stats["drops"] += 1
            # Never queue 500ms of old audio; drop oldest work and resync.
            try:
                self.jobs.get_nowait()
            except queue.Empty:
                pass
            try:
                self.jobs.put_nowait(job)
            except queue.Full:
                return False
        return True

    def feed_mic(self, frame: bytes, received_at=None):
        self.stats["received"] += 1
        if len(frame) != MIC_FRAME_BYTES:
            self.stats["errors"] += 1
            return False
        return self._offer(("mic", self.generation, bytes(frame),
                            time.monotonic() if received_at is None else received_at))

    def feed_tts(self, packet):
        if packet.get("type") == "tts":
            self._offer(("render", self.generation, packet, 0))

    def feed_receipt(self, packet):
        self._offer(("receipt", self.generation, packet, time.monotonic()))

    def reset(self):
        self.generation += 1
        self._offer(("reset", self.generation, None, 0))

    def snapshot(self):
        reported = self.timeline.report_time
        latencies = sorted(self.process_times)
        return {"enabled": True, "backend": "webrtc_apm", "worker": self.running,
                "queue_depth": self.jobs.qsize(), "status": self.last_status,
                "processing_p95_ms": (round(latencies[int(.95 * (len(latencies) - 1))], 2)
                                       if latencies else None),
                "speaker_receipt_age_ms": (max(0, int((time.monotonic() - reported) * 1000))
                                           if reported is not None else None),
                "speaker_played_samples": self.timeline.cursor,
                "speaker_active": self.timeline.playing,
                **self.stats.copy()}

    def _run(self, backend):
        while self.running:
            try:
                kind, generation, payload, at = self.jobs.get(timeout=0.1)
            except queue.Empty:
                continue
            if generation != self.generation:
                continue
            began = time.perf_counter() if kind == "mic" else None
            try:
                if kind == "reset":
                    self.timeline.reset()
                    backend = self.backend_factory()
                elif kind == "render":
                    self.timeline.render(payload)
                elif kind == "receipt":
                    if self.timeline.receipt(payload, at):
                        self.stats["receipts"] += 1
                elif kind == "mic":
                    reference = self.timeline.reference(at)
                    if reference is None:
                        if self.timeline.identity is None:
                            self.last_status = "idle_no_tts"
                        elif not self.timeline.playing:
                            self.last_status = "speaker_inactive"
                        else:
                            self.last_status = "unverified_or_stale_reference"
                            self.stats["missing_reference"] += 1
                        self.stats["bypassed"] += 1
                        clean = payload
                    else:
                        clean = (backend.process(reference[0], payload[:CAPTURE_CHUNK_BYTES]) +
                                 backend.process(reference[1], payload[CAPTURE_CHUNK_BYTES:]))
                        self.last_status = "reference_aligned"
                        self.stats["processed"] += 1
                    self.loop.call_soon_threadsafe(self._deliver, generation, clean, at)
            except Exception:
                self.stats["errors"] += 1
                self.last_status = "processing_error"
                if kind == "mic":
                    self.loop.call_soon_threadsafe(self._deliver, generation, payload, at)
            finally:
                if began is not None:
                    self.process_times.append((time.perf_counter() - began) * 1000)
                    self._note_status()

    def _note_status(self):
        """10/21 demo: echo got through and the log had no AEC line at all."""
        status, (logged, at) = self.last_status, self._logged_status
        now = time.monotonic()
        if status == logged or now - at < STATUS_LOG_SECONDS:
            return
        self._logged_status = (status, now)
        reported = self.timeline.report_time
        logger.info("cloud_aec status=%s processed=%d bypassed=%d missing_reference=%d "
                    "receipts=%d drops=%d errors=%d receipt_age_ms=%s", status,
                    self.stats["processed"], self.stats["bypassed"],
                    self.stats["missing_reference"], self.stats["receipts"],
                    self.stats["drops"], self.stats["errors"],
                    int((now - reported) * 1000) if reported is not None else None)

    def _deliver(self, generation, pcm, at):
        if self.running and generation == self.generation:
            self.output(pcm, at)

    def close(self):
        self.running = False
        if self.worker:
            self.worker.join(timeout=1.0)
        self.worker = None


class AecTtsPublisher:
    """Wrap the existing Room publisher; never publish reference a second time."""

    def __init__(self, publisher, aec):
        self.publisher = publisher
        self.aec = aec

    async def publish(self, message):
        await self.publisher.publish(message)
        self.aec.feed_tts(message)

    async def publish_if(self, message, permitted):
        result = await self.publisher.publish_if(message, permitted)
        if result:
            self.aec.feed_tts(message)
        return result

    async def open(self):
        return await self.publisher.open()

    async def close(self):
        return await self.publisher.close()
