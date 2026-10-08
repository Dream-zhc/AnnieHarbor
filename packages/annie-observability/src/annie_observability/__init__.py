"""Small typed local trace recorder; not an event bus or a turn dependency."""

import asyncio
import logging
import sys
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeEvent:
    robot_id: str
    room_id: str
    kind: str
    request_id: str | None = None
    elapsed_ms: float | None = None
    error: str | None = None
    queue_wait_ms: float | None = None
    source: str | None = None
    ts_ms: int = field(default_factory=lambda: time.time_ns() // 1_000_000)


class Recorder:
    def __init__(self, write_batch, capacity=1024):
        self.write_batch = write_batch
        self.queue = asyncio.Queue(maxsize=capacity)
        self.task = None
        self.dropped = 0
        self.last_error = None

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self._write(), name="harbor:sqlite-recorder")

    def emit(self, event: RuntimeEvent, snapshot: dict):
        try:
            self.queue.put_nowait((event, snapshot))
        except asyncio.QueueFull:
            self.dropped += 1
            logger.error("SQLite trace queue full; dropped=%s", self.dropped)

    async def _write(self):
        while True:
            batch = [await self.queue.get()]
            while len(batch) < 32 and not self.queue.empty():
                batch.append(self.queue.get_nowait())
            try:
                await asyncio.to_thread(self.write_batch, batch)
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)
                logger.exception("SQLite trace write failed")
            finally:
                for _ in batch:
                    self.queue.task_done()

    async def flush(self):
        await self.queue.join()

    async def close(self):
        if self.task is not None:
            await self.flush()
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None


class ProcessProbe:
    """One low-frequency sample per Runtime, never on the PCM or LLM path."""

    def __init__(self, interval=2.0):
        self.interval = interval
        self.task = None
        self.metrics = {"cpu_percent": None, "rss_peak_mb": None,
                        "loop_lag_ms": None, "loop_lag_max_ms": None,
                        "samples": 0, "memory_source": "unavailable"}

    def snapshot(self):
        return dict(self.metrics)

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run(), name="harbor:process-probe")

    async def _run(self):
        last_wall, last_cpu = time.monotonic(), time.process_time()
        while True:
            expected = last_wall + self.interval
            await asyncio.sleep(self.interval)
            now, cpu = time.monotonic(), time.process_time()
            elapsed = now - last_wall
            lag = round(max(0.0, (now - expected) * 1000), 2)
            self.metrics.update(
                cpu_percent=round(100 * (cpu - last_cpu) / elapsed, 2) if elapsed > 0 else None,
                loop_lag_ms=lag,
                loop_lag_max_ms=max(self.metrics["loop_lag_max_ms"] or 0, lag),
                samples=self.metrics["samples"] + 1,
            )
            if sys.platform != "win32":
                import resource

                peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                # ru_maxrss is bytes on macOS, KiB on Linux: peak, not live RSS.
                self.metrics["rss_peak_mb"] = round(
                    peak / (1024 * 1024 if sys.platform == "darwin" else 1024), 2)
                self.metrics["memory_source"] = "os_peak_rss"
            last_wall, last_cpu = now, cpu

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
