"""Phase 2 protocol adapter and bounded PCM ingress tests."""

import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "docs/phase0/2026-09-17/fixtures"
PHASE2_CONTROL_FIXTURES = ROOT / "docs/phase2/2026-09-18/room_control_live.jsonl"


def load_contract(contract_id):
    for raw in (FIXTURES / "external_contract.jsonl").read_text().splitlines():
        row = json.loads(raw)
        if row["id"] == contract_id:
            return row["message"]
    raise AssertionError(f"missing contract fixture: {contract_id}")


class ProtocolAdapterTests(unittest.TestCase):
    def test_live_room_control_welcome_and_subscribe_ack_are_strictly_parsed(self):
        from annie_adapters import parse_room_welcome, parse_subscribe_ack

        rows = [json.loads(raw) for raw in PHASE2_CONTROL_FIXTURES.read_text().splitlines()]
        welcome = parse_room_welcome(rows[0]["message"])
        self.assertEqual((welcome.robot_id, welcome.room), ("reb_mic", "roomv2"))
        self.assertTrue(welcome.client_id.startswith("anonymous-"))
        self.assertIn("MIC_AUDIO", welcome.catalog_types)
        self.assertNotIn("mic.start", welcome.catalog_types)
        ack = parse_subscribe_ack(rows[1]["message"])
        self.assertEqual(ack.subscriptions, ("*",))

        with self.assertRaises(ValueError):
            parse_room_welcome({"type": "WELCOME", "ok": True, "clientId": "x"})
        with self.assertRaises(ValueError):
            parse_subscribe_ack({"type": "ACK", "data": {"what": "other"}})

    def test_binary_frame_decoder_matches_phase0_fixtures(self):
        from annie_adapters import decode_room_binary_frame

        manifest = json.loads((FIXTURES / "binary_frames.json").read_text())
        for row in manifest:
            with self.subTest(row=row["id"]):
                decoded = decode_room_binary_frame((FIXTURES / Path(row["file"]).name).read_bytes())
                if row["expected"] is None:
                    self.assertIsNone(decoded)
                else:
                    message_type, payload = decoded
                    self.assertEqual(message_type, row["expected"]["type"])
                    self.assertEqual(len(payload), row["expected"]["payload_size"])

    def test_mic_start_extracts_only_verified_pcm_contract(self):
        from annie_adapters import parse_mic_start

        audio = parse_mic_start(load_contract("mic-start"))
        self.assertEqual(audio.codec, "pcm_s16le")
        self.assertEqual((audio.sample_rate, audio.channels), (16000, 1))
        self.assertEqual((audio.frame_ms, audio.bytes_per_frame), (20, 640))

        flat = {
            "type": "mic.start",
            "data": {
                "codec": "pcm_s16le",
                "sample_rate": 16000,
                "channels": 1,
                "frame_ms": 20,
                "bytes_per_frame": 640,
            },
        }
        self.assertEqual(parse_mic_start(flat), audio)
        with self.assertRaises(ValueError):
            parse_mic_start({"type": "mic.start", "data": {"audio": {"sample_rate": 16000}}})
        different_but_consistent = {
            "type": "mic.start",
            "data": {
                "audio": {
                    "codec": "pcm_s16le",
                    "sample_rate": 24000,
                    "channels": 1,
                    "frame_ms": 20,
                    "bytes_per_frame": 960,
                }
            },
        }
        with self.assertRaises(ValueError):
            parse_mic_start(different_but_consistent)

    def test_asr_event_sensor_and_vision_normalize_without_inventing_target(self):
        from annie_adapters import normalize_room_event

        asr = normalize_room_event(load_contract("asr-nested-precedence"))
        self.assertEqual((asr.kind, asr.payload["text"]), ("legacy_asr", "payload文本"))
        self.assertIsNone(asr.target_robot_id)

        robot_event = normalize_room_event(load_contract("robot-event-envelope"))
        self.assertEqual(robot_event.kind, "sensor.pressure")
        self.assertEqual(robot_event.source, "embedded.pressure")
        self.assertEqual(robot_event.payload, {"id": 1, "level": 2})

        touch = normalize_room_event(load_contract("sensor-touch"))
        self.assertEqual((touch.kind, touch.payload["level"]), ("sensor.touch", 1))
        self.assertIsNone(normalize_room_event({"type": "pressure", "level": 6}))

        vision = normalize_room_event(load_contract("vision-face"))
        self.assertEqual(vision.kind, "vision.face")
        self.assertEqual(vision.source, "fixture-vision")
        self.assertEqual(vision.payload["tracks"][0]["name"], "fixture-user")

    def test_cancel_identity_fixture_rejects_late_or_conflicting_cancel(self):
        from annie_adapters import match_cancel_identity

        for raw in (FIXTURES / "cancel_identity.jsonl").read_text().splitlines():
            row = json.loads(raw)
            with self.subTest(row=row["id"]):
                self.assertEqual(match_cancel_identity(row["active"], row["input"]), tuple(row["expected"]))


class AudioGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from annie_core import RobotConfig
        from annie_runtime import HarborRuntime

        self.tmp = tempfile.TemporaryDirectory(prefix="phase2-test-", dir=ROOT / "var")
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "runtime.db"
        self.runtime = HarborRuntime(
            [
                RobotConfig(
                    "annie-a",
                    listen_enabled=True,
                    fake_delay_ms=0,
                    audio_queue_capacity=2,
                    audio_max_age_ms=100,
                ),
                RobotConfig(
                    "annie-b",
                    listen_enabled=True,
                    fake_delay_ms=0,
                    audio_queue_capacity=2,
                    audio_max_age_ms=100,
                ),
            ],
            self.db,
        )
        await self.runtime.start()
        self.addAsyncCleanup(self.runtime.close)
        self.binary = (FIXTURES / "valid-mic.bin").read_bytes()
        self.mic_start = load_contract("mic-start")

    def test_targeted_and_broadcast_delivery_are_per_head(self):
        self.runtime.bind_input_source("shared-mic", ["annie-a", "annie-b"])
        self.runtime.ingest_mic_start("shared-mic", self.mic_start, broadcast=True)
        self.assertEqual(
            self.runtime.ingest_pcm("shared-mic", self.binary, sequence=1, broadcast=True),
            ("annie-a", "annie-b"),
        )
        self.assertEqual(self.runtime.input_stats("annie-a")["queue_depth"], 1)
        self.assertEqual(self.runtime.input_stats("annie-b")["queue_depth"], 1)

        frame_a, _ = self.runtime.next_pcm_nowait("annie-a")
        frame_b, _ = self.runtime.next_pcm_nowait("annie-b")
        self.assertEqual(frame_a.payload, frame_b.payload)
        self.assertEqual(frame_a.source_id, "shared-mic")

        self.runtime.ingest_pcm("shared-mic", self.binary, sequence=2, target_robot_id="annie-a")
        self.assertEqual(self.runtime.input_stats("annie-a")["queue_depth"], 1)
        self.assertEqual(self.runtime.input_stats("annie-b")["queue_depth"], 0)

    def test_unknown_source_loopback_stale_format_order_and_backpressure_are_rejected_or_bounded(self):
        from annie_core import InputRejectedError

        with self.assertRaisesRegex(InputRejectedError, "unknown_source"):
            self.runtime.ingest_mic_start("unknown", self.mic_start)

        self.runtime.bind_input_source("harbor-output", ["annie-a"], local=True)
        with self.assertRaisesRegex(InputRejectedError, "loopback_source"):
            self.runtime.ingest_mic_start("harbor-output", self.mic_start)

        self.runtime.bind_input_source("mic-a", ["annie-a"])
        self.runtime.ingest_mic_start("mic-a", self.mic_start)
        with self.assertRaisesRegex(InputRejectedError, "invalid_frame_size"):
            message_type = b"MIC_AUDIO"
            short_pcm = len(message_type).to_bytes(2, "big") + message_type + bytes(10)
            self.runtime.ingest_pcm("mic-a", short_pcm, sequence=1)
        with self.assertRaisesRegex(InputRejectedError, "stale_audio"):
            self.runtime.ingest_pcm("mic-a", self.binary, sequence=1, received_at=time.monotonic() - 1)

        self.runtime.ingest_pcm("mic-a", self.binary, sequence=1)
        with self.assertRaisesRegex(InputRejectedError, "out_of_order"):
            self.runtime.ingest_pcm("mic-a", self.binary, sequence=1)
        self.runtime.ingest_pcm("mic-a", self.binary, sequence=2)
        self.runtime.ingest_pcm("mic-a", self.binary, sequence=3)
        stats = self.runtime.input_stats("annie-a")
        self.assertEqual((stats["queue_depth"], stats["dropped"]), (2, 1))
        first, _ = self.runtime.next_pcm_nowait("annie-a")
        second, _ = self.runtime.next_pcm_nowait("annie-a")
        self.assertEqual((first.sequence, second.sequence), (2, 3))

    def test_reconnect_drops_stale_queue_and_requires_fresh_mic_start(self):
        from annie_core import InputRejectedError

        self.runtime.bind_input_source("mic-a", ["annie-a"])
        self.runtime.ingest_mic_start("mic-a", self.mic_start)
        self.runtime.ingest_pcm("mic-a", self.binary, sequence=1)
        self.assertEqual(self.runtime.input_stats("annie-a")["queue_depth"], 1)
        self.runtime.reconnect_input_source("mic-a")
        self.assertEqual(self.runtime.input_stats("annie-a")["queue_depth"], 0)
        with self.assertRaisesRegex(InputRejectedError, "stream_not_started"):
            self.runtime.ingest_pcm("mic-a", self.binary, sequence=2)
        self.runtime.ingest_mic_start("mic-a", self.mic_start)
        self.runtime.ingest_pcm("mic-a", self.binary, sequence=1)

    def test_verified_transport_format_can_start_gate_without_wire_mic_start(self):
        from annie_adapters import VERIFIED_MIC_FORMAT
        from annie_core import AudioFormat, InputRejectedError

        self.runtime.bind_input_source("room-reb-mic", ["annie-a"])
        self.runtime.start_pcm_stream("room-reb-mic", VERIFIED_MIC_FORMAT)
        self.runtime.ingest_pcm("room-reb-mic", self.binary, sequence=1)
        stats = self.runtime.input_stats("annie-a")
        self.assertEqual(stats["accepted"], 1)
        self.assertEqual(stats["format"]["bytes_per_frame"], 640)
        with self.assertRaisesRegex(InputRejectedError, "unsupported_audio_format"):
            self.runtime.start_pcm_stream(
                "room-reb-mic",
                AudioFormat("pcm_s16le", 24000, 1, 20, 960),
            )

    async def test_pcm_active_blocks_same_source_legacy_asr_double_trigger(self):
        from annie_core import InputRejectedError

        self.runtime.bind_input_source("mic-a", ["annie-a"])
        asr = {"type": "asr", "text": "兼容输入", "from": "mic-a", "ts": 1}
        result = await self.runtime.ingest_legacy_asr("mic-a", asr)
        self.assertEqual(result["annie-a"].reply, "[FAKE annie-a] 兼容输入")

        self.runtime.ingest_mic_start("mic-a", self.mic_start)
        with self.assertRaisesRegex(InputRejectedError, "pcm_stream_active"):
            await self.runtime.ingest_legacy_asr("mic-a", asr)

    async def test_normalized_events_are_recorded_without_triggering_fake_brain(self):
        self.runtime.bind_input_source("device-events", ["annie-a", "annie-b"])
        delivered = self.runtime.dispatch_room_event(
            "device-events", load_contract("sensor-pressure"), broadcast=True
        )
        self.assertEqual(set(delivered), {"annie-a", "annie-b"})
        for robot_id in delivered:
            stats = self.runtime.input_stats(robot_id)
            self.assertEqual(stats["last_event_kind"], "sensor.pressure")
            self.assertEqual(stats["event_count"], 1)
        self.assertIsNone(self.runtime.robots["annie-a"].snapshot.last_reply)

    async def test_pcm_queue_wait_is_typed_trace_and_persisted(self):
        self.runtime.bind_input_source("mic-a", ["annie-a"])
        self.runtime.ingest_mic_start("mic-a", self.mic_start)
        self.runtime.ingest_pcm("mic-a", self.binary, sequence=1)
        await asyncio.sleep(0.02)
        _, queue_wait_ms = self.runtime.next_pcm_nowait("annie-a")
        self.assertGreater(queue_wait_ms, 10)
        await self.runtime.recorder.flush()
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute("pragma user_version").fetchone()[0], 2)
            row = db.execute(
                "select queue_wait_ms, source from runtime_events where kind='pcm_dequeued' order by id desc limit 1"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertGreater(row[0], 10)
        self.assertEqual(row[1], "mic-a")


class Phase2HTTPTests(unittest.TestCase):
    def setUp(self):
        from annie_core import RobotConfig
        from annie_runtime import HarborRuntime
        from annie_web import create_app
        from fastapi.testclient import TestClient

        self.tmp = tempfile.TemporaryDirectory(prefix="phase2-http-", dir=ROOT / "var")
        self.addCleanup(self.tmp.cleanup)
        runtime = HarborRuntime(
            [RobotConfig("annie-a", listen_enabled=True, fake_delay_ms=0)],
            Path(self.tmp.name) / "api.db",
        )
        self.client = self.enterContext(
            TestClient(create_app(runtime, ROOT / "web/src", ROOT / "tests/html"))
        )

    def test_health_and_binary_debug_ingress_are_explicit_about_no_live_room(self):
        health = self.client.get("/health").json()
        self.assertEqual(health["phase"], 2)
        self.assertEqual(health["capabilities"]["room"], "protocol_ready_not_connected")
        response = self.client.post(
            "/api/debug/robots/annie-a/room-pcm?source=phase2-html",
            content=(FIXTURES / "valid-mic.bin").read_bytes(),
            headers={"content-type": "application/octet-stream"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["queue_depth"], 1)
        self.assertEqual(self.client.get("/api/robots/annie-a/inputs").json()["accepted"], 1)

        bad = self.client.post(
            "/api/debug/robots/annie-a/room-pcm?source=phase2-html",
            content=b"bad",
            headers={"content-type": "application/octet-stream"},
        )
        self.assertEqual(bad.status_code, 422)

    def test_phase2_visual_acceptance_page_is_served(self):
        page = self.client.get("/phase-tests/phase2.html")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Phase 2", page.text)
        self.assertIn("Room BinaryFrame", page.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
