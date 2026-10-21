"""Self-hosted d3 decision model, used only while it answers.

The d3 box is a pay-as-you-go cloud container: it may be off, and its first
question after a start takes about 25 s. A background probe asks /health and
one warm question; only after both succeed does d3 take real questions. A real
question that fails hands the work back to DashScope until the next probe
succeeds. The probe also keeps the connection and the model warm.
"""

import asyncio
import logging
import time

from annie_adapters.decision import error_kind

logger = logging.getLogger(__name__)

WARM_INTERVAL_SECONDS = 60  # while ready: keeps the keep-alive connection and kernels hot
RETRY_INTERVAL_SECONDS = 30  # while down: the box may be started at any time
WARM_TIMEOUT_SECONDS = 40  # the first answer after a container start takes ~25 s


class D3Route:
    def __init__(self, client, *, model, expression_threshold=0.7,
                 warm_interval=WARM_INTERVAL_SECONDS, retry_interval=RETRY_INTERVAL_SECONDS,
                 warm_timeout=WARM_TIMEOUT_SECONDS):
        self.client = client
        self.model = model
        self.expression_threshold = expression_threshold
        self.warm_interval = warm_interval
        self.retry_interval = retry_interval
        self.warm_timeout = warm_timeout
        self.ready = False
        self.status = "probing"  # probing -> ready / down
        self.error = None
        self.checked_ms = None
        self.failures = 0
        self._task = None

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="d3-probe")

    async def _run(self):
        while True:
            await self.probe()
            await asyncio.sleep(self.warm_interval if self.ready else self.retry_interval)

    async def probe(self):
        try:
            await self.client.health()
            await self.client.warm(timeout=self.warm_timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._mark(False, error_kind(exc))
        else:
            self._mark(True, None)
        self.checked_ms = int(time.time() * 1000)

    def failed(self, error):
        """A real question failed: DashScope answers until the next probe succeeds."""
        self.failures += 1
        self._mark(False, error)

    def _mark(self, ready, error):
        status = "ready" if ready else "down"
        if status != self.status:
            logger.info("d3_decision status=%s model=%s error=%s", status, self.model, error)
        self.ready, self.status, self.error = ready, status, error

    def snapshot(self):
        return {"model": self.model, "status": self.status, "ready": self.ready,
                "error": self.error, "checked_ms": self.checked_ms, "failures": self.failures,
                "expression_threshold": self.expression_threshold}

    async def close(self):
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await self.client.close()
