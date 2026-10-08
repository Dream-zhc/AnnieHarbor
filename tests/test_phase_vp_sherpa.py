"""Sherpa streaming gate tests; do not confuse fake KWS with accuracy evidence."""

import asyncio
import unittest
from pathlib import Path

from annie_runtime.wakeword import FRAME_BYTES, PRE_ROLL_BYTES, WakeAdmission

ROOT = Path(__file__).resolve().parents[1]


class SherpaKeywordConfigTests(unittest.TestCase):
    def test_default_and_experimental_files_remain_separate(self):
        default = (ROOT / "config/小安妮唤醒词.tokens.txt").read_text(
            encoding="utf-8"
        ).splitlines()
        experimental = (ROOT / "config/小安妮唤醒词_宽松实验.tokens.txt").read_text(
            encoding="utf-8"
        ).splitlines()
        self.assertEqual(default, ["x iǎo ān n ī :1.5 @小安妮"])
        self.assertEqual(len(experimental), 14)
        self.assertTrue(all(" #" in line and " @" in line and " :" in line
                            for line in experimental))
        self.assertIn("x iǎo ān n ī :3.0 #0.04 @小妮", experimental)
        self.assertIn("ān n ǐ :2.4 #0.12 @安妮", experimental)
        # Short phrases and low thresholds are available only by explicit opt-in.
        self.assertFalse(any("#0.04" in line for line in default))

    def test_real_model_reports_kws_audio_level_and_decoder_progress(self):
        import wave

        from annie_adapters.kws import SherpaWakeModel

        root = ROOT / "var/models/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"
        if not (root / "test_wavs/zh_3.wav").is_file():
            self.skipTest("optional official sherpa test fixture is not installed")
        model = SherpaWakeModel(root, keywords=root / "test_wavs/keywords.txt")
        stream = model.new_stream()
        with wave.open(str(root / "test_wavs/zh_3.wav")) as wav:
            pcm = wav.readframes(wav.getnframes())
        triggered = any(stream._decode(pcm[i:i + 3200])
                        for i in range(0, len(pcm), 3200))
        self.assertTrue(triggered)
        self.assertGreater(stream.decoded_batches, 0)
        self.assertGreater(stream.input_dbfs, -55)
        self.assertIsNotNone(stream.last_keyword)
        self.assertIsNotNone(stream.last_decode_ms)

        # A shorter streaming batch must still detect official positive audio.
        fresh = model.new_stream()
        triggered_40ms = any(
            fresh._decode(pcm[i:i + 1280])
            for i in range(0, len(pcm), 1280)
        )
        self.assertTrue(triggered_40ms)


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
    async def test_streaming_batches_are_40ms_and_pcm_isolation(self):
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
        self.assertTrue(all(size >= 2 * FRAME_BYTES for size in detector.streams[0].sizes))
        self.assertEqual(a.snapshot()["batch_audio_ms"], 40)
        self.assertEqual(a.snapshot()["preroll_ms"], 1600)
        self.assertEqual(PRE_ROLL_BYTES // FRAME_BYTES, 80)
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

    async def test_same_breath_question_started_before_kws_hit_is_not_truncated(self):
        class LateHitStream:
            def __init__(self):
                self.calls = 0

            async def detected(self, _pcm):
                self.calls += 1
                await asyncio.sleep(0)
                return self.calls == 15

        class LateHitModel:
            def new_stream(self):
                return LateHitStream()

        gate = WakeAdmission(LateHitModel())
        forwarded = []
        for i in range(45):
            # The model may finish confirming the wake phrase after the user
            # has started the next word. No VAD pause separates the two.
            sample = (0x42 if 17 <= i < 27 else 0x11)
            forwarded.extend(await gate.frames(bytes([sample]) * FRAME_BYTES))
            await asyncio.sleep(0)
        self.assertEqual(gate.state, "WAKE_WAIT")
        self.assertIn(bytes([0x42]) * FRAME_BYTES, forwarded)
        self.assertEqual(forwarded[0], bytes([0x11]) * FRAME_BYTES)

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
