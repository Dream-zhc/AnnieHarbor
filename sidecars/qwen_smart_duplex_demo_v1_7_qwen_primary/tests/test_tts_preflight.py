from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from config import Settings
from tts_client import QwenAudioTTS


class FakeTTSWebSocket:
    def __init__(self, fail_all: bool = False, fail_instruction: bool = False) -> None:
        self.fail_all = fail_all
        self.fail_instruction = fail_instruction
        self.state = SimpleNamespace(name="OPEN")
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        message = json.loads(raw)
        self.sent.append(message)
        action = message["header"]["action"]
        task_id = message["header"]["task_id"]
        if action == "run-task":
            params = message["payload"]["parameters"]
            should_fail = self.fail_all or (self.fail_instruction and "instruction" in params)
            if should_fail:
                await self.queue.put(
                    json.dumps(
                        {
                            "header": {
                                "task_id": task_id,
                                "event": "task-failed",
                                "error_code": "InvalidParameter",
                                "error_message": "url error, please check url！",
                            },
                            "payload": {},
                        }
                    )
                )
            else:
                await self.queue.put(
                    json.dumps({"header": {"task_id": task_id, "event": "task-started"}})
                )
        elif action == "finish-task":
            await self.queue.put(
                json.dumps({"header": {"task_id": task_id, "event": "task-finished"}})
            )

    async def close(self) -> None:
        self.state.name = "CLOSED"

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.queue.get()


class TTSPreflightTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_falls_back_to_public_endpoint(self) -> None:
        events: list[dict] = []
        connections: list[tuple[str, dict]] = []

        async def on_event(event: dict) -> None:
            events.append(event)

        async def connect(url: str, **kwargs):
            connections.append((url, kwargs.get("additional_headers", {})))
            return FakeTTSWebSocket(fail_all="llm-test" in url)

        cfg = Settings(
            api_key="sk-test",
            workspace_id="llm-test",
            tts_instruction="温柔自然",
            tts_endpoint_mode="auto",
        )
        tts = QwenAudioTTS(cfg, on_audio=lambda *_: asyncio.sleep(0), on_event=on_event)
        with patch("tts_client.websockets.connect", new=connect):
            await tts.connect()

        self.assertTrue(tts.preflight_complete)
        self.assertEqual(tts.endpoint.label, "public")
        self.assertFalse(connections[-1][0].endswith("/"))
        self.assertEqual(
            connections[-1][1].get("X-DashScope-WorkSpace"),
            "llm-test",
        )
        self.assertTrue(any(e.get("type") == "tts.ready" for e in events))
        await tts.close()

    async def test_instruction_failure_downgrades_to_minimal_parameters(self) -> None:
        events: list[dict] = []

        async def on_event(event: dict) -> None:
            events.append(event)

        async def connect(url: str, **kwargs):
            return FakeTTSWebSocket(fail_instruction=True)

        cfg = Settings(
            api_key="sk-test",
            workspace_id="llm-test",
            tts_instruction="温柔自然",
            tts_endpoint_mode="workspace",
        )
        tts = QwenAudioTTS(cfg, on_audio=lambda *_: asyncio.sleep(0), on_event=on_event)
        with patch("tts_client.websockets.connect", new=connect):
            await tts.connect()

        self.assertTrue(tts.preflight_complete)
        self.assertEqual(tts.endpoint.label, "workspace")
        self.assertFalse(tts.use_instruction)
        ready = [e for e in events if e.get("type") == "tts.ready"][-1]
        self.assertFalse(ready["instruction"])
        await tts.close()


if __name__ == "__main__":
    unittest.main()
