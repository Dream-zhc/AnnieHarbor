"""KWS completion wakes the real audio pump without a future PCM packet."""

import asyncio
import base64
import tempfile
import time
import unittest
from pathlib import Path

from annie_adapters import VERIFIED_MIC_FORMAT
from annie_core import RobotConfig
from annie_runtime import HarborRuntime
from annie_runtime.wakeword import FRAME_BYTES


class ControlledStream:
    last_keyword = "小安妮"
    last_keyword_tokens = ["x", "iǎo", "ān", "n", "ī"]
    last_keyword_token_times_ms = [0, 10, 20, 30, 40]
    last_decode_ms = 5.0

    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = []
        self.answers = [True]
        self.failure = False

    async def detected(self, pcm):
        self.calls.append(pcm)
        self.started.set()
        await self.release.wait()
        if self.failure:
            raise RuntimeError("controlled_native_failure")
        return self.answers[min(len(self.calls) - 1, len(self.answers) - 1)]


class ControlledModel:
    def __init__(self):
        self.streams = []

    def new_stream(self):
        stream = ControlledStream()
        self.streams.append(stream)
        return stream


class CaptureSocket:
    def __init__(self):
        self.packets = []
        self.closed = False

    async def send(self, packet):
        self.packets.append(packet)

    async def close(self):
        self.closed = True


class WakeCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.model = ControlledModel()
        self.runtime = HarborRuntime(
            [RobotConfig("annie-a", room_id="roomv2", listen_enabled=True)],
            Path(self.directory.name) / "completion.db",
            realtime_key="fixture-not-a-provider-key", wake_detector=self.model,
        )
        self.client = self.runtime.realtime["annie-a"]
        self.audio = self.client.robot.audio
        self.audio.start_stream("fixture-mic", VERIFIED_MIC_FORMAT)
        self.client.socket = CaptureSocket()
        self.client.ready = True
        self.events = self.client.subscribe()
        self.pump = asyncio.create_task(self.client._audio_pump(self.client.robot.generation))
        self.stream = self.client.wake.detector

    async def asyncTearDown(self):
        self.client.ready = False
        self.pump.cancel()
        await asyncio.gather(self.pump, return_exceptions=True)
        if self.client.wake:
            self.client.wake.reset()
        for stream in self.model.streams:
            stream.release.set()
        self.client.unsubscribe(self.events)
        self.directory.cleanup()

    def put(self, number):
        pcm = bytes([number]) * FRAME_BYTES
        self.audio.put("fixture-mic", pcm, sequence=None, received_at=time.monotonic())
        return pcm

    async def event(self, kind):
        async def read():
            while True:
                event = await self.events.get()
                if event["kind"] == kind:
                    return event
        try:
            return await asyncio.wait_for(read(), .2)
        except asyncio.TimeoutError:
            return None

    def forwarded(self):
        return [base64.b64decode(packet["audio"]) for packet in self.client.socket.packets
                if packet["type"] == "input_audio_buffer.append"]

    async def test_hit_notifies_and_forwards_preroll_while_pcm_is_stopped(self):
        expected = [self.put(1), self.put(2)]
        await asyncio.wait_for(self.stream.started.wait(), .2)
        self.stream.release.set()
        self.assertIsNotNone(await self.event("wakeword.detected"))
        await asyncio.wait_for(self.audio.queue.join(), .2)
        self.assertEqual(self.client.wake.state, "WAKE_WAIT")
        self.assertEqual(self.client.wake.wakes, 1)
        self.assertEqual(self.forwarded(), expected)

    async def test_native_failure_closes_without_waiting_for_next_pcm(self):
        self.stream.failure = True
        self.put(1)
        self.put(2)
        await asyncio.wait_for(self.stream.started.wait(), .2)
        self.stream.release.set()
        self.assertIsNotNone(await self.event("wakeword.error"))
        self.assertFalse(self.client.ready)
        self.assertTrue(self.client.socket.closed)
        self.assertEqual(self.client.wake.state, "SLEEP")
        self.assertEqual(self.forwarded(), [])
        # 10/20 18:47: this used to stop the client for good (woke, heard nothing).
        self.assertFalse(self.client._stopping)

    async def test_simultaneous_hit_and_next_pcm_forward_in_order_once(self):
        expected = [self.put(1), self.put(2)]
        await asyncio.wait_for(self.stream.started.wait(), .2)
        self.stream.release.set()
        expected.append(self.put(3))
        self.assertIsNotNone(await self.event("wakeword.detected"))
        await asyncio.wait_for(self.audio.queue.join(), .2)
        self.assertEqual(self.forwarded(), expected)
        self.assertEqual(self.client.wake.wakes, 1)

    async def test_pending_audio_decodes_after_negative_completion_without_next_pcm(self):
        self.stream.answers = [False, True]
        expected = [self.put(1), self.put(2)]
        await asyncio.wait_for(self.stream.started.wait(), .2)
        expected.extend([self.put(3), self.put(4)])
        await asyncio.wait_for(self.audio.queue.join(), .2)
        self.stream.release.set()
        self.assertIsNotNone(await self.event("wakeword.detected"))
        self.assertEqual(len(self.stream.calls), 2)
        self.assertEqual(self.forwarded(), expected)

    async def test_new_input_epoch_rejects_old_completion_and_old_preroll(self):
        self.put(1)
        self.put(2)
        await asyncio.wait_for(self.stream.started.wait(), .2)
        self.audio.start_stream("fixture-mic", VERIFIED_MIC_FORMAT)
        self.stream.release.set()
        self.assertIsNone(await self.event("wakeword.detected"))
        self.assertEqual(self.forwarded(), [])
        expected = [self.put(3), self.put(4)]
        for _ in range(50):
            if self.client.wake.detector is not self.stream:
                break
            await asyncio.sleep(.001)
        fresh = self.client.wake.detector
        self.assertIsNot(fresh, self.stream)
        fresh.release.set()
        self.assertIsNotNone(await self.event("wakeword.detected"))
        await asyncio.wait_for(self.audio.queue.join(), .2)
        self.assertEqual(self.forwarded(), expected)

    async def test_changed_robot_generation_discards_late_completion(self):
        self.put(1)
        self.put(2)
        await asyncio.wait_for(self.stream.started.wait(), .2)
        self.client.robot.generation += 1
        self.stream.release.set()
        self.assertIsNone(await self.event("wakeword.detected"))
        self.assertEqual(self.forwarded(), [])
        self.assertEqual(self.client.wake.wakes, 0)

    async def test_overage_native_completion_never_opens_gate(self):
        self.put(1)
        self.put(2)
        await asyncio.wait_for(self.stream.started.wait(), .2)
        await asyncio.sleep(self.client.robot.config.audio_max_age_ms / 1000 + .02)
        self.stream.release.set()
        self.assertIsNone(await self.event("wakeword.detected"))
        self.assertEqual(self.forwarded(), [])
        self.assertEqual(self.client.wake.state, "SLEEP")
        self.assertEqual(self.client.wake.wakes, 0)
        self.assertTrue(self.client.ready)

    async def test_overage_native_failure_still_reports_error_and_closes(self):
        self.stream.failure = True
        self.put(1)
        self.put(2)
        await asyncio.wait_for(self.stream.started.wait(), .2)
        await asyncio.sleep(self.client.robot.config.audio_max_age_ms / 1000 + .02)
        self.stream.release.set()
        self.assertIsNotNone(await self.event("wakeword.error"))
        self.assertFalse(self.client.ready)
        self.assertTrue(self.client.socket.closed)
        self.assertEqual(self.client.wake.state, "SLEEP")
        self.assertEqual(self.client.wake.wakes, 0)
        self.assertEqual(self.forwarded(), [])

    async def test_new_epoch_pcm_before_old_completion_preserves_only_new_audio(self):
        self.put(1)
        self.put(2)
        await asyncio.wait_for(self.stream.started.wait(), .2)
        self.audio.start_stream("fixture-mic", VERIFIED_MIC_FORMAT)
        expected = [self.put(3), self.put(4)]
        for _ in range(50):
            if self.client.wake.detector is not self.stream:
                break
            await asyncio.sleep(.001)
        fresh = self.client.wake.detector
        self.assertIsNot(fresh, self.stream)
        self.stream.release.set()
        fresh.release.set()
        self.assertIsNotNone(await self.event("wakeword.detected"))
        await asyncio.wait_for(self.audio.queue.join(), .2)
        self.assertEqual(self.forwarded(), expected)

    async def test_disabled_kws_forwards_pcm_in_order(self):
        self.client.wake = None
        expected = [self.put(1), self.put(2), self.put(3)]
        await asyncio.wait_for(self.audio.queue.join(), .2)
        self.assertEqual(self.forwarded(), expected)

    async def test_cancelled_pump_releases_pending_queue_reader(self):
        await asyncio.sleep(0)
        self.pump.cancel()
        await asyncio.gather(self.pump, return_exceptions=True)
        self.assertEqual(len(self.audio.queue._getters), 0)
