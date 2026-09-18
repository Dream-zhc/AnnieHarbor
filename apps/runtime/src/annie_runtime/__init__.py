"""One process, independent Fake RobotRuntime queues/tasks, local SQLite."""

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from annie_adapters import RuntimeStore
from annie_brain import FakeBrain
from annie_core import RobotConfig, RobotQueueFullError, RobotSnapshot, RobotUnavailableError, TurnResult
from annie_observability import Recorder, RuntimeEvent

logger = logging.getLogger(__name__)


@dataclass
class Job:
    request_id: str
    text: str
    fail: bool
    future: asyncio.Future
    enqueued_at: float


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
        self.worker = None
        self.turn_task = None
        self.job = None
        self.stopping = False
        self.lifecycle = asyncio.Lock()

    def record(self, kind, request_id=None, elapsed_ms=None, error=None):
        self.snapshot.queue_depth = self.queue.qsize()
        self.recorder.emit(
            RuntimeEvent(self.config.robot_id, self.config.room_id, kind, request_id, elapsed_ms, error),
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
        self.started = False

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
            snapshots.append(robot.snapshot.to_dict())
        active = sum(s["state"] in ("idle", "running") for s in snapshots)
        expected_active = sum(robot.config.enabled for robot in self.robots.values())
        ready = self.started and self.recorder.last_error is None and active > 0
        return {
            "project": "Annie Harbor",
            "phase": 1,
            "mode": "fake",
            "pid": os.getpid(),
            "ready": ready,
            "status": "healthy"
            if ready and active == expected_active
            else "degraded"
            if ready
            else "not_ready",
            "robots": snapshots,
            "storage": {
                "schema_version": 1,
                "error": self.recorder.last_error,
                "trace_dropped": self.recorder.dropped,
            },
            "capabilities": {
                "brain": "fake",
                "room": "not_connected",
                "llm": "not_connected",
                "tts": "not_connected",
                "memory": "not_started",
                "viseme": "not_started",
            },
        }
