"""Phase 4B isolated TTS wire, audio ownership, cancellation and publication tests."""

import asyncio
import base64
import json
import time
import unittest

from annie_adapters.playback import RoomEventListener, playback_receipt
from annie_adapters.tts import MODEL, RoomTtsPublisher, command, validate_url
from annie_runtime.speech import SpeechOutput


class FakeRobot:
    def __init__(self, robot_id="annie-a", room_id="roomv2"):
        self.config = type("Config", (), {"robot_id": robot_id, "room_id": room_id})()
        self.generation = 1
        self.turn_cache = {}
        self.writes = []
        self.context_cache = []
        self.ledger = type("Ledger", (), {"enqueue": lambda _, *a, **k: self.writes.append((a, k))})()

    def session_state(self):
        return {
            "robot_id": self.config.robot_id, "room_id": self.config.room_id,
            "chat_id": "default", "persona_id": "annie",
            "session_epoch": 1, "generation": self.generation,
        }

    def _cache_context_turn(self, turn, *, allow_generated=True):
        self.context_cache.append(dict(turn))


def receipt(kind, cid, epoch, samples, *, source="browser_test", seq=1, playback_id="test-1"):
    return {"type": "tts.playback." + kind, "data": {
        "protocol_version": 1, "conversation_id": cid, "stream_epoch": epoch,
        "played_sample_count": samples, "sample_rate": 24000, "seq": seq,
        "source": source, "playback_id": playback_id,
    }}


class FakePublisher:
    def __init__(self):
        self.messages = []
        self.closed = False

    async def publish(self, message):
        self.messages.append(message)

    async def close(self):
        self.closed = True


class FakeTts:
    instances = []

    def __init__(self, url, key):
        self.url = url
        self.sent = []
        self.received = asyncio.Queue()
        self.closed = False
        self.__class__.instances.append(self)

    async def open(self):
        return self

    async def send(self, message):
        self.sent.append(message)
        task = message["header"]["task_id"]
        if message["header"]["action"] == "run-task":
            await self.received.put({"header": {"event": "task-started", "task_id": task}})
        elif message["header"]["action"] == "finish-task":
            # Split arbitrary binary chunks across PCM sample and 20ms boundaries.
            await self.received.put(bytes([1, 0]) * 100)
            await self.received.put(bytes([2, 0]) * 900)
            await self.received.put({"header": {"event": "task-finished", "task_id": task}})

    async def recv(self):
        return await self.received.get()

    async def close(self):
        self.closed = True


class TtsContractTests(unittest.TestCase):
    def test_receipts_require_strict_sample_cursor_and_identity(self):
        playback_receipt(receipt("started", "cid", 1, 0))
        for invalid in (
            receipt("started", "cid", 0, 0),
            receipt("started", "cid", 1, -1),
            receipt("started", "cid", 1, 0, source="unknown"),
            receipt("started", "cid", 1, 0, seq=0),
        ):
            with self.assertRaises(ValueError):
                playback_receipt(invalid)
    def test_qwen_tts_not_realtime_protocol_and_fixed_model(self):
        started = command("run-task", "task1", voice="custom", sample_rate=24000)
        self.assertEqual(started["payload"]["model"], MODEL)
        self.assertEqual(started["header"]["action"], "run-task")
        self.assertEqual(started["payload"]["parameters"]["voice"], "custom")
        self.assertEqual(started["payload"]["parameters"]["format"], "pcm")
        self.assertEqual(command("continue-task", "task1", text="你好")["payload"]["input"], {"text": "你好"})
        self.assertEqual(command("finish-task", "task1")["header"]["action"], "finish-task")
        self.assertNotIn("session.update", str(started))
        self.assertEqual(validate_url("wss://dashscope.aliyuncs.com/api-ws/v1/inference"),
                         "wss://dashscope.aliyuncs.com/api-ws/v1/inference")
        with self.assertRaises(ValueError):
            validate_url("ws://dashscope.aliyuncs.com/api-ws/v1/inference")


class RoomTtsContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_publishes_only_to_owned_room_tts_with_welcome_check(self):
        import json

        class FakeConnection:
            def __init__(self):
                self.sent = []
                self.transport = self
                self.aborted = False

            async def recv(self):
                return json.dumps({
                    "type": "WELCOME", "ok": True, "clientId": "test", "robotId": "reb_tts",
                    "room": "roomv2", "size": 1, "subscriptions": [], "catalog": [],
                })

            async def send(self, payload):
                self.sent.append(json.loads(payload))

            def abort(self):
                self.aborted = True

        connection = FakeConnection()

        async def factory(url, **_kwargs):
            self.assertEqual(url, "ws://localhost:8090/robot/roomv2/reb_tts")
            return connection

        config = type("Rooms", (), {
            "url_for": lambda _, room, ch: f"ws://localhost:8090/robot/{room}/reb_{ch}",
            "channels": {"tts": "reb_tts"}, "connect_timeout_sec": 1,
        })()
        publisher = RoomTtsPublisher(config, "roomv2", socket_factory=factory)
        await publisher.publish({"type": "tts", "data": {"audio": "AA=="}})
        self.assertEqual(connection.sent, [{"type": "tts", "data": {"audio": "AA=="}}])
        await publisher.close()
        self.assertTrue(connection.aborted)

    async def test_main_subscription_receives_speaker_events_without_guessing_room(self):
        class FakeRoom:
            def __init__(self):
                self.incoming=asyncio.Queue()
                self.sent=[]
                self.transport=self
                self.aborted=False
                self.incoming.put_nowait(json.dumps({
                    "type":"WELCOME","ok":True,"clientId":"listener",
                    "robotId":"reb_main","room":"roomv2","size":1,
                    "subscriptions":[],"catalog":[],
                }))
                self.incoming.put_nowait(json.dumps({
                    "type":"ACK","data":{"what":"subscribed","subscriptions":["*"]},
                }))

            async def __aenter__(self):return self
            async def __aexit__(self,*_):return None
            async def recv(self):return await self.incoming.get()
            async def send(self,value):self.sent.append(json.loads(value))
            def abort(self):self.aborted=True
            def __aiter__(self):return self
            async def __anext__(self):return await self.recv()

        room=FakeRoom()
        def connect_test(url,**_):
            self.assertEqual(url,"ws://example/roomv2/reb_main")
            return room
        config=type("Room",(),{
            "url_for":lambda _,r,c:f"ws://example/{r}/reb_{c}",
            "channels":{"main":"reb_main"},
            "connect_timeout_sec":1,"reconnect_min_sec":0.1,"reconnect_max_sec":0.2,
        })()
        seen=[]
        listener=RoomEventListener(config,"roomv2","main",seen.append,socket_factory=connect_test)
        listener.start()
        for _ in range(40):
            if listener.connected:
                break
            await asyncio.sleep(0.005)
        self.assertTrue(listener.connected)
        self.assertEqual(room.sent,[{"type":"SUBSCRIBE","data":{"types":["*"]}}])
        message=receipt("started","cid",1,0,source="speaker")
        await room.incoming.put(json.dumps(message))
        for _ in range(40):
            if seen:
                break
            await asyncio.sleep(0.005)
        self.assertEqual(seen,[message])
        await listener.close()
        self.assertTrue(room.aborted)


class SpeechTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeTts.instances.clear()
        self.robot = FakeRobot()
        self.publisher = FakePublisher()
        self.events = []
        self.speech = SpeechOutput(
            self.robot, self.publisher, key="fake", voice="test-voice",
            url="wss://dashscope.aliyuncs.com/api-ws/v1/inference",
            socket_factory=FakeTts, event=lambda name, **data: self.events.append((name, data)),
            first_wait_ms=5,
        )
        self.addAsyncCleanup(self.speech.close)

    async def test_playback_commands_coalesce_without_task_chain_and_stop_wins(self):
        started, release = asyncio.Event(), asyncio.Event()
        original = self.publisher.publish

        async def slow_publish(packet):
            if packet["type"] == "tts.playback.hold" and not started.is_set():
                started.set()
                await release.wait()
            await original(packet)

        self.publisher.publish = slow_publish
        self.speech.turn_id = "stress"
        self.speech.identity = self.robot.session_state()
        self.speech.stream_epoch = 1
        self.assertTrue(self.speech.request_playback_control("hold", reason="speech_started"))
        await asyncio.wait_for(started.wait(), .5)
        one_worker = self.speech.control_task
        for index in range(200):
            self.assertTrue(self.speech.request_playback_control(
                "resume" if index % 2 else "hold", reason="turn_invalid"))
        self.assertIs(self.speech.control_task, one_worker)
        self.assertEqual(self.speech._control_next[0], "resume")
        self.assertLessEqual(len(self.speech.pending_controls), 1)
        release.set()
        await asyncio.wait_for(one_worker, 1)
        commands = [p["type"] for p in self.publisher.messages]
        self.assertEqual(commands, ["tts.playback.hold", "tts.playback.resume"])
        await self.speech.cancel("interrupt")
        self.assertEqual(self.publisher.messages[-1]["type"], "tts")
        self.assertTrue(self.publisher.messages[-1]["data"]["stop"])
        self.assertIsNone(self.speech._control_next)

    async def test_voice_channel_mirrors_control_with_sender_and_preserves_pcm_channel(self):
        voice = FakePublisher()
        self.speech.voice_publisher = voice
        await self.speech.feed("voice-control", self.robot.session_state(), "测试语音交互。")
        await self.speech.finish("voice-control")
        await asyncio.wait_for(self.speech.worker, 2)
        self.assertTrue(self.speech.request_playback_control("hold", reason="speech_started"))
        await asyncio.wait_for(self.speech.control_task, 1)
        await asyncio.sleep(0)
        hold = voice.messages[-1]
        self.assertEqual(hold["type"], "tts.playback.hold")
        self.assertEqual(hold["data"]["sender"], "brain")
        self.assertEqual(hold["data"]["trigger_source"], "qwen_realtime")
        self.assertEqual(hold["data"]["fade_ms"], 40)
        self.assertEqual(self.publisher.messages[-1]["type"], hold["type"])
        self.assertTrue(self.speech.request_playback_control("resume", reason="turn_invalid"))
        await asyncio.wait_for(self.speech.control_task, 1)
        await asyncio.sleep(0)
        resume = voice.messages[-1]
        self.assertEqual(resume["data"]["fade_ms"], 120)
        self.assertGreater(resume["data"]["command_seq"], hold["data"]["command_seq"])
        await self.speech.cancel("valid_smart_turn")
        await asyncio.sleep(0)
        stop = voice.messages[-1]
        self.assertEqual(stop["type"], "tts.playback.stop")
        self.assertEqual(stop["data"]["conversation_id"], "voice-control")
        self.assertEqual(stop["data"]["sender"], "brain")
        self.assertEqual(stop["data"]["trigger_source"], "qwen_realtime")
        self.assertTrue(any(packet["type"] == "tts" and packet["data"].get("stop")
                            for packet in self.publisher.messages))

    async def test_browser_controls_never_confirm_speaker_held_state(self):
        cid = "browser-controls"
        await self.speech.feed(cid, self.robot.session_state(), "暂停后恢复。")
        await self.speech.finish(cid)
        await asyncio.wait_for(self.speech.worker, 2)
        epoch = self.speech.stream_epoch
        self.assertTrue(self.speech.on_playback_receipt(receipt("started", cid, epoch, 0)))
        self.assertTrue(self.speech.request_playback_control("hold", reason="speech_started"))
        await asyncio.wait_for(self.speech.control_task, 1)
        seq = self.publisher.messages[-1]["data"]["command_seq"]
        ack = receipt("held", cid, epoch, 200, seq=2)
        ack["data"]["command_seq"] = seq
        ack["data"]["sender"] = "browser_test"
        self.assertTrue(self.speech.on_playback_receipt(ack))
        self.assertEqual(self.speech.control_state, "hold_requested")
        self.assertIn(seq, self.speech.pending_controls)
        with self.assertRaisesRegex(ValueError, "sender/source mismatch"):
            playback_receipt({**ack, "data": {**ack["data"], "sender": "speaker"}})

    async def test_short_stalled_first_phrase_reaches_tts_without_response_done(self):
        identity = self.robot.session_state()
        await self.speech.feed("short-turn", identity, "你好")
        for _ in range(100):
            if self.speech.submitted_text:
                break
            await asyncio.sleep(0.005)
        self.assertEqual(self.speech.submitted_text, "你好")
        await self.speech.finish("short-turn")
        await asyncio.wait_for(self.speech.worker, 1)
        self.assertIsNone(self.speech.error)

    async def test_first_ten_chars_are_queued_without_waiting_for_timeout(self):
        self.speech.first_wait_ms = 360
        identity = self.robot.session_state()
        await self.speech.feed("instant-phrase", identity, "今天我们就聊聊咖啡豆吧")
        self.assertEqual(self.speech.segment, 1)
        self.assertEqual(self.speech.pending, "吧")
        await self.speech.finish("instant-phrase")
        await asyncio.wait_for(self.speech.worker, 1)
        self.assertEqual(self.speech.submitted_text, "今天我们就聊聊咖啡豆吧")

    async def test_fast_long_reply_survives_slow_tts_without_blocking_realtime(self):
        """Formerly 8 queued segments -> queue_full/timeout -> Room STOP."""
        identity = self.robot.session_state()
        text = "你好，" * 120
        class SlowTts(FakeTts):
            async def send(self, message):
                if message["header"]["action"] == "continue-task":
                    await asyncio.sleep(0.005)
                await super().send(message)

        self.speech.socket_factory = SlowTts
        self.speech.max_chars = 20
        began = time.perf_counter()
        await self.speech.feed("long-turn", identity, text)
        await self.speech.finish("long-turn")
        self.assertLess(time.perf_counter() - began, 0.2)
        self.assertLessEqual(self.speech.queue.qsize(), 8)
        self.assertTrue(self.speech.pending)
        await asyncio.wait_for(self.speech.worker, 3)
        self.assertEqual(self.speech.submitted_text, text)
        self.assertEqual(len(FakeTts.instances), 1)
        self.assertFalse(self.speech.cancelled)
        self.assertIsNone(self.speech.error)
        self.assertIsNone(self.speech.snapshot()["last_stop_reason"])
        self.assertTrue(self.publisher.messages[-1]["data"]["stream_final"])
        self.assertFalse(any(packet["data"].get("stop") for packet in self.publisher.messages))

    async def test_finish_after_queue_drained_still_publishes_stream_final(self):
        identity = self.robot.session_state()
        await self.speech.feed("late-finish", identity, "你好你好你好你好你好。")
        await asyncio.wait_for(self.speech.queue.join(), 1)
        await asyncio.sleep(0.03)
        self.assertFalse(self.speech.finished)
        await self.speech.finish("late-finish")
        await asyncio.wait_for(self.speech.worker, 1)
        self.assertTrue(self.speech.synthesized_complete)
        self.assertIsNotNone(self.speech.local_output_completed_at)
        self.assertEqual(self.speech.playback_evidence, "unknown")
        self.assertTrue(any(packet["data"].get("stream_final") for packet in self.publisher.messages))

    async def test_one_actual_pcm_shared_with_room_no_second_synthesis_and_no_false_playback(self):
        identity = self.robot.session_state()
        self.robot.turn_cache["turn1"] = {"turn_id": "turn1"}
        await self.speech.feed("turn1", identity, "你好。")
        await self.speech.finish("turn1")
        await asyncio.wait_for(self.speech.worker, 2)
        self.assertEqual(len(FakeTts.instances), 1)
        actions = [item["header"]["action"] for item in FakeTts.instances[0].sent]
        self.assertEqual(actions, ["run-task", "continue-task", "finish-task"])
        frames = [m for m in self.publisher.messages if m["data"]["audio"]]
        self.assertEqual([len(base64.b64decode(m["data"]["audio"])) for m in frames], [960, 960, 80])
        self.assertTrue(all(m["data"]["sample_rate"] == 24000 for m in frames))
        self.assertTrue(all(m["data"]["text"] == "你好。" for m in frames))
        self.assertEqual(self.speech.frames, 3)
        self.assertEqual(self.speech.samples, 1000)
        self.assertEqual(self.publisher.messages[-1]["data"]["final"], True)
        self.assertIsNotNone(self.speech.first_audio_ms)
        self.assertIsNotNone(self.speech.first_publish_ms)
        self.assertEqual(self.speech.snapshot()["playback_evidence"], "unknown")
        self.assertEqual(self.speech.snapshot()["viseme"], "not_connected")
        self.assertIsNone(self.robot.turn_cache["turn1"].get("assistant_spoken_text"))
        self.assertEqual(self.robot.turn_cache["turn1"]["assistant_published_text"], "你好。")
        for frame in frames:
            d = frame["data"]
            self.assertEqual(d["playback_protocol"], "sample_cursor_v1")
            self.assertEqual(d["protocol_version"], 1)
            self.assertGreaterEqual(d["end_sample"], d["start_sample"])
            self.assertEqual(d["end_sample"]-d["start_sample"],len(base64.b64decode(d["audio"]))//2)
        self.assertTrue(self.publisher.messages[-1]["data"]["stream_final"])

    async def test_browser_test_drain_does_not_claim_robot_spoke(self):
        cid="browser-turn"
        self.robot.turn_cache[cid]={"turn_id":cid,"status":"completed"}
        await self.speech.feed(cid, self.robot.session_state(), "你好。")
        await self.speech.finish(cid)
        await asyncio.wait_for(self.speech.worker,2)
        epoch=self.speech.stream_epoch
        self.assertFalse(self.speech.on_playback_receipt(receipt("started",cid,epoch+1,0)))
        self.assertTrue(self.speech.on_playback_receipt(receipt("started",cid,epoch,0)))
        self.assertFalse(self.speech.on_playback_receipt(receipt("progress",cid,epoch,0)))
        self.assertFalse(self.speech.on_playback_receipt(receipt("progress",cid,epoch,1001,seq=2)))
        self.assertTrue(self.speech.on_playback_receipt(receipt("progress",cid,epoch,480,seq=2)))
        self.assertFalse(self.speech.on_playback_receipt(receipt("progress",cid,epoch,400,seq=3)))
        self.assertTrue(self.speech.on_playback_receipt(receipt("drained",cid,epoch,1000,seq=3)))
        self.assertEqual(self.speech.playback_evidence,"browser_test_reported_drained")
        self.assertNotIn("assistant_spoken_text",self.robot.turn_cache[cid])
        self.assertEqual(self.robot.context_cache,[])
        self.assertFalse(self.speech.on_playback_receipt(receipt("progress",cid,epoch,1000,seq=4)))

    async def test_speaker_reported_full_drain_promotes_spoken_and_context(self):
        cid="speaker-turn"
        self.robot.turn_cache[cid]={"turn_id":cid,"status":"completed"}
        await self.speech.feed(cid,self.robot.session_state(),"你好。")
        await self.speech.finish(cid)
        await asyncio.wait_for(self.speech.worker,2)
        epoch=self.speech.stream_epoch
        self.assertTrue(self.speech.on_playback_receipt(
            receipt("started",cid,epoch,0,source="speaker")))
        self.assertTrue(self.speech.on_playback_receipt(
            receipt("drained",cid,epoch,1000,source="speaker",seq=2)))
        self.assertEqual(self.robot.turn_cache[cid]["assistant_spoken_text"],"你好。")
        self.assertEqual(len(self.robot.context_cache),1)
        self.assertEqual(self.speech.playback_evidence,"speaker_reported_drained")

    async def test_room_hold_resume_requires_physical_speaker_ack_and_preserves_cursor(self):
        cid = "duplex-controls"
        await self.speech.feed(cid, self.robot.session_state(), "测试打断恢复。")
        await self.speech.finish(cid)
        await asyncio.wait_for(self.speech.worker, 2)
        epoch = self.speech.stream_epoch
        self.assertTrue(self.speech.on_playback_receipt(
            receipt("started", cid, epoch, 100, source="speaker")))
        self.assertTrue(self.speech.request_playback_control("hold", reason="speech_started"))
        await asyncio.wait_for(self.speech.control_task, 1)
        hold = self.publisher.messages[-1]
        self.assertEqual(hold["type"], "tts.playback.hold")
        self.assertEqual(hold["data"]["conversation_id"], cid)
        self.assertEqual(hold["data"]["stream_epoch"], epoch)
        self.assertEqual(self.speech.control_state, "hold_requested")
        self.assertNotEqual(self.speech.playback_evidence, "speaker_reported_held")
        ack = receipt("held", cid, epoch, 100, source="speaker", seq=2)
        ack["data"]["command_seq"] = hold["data"]["command_seq"]
        browser = {**ack, "data": {**ack["data"], "source": "browser_test",
                                   "playback_id": "browser-1"}}
        self.assertTrue(self.speech.on_playback_receipt(browser))
        self.assertEqual(self.speech.control_state, "hold_requested")
        self.assertEqual(self.speech.playback_evidence, "speaker_reported_started")
        self.assertTrue(self.speech.on_playback_receipt(ack))
        self.assertEqual(self.speech.control_state, "held_confirmed")
        self.assertEqual(self.speech.played_samples, 100)
        self.assertTrue(self.speech.request_playback_control("resume", reason="turn_invalid"))
        await asyncio.wait_for(self.speech.control_task, 1)
        resume = self.publisher.messages[-1]
        self.assertEqual(resume["type"], "tts.playback.resume")
        self.assertGreater(resume["data"]["command_seq"], hold["data"]["command_seq"])
        ack = receipt("resumed", cid, epoch, 100, source="speaker", seq=3)
        ack["data"]["command_seq"] = resume["data"]["command_seq"]
        self.assertTrue(self.speech.on_playback_receipt(ack))
        self.assertEqual(self.speech.control_state, "resumed_confirmed")
        self.assertEqual(self.speech.played_samples, 100)
        self.assertFalse(self.speech.on_playback_receipt(ack))
        self.assertTrue(self.speech.on_playback_receipt(
            receipt("progress", cid, epoch, 480, source="speaker", seq=4)))
        await self.speech.cancel("interrupt")
        self.assertFalse(self.speech.on_playback_receipt(ack))

    async def test_browser_monitor_never_blocks_real_speaker_receipts(self):
        # Separate from the hold/resume control acknowledgements below.
        cid="parallel-output"
        self.robot.turn_cache[cid]={"turn_id":cid,"status":"completed"}
        await self.speech.feed(cid,self.robot.session_state(),"你好。")
        await self.speech.finish(cid)
        await asyncio.wait_for(self.speech.worker,2)
        epoch=self.speech.stream_epoch
        # Browser listener starts first; physical speaker begins later at its
        # own sample cursor, independently of the browser's progress.
        self.assertTrue(self.speech.on_playback_receipt(
            receipt("started",cid,epoch,0)))
        self.assertTrue(self.speech.on_playback_receipt(
            receipt("progress",cid,epoch,960,seq=2)))
        self.assertTrue(self.speech.on_playback_receipt(
            receipt("started",cid,epoch,0,source="speaker",seq=1)))
        self.assertEqual(self.speech.playback_source,"speaker")
        self.assertEqual(self.speech.played_samples,0)
        self.assertTrue(self.speech.on_playback_receipt(
            receipt("progress",cid,epoch,1000,seq=3)))
        self.assertEqual(self.speech.playback_source,"speaker")
        self.assertTrue(self.speech.on_playback_receipt(
            receipt("drained",cid,epoch,1000,source="speaker",seq=2)))
        self.assertEqual(self.speech.playback_evidence,"speaker_reported_drained")
        self.assertEqual(self.robot.turn_cache[cid]["assistant_spoken_text"],"你好。")
        self.assertFalse(self.speech.on_playback_receipt(
            receipt("progress",cid,epoch,999,source="speaker",seq=3)))
        self.assertFalse(self.speech.on_playback_receipt(
            receipt("started",cid,epoch,0,source="speaker",seq=4)))

    async def test_stop_ack_of_old_epoch_does_not_claim_new_turn_stopped(self):
        await self.speech.feed("old",self.robot.session_state(),"你好。")
        await self.speech.finish("old")
        await asyncio.wait_for(self.speech.worker,2)
        old_epoch=self.speech.stream_epoch
        old_total=self.speech.samples
        self.speech.on_playback_receipt(receipt("started","old",old_epoch,0))
        await self.speech.cancel("interrupt")
        self.assertEqual(self.speech.snapshot()["last_stop_reason"], "interrupt")
        self.assertEqual(self.publisher.messages[-1]["data"]["stop"],True)
        self.assertEqual(self.publisher.messages[-1]["data"]["stream_epoch"],old_epoch)
        await self.speech.feed("new",self.robot.session_state(),"再见。")
        self.assertEqual(self.speech.stop_confirmed_ms,None)
        self.assertTrue(self.speech.on_playback_receipt(
            receipt("stopped","old",old_epoch,old_total,seq=2)))
        self.assertEqual(self.speech.stopped_conversation_id,"old")
        self.assertEqual(self.speech.playback_state,"unknown")
        self.assertFalse(self.speech.on_playback_receipt(
            receipt("progress","old",old_epoch,old_total,seq=3)))

    async def test_cancel_during_room_send_reserves_inflight_sample_end(self):
        """A frame may reach Room before send returns; stop must cover it."""
        entered = asyncio.Event()
        original = self.publisher.publish

        async def blocked_publish(message):
            if message["data"]["audio"]:
                entered.set()
                await asyncio.Event().wait()
            await original(message)

        self.publisher.publish = blocked_publish
        await self.speech.feed("inflight", self.robot.session_state(), "你好。")
        await self.speech.finish("inflight")
        await asyncio.wait_for(entered.wait(), 1)
        self.assertEqual(self.speech.samples, 0)
        self.assertEqual(self.speech.inflight_end, 480)
        await asyncio.wait_for(self.speech.cancel("interrupt"), 1)
        stop = self.publisher.messages[-1]["data"]
        self.assertTrue(stop["stop"])
        self.assertEqual(stop["start_sample"], 480)
        self.assertEqual(stop["end_sample"], 480)
        self.assertTrue(self.speech.on_playback_receipt(receipt(
            "stopped", "inflight", stop["stream_epoch"], 480,
        )))
        self.assertEqual(self.speech.stopped_conversation_id, "inflight")

    async def test_short_clauses_are_not_each_synthesized_as_a_new_voice_task(self):
        identity = self.robot.session_state()
        for chunk in ("你好，", "你好。", "我可以陪你聊", "天、解答问题。"):
            await self.speech.feed("natural", identity, chunk)
        await self.speech.finish("natural")
        await asyncio.wait_for(self.speech.worker, 2)
        sent = [item["payload"]["input"].get("text") for socket in FakeTts.instances
                for item in socket.sent if item["header"]["action"] == "continue-task"]
        self.assertEqual(sent, ["你好，你好。我可以陪你聊天、解答问题。"])
        self.assertEqual(self.speech.published_text, "".join(sent))

    async def test_multiple_chunks_share_one_task_and_one_pcm_alignment_segment(self):
        class StreamingTts(FakeTts):
            async def send(self, message):
                action = message["header"]["action"]
                self.sent.append(message)
                task = message["header"]["task_id"]
                if action == "run-task":
                    await self.received.put({"header": {"event": "task-started", "task_id": task}})
                elif action == "continue-task":
                    # Simulate provider streaming PCM while later text is still arriving.
                    await self.received.put(bytes([1, 0]) * 480)
                elif action == "finish-task":
                    await self.received.put({"header": {"event": "task-finished", "task_id": task}})

        self.speech.socket_factory = StreamingTts
        identity = self.robot.session_state()
        await self.speech.feed("duplex", identity, "第一句话结束。")
        for _ in range(100):
            if self.speech.frames:
                break
            await asyncio.sleep(.002)
        self.assertGreater(self.speech.frames, 0, "first PCM must publish before LLM finishes")
        await self.speech.feed("duplex", identity, "第二句话结束。")
        await self.speech.finish("duplex")
        await asyncio.wait_for(self.speech.worker, 2)
        sent = FakeTts.instances[0].sent
        actions = [item["header"]["action"] for item in sent]
        self.assertEqual(actions, ["run-task", "continue-task", "continue-task", "finish-task"])
        self.assertEqual(len(FakeTts.instances), 1)
        frames = [item["data"] for item in self.publisher.messages if item["data"]["audio"]]
        self.assertEqual([frame["start_sample"] for frame in frames], [0, 480])
        self.assertEqual([frame["end_sample"] for frame in frames], [480, 960])
        self.assertEqual({frame["turn_id"] for frame in frames}, {"duplex-1"})
        self.assertEqual(frames[0]["text"], "第一句话结束。")
        self.assertEqual(frames[-1]["text"], "第一句话结束。第二句话结束。")
        self.assertEqual(self.speech.published_text, "第一句话结束。第二句话结束。")
        self.assertEqual(sum(bool(item["data"].get("stream_final"))
                             for item in self.publisher.messages), 1)

    async def test_pcm_publisher_can_build_a_jitter_reserve_before_realtime_pacing(self):
        identity = self.robot.session_state()
        await self.speech._begin("reserve", identity)
        started = time.perf_counter()
        for _ in range(13):
            await self.speech._publish_pcm(bytes(960), "长句", "part-1", "reserve", identity, started)
        self.assertEqual(self.speech.samples, 13 * 480)
        self.assertLess(time.perf_counter() - started, .16)

    async def test_pcm_room_keeps_50hz_while_sse_progress_is_bounded(self):
        identity = self.robot.session_state()
        self.speech.turn_id = "performance"
        self.speech.identity = identity
        self.speech.started_at = time.perf_counter()
        for _ in range(51):
            # Eliminate clock pacing only in the fake publisher fixture.
            self.speech.next_pcm_at = None
            await self.speech._publish_pcm(
                bytes(960), "你好", "performance-1", "performance",
                identity, self.speech.started_at,
            )
        audio = [msg for msg in self.publisher.messages if msg["data"]["audio"]]
        progress = [data for kind, data in self.events if kind == "tts.pcm_published"]
        self.assertEqual(len(audio), 51)
        self.assertEqual([data["frames"] for data in progress], [1, 25, 50])
        self.assertEqual(audio[-1]["data"]["end_sample"], 51 * 480)

    async def test_only_aligned_pcm_boundaries_can_report_mouth(self):
        cid="mouth-turn"
        await self.speech.feed(cid,self.robot.session_state(),"你好。")
        await self.speech.finish(cid)
        await asyncio.wait_for(self.speech.worker,2)
        good={"type":"mouth_script","conversation_id":cid,"stream_id":"actual",
              "seq":1,"start_sample":12,"end_sample":96,"viseme":"a",
              "timing_source":"qwen3_forced_aligner_online"}
        self.assertFalse(self.speech.on_mouth_script({**good,"end_sample":1001}))
        self.assertFalse(self.speech.on_mouth_script({**good,"timing_source":"estimate"}))
        self.assertTrue(self.speech.on_mouth_script(good))
        self.assertFalse(self.speech.on_mouth_script(good))
        self.assertEqual(self.speech.viseme_state,"aligner_reporting")

    async def test_cancel_prevents_old_generation_and_marks_stop_unknown(self):
        identity = self.robot.session_state()
        await self.speech.feed("turn1", identity, "你好")
        self.robot.generation += 1
        await self.speech.cancel("session_switch")
        await self.speech.feed("turn1", identity, "迟到文字")
        self.assertEqual(self.speech.frames, 0)
        self.assertTrue(any(m["data"].get("stop") for m in self.publisher.messages))
        self.assertEqual(self.events[-1][1]["device_stopped"], "unknown")

    async def test_provider_failure_is_visible_and_clears_playback_without_fake_success(self):
        class FailedTts(FakeTts):
            async def send(self, message):
                self.sent.append(message)
                if message["header"]["action"] == "run-task":
                    await self.received.put({
                        "header": {
                            "event": "task-failed", "task_id": message["header"]["task_id"],
                            "error_code": "test_failure",
                        },
                    })

        self.speech.socket_factory = FailedTts
        await self.speech.feed("turn-failed", self.robot.session_state(), "失败测试。")
        await self.speech.finish("turn-failed")
        await asyncio.wait_for(self.speech.worker, 2)
        self.assertEqual(self.speech.snapshot()["state"], "error")
        self.assertEqual(self.speech.frames, 0)
        self.assertTrue(any(name == "tts.error" for name, _ in self.events))
        self.assertTrue(any(m["data"].get("stop") for m in self.publisher.messages))
        self.assertIsNone(self.robot.turn_cache.get("turn-failed"))

    async def test_two_robots_cannot_cross_room_or_voice(self):
        other = FakeRobot("annie-b", "roomv3")
        p2 = FakePublisher()
        output = SpeechOutput(
            other, p2, key="fake", voice="test-voice",
            url="wss://dashscope.aliyuncs.com/api-ws/v1/inference", socket_factory=FakeTts,
        )
        self.addAsyncCleanup(output.close)
        await asyncio.gather(
            self.speech.feed("a-turn", self.robot.session_state(), "甲。"),
            output.feed("b-turn", other.session_state(), "乙。"),
        )
        await asyncio.gather(self.speech.finish("a-turn"), output.finish("b-turn"))
        await asyncio.gather(
            asyncio.wait_for(self.speech.worker, 2),
            asyncio.wait_for(output.worker, 2),
        )
        self.assertTrue(all(m["data"]["conversation_id"] == "a-turn" for m in self.publisher.messages))
        self.assertTrue(all(m["data"]["conversation_id"] == "b-turn" for m in p2.messages))
        self.assertNotEqual(self.publisher, p2)
