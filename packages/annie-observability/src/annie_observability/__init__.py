"""Small typed local trace recorder; not an event bus or a turn dependency."""

import asyncio
import logging
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
