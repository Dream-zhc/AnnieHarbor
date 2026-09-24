"""Conversation feedback is evidence-based and does not add a second model."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from annie_brain.dialogue import DialogueState
from annie_runtime.realtime import RealtimeRobot


class DialogueStateTests(unittest.TestCase):
    def test_expired_short_reply_and_interrupt_do_not_survive_silence(self):
        with patch("annie_brain.dialogue.time.monotonic", return_value=100.0):
            state = DialogueState()
            state.observe_user("1", "嗯")
            state.observe_user("2", "好。")
            self.assertEqual(state.snapshot()["mode"], "short_ack")
            self.assertEqual(state.snapshot()["followup"], "avoid")
            state.mark_interrupt()
        with patch("annie_brain.dialogue.time.monotonic", return_value=300.0):
            self.assertEqual(state.snapshot()["followup"], "contextual")
            self.assertEqual(state.snapshot()["short_reply_streak"], 0)
            self.assertFalse(state.snapshot()["recent_user_interrupt"])
            state.observe_user("3", "好")
            self.assertEqual(state.snapshot()["short_reply_streak"], 1)
            self.assertEqual(state.instruction_hint(), "")

    def test_old_questions_do_not_suppress_later_conversation(self):
        with patch("annie_brain.dialogue.time.monotonic", return_value=100.0):
            state = DialogueState()
            state.observe_delivered("1", "你喝什么？")
            state.observe_delivered("2", "然后呢？")
            self.assertEqual(state.snapshot()["followup"], "avoid")
        with patch("annie_brain.dialogue.time.monotonic", return_value=300.0):
            self.assertEqual(state.snapshot()["delivered_question_streak"], 0)
            self.assertEqual(state.snapshot()["followup"], "contextual")
            state.observe_delivered("3", "喜欢什么茶？")
            self.assertEqual(state.snapshot()["delivered_question_streak"], 1)

    def test_short_ack_feedback_and_explicit_detail_reset(self):
        state = DialogueState()
        state.observe_user("1", "嗯")
        self.assertEqual(state.snapshot()["followup"], "contextual")
        state.observe_user("2", "好")
        self.assertEqual(state.snapshot()["followup"], "avoid")
        self.assertIn("连续简短应答", state.instruction_hint())
        state.observe_user("3", "为什么凤凰单丛那么香？详细讲讲")
        self.assertEqual(state.snapshot()["length"], "detailed")
        self.assertEqual(state.snapshot()["followup"], "contextual")
        self.assertEqual(state.instruction_hint(), "")

    def test_asr_correction_and_late_duplicate_do_not_count_twice(self):
        state = DialogueState()
        state.observe_user("1", "嗯")
        state.observe_user("1", "不是，我想问为什么")
        self.assertEqual(state.snapshot()["mode"], "correction")
        self.assertEqual(state.snapshot()["short_reply_streak"], 0)
        state.observe_user("2", "好")
        state.observe_user("2", "好")
        self.assertEqual(state.snapshot()["short_reply_streak"], 1)

    def test_unheard_reply_never_increases_question_pressure(self):
        state = DialogueState()
        state.mark_interrupt()
        self.assertIn("有效打断", state.instruction_hint())
        # Cancelled/generated text is not a delivered reply.
        self.assertEqual(state.snapshot()["delivered_question_streak"], 0)
        state.observe_delivered("assistant1", "你想喝哪种茶？")
        state.observe_delivered("assistant1", "你想喝哪种茶？")
        self.assertEqual(state.snapshot()["delivered_question_streak"], 1)
        state.observe_delivered("assistant2", "那你又想要什么？")
        self.assertEqual(state.snapshot()["followup"], "avoid")
        state.observe_delivered("assistant3", "好的。")
        self.assertEqual(state.snapshot()["followup"], "contextual")


class DialogueSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_dialogue_state_expiry_is_sent_when_new_user_arrives(self):
        class Socket:
            def __init__(self):
                self.sent = []

            async def send(self, event):
                self.sent.append(event)

        robot = SimpleNamespace(
            config=SimpleNamespace(robot_id="a"), generation=1, turn_cache={},
            session_state=lambda: {"robot_id": "a", "chat_id": "one"},
        )
        client = RealtimeRobot(robot, url="", key="")
        client.socket = Socket()
        client.ready = True
        client._visual_base_instructions = "BASE"
        with patch("annie_brain.dialogue.time.monotonic", return_value=100.0):
            client.dialogue.observe_user("1", "好")
            client.dialogue.observe_user("2", "嗯")
            old_hint = client.dialogue.instruction_hint()
            client._visual_sent_instructions = "BASE" + old_hint
            client._dialogue_sent_hint = old_hint
        with patch("annie_brain.dialogue.time.monotonic", return_value=300.0):
            client.dialogue.observe_user("3", "好")
            self.assertEqual(client.dialogue.instruction_hint(), "")
            # A stale provider hint must be cleared even though the local hint
            # expired before observe_user() compared its before/after state.
            if client._dialogue_sent_hint != client.dialogue.instruction_hint():
                client._dialogue_changed()
        for _ in range(50):
            if client.socket.sent:
                break
            await asyncio.sleep(.005)
        self.assertEqual(client.socket.sent[-1]["session"]["instructions"], "BASE")
        await client._stop_visual_task()

    async def test_only_real_speaker_drain_counts_delivered_question(self):
        robot = SimpleNamespace(
            config=SimpleNamespace(robot_id="a"), generation=1,
            turn_cache={"turn-1": {"assistant_generated_text": "你想喝茶吗？"}},
            session_state=lambda: {"robot_id": "a", "chat_id": "one"},
        )
        client = RealtimeRobot(robot, url="", key="")
        client.publish("tts.playback.drained", source="brain", turn_id="turn-1")
        self.assertEqual(client.dialogue.snapshot()["delivered_question_streak"], 0)
        client.publish("tts.playback.drained", source="speaker", turn_id="turn-1")
        client.publish("tts.playback.drained", source="speaker", turn_id="turn-1")
        self.assertEqual(client.dialogue.snapshot()["delivered_question_streak"], 1)

    async def test_policy_and_visual_updates_share_one_instruction_writer(self):
        class Socket:
            def __init__(self):
                self.sent = []

            async def send(self, event):
                self.sent.append(event)

        robot = SimpleNamespace(
            config=SimpleNamespace(robot_id="a"), generation=3, turn_cache={},
            session_state=lambda: {"robot_id": "a", "chat_id": "one",
                                   "persona_id": "annie", "generation": 3},
        )
        client = RealtimeRobot(robot, url="", key="")
        client.socket = Socket()
        client.ready = True
        client._visual_base_instructions = "BASE"
        client._visual_sent_instructions = "BASE"
        client.visual_observer = lambda: "窗外有树"
        client.dialogue.observe_user("1", "嗯")
        client.dialogue.observe_user("2", "好")
        client._dialogue_changed()
        for _ in range(50):
            if client.socket.sent:
                break
            await asyncio.sleep(.005)
        instructions = client.socket.sent[-1]["session"]["instructions"]
        self.assertIn("窗外有树", instructions)
        self.assertIn("连续简短应答", instructions)
        self.assertEqual(client.snapshot()["dialogue_policy"]["provider_hint_confirmed"], False)
        await client.handle({"type": "session.updated", "session": {"instructions": instructions}}, 3)
        self.assertEqual(client.snapshot()["dialogue_policy"]["provider_hint_confirmed"], True)
        await client._stop_visual_task()

    async def test_dialogue_only_update_does_not_fake_visual_sequence(self):
        class Socket:
            def __init__(self):
                self.sent = []

            async def send(self, event):
                self.sent.append(event)

        robot = SimpleNamespace(
            config=SimpleNamespace(robot_id="a"), generation=1, turn_cache={},
            session_state=lambda: {"robot_id": "a", "chat_id": "one"},
        )
        client = RealtimeRobot(robot, url="", key="")
        client.socket = Socket()
        client.ready = True
        client._visual_base_instructions = "BASE"
        client._visual_sent_instructions = "BASE"
        client.dialogue.observe_user("1", "嗯")
        client.dialogue.observe_user("2", "好")
        client._dialogue_changed()
        for _ in range(50):
            if client.socket.sent:
                break
            await asyncio.sleep(.005)
        self.assertEqual(client._visual_sent_seq, 0)
        self.assertTrue(any(e["kind"] == "dialogue.policy.sent" for e in client.events))
        self.assertFalse(any(e["kind"].startswith("vision.context.") for e in client.events))
        await client._stop_visual_task()

    async def test_two_robot_sessions_keep_feedback_separate(self):
        def make_robot(name):
            return SimpleNamespace(config=SimpleNamespace(robot_id=name), generation=1,
                                   turn_cache={}, session_state=lambda: {"robot_id": name})

        a = RealtimeRobot(make_robot("a"), url="", key="")
        b = RealtimeRobot(make_robot("b"), url="", key="")
        a.dialogue.observe_user("a-1", "嗯")
        a.dialogue.observe_user("a-2", "好")
        self.assertEqual(a.dialogue.snapshot()["followup"], "avoid")
        self.assertEqual(b.dialogue.snapshot()["followup"], "contextual")
        a.dialogue = DialogueState()  # session switch/reset
        self.assertEqual(a.dialogue.snapshot()["followup"], "contextual")
