"""One process with isolated Phase 3 sessions, ledger, and bounded PCM input gates."""

import asyncio
import logging
import os
import re
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from annie_adapters import (
    VERIFIED_MIC_FORMAT,
    RoomPcmConnection,
    RuntimeStore,
    decode_room_binary_frame,
    match_cancel_identity,
    normalize_room_event,
    parse_mic_start,
)
from annie_adapters.playback import PLAYBACK_EVENTS as PLAYBACK_RECEIPTS
from annie_adapters.playback import playback_receipt
from annie_brain import FakeBrain
from annie_brain.persona import CHAT_PREFERENCES
from annie_core import (
    AudioFormat,
    InputRejectedError,
    NormalizedEvent,
    PcmFrame,
    RobotConfig,
    RobotQueueFullError,
    RobotSnapshot,
    RobotUnavailableError,
    TurnResult,
)
from annie_observability import ProcessProbe, Recorder, RuntimeEvent

from annie_runtime.audio_devices import RoomAudioDevices
from annie_runtime.events import EventService
from annie_runtime.idle import IdleService
from annie_runtime.realtime import RealtimeRobot
from annie_runtime.reminders import ReminderService
from annie_runtime.room_monitor import RoomMonitor
from annie_runtime.speaker_volume import SpeakerVolume
from annie_runtime.speech import SpeechOutput

logger = logging.getLogger(__name__)
PERCEPTION_REPEAT_SECONDS = 5.0

# Tracking frames arrive many times a second. The snapshot and event policy
# still see them; a per-frame runtime_event line only floods the console.
_QUIET_INPUT_EVENTS = frozenset({
    "vision.face",
    "vision.object",
    "vision.gesture",
})


@dataclass
class Job:
    request_id: str
    text: str
    fail: bool
    future: asyncio.Future
    enqueued_at: float
    chat_id: str
    persona_id: str
    persona_revision: int
    session_epoch: int
    generation: int
    cancel_source: str | None = None


class TurnCoordinator:
    """Per-robot bounded manual-response arbitration for Phase 3."""

    def __init__(self, capacity: int = 16):
        self.capacity = capacity
        self.playback_state = "idle"
        self.pending: dict[str, dict] = {}
        self.dedupe_until: dict[str, int] = {}
        self.expired = 0

    def reset(self):
        self.pending.clear()
        self.dedupe_until.clear()
        self.playback_state = "idle"

    def set_playback_state(self, state: str):
        if state not in {"idle", "playing", "unknown", "stopped"}:
            raise ValueError("invalid_playback_state")
        self.playback_state = state

    def event_block_reason(self, *, model_busy: bool, playback_active: bool):
        if model_busy:
            return "model_busy"
        if playback_active or self.playback_state in {"playing", "unknown"}:
            return "playback_active"
        return None

    def _prune(self, now_ms: int):
        expired = [
            key
            for key, request in self.pending.items()
            if request["expires_at_ms"] <= now_ms
        ]
        for key in expired:
            self.pending.pop(key, None)
        self.expired += len(expired)
        for key in [key for key, expires_at in self.dedupe_until.items() if expires_at <= now_ms]:
            self.dedupe_until.pop(key, None)

    def request(self, *, source: str, dedupe_key: str, ttl_ms: int, model_busy: bool):
        if not source.strip() or not dedupe_key.strip():
            raise ValueError("source and dedupe_key must not be blank")
        if type(ttl_ms) is not int or ttl_ms < 1 or ttl_ms > 60000:
            raise ValueError("ttl_ms must be an integer between 1 and 60000")
        now_ms = time.time_ns() // 1_000_000
        self._prune(now_ms)
        if dedupe_key in self.dedupe_until:
            return {"status": "dropped", "reason": "duplicate", "dedupe_key": dedupe_key}
        waiting_reason = "model_busy" if model_busy else None
        if waiting_reason is None and self.playback_state in {"playing", "unknown"}:
            waiting_reason = "playback_active"
        if waiting_reason is None:
            self.dedupe_until[dedupe_key] = now_ms + ttl_ms
            return {"status": "allowed", "reason": None, "dedupe_key": dedupe_key}
        if len(self.pending) >= self.capacity:
            return {"status": "dropped", "reason": "capacity", "dedupe_key": dedupe_key}
        request = {
            "source": source,
            "dedupe_key": dedupe_key,
            "created_ms": now_ms,
            "expires_at_ms": now_ms + ttl_ms,
            "reason": waiting_reason,
        }
        self.pending[dedupe_key] = request
        self.dedupe_until[dedupe_key] = now_ms + ttl_ms
        return {"status": "waiting", "reason": waiting_reason, "dedupe_key": dedupe_key}

    def snapshot(self, *, model_busy: bool):
        self._prune(time.time_ns() // 1_000_000)
        return {
            "model_busy": model_busy,
            "playback_state": self.playback_state,
            "pending_count": len(self.pending),
            "pending": list(self.pending.values()),
            "expired": self.expired,
            "dedupe_count": len(self.dedupe_until),
            "capacity": self.capacity,
        }


class ConversationLedgerWriter:
    """Ordered, bounded SQLite writer that never makes a normal turn await disk I/O."""

    def __init__(self, store: RuntimeStore, capacity: int = 512):
        self.store = store
        self.queue = asyncio.Queue(maxsize=capacity)
        self.task = None
        self.last_error: str | None = None

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self._run(), name="harbor:conversation-ledger")

    def enqueue(self, method: str, *args, **kwargs):
        if self.task is None or self.task.done():
            raise RobotUnavailableError("conversation ledger writer is not running")
        if self.last_error is not None:
            raise RobotUnavailableError(f"conversation ledger unavailable: {self.last_error}")
        try:
            self.queue.put_nowait((method, args, kwargs))
        except asyncio.QueueFull as exc:
            self.last_error = "conversation ledger queue full"
            raise RobotQueueFullError(self.last_error) from exc

    async def _run(self):
        while True:
            method, args, kwargs = await self.queue.get()
            try:
                if method == "__barrier__":
                    future = args[0]
                    if self.last_error is None:
                        if not future.done():
                            future.set_result(None)
                    elif not future.done():
                        future.set_exception(RuntimeError(self.last_error))
                    continue
                await asyncio.to_thread(getattr(self.store, method), *args, **kwargs)
            except Exception as exc:
                self.last_error = str(exc)
                logger.exception("conversation ledger write failed method=%s", method)
            finally:
                self.queue.task_done()

    async def flush(self):
        if self.task is None:
            return
        future = asyncio.get_running_loop().create_future()
        await self.queue.put(("__barrier__", (future,), {}))
        await future

    async def close(self):
        if self.task is None:
            return
        error = None
        try:
            await self.flush()
        except Exception as exc:
            error = exc
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        self.task = None
        if error is not None:
            raise error


class AudioInputGate:
    """Per-robot PCM admission and freshness boundary; it does not infer wire identity."""

    def __init__(self, config: RobotConfig):
        self.config = config
        self.queue = asyncio.Queue(maxsize=config.audio_queue_capacity)
        self.source_id = None
        self.audio_format: AudioFormat | None = None
        self.last_sequence = None
        self.stream_epoch = 0
        self.accepted = 0
        self.dropped = 0
        self.rejected = 0
        # (monotonic, frames) of PCM that never reached the provider, so a turn
        # with missing words shows whether audio was lost around it (10-22).
        self.losses = deque(maxlen=256)

    def note_lost(self, frames: int):
        if frames > 0:
            self.losses.append((time.monotonic(), frames))

    def lost_since(self, since: float) -> int:
        return sum(frames for at, frames in self.losses if at >= since)

    def _reject(self, reason: str, *, frame=False):
        self.rejected += 1
        if frame:
            self.note_lost(1)
        raise InputRejectedError(reason)

    def _clear(self):
        cleared = 0
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()
            cleared += 1
        self.dropped += cleared
        self.note_lost(cleared)
        return cleared

    def start_stream(self, source_id: str, audio_format: AudioFormat):
        if not self.config.listen_enabled:
            self._reject("listen_disabled")
        self._clear()
        self.stream_epoch += 1
        self.source_id = source_id
        self.audio_format = audio_format
        self.last_sequence = None

    def reset_source(self, source_id: str):
        if self.source_id != source_id:
            return 0
        cleared = self._clear()
        self.stream_epoch += 1
        self.source_id = None
        self.audio_format = None
        self.last_sequence = None
        return cleared

    def put(self, source_id: str, payload: bytes, *, sequence: int | None, received_at: float):
        if not self.config.listen_enabled:
            self._reject("listen_disabled", frame=True)
        if self.source_id is None or self.audio_format is None:
            self._reject("stream_not_started", frame=True)
        if source_id != self.source_id:
            self._reject("source_mismatch", frame=True)
        if len(payload) != self.audio_format.bytes_per_frame:
            self._reject("invalid_frame_size", frame=True)
        if (time.monotonic() - received_at) * 1000 > self.config.audio_max_age_ms:
            self._reject("stale_audio", frame=True)
        if sequence is None:
            sequence = 1 if self.last_sequence is None else self.last_sequence + 1
        if type(sequence) is not int or sequence < 0:
            self._reject("invalid_sequence", frame=True)
        if self.last_sequence is not None and sequence <= self.last_sequence:
            self._reject("out_of_order", frame=True)

        if self.queue.full():
            self.queue.get_nowait()
            self.queue.task_done()
            self.dropped += 1
            self.note_lost(1)
        enqueued_at = time.monotonic()
        self.queue.put_nowait(
            PcmFrame(self.config.robot_id, source_id, payload, sequence, received_at, enqueued_at)
        )
        self.last_sequence = sequence
        self.accepted += 1

    def next_nowait(self):
        frame = self.queue.get_nowait()
        queue_wait_ms = round((time.monotonic() - frame.enqueued_at) * 1000, 3)
        self.queue.task_done()
        return frame, queue_wait_ms

    async def next_frame(self):
        """Wake on PCM admission; the consumer calls task_done after send/drop."""
        frame = await self.queue.get()
        return frame, self.stream_epoch

    def stats(self):
        fmt = self.audio_format
        return {
            "queue_depth": self.queue.qsize(),
            "accepted": self.accepted,
            "dropped": self.dropped,
            "rejected": self.rejected,
            "stream_started": fmt is not None,
            "source_id": self.source_id,
            "format": None
            if fmt is None
            else {
                "codec": fmt.codec,
                "sample_rate": fmt.sample_rate,
                "channels": fmt.channels,
                "frame_ms": fmt.frame_ms,
                "bytes_per_frame": fmt.bytes_per_frame,
            },
        }


class RobotRuntime:
    SOCIAL_STATE_KEYS = {
        "user_affect",
        "conversation_mode",
        "relationship_tone",
        "response_need",
        "question_pressure",
        "persona_style",
        "humor_level",
        "advice_readiness",
        "reply_length",
        "speech_rate",
        "speech_volume",
        "spontaneous_motions",
        "home_city",
    }

    def __init__(
        self,
        config: RobotConfig,
        recorder: Recorder,
        store: RuntimeStore,
        ledger: ConversationLedgerWriter,
        context_limit: int,
    ):
        self.config = config
        self.recorder = recorder
        self.store = store
        self.ledger = ledger
        self.context_limit = context_limit
        self.brain = FakeBrain(config)
        self.snapshot = RobotSnapshot(
            config.robot_id,
            config.room_id,
            config.persona_id,
            state="stopped" if config.enabled else "disabled",
        )
        self.queue = asyncio.Queue(maxsize=config.queue_capacity)
        self.audio = AudioInputGate(config)
        self.worker = None
        self.turn_task = None
        self.job = None
        self.stopping = False
        self.lifecycle = asyncio.Lock()
        self.session_lock = asyncio.Lock()
        self.chat_id = "default"
        self.persona_id = config.persona_id
        self.persona_revision = 1
        self.session_epoch = 0
        self.generation = 0
        self.coordinator = TurnCoordinator()
        self.context_cache: list[dict] = []
        self.turn_cache: dict[str, dict] = {}

    def _remember_turn(self, turn: dict):
        turn_id = turn["turn_id"]
        self.turn_cache[turn_id] = turn
        while len(self.turn_cache) > max(64, self.context_limit * 4):
            self.turn_cache.pop(next(iter(self.turn_cache)))

    def _cache_context_turn(self, turn: dict, *, allow_generated: bool = True):
        if turn.get("turn_source", "user") != "user":
            return  # internal events are not conversation evidence
        context = dict(turn)
        context["_requires_speaker"] = not allow_generated
        context["assistant_context_text"] = (
            context.get("assistant_spoken_text")
            if not allow_generated else
            context.get("assistant_spoken_text") or context.get("assistant_published_text")
            or context.get("assistant_generated_text")
        )
        if not context["assistant_context_text"] and not (
            not allow_generated and context.get("user_text", "").strip()
        ):
            return
        self.context_cache = [item for item in self.context_cache if item["turn_id"] != turn["turn_id"]]
        self.context_cache.append(context)
        if len(self.context_cache) > self.context_limit:
            del self.context_cache[: len(self.context_cache) - self.context_limit]

    async def _load_context_cache(self):
        rows = await asyncio.to_thread(
            self.store.recent_conversation_context,
            self.config.robot_id,
            self.chat_id,
            self.persona_id,
            self.context_limit,
        )
        self.context_cache = rows
        for row in rows:
            self._remember_turn(dict(row))

    def recent_context(self):
        return [dict(item) for item in self.context_cache]

    def cached_turn(self, turn_id: str):
        turn = self.turn_cache.get(turn_id)
        return None if turn is None else dict(turn)

    def update_cached_playback(
        self,
        turn_id: str,
        *,
        published_text: str | None,
        spoken_text: str | None,
        evidence_status: str,
        updated_ms: int,
    ):
        turn = self.turn_cache.get(turn_id)
        if turn is None:
            return None
        if published_text is not None:
            turn["assistant_published_text"] = published_text
        if spoken_text is not None:
            turn["assistant_spoken_text"] = spoken_text
        turn["playback_evidence_status"] = evidence_status
        turn["updated_ms"] = updated_ms
        for item in self.context_cache:
            if item["turn_id"] != turn_id:
                continue
            if published_text is not None:
                item["assistant_published_text"] = published_text
            if spoken_text is not None:
                item["assistant_spoken_text"] = spoken_text
            item["playback_evidence_status"] = evidence_status
            item["updated_ms"] = updated_ms
            item["assistant_context_text"] = (
                item.get("assistant_spoken_text") if item.get("_requires_speaker")
                else item.get("assistant_spoken_text") or item.get("assistant_published_text")
                or item.get("assistant_generated_text")
            )
            break
        return dict(turn)

    def sync_input_snapshot(self):
        stats = self.audio.stats()
        self.snapshot.audio_queue_depth = stats["queue_depth"]
        self.snapshot.audio_accepted = stats["accepted"]
        self.snapshot.audio_dropped = stats["dropped"]
        self.snapshot.audio_rejected = stats["rejected"]

    def record(
        self,
        kind,
        request_id=None,
        elapsed_ms=None,
        error=None,
        *,
        queue_wait_ms=None,
        source=None,
    ):
        self.snapshot.queue_depth = self.queue.qsize()
        self.sync_input_snapshot()
        self.recorder.emit(
            RuntimeEvent(
                self.config.robot_id,
                self.config.room_id,
                kind,
                request_id,
                elapsed_ms,
                error,
                queue_wait_ms=queue_wait_ms,
                source=source,
            ),
            self.snapshot.to_dict(),
        )
        log = logger.warning if error else logger.info
        log(
            "runtime_event robot=%s kind=%s request_id=%s source=%s elapsed_ms=%s queue_wait_ms=%s error=%s",
            self.config.robot_id,
            kind,
            request_id,
            source,
            elapsed_ms,
            queue_wait_ms,
            error,
        )

    def session_state(self):
        return {
            "robot_id": self.config.robot_id,
            "room_id": self.config.room_id,
            "chat_id": self.chat_id,
            "persona_id": self.persona_id,
            "persona_revision": self.persona_revision,
            "session_epoch": self.session_epoch,
            "generation": self.generation,
        }

    def _sync_session_snapshot(self):
        self.snapshot.persona_id = self.persona_id
        self.snapshot.chat_id = self.chat_id
        self.snapshot.persona_revision = self.persona_revision
        self.snapshot.session_epoch = self.session_epoch
        self.snapshot.generation = self.generation

    async def initialize_session(self):
        stored = await asyncio.to_thread(
            self.store.load_conversation_session,
            self.config.robot_id,
        )
        if stored is not None:
            self.chat_id = stored["chat_id"]
            self.persona_id = stored["persona_id"]
            self.persona_revision = stored["persona_revision"]
            self.session_epoch = stored["session_epoch"] + 1
            self.generation = stored["generation"] + 1
        else:
            self.session_epoch = 1
            self.generation = 1
        self._sync_session_snapshot()
        await self._persist_session()
        await self._load_context_cache()

    async def _persist_session(self):
        session = self.session_state()
        session["updated_ms"] = time.time_ns() // 1_000_000
        await asyncio.to_thread(self.store.save_conversation_session, session)

    async def switch_session(self, *, chat_id: str, persona_id: str, persona_revision: int = 1):
        chat_id = chat_id.strip()
        persona_id = persona_id.strip()
        if not chat_id or not persona_id:
            raise ValueError("chat_id and persona_id must not be blank")
        if type(persona_revision) is not int or persona_revision < 1:
            raise ValueError("persona_revision must be a positive integer")
        async with self.session_lock:
            unchanged = (
                self.chat_id == chat_id
                and self.persona_id == persona_id
                and self.persona_revision == persona_revision
            )
            if unchanged:
                return self.session_state()
            self.chat_id = chat_id
            self.persona_id = persona_id
            self.persona_revision = persona_revision
            self.session_epoch += 1
            self.generation += 1
            self.coordinator.reset()
            self.context_cache = []
            self._sync_session_snapshot()
            await self._persist_session()
            await self.cancel(cancel_source="session_switch")
            await self.discard_pending(cancel_source="session_switch")
            await self.ledger.flush()
            await self._load_context_cache()
        self.record("session_switched")
        return self.session_state()

    async def delete_current_conversation(self):
        async with self.session_lock:
            chat_id = self.chat_id
            persona_id = self.persona_id
            self.session_epoch += 1
            self.generation += 1
            self.coordinator.reset()
            self._sync_session_snapshot()
            await self._persist_session()
            await self.cancel(cancel_source="conversation_delete")
            await self.discard_pending(cancel_source="conversation_delete")
            await self.ledger.flush()
            self.context_cache = []
            deleted = await asyncio.to_thread(
                self.store.delete_conversation,
                self.config.robot_id,
                chat_id,
                persona_id,
            )
            self.turn_cache = {
                turn_id: turn
                for turn_id, turn in self.turn_cache.items()
                if not (turn["chat_id"] == chat_id and turn["persona_id"] == persona_id)
            }
        self.record("conversation_deleted")
        return deleted

    async def start(self):
        async with self.lifecycle:
            if not self.config.enabled or self.worker is not None and not self.worker.done():
                return
            self.stopping = False
            self.snapshot.state = "idle"
            self.snapshot.error = None
            self.worker = asyncio.create_task(self._run(), name=f"harbor:robot:{self.config.robot_id}")
            self.record("started")

    async def submit(self, text, *, fail=False):
        if self.snapshot.state not in ("idle", "running"):
            raise RobotUnavailableError(f"{self.config.robot_id} is {self.snapshot.state}")
        async with self.session_lock:
            if self.queue.full():
                raise RobotQueueFullError(f"{self.config.robot_id} queue full")
            request_id = uuid4().hex
            job = Job(
                request_id,
                text,
                fail,
                asyncio.get_running_loop().create_future(),
                time.perf_counter(),
                self.chat_id,
                self.persona_id,
                self.persona_revision,
                self.session_epoch,
                self.generation,
            )
            created_ms = time.time_ns() // 1_000_000
            turn = {
                "id": None,
                "turn_id": request_id,
                "robot_id": self.config.robot_id,
                "room_id": self.config.room_id,
                "chat_id": job.chat_id,
                "persona_id": job.persona_id,
                "persona_revision": job.persona_revision,
                "session_epoch": job.session_epoch,
                "generation": job.generation,
                "user_text": text,
                "assistant_planned_text": None,
                "assistant_generated_text": None,
                "assistant_published_text": None,
                "assistant_spoken_text": None,
                "playback_evidence_status": "unknown",
                "status": "queued",
                "interrupted": False,
                "cancelled": False,
                "cancel_source": None,
                "created_ms": created_ms,
                "updated_ms": created_ms,
            }
            self.ledger.enqueue(
                "create_conversation_turn",
                {
                    "turn_id": request_id,
                    "robot_id": self.config.robot_id,
                    "room_id": self.config.room_id,
                    "chat_id": job.chat_id,
                    "persona_id": job.persona_id,
                    "persona_revision": job.persona_revision,
                    "session_epoch": job.session_epoch,
                    "generation": job.generation,
                    "user_text": text,
                    "status": "queued",
                    "created_ms": created_ms,
                    "updated_ms": created_ms,
                },
            )
            self._remember_turn(turn)
            self.queue.put_nowait(job)
        self.record("queued", job.request_id)
        return await asyncio.shield(job.future)

    async def discard_pending(self, *, cancel_source: str | None = None):
        while not self.queue.empty():
            job = self.queue.get_nowait()
            elapsed = round((time.perf_counter() - job.enqueued_at) * 1000, 3)
            updated_ms = time.time_ns() // 1_000_000
            try:
                self.ledger.enqueue(
                    "finish_conversation_turn",
                    self.config.robot_id,
                    job.request_id,
                    status="discarded",
                    cancelled=bool(cancel_source),
                    cancel_source=cancel_source,
                    updated_ms=updated_ms,
                )
            except (RobotQueueFullError, RobotUnavailableError) as exc:
                self.snapshot.error = str(exc)
                logger.error(
                    "conversation ledger unavailable while discarding robot=%s request_id=%s error=%s",
                    self.config.robot_id,
                    job.request_id,
                    exc,
                )
            turn = self.turn_cache.get(job.request_id)
            if turn is not None:
                turn.update(
                    status="discarded",
                    cancelled=bool(cancel_source),
                    cancel_source=cancel_source,
                    updated_ms=updated_ms,
                )
            result = TurnResult(
                self.config.robot_id,
                job.request_id,
                "discarded",
                elapsed_ms=elapsed,
                queue_wait_ms=elapsed,
            )
            self.record("discarded", job.request_id, elapsed)
            job.future.set_result(result)
            self.queue.task_done()

    async def _run(self):
        try:
            while True:
                job = await self.queue.get()
                self.job = job
                snap = self.snapshot
                snap.state = "running"
                snap.current_request_id = job.request_id
                snap.last_input = job.text
                self.record("turn_started", job.request_id)
                began = time.perf_counter()
                queue_wait = round((began - job.enqueued_at) * 1000, 3)
                self.turn_task = asyncio.create_task(
                    self.brain.generate(job.text, fail=job.fail), name=f"harbor:turn:{self.config.robot_id}"
                )
                status, reply, generated, error = "completed", None, None, None
                try:
                    generated = await self.turn_task
                    if (
                        job.session_epoch != self.session_epoch
                        or job.generation != self.generation
                        or job.chat_id != self.chat_id
                        or job.persona_id != self.persona_id
                    ):
                        status = "discarded"
                    else:
                        reply = generated
                except asyncio.CancelledError:
                    status = "cancelled"
                    snap.cancelled += 1
                except Exception as exc:
                    status, error = "failed", str(exc)
                    snap.failures += 1
                    snap.error = error
                    logger.exception(
                        "Fake turn failed robot_id=%s request_id=%s", snap.robot_id, job.request_id
                    )
                finished = time.perf_counter()
                service = round((finished - began) * 1000, 3)
                elapsed = round((finished - job.enqueued_at) * 1000, 3)
                snap.queue_wait_ms = queue_wait
                snap.service_ms = service
                snap.elapsed_ms = elapsed
                snap.current_request_id = None
                self.turn_task = None
                self.job = None
                updated_ms = time.time_ns() // 1_000_000
                try:
                    self.ledger.enqueue(
                        "finish_conversation_turn",
                        self.config.robot_id,
                        job.request_id,
                        status=status,
                        planned_text=generated,
                        generated_text=generated,
                        cancelled=status == "cancelled",
                        cancel_source=job.cancel_source,
                        updated_ms=updated_ms,
                    )
                except (RobotQueueFullError, RobotUnavailableError) as exc:
                    status = "failed"
                    reply = None
                    error = str(exc)
                    snap.failures += 1
                    snap.error = error
                turn = self.turn_cache.get(job.request_id)
                if turn is not None:
                    turn.update(
                        status=status,
                        assistant_planned_text=generated,
                        assistant_generated_text=generated,
                        cancelled=status == "cancelled",
                        cancel_source=job.cancel_source,
                        updated_ms=updated_ms,
                    )
                    if status == "completed":
                        self._cache_context_turn(turn)
                if status == "completed":
                    snap.completed += 1
                    snap.last_reply = reply
                snap.state = "stopped" if self.stopping else "failed" if status == "failed" else "idle"
                self.record(status, job.request_id, elapsed, error)
                job.future.set_result(
                    TurnResult(
                        snap.robot_id,
                        job.request_id,
                        status,
                        reply,
                        error,
                        elapsed,
                        queue_wait,
                        service,
                    )
                )
                self.queue.task_done()
                if self.stopping or status == "failed":
                    break
        finally:
            await self.discard_pending(cancel_source="runtime_stop" if self.stopping else None)

    async def cancel(self, *, cancel_source: str = "local"):
        async with self.lifecycle:
            if self.turn_task is not None:
                job = self.job
                job.cancel_source = cancel_source
                self.turn_task.cancel()
                await asyncio.shield(job.future)
            return self.snapshot.to_dict()

    async def stop(self):
        async with self.lifecycle:
            self.stopping = True
            if self.worker is not None and not self.worker.done():
                self.worker.cancel()
                await asyncio.gather(self.worker, return_exceptions=True)
            self.worker = None
            await self.discard_pending(cancel_source="runtime_stop")
            self.audio._clear()
            self.audio.source_id = None
            self.audio.audio_format = None
            self.audio.last_sequence = None
            if self.config.enabled:
                self.snapshot.state = "stopped"
            self.record("stopped")
            return self.snapshot.to_dict()


class HarborRuntime:
    def __init__(
        self,
        configs: list[RobotConfig],
        database: Path,
        room_server=None,
        *,
        conversation_history_turns: int = 20,
        realtime_history_turns: int = 8,
        realtime_restore_char_budget: int = 3000,
        persona_profiles: dict | None = None,
        realtime_key: str | None = None,
        realtime_url: str | None = None,
        realtime_model: str | None = None,
        realtime_socket_factory=None,
        acoustic_enabled: bool = False,
        tts_expression_enabled: bool = False,
        tts_mixed_mode_enabled: bool = False,
        acoustic_url: str | None = None,
        acoustic_socket_factory=None,
        wake_detector=None,
        wake_wait_ms: int = 5000,
        wake_followup_ms: int = 20000,
        side_talk_listen_ms: int = 120000,
        greet_known_cooldown_minutes: float = 30,
        face_api_urls: dict[str, str] | None = None,
        face_registries: dict | None = None,
        face_samples: int = 18,
        cloud_aec_robots: tuple[str, ...] = (),
        cloud_aec_sync_offset_ms: int = 80,
        cloud_aec_delay_ms: int = 0,
        tts_voice: str | None = None,
        tts_url: str | None = None,
        tts_socket_factory=None,
        room_tts_factory=None,
        tts_first_chars: int = 10,
        tts_min_chars: int | None = None,
        tts_max_chars: int = 48,
        memory_key: str | None = None,
        memory_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
        memory_extract_model: str = "qwen3.8-flash",
        memory_embedding_model: str = "text-embedding-v4",
        memory_embedding_dimensions: int = 256,
        memory_model=None,
        action_catalog: Path | None = None,
        actions_enabled: bool = False,
        action_allowlist: tuple[str, ...] = (),
        room_action_factory=None,
        weather_key: str | None = None,
        weather_client=None,
        weather_timeout_ms: int = 1200,
        weather_cache_sec: int = 180,
        weather_default_city: str | None = None,
        idle_model=None,
        idle_seconds: float = 60,
        idle_second_seconds: float = 120,
        continuation_enabled: bool = False,
        continuation_seconds: float = 15,
        speaker_embedder=None,
        speaker_threshold: float = 0.5,
        speaker_margin: float = 0.08,
        decision_url: str | None = None,
        addressee_model: str | None = None,
        addressee_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
        addressee_timeout_ms: int = 1200,
        decision_client=None,
        decision_timeout_ms: int = 1500,
        d3_url: str | None = None,
        d3_key: str | None = None,
        d3_model: str = "d3-NVFP4",
        d3_timeout_ms: int = 1500,
        d3_expression_threshold: float = 0.7,
        d3_route=None,
        addressee_enabled: bool = False,
        auto_expression_enabled: bool = False,
        expression_threshold: float = 0.55,
        ambient_enabled: bool = False,
        inner_life_enabled: bool = False,
        inner_life_summarizer=None,
        inner_life_close_seconds: float = 120,
        inner_life_keep_days: int = 7,
        motion_options: dict | None = None,
        vision_intent_enabled: bool = False,
        vision_intent_route: str = "cloud",
        vision_intent_deadline_ms: int = 3500,
        vision_intent_action_extra_ms: int = 1200,
    ):
        if not configs or len({c.robot_id for c in configs}) != len(configs):
            raise ValueError("at least one config and unique robot_id values required")
        if (type(vision_intent_action_extra_ms) is not int
                or not 0 <= vision_intent_action_extra_ms <= 10000):
            raise ValueError("vision_intent_action_extra_ms must be an integer between 0 and 10000")
        if type(conversation_history_turns) is not int or not 1 <= conversation_history_turns <= 100:
            raise ValueError("conversation_history_turns must be an integer between 1 and 100")
        if type(realtime_history_turns) is not int or not 1 <= realtime_history_turns <= 50:
            raise ValueError("realtime_history_turns must be an integer between 1 and 50")
        if (type(realtime_restore_char_budget) is not int or
                not 1 <= realtime_restore_char_budget <= 12000):
            raise ValueError("realtime_restore_char_budget must be an integer between 1 and 12000")
        if type(tts_expression_enabled) is not bool:
            raise ValueError("tts_expression_enabled must be boolean")
        if type(tts_mixed_mode_enabled) is not bool or (tts_mixed_mode_enabled
                                                          and not tts_expression_enabled):
            raise ValueError("mixed TTS requires enabled expression parser")
        if tts_mixed_mode_enabled and not tts_voice:
            raise ValueError("mixed TTS requires enabled TTS voice")
        if continuation_enabled and (idle_model is None or
                                     not 8 <= continuation_seconds <= 25):
            raise ValueError("short continuation requires idle model and delay from 8 to 25 seconds")
        self.store = RuntimeStore(database)
        from annie_runtime.speakers import SpeakerService

        self.speakers = SpeakerService(
            self.store, speaker_embedder,
            threshold=speaker_threshold, margin=speaker_margin,
        )
        self.voices = None
        if realtime_key and tts_voice:
            from annie_adapters.tts import DEFAULT_URL as TTS_DEFAULT_URL

            from annie_runtime.voice_profiles import DashscopeVoiceProvider, VoiceProfiles
            provider = DashscopeVoiceProvider(realtime_key, tts_url or TTS_DEFAULT_URL)
            self.voices = VoiceProfiles(self.store, provider, tts_voice)
        self.recorder = Recorder(self.store.write_batch)
        self.process_probe = ProcessProbe()
        self.ledger = ConversationLedgerWriter(self.store)
        if memory_key or memory_model:
            from annie_brain.memory import CloudMemoryModel, MemoryService

        self.memory = (
            MemoryService(
                self.store,
                memory_model or CloudMemoryModel(
                    memory_key, memory_base_url, memory_extract_model,
                    memory_embedding_model, memory_embedding_dimensions,
                ),
                [c.robot_id for c in configs if c.enabled],
            ) if memory_key or memory_model else None
        )
        self.conversation_history_turns = conversation_history_turns
        self.persona_profiles = dict(persona_profiles or {})
        self.robots = {
            c.robot_id: RobotRuntime(
                c,
                self.recorder,
                self.store,
                self.ledger,
                conversation_history_turns,
            )
            for c in configs
        }
        # One microphone and one speaker per Room: observed, never enforced.
        self.audio_devices = {
            robot_id: RoomAudioDevices(
                on_change=lambda status, snapshot, robot_id=robot_id:
                self._audio_devices_changed(robot_id, status, snapshot),
                on_head=lambda kind, details, robot_id=robot_id:
                self._robot_head_changed(robot_id, kind, details))
            for robot_id in self.robots
        }
        self.input_bindings: dict[str, tuple[str, ...]] = {}
        self.local_input_sources: set[str] = set()
        self._perception_last = {}  # (robot_id, kind) -> (signature, monotonic)
        self.room_server = room_server
        self.room_connections = {}
        self.room_sources = {}
        self.cloud_aec = {}
        self.cloud_aec_robots = tuple(cloud_aec_robots)
        self.cloud_aec_sync_offset_ms = cloud_aec_sync_offset_ms
        self.cloud_aec_delay_ms = cloud_aec_delay_ms
        if self.cloud_aec_robots and (not realtime_key or not tts_voice or room_server is None):
            raise ValueError("cloud AEC requires Realtime, active TTS and a configured Room")
        if any(robot_id not in self.robots or not self.robots[robot_id].config.listen_enabled
               for robot_id in self.cloud_aec_robots):
            raise ValueError("cloud AEC robot must have a configured listening Room")
        self.playback_listeners = {}
        self.voice_publishers = {}
        self.voice_tasks = set()
        self.display_outboxes = {}
        self.room_monitors = {
            robot_id: RoomMonitor(robot.config.room_id)
            for robot_id, robot in self.robots.items() if robot.config.enabled
        }
        self.smallbrain_links = {}
        self.smallbrain_tasks = set()
        self._viseme_probe = None
        self.realtime = {}
        if realtime_key:
            from annie_adapters.realtime import DEFAULT_URL, MODEL, QwenRealtimeSocket, validate_model
            from annie_adapters.realtime import realtime_url as validate_url

            model = validate_model(realtime_model or MODEL)
            url = validate_url(realtime_url or DEFAULT_URL, model)
            self.realtime = {
                robot_id: RealtimeRobot(
                    robot, url=url, key=realtime_key, model=model,
                    history_turns=realtime_history_turns,
                    restore_char_budget=realtime_restore_char_budget,
                    persona_profiles=self.persona_profiles,
                    socket_factory=realtime_socket_factory or QwenRealtimeSocket,
                    wake_detector=wake_detector,
                    wake_wait_ms=wake_wait_ms,
                    wake_followup_ms=wake_followup_ms,
                    side_talk_listen_ms=side_talk_listen_ms,
                    expression_enabled=tts_expression_enabled and bool(tts_voice),
                )
                for robot_id, robot in self.robots.items() if robot.config.enabled
            }
        if type(acoustic_enabled) is not bool:
            raise ValueError('acoustic_enabled must be boolean')
        if self.realtime:
            from annie_adapters.acoustic import QwenASRSocket
            from annie_adapters.realtime import DEFAULT_URL as ASR_URL

            from annie_runtime.acoustic import AcousticSidecar
            for robot_id, client in self.realtime.items():
                client.acoustic = AcousticSidecar(
                    robot_id=robot_id, room_id=client.robot.config.room_id,
                    url=acoustic_url or ASR_URL, key=realtime_key,
                    on_observation=client.accept_acoustic_observation,
                    socket_factory=acoustic_socket_factory or QwenASRSocket,
                    enabled=acoustic_enabled)
                client.language_preference_writer = (
                    lambda robot_id=robot_id, **kwargs:
                    self._write_language_preference(robot_id, **kwargs))
        from annie_runtime.speakers import SpeakerTracker

        for client in self.realtime.values():
            client.speakers = SpeakerTracker(self.speakers, client.note_speaker)
        # Decision model: one shared keep-alive client for every robot.
        self.decision = decision_client
        self._owns_decision = False
        if self.decision is None and decision_url and realtime_key and self.realtime:
            from annie_adapters.decision import DecisionClient

            self.decision = DecisionClient(decision_url, realtime_key,
                                           timeout=decision_timeout_ms / 1000)
            self._owns_decision = True
        # Self-hosted d3 answers first while its probe says it is up; DashScope
        # (qwen-flash for addressee, the decision model for faces) is the fallback.
        self.d3 = d3_route
        self._owns_d3 = False
        if self.d3 is None and d3_url and self.realtime:
            from annie_adapters.decision import DecisionClient

            from annie_runtime.decision_route import D3Route
            if not d3_key:
                raise ValueError("ANNIE_D3_URL requires ANNIE_D3_TOKEN")
            self.d3 = D3Route(
                DecisionClient(d3_url, d3_key, model=d3_model, timeout=d3_timeout_ms / 1000,
                               gateway=True),
                model=d3_model, expression_threshold=d3_expression_threshold)
            self._owns_d3 = True
        self.addressee_judge = None
        if addressee_enabled and addressee_model and realtime_key and self.realtime:
            from annie_adapters.addressee import AddresseeJudge

            # qwen-flash decides "is this for her?" (user decision 2026-10-21);
            # the decision model keeps facial expressions.
            self.addressee_judge = AddresseeJudge(
                realtime_key, model=addressee_model, base_url=addressee_base_url,
                timeout_ms=addressee_timeout_ms)
        for client in self.realtime.values():
            client.decision = self.decision
            client.d3 = self.d3
            client.addressee_judge = self.addressee_judge
            client.addressee_enabled = bool(addressee_enabled and (
                self.decision or self.addressee_judge or self.d3))
            client.auto_expression_enabled = bool(auto_expression_enabled
                                                  and (self.decision or self.d3))
            client.expression_threshold = expression_threshold
        if ambient_enabled and self.realtime:
            from annie_adapters.acoustic import QwenASRSocket as AmbientSocket
            from annie_adapters.realtime import DEFAULT_URL as AMBIENT_URL

            from annie_runtime.acoustic import AcousticSidecar
            from annie_runtime.ambient import MAX_LAG_MS, QUEUE_FRAMES, AmbientListener
            for robot_id, client in self.realtime.items():
                if client.wake is None:
                    continue  # no wake gate: every utterance already reaches Realtime
                sidecar = AcousticSidecar(
                    robot_id=robot_id, room_id=client.robot.config.room_id,
                    url=acoustic_url or AMBIENT_URL, key=realtime_key,
                    on_observation=lambda row, client=client: client.ambient.observe(row),
                    socket_factory=acoustic_socket_factory or AmbientSocket,
                    max_lag_ms=MAX_LAG_MS, queue_frames=QUEUE_FRAMES)
                client.ambient = AmbientListener(sidecar, client._remember_overheard)
        for robot_id, client in self.realtime.items():
            client.visual_observer = (
                lambda robot_id=robot_id: self._current_visual(robot_id)
            )
        if self.memory:
            for client in self.realtime.values():
                client.memory = self.memory
        if room_server is not None and "voice" in room_server.channels and self.realtime:
            from annie_adapters.tts import RoomTtsPublisher

            for robot_id, client in self.realtime.items():
                publisher = RoomTtsPublisher(
                    room_server, client.robot.config.room_id, channel="voice",
                )
                self.voice_publishers[robot_id] = publisher
                # "大声点" turns the board speaker itself up (room-speaker).
                client.speaker_volume = SpeakerVolume(self.audio_devices[robot_id], publisher)
                client.voice_event = (
                    lambda kind, event, robot_id=robot_id:
                    self._send_voice_event(robot_id, kind, event)
                )
        if room_server is not None and "display" in room_server.channels and self.realtime:
            from annie_adapters.tts import RoomTtsPublisher

            from annie_runtime.display import DisplayOutbox, wake_fields

            for robot_id, client in self.realtime.items():
                publisher = (room_tts_factory or RoomTtsPublisher)(
                    room_server, client.robot.config.room_id, channel="display",
                )
                box = DisplayOutbox(
                    publisher, robot_id, client.robot.config.room_id,
                    lambda client=client: wake_fields(client),
                )
                self.display_outboxes[robot_id] = box
                client.display_event = lambda kind, event, box=box: box.observe(kind, event)
                if client.wake is not None:
                    client.wake.on_display_wake = box.note_wake
        if tts_voice:
            if not realtime_key or room_server is None:
                raise ValueError("TTS requires Realtime and a configured Room server")
            from annie_adapters.playback import RoomEventListener
            from annie_adapters.tts import DEFAULT_URL as TTS_DEFAULT_URL
            from annie_adapters.tts import QwenTtsSocket, RoomTtsPublisher, validate_url

            if self.cloud_aec_robots:
                from annie_runtime.cloud_aec import CloudAec

                for robot_id in self.cloud_aec_robots:
                    self.cloud_aec[robot_id] = CloudAec(
                        None,  # current asyncio loop is bound in start()
                        lambda pcm, received_at, robot_id=robot_id: self._accept_aec_pcm(
                            robot_id, pcm, received_at),
                        sync_offset_ms=self.cloud_aec_sync_offset_ms,
                        delay_ms=self.cloud_aec_delay_ms,
                    )

            url = validate_url(tts_url or TTS_DEFAULT_URL)
            for robot_id, client in self.realtime.items():
                publisher = (room_tts_factory or RoomTtsPublisher)(
                    room_server, client.robot.config.room_id,
                )
                if robot_id in self.cloud_aec:
                    from annie_runtime.cloud_aec import AecTtsPublisher

                    publisher = AecTtsPublisher(publisher, self.cloud_aec[robot_id])
                client.speech = SpeechOutput(
                    client.robot, publisher, url=url, key=realtime_key,
                    voice=tts_voice, socket_factory=tts_socket_factory or QwenTtsSocket,
                    first_chars=tts_first_chars, min_chars=tts_min_chars, max_chars=tts_max_chars,
                    event=client.publish,
                    style_provider=client.turn_speech_style,
                    mixed_enabled=tts_mixed_mode_enabled,
                    voice_provider=(lambda identity: self.voices.resolved(identity))
                    if self.voices is not None else None,
                    voice_publisher=self.voice_publishers.get(robot_id),
                )
                client.voices = self.voices
                self.playback_listeners[robot_id] = (
                    RoomEventListener(
                        room_server, client.robot.config.room_id, "main",
                        lambda message, robot_id=robot_id: self._room_playback_receipt(robot_id, message),
                    ),
                )
        self.actions = None
        if action_catalog is not None:
            from annie_brain.action_catalog import load_action_catalog, resolve_action

            from annie_runtime.actions import ActionCoordinator

            if actions_enabled and (not self.realtime or room_server is None or not tts_voice):
                raise ValueError("real action delivery requires Realtime, Room and TTS anchor")
            catalog = load_action_catalog(action_catalog)
            for script in action_allowlist:
                resolve_action(catalog, script)
            if len(set(action_allowlist)) != len(action_allowlist):
                raise ValueError("duplicate action allowlist entry")
            if actions_enabled and not action_allowlist:
                raise ValueError("real actions require an explicit nonempty allowlist")
            from annie_adapters.tts import RoomTtsPublisher

            publishers = {
                robot_id: (room_action_factory or RoomTtsPublisher)(
                    room_server, robot.config.room_id, channel="main",
                )
                for robot_id, robot in self.robots.items()
                if robot_id in self.realtime and room_server is not None and actions_enabled
            }
            self.actions = ActionCoordinator(
                self.store, catalog, self.robots, publishers,
                lambda robot_id, kind, **fields: self.realtime[robot_id].publish(kind, **fields),
                enabled=actions_enabled,
                allowlist=action_allowlist,
            )
            for client in self.realtime.values():
                client.actions = self.actions
        self.weather = None
        if weather_key or weather_client:
            from annie_adapters.weather import AmapWeather

            self.weather = AmapWeather(
                weather_key or "", client=weather_client,
                timeout_ms=weather_timeout_ms, cache_sec=weather_cache_sec,
                default_city=weather_default_city,
            )
            for client in self.realtime.values():
                client.weather = self.weather
        if self.memory is not None:
            self.memory.on_change = self._memory_changed
        self.started = False
        self.start_time = time.time()
        self.events = EventService(self, greet_cooldown_ms=int(greet_known_cooldown_minutes * 60_000))
        # "记住我/忘掉我" in conversation, on each robot's own camera service.
        self.faces = None
        registries = dict(face_registries or {})
        if face_api_urls:
            from annie_adapters.face_registry import FaceRegistry

            for robot_id, url in face_api_urls.items():
                if robot_id in self.robots and robot_id not in registries and url:
                    registries[robot_id] = FaceRegistry(url)
        if registries:
            from annie_runtime.faces import FaceService

            self.faces = FaceService(self, registries, samples=face_samples)
            for client in self.realtime.values():
                client.faces = self.faces
        self.reminders = ReminderService(self)
        self.idle = IdleService(self, idle_model, idle_seconds=idle_seconds,
                                second_seconds=idle_second_seconds,
                                continuation_enabled=continuation_enabled,
                                continuation_seconds=continuation_seconds)
        # Her own mood and day: beside the reply path, read by the session prompt.
        self.inner_life = None
        if inner_life_enabled:
            from annie_runtime.inner_life import InnerLifeService

            self.inner_life = InnerLifeService(
                self, summarizer=inner_life_summarizer,
                close_seconds=inner_life_close_seconds, keep_days=inner_life_keep_days)
            for client in self.realtime.values():
                client.inner_life = self.inner_life
        # Her body while awake and talking; idle motion stays with the embedded player.
        self.motions = None
        if motion_options is not None and self.actions is not None:
            from annie_runtime.motions import InteractionMotions

            self.motions = InteractionMotions(self, **motion_options)
            for client in self.realtime.values():
                client.motions = self.motions
        for robot_id, client in self.realtime.items():
            client.idle_observer = (
                lambda event, robot_id=robot_id: self.idle.on_event(robot_id, event)
            )
            client.reminders = self.reminders
            def wake_external(robot_id=robot_id):
                self.events.wake(robot_id)
                self.reminders.wake(robot_id)

            client.event_activity = wake_external
        self.vision_intents = None
        if room_server is not None:
            from annie_adapters.playback import RoomEventListener

            for config in configs:
                if not config.enabled:
                    continue
                room = config.room_id
                listeners = list(self.playback_listeners.get(config.robot_id, ()))
                if "voice" in room_server.channels and config.robot_id in self.realtime:
                    listeners.append(RoomEventListener(
                        room_server, room, "voice",
                        lambda message, robot_id=config.robot_id:
                        self._room_voice_event(robot_id, message),
                    ))
                listeners.append(RoomEventListener(
                    room_server, room, "sam",
                    lambda message, robot_id=config.robot_id: self._room_sam_event(
                        robot_id, message),
                ))
                if config.robot_id in self.realtime:
                    for channel in ("main", "visualdata"):
                        self.bind_input_source(f"room:{room}:{channel}-event", [config.robot_id])
                    if not tts_voice:
                        listeners.append(RoomEventListener(
                            room_server, room, "main",
                            lambda message, robot_id=config.robot_id: self._room_external_event(
                                robot_id, "main", message),
                        ))
                    listeners.append(RoomEventListener(
                        room_server, room, "visualdata",
                        lambda message, robot_id=config.robot_id: self._room_external_event(
                            robot_id, "visualdata", message),
                    ))
                # Preserve the existing roomv2 listener; 8C needs independent
                # visual evidence for every enabled robot's own Room.
                if ((room == "roomv2" or
                     idle_model is not None and config.robot_id in self.realtime)
                        and "visualllm" in room_server.channels):
                    listeners.append(RoomEventListener(
                        room_server, room, "visualllm",
                        lambda message, robot_id=config.robot_id: self._room_visual_event(
                            robot_id, message),
                    ))
                self.playback_listeners[config.robot_id] = tuple(listeners)
                client = self.realtime.get(config.robot_id)
                if client is not None and any(item.channel == "visualllm" for item in listeners):
                    # look_closely: only robots with a camera feed get the tool.
                    client.visual_details = (
                        lambda robot_id=config.robot_id: self._current_view_details(robot_id))
            # "你看看左边有什么": a robot with a camera feed asks its vision service to look
            # now (docs/规范/视觉意图协议.md). The answer comes back on reb_main, which
            # reaches _room_playback_receipt only when TTS is configured.
            if vision_intent_enabled and tts_voice:
                from annie_adapters.tts import RoomTtsPublisher

                from annie_runtime.vision_intent import VisionIntents

                cameras = [robot_id for robot_id, client in self.realtime.items()
                           if client.visual_details is not None]
                self.vision_intents = VisionIntents(
                    {robot_id: (room_action_factory or RoomTtsPublisher)(
                        room_server, self.robots[robot_id].config.room_id, channel="main")
                     for robot_id in cameras},
                    route=vision_intent_route, deadline_ms=vision_intent_deadline_ms)
                for robot_id in cameras:
                    self.realtime[robot_id].vision_intents = self.vision_intents
                    self.realtime[robot_id].vision_action_extra_ms = vision_intent_action_extra_ms
            live_configs = [c for c in configs if c.enabled and c.listen_enabled]
            room_ids = [c.room_id for c in live_configs]
            if len(set(room_ids)) != len(room_ids):
                raise ValueError(
                    "live Room PCM requires a unique room_id per enabled listening robot; "
                    "MIC_AUDIO has no proven wire target field"
                )
            for config in live_configs:
                source_id = f"room:{config.room_id}:{room_server.mic_channel}"
                self.bind_input_source(source_id, [config.robot_id])
                self.room_sources[config.robot_id] = source_id
                self.room_connections[config.robot_id] = RoomPcmConnection(
                    room_server,
                    config.room_id,
                    lambda frame, robot_id=config.robot_id, source_id=source_id: self._ingest_live_room_pcm(
                        robot_id, source_id, frame
                    ),
                    lambda source_id=source_id: self._room_input_disconnected(source_id),
                )

    def bind_input_source(self, source_id: str, robot_ids, *, local=False):
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError("source_id must be a non-empty string")
        targets = tuple(dict.fromkeys(robot_ids))
        if not targets:
            raise ValueError("input source must bind at least one robot")
        missing = [robot_id for robot_id in targets if robot_id not in self.robots]
        if missing:
            raise ValueError(f"unknown robot_id in input binding: {missing[0]}")
        self.input_bindings[source_id] = targets
        if local:
            self.local_input_sources.add(source_id)
        else:
            self.local_input_sources.discard(source_id)
        return targets

    def _input_targets(self, source_id: str, *, target_robot_id=None, broadcast=False):
        if source_id in self.local_input_sources:
            raise InputRejectedError("loopback_source")
        targets = self.input_bindings.get(source_id)
        if targets is None:
            raise InputRejectedError("unknown_source")
        if broadcast and target_robot_id is not None:
            raise InputRejectedError("ambiguous_target")
        if target_robot_id is not None:
            if target_robot_id not in targets:
                raise InputRejectedError("target_not_bound")
            return (target_robot_id,)
        if broadcast:
            return targets
        if len(targets) != 1:
            raise InputRejectedError("ambiguous_target")
        return targets

    def start_pcm_stream(
        self,
        source_id: str,
        audio_format: AudioFormat,
        *,
        target_robot_id=None,
        broadcast=False,
    ):
        if audio_format != VERIFIED_MIC_FORMAT:
            raise InputRejectedError("unsupported_audio_format")
        targets = self._input_targets(
            source_id, target_robot_id=target_robot_id, broadcast=broadcast
        )
        for robot_id in targets:
            robot = self.robots[robot_id]
            if not robot.config.listen_enabled:
                raise InputRejectedError("listen_disabled")
        for robot_id in targets:
            robot = self.robots[robot_id]
            robot.audio.start_stream(source_id, audio_format)
            robot.snapshot.last_input_source = source_id
            robot.record("pcm_stream_started", source=source_id)
        return targets

    def ingest_mic_start(self, source_id: str, message: dict, *, target_robot_id=None, broadcast=False):
        return self.start_pcm_stream(
            source_id,
            parse_mic_start(message),
            target_robot_id=target_robot_id,
            broadcast=broadcast,
        )

    def ingest_pcm(
        self,
        source_id: str,
        frame: bytes,
        *,
        sequence: int | None = None,
        received_at: float | None = None,
        target_robot_id=None,
        broadcast=False,
    ):
        decoded = decode_room_binary_frame(frame)
        if decoded is None:
            raise InputRejectedError("invalid_binary_frame")
        message_type, payload = decoded
        if message_type != "MIC_AUDIO":
            raise InputRejectedError("unsupported_binary_type")
        targets = self._input_targets(
            source_id, target_robot_id=target_robot_id, broadcast=broadcast
        )
        received_at = time.monotonic() if received_at is None else received_at
        for robot_id in targets:
            robot = self.robots[robot_id]
            robot.audio.put(source_id, payload, sequence=sequence, received_at=received_at)
            robot.snapshot.last_input_source = source_id
        return targets

    def next_pcm_nowait(self, robot_id: str):
        robot = self.robots[robot_id]
        return robot.audio.next_nowait()

    def reconnect_input_source(self, source_id: str):
        targets = self.input_bindings.get(source_id)
        if targets is None:
            raise InputRejectedError("unknown_source")
        for robot_id in targets:
            robot = self.robots[robot_id]
            robot.audio.reset_source(source_id)
            robot.record("input_reconnected", source=source_id)
        return targets

    async def ingest_legacy_asr(
        self, source_id: str, message: dict, *, target_robot_id=None, broadcast=False
    ):
        event = normalize_room_event(message)
        if event is None or event.kind != "legacy_asr":
            raise InputRejectedError("invalid_legacy_asr")
        targets = self._input_targets(
            source_id, target_robot_id=target_robot_id, broadcast=broadcast
        )
        if any(self.robots[robot_id].audio.source_id == source_id for robot_id in targets):
            raise InputRejectedError("pcm_stream_active")
        results = await asyncio.gather(
            *(self.submit(robot_id, event.payload["text"]) for robot_id in targets)
        )
        for robot_id in targets:
            self.robots[robot_id].snapshot.last_input_source = source_id
        return dict(zip(targets, results, strict=True))

    def dispatch_room_event(
        self, source_id: str, message: dict, *, target_robot_id=None, broadcast=False
    ):
        event = normalize_room_event(message)
        if event is None or event.kind == "legacy_asr":
            raise InputRejectedError("unsupported_room_event")
        targets = self._input_targets(
            source_id, target_robot_id=target_robot_id, broadcast=broadcast
        )
        for robot_id in targets:
            robot = self.robots[robot_id]
            robot.snapshot.event_count += 1
            robot.snapshot.last_event_kind = event.kind
            robot.snapshot.last_input_source = source_id
            if event.kind not in _QUIET_INPUT_EVENTS:
                robot.record(f"input_event:{event.kind}", source=source_id)
            self.events.route(robot_id, event, source_id)
        return {robot_id: event for robot_id in targets}

    def input_stats(self, robot_id: str):
        robot = self.robots[robot_id]
        stats = robot.audio.stats()
        stats.update(
            event_count=robot.snapshot.event_count,
            last_event_kind=robot.snapshot.last_event_kind,
            last_input_source=robot.snapshot.last_input_source,
        )
        if robot_id in self.cloud_aec:
            stats["aec"] = self.cloud_aec[robot_id].snapshot()
        stats["audio_devices"] = self.audio_devices[robot_id].snapshot()
        return stats

    def debug_ingest_room_pcm(self, robot_id: str, source_id: str, frame: bytes):
        if robot_id not in self.robots:
            raise KeyError(robot_id)
        existing = self.input_bindings.get(source_id)
        if existing is None:
            self.bind_input_source(source_id, [robot_id])
        elif existing != (robot_id,):
            raise InputRejectedError("debug_source_conflict")
        robot = self.robots[robot_id]
        if robot.audio.source_id != source_id:
            self.start_pcm_stream(source_id, VERIFIED_MIC_FORMAT)
        self.ingest_pcm(source_id, frame, target_robot_id=robot_id)
        return self.input_stats(robot_id)

    def _ingest_live_room_pcm(self, robot_id: str, source_id: str, frame: bytes):
        if not self.started:
            return
        decoded = decode_room_binary_frame(frame)
        if decoded is None or decoded[0] != "MIC_AUDIO":
            self.robots[robot_id].record(
                "room_pcm_rejected",
                error="invalid_or_unsupported_binary_frame",
                source=source_id,
            )
            return
        robot = self.robots[robot_id]
        self.audio_devices[robot_id].mic_frame()
        aec = self.cloud_aec.get(robot_id)
        if aec is not None:
            # Native DSP never executes in the websocket reader. The worker
            # alone forwards cleaned PCM into the existing Room input gate.
            if not aec.feed_mic(decoded[1]):
                robot.record("cloud_aec_overloaded", source=source_id)
            return
        try:
            if robot.audio.source_id != source_id:
                self.start_pcm_stream(
                    source_id,
                    VERIFIED_MIC_FORMAT,
                    target_robot_id=robot_id,
                )
            self.ingest_pcm(source_id, frame, target_robot_id=robot_id)
        except InputRejectedError as exc:
            robot.record("room_pcm_rejected", error=exc.reason, source=source_id)

    def _accept_aec_pcm(self, robot_id, pcm, received_at):
        if not self.started:
            return
        source_id = self.room_sources[robot_id]
        try:
            if self.robots[robot_id].audio.source_id != source_id:
                self.start_pcm_stream(source_id, VERIFIED_MIC_FORMAT,
                                      target_robot_id=robot_id)
            name = b"MIC_AUDIO"
            frame = len(name).to_bytes(2, "big") + name + pcm
            self.ingest_pcm(source_id, frame, target_robot_id=robot_id,
                            received_at=received_at)
        except InputRejectedError as exc:
            self.robots[robot_id].record("room_pcm_rejected", error=exc.reason,
                                         source=source_id)

    def _room_input_disconnected(self, source_id: str):
        if not self.started:
            return
        for robot_id in self.input_bindings.get(source_id, ()):
            if robot_id in self.cloud_aec:
                self.cloud_aec[robot_id].reset()
        try:
            self.reconnect_input_source(source_id)
            for robot_id in self.input_bindings.get(source_id, ()):
                if robot_id in self.realtime:
                    asyncio.create_task(self.realtime[robot_id].restart())
        except InputRejectedError:
            logger.exception("failed to reset disconnected Room input source=%s", source_id)

    def _room_playback_receipt(self, robot_id, message):
        self._room_external_event(robot_id, "main", message)
        if str(message.get("type", "")).startswith("action.") and self.actions is not None:
            self.actions.on_receipt(robot_id, message)
            return
        if message.get("type") == "vision.intent.result":
            if self.vision_intents is not None:
                self.vision_intents.on_result(robot_id, message)
            return
        client = self.realtime.get(robot_id)
        if client is None:
            return
        if message.get("type") in {"asr", "ASR_RESULT"}:
            event = normalize_room_event(message)
            current = client.robot.turn_cache.get(client.turn_id) if client.turn_id else None
            if (event is not None and current is not None and
                    event.payload["text"] == current.get("user_text") and
                    time.time_ns() // 1_000_000 - current.get("created_ms", 0) < 10_000):
                self.smallbrain_links[robot_id] = {
                    "turn_id": client.turn_id,
                    "generation": client.robot.generation,
                    "asr_event_id": event.event_id,
                    "asr_turn_token": event.payload.get("asr_turn_token"),
                    "asr_from": event.source, "asr_ts": event.ts_ms,
                    "text": event.payload["text"], "cancelled": False,
                }
            return
        if message.get("type") == "cancel" and message.get("reason") == "action_prep":
            link = self.smallbrain_links.get(robot_id)
            matched, reason = match_cancel_identity(link or {}, message)
            if not (matched and client.ready and link["turn_id"] == client.turn_id and
                    link["generation"] == client.robot.generation):
                client.publish("smallbrain.cancel.ignored", reason=reason,
                               device_action_untouched=True)
                return
            link["cancelled"] = True

            async def apply_cancel():
                if client.turn_id != link["turn_id"] or client.robot.generation != link["generation"]:
                    return
                # Small brain owns its immediate motion. Stop our pending
                # semantic action + speech, NEVER send motion.stop to take
                # ownership of the small brain's action.
                try:
                    await client._interrupt_active_turn("action_prep")
                    client.publish("smallbrain.cancel.accepted",
                                   device_action_untouched=True)
                except Exception as exc:
                    client.publish("smallbrain.cancel.failed",
                                   reason=type(exc).__name__, device_action_untouched=True)

            task = asyncio.create_task(apply_cancel())
            self.smallbrain_tasks.add(task)
            task.add_done_callback(self.smallbrain_tasks.discard)
            return
        if str(message.get("type", "")) in PLAYBACK_RECEIPTS:
            try:
                # A second speaker's receipts are rejected below as stale;
                # they still prove that it is playing in this Room.
                self.audio_devices[robot_id].playback_receipt(playback_receipt(message))
            except ValueError:
                pass
        if client.speech is None or not str(message.get("type", "")).startswith("tts.playback."):
            return
        try:
            if not client.speech.on_playback_receipt(message):
                client.publish("tts.playback.rejected", reason="stale_or_invalid_state")
            elif (robot_id in self.cloud_aec and
                  (message.get("data") or {}).get("source") == "speaker"):
                self.cloud_aec[robot_id].feed_receipt(message)
        except ValueError as exc:
            client.publish("tts.playback.rejected", reason=str(exc)[:120])

    def _room_voice_event(self, robot_id, message):
        """reb_voice carries device status; microphone status never triggers ASR."""
        kind = message.get("type", "")
        self.audio_devices[robot_id].report(message)
        if isinstance(kind, str) and kind.startswith("tts.playback."):
            # Commands from Brain also pass through this subscription. Only
            # device reports are accepted; avoid treating our own hold as ACK.
            if kind in {"tts.playback.started", "tts.playback.progress",
                        "tts.playback.held", "tts.playback.resumed",
                        "tts.playback.stopped", "tts.playback.drained"}:
                self._room_playback_receipt(robot_id, message)
        elif kind in {"mic.capture.started", "mic.capture.stopped"}:
            data = message.get("data")
            if (isinstance(data, dict) and data.get("sender") == "microphone"
                    and robot_id in self.realtime):
                self.realtime[robot_id].publish(
                    "voice.microphone", event_kind=kind, source="microphone",
                )

    def _audio_devices_changed(self, robot_id, status, snapshot):
        """A second microphone/speaker appeared or went away: tell the screens."""
        reasons = ";".join(f"{item['role']}:{item['reason']}" for item in snapshot["conflicts"])
        robot = self.robots.get(robot_id)
        if robot is not None:
            robot.record("audio_devices_" + status, error=reasons or None, source="room_audio")
        client = self.realtime.get(robot_id) if hasattr(self, "realtime") else None
        if client is not None:
            client.publish("audio.devices." + status, conflicts=snapshot["conflicts"],
                           microphones=len(snapshot["microphones"]),
                           speakers=len(snapshot["speakers"]))

    def _robot_head_changed(self, robot_id, kind, details):
        """Which robot head (board) serves this Room: online, handover or offline."""
        robot = self.robots.get(robot_id)
        if kind == "handover":
            older, newer = details.get("from") or {}, details.get("to") or {}
            fields = {"from_name": older.get("head_name"), "from_address": older.get("address"),
                      "to_name": newer.get("head_name"), "to_address": newer.get("address")}
            note = f"{fields['to_name']}接管，{fields['from_name']}已停止"
        else:
            fields = {"head_name": details.get("head_name"), "address": details.get("address"),
                      "head_id": details.get("head_id")}
            note = None
        if robot is not None:
            # A handover means both boards were started: worth a warning line.
            robot.record("robot_head_" + kind, error=note, source="room_audio")
        client = self.realtime.get(robot_id) if hasattr(self, "realtime") else None
        if client is not None:
            client.publish("robot.head." + kind,
                           **{key: value for key, value in fields.items() if value is not None})

    def _send_voice_event(self, robot_id, kind, event):
        """Relay Qwen's observations asynchronously; it does not talk to Room."""
        publisher = self.voice_publishers.get(robot_id)
        if publisher is None:
            return
        if len(self.voice_tasks) >= 32:
            self.realtime[robot_id].publish("voice.signal.dropped", event_kind=kind)
            return

        async def send():
            try:
                labels = {
                    "speech.started": "检测到用户开始说话",
                    "speech.stopped": "检测到用户结束说话",
                    "transcript.done": "用户语音识别最终文本",
                    "transcript.final_preview": "用户语音识别待确认文本",
                    "transcript.ambient": "未触发对话的环境语音",
                    "turn.started": "机器人开始思考回复",
                    "tts.first_published": "机器人开始说话",
                }
                await publisher.publish({
                    "type": "voice.interaction",
                    "description": labels.get(kind, "语音交互事件"),
                    "ts": event["ts_ms"],
                    "data": {
                        "sender": "brain",
                        "trigger_source": ("qwen_realtime" if kind.startswith(
                            ("speech.", "transcript."),
                        ) else "brain"),
                        "event": kind, "robot_id": robot_id,
                        "item_id": event.get("item_id"),
                        "conversation_id": event.get("turn_id"),
                        "text": str(event.get("text") or "")[:256],
                        # Coarse state for the device's own micro-motions.
                        "interaction_state": {
                            "speech.started": "listening", "turn.started": "thinking",
                            "tts.first_published": "speaking", "transcript.ambient": "idle",
                        }.get(kind),
                    },
                })
            except Exception as exc:
                self.realtime[robot_id].publish(
                    "voice.signal.error", event_kind=kind,
                    error=type(exc).__name__,
                )

        task = asyncio.create_task(send())
        self.voice_tasks.add(task)
        task.add_done_callback(self.voice_tasks.discard)

    def _room_external_event(self, robot_id, channel, message):
        if message.get("type") not in {
            "pressure", "touch", "gas", "vision.perception", "robot_event",
            "face", "object", "gesture",
        }:
            return
        if self._repeated_perception(robot_id, message):
            return
        source = f"room:{self.robots[robot_id].config.room_id}:{channel}-event"
        try:
            self.dispatch_room_event(source, message, target_robot_id=robot_id)
        except InputRejectedError:
            logger.warning("Room external event rejected robot=%s channel=%s", robot_id, channel)

    def _repeated_perception(self, robot_id, message):
        """Vision streams face/gesture ~27 times a second, nearly always unchanged.

        Each one used to be a trace row and an INFO line while the policy only
        keeps it as context. Forward a change at once, a repeat every 5 s.
        """
        kind = message.get("kind") if message.get("type") == "vision.perception" \
            else message.get("type")
        if kind == "gesture":
            gesture = message.get("gesture") or {}
            signature = (gesture.get("state"), gesture.get("name"), gesture.get("gesture")) \
                if isinstance(gesture, dict) else None
        elif kind == "face":
            tracks = message.get("tracks")
            signature = tuple(sorted(
                (str(item.get("name")), str(item.get("expression")))
                for item in tracks if isinstance(item, dict))) if isinstance(tracks, list) else None
        else:
            return False
        now = time.monotonic()
        last = self._perception_last.get((robot_id, kind))
        if last is not None and last[0] == signature and now - last[1] < PERCEPTION_REPEAT_SECONDS:
            return True
        self._perception_last[(robot_id, kind)] = (signature, now)
        return False

    def _room_sam_event(self, robot_id, message):
        client = self.realtime.get(robot_id)
        speech = client.speech if client is not None else None
        mouth = message.get("type") == "mouth_script" and speech is not None
        timing = speech.mouth_timing(message, time.monotonic()) if mouth else None
        self.room_monitors[robot_id].on_sam(message, timing=timing)
        if mouth:
            speech.on_mouth_script(message)

    def _room_visual_event(self, robot_id, message):
        monitor = self.room_monitors[robot_id]
        if monitor.on_visual(message):
            self.idle.observe_visual(
                robot_id,
                message if monitor.visual_facts_accepted else {},
            )
            transport = f"room:{monitor.room_id}:visualllm"
            for arrival in monitor.known_arrivals:
                # Settled by the runtime's smoothing; the policy decides to greet.
                self.events.route(robot_id, NormalizedEvent(
                    kind="vision.known_arrival", source="vision",
                    payload={"id": arrival["subject_id"], "name": arrival["name"]},
                    ts_ms=time.time_ns() // 1_000_000, ttl_ms=10_000), transport)
            client = self.realtime.get(robot_id)
            if client is not None:
                client.visual_changed()

    def _current_visual(self, robot_id):
        listeners = self.playback_listeners.get(robot_id, ())
        visual = next((item for item in listeners if item.channel == "visualllm"), None)
        return self.room_monitors[robot_id].current_visual(
            connected=bool(visual and visual.connected),
        )

    def _current_view_details(self, robot_id):
        listeners = self.playback_listeners.get(robot_id, ())
        visual = next((item for item in listeners if item.channel == "visualllm"), None)
        if not (visual and visual.connected):
            return None
        return self.room_monitors[robot_id].view_details()

    def room_observations(self, robot_id):
        monitor = self.room_monitors[robot_id]
        listeners = self.playback_listeners.get(robot_id, ())
        sam = next((item for item in listeners if item.channel == "sam"), None)
        visual = next((item for item in listeners if item.channel == "visualllm"), None)
        snapshot = monitor.snapshot(
            sam_connected=sam.connected if sam else None,
            visual_connected=bool(visual and visual.connected),
            visual_configured=visual is not None,
        )
        client = self.realtime.get(robot_id)
        snapshot["visual"]["llm_context"] = (
            client.visual_context_snapshot() if client else {"state": "not_configured"}
        )
        return snapshot

    def room_routes(self):
        routes = []
        for robot_id, connection in self.room_connections.items():
            robot = self.robots[robot_id]
            status = connection.snapshot()
            stats = self.input_stats(robot_id)
            routes.append(
                {
                    "robot_id": robot_id,
                    "room_id": robot.config.room_id,
                    "urls": self.room_server.urls_for_room(robot.config.room_id),
                    "mic_url": status["url"],
                    "source_id": self.room_sources[robot_id],
                    "state": status["state"],
                    "connected": status["connected"],
                    "subscribed": status["subscribed"],
                    "frames_received": status["frames_received"],
                    "accepted": stats["accepted"],
                    "queue_depth": stats["queue_depth"],
                    "last_error": status["last_error"],
                }
            )
        return routes

    def preflight(self):
        self.store.preflight()

    def _memory_changed(self, robot_id):
        client = self.realtime.get(robot_id)
        if client is not None:
            client.memory_changed()

    async def _warm_decision(self):
        try:
            await self.decision.warm()
        except Exception as exc:  # the first real question will report failures
            logger.info("decision_warm_failed error=%s", type(exc).__name__)

    async def start(self):
        if self.started:
            return
        for aec in self.cloud_aec.values():
            aec.loop = asyncio.get_running_loop()
            aec.start()
        await asyncio.to_thread(self.store.migrate)
        await asyncio.to_thread(self.speakers.reload)
        if self.voices is not None:
            await self.voices.start()
        if self.actions is not None:
            await self.actions.start()
        self.ledger.start()
        self.recorder.start()
        self.started = True
        self.process_probe.start()
        if self.memory:
            await self.memory.start()
        for robot in self.robots.values():
            await robot.initialize_session()
            await robot.start()
        await self.reminders.start()
        await self.idle.start()
        if self.inner_life is not None:
            await self.inner_life.start()
        for realtime in self.realtime.values():
            realtime.start()
        if self.decision is not None and self._owns_decision:
            self._decision_warm = asyncio.create_task(self._warm_decision())
        if self.d3 is not None and self._owns_d3:
            self.d3.start()
        for connection in self.room_connections.values():
            connection.start()
        for connections in self.playback_listeners.values():
            for connection in connections:
                connection.start()
        self._viseme_probe = asyncio.create_task(self._probe_viseme())

    def viseme_status(self):
        """External Windows tts_viseme liveness per Room, from its reb_sam viseme_health."""
        status = {}
        for robot_id, monitor in self.room_monitors.items():
            sam = next((item for item in self.playback_listeners.get(robot_id, ())
                        if item.channel == "sam"), None)
            viseme = monitor.snapshot(
                sam_connected=sam.connected if sam else None,
                visual_connected=False, visual_configured=False,
            )["viseme"]
            status[robot_id] = {"room_id": monitor.room_id, "state": viseme["state"],
                                "heartbeat_age_ms": viseme["heartbeat_age_ms"],
                                "reason": viseme["reason"]}
        return status

    async def _probe_viseme(self, timeout=10.0, interval=0.5):
        """Startup check: the mouth service is external, so report whether it heartbeats."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            status = self.viseme_status()
            if (loop.time() >= deadline or not any(
                    item["state"] in ("waiting", "disconnected") for item in status.values())):
                break
            await asyncio.sleep(interval)
        for robot_id, item in status.items():
            if item["state"] == "not_configured":
                continue
            if item["state"] == "online":
                logger.info("嘴型服务在线 robot=%s room=%s heartbeat_age_ms=%s",
                            robot_id, item["room_id"], item["heartbeat_age_ms"])
            else:
                logger.warning(
                    "嘴型服务未在线 robot=%s room=%s state=%s reason=%s：%.0f 秒内未收到可用 "
                    "viseme_health，请确认 Windows tts_viseme 已启动并监听该房间",
                    robot_id, item["room_id"], item["state"], item["reason"], timeout,
                )

    async def wait_room_gateway(self, timeout: float) -> bool:
        if not self.room_connections:
            return True
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if all(connection.snapshot()["subscribed"] for connection in self.room_connections.values()):
                return True
            await asyncio.sleep(0.05)
        return all(connection.snapshot()["subscribed"] for connection in self.room_connections.values())

    async def close(self):
        await self.process_probe.close()
        for aec in self.cloud_aec.values():
            aec.close()
        if self._viseme_probe is not None:
            self._viseme_probe.cancel()
            self._viseme_probe = None
        if self.started:
            self.started = False
            for task in tuple(self.smallbrain_tasks):
                task.cancel()
            await asyncio.gather(*self.smallbrain_tasks, return_exceptions=True)
            self.smallbrain_tasks.clear()
            await self.reminders.close()
            await self.idle.close()
            await self.events.close()
            if self.inner_life is not None:
                await self.inner_life.close()
            if self.motions is not None:
                await self.motions.close()
            for realtime in self.realtime.values():
                await realtime.stop()
            if self.voices is not None:
                await self.voices.close()
            for task in tuple(self.voice_tasks):
                task.cancel()
            await asyncio.gather(*self.voice_tasks, return_exceptions=True)
            self.voice_tasks.clear()
            for publisher in self.voice_publishers.values():
                await publisher.close()
            if self.actions is not None:
                await self.actions.close()
            if self.vision_intents is not None:
                await self.vision_intents.close()
            if self.weather is not None:
                await self.weather.close()
            if self.faces is not None:
                await self.faces.close()
            if self.decision is not None and self._owns_decision:
                await self.decision.close()
            if self.d3 is not None and self._owns_d3:
                await self.d3.close()
            if self.addressee_judge is not None:
                await self.addressee_judge.close()
            if self.memory:
                await self.memory.close()
            for connections in self.playback_listeners.values():
                await asyncio.gather(*(connection.close() for connection in connections))
            if self.room_connections:
                await asyncio.gather(*(connection.stop() for connection in self.room_connections.values()))
            for robot in self.robots.values():
                await robot.stop()
            ledger_error = None
            try:
                await self.ledger.close()
            except Exception as exc:
                ledger_error = exc
            await self.recorder.close()
            if ledger_error is not None:
                raise ledger_error
        for box in self.display_outboxes.values():
            await box.close()

    async def submit(self, robot_id, text, *, fail=False):
        return await self.robots[robot_id].submit(text, fail=fail)

    async def cancel(self, robot_id):
        if robot_id in self.realtime:
            await self.realtime[robot_id].cancel()
            await self.realtime[robot_id].restart()
        return await self.robots[robot_id].cancel()

    def session_state(self, robot_id: str):
        return self.robots[robot_id].session_state()

    def voice_library(self, robot_id):
        identity = self.session_state(robot_id)
        if self.voices is None:
            return {"available": False, "voices": [], "previews": []}
        return {"available": True, **self.voices.snapshot(identity)}

    async def clone_voice(self, robot_id, name, filename, audio, language="zh"):
        if self.voices is None:
            raise ValueError("TTS voice cloning is not configured")
        from annie_runtime.voice_profiles import check_audio
        check_audio(filename, audio)
        result = await self.voices.clone(name, filename, audio, language=language)
        for client in self.realtime.values():
            client.visual_changed()
        return result

    def speaker_catalog(self):
        return self.speakers.catalog()

    async def enroll_speaker(self, name, audio):
        return await asyncio.to_thread(self.speakers.enroll, name, audio)

    async def delete_speaker(self, speaker_id):
        removed = await asyncio.to_thread(self.speakers.delete, speaker_id)
        if removed is None:
            raise KeyError(speaker_id)
        for client in self.realtime.values():
            tracker = getattr(client, "speakers", None)
            if tracker is not None and tracker.forget(removed["speaker_id"]):
                await client.note_speaker()
        return {"status": "deleted", **removed}

    async def select_voice(self, robot_id, voice_id, scope="chat", expected_identity=None):
        if self.voices is None:
            raise ValueError("TTS voices are not configured")
        identity = self.session_state(robot_id)
        if expected_identity and any(
                expected_identity.get(k) != identity[k]
                for k in ("chat_id", "persona_id", "session_epoch", "generation")):
            raise ValueError("stale_voice_session")
        result = await self.voices.select(identity, voice_id, scope)
        client = self.realtime.get(robot_id)
        if client:
            client.publish("tts.voice.selected", voice_id=result["active_voice_id"],
                           scope=scope, effective="next_tts_task")
            client.visual_changed()
        return {**result, **self.voice_library(robot_id)}

    async def rename_voice(self, robot_id, voice_id, name):
        if self.voices is None:
            raise ValueError("TTS voices are not configured")
        result = await self.voices.rename(voice_id, name)
        for client in self.realtime.values():
            client.visual_changed()
        return result

    async def delete_voice(self, robot_id, voice_id):
        if self.voices is None:
            raise ValueError("TTS voices are not configured")
        result = await self.voices.delete(voice_id)
        for client in self.realtime.values():
            client.visual_changed()
        return result

    async def list_persona_instances(self, robot_id):
        return await asyncio.to_thread(self.store.list_persona_instances, robot_id)

    async def name_persona_instance(self, robot_id, persona_id, display_name):
        if (not isinstance(persona_id, str) or not 1 <= len(persona_id.strip()) <= 128
                or not isinstance(display_name, str) or not 1 <= len(display_name.strip()) <= 40):
            raise ValueError("invalid Persona id or display name")
        return await asyncio.to_thread(self.store.set_persona_name, robot_id,
                                       persona_id.strip(), display_name.strip())

    async def switch_session(
        self,
        robot_id: str,
        *,
        chat_id: str,
        persona_id: str,
        persona_revision: int = 1,
        style_id: str | None = None,
    ):
        if style_id is not None and style_id not in self.persona_profiles:
            raise ValueError("unknown style_id")
        previous = self.robots[robot_id].session_state()
        result = await self.robots[robot_id].switch_session(
            chat_id=chat_id,
            persona_id=persona_id,
            persona_revision=persona_revision,
        )
        if style_id is not None:
            await asyncio.to_thread(
                self.store.upsert_social_state,
                robot_id, result["chat_id"], result["persona_id"],
                "persona_style", style_id,
                source="user_setting", source_turn_id=None,
                generation=result["generation"], expires_at_ms=None,
                updated_ms=time.time_ns() // 1_000_000,
            )
        if result != previous:
            if robot_id in self.cloud_aec:
                self.cloud_aec[robot_id].reset()
            try:
                await self.reminders.invalidate_previous(previous)
            except Exception:
                logger.exception("reminder session invalidation failed robot=%s", robot_id)
        if robot_id in self.realtime and (result != previous or style_id is not None):
            await self.realtime[robot_id].restart()
        if result != previous:
            self.events.wake(robot_id)
            self.reminders.wake(robot_id)
        return result

    async def list_conversations(self, robot_id: str):
        await self.ledger.flush()
        return await asyncio.to_thread(self.store.list_conversations, robot_id)

    async def load_conversation(self, robot_id: str, chat_id: str, persona_id: str,
                                *, limit: int | None = None, before_id: int | None = None):
        await self.ledger.flush()
        if limit is None:
            page = {"turns": await asyncio.to_thread(
                self.store.load_conversation, robot_id, chat_id, persona_id)}
        else:
            page = await asyncio.to_thread(
                self.store.load_conversation_page, robot_id, chat_id, persona_id,
                limit=limit, before_id=before_id,
            )
        return {
            "robot_id": robot_id,
            "chat_id": chat_id,
            "persona_id": persona_id,
            **page,
        }

    async def recent_context(self, robot_id: str):
        return self.robots[robot_id].recent_context()

    async def get_turn(self, robot_id: str, turn_id: str):
        cached = self.robots[robot_id].cached_turn(turn_id)
        if cached is not None:
            return cached
        await self.ledger.flush()
        turn = await asyncio.to_thread(self.store.get_conversation_turn, robot_id, turn_id)
        if turn is None:
            raise KeyError(turn_id)
        self.robots[robot_id]._remember_turn(dict(turn))
        return turn

    async def delete_conversation(self, robot_id: str, chat_id: str, persona_id: str):
        robot = self.robots[robot_id]
        if robot.chat_id == chat_id and robot.persona_id == persona_id:
            deleted = await robot.delete_current_conversation()
            if robot_id in self.realtime:
                await self.realtime[robot_id].restart()
        else:
            await self.ledger.flush()
            deleted = await asyncio.to_thread(
                self.store.delete_conversation,
                robot_id,
                chat_id,
                persona_id,
            )
            robot.turn_cache = {
                turn_id: turn
                for turn_id, turn in robot.turn_cache.items()
                if not (turn["chat_id"] == chat_id and turn["persona_id"] == persona_id)
            }
        if self.inner_life is not None:
            self.inner_life.on_chat_deleted(robot_id, chat_id)
        return {
            "robot_id": robot_id,
            "chat_id": chat_id,
            "persona_id": persona_id,
            "deleted_turns": deleted,
        }

    async def update_playback(
        self,
        robot_id: str,
        turn_id: str,
        *,
        published_text: str | None,
        spoken_text: str | None,
        evidence_status: str,
        playback_state: str,
    ):
        robot = self.robots[robot_id]
        robot.coordinator.set_playback_state(playback_state)
        self.events.wake(robot_id)
        self.reminders.wake(robot_id)
        updated_ms = time.time_ns() // 1_000_000
        if robot.cached_turn(turn_id) is None:
            await self.ledger.flush()
            stored = await asyncio.to_thread(self.store.get_conversation_turn, robot_id, turn_id)
            if stored is None:
                raise KeyError(turn_id)
            robot._remember_turn(dict(stored))
        self.ledger.enqueue(
            "update_playback",
            robot_id,
            turn_id,
            published_text=published_text,
            spoken_text=spoken_text,
            evidence_status=evidence_status,
            updated_ms=updated_ms,
        )
        return robot.update_cached_playback(
            turn_id,
            published_text=published_text,
            spoken_text=spoken_text,
            evidence_status=evidence_status,
            updated_ms=updated_ms,
        )

    async def set_playback_state(self, robot_id: str, state: str):
        self.robots[robot_id].coordinator.set_playback_state(state)
        self.events.wake(robot_id)
        self.reminders.wake(robot_id)
        return self.coordinator_state(robot_id)

    def request_response(self, robot_id: str, *, source: str, dedupe_key: str, ttl_ms: int):
        robot = self.robots[robot_id]
        return robot.coordinator.request(
            source=source,
            dedupe_key=dedupe_key,
            ttl_ms=ttl_ms,
            model_busy=robot.turn_task is not None,
        )

    def coordinator_state(self, robot_id: str):
        robot = self.robots[robot_id]
        return robot.coordinator.snapshot(model_busy=robot.turn_task is not None)

    async def update_social_state(
        self,
        robot_id: str,
        fields: dict,
        *,
        source: str,
        generation: int,
        source_turn_id: str | None = None,
        ttl_ms: int | None = None,
    ):
        robot = self.robots[robot_id]
        if generation != robot.generation:
            raise ValueError("stale_generation")
        if not isinstance(fields, dict) or not fields:
            raise ValueError("social state fields required")
        unknown = set(fields) - robot.SOCIAL_STATE_KEYS
        if unknown:
            raise ValueError(f"unknown social state key: {sorted(unknown)[0]}")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("social state source required")
        if source == "user_setting":
            (key, value), = fields.items() if len(fields) == 1 else ((None, None),)
            if not (
                (key == "persona_style" and isinstance(value, str)
                 and value in self.persona_profiles)
                or (key in CHAT_PREFERENCES and value in CHAT_PREFERENCES[key])
                or (key == "home_city" and isinstance(value, str)
                    and re.fullmatch(r"[\u4e00-\u9fffA-Za-z]{2,20}", value))
            ):
                raise ValueError("user_setting requires one configured persona_style or chat preference")
            if ttl_ms is not None:
                raise ValueError("user_setting cannot expire")
            client = self.realtime.get(robot_id)
            if ("question_pressure" in fields and client and
                    (client.turn_id or client.response_id or client.speech_active)):
                raise ValueError("Realtime busy; set conversation preference after current turn")
        if ttl_ms is not None and (type(ttl_ms) is not int or not 1 <= ttl_ms <= 86_400_000):
            raise ValueError("ttl_ms must be between 1 and 86400000")
        identity = robot.session_state()
        now_ms = time.time_ns() // 1_000_000
        if source != "user_setting" and ttl_ms is None:
            ttl_ms = 120_000  # observed interaction cues are not permanent user preferences
        expires_at_ms = None if ttl_ms is None else now_ms + ttl_ms
        for key, value in fields.items():
            await asyncio.to_thread(
                self.store.upsert_social_state,
                robot_id,
                identity["chat_id"],
                identity["persona_id"],
                key,
                value,
                source=source,
                source_turn_id=source_turn_id,
                generation=generation,
                expires_at_ms=expires_at_ms,
                updated_ms=now_ms,
            )
        if identity != robot.session_state():
            raise ValueError("session changed while updating social state")
        if source != "user_setting" and robot_id in self.realtime:
            client = self.realtime[robot_id]
            social = await self.social_state(robot_id)
            if identity == robot.session_state():
                client.social_state = social
                client.visual_changed()
        if source == "user_setting" and robot_id in self.realtime:
            # User-initiated setting, not a per-turn SocialState update. Apply
            # on a newly created Session without injecting full Persona midturn.
            await self.realtime[robot_id].restart()
        return await self.social_state(robot_id)

    async def social_state(self, robot_id: str):
        robot = self.robots[robot_id]
        return await asyncio.to_thread(
            self.store.load_social_state,
            robot_id,
            robot.chat_id,
            robot.persona_id,
            time.time_ns() // 1_000_000,
        )

    async def stop_robot(self, robot_id):
        if robot_id in self.cloud_aec:
            self.cloud_aec[robot_id].reset()
        if robot_id in self.realtime:
            await self.realtime[robot_id].stop()
        return await self.robots[robot_id].stop()

    async def start_robot(self, robot_id):
        if not self.started:
            raise RobotUnavailableError("runtime is closed")
        await self.robots[robot_id].start()
        if robot_id in self.realtime:
            self.realtime[robot_id].start()
        return self.robots[robot_id].snapshot.to_dict()

    async def realtime_text(self, robot_id: str, text: str):
        if robot_id not in self.realtime:
            raise RobotUnavailableError("Realtime is not configured; set DASHSCOPE_API_KEY")
        return await self.realtime[robot_id].submit_text(text)

    def viewed_text_turn(self, robot_id: str, turn_id: str, identity: dict):
        if robot_id not in self.realtime:
            raise RobotUnavailableError("Realtime is not configured")
        return self.idle.short.viewed_text(robot_id, turn_id, identity)

    async def action_list(self, robot_id: str):
        if robot_id not in self.robots:
            raise KeyError(robot_id)
        if self.actions is None:
            return {"status": "not_configured", "enabled": False,
                    "allowlist": [], "actions": [], "requests": []}
        return {
            "status": "ready" if self.actions.enabled else "disabled",
            "enabled": self.actions.enabled,
            "allowlist": list(self.actions.allowlist),
            "actions": list(self.actions.catalog.values()),
            "requests": await self.actions.list(robot_id),
            "transport_error": self.actions.connection_errors.get(robot_id),
        }

    async def memory_list(self, robot_id, persona_id, include_deleted=False):
        if self.memory is None:
            return {"status": "not_configured", "memories": []}
        if robot_id not in self.memory.robot_ids:
            raise RobotUnavailableError("robot memory not enabled")
        return {"status": "degraded" if robot_id in self.memory.errors else "ready",
                "memories": await self.memory.list(
            robot_id, persona_id, include_deleted,
        )}

    async def memory_put(self, robot_id, persona_id, kind, key, text):
        if self.memory is None:
            raise RobotUnavailableError("memory not configured")
        if robot_id not in self.memory.robot_ids:
            raise RobotUnavailableError("robot memory not enabled")
        result = await self.memory.put(robot_id, persona_id, kind, key, text)
        return {"status": "active", "id": result[0], "version": result[1]}

    async def memory_change(self, robot_id, persona_id, ident, version, text=None):
        if self.memory is None:
            raise RobotUnavailableError("memory not configured")
        if robot_id not in self.memory.robot_ids:
            raise RobotUnavailableError("robot memory not enabled")
        return await self.memory.change(robot_id, persona_id, ident, version, text)

    async def chat_memory_list(self, robot_id, include_deleted=False):
        if self.memory is None:
            return {"status": "not_configured", "memories": []}
        robot = self.robots[robot_id]
        return {"status": "degraded" if robot_id in self.memory.errors else "ready",
                "memories": await self.memory.chat_list(
                    robot_id, robot.persona_id, robot.chat_id, include_deleted,
                )}

    def _check_chat_memory_identity(self, robot_id, chat_id, persona_id, generation):
        identity = self.robots[robot_id].session_state()
        if (identity["chat_id"] != chat_id or identity["persona_id"] != persona_id
                or identity["generation"] != generation):
            raise ValueError("stale_chat_memory_session")
        return identity

    async def chat_memory_put(self, robot_id, key, text, expires_at_ms=None, *,
                              chat_id, persona_id, generation):
        if self.memory is None:
            raise RobotUnavailableError("memory not configured")
        robot = self.robots[robot_id]
        identity = self._check_chat_memory_identity(
            robot_id, chat_id, persona_id, generation,
        )
        result = await self.memory.chat_put(
            robot_id, identity["persona_id"], identity["chat_id"], key, text,
            expires_at_ms=expires_at_ms,
        )
        if identity != robot.session_state():
            raise ValueError("session changed while saving; check original Chat")
        return {"status": "active", "id": result[0], "version": result[1]}

    async def chat_memory_change(self, robot_id, ident, version, text=None, *,
                                 chat_id, persona_id, generation):
        if self.memory is None:
            raise RobotUnavailableError("memory not configured")
        robot = self.robots[robot_id]
        identity = self._check_chat_memory_identity(
            robot_id, chat_id, persona_id, generation,
        )
        result = await self.memory.chat_change(
            robot_id, identity["persona_id"], identity["chat_id"], ident, version, text,
        )
        if identity != robot.session_state():
            raise ValueError("session changed while modifying; check original Chat")
        return result

    async def speech_expression_state(self, robot_id):
        client = self.realtime.get(robot_id)
        if client:
            return client.speech_expression_snapshot()
        return {**self.robots[robot_id].session_state(), 'enabled': False,
                'sidecar': {'state': 'not_configured'}, 'speech': {'state': 'not_configured'},
                'language': {'input': None, 'application': 'unknown',
                    'target': {'language': 'zh', 'dialect': 'unknown', 'source': 'runtime_default'},
                    'llm_decision': None,
                    'preferences': await asyncio.to_thread(self.store.load_language_preferences,
                        robot_id, self.robots[robot_id].chat_id),
                    'llm_actual_text_language': 'unknown', 'tts_audibly_verified_language': 'unknown'}}

    async def set_acoustic_enabled(self, robot_id, enabled, *, expected_identity):
        self._check_language_identity(robot_id, expected_identity)
        if type(enabled) is not bool:
            raise ValueError('enabled must be boolean')
        client = self.realtime.get(robot_id)
        if client is None or client.acoustic is None:
            raise RobotUnavailableError('Realtime not configured')
        client.acoustic.set_enabled(enabled)
        client.invalidate_acoustic('sidecar_toggle', reset_policy=True)
        client.visual_changed()
        return client.speech_expression_snapshot()

    def _check_language_identity(self, robot_id, expected_identity):
        identity = self.robots[robot_id].session_state()
        if expected_identity is None or any(expected_identity.get(k) != identity[k] for k in (
                'chat_id', 'persona_id', 'generation', 'session_epoch')):
            raise RobotUnavailableError('stale language preference identity')
        return identity

    async def set_language_preference(self, robot_id, *, scope, language, dialect,
                                     request_id, expected_identity):
        return await self._write_language_preference(robot_id, clear=False, scope=scope,
            language=language, dialect=dialect, request_id=request_id,
            expected_identity=expected_identity)

    async def clear_language_preference(self, robot_id, *, scope, request_id, expected_identity):
        return await self._write_language_preference(robot_id, clear=True, scope=scope,
            request_id=request_id, expected_identity=expected_identity)

    async def _write_language_preference(self, robot_id, *, clear, scope, request_id,
                                        expected_identity, source_turn_id=None,
                                        language=None, dialect=None, validate_current=None):
        robot = self.robots[robot_id]
        # Session switching and writes serialize only this robot's short SQLite transaction.
        async with robot.session_lock:
            identity = self._check_language_identity(robot_id, expected_identity)
            if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
                raise ValueError('request_id must be 1..128 characters')
            method = self.store.clear_language_preference if clear else self.store.set_language_preference
            kwargs = dict(scope=scope, source_turn_id=source_turn_id,
                          call_id=request_id, updated_ms=time.time_ns()//1_000_000)
            if not clear:
                kwargs.update(language=language, dialect=dialect)
            def write():
                self._check_language_identity(robot_id, identity)
                if validate_current:
                    validate_current()
                    kwargs['validate_current'] = validate_current
                return method(robot_id, identity['chat_id'], **kwargs)
            task = asyncio.create_task(asyncio.to_thread(write))
            try:
                result = await asyncio.shield(task)
            except asyncio.CancelledError:
                # Keep session lock until the transaction commits/rolls back, even if a tool is cancelled.
                await task
                raise
            preferences = await asyncio.to_thread(self.store.load_language_preferences,
                                                  robot_id, identity['chat_id'])
            client = self.realtime.get(robot_id)
            if client and robot.session_state() == identity:
                client.language.load_preferences(preferences)
                client.visual_changed()
                client.publish('language.preference.changed', scope=scope,
                               revision=result['revision'], status=result['status'])
            return result

    def realtime_state(self, robot_id: str):
        if robot_id not in self.realtime:
            return {**self.session_state(robot_id), "ready": False, "state": "not_configured"}
        return self.realtime[robot_id].snapshot()

    def realtime_diagnostics(self, robot_id: str):
        if robot_id not in self.realtime:
            return {**self.session_state(robot_id), "ready": False,
                    "prompt_snapshot": None, "prompt_history": [],
                    "turn_prompt_refs": [], "tool_history": [],
                    "idle_planning": self.idle.snapshot(robot_id)}
        return {**self.realtime[robot_id].diagnostic_snapshot(),
                "idle_planning": self.idle.snapshot(robot_id)}

    def realtime_events(self, robot_id: str, after: int):
        if robot_id not in self.realtime:
            return {"events": [], "latest": 0, "gap": False}
        return self.realtime[robot_id].since(after)

    def external_events(self, robot_id: str):
        return {**self.events.snapshot(robot_id), "idle": self.idle.snapshot(robot_id)}

    async def reminder_list(self, robot_id):
        return await self.reminders.list(robot_id, self.session_state(robot_id))

    async def reminder_create(self, robot_id, text, delay_ms, request_key):
        return await self.reminders.create(robot_id, text, delay_ms, request_key)

    async def reminder_change(self, robot_id, task_id, action):
        return await self.reminders.change(robot_id, task_id, action)

    def health(self):
        snapshots = []
        for robot in self.robots.values():
            robot.snapshot.queue_depth = robot.queue.qsize()
            robot.sync_input_snapshot()
            snapshots.append(robot.snapshot.to_dict())
        active = sum(s["state"] in ("idle", "running") for s in snapshots)
        expected_active = sum(robot.config.enabled for robot in self.robots.values())
        local_ready = (
            self.started
            and self.recorder.last_error is None
            and self.ledger.last_error is None
            and active > 0
        )
        room_status = {
            robot_id: connection.snapshot()
            for robot_id, connection in self.room_connections.items()
        }
        room_required = bool(room_status)
        room_ready = room_required and all(status["subscribed"] for status in room_status.values())
        ready = local_ready and (not room_required or room_ready)
        if not room_required:
            room_capability = "protocol_ready_not_connected"
        elif room_ready:
            room_capability = "connected"
        elif any(status["connected"] for status in room_status.values()):
            room_capability = "partial"
        else:
            room_capability = "connecting"
        visemes = self.viseme_status()
        return {
            "project": "Annie Harbor",
            "phase": 4 if self.realtime else 3,
            "mode": (
                "realtime_text_live_room_pcm" if self.realtime else
                "fake_brain_session_live_room_pcm" if room_required else "fake_brain_session_control"
            ),
            "pid": os.getpid(),
            "ready": ready,
            "status": "healthy"
            if ready and active == expected_active
            else "degraded"
            if ready
            else "not_ready",
            "robots": [
                {
                    **item,
                    "realtime": (
                        self.realtime[item["robot_id"]].snapshot()
                        if item["robot_id"] in self.realtime
                        else {"ready": False, "state": "not_configured"}
                    ),
                }
                for item in snapshots
            ],
            "storage": {
                "schema_version": self.store.SCHEMA_VERSION,
                "error": self.recorder.last_error,
                "trace_dropped": self.recorder.dropped,
                "trace_queue_depth": self.recorder.queue.qsize(),
                "trace_queue_capacity": self.recorder.queue.maxsize,
                "ledger_error": self.ledger.last_error,
                "ledger_queue_depth": self.ledger.queue.qsize(),
                "ledger_queue_capacity": self.ledger.queue.maxsize,
            },
            "performance": self.process_probe.snapshot(),
            "memory": {
                "configured": self.memory is not None,
                "running": bool(self.memory and self.memory.running),
                "errors": dict(self.memory.errors) if self.memory else {},
                "backend": "sqlite_vec_versioned",
                "extraction": "langmem_alibaba_cloud" if self.memory else "not_configured",
                "recall": "on_demand_only",
            },
            "actions": {
                "configured": self.actions is not None,
                "enabled": bool(self.actions and self.actions.enabled),
                "allowlist": list(self.actions.allowlist) if self.actions else [],
                "device_completion": "embedded_receipts_v1",
                "receipts": dict(self.actions.receipts) if self.actions else {},
                "transport_errors": dict(self.actions.connection_errors) if self.actions else {},
            },
            "weather": {"configured": self.weather is not None, "source": "amap"},
            "faces": {"configured_robots": sorted(self.faces.registries) if self.faces else []},
            "vision_intent": {
                "configured_robots": (sorted(self.vision_intents.publishers)
                                      if self.vision_intents else []),
                "route": self.vision_intents.route if self.vision_intents else None,
            },
            "room_gateway": {
                "configured": room_required,
                "ready": room_ready,
                "connections": room_status,
            },
            # Mouth alignment runs as the standalone Windows tts_viseme service.
            "viseme": visemes,
            "capabilities": {
                "brain": "fake",
                "room": room_capability,
                "pcm_input": "room_gateway_ready" if room_ready else "gate_ready",
                "conversation": "ledger_ready" if self.ledger.last_error is None else "ledger_failed",
                "tts": (
                    "published_unconfirmed" if any(
                        client.speech and client.speech.frames for client in self.realtime.values()
                    ) else "configured_not_verified" if any(
                        client.speech for client in self.realtime.values()
                    ) else "not_connected"
                ),
                "viseme": (
                    "aligner_reporting" if any(
                        client.speech and client.speech.viseme_events
                        for client in self.realtime.values()
                    ) else "external_online" if any(
                        item["state"] == "online" for item in visemes.values()
                    ) else "external_offline" if any(
                        item["state"] != "not_configured" for item in visemes.values()
                    ) else "not_connected"
                ),
                "llm": (
                    "connected" if self.realtime and all(client.ready for client in self.realtime.values())
                    else "partial" if any(client.ready for client in self.realtime.values())
                    else "connecting" if self.realtime else "not_connected"
                ),
                "memory": "not_started",
            },
        }
