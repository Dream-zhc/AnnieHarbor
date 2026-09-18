"""The Phase 2 brain remains an asynchronous deterministic Fake."""

import asyncio

from annie_core import RobotConfig


class FakeBrain:
    def __init__(self, config: RobotConfig):
        self.config = config

    async def generate(self, text: str, *, fail: bool = False) -> str:
        await asyncio.sleep(self.config.fake_delay_ms / 1000)
        if fail:
            raise RuntimeError("Injected FakeBrain failure")
        return f"[FAKE {self.config.robot_id}] {text}"
