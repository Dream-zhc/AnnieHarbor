"""One process with isolated Phase 3 sessions, ledger, and bounded PCM input gates."""

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from annie_adapters import (
    VERIFIED_MIC_FORMAT,
    RoomPcmConnection,
    RuntimeStore,
    decode_room_binary_frame,
    normalize_room_event,
    parse_mic_start,
)
from annie_brain import FakeBrain
from annie_core import (
    AudioFormat,
    InputRejectedError,
    PcmFrame,
    RobotConfig,
    RobotQueueFullError,
    RobotSnapshot,
    RobotUnavailableError,
    TurnResult,
)
from annie_observability import Recorder, RuntimeEvent

from annie_runtime.realtime import RealtimeRobot
from annie_runtime.speech import SpeechOutput
from annie_runtime.viseme import VisemeSidecars

logger = logging.getLogger(__name__)


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

    def _reject(self, reason: str):
        self.rejected += 1
        raise InputRejectedError(reason)

    def _clear(self):
        cleared = 0
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()
            cleared += 1
        self.dropped += cleared
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
            self._reject("listen_disabled")
        if self.source_id is None or self.audio_format is None:
            self._reject("stream_not_started")
        if source_id != self.source_id:
            self._reject("source_mismatch")
        if len(payload) != self.audio_format.bytes_per_frame:
            self._reject("invalid_frame_size")
        if (time.monotonic() - received_at) * 1000 > self.config.audio_max_age_ms:
            self._reject("stale_audio")
        if sequence is None:
            sequence = 1 if self.last_sequence is None else self.last_sequence + 1
        if type(sequence) is not int or sequence < 0:
            self._reject("invalid_sequence")
        if self.last_sequence is not None and sequence <= self.last_sequence:
            self._reject("out_of_order")

        if self.queue.full():
            self.queue.get_nowait()
            self.queue.task_done()
            self.dropped += 1
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
        "humor_level",
        "advice_readiness",
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
        persona_profiles: dict | None = None,
        realtime_key: str | None = None,
        realtime_url: str | None = None,
        external_llm=None,
        realtime_socket_factory=None,
        tts_voice: str | None = None,
        tts_url: str | None = None,
        tts_socket_factory=None,
        room_tts_factory=None,
        viseme_root: Path | None = None,
        viseme_python: str | None = None,
    ):
        if not configs or len({c.robot_id for c in configs}) != len(configs):
            raise ValueError("at least one config and unique robot_id values required")
        if type(conversation_history_turns) is not int or not 1 <= conversation_history_turns <= 100:
            raise ValueError("conversation_history_turns must be an integer between 1 and 100")
        self.store = RuntimeStore(database)
        self.recorder = Recorder(self.store.write_batch)
        self.ledger = ConversationLedgerWriter(self.store)
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
        self.input_bindings: dict[str, tuple[str, ...]] = {}
        self.local_input_sources: set[str] = set()
        self.room_server = room_server
        self.room_connections = {}
        self.room_sources = {}
        self.playback_listeners = {}
        self.viseme_sidecars = None
        self.external_llm = external_llm
        self.realtime = {}
        if realtime_key:
            from annie_adapters.realtime import DEFAULT_URL, QwenRealtimeSocket
            from annie_adapters.realtime import realtime_url as validate_url

            url = validate_url(realtime_url or DEFAULT_URL)
            self.realtime = {
                robot_id: RealtimeRobot(
                    robot, url=url, key=realtime_key,
                    persona_profiles=self.persona_profiles,
                    external_llm=external_llm,
                    socket_factory=realtime_socket_factory or QwenRealtimeSocket,
                )
                for robot_id, robot in self.robots.items() if robot.config.enabled
            }
        if tts_voice:
            if not realtime_key or room_server is None:
                raise ValueError("TTS requires Realtime and a configured Room server")
            from annie_adapters.playback import RoomEventListener
            from annie_adapters.tts import DEFAULT_URL as TTS_DEFAULT_URL
            from annie_adapters.tts import QwenTtsSocket, RoomTtsPublisher, validate_url

            url = validate_url(tts_url or TTS_DEFAULT_URL)
            for robot_id, client in self.realtime.items():
                publisher = (room_tts_factory or RoomTtsPublisher)(
                    room_server, client.robot.config.room_id,
                )
                client.speech = SpeechOutput(
                    client.robot, publisher, url=url, key=realtime_key,
                    voice=tts_voice, socket_factory=tts_socket_factory or QwenTtsSocket,
                    event=client.publish,
                )
                self.playback_listeners[robot_id] = (
                    RoomEventListener(
                        room_server, client.robot.config.room_id, "main",
                        lambda message, robot_id=robot_id: self._room_playback_receipt(robot_id, message),
                    ),
                    RoomEventListener(
                        room_server, client.robot.config.room_id, "sam",
                        lambda message, robot_id=robot_id: self._room_mouth_script(robot_id, message),
                    ),
                )
        if viseme_root is not None:
            if not tts_voice or room_server is None:
                raise ValueError("ForcedAligner requires enabled TTS and a Room server")
            self.viseme_sidecars = VisemeSidecars(
                viseme_root, room_server, [client.robot for client in self.realtime.values()],
                python=viseme_python,
            )
        self.started = False
        if room_server is not None:
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
            robot.record(f"input_event:{event.kind}", source=source_id)
        return {robot_id: event for robot_id in targets}

    def input_stats(self, robot_id: str):
        robot = self.robots[robot_id]
        stats = robot.audio.stats()
        stats.update(
            event_count=robot.snapshot.event_count,
            last_event_kind=robot.snapshot.last_event_kind,
            last_input_source=robot.snapshot.last_input_source,
        )
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

    def _room_input_disconnected(self, source_id: str):
        if not self.started:
            return
        try:
            self.reconnect_input_source(source_id)
            for robot_id in self.input_bindings.get(source_id, ()):
                if robot_id in self.realtime:
                    asyncio.create_task(self.realtime[robot_id].restart())
        except InputRejectedError:
            logger.exception("failed to reset disconnected Room input source=%s", source_id)

    def _room_playback_receipt(self, robot_id, message):
        client = self.realtime.get(robot_id)
        if client is None or client.speech is None or not str(message.get("type", "")).startswith(
            "tts.playback."
        ):
            return
        try:
            if not client.speech.on_playback_receipt(message):
                client.publish("tts.playback.rejected", reason="stale_or_invalid_state")
        except ValueError as exc:
            client.publish("tts.playback.rejected", reason=str(exc)[:120])

    def _room_mouth_script(self, robot_id, message):
        client = self.realtime.get(robot_id)
        if client is not None and client.speech is not None:
            client.speech.on_mouth_script(message)

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
        if self.viseme_sidecars is not None:
            self.viseme_sidecars.preflight()

    async def start(self):
        if self.started:
            return
        await asyncio.to_thread(self.store.migrate)
        self.ledger.start()
        self.recorder.start()
        self.started = True
        for robot in self.robots.values():
            await robot.initialize_session()
            await robot.start()
        if self.viseme_sidecars is not None:
            await self.viseme_sidecars.start()
        for realtime in self.realtime.values():
            realtime.start()
        for connection in self.room_connections.values():
            connection.start()
        for connections in self.playback_listeners.values():
            for connection in connections:
                connection.start()

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
        if self.started:
            self.started = False
            for realtime in self.realtime.values():
                await realtime.stop()
            for connections in self.playback_listeners.values():
                await asyncio.gather(*(connection.close() for connection in connections))
            if self.viseme_sidecars is not None:
                await self.viseme_sidecars.close()
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
            if self.external_llm is not None:
                await self.external_llm.close()
            if ledger_error is not None:
                raise ledger_error

    async def submit(self, robot_id, text, *, fail=False):
        return await self.robots[robot_id].submit(text, fail=fail)

    async def cancel(self, robot_id):
        if robot_id in self.realtime:
            await self.realtime[robot_id].cancel()
            await self.realtime[robot_id].restart()
        return await self.robots[robot_id].cancel()

    def session_state(self, robot_id: str):
        return self.robots[robot_id].session_state()

    async def switch_session(
        self,
        robot_id: str,
        *,
        chat_id: str,
        persona_id: str,
        persona_revision: int = 1,
    ):
        previous = self.robots[robot_id].session_state()
        result = await self.robots[robot_id].switch_session(
            chat_id=chat_id,
            persona_id=persona_id,
            persona_revision=persona_revision,
        )
        if robot_id in self.realtime and result != previous:
            await self.realtime[robot_id].restart()
        return result

    async def list_conversations(self, robot_id: str):
        await self.ledger.flush()
        return await asyncio.to_thread(self.store.list_conversations, robot_id)

    async def load_conversation(self, robot_id: str, chat_id: str, persona_id: str):
        await self.ledger.flush()
        turns = await asyncio.to_thread(
            self.store.load_conversation,
            robot_id,
            chat_id,
            persona_id,
        )
        return {
            "robot_id": robot_id,
            "chat_id": chat_id,
            "persona_id": persona_id,
            "turns": turns,
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
            if set(fields) != {"question_pressure"} or fields["question_pressure"] not in {
                "normal", "low", "none",
            }:
                raise ValueError("user_setting requires normal/low/none question_pressure")
            client = self.realtime.get(robot_id)
            if client and (client.turn_id or client.response_id or client.speech_active):
                raise ValueError("Realtime busy; set conversation preference after current turn")
        if ttl_ms is not None and (type(ttl_ms) is not int or not 1 <= ttl_ms <= 86_400_000):
            raise ValueError("ttl_ms must be between 1 and 86400000")
        now_ms = time.time_ns() // 1_000_000
        expires_at_ms = None if ttl_ms is None else now_ms + ttl_ms
        for key, value in fields.items():
            await asyncio.to_thread(
                self.store.upsert_social_state,
                robot_id,
                robot.chat_id,
                robot.persona_id,
                key,
                value,
                source=source,
                source_turn_id=source_turn_id,
                generation=generation,
                expires_at_ms=expires_at_ms,
                updated_ms=now_ms,
            )
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

    def realtime_state(self, robot_id: str):
        if robot_id not in self.realtime:
            return {**self.session_state(robot_id), "ready": False, "state": "not_configured"}
        return self.realtime[robot_id].snapshot()

    def realtime_events(self, robot_id: str, after: int):
        if robot_id not in self.realtime:
            return {"events": [], "latest": 0, "gap": False}
        return self.realtime[robot_id].since(after)

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
                "ledger_error": self.ledger.last_error,
                "ledger_queue_depth": self.ledger.queue.qsize(),
                "ledger_queue_capacity": self.ledger.queue.maxsize,
            },
            "room_gateway": {
                "configured": room_required,
                "ready": room_ready,
                "connections": room_status,
            },
            "viseme_sidecars": self.viseme_sidecars.snapshot() if self.viseme_sidecars else {},
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
                    ) else "sidecar_exited" if self.viseme_sidecars is not None and any(
                        proc.poll() is not None
                        for proc in self.viseme_sidecars.processes.values()
                    ) else "sidecar_started_unverified" if self.viseme_sidecars is not None
                    else "not_connected"
                ),
                "llm": (
                    "connected" if self.realtime and all(client.ready for client in self.realtime.values())
                    else "partial" if any(client.ready for client in self.realtime.values())
                    else "connecting" if self.realtime else "not_connected"
                ),
                "memory": "not_started",
            },
        }
