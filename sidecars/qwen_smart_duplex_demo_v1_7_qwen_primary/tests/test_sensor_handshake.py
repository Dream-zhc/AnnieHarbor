from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from config import Settings
from qwen_audio_sensor import QwenAudioSensor


class FakeWebSocket:
    def __init__(self, initial_events: list[dict]) -> None:
        self._recv = asyncio.Queue()
        for event in initial_events:
            self._recv.put_nowait(json.dumps(event))
        self._stream = asyncio.Queue()
        self.sent: list[dict] = []
        self.state = SimpleNamespace(name="OPEN")

    async def recv(self) -> str:
        return await self._recv.get()

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def close(self) -> None:
        self.state.name = "CLOSED"

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._stream.get()


class SensorHandshakeTests(unittest.IsolatedAsyncioTestCase):
    async def test_smart_turn_uses_required_modalities_and_waits_for_updated(self) -> None:
        fake = FakeWebSocket(
            [
                {"type": "session.created"},
                {"type": "session.updated"},
            ]
        )
        events: list[dict] = []

        async def on_event(event: dict) -> None:
            events.append(event)

        sensor = QwenAudioSensor(Settings(api_key="sk-test"), on_event)
        with patch("qwen_audio_sensor.websockets.connect", new=AsyncMock(return_value=fake)):
            await sensor.connect()

        self.assertTrue(sensor.connected.is_set())
        self.assertEqual(fake.sent[0]["session"]["modalities"], ["text", "audio"])
        self.assertEqual(events[-1]["type"], "session.updated")
        await sensor.close()

    async def test_session_update_error_does_not_mark_connected(self) -> None:
        fake = FakeWebSocket(
            [
                {"type": "session.created"},
                {
                    "type": "error",
                    "error": {"message": "bad session config"},
                },
            ]
        )

        async def on_event(event: dict) -> None:
            pass

        sensor = QwenAudioSensor(Settings(api_key="sk-test"), on_event)
        with patch("qwen_audio_sensor.websockets.connect", new=AsyncMock(return_value=fake)):
            with self.assertRaisesRegex(RuntimeError, "bad session config"):
                await sensor.connect()

        self.assertFalse(sensor.connected.is_set())
        await sensor.close()

    async def test_internal_response_completes_silently_instead_of_immediate_cancel(self) -> None:
        fake = FakeWebSocket([])
        events: list[dict] = []

        async def on_event(event: dict) -> None:
            events.append(event)

        sensor = QwenAudioSensor(Settings(api_key="sk-test"), on_event)
        sensor.ws = fake
        sensor.connected.set()

        await sensor._handle_event({"type": "response.created", "response": {"id": "r1"}})
        self.assertTrue(sensor._response_active)
        self.assertFalse(any(x.get("type") == "response.cancel" for x in fake.sent))

        await sensor._handle_event(
            {
                "type": "response.output_item.added",
                "item": {"id": "assistant-dot", "type": "message"},
            }
        )
        await sensor._handle_event(
            {"type": "response.done", "response": {"status": "completed"}}
        )
        self.assertFalse(sensor._response_active)
        self.assertTrue(
            any(
                x.get("type") == "conversation.item.delete"
                and x.get("item_id") == "assistant-dot"
                for x in fake.sent
            )
        )

    async def test_reconnect_audio_backlog_is_bounded(self) -> None:
        sensor = QwenAudioSensor(
            Settings(api_key="sk-test", audio_sensor_reconnect_buffer_ms=100),
            AsyncMock(),
        )
        for _ in range(20):
            await sensor.send_audio(b"x" * 640)  # 20ms @ 16k PCM16
        self.assertLessEqual(sensor._audio_backlog_bytes, sensor._audio_backlog_limit)
        self.assertGreater(len(sensor._audio_backlog), 0)


if __name__ == "__main__":
    unittest.main()
