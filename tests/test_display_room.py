"""reb_display is a droppable side channel and must not wait on the room."""

import asyncio
import time
import unittest

from annie_adapters import RoomServerConfig
from annie_core import RobotConfig
from annie_runtime import HarborRuntime
from annie_runtime.display import DisplayOutbox, build_display_message


class HoldingPublisher:
    def __init__(self, *_args, **kwargs):
        self.channel = kwargs.get("channel")
        self.messages = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def publish(self, message):
        self.started.set()
        await self.release.wait()
        self.messages.append(message)

    async def close(self):
        self.release.set()


class DisplayMessageTests(unittest.TestCase):
    def test_display_message_keeps_only_screen_fields(self):
        message = build_display_message(
            "transcript.delta",
            {"ts_ms": 10, "item_id": "u1", "text": "今天", "stash": "天气",
             "chat_id": "secret-chat", "persona_id": "annie"},
            "annie-a", "roomv2",
        )
        self.assertEqual(message["type"], "display.event")
        self.assertEqual(message["description"], "用户语音识别中间文本")
        self.assertEqual(message["data"], {
            "sender": "brain", "robot_id": "annie-a", "room_id": "roomv2",
            "event": "transcript.delta", "item_id": "u1", "text": "今天", "stash": "天气",
        })
        self.assertIsNone(build_display_message(
            "viseme.aligned", {"ts_ms": 1}, "annie-a", "roomv2"))

    def test_action_state_never_claims_the_device_moved(self):
        message = build_display_message(
            "action.state",
            {"ts_ms": 2, "call_id": "c", "turn_id": "t", "action_kind": "action",
             "action_value": "listen_tilt", "status": "published_unknown", "generation": 3,
             "real_device_executed": True},
            "annie-a", "roomv2",
        )
        self.assertFalse(message["data"]["real_device_executed"])
        self.assertEqual(message["data"]["action_value"], "listen_tilt")
        finished = build_display_message(
            "action.state",
            {"ts_ms": 3, "call_id": "c", "turn_id": "t", "action_kind": "action",
             "action_value": "listen_tilt", "status": "device_finished", "generation": 3,
             "real_device_executed": True},
            "annie-a", "roomv2",
        )
        self.assertTrue(finished["data"]["real_device_executed"])


class DisplayRelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_partials_do_not_wait_for_the_room_and_keep_the_latest_text(self):
        publisher = HoldingPublisher()
        box = DisplayOutbox(publisher, "annie-a", "roomv2", lambda: {
            "enabled": False, "state": None, "candidate_speech": False,
            "deadline_remaining_ms": None, "last_keyword": None, "error": None,
        })
        started = time.monotonic()
        for index in range(40):
            box.observe("transcript.delta", {"ts_ms": index, "item_id": "u1",
                                             "text": f"字{index}", "stash": ""})
        self.assertLess(time.monotonic() - started, 0.2)
        await asyncio.wait_for(publisher.started.wait(), 1)
        self.assertEqual(publisher.messages, [])
        publisher.release.set()
        for _ in range(50):
            if publisher.messages:
                break
            await asyncio.sleep(0.01)
        self.assertTrue(publisher.messages)
        self.assertEqual(publisher.messages[0]["data"]["text"], "字39")
        await box.close()

    async def test_runtime_publishes_each_robot_on_its_own_room(self):
        published = []

        class Publisher:
            def __init__(self, *_args, **kwargs):
                self.channel = kwargs.get("channel")

            async def publish(self, message):
                published.append(message)

            async def close(self):
                return None

        names = ("main", "tts", "mic", "visualdata", "vision", "sam", "display")
        channels = {name: "reb_" + name for name in names}
        root = "ws://127.0.0.1:9/robot"
        room = RoomServerConfig(
            base_url=root + "/roomv2", room_root=root, channels=channels,
            default_urls={name: f"{root}/roomv2/{channels[name]}" for name in channels},
        )
        runtime = HarborRuntime(
            [RobotConfig("annie-a", room_id="roomv2"),
             RobotConfig("annie-b", room_id="roomv3")],
            self.tmp / "display.db", room, realtime_key="test",
            room_tts_factory=Publisher,
        )
        try:
            self.assertIsNotNone(runtime.realtime["annie-a"].display_event)
            runtime.realtime["annie-a"].publish(
                "text.delta", turn_id="t1", text="你好", delta="好",
            )
            runtime.realtime["annie-b"].publish(
                "text.delta", turn_id="t2", text="别的机器人", delta="别",
            )
            for _ in range(50):
                if len(published) >= 2:
                    break
                await asyncio.sleep(0.01)
            by_robot = {item["data"]["robot_id"]: item for item in published}
            self.assertEqual(by_robot["annie-a"]["data"]["room_id"], "roomv2")
            self.assertEqual(by_robot["annie-a"]["data"]["text"], "你好")
            self.assertEqual(by_robot["annie-b"]["data"]["room_id"], "roomv3")
            self.assertEqual(by_robot["annie-b"]["data"]["text"], "别的机器人")
            self.assertNotIn("chat_id", by_robot["annie-a"]["data"])
        finally:
            await runtime.close()

    def setUp(self):
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / "var",
                                                prefix="display-room-")
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()
