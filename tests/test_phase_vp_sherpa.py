"""Sherpa streaming gate tests; do not confuse fake KWS with accuracy evidence."""

import asyncio
import unittest

from annie_runtime.wakeword import FRAME_BYTES, PRE_ROLL_BYTES, WakeAdmission


class Stream:
    def __init__(self, trigger):
        self.trigger = trigger
        self.calls = 0
        self.sizes = []

    async def detected(self, pcm):
        self.calls += 1
        self.sizes.append(len(pcm))
        await asyncio.sleep(0)
        return self.trigger and self.calls == 3


class Detector:
    def __init__(self):
        self.streams = []
        self.trigger = True

    def new_stream(self):
        stream = Stream(self.trigger)
        self.streams.append(stream)
        return stream


class SherpaWakeGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_streaming_batches_are_100ms_and_pcm_isolation(self):
        detector = Detector()
        a = WakeAdmission(detector)
        b = WakeAdmission(detector)
        pcm = b"\x01\x02" * (FRAME_BYTES // 2)
        sent_a, sent_b = [], []
        for _ in range(65):
            sent_a.extend(await a.frames(pcm))
            sent_b.extend(await b.frames(pcm))
            await asyncio.sleep(0.001)
        self.assertEqual(a.state, "WAKE_WAIT")
        self.assertEqual(b.state, "WAKE_WAIT")
        self.assertEqual(a.wakes, 1)
        self.assertGreaterEqual(len(sent_a), PRE_ROLL_BYTES // FRAME_BYTES)
        self.assertLessEqual(len(sent_a), 65 + PRE_ROLL_BYTES // FRAME_BYTES)
        self.assertTrue(all(size % FRAME_BYTES == 0 for size in detector.streams[0].sizes))
        self.assertTrue(all(size >= 5 * FRAME_BYTES for size in detector.streams[0].sizes))
        self.assertIsNot(detector.streams[0], detector.streams[1])

    async def test_disabled_robot_never_inherits_other_stream(self):
        detector = Detector()
        a = WakeAdmission(detector)
        detector.trigger = False
        b = WakeAdmission(detector)
        pcm = bytes(FRAME_BYTES)
        for _ in range(55):
            await a.frames(pcm)
            self.assertEqual(await b.frames(pcm), ())
            await asyncio.sleep(.001)
        self.assertEqual(a.wakes, 1)
        self.assertEqual(b.wakes, 0)
        self.assertEqual(b.state, "SLEEP")

    async def test_stream_failure_is_fail_closed(self):
        detector = Detector()
        gate = WakeAdmission(detector)

        async def fail(_):
            raise RuntimeError("native_onnx_failed")

        gate.detector.detected = fail
        pcm = bytes(FRAME_BYTES)
        for _ in range(5):
            self.assertEqual(await gate.frames(pcm), ())
        await asyncio.sleep(0)
        with self.assertRaisesRegex(RuntimeError, "sherpa KWS failed"):
            await gate.frames(pcm)
        self.assertEqual(gate.state, "SLEEP")
        self.assertIsNotNone(gate.snapshot()["error"])

    async def test_expired_window_reinitializes_decoder_for_next_wake(self):
        class Clock:
            now = 0.0

            def __call__(self):
                return self.now

        clock = Clock()
        detector = Detector()
        gate = WakeAdmission(detector, clock=clock)
        gate.presence.wake_detected()
        self.assertEqual(await gate.frames(bytes(FRAME_BYTES)), (bytes(FRAME_BYTES),))
        clock.now = 5.1
        self.assertEqual(await gate.frames(bytes(FRAME_BYTES)), ())
        self.assertEqual(gate.state, "SLEEP")
        self.assertEqual(len(detector.streams), 2)


if __name__ == "__main__":
    unittest.main()
