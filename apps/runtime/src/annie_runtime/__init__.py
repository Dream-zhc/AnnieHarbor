"""One process with isolated robot turns and Phase 2 bounded input gates."""

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from annie_adapters import RuntimeStore, decode_room_binary_frame, normalize_room_event, parse_mic_start
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

logger = logging.getLogger(__name__)


@dataclass
class Job:
    request_id: str
    text: str
    fail: bool
    future: asyncio.Future
    enqueued_at: float


class AudioInputGate:
    """Per-robot PCM admission and freshness boundary; it does not infer wire identity."""

    def __init__(self, config: RobotConfig):
        self.config = config
        self.queue = asyncio.Queue(maxsize=config.audio_queue_capacity)
        self.source_id = None
        self.audio_format: AudioFormat | None = None
        self.last_sequence = None
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
        self.source_id = source_id
        self.audio_format = audio_format
        self.last_sequence = None

    def reset_source(self, source_id: str):
        if self.source_id != source_id:
            return 0
        cleared = self._clear()
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
    def __init__(self, config: RobotConfig, recorder: Recorder):
        self.config = config
        self.recorder = recorder
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
        job = Job(
            uuid4().hex,
            text,
            fail,
            asyncio.get_running_loop().create_future(),
            time.perf_counter(),
        )
        try:
            self.queue.put_nowait(job)
        except asyncio.QueueFull as exc:
            raise RobotQueueFullError(f"{self.config.robot_id} queue full") from exc
        self.record("queued", job.request_id)
        return await asyncio.shield(job.future)

    def discard_pending(self):
        while not self.queue.empty():
            job = self.queue.get_nowait()
            elapsed = round((time.perf_counter() - job.enqueued_at) * 1000, 3)
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
                status, reply, error = "completed", None, None
                try:
                    reply = await self.turn_task
                    snap.completed += 1
                    snap.last_reply = reply
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
                snap.state = "stopped" if self.stopping else "failed" if status == "failed" else "idle"
                self.turn_task = None
                self.job = None
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
            self.discard_pending()

    async def cancel(self):
        async with self.lifecycle:
            if self.turn_task is not None:
                job = self.job
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
            self.discard_pending()
            self.audio._clear()
            self.audio.source_id = None
            self.audio.audio_format = None
            self.audio.last_sequence = None
            if self.config.enabled:
                self.snapshot.state = "stopped"
            self.record("stopped")
            return self.snapshot.to_dict()


class HarborRuntime:
    def __init__(self, configs: list[RobotConfig], database: Path):
        if not configs or len({c.robot_id for c in configs}) != len(configs):
            raise ValueError("at least one config and unique robot_id values required")
        self.store = RuntimeStore(database)
        self.recorder = Recorder(self.store.write_batch)
        self.robots = {c.robot_id: RobotRuntime(c, self.recorder) for c in configs}
        self.input_bindings: dict[str, tuple[str, ...]] = {}
        self.local_input_sources: set[str] = set()
        self.started = False

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

    def ingest_mic_start(self, source_id: str, message: dict, *, target_robot_id=None, broadcast=False):
        audio_format = parse_mic_start(message)
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
            robot.record("mic_stream_started", source=source_id)
        return targets

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
            robot.record("pcm_enqueued", source=source_id)
        return targets

    def next_pcm_nowait(self, robot_id: str):
        robot = self.robots[robot_id]
        frame, queue_wait_ms = robot.audio.next_nowait()
        robot.record("pcm_dequeued", queue_wait_ms=queue_wait_ms, source=frame.source_id)
        return frame, queue_wait_ms

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
            self.ingest_mic_start(
                source_id,
                {
                    "type": "mic.start",
                    "data": {
                        "audio": {
                            "codec": "pcm_s16le",
                            "sample_rate": 16000,
                            "channels": 1,
                            "frame_ms": 20,
                            "bytes_per_frame": 640,
                        }
                    },
                },
            )
        self.ingest_pcm(source_id, frame, target_robot_id=robot_id)
        return self.input_stats(robot_id)

    def preflight(self):
        self.store.preflight()

    async def start(self):
        if self.started:
            return
        await asyncio.to_thread(self.store.migrate)
        self.recorder.start()
        self.started = True
        for robot in self.robots.values():
            await robot.start()

    async def close(self):
        if self.started:
            for robot in self.robots.values():
                await robot.stop()
            await self.recorder.close()
            self.started = False

    async def submit(self, robot_id, text, *, fail=False):
        return await self.robots[robot_id].submit(text, fail=fail)

    async def cancel(self, robot_id):
        return await self.robots[robot_id].cancel()

    async def stop_robot(self, robot_id):
        return await self.robots[robot_id].stop()

    async def start_robot(self, robot_id):
        if not self.started:
            raise RobotUnavailableError("runtime is closed")
        await self.robots[robot_id].start()
        return self.robots[robot_id].snapshot.to_dict()

    def health(self):
        snapshots = []
        for robot in self.robots.values():
            robot.snapshot.queue_depth = robot.queue.qsize()
            robot.sync_input_snapshot()
            snapshots.append(robot.snapshot.to_dict())
        active = sum(s["state"] in ("idle", "running") for s in snapshots)
        expected_active = sum(robot.config.enabled for robot in self.robots.values())
        ready = self.started and self.recorder.last_error is None and active > 0
        return {
            "project": "Annie Harbor",
            "phase": 2,
            "mode": "fake_brain_pcm_gate",
            "pid": os.getpid(),
            "ready": ready,
            "status": "healthy"
            if ready and active == expected_active
            else "degraded"
            if ready
            else "not_ready",
            "robots": snapshots,
            "storage": {
                "schema_version": self.store.SCHEMA_VERSION,
                "error": self.recorder.last_error,
                "trace_dropped": self.recorder.dropped,
            },
            "capabilities": {
                "brain": "fake",
                "room": "protocol_ready_not_connected",
                "pcm_input": "gate_ready",
                "llm": "not_connected",
                "tts": "not_connected",
                "memory": "not_started",
                "viseme": "not_started",
            },
        }
