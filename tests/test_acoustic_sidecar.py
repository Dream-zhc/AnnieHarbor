"""ASR sidecar protocol and failure-isolation regressions; no provider charges."""

import asyncio
import base64
import time
import unittest

from annie_adapters.acoustic import (
    MODEL,
    QwenASRSocket,
    acoustic_session_update,
    parse_acoustic_event,
)
from annie_runtime.acoustic import AcousticSidecar

IDENTITY = {"robot_id": "annie-a", "chat_id": "chat-a", "persona_id": "persona-a", "generation": 3}
URL = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
PCM = bytes(640)


class FakeSocket:
    instances = []
    open_error = None
    send_gate = None

    def __init__(self, url, key):
        self.url = url
        self.key = key
        self.outbound = []
        self.inbound = asyncio.Queue()
        self.closed = False
        self.aborted = False
        self.__class__.instances.append(self)

    async def open(self):
        if self.open_error:
            raise self.open_error("PRIVATE-KEY must never appear")
        return self

    async def send(self, event):
        if event["type"] == "input_audio_buffer.append" and self.send_gate:
            await self.send_gate.wait()
        self.outbound.append(event)
        if event["type"] == "session.update":
            self.inbound.put_nowait({"type": "session.updated"})

    async def recv(self):
        return await self.inbound.get()

    def abort(self):
        self.aborted = True

    async def close(self):
        self.closed = True


async def eventually(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0.001)


class AcousticProtocolTests(unittest.TestCase):
    def test_fixed_model_session_and_auto_language(self):
        socket = QwenASRSocket(URL, "PRIVATE-KEY")
        self.assertIn("model=" + MODEL, socket.url)
        event = acoustic_session_update()
        self.assertTrue(event["event_id"])
        self.assertEqual(event["session"]["input_audio_format"], "pcm")
        self.assertEqual(event["session"]["sample_rate"], 16000)
        self.assertEqual(event["session"]["turn_detection"]["type"], "server_vad")
        self.assertNotIn("language", event["session"]["input_audio_transcription"])
        self.assertNotIn("tools", event["session"])
        with self.assertRaises(ValueError):
            QwenASRSocket(URL + "?model=other", "PRIVATE-KEY")

    def test_stable_prefix_stash_and_completed_are_distinct(self):
        row = parse_acoustic_event({"type": "conversation.item.input_audio_transcription.text",
                                    "item_id": "asr-1", "text": "今天", "stash": "很开心",
                                    "language": "zh", "emotion": "happy"})
        self.assertEqual(row["kind"], "partial")
        self.assertEqual(row["text"], "今天")
        self.assertEqual(row["stash"], "很开心")
        self.assertEqual(row["dialect"], "unknown")
        self.assertIsNone(row["confidence"])
        row = parse_acoustic_event({"type": "conversation.item.input_audio_transcription.completed",
                                    "item_id": "asr-1", "transcript": "今天很开心", "language": "yue"})
        self.assertEqual(row["text"], "今天很开心")
        self.assertEqual(row["stash"], "")
        self.assertEqual(row["language"], "yue")
        self.assertEqual(row["emotion"], "unknown")

    def test_error_exposes_only_code_not_message_or_param(self):
        row = parse_acoustic_event({"type": "error", "error": {
            "code": "invalid_value", "message": "PRIVATE-KEY", "param": "PRIVATE-KEY"}})
        self.assertEqual(row["error_code"], "invalid_value")
        self.assertNotIn("PRIVATE-KEY", repr(row))
        self.assertIsNone(parse_acoustic_event({"type": "response.audio.delta"}))


class AcousticSidecarTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeSocket.instances = []
        FakeSocket.open_error = None
        FakeSocket.send_gate = None
        self.observations = []
        self.sidecars = []

    async def asyncTearDown(self):
        for sidecar in self.sidecars:
            await sidecar.stop()

    def create(self, **kwargs):
        sidecar = AcousticSidecar(robot_id="annie-a", room_id="roomv2", url=URL,
                                  key="PRIVATE-KEY", on_observation=self.observations.append,
                                  socket_factory=FakeSocket, **kwargs)
        self.sidecars.append(sidecar)
        sidecar.start()
        return sidecar

    def offer(self, sidecar, *, identity=None, epoch=1, source="mic", received_at=None, sample=3200):
        return sidecar.offer(PCM, identity=identity or IDENTITY, stream_epoch=epoch,
                             source_id=source, received_at=time.monotonic() if received_at is None else received_at,
                             sample_start=sample)

    async def connected(self, sidecar):
        await eventually(lambda: bool(FakeSocket.instances) and
                         any(e["type"] == "input_audio_buffer.append"
                             for e in FakeSocket.instances[-1].outbound))
        return FakeSocket.instances[-1]

    async def test_lazy_connection_protocol_and_samples(self):
        sidecar = self.create()
        await asyncio.sleep(0)
        self.assertEqual(FakeSocket.instances, [])
        self.assertTrue(self.offer(sidecar))
        socket = await self.connected(sidecar)
        self.assertEqual([e["type"] for e in socket.outbound],
                         ["session.update", "input_audio_buffer.append"])
        self.assertEqual(base64.b64decode(socket.outbound[-1]["audio"]), PCM)
        socket.inbound.put_nowait({"type": "input_audio_buffer.speech_started", "item_id": "asr-only",
                                  "audio_start_ms": 0})
        socket.inbound.put_nowait({"type": "input_audio_buffer.speech_stopped", "item_id": "asr-only",
                                  "audio_end_ms": 20})
        socket.inbound.put_nowait({"type": "conversation.item.input_audio_transcription.completed",
                                  "item_id": "asr-only", "transcript": "你好", "language": "zh", "emotion": "happy"})
        await eventually(lambda: len(self.observations) == 3)
        row = self.observations[-1]
        for name, value in IDENTITY.items():
            self.assertEqual(row[name], value)
        self.assertEqual(row["main_sample_start"], 3200)
        self.assertEqual(row["main_sample_end"], 3520)
        self.assertEqual(row["source_id"], "mic")
        self.assertEqual(row["stream_epoch"], 1)
        self.assertEqual(row["provider_item_id"], "asr-only")
        self.assertGreater(row["observed_at"], 0)

    async def test_identity_change_aborts_old_and_discards_late(self):
        sidecar = self.create()
        self.offer(sidecar)
        old = await self.connected(sidecar)
        generation = sidecar.snapshot()["sidecar_generation"]
        changed = {**IDENTITY, "chat_id": "chat-b", "generation": 4}
        self.offer(sidecar, identity=changed, sample=0)
        old.inbound.put_nowait({"type": "conversation.item.input_audio_transcription.completed",
                               "item_id": "old", "transcript": "旧人声"})
        await eventually(lambda: len(FakeSocket.instances) == 2 and old.closed)
        self.assertTrue(old.aborted)
        self.assertGreater(sidecar.snapshot()["sidecar_generation"], generation)
        self.assertFalse(any(row.get("provider_item_id") == "old" for row in self.observations))

    async def test_overflow_invalidates_whole_segment_not_partial_splice(self):
        sidecar = self.create(queue_frames=1)
        self.assertTrue(self.offer(sidecar))
        self.assertFalse(self.offer(sidecar, sample=3520))
        self.assertEqual(sidecar.snapshot()["queued_frames"], 0)
        self.assertEqual(sidecar.snapshot()["reason"], "queue_overflow")
        self.assertTrue(self.offer(sidecar, sample=3840))
        socket = await self.connected(sidecar)
        self.assertEqual(sum(e["type"] == "input_audio_buffer.append" for e in socket.outbound), 1)

    async def test_old_frame_invalidates_without_connecting(self):
        sidecar = self.create(max_lag_ms=10)
        self.assertFalse(self.offer(sidecar, received_at=time.monotonic() - 1))
        await asyncio.sleep(0)
        self.assertEqual(FakeSocket.instances, [])
        self.assertEqual(sidecar.snapshot()["reason"], "lag_exceeded")

    async def test_transport_failure_is_bounded_and_private(self):
        FakeSocket.open_error = ConnectionError
        sidecar = self.create(max_retries=1, reconnect_delay=0)
        self.assertTrue(self.offer(sidecar))
        await eventually(lambda: sidecar.snapshot()["failures"] == 1)
        self.assertNotIn("PRIVATE-KEY", repr(sidecar.snapshot()))
        self.assertTrue(self.offer(sidecar, sample=3520))
        await eventually(lambda: sidecar.snapshot()["status"] == "unavailable")
        self.assertFalse(self.offer(sidecar, sample=3840))
        self.assertEqual(len(FakeSocket.instances), 2)

    async def test_stop_clears_queue_and_connection_and_prevents_offers(self):
        sidecar = self.create()
        self.offer(sidecar)
        socket = await self.connected(sidecar)
        await sidecar.stop()
        self.assertTrue(socket.closed)
        self.assertFalse(self.offer(sidecar))
        self.assertEqual(sidecar.snapshot()["queued_frames"], 0)
        self.assertEqual(sidecar.snapshot()["status"], "stopped")

    async def test_explicit_disable_invalidates_and_no_connection_until_enabled(self):
        sidecar = self.create()
        sidecar.set_enabled(False)
        self.assertFalse(self.offer(sidecar))
        self.assertEqual(FakeSocket.instances, [])
        sidecar.set_enabled(True)
        self.assertTrue(self.offer(sidecar))
        await self.connected(sidecar)

    async def test_idle_disconnect_and_epoch_changes_reconnect(self):
        sidecar = self.create(idle_timeout=0.01)
        self.offer(sidecar)
        first = await self.connected(sidecar)
        await eventually(lambda: first.closed and sidecar.snapshot()["status"] == "idle")
        self.assertEqual(sidecar.snapshot()["status"], "idle")
        self.offer(sidecar, epoch=2, sample=0)
        await eventually(lambda: len(FakeSocket.instances) == 2)

    async def test_missing_provider_audio_window_is_not_guessed(self):
        sidecar = self.create()
        self.offer(sidecar)
        socket = await self.connected(sidecar)
        socket.inbound.put_nowait({"type": "conversation.item.input_audio_transcription.completed",
                                  "item_id": "no-window", "transcript": "你好"})
        await eventually(lambda: bool(self.observations))
        self.assertIsNone(self.observations[-1]["main_sample_start"])
        self.assertIsNone(self.observations[-1]["main_sample_end"])


    async def test_slow_send_is_retired_at_max_lag_not_io_timeout(self):
        FakeSocket.send_gate = asyncio.Event()
        sidecar = self.create(max_lag_ms=15, io_timeout=0.5)
        self.offer(sidecar)
        await eventually(lambda: bool(FakeSocket.instances))
        socket = FakeSocket.instances[-1]
        await asyncio.sleep(0.05)
        self.assertTrue(socket.closed)
        self.assertEqual(sidecar.snapshot()["reason"], "lag_exceeded")
        self.assertEqual(sidecar.snapshot()["sent_frames"], 0)

    async def test_new_epoch_or_source_revokes_old_generation(self):
        for change in ({"epoch": 2}, {"source": "replacement"}):
            sidecar = self.create()
            self.offer(sidecar)
            socket = await self.connected(sidecar)
            previous = sidecar.snapshot()["sidecar_generation"]
            self.offer(sidecar, **change)
            self.assertTrue(socket.aborted)
            self.assertGreater(sidecar.snapshot()["sidecar_generation"], previous)
            await sidecar.stop()
            FakeSocket.instances = []

    async def test_sample_gap_starts_new_connection_without_splicing(self):
        sidecar = self.create()
        self.offer(sidecar)
        old = await self.connected(sidecar)
        self.offer(sidecar, sample=9600)
        await eventually(lambda: len(FakeSocket.instances) == 2 and old.closed)
        self.assertTrue(old.aborted)

    async def test_queued_expiry_during_connection_open_retires_all(self):
        class SlowSocket(FakeSocket):
            async def open(self):
                await asyncio.sleep(0.05)
                return self
        sidecar = AcousticSidecar(robot_id="annie-a", room_id="roomv2", url=URL,
                                  key="PRIVATE-KEY", on_observation=self.observations.append,
                                  socket_factory=SlowSocket, max_lag_ms=10)
        self.sidecars.append(sidecar)
        sidecar.start()
        self.offer(sidecar)
        await eventually(lambda: sidecar.snapshot()["reason"] == "lag_exceeded")
        self.assertFalse(any(event["type"] == "input_audio_buffer.append"
                             for event in SlowSocket.instances[-1].outbound))

    async def test_callback_exception_does_not_escape_pcm_producer(self):
        def fail(row):
            raise RuntimeError("PRIVATE-KEY")
        sidecar = self.create()
        sidecar._on_observation = fail
        self.offer(sidecar)
        socket = await self.connected(sidecar)
        socket.inbound.put_nowait({"type": "conversation.item.input_audio_transcription.completed",
                                  "item_id": "observation", "transcript": "你好"})
        await eventually(lambda: sidecar.snapshot()["last_error"] == "RuntimeError")
        self.assertNotIn("PRIVATE-KEY", repr(sidecar.snapshot()))
        self.assertTrue(self.offer(sidecar, sample=3520))

    async def test_transcription_failed_is_sanitized_and_has_no_text(self):
        sidecar = self.create()
        self.offer(sidecar)
        socket = await self.connected(sidecar)
        socket.inbound.put_nowait({"type": "conversation.item.input_audio_transcription.failed",
                                  "item_id": "failed", "error": {"code": "invalid_value",
                                  "message": "PRIVATE-KEY"}})
        await eventually(lambda: bool(self.observations))
        row = self.observations[-1]
        self.assertEqual(row["kind"], "failed")
        self.assertEqual(row["error_code"], "invalid_value")
        self.assertEqual(row["text"], "")
        self.assertNotIn("PRIVATE-KEY", repr(row))

    async def test_stop_cancels_pending_connection_open(self):
        gate = asyncio.Event()
        class WaitingSocket(FakeSocket):
            async def open(self):
                await gate.wait()
                return self
        sidecar = AcousticSidecar(robot_id="annie-a", room_id="roomv2", url=URL,
                                  key="PRIVATE-KEY", on_observation=self.observations.append,
                                  socket_factory=WaitingSocket)
        self.sidecars.append(sidecar)
        sidecar.start()
        self.offer(sidecar)
        await eventually(lambda: bool(WaitingSocket.instances))
        socket = WaitingSocket.instances[-1]
        await sidecar.stop()
        self.assertTrue(socket.closed)
        self.assertFalse(sidecar.snapshot()["connected"])

    async def test_error_payload_cannot_smuggle_transcript_or_key_into_snapshot(self):
        sidecar = self.create()
        self.offer(sidecar)
        socket = await self.connected(sidecar)
        socket.inbound.put_nowait({"type": "error", "text": "PRIVATE-KEY", "stash": "PRIVATE-KEY",
                                  "error": {"code": "invalid_value", "message": "PRIVATE-KEY"}})
        await eventually(lambda: bool(self.observations))
        self.assertNotIn("PRIVATE-KEY", repr(self.observations))
        await eventually(lambda: sidecar.snapshot()["failures"] == 1)
        self.assertNotIn("PRIVATE-KEY", repr(sidecar.snapshot()))

    async def test_both_robot_sidecars_are_independent(self):
        first = self.create()
        second_observations = []
        second = AcousticSidecar(robot_id="annie-b", room_id="roomv3", url=URL,
                                 key="PRIVATE-KEY", on_observation=second_observations.append,
                                 socket_factory=FakeSocket)
        self.sidecars.append(second)
        second.start()
        self.offer(first)
        first_socket = await self.connected(first)
        second.offer(PCM, identity={**IDENTITY, "robot_id": "annie-b"}, stream_epoch=1,
                     source_id="mic-b", received_at=time.monotonic(), sample_start=0)
        await eventually(lambda: len(FakeSocket.instances) == 2 and
                         any(e["type"] == "input_audio_buffer.append"
                             for e in FakeSocket.instances[-1].outbound))
        second_socket = FakeSocket.instances[-1]
        first.invalidate("sleep")
        second_socket.inbound.put_nowait({"type": "conversation.item.input_audio_transcription.completed",
                                         "item_id": "b", "transcript": "你好"})
        await eventually(lambda: bool(second_observations))
        self.assertTrue(first_socket.aborted)
        self.assertFalse(second_socket.closed)
        self.assertEqual(second_observations[-1]["robot_id"], "annie-b")
        self.assertEqual(second_observations[-1]["room_id"], "roomv3")

    async def test_successful_upload_resets_consecutive_failure_budget(self):
        FakeSocket.open_error = ConnectionError
        sidecar = self.create(max_retries=1, reconnect_delay=0)
        self.offer(sidecar)
        await eventually(lambda: sidecar.snapshot()["failures"] == 1)
        FakeSocket.open_error = None
        self.offer(sidecar, sample=3520)
        await self.connected(sidecar)
        await eventually(lambda: sidecar.snapshot()["sent_frames"] == 1)
        self.assertEqual(sidecar.snapshot()["failures"], 0)

    async def test_observation_contract_has_canonical_epoch_and_millisecond_clock(self):
        sidecar = self.create()
        self.offer(sidecar, epoch=7)
        socket = await self.connected(sidecar)
        socket.inbound.put_nowait({"type": "conversation.item.input_audio_transcription.completed",
                                  "item_id": "canonical", "transcript": "你好"})
        await eventually(lambda: bool(self.observations))
        row = self.observations[-1]
        self.assertEqual(row["audio_stream_epoch"], 7)
        self.assertEqual(row["stream_epoch"], 7)
        self.assertAlmostEqual(row["observed_monotonic_ms"], row["observed_at"] * 1000)
        self.assertLess(abs(row["observed_monotonic_ms"] - time.monotonic() * 1000), 100)

    async def test_partial_window_uses_explicit_upload_cursor_until_provider_stop(self):
        sidecar = self.create()
        self.offer(sidecar, sample=3200)
        socket = await self.connected(sidecar)
        await eventually(lambda: sidecar.snapshot()["sent_frames"] == 1)
        socket.inbound.put_nowait({"type": "input_audio_buffer.speech_started", "item_id": "partial",
                                  "audio_start_ms": 0})
        socket.inbound.put_nowait({"type": "conversation.item.input_audio_transcription.text",
                                  "item_id": "partial", "text": "今天", "stash": "不错"})
        await eventually(lambda: len(self.observations) == 2)
        row = self.observations[-1]
        self.assertEqual(row["main_sample_start"], 3200)
        self.assertEqual(row["main_sample_end"], 3520)
        self.assertEqual(row["window_end_source"], "upload_cursor")
        self.assertIsNone(row["audio_end_ms"])
        socket.inbound.put_nowait({"type": "input_audio_buffer.speech_stopped", "item_id": "partial",
                                  "audio_end_ms": 20})
        socket.inbound.put_nowait({"type": "conversation.item.input_audio_transcription.completed",
                                  "item_id": "partial", "transcript": "今天不错"})
        await eventually(lambda: len(self.observations) == 4)
        row = self.observations[-1]
        self.assertEqual(row["main_sample_end"], 3520)
        self.assertEqual(row["window_end_source"], "provider_vad")

    async def test_partial_without_provider_start_keeps_window_unknown(self):
        sidecar = self.create()
        self.offer(sidecar)
        socket = await self.connected(sidecar)
        socket.inbound.put_nowait({"type": "conversation.item.input_audio_transcription.text",
                                  "item_id": "missing-start", "text": "今天", "stash": "不错"})
        await eventually(lambda: bool(self.observations))
        row = self.observations[-1]
        self.assertIsNone(row["main_sample_start"])
        self.assertIsNone(row["main_sample_end"])
        self.assertIsNone(row["window_end_source"])
