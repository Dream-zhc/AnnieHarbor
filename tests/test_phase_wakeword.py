"""Synthetic wake admission regressions; no claim of Chinese model accuracy."""

import unittest

from annie_runtime.wakeword import (
    FOLLOWUP_SECONDS,
    FRAME_BYTES,
    PRE_ROLL_BYTES,
    WAKE_WAIT_SECONDS,
    WINDOW_BYTES,
    WakeAdmission,
)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class Detector:
    def __init__(self):
        self.trigger = False
        self.calls = 0

    async def detected(self, window):
        self.calls += 1
        assert len(window) == WINDOW_BYTES
        return self.trigger


class WakeAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def _prime(self, gate, detector):
        packet = b"\x01\x00" * 320  # 20 ms at 16 kHz
        detector.trigger = True
        for _ in range(99):
            self.assertEqual(await gate.frames(packet), ())
        self.assertEqual(await gate.frames(packet), (packet,) * (PRE_ROLL_BYTES // FRAME_BYTES))
        self.assertEqual(gate.state, "WAKE_WAIT")
        return packet

    async def test_sleep_never_forwards_pcm_until_wake_and_wait_expires(self):
        clock, detector = Clock(), Detector()
        gate = WakeAdmission(detector, clock=clock)
        packet = await self._prime(gate, detector)
        self.assertEqual(gate.wakes, 1)
        self.assertEqual(await gate.frames(packet), (packet,))
        clock.now += WAKE_WAIT_SECONDS + 0.001
        detector.trigger = False
        self.assertEqual(await gate.frames(packet), ())
        self.assertEqual(gate.snapshot()["state"], "SLEEP")
        self.assertEqual(gate.expirations, 1)

    async def test_followup_uses_actual_speaker_drained_not_llm_completion(self):
        clock, detector = Clock(), Detector()
        gate = WakeAdmission(detector, clock=clock)
        packet = await self._prime(gate, detector)
        gate.speech_started("u1")
        self.assertEqual(gate.state, "WAKE_WAIT")
        gate.speech_stopped("u1")
        self.assertTrue(gate.transcript_accepted("今天怎么样", "u1"))
        self.assertEqual(gate.state, "ACTIVE")
        clock.now += 3
        self.assertEqual(await gate.frames(packet), (packet,))
        self.assertEqual(gate.state, "ACTIVE")
        gate.authorized_turn_id = "turn1"
        gate.speaker_drained("turn1")
        self.assertEqual(gate.state, "FOLLOWUP")
        clock.now += FOLLOWUP_SECONDS - 0.1
        gate.speech_started("u2")
        clock.now += 35
        self.assertEqual(await gate.frames(packet), (packet,))
        self.assertTrue(gate.transcript_accepted("继续刚才的话题", "u2"))
        self.assertEqual(gate.state, "ACTIVE")

    async def test_two_robots_share_detector_but_never_share_gate_state(self):
        clock, detector = Clock(), Detector()
        a = WakeAdmission(detector, clock=clock)
        b = WakeAdmission(detector, clock=clock)
        packet = await self._prime(a, detector)
        detector.trigger = False
        self.assertEqual(await b.frames(packet), ())
        self.assertEqual(a.snapshot()["state"], "WAKE_WAIT")
        self.assertEqual(b.snapshot()["state"], "SLEEP")
        a.reset()
        self.assertEqual(a.state, "SLEEP")
        self.assertEqual(b.state, "SLEEP")

    async def test_detection_is_rate_limited_and_invalid_window_stays_local(self):
        clock, detector = Clock(), Detector()
        gate = WakeAdmission(detector, clock=clock)
        packet = b"\x00" * 640
        for _ in range(200):
            await gate.frames(packet)
        self.assertEqual(detector.calls, 1)
        clock.now += 0.5
        await gate.frames(packet)
        self.assertEqual(detector.calls, 2)
        self.assertEqual(gate.state, "SLEEP")


if __name__ == "__main__":
    unittest.main()
