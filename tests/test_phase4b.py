"""Phase 4B isolated TTS wire, audio ownership, cancellation and publication tests."""

import asyncio
import base64
import unittest

from annie_adapters.tts import MODEL, RoomTtsPublisher, command, validate_url
from annie_runtime.speech import SpeechOutput


class FakeRobot:
    def __init__(self, robot_id="annie-a", room_id="roomv2"):
        self.config = type("Config", (), {"robot_id": robot_id, "room_id": room_id})()
        self.generation = 1
        self.turn_cache = {}
        self.writes = []
        self.ledger = type("Ledger", (), {"enqueue": lambda _, *a, **k: self.writes.append((a, k))})()

    def session_state(self):
        return {
            "robot_id": self.config.robot_id, "room_id": self.config.room_id,
            "chat_id": "default", "persona_id": "annie",
            "session_epoch": 1, "generation": self.generation,
        }


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

    async def test_one_actual_pcm_shared_with_room_no_second_synthesis_and_no_false_playback(self):
        identity = self.robot.session_state()
        self.robot.turn_cache["turn1"] = {"turn_id": "turn1"}
        await self.speech.feed("turn1", identity, "你好。")
        await self.speech.finish("turn1")
        await asyncio.wait_for(self.speech.queue.join(), 2)
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
        await asyncio.wait_for(self.speech.queue.join(), 2)
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
            asyncio.wait_for(self.speech.queue.join(), 2),
            asyncio.wait_for(output.queue.join(), 2),
        )
        self.assertTrue(all(m["data"]["conversation_id"] == "a-turn" for m in self.publisher.messages))
        self.assertTrue(all(m["data"]["conversation_id"] == "b-turn" for m in p2.messages))
        self.assertNotEqual(self.publisher, p2)
