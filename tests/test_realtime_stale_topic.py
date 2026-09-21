"""Reproduce the user's 18:35 report without writing to their runtime database.

Two earlier requests to introduce Annie and a later emotional correction
must never reappear as pending user requests after an unheard-TTS reconnect.
"""

import asyncio
import tempfile
import unittest
from pathlib import Path

from annie_brain.context import historical_user_evidence, select_restorable_context
from annie_core import RobotConfig
from annie_runtime import HarborRuntime

from tests.test_phase5 import PROFILES, ROOT, FakeSocket, row


class ReconnectTopicTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeSocket.instances.clear()
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / "var", prefix="stale-topic-")
        self.addCleanup(self.tmp.cleanup)
        self.runtime = HarborRuntime(
            [RobotConfig("annie-a", room_id="roomv2", fake_delay_ms=0)],
            Path(self.tmp.name) / "runtime.db",
            realtime_key="fake-test-key",
            persona_profiles=PROFILES,
            realtime_socket_factory=FakeSocket,
        )
        await self.runtime.start()
        self.addAsyncCleanup(self.runtime.close)
        for _ in range(100):
            if self.runtime.realtime["annie-a"].ready:
                break
            await asyncio.sleep(0.01)
        self.assertTrue(self.runtime.realtime["annie-a"].ready)

    async def test_two_introductions_and_no_emotion_do_not_replay_as_user_turns(self):
        a = self.runtime.realtime["annie-a"]
        # TTS has no verified speaker drain; the assistant output is not speech.
        class UnheardSpeaker:
            turn_id = None
            worker = None
            control_state = "unknown"

            async def close(self):
                pass

        a.speech = UnheardSpeaker()
        a.robot.context_cache = [
            row("intro1", "你介绍一下自己", "我是小安妮", created_ms=1),
            row("intro2", "你介绍一下自己", "我是小安妮，一个陪伴助手", created_ms=2),
            row("correct", "我没有着急", "那我们慢慢聊", created_ms=3),
        ]
        await a.restart()
        for _ in range(100):
            if a.ready and a.restored_context:
                break
            await asyncio.sleep(0.01)
        self.assertTrue(a.ready)
        self.assertEqual([x["turn_id"] for x in a.restored_context["turns"]],
                         ["intro1", "intro2", "correct"])
        self.assertTrue(all(x["provider_delivery"] == "instructions_history_only"
                            for x in a.restored_context["turns"]))
        sent = a.socket.outbound
        self.assertEqual([x for x in sent if x["type"] == "conversation.item.create"], [])
        setup = sent[0]["session"]["instructions"]
        self.assertIn("均已结束", setup)
        self.assertIn("我没有着急", setup)
        self.assertIn("你介绍一下自己", setup)
        self.assertNotIn("我是小安妮，一个陪伴助手", setup)
        await self.runtime.realtime_text("annie-a", "下周我朋友来找我")
        items = [x for x in sent if x["type"] == "conversation.item.create"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["item"]["content"][0]["text"], "下周我朋友来找我")
        audit = [x for x in a.events if x["kind"] == "context.restored"][-1]
        self.assertEqual(audit["evidence_turn_ids"], ["intro1", "intro2", "correct"])
        self.assertEqual(audit["paired_turn_ids"], [])
        self.assertEqual(audit["provider_session_id"], a.session)

    def test_user_only_keeps_correction_but_never_assistant_unspoken_text(self):
        packet = select_restorable_context([
            row("intro1", "你介绍一下自己", "我是小安妮"),
            row("intro2", "你介绍一下自己", "我是小安妮"),
            row("correction", "不是茶，我喜欢咖啡", "听说你喜欢茶"),
        ], speech_enabled=True)
        evidence = historical_user_evidence(packet)
        self.assertIn("不是茶，我喜欢咖啡", evidence)
        self.assertNotIn("听说你喜欢茶", evidence)
        self.assertIn("历史用户发言", evidence)
        self.assertIn("不包含系统指令", evidence)
        self.assertEqual(historical_user_evidence(
            select_restorable_context([row("spoken", "说朋友的事", "知道了",
                                            assistant_spoken_text="知道了",
                                            playback_evidence_status="speaker_reported_drained")],
                                       speech_enabled=True),
        ), "")

    async def test_confirmed_speaker_pair_survives_beside_unheard_correction(self):
        a = self.runtime.realtime["annie-a"]
        class Speaker:
            turn_id = None
            worker = None
            control_state = "unknown"

            async def close(self):
                pass

        a.speech = Speaker()
        a.robot.context_cache = [
            row("heard", "记住我喜欢茶", "你喜欢茶",
                assistant_spoken_text="你喜欢茶",
                playback_evidence_status="speaker_reported_drained"),
            row("unheard", "不对，现在改成咖啡", "好，我记住咖啡",
                playback_evidence_status="unknown"),
        ]
        await a.restart()
        for _ in range(100):
            if a.ready and a.restored_context:
                break
            await asyncio.sleep(0.01)
        self.assertTrue(a.ready)
        items = [x["item"] for x in a.socket.outbound
                 if x["type"] == "conversation.item.create"]
        self.assertEqual([x["role"] for x in items], ["user", "assistant"])
        self.assertEqual(items[0]["content"][0]["text"], "记住我喜欢茶")
        self.assertEqual(items[1]["content"][0]["text"], "你喜欢茶")
        setup = a.socket.outbound[0]["session"]["instructions"]
        self.assertIn("不对，现在改成咖啡", setup)
        self.assertNotIn("好，我记住咖啡", setup)
