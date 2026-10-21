"""Per-robot independent TTS and the verified reb_tts PCM publication boundary."""

import asyncio
import base64
import logging
import re
import time
from collections import deque
from dataclasses import asdict, replace
from hashlib import sha256

from annie_adapters.tts import TTS_LANGUAGE_HINTS, QwenTtsSocket, command, task_id
from annie_core.speech_style import SpeechStyle

logger = logging.getLogger(__name__)
FRAME_BYTES = 960  # 20ms, 24kHz, S16LE, mono; not the 16kHz MIC_AUDIO format.
SENTENCE_END = frozenset("。！？!?；;\n")
CLAUSE_END = frozenset("，,、")
MIXED_SENTENCE_END = frozenset("。！？!?；;\n.")
MAX_MIXED_SENTENCES = 16
MAX_TTS_TEXT_CHARS = 8000  # matches bounded playback text fields
PCM_LEAD_SECONDS = 0.24  # publish ahead of the sample clock to absorb network jitter
FAILED_STOP_TIMEOUT_SECONDS = 1.0
RECEIPT_STALE_SECONDS = 3.0  # a speaker reports progress every ~100 ms while playing
FAILED_STOP_RETRY_SECONDS = .1
# After the stop packet is published, worker and socket teardown must not hold
# the Realtime receive loop. A mixed TaskGroup can ignore cancellation.
CANCEL_CLEANUP_SECONDS = 1.0
# Alibaba recommends reusing one TTS WebSocket across sequential tasks; the
# handshake otherwise sits on every reply's first-audio path. The provider does
# not document an idle timeout, so an idle connection is only trusted briefly
# and must prove itself (task-started) before any text depends on it.
WARM_SOCKET_MAX_IDLE_SECONDS = 25.0
WARM_CONFIRM_SECONDS = 1.5


def _tts_failure(exc):
    """Expose the actual TaskGroup failure, not its useless outer wrapper."""
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(_tts_failure(child) for child in exc.exceptions[:3])[:220]
    detail = str(exc)
    detail = re.sub(r"(?i)\bBearer\s+\S+|\bsk-[A-Za-z0-9_-]+", "[redacted]", detail)
    detail = re.sub(r"https?://\S+", "[endpoint]", detail)
    return f"{type(exc).__name__}: {detail[:160]}"[:220]


async def _tts_stage(name, awaitable):
    try:
        return await awaitable
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise RuntimeError(f"{name}: {_tts_failure(exc)}") from exc
_LATER_SEMAPHORE = asyncio.Semaphore(2)  # global later-task cap; never gates first PCM
_ONE_SHOT_TAGS = frozenset({"serious", "mischievously", "sarcastic", "curious",
                            "amazed", "excited", "empathetic", "sad", "whispers"})
_TAG_INSTRUCTIONS = {
    "serious": "语气认真清晰，关键提醒适当加重，句尾稳住。",
    "mischievously": "像熟人善意打趣，语气机灵轻快，反问略微上扬。",
    "sarcastic": "克制地机灵打趣，重音点到为止，不要刻薄。",
    "curious": "带着自然好奇提问，疑问尾音适度上扬。",
    "amazed": "短暂表现惊喜，语气明亮但不过度尖叫。",
    "excited": "轻快高兴，语调有起伏但不要持续大喊。",
    "empathetic": "真诚安慰，尾音柔和，适当放慢节奏。",
    "sad": "声音收敛轻柔，避免刻意哭腔。",
    "whispers": "像贴近耳边轻声说话，咬字保持清晰。",
}


def sentence_style(base, controls):
    """Per-sentence style cannot override a turn-frozen language or explicit pace."""
    if base is None:
        base = SpeechStyle()
    if base.source == "explicit_user":
        return base, None  # explicit style overrides LLM director fields
    tag = controls.get("latest_tag")
    tag = tag if tag in _ONE_SHOT_TAGS else None
    raw = controls.get("sentence_instruction") or controls.get("instruction")
    accepted = (isinstance(raw, str) and raw not in {"无", "默认", "无需"}
                and 0 < len(raw) <= 120
                and not re.search(r"(?:重音|强调).{0,12}[“\"][^”\"]{1,24}[”\"]", raw)
                and not any(item in raw for item in ("执行", "接口", "系统", "命令", "URL")))
    extra = raw if accepted else _TAG_INSTRUCTIONS.get(tag, "")
    if base.emotion == "serious" and tag in {"mischievously", "sarcastic", "excited"}:
        tag, extra = None, ""
    instruction = (base.instruction + (" " if base.instruction and extra else "") + extra)[:512]
    pace = controls.get("pace")
    rate = base.rate
    if base.source != "explicit_user" and pace in {"slower", "faster"}:
        rate = min(1.12, max(.90, base.rate + (-.04 if pace == "slower" else .04)))
    return replace(base, instruction=instruction, rate=rate,
                   source="llm_sentence" if accepted else "llm_tag" if extra else base.source), tag


class SpeechOutput:
    """One bounded text queue and one synthesis/publisher owner per robot."""

    def __init__(self, robot, publisher, *, url, key, voice, socket_factory=QwenTtsSocket,
                 first_chars=10, min_chars=None, max_chars=48, first_wait_ms=360, event=None,
                 voice_publisher=None, style_provider=None, voice_provider=None,
                 mixed_enabled=False):
        min_chars = first_chars if min_chars is None else min_chars
        if any(type(value) is not int or value <= 0
               for value in (first_chars, min_chars, max_chars)):
            raise ValueError("TTS chars thresholds must be positive integers")
        if first_chars > max_chars or min_chars > max_chars:
            raise ValueError("TTS first/min chars must not exceed max chars")
        self.robot = robot
        self.publisher = publisher
        self.voice_publisher = voice_publisher  # control/status; PCM stays reb_tts
        self.url = url
        self.key = key
        self.voice = voice
        self.voice_provider = voice_provider
        self.locked_voice = None
        self.socket_factory = socket_factory
        self.style_provider = style_provider
        self.mixed_enabled = mixed_enabled
        self.speech_style = None
        self.run_to_first_pcm_ms = None
        self.first_chars = first_chars
        self.min_chars = min_chars
        self.max_chars = max_chars
        self.first_wait_ms = first_wait_ms
        self.event = event or (lambda *_args, **_kw: None)
        self.queue = asyncio.Queue(maxsize=8)
        self.worker = None
        self.flush_timer = None
        self.socket = None
        self.turn_id = None
        self.identity = None
        self.pending = ""
        self.segment = 0
        self.seq = 0
        self.samples = 0
        self.inflight_end = 0
        self.frames = 0
        self.first_audio_ms = None
        self.first_publish_ms = None
        self.error = None
        self.cancelled = False
        self.last_stop_reason = None
        self.finished = False
        self.published_text = ""
        self.submitted_text = ""
        self.generated_text = ""  # actual Realtime text before PCM/alignment
        self.last_published_segment = None
        self.started_at = None
        self.next_pcm_at = None
        self.failed_turn = None
        self.received_chars = 0
        self.failure_cleanup = None
        self._cancel_inflight = None

        self.stream_epoch = 0
        self.playback_id = self.playback_source = None
        self.receipt_tracks = {}  # independent browser_test and speaker cursors
        self.played_samples = self.receipt_seq = 0
        self.playback_state = self.playback_evidence = "unknown"
        self.stop_requested_at = self.stop_published_at = self.stop_confirmed_ms = None
        self.started_reported_ms = None
        self.pending_stop = None
        self.pending_stop_identity = None
        self.pending_stop_source = None
        self.stopped_conversation_id = None
        self.last_stop_evidence = "unknown"
        self.synthesized_complete = False
        self.local_output_completed_at = None  # monotonic; NOT a speaker receipt
        self._last_receipt_t = None  # monotonic, current turn only
        self.local_output_stopped_at = None  # successful STOP publication, NOT a receipt
        self.viseme_events = 0
        self.viseme_seen = set()
        self.alignment_points = {}  # original char index -> validated current-epoch sample
        self.viseme_state = "not_connected"
        self.control_task = None
        self._control_next = None  # latest unsent hold/resume; at most one waiting
        self.control_seq = 0
        self.pending_controls = {}
        self.control_state = "unknown"
        self.voice_tasks = set()
        self._warm_socket = None  # opened, no task in flight
        self._warm_origin = None  # "prewarmed" or "reused"
        self._warm_at = None
        self._warm_task = None
        self._close_tasks = set()
        self._closing = False
        self.connect_stats = {"fresh": 0, "prewarmed": 0, "reused": 0,
                              "stale_dropped": 0, "reuse_failed": 0, "prewarm_failed": 0}
        self.last_connect = None

        self._mixed_reset()

    def _mixed_reset(self):
        self._mixed_first_text = ""
        self._mixed_first_closed = asyncio.Event()
        self._mixed_first_sent = asyncio.Event()
        self._mixed_ready = asyncio.Event()
        self._mixed_later = deque()
        self._mixed_sentence = ""
        self._mixed_later_text = ""
        self._mixed_pos = 0
        self._mixed_controls = deque()
        self._mixed_style = {}
        self._mixed_sockets = set()
        self._mixed_segment_index = 1
        self._mixed_sentences_submitted = 0
        self._mixed_last_run = None
        self._mixed_seen_segments = set()
        self._mixed_text_events = set()
        self._mixed_sentences_queued = 0
        self._mixed_overflow = ""

    def _voice_signal(self, packet):
        """Best-effort signaling must never await network I/O in a cancel path."""
        if self.voice_publisher is None:
            return
        labels = {
            "tts.playback.hold": "请求暂停播放",
            "tts.playback.resume": "请求恢复播放",
            "tts.playback.stop": "请求停止播放",
        }
        packet = {"type": packet["type"],
                  "description": labels.get(packet["type"], "播放控制信令"),
                  **{key: value for key, value in packet.items() if key != "type"}}
        if len(self.voice_tasks) >= 16:
            self._emit("tts.voice_signal.error", reason=packet["type"],
                       error="voice_queue_full")
            return

        async def send():
            try:
                await self.voice_publisher.publish(packet)
            except Exception as exc:
                self._emit("tts.voice_signal.error", reason=packet["type"],
                           error=type(exc).__name__)

        task = asyncio.create_task(send())
        self.voice_tasks.add(task)
        task.add_done_callback(self.voice_tasks.discard)

    def snapshot(self):
        return {
            "state": "cancelled" if self.cancelled else "error" if self.error else
                     "active" if self.worker and not self.worker.done() else "idle",
            "voice": self.locked_voice or self.voice, "turn_id": self.turn_id,
            "queue_depth": self.queue.qsize(),
            "pcm_frames_published": self.frames, "pcm_samples_published": self.samples,
            "first_audio_ms": self.first_audio_ms, "first_publish_ms": self.first_publish_ms,
            "stream_epoch": self.stream_epoch, "playback_state": self.playback_state,
            "playback_source": self.playback_source, "playback_id": self.playback_id,
            "played_sample_count": self.played_samples,
            "playback_evidence": self.playback_evidence, "viseme": self.viseme_state,
            "viseme_events": self.viseme_events,
            "stop_requested_ms": self.stop_requested_at,
            "stop_published_ms": self.stop_published_at,
            "started_reported_ms": self.started_reported_ms,
            "stop_confirmed_ms": self.stop_confirmed_ms,
            "stopped_conversation_id": self.stopped_conversation_id,
            "last_stop_evidence": self.last_stop_evidence,
            "speech_style": asdict(self.speech_style) if self.speech_style else None,
            "run_to_first_pcm_ms": self.run_to_first_pcm_ms,
            "mixed_enabled": self.mixed_enabled,
            "mixed_pending_sentences": len(self._mixed_later),
            "mixed_sentences_submitted": self._mixed_sentences_submitted,
            "mixed_last_run": dict(self._mixed_last_run) if self._mixed_last_run else None,
            "error": self.error,
            "last_stop_reason": self.last_stop_reason,
            "failure_stop_published": self.local_output_stopped_at is not None,
            "playback_control_state": self.control_state,
            "playback_control_pending": len(self.pending_controls),
            "connection": {**self.connect_stats, "warm_ready": self._warm_socket is not None,
                           "last": dict(self.last_connect) if self.last_connect else None},
        }

    def prewarm(self):
        """Open the next provider connection off the text path; starts no task."""
        if self._closing:
            return
        if self._warm_task is not None and not self._warm_task.done():
            return
        if (self._warm_socket is not None
                and time.monotonic() - self._warm_at < WARM_SOCKET_MAX_IDLE_SECONDS):
            return
        self._warm_task = asyncio.create_task(
            self._open_warm(), name=f"tts-prewarm:{self.robot.config.robot_id}")

    async def _open_warm(self):
        stale, self._warm_socket = self._warm_socket, None
        if stale is not None:
            self.connect_stats["stale_dropped"] += 1
            await self._close_idle(stale)
        socket = self.socket_factory(self.url, self.key)
        started = time.perf_counter()
        try:
            async with asyncio.timeout(8):
                await socket.open()
        except asyncio.CancelledError:
            self._close_detached(socket)
            raise
        except Exception as exc:
            await self._close_idle(socket)
            self.connect_stats["prewarm_failed"] += 1
            logger.info("tts_prewarm_failed robot=%s error=%s",
                        self.robot.config.robot_id, _tts_failure(exc))
            return
        if self._closing:
            await self._close_idle(socket)
            return
        connect_ms = round((time.perf_counter() - started) * 1000, 1)
        logger.info("tts_prewarm robot=%s connect_ms=%.1f", self.robot.config.robot_id, connect_ms)
        await self._park(socket, "prewarmed")

    async def _park(self, socket, origin):
        old, self._warm_socket = self._warm_socket, socket
        self._warm_origin, self._warm_at = origin, time.monotonic()
        if old is not None and old is not socket:
            await self._close_idle(old)

    def _close_detached(self, socket):
        task = asyncio.create_task(socket.close(), name="tts-idle-close")
        self._close_tasks.add(task)  # strong reference until the abort finishes
        task.add_done_callback(self._close_tasks.discard)

    async def _close_idle(self, socket):
        """An idle provider close must not hold a reply or shutdown path."""
        task = asyncio.create_task(socket.close(), name="tts-idle-close")
        await self._abandon_tasks((task,), time.monotonic() + CANCEL_CLEANUP_SECONDS)

    async def _acquire_socket(self, turn_id, identity):
        """Return (socket, origin, connect_ms); origin is prewarmed/reused/fresh."""
        task = self._warm_task
        if task is not None and not task.done():
            await asyncio.wait({task}, timeout=8)  # shared opener; never cancel it here
        socket, self._warm_socket = self._warm_socket, None
        if socket is not None:
            idle = time.monotonic() - self._warm_at
            if idle <= WARM_SOCKET_MAX_IDLE_SECONDS:
                return socket, self._warm_origin, 0.0
            self.connect_stats["stale_dropped"] += 1
            await self._close_idle(socket)
        started = time.perf_counter()
        socket = self.socket_factory(self.url, self.key)
        self.socket = socket
        # Only retry connection establishment: no submitted text/PCM can duplicate.
        for attempt in range(2):
            try:
                async with asyncio.timeout(8):
                    await socket.open()
                break
            except (OSError, TimeoutError) as exc:
                await socket.close()
                if attempt:
                    raise RuntimeError(f"provider_connect: {_tts_failure(exc)}") from exc
                self._emit("tts.connect_retry", error=_tts_failure(exc), attempt=1)
                await asyncio.sleep(.25)
                if not self._current(turn_id, identity):
                    return None, "fresh", None
                socket = self.socket_factory(self.url, self.key)
                self.socket = socket
        return socket, "fresh", round((time.perf_counter() - started) * 1000, 1)

    def _emit(self, kind, **data):
        self.event(kind, **{**(self.identity or self.robot.session_state()),
                            "turn_id": self.turn_id, **data})

    async def _begin(self, turn_id, identity):
        if turn_id == self.turn_id:
            return
        if self.worker or self.turn_id:
            await self.cancel("next_turn")
        self.queue = asyncio.Queue(maxsize=8)
        self.stream_epoch += 1
        self.turn_id = turn_id
        self.identity = dict(identity)
        self.speech_style = None
        self.run_to_first_pcm_ms = None
        self.cancelled = False
        self.last_stop_reason = None
        self.finished = False
        self.pending = ""
        self.segment = self.seq = self.samples = self.frames = 0
        self.inflight_end = 0
        self.error = self.first_audio_ms = self.first_publish_ms = None
        self.published_text = ""
        self.submitted_text = ""
        self.locked_voice = None
        self.generated_text = ""
        self.last_published_segment = None
        self.started_at = time.perf_counter()
        self.next_pcm_at = None
        self.failed_turn = None
        self.received_chars = 0
        self.playback_id = self.playback_source = None
        self.receipt_tracks.clear()
        self.played_samples = self.receipt_seq = self.viseme_events = 0
        self.playback_state = self.playback_evidence = "unknown"
        self.stop_requested_at = self.stop_published_at = self.stop_confirmed_ms = None
        self.stopped_conversation_id = None
        self.last_stop_evidence = "unknown"
        self.synthesized_complete = False
        self.local_output_completed_at = None
        self.local_output_stopped_at = None
        self._last_receipt_t = None
        self.viseme_state = "not_connected"
        self.viseme_seen.clear()
        self.alignment_points.clear()
        self.control_seq = 0
        self._control_next = None
        self.pending_controls.clear()
        self.control_state = "unknown"
        self._mixed_reset()
        self.worker = asyncio.create_task(self._run(turn_id, dict(identity)), name=f"tts:{self.robot.config.robot_id}")

    async def expression_events(self, turn_id, identity, events):
        if not self.mixed_enabled:
            return
        await self._begin(turn_id, identity)
        if self._current(turn_id, identity):
            self._mixed_controls.extend(events)

    def _mixed_feed(self, delta):
        """First phrase enters the existing duplex queue without waiting for metadata."""
        for char in delta:
            while self._mixed_controls and self._mixed_controls[0][0] <= self._mixed_pos:
                _, field, value = self._mixed_controls.popleft()
                self._mixed_style[field] = value
            if not self._mixed_first_closed.is_set():
                self.pending += char
                self._mixed_first_text += char
                if char in MIXED_SENTENCE_END and not (char == "." and
                    len(self._mixed_first_text) > 1 and self._mixed_first_text[-2].isdigit()):
                    self._drain_pending(force=True)
                    self._mixed_first_closed.set()
                    if not self.queue.full():
                        self.queue.put_nowait("")
            else:
                self._mixed_sentence += char
                if char in MIXED_SENTENCE_END and not (char == "." and
                    len(self._mixed_sentence) > 1 and self._mixed_sentence[-2].isdigit()):
                    self._mixed_commit_sentence()
            self._mixed_pos += 1
        if not self._mixed_first_closed.is_set():
            self._drain_pending()

    def _mixed_commit_sentence(self):
        if self._mixed_sentence.strip():
            if self._mixed_sentences_queued < MAX_MIXED_SENTENCES:
                self._mixed_later.append((self._mixed_sentence, dict(self._mixed_style)))
                self._mixed_sentences_queued += 1
                self._mixed_ready.set()
            else:
                # Coalesce overflow; preserve every spoken character without
                # launching unbounded provider tasks on punctuation-heavy text.
                self._mixed_overflow += self._mixed_sentence
        self._mixed_sentence = ""
        self._mixed_style.pop("sentence_instruction", None)
        self._mixed_style.pop("latest_tag", None)

    def _current(self, turn_id, identity):
        return (
            not self.cancelled and self.turn_id == turn_id
            and self.identity == identity and self.robot.session_state() == identity
        )

    def _enqueue(self, segment):
        if not segment.strip():
            return True
        if self.queue.full():
            return False  # retain the text in pending, never silently drop it
        self.queue.put_nowait(segment)
        self.segment += 1
        self._emit("tts.segment_queued", chars=len(segment))
        return True

    def _drain_pending(self, *, force=False):
        """Nonblocking coalescing: slow TTS must not stall Realtime reception."""
        while self.pending and not self.queue.full():
            head = self.pending[:self.max_chars]
            sentence_min = self.first_chars if self.segment == 0 else self.min_chars
            cut = next((i + 1 for i, char in enumerate(head)
                        if char in SENTENCE_END and i + 1 >= sentence_min), None)
            if cut is None and len(self.pending) >= 24:
                cut = next((i + 1 for i, char in enumerate(head)
                            if char in CLAUSE_END and i + 1 >= 12), None)
            if cut is None and len(self.pending) >= self.max_chars:
                cut = self.max_chars
            if (cut is None and self.segment == 0 and len(self.pending) >= self.first_chars
                    and not any(char in SENTENCE_END or char in CLAUSE_END
                                for char in head)):
                # Don't pay the fallback timer when the model already supplied
                # a usable opening phrase without punctuation.
                cut = self.first_chars
            if cut is None and force:
                cut = len(self.pending)
            if cut is None:
                return
            segment = self.pending[:cut]
            if not segment.strip():
                first_text = next((i for i, char in enumerate(self.pending)
                                   if not char.isspace()), None)
                if first_text is None:
                    if force and self.finished:
                        self.pending = ""  # no spoken word exists to anchor
                    return
                cut = min(len(self.pending), first_text + self.max_chars)
                segment = self.pending[:cut]
            if not self._enqueue(segment):
                return
            self.pending = self.pending[cut:]

    async def feed(self, turn_id, identity, delta):
        if not delta or turn_id == self.failed_turn:
            return
        await self._begin(turn_id, identity)
        if self.finished or not self._current(turn_id, identity):
            return
        if self.received_chars + len(delta) > MAX_TTS_TEXT_CHARS:
            self.failed_turn = turn_id
            self.error = f"tts_text_limit: exceeded {MAX_TTS_TEXT_CHARS} chars"
            self.cancelled = True
            self.pending = ""
            if self.worker:
                self.worker.cancel()
            if self.flush_timer:
                self.flush_timer.cancel()
            while not self.queue.empty():
                self.queue.get_nowait()
                self.queue.task_done()
            self._emit("tts.error", error=self.error, reason="tts_text_limit")
            # Immediate admission failure is local. Room stop/transport cleanup
            # must not hold Realtime's receive loop behind network publication.
            self.failure_cleanup = asyncio.create_task(
                self.cancel("tts_text_limit"), name=f"tts-limit:{turn_id}",
            )
            return
        self.received_chars += len(delta)
        self.generated_text += delta
        if self.mixed_enabled:
            self._mixed_feed(delta)
            if self.pending and not self.flush_timer:
                self.flush_timer = asyncio.create_task(self._first_flush(turn_id, identity))
            return
        self.pending += delta
        self._drain_pending()
        if self.pending and not self.flush_timer:
            self.flush_timer = asyncio.create_task(self._first_flush(turn_id, identity))

    async def _first_flush(self, turn_id, identity):
        try:
            await asyncio.sleep(self.first_wait_ms / 1000)
            if self._current(turn_id, identity) and self.pending and (
                len(self.pending.strip()) >= 2 or self.pending[-1] in SENTENCE_END
            ):
                # The timeout is an upper bound for a short, stalled phrase,
                # not another minimum-length gate. Otherwise "你好" can sit
                # unspoken until a later LLM delta or response.done.
                self._drain_pending(force=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = str(exc)
            self._emit("tts.error", error=self.error)
        finally:
            self.flush_timer = None

    def output_active(self):
        """Still synthesizing or still audible. The last turn_id stays set after a
        normal finish, so it alone must not count: events waiting for a quiet
        robot (greetings, face results, sensors) were blocked until they expired.
        """
        if self.turn_id is None:
            return False
        if self.worker is not None and not self.worker.done():
            return True
        if self.playback_state in {"drained", "stopped"}:
            return False
        now = time.monotonic()
        if self._last_receipt_t is not None:
            # Playing per the speaker, unless it stopped reporting (unplugged).
            return now - self._last_receipt_t < RECEIPT_STALE_SECONDS
        done = self.local_output_completed_at
        # No receipt at all: the audio lasts about as long as what was synthesized.
        return done is not None and now - done < self.samples / 24000 + 1.0

    async def finish(self, turn_id):
        if turn_id != self.turn_id or self.finished:
            return
        self.finished = True
        if self.mixed_enabled:
            while self._mixed_controls and self._mixed_controls[0][0] <= self._mixed_pos:
                _, field, value = self._mixed_controls.popleft()
                self._mixed_style[field] = value
            self._mixed_first_closed.set()
            self._mixed_commit_sentence()
            if self._mixed_overflow.strip():
                self._mixed_later.append((self._mixed_overflow, dict(self._mixed_style)))
                self._mixed_overflow = ""
            self._mixed_ready.set()
        if self.flush_timer:
            self.flush_timer.cancel()
            self.flush_timer = None
        self._drain_pending(force=True)
        if self.worker and not self.worker.done() and self.queue.empty():
            try:
                self.queue.put_nowait("")
            except asyncio.QueueFull:
                pass

    async def cancel(self, reason="cancel"):
        current = asyncio.current_task()
        # Let scheduled failure cleanup acquire cancellation ownership first.
        # Otherwise close() can own the lock, await cleanup, and make cleanup
        # await close() in turn (a permanent cycle).
        cleanup = self.failure_cleanup
        if cleanup is not None and cleanup is not current and not cleanup.done():
            await asyncio.gather(cleanup, return_exceptions=True)
            return
        if (self._cancel_inflight is not None and self._cancel_inflight is not current
                and not self._cancel_inflight.done()):
            await asyncio.gather(self._cancel_inflight, return_exceptions=True)
            return
        self._cancel_inflight = current
        was_active = self.turn_id is not None
        old_cleanup = (self.failure_cleanup if self.failure_cleanup is not asyncio.current_task()
                       else None)
        self.failure_cleanup = None
        self.cancelled = True
        control_task, self.control_task = self.control_task, None
        if control_task:
            control_task.cancel()
        self._control_next = None
        self.pending_controls.clear()
        self.control_state = "unknown"
        if was_active:
            self.last_stop_reason = reason
        if was_active:
            self.stop_requested_at = time.time_ns() // 1_000_000
            self.playback_state = "stop_requested"
            self.pending_stop = (
                self.turn_id, self.stream_epoch,
                max(self.samples, self.inflight_end), self.played_samples,
            )
            self.pending_stop_identity = self.identity
            self.pending_stop_source = (self.playback_source, self.playback_id)
            self._emit("tts.stop_requested", reason=reason,
                       stop_requested_ms=self.stop_requested_at)
        flush_timer, self.flush_timer = self.flush_timer, None
        if flush_timer:
            flush_timer.cancel()
        worker = self.worker
        if worker and not worker.done():
            worker.cancel()
        if was_active:
            # The established Room contract only proves publication of clear,
            # not that the device has actually stopped or which samples played.
            stop_cid, stop_epoch = self.pending_stop[:2]
            self._voice_signal({
                "type": "tts.playback.stop", "ts": time.time_ns() // 1_000_000,
                "data": {"protocol_version": 1,
                         "conversation_id": stop_cid,
                         "stream_epoch": stop_epoch,
                         "reason": reason, "sender": "brain",
                         "trigger_source": "qwen_realtime" if reason in {
                             "valid_smart_turn", "meaningful_ambient",
                         } else "runtime"},
            })
            try:
                async with asyncio.timeout(2 if reason == "tts_text_limit" else 1):
                    await self.publisher.publish(
                        self._packet(b"", "", final=True, stop=True, reason=reason),
                    )
                self.stop_published_at = time.time_ns() // 1_000_000
                self._emit("tts.stop_published", reason=reason, device_stopped="unknown",
                           stop_published_ms=self.stop_published_at)
            except Exception as exc:
                self.error = f"cannot publish stop: {type(exc).__name__}"
                self._emit("tts.error", error=self.error)
        # All potentially slow cleanup follows the first bounded stop send.
        # Keep it serialized before a replacement turn can reuse these fields.
        # A provider close or mixed TaskGroup can ignore cancellation. Waiting
        # forever holds the Realtime receive loop on the open speech flag and
        # drops the interrupting turn.
        deadline = time.monotonic() + CANCEL_CLEANUP_SECONDS
        await self._abandon_tasks(
            (task for task in (old_cleanup, control_task, flush_timer, worker) if task),
            deadline,
        )
        closers = [asyncio.create_task(socket.close())
                   for socket in tuple(self._mixed_sockets)]
        if self.socket is not None:
            closers.append(asyncio.create_task(self.socket.close()))
        await self._abandon_tasks(closers, deadline)
        self._mixed_sockets.clear()
        self.socket = None
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()
        self.worker = None
        self.turn_id = None
        self.identity = None
        self.speech_style = None
        self.run_to_first_pcm_ms = None
        self.pending = ""
        self._mixed_reset()
        if self._cancel_inflight is current:
            self._cancel_inflight = None
        if was_active and reason not in {"runtime_stop", "realtime_disconnected"}:
            self.prewarm()  # the interrupting turn's reply should not pay a handshake

    async def _abandon_tasks(self, tasks, deadline):
        pending = [task for task in tasks if task is not None and not task.done()]
        if not pending:
            return
        _done, leftover = await asyncio.wait(
            pending, timeout=max(0.0, deadline - time.monotonic()),
        )
        if not leftover:
            return
        logger.warning(
            "TTS cancel cleanup still pending robot=%s count=%s",
            self.robot.config.robot_id, len(leftover),
        )
        for task in leftover:
            try:
                task.cancel()
            except RecursionError:
                logger.warning(
                    "TTS cancel cleanup recursion robot=%s", self.robot.config.robot_id,
                )

    async def close(self):
        self._closing = True
        await self.cancel("runtime_stop")
        warm_task, self._warm_task = self._warm_task, None
        if warm_task is not None:
            warm_task.cancel()
            await self._abandon_tasks((warm_task,), time.monotonic() + CANCEL_CLEANUP_SECONDS)
        warm, self._warm_socket = self._warm_socket, None
        if warm is not None:
            await self._close_idle(warm)
        await self.publisher.close()
        # Give the queued stop signal a bounded chance to reach reb_voice
        # before closing its socket. Shutdown must not wait for a dead Room.
        try:
            await asyncio.wait_for(asyncio.gather(*self.voice_tasks,
                                                   return_exceptions=True), .5)
        except asyncio.TimeoutError:
            pass
        for task in tuple(self.voice_tasks):
            task.cancel()
        await asyncio.gather(*self.voice_tasks, return_exceptions=True)
        if self.voice_publisher is not None:
            await self.voice_publisher.close()

    def request_playback_control(self, kind, *, reason):
        """Coalesce stale commands behind one bounded per-robot publisher task."""
        if kind not in {"hold", "resume"}:
            raise ValueError("unsupported playback command")
        if self.cancelled or not self.turn_id or not self.identity or self.turn_id == self.failed_turn:
            return False
        self.control_seq += 1
        sequence = self.control_seq
        epoch, turn_id, identity = self.stream_epoch, self.turn_id, dict(self.identity)
        self.control_state = f"{kind}_requested"
        self._control_next = (kind, reason, sequence, epoch, turn_id, identity)
        if self.control_task is None or self.control_task.done():
            self.control_task = asyncio.create_task(
                self._publish_playback_controls(), name=f"playback:{self.robot.config.robot_id}",
            )
        return True

    async def _publish_playback_controls(self):
        while self._control_next is not None and not self.cancelled:
            kind, reason, sequence, epoch, turn_id, identity = self._control_next
            self._control_next = None
            if not self._current(turn_id, identity) or self.stream_epoch != epoch:
                continue
            data = {
                "protocol_version": 1, "conversation_id": turn_id,
                "stream_epoch": epoch, "command_seq": sequence, "reason": reason,
                "sender": "brain",
                "trigger_source": "qwen_realtime" if reason in {
                    "speech_started", "turn_invalid", "ambient_audio",
                } else "runtime",
                "fade_ms": 40 if kind == "hold" else 120,
            }
            self.pending_controls[sequence] = kind
            packet = {"type": f"tts.playback.{kind}",
                      "ts": time.time_ns() // 1_000_000, "data": data}
            self._voice_signal(packet)
            try:
                async with asyncio.timeout(2):
                    await self.publisher.publish(packet)
            except Exception as exc:
                self.pending_controls.pop(sequence, None)
                self.control_state = "control_error"
                self._emit("tts.playback.control_error", command=kind,
                           error=type(exc).__name__)
                continue
            if self._current(turn_id, identity) and self.stream_epoch == epoch:
                if len(self.pending_controls) > 32:
                    self.pending_controls.pop(next(iter(self.pending_controls)))
                self._emit(f"tts.playback.{kind}_published", conversation_id=turn_id,
                           stream_epoch=epoch, command_seq=sequence, device_applied="unknown")

    def _packet(self, pcm, text, *, final=False, stop=False, segment_id=None,
                reason=None, stream_final=False):
        self.seq += 1
        # Cancellation may interrupt await ws.send() after transport writes a
        # frame but before publish returns. Never cap a stop below that frame.
        start_sample = max(self.samples, self.inflight_end) if stop else self.samples
        return {
            "type": "tts", "ts": time.time_ns() // 1_000_000,
            "data": {
                "audio": base64.b64encode(pcm).decode("ascii"), "format": "pcm_s16le",
                "sample_rate": 24000, "channels": 1, "text": text, "seq": self.seq,
                "final": final, "turn_id": segment_id or self.turn_id,
                "conversation_id": self.turn_id, "source": "robot_main_brain",
                "playback_protocol": "sample_cursor_v1", "protocol_version": 1,
                "stream_epoch": self.stream_epoch, "start_sample": start_sample,
                "end_sample": start_sample + len(pcm) // 2,
                "audio_total_samples": start_sample + len(pcm) // 2,
                **({"stream_final": True} if stream_final else {}),
                **({"stop": True} if stop else {}),
                **({"reason": reason} if reason else {}),
            },
        }

    async def _run(self, turn_id, identity):
        try:
            if self.mixed_enabled:
                await self._synthesize_mixed(turn_id, identity)
            else:
                await self._synthesize_stream(turn_id, identity)
            if self._current(turn_id, identity):
                await self.publisher.publish(self._packet(b"", self.published_text,
                                                          final=True, stream_final=True))
                self.synthesized_complete = True
                self.local_output_completed_at = time.monotonic()
                self._emit("tts.stream_finished", device_played="unknown")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = _tts_failure(exc)
            logger.warning("TTS robot=%s failed: %s", self.robot.config.robot_id, self.error)
            failure_stage = re.search(r"\b(provider_connect|text_submit|provider_receive|room_publish)\b", self.error)
            self._emit("tts.error", error=self.error,
                       error_stage=failure_stage.group(1) if failure_stage else "tts_worker")
            self.failed_turn = turn_id
            self.finished = True
            self.pending = ""
            while not self.queue.empty():
                self.queue.get_nowait()
                self.queue.task_done()
            await self._stop_failed_output(turn_id, identity)

    async def _stop_failed_output(self, turn_id, identity):
        """Stop failed PCM boundedly; keep receipt identity and failure evidence intact."""
        for task in (self.flush_timer, self.control_task):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.flush_timer = self.control_task = None
        self._control_next = None
        self.pending_controls.clear()
        if not self._current(turn_id, identity):
            return
        self.last_stop_reason = "tts_error"
        self.stop_requested_at = time.time_ns() // 1_000_000
        self._voice_signal({
            "type": "tts.playback.stop", "ts": self.stop_requested_at,
            "data": {"protocol_version": 1, "conversation_id": turn_id,
                     "stream_epoch": self.stream_epoch, "reason": "tts_error",
                     "sender": "brain", "trigger_source": "runtime"},
        })
        for attempt in range(2):
            try:
                async with asyncio.timeout(FAILED_STOP_TIMEOUT_SECONDS):
                    await self.publisher.publish(
                        self._packet(b"", "", final=True, stop=True, reason="tts_error"),
                    )
            except Exception as exc:
                self._emit("tts.stop_retry" if attempt == 0 else "tts.recovery_failed",
                           error=_tts_failure(exc), reason="tts_error", attempt=attempt + 1,
                           device_stopped="unknown")
                if attempt == 0:
                    await asyncio.sleep(FAILED_STOP_RETRY_SECONDS)
            else:
                self.stop_published_at = time.time_ns() // 1_000_000
                self.local_output_stopped_at = time.monotonic()
                self._emit("tts.stop_published", reason="tts_error", device_stopped="unknown",
                           stop_published_ms=self.stop_published_at)
                self._emit("tts.output_stopped", device_stopped="unknown",
                           recovery="local_wait_grace")
                return

    async def _synthesize_mixed(self, turn_id, identity):
        """Duplex opening, then sequential one-text-per-task realtime synthesis.

        The raw Qwen WS only supports 'duplex' wire mode. Later requests are
        one-shot submissions, NOT mislabelled SDK simplex; no second LLM.
        """
        self._emit("tts.mixed.started", stream_epoch=self.stream_epoch)
        async with asyncio.TaskGroup() as tasks:
            first = tasks.create_task(
                self._synthesize_stream(turn_id, identity, first_only=True),
                name=f"tts-first:{turn_id}",
            )
            tasks.create_task(self._mixed_later_loop(turn_id, identity, first),
                              name=f"tts-later:{turn_id}")

    async def _mixed_later_loop(self, turn_id, identity, first):
        while self._current(turn_id, identity):
            if not self._mixed_later:
                if self.finished:
                    return
                self._mixed_ready.clear()
                await self._mixed_ready.wait()
                continue
            sentence, controls = self._mixed_later.popleft()
            await self._mixed_first_sent.wait()
            if not self._current(turn_id, identity):
                return
            self._mixed_segment_index += 1
            segment_id = f"{turn_id}-{self._mixed_segment_index}"
            prefix = self._mixed_first_text + self._mixed_later_text + sentence
            start_char = len(self.submitted_text)
            self.submitted_text += sentence
            style, tag = sentence_style(self.speech_style, controls)
            published = 0
            for attempt in range(2):
                use_style = style if attempt == 0 else self.speech_style
                use_tag = tag if attempt == 0 else None
                frames = asyncio.Queue(maxsize=64)
                producer = asyncio.create_task(
                    self._mixed_one_shot(turn_id, identity, segment_id, sentence,
                                         use_style, use_tag, frames, start_char),
                    name=f"tts-one-shot:{segment_id}:{attempt}",
                )
                try:
                    # May prepare up to 1.28s of PCM ahead, but the Room's sole
                    # output cursor cannot advance until the first task drains.
                    await first
                    started = time.perf_counter()
                    while self._current(turn_id, identity):
                        item = await frames.get()
                        if item is None:
                            break
                        if isinstance(item, Exception):
                            raise item
                        await self._publish_pcm(item, prefix, segment_id, turn_id,
                                                identity, started)
                        published += 1
                    await producer
                    if not self._current(turn_id, identity):
                        return
                    if published == 0:
                        raise RuntimeError("one-shot TTS returned no audio")
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if published or attempt:
                        raise
                    self._emit("tts.sentence_fallback", segment_id=segment_id,
                               error=type(exc).__name__, fallback="default_duplex")
                finally:
                    if not producer.done():
                        producer.cancel()
                    await asyncio.gather(producer, return_exceptions=True)
            if self._current(turn_id, identity):
                self._mixed_later_text += sentence
                await self.publisher.publish(self._packet(b"", prefix, final=True,
                                                          segment_id=segment_id))
                self._emit("tts.segment_done", segment_id=segment_id, samples=self.samples)

    async def _mixed_one_shot(self, turn_id, identity, segment_id, text, style, tag, frames,
                              start_char):
        """Single text submission on a separate bounded async WS task."""
        socket = None
        try:
            async with _LATER_SEMAPHORE:
                if not self._current(turn_id, identity):
                    return
                socket = self.socket_factory(self.url, self.key)
                self._mixed_sockets.add(socket)
                async with asyncio.timeout(6):
                    await socket.open()
                task = task_id()
                run = command("run-task", task, voice=self.locked_voice or self.voice,
                              style=style, one_shot=True)
                await socket.send(run)
                self._emit("tts.run_task", task_id=task, segment_id=segment_id,
                           streaming=run["header"]["streaming"],
                           parameters={k: v for k, v in run["payload"]["parameters"].items()
                                       if k != "voice"}, speech_style=asdict(style),
                           expression_source=style.source)
                submitted = False
                tail = bytearray()
                while self._current(turn_id, identity):
                    item = await asyncio.wait_for(socket.recv(), timeout=15)
                    if isinstance(item, bytes):
                        tail.extend(item)
                        while len(tail) >= FRAME_BYTES:
                            frame = bytes(tail[:FRAME_BYTES])
                            del tail[:FRAME_BYTES]
                            await frames.put(frame)
                    else:
                        header = item.get("header") or {}
                        if header.get("task_id") not in (None, task):
                            raise ValueError("one-shot TTS task ID mismatch")
                        event = header.get("event")
                        if event == "task-started":
                            if submitted:
                                raise ValueError("duplicate TTS task-started")
                            submitted = True
                            self._emit("tts.task_started", task_id=task, segment_id=segment_id)
                            provider_text = f"[{tag}]{text}" if tag else text
                            await socket.send(command("continue-task", task, text=provider_text))
                            if segment_id not in self._mixed_text_events:
                                self._mixed_text_events.add(segment_id)
                                self._emit("tts.text_submitted", task_id=task,
                                           segment_id=segment_id, text=text, chars=len(text),
                                           start_char=start_char, end_char=start_char + len(text))
                            if segment_id not in self._mixed_seen_segments:
                                self._mixed_seen_segments.add(segment_id)
                                self._mixed_sentences_submitted += 1
                            self._mixed_last_run = {
                                "segment_id": segment_id, "task_id": task,
                                "instruction": style.instruction, "tag": tag,
                                "rate": style.rate, "source": style.source,
                            }
                            await socket.send(command("finish-task", task))
                        elif event in {"task-failed", "error"}:
                            raise RuntimeError(f"TTS {event}: {str(header.get('error_code', 'provider_error'))[:80]}")
                        elif event == "task-finished":
                            if not submitted or len(tail) % 2:
                                raise ValueError("one-shot task ended before start or invalid PCM")
                            if tail:
                                await frames.put(bytes(tail))
                            self._emit("tts.task_finished", task_id=task, segment_id=segment_id)
                            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await frames.put(exc)
        finally:
            if socket is not None:
                try:
                    await socket.close()
                finally:
                    self._mixed_sockets.discard(socket)
            await frames.put(None)

    async def _synthesize_stream(self, turn_id, identity, *, first_only=False):
        """One provider task; receive and paced Room publication must not stall text."""
        started = time.perf_counter()
        task = task_id()
        segment_id = f"{turn_id}-1"  # one acoustic/text segment per provider task
        task_started = asyncio.Event()
        audio = asyncio.Queue(maxsize=48)  # bounded ~960ms ahead of Room publication
        socket = None
        clean = False  # task-finished received: the connection may serve the next task
        try:
            socket, origin, connect_ms = await self._acquire_socket(turn_id, identity)
            if socket is None:
                return
            self.socket = socket
            if not self._current(turn_id, identity):
                return
            # No await/model call between the synchronous choice and this turn's run-task.
            style = self.style_provider(turn_id, dict(identity)) if self.style_provider else None
            if style is not None and not isinstance(style, SpeechStyle):
                raise ValueError("TTS style_provider must return SpeechStyle or None")
            self.speech_style = style
            if style is not None:
                self._emit("tts.style.locked", speech_style=asdict(style))
                if style.language != "yue" and style.language not in TTS_LANGUAGE_HINTS:
                    # ASR recognition is not evidence of TTS support. Omit the hint,
                    # diagnose the gap and preserve the existing audio path.
                    self._emit("tts.language_hint.unsupported", language=style.language,
                               fallback="provider_default", source=style.source)
            run_started = time.perf_counter()
            selected_voice = (self.voice_provider(dict(identity))
                              if self.voice_provider else self.voice)
            if not isinstance(selected_voice, str) or not selected_voice:
                raise ValueError("invalid selected TTS voice")
            self.locked_voice = selected_voice
            run_command = command("run-task", task, voice=selected_voice, style=style)
            if origin == "fresh":
                await socket.send(run_command)
            else:
                # An idle connection may have been closed by the provider. Prove
                # it with task-started before any text is queued on it; on
                # failure nothing was submitted, so one fresh retry is safe.
                try:
                    await socket.send(run_command)
                    async with asyncio.timeout(WARM_CONFIRM_SECONDS):
                        reply = await socket.recv()
                    header = (reply.get("header") or {}) if isinstance(reply, dict) else {}
                    if header.get("event") != "task-started" or header.get("task_id") not in (None, task):
                        raise ConnectionError(f"warm TTS answered {header.get('event') or type(reply).__name__}")
                    task_started.set()
                    self._emit("tts.task_started", task_id=task, segment_id=segment_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.connect_stats["reuse_failed"] += 1
                    logger.info("tts_reuse_failed robot=%s origin=%s error=%s",
                                self.robot.config.robot_id, origin, _tts_failure(exc))
                    await self._close_idle(socket)
                    socket, origin, connect_ms = await self._acquire_socket(turn_id, identity)
                    if socket is None:
                        return
                    self.socket = socket
                    if not self._current(turn_id, identity):
                        return
                    run_started = time.perf_counter()
                    await socket.send(run_command)
            self.connect_stats[origin] += 1
            self.last_connect = {"origin": origin, "connect_ms": connect_ms, "turn_id": turn_id}
            self._emit("tts.connection", origin=origin, connect_ms=connect_ms)
            self._emit("tts.run_task", task_id=task,
                       segment_id=segment_id, streaming=run_command["header"]["streaming"],
                       voice_fingerprint=sha256(selected_voice.encode()).hexdigest()[:12],
                       parameters={k: v for k, v in run_command["payload"]["parameters"].items()
                                   if k != "voice"},
                       speech_style=asdict(style) if style else None)

            async def submit():
                async with asyncio.timeout(10):
                    await task_started.wait()
                def is_finished():
                    closing = self._mixed_first_closed.is_set() if first_only else self.finished
                    return closing and not self.pending and self.queue.empty()
                while self._current(turn_id, identity):
                    text = await self.queue.get()
                    try:
                        if not text and is_finished():
                            await socket.send(command("finish-task", task))
                            if first_only:
                                self._mixed_first_sent.set()
                            return
                        if text:
                            await socket.send(command("continue-task", task, text=text))
                            start_char = len(self.submitted_text)
                            self.submitted_text += text
                            self._emit("tts.text_submitted", text=text, chars=len(text),
                                       task_id=task, segment_id=segment_id,
                                       start_char=start_char, end_char=len(self.submitted_text))
                        self._drain_pending(force=self.finished or
                                            (first_only and self._mixed_first_closed.is_set()))
                        if is_finished():
                            await socket.send(command("finish-task", task))
                            if first_only:
                                self._mixed_first_sent.set()
                            return
                    finally:
                        self.queue.task_done()

            async def receive():
                nonlocal clean
                tail = bytearray()
                first = True
                while self._current(turn_id, identity):
                    item = await asyncio.wait_for(socket.recv(), timeout=30)
                    if isinstance(item, bytes):
                        if not item:
                            continue
                        if first:
                            first = False
                            ms = round((time.perf_counter() - started) * 1000, 2)
                            if self.first_audio_ms is None:
                                self.first_audio_ms = ms
                            self.run_to_first_pcm_ms = round(
                                (time.perf_counter() - run_started) * 1000, 2,
                            )
                            self._emit("tts.first_audio", first_audio_ms=ms,
                                       run_to_first_pcm_ms=self.run_to_first_pcm_ms)
                        tail.extend(item)
                        while len(tail) >= FRAME_BYTES:
                            frame = bytes(tail[:FRAME_BYTES])
                            del tail[:FRAME_BYTES]
                            await audio.put(frame)
                    else:
                        header = item.get("header") or {}
                        if header.get("task_id") not in (None, task):
                            raise ValueError("TTS task_id mismatch")
                        event = header.get("event")
                        if event == "task-started":
                            task_started.set()
                            self._emit("tts.task_started", task_id=task, segment_id=segment_id)
                        elif event in {"task-failed", "error"}:
                            raise RuntimeError(f"TTS {event}: {str(header.get('error_code', 'provider_error'))[:80]}")
                        elif event == "task-finished":
                            self._emit("tts.task_finished", task_id=task, segment_id=segment_id)
                            if len(tail) % 2:
                                raise ValueError("TTS PCM has an odd total byte count")
                            clean = True
                            if tail:
                                await audio.put(bytes(tail))
                            await audio.put(None)
                            return

            async def publish():
                while self._current(turn_id, identity):
                    pcm = await audio.get()
                    if pcm is None:
                        return
                    await self._publish_pcm(pcm, self.submitted_text[:len(self._mixed_first_text)] if first_only
                                            else self.submitted_text, segment_id,
                                            turn_id, identity, started)

            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(_tts_stage("text_submit", submit()))
                tasks.create_task(_tts_stage("provider_receive", receive()))
                tasks.create_task(_tts_stage("room_publish", publish()))
            if self._current(turn_id, identity):
                await self.publisher.publish(self._packet(b"", self._mixed_first_text if first_only
                                                          else self.submitted_text,
                                                          final=True, segment_id=segment_id))
                self._emit("tts.segment_done", segment_id=segment_id, samples=self.samples)
        except BaseException:
            clean = False
            raise
        finally:
            if self.socket is socket:
                self.socket = None
            if socket is not None:
                if clean and not self._closing:
                    await self._park(socket, "reused")
                else:
                    await socket.close()

    async def _publish_pcm(self, pcm, text, segment_id, turn_id, identity, started):
        if not self._current(turn_id, identity):
            return
        # Keep up to 240 ms ahead of the PCM sample clock. Strict 1x publication
        # leaves no reserve for WebSocket and browser scheduling jitter.
        now = time.monotonic()
        if self.next_pcm_at is not None and self.next_pcm_at > now:
            await asyncio.sleep(self.next_pcm_at - now)
        if not self._current(turn_id, identity):
            return
        now = time.monotonic()
        self.next_pcm_at = max(now - PCM_LEAD_SECONDS,
                               self.next_pcm_at if self.next_pcm_at is not None else now - PCM_LEAD_SECONDS
                               ) + len(pcm) / 2 / 24000
        self.inflight_end = self.samples + len(pcm) // 2
        async with asyncio.timeout(2):
            await self.publisher.publish(self._packet(pcm, text, segment_id=segment_id))
        self.samples += len(pcm) // 2
        self.frames += 1
        if self.first_publish_ms is None:
            self.first_publish_ms = round((time.perf_counter() - self.started_at) * 1000, 2)
            self._emit("tts.first_published", first_publish_ms=self.first_publish_ms)
        if text != self.published_text:
            self.last_published_segment = segment_id
            self.published_text = text
            now = time.time_ns() // 1_000_000
            self.robot.ledger.enqueue(
                "update_playback", self.robot.config.robot_id, turn_id,
                published_text=self.published_text, spoken_text=None,
                evidence_status="unknown", updated_ms=now,
            )
            turn = self.robot.turn_cache.get(turn_id)
            if turn is not None:
                turn["assistant_published_text"] = self.published_text
        # The Room still receives every 20 ms PCM frame. Broadcasting each
        # frame into SSE makes React re-render the entire cockpit at 50 Hz
        # during speech; progress is diagnostic, not a realtime audio clock.
        if self.frames == 1 or self.frames % 25 == 0:
            self._emit("tts.pcm_published", segment_id=segment_id,
                       bytes=len(pcm), samples=self.samples, frames=self.frames)

    def on_playback_receipt(self, message):
        """Reported playback cursor; caller validates wire shape and Room ownership."""
        from annie_adapters.playback import playback_receipt

        playback_receipt(message)
        data = message["data"]
        cid, epoch = data["conversation_id"], data["stream_epoch"]
        active = (cid, epoch) == (self.turn_id, self.stream_epoch)
        stopped = self.pending_stop is not None and (cid, epoch) == self.pending_stop[:2]
        if not active and not stopped:
            return False
        if active:
            self._last_receipt_t = time.monotonic()
        limit = max(self.samples, self.inflight_end) if active else self.pending_stop[2]
        if data["played_sample_count"] > limit:
            return False
        source, playback_id = data["source"], data["playback_id"]
        track = self.receipt_tracks.get(source) if active else None
        kind = message["type"].removeprefix("tts.playback.")
        if kind in {"held", "resumed"}:
            command_seq = data["command_seq"]
            expected = "hold" if kind == "held" else "resume"
            if not active or self.pending_controls.get(command_seq) != expected:
                return False
            if track is not None and (
                track["id"] != playback_id or data["seq"] <= track["seq"] or
                data["played_sample_count"] < track["played"] or
                track["state"] in {"stopped", "drained"}
            ):
                return False
            if kind == "resumed" and (track is None or track["state"] != "held"):
                return False
            # Browser auditions report only browser playback. They may not
            # consume or confirm the physical speaker's pending control.
            if source == "speaker":
                self.pending_controls.pop(command_seq)
            self.receipt_tracks[source] = {
                "id": playback_id, "seq": data["seq"],
                "played": data["played_sample_count"], "state": kind,
            }
            if source == "speaker" and not any(
                seq > command_seq for seq in self.pending_controls
            ):
                self.control_state = f"{kind}_confirmed"
            if self.playback_source != "speaker" or source == "speaker":
                self.playback_source = source
                self.playback_id = playback_id
                self.playback_state = kind
                self.playback_evidence = f"{source}_reported_{kind}"
                self.played_samples = data["played_sample_count"]
            self._emit("tts.playback." + kind, conversation_id=cid,
                       stream_epoch=epoch, command_seq=command_seq,
                       played_sample_count=data["played_sample_count"],
                       source=source, evidence=f"{source}_reported_{kind}",
                       receipt_ms=time.time_ns() // 1_000_000)
            return True
        if active and track is not None:
            if track["id"] != playback_id or data["seq"] <= track["seq"]:
                return False
            if data["played_sample_count"] < track["played"]:
                return False
        if stopped and not active and data["played_sample_count"] < self.pending_stop[3]:
            return False
        if stopped and not active and self.pending_stop_source[0] is not None and (
            data["source"], data["playback_id"]
        ) != self.pending_stop_source:
            return False
        if kind == "started" and (not active or limit == 0):
            return False
        state = track["state"] if track is not None else "unknown"
        if kind in {"progress", "drained"} and state not in {"started", "progress", "resumed"}:
            return False
        if kind in {"started", "progress"} and (not active or state in {"stopped", "drained"}):
            return False
        if kind == "started" and track is not None and state != "resumed":
            return False
        if kind == "drained" and (
            not active or not self.synthesized_complete or
            data["played_sample_count"] != self.samples or self.samples == 0
        ):
            return False
        if kind == "stopped" and not stopped and not active:
            return False
        if stopped and not active and kind != "stopped":
            return False
        received_ms = time.time_ns() // 1_000_000
        preferred = self.playback_source != "speaker" or source == "speaker"
        if active:
            self.receipt_tracks[source] = {
                "id": playback_id, "seq": data["seq"],
                "played": data["played_sample_count"], "state": kind,
            }
            # The test listener cannot eclipse real speaker evidence.
            if preferred:
                self.playback_source = source
                self.playback_id = playback_id
                self.receipt_seq = data["seq"]
                self.played_samples = data["played_sample_count"]
                self.playback_state = kind
                self.playback_evidence = f"{source}_reported_{kind}"
        if kind == "started" and preferred:
            self.started_reported_ms = received_ms
        if kind == "stopped":
            self.stop_confirmed_ms = received_ms
            self.stopped_conversation_id = cid
            self.last_stop_evidence = f"{data['source']}_reported_stopped"
            self.pending_stop = None
            old_identity = self.pending_stop_identity
            self.pending_stop_identity = self.pending_stop_source = None
        else:
            old_identity = None
        evidence = f"{data['source']}_reported_{kind}"
        self._emit("tts.playback." + kind, conversation_id=cid, stream_epoch=epoch,
                   played_sample_count=data["played_sample_count"], source=data["source"],
                   playback_id=data["playback_id"], receipt_ms=received_ms,
                   evidence=evidence, turn_id=cid, **(old_identity or {}))
        # Only a completed, fully drained turn can claim a complete spoken text.
        # A partial stop has no per-word acoustic boundary, so never infer it.
        if kind == "drained" and data["source"] == "speaker" and cid in self.robot.turn_cache:
            turn = self.robot.turn_cache[cid]
            if turn.get("status") == "completed":
                now = received_ms
                self.robot.ledger.enqueue(
                    "update_playback", self.robot.config.robot_id, cid,
                    published_text=None, spoken_text=self.published_text,
                    evidence_status=self.playback_evidence, updated_ms=now,
                )
                turn["assistant_spoken_text"] = self.published_text
                turn["playback_evidence_status"] = self.playback_evidence
                if self.identity == self.robot.session_state():
                    self.robot._cache_context_turn(turn)
        return True

    def on_mouth_script(self, event):
        """Accept only real acoustic alignment for the currently active PCM."""
        if (event.get("type") != "mouth_script" or self.turn_id is None or self.cancelled
                or self.turn_id == self.failed_turn):
            return False
        if event.get("conversation_id") != self.turn_id:
            return False
        epoch = event.get("stream_epoch")
        if epoch is not None and (type(epoch) is not int or epoch != self.stream_epoch):
            return False
        if not str(event.get("timing_source", "")).startswith("qwen3_forced_aligner"):
            return False
        start, end = event.get("start_sample"), event.get("end_sample")
        if type(start) is not int or type(end) is not int or start < 0 or end <= start:
            return False
        if end > self.samples:
            return False
        rich = event.get("timing_mode") == "speech_sample_v1"
        if not rich and start < self.played_samples:
            return False
        if rich:
            index, rate, char = event.get("char_index"), event.get("sample_rate"), event.get("char")
            if (type(epoch) is not int or type(index) is not int or type(rate) is not int
                    or event.get("timing_mode") != "speech_sample_v1"
                    or rate != 24000 or not 0 <= index < len(self.submitted_text)
                    or self.submitted_text[index] != char
                    or not self.submitted_text.startswith(self.published_text)):
                return False
        marker = (event.get("stream_id"), event.get("seq"))
        if marker in self.viseme_seen:
            return False
        self.viseme_seen.add(marker)
        self.viseme_state = "aligner_reporting"
        self.viseme_events += 1
        fields = dict(conversation_id=self.turn_id, start_sample=start,
                      end_sample=end, viseme=event.get("viseme"), char=event.get("char"),
                      timing_source=event["timing_source"])
        # Never upgrade legacy packets to strict word-action evidence by
        # inventing an epoch, character index, or sample rate.
        if epoch is not None:
            fields["stream_epoch"] = epoch
        if rich:
            fields.update(char_index=index, sample_rate=rate, timing_mode="speech_sample_v1")
            self.alignment_points[index] = {
                **self.identity, "turn_id": self.turn_id, **fields,
            }
        self._emit("viseme.aligned", **fields)
        return True
