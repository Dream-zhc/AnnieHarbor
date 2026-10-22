"""Two independent room pairs without loading a real model or opening sockets."""

import base64
import json
from argparse import Namespace
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from forced_aligner_runtime import AlignedChar
from qwen_stream_room_g2pw import (
    DualG2P, RoomPairRuntime, SharedForcedAligner, room_configs,
)


class FakeModel:
    backend = "fake"

    def __init__(self):
        self.active = 0
        self.max_active = 0
        self.calls = []
        self.last_align_ms = None
        self._lock = threading.Lock()

    def align_pcm(self, pcm, text, *, sample_rate, channels=1, language="Chinese"):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.03)
            self.calls.append(text)
            self.last_align_ms = len(text) * 10.0
            return [AlignedChar(text, 0.0, 0.1)]
        finally:
            with self._lock:
                self.active -= 1


class FakeSocket:
    def __init__(self):
        self.sent = []
        self._lock = threading.Lock()

    def send(self, payload):
        with self._lock:
            self.sent.append(json.loads(payload))

    def close(self):
        pass


class TwoProjectsTests(unittest.TestCase):
    def test_room_urls_must_be_complete_and_distinct(self):
        args = Namespace(
            sam_url="ws://host/a", tts_url="ws://host/b",
            sam_url_b="", tts_url_b="", extra_rooms="",
        )
        self.assertEqual(len(room_configs(args)), 1)
        args.sam_url_b = "ws://host/c"
        with self.assertRaisesRegex(ValueError, "BOTH"):
            room_configs(args)
        args.tts_url_b = args.tts_url
        with self.assertRaisesRegex(ValueError, "distinct"):
            room_configs(args)
        args.tts_url_b = "ws://host/d"
        self.assertEqual([x[0] for x in room_configs(args)], ["A", "B"])

    def test_extra_rooms_derive_sibling_pairs_on_project_a_server(self):
        args = Namespace(
            sam_url="ws://h:8090/robot/room/reb_sam",
            tts_url="ws://h:8090/robot/room/reb_tts",
            sam_url_b="", tts_url_b="", extra_rooms="roomv2, roomv3,room",
        )
        self.assertEqual(room_configs(args), [
            ("A", "ws://h:8090/robot/room/reb_sam", "ws://h:8090/robot/room/reb_tts"),
            ("roomv2", "ws://h:8090/robot/roomv2/reb_sam", "ws://h:8090/robot/roomv2/reb_tts"),
            ("roomv3", "ws://h:8090/robot/roomv3/reb_sam", "ws://h:8090/robot/roomv3/reb_tts"),
        ])
        args.extra_rooms = "room/x"
        with self.assertRaisesRegex(ValueError, "invalid extra room"):
            room_configs(args)

    def test_one_model_serializes_inference_and_keeps_caller_metrics(self):
        model = FakeModel()
        shared = SharedForcedAligner(model)
        barrier = threading.Barrier(2)

        def run(text):
            barrier.wait()
            result = shared.align_pcm(b"pcm", text, sample_rate=24000)
            return result[0].text, shared.last_align_ms

        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(run, "一")
            b = pool.submit(run, "二三")
            self.assertEqual(a.result(), ("一", 10.0))
            self.assertEqual(b.result(), ("二三", 20.0))
        self.assertEqual(model.max_active, 1)
        self.assertEqual(model.calls.count("一"), 1)
        self.assertEqual(model.calls.count("二三"), 1)
        self.assertEqual(shared.snapshot()["aligner_inference_completed"], 2)
        self.assertEqual(shared.snapshot()["aligner_queue_depth"], 0)

    def test_room_pair_heartbeats_mouth_and_reset_do_not_cross(self):
        shared = SharedForcedAligner(FakeModel())
        resolver = DualG2P()
        a = RoomPairRuntime("A", "ws://host/reb_sam_a", "ws://host/reb_tts_a", resolver, shared)
        b = RoomPairRuntime("B", "ws://host/reb_sam_b", "ws://host/reb_tts_b", resolver, shared)
        threads = []
        try:
            for group in (a, b):
                sock = FakeSocket()
                group.sam._ws = sock
                group.sam.connected.set()
                group.sam.subscribed.set()
                group.tts.connected.set()
                group.tts.subscribed.set()
                group.sock = sock
                thread = threading.Thread(target=group.sam._send_loop)
                thread.start()
                threads.append(thread)

            a.on_sam_message({"type": "brain_health"})
            self.assertIsNotNone(a.health()["brain_heartbeat_age_ms"])
            self.assertIsNone(b.health()["brain_heartbeat_age_ms"])
            self.assertEqual(a.health()["project_id"], "A")
            self.assertEqual(b.health()["project_id"], "B")
            a.engine.on_viseme_control({"enabled": False})
            self.assertTrue(b.engine.emit_enabled)
            a.on_disconnect()
            self.assertTrue(b.engine.emit_enabled)

            b.pacer.enqueue({
                "type": "mouth_script", "seq": 1, "stream_id": "b-only",
                "timing_source": "qwen3_forced_aligner_online",
                "at_sample": 0, "sample_rate": 24000,
            })
            b.pacer.enqueue({"type": "mouth_sync_summary", "stream_id": "b-only"})
            deadline = time.monotonic() + 2.0
            while len(b.sock.sent) < 3 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIn("mouth_script", [msg["type"] for msg in b.sock.sent])
            self.assertIn("mouth_sync_summary", [msg["type"] for msg in b.sock.sent])
            self.assertEqual(a.sam.sent_count, 0)
            self.assertEqual(a.health()["mouth_send_completed"], 0)
            self.assertEqual(b.health()["mouth_send_completed"], 2)
            self.assertTrue(b.tts.connected.is_set())
        finally:
            a.stop()
            b.stop()
            for thread in threads:
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())

    def test_two_real_pcm_messages_have_isolated_output(self):
        shared = SharedForcedAligner(FakeModel())
        resolver = DualG2P()
        a = RoomPairRuntime("A", "ws://host/a-sam", "ws://host/a-tts", resolver, shared)
        b = RoomPairRuntime("B", "ws://host/b-sam", "ws://host/b-tts", resolver, shared)
        # Non-silent actual PCM bytes; the fake aligner only replaces ML compute.
        rate = 24000
        audio = (np.sin(np.arange(rate) * 440 * 2 * np.pi / rate) * 10000).astype("<i2")
        b64 = base64.b64encode(audio.tobytes()).decode("ascii")

        def feed(group, cid, turn, text):
            group.engine.on_tts_message({
                "type": "tts",
                "data": {
                    "audio": b64, "format": "pcm_s16le",
                    "sample_rate": rate, "channels": 1,
                    "text": text, "seq": 1, "final": True,
                    "conversation_id": cid, "turn_id": turn,
                },
            })

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(feed, a, "a-conv", "a-turn", "阿"),
                    pool.submit(feed, b, "b-conv", "b-turn", "哦"),
                ]
                for future in futures:
                    future.result(timeout=10)
            deadline = time.monotonic() + 2.0
            while (a.outgoing.empty() or b.outgoing.empty()) and time.monotonic() < deadline:
                time.sleep(0.01)
            frames_a, frames_b = [], []
            for group, frames in ((a, frames_a), (b, frames_b)):
                while not group.outgoing.empty():
                    frames.append(json.loads(group.outgoing.get_nowait()))
            mouths_a = [x for x in frames_a if x["type"] == "mouth_script"]
            mouths_b = [x for x in frames_b if x["type"] == "mouth_script"]
            self.assertTrue(mouths_a, frames_a)
            self.assertTrue(mouths_b, frames_b)
            self.assertEqual({x["conversation_id"] for x in mouths_a}, {"a-conv"})
            self.assertEqual({x["conversation_id"] for x in mouths_b}, {"b-conv"})
            self.assertEqual({x["project_id"] for x in frames_a}, {"A"})
            self.assertEqual({x["project_id"] for x in frames_b}, {"B"})
            self.assertEqual(shared.model.max_active, 1)
            a.on_disconnect()
            feed(b, "b2-conv", "b2-turn", "哦")
            deadline = time.monotonic() + 2.0
            while b.outgoing.empty() and time.monotonic() < deadline:
                time.sleep(0.01)
            more_b = []
            while not b.outgoing.empty():
                more_b.append(json.loads(b.outgoing.get_nowait()))
            self.assertIn(
                "b2-conv",
                {x.get("conversation_id") for x in more_b if x["type"] == "mouth_script"},
                more_b,
            )
        finally:
            a.stop()
            b.stop()


if __name__ == "__main__":
    unittest.main()
