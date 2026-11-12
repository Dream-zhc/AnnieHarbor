"""11-03: answer the sentence just said, in her own voice, without repeating herself."""

import asyncio
import tempfile
import unittest
from pathlib import Path

from annie_brain.inner_life import thought_prompt
from annie_core import RobotConfig
from annie_runtime import HarborRuntime
from annie_runtime.realtime import CURRENT_TURN_RULE, visual_instructions
from annie_runtime.wakeword import WakeAdmission

from tests.test_face_registration import FakeRegistry, run_live
from tests.test_phase4_realtime import ROOT, FakeSocket


def test_visual_answers_take_facts_from_the_view_and_words_from_the_persona():
    text = visual_instructions("base", "【眼前画面】左边有一扇玻璃门。")
    assert "说法用你当前人格平时说话的口吻" in text and "不照念描述里的句子" in text
    assert "已有话题" not in text
    assert "用你自己的话重新说" in thought_prompt("到饭点了", mood="心情平静")


def test_sessions_answer_the_current_sentence_and_events_see_her_recent_lines(tmp_path):
    async def body(rt, client):
        setup = next(item for item in client.socket.outbound if item["type"] == "session.update")
        assert CURRENT_TURN_RULE in setup["session"]["instructions"]
        client.robot.context_cache.append(
            {"turn_id": "t0", "user_text": "你好", "assistant_context_text": "小明，你来了呀。"})
        event = "【外部事件，非用户发言】现场情况：有人摸了摸你的头。"
        accepted = await client.submit_text(event, source="external")
        sent = [item["item"]["content"][0]["text"] for item in client.socket.outbound
                if item["type"] == "conversation.item.create"
                and item["item"].get("type") == "message"]
        assert sent[-1] == event + "\n你最近说过：「小明，你来了呀。」。这次换个说法，不要重复这些话。"
        assert client.robot.cached_turn(accepted["turn_id"])["external_prompt"] == event

    run_live(tmp_path, FakeRegistry(), body)


class HeldReplyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeSocket.instances.clear()
        tmp = tempfile.TemporaryDirectory(dir=ROOT / "var", prefix="reply-focus-")
        self.addCleanup(tmp.cleanup)
        self.runtime = HarborRuntime(
            [RobotConfig("annie-a", room_id="roomv2", fake_delay_ms=0, listen_enabled=True)],
            Path(tmp.name) / "runtime.db", realtime_key="fake-test-key",
            realtime_socket_factory=FakeSocket)
        await self.runtime.start()
        self.addAsyncCleanup(self.runtime.close)
        self.client = self.runtime.realtime["annie-a"]
        for _ in range(100):
            if self.client.ready:
                break
            await asyncio.sleep(.01)

        class Detector:
            async def detected(self, _):
                return False

        self.client.wake = WakeAdmission(Detector())
        self.client.wake.presence.wake_detected()
        self.client.robot.context_cache.append(
            {"turn_id": "t0", "user_text": "我来了", "assistant_context_text": "小明，你来了呀。"})

    async def hear(self, item_id, events, transcript):
        generation = self.client.robot.generation
        await self.client.handle({"type": "input_audio_buffer.speech_started",
                                  "item_id": item_id}, generation)
        for event in events:
            await self.client.handle(event, generation)
        self.assertIsNone(self.client.turn_id)  # held until the final ASR
        await self.client.handle({"type": "conversation.item.input_audio_transcription.completed",
                                  "item_id": item_id, "transcript": transcript}, generation)

    @staticmethod
    def reply(response_id, item_id, text, *, done=True):
        return [
            {"type": "response.created", "response": {"id": response_id}},
            {"type": "response.output_item.added", "response_id": response_id,
             "item": {"id": item_id, "type": "message", "role": "assistant"}},
            {"type": "response.text.delta", "response_id": response_id, "delta": text},
        ] + ([{"type": "response.done",
               "response": {"id": response_id, "status": "completed"}}] if done else [])

    async def test_only_the_newest_held_reply_is_released(self):
        # 11-02 16:46:16: the answer to her own echo went out before the real one.
        await self.hear("q1", self.reply("echo", "echo-item", "没错，就是那扇门")
                        + self.reply("real", "real-item", "我看看北京的天气", done=False),
                        "北京天气怎么样")
        self.assertEqual(self.client.response_id, "real")
        self.assertEqual(self.client.text, "我看看北京的天气")
        self.assertIn({"type": "conversation.item.delete", "item_id": "echo-item"},
                      self.client.socket.outbound)
        self.assertNotIn({"type": "conversation.item.delete", "item_id": "real-item"},
                         self.client.socket.outbound)

    async def test_a_finished_held_reply_repeating_her_last_line_is_asked_again(self):
        # 11-02 18:56:39: “今晚天气怎么样” got her previous greeting once more.
        await self.hear("q2", self.reply("dup", "dup-item", "小明，你来了呀"), "今晚天气怎么样")
        outbound = self.client.socket.outbound
        self.assertEqual(self.client.text, "")
        self.assertIsNotNone(self.client.turn_id)
        self.assertIsNone(self.client.response_id)
        self.assertIn({"type": "conversation.item.delete", "item_id": "dup-item"}, outbound)
        notes = [item["item"]["content"][0]["text"] for item in outbound
                 if item["type"] == "conversation.item.create"
                 and "已经撤回" in item["item"]["content"][0].get("text", "")]
        self.assertEqual(len(notes), 1)
        self.assertIn("「今晚天气怎么样」", notes[0])
        self.assertEqual(outbound[-1]["type"], "response.create")
        self.assertEqual(self.client._recovered_create["text"], "今晚天气怎么样")

    async def test_asked_to_say_it_again_she_says_it_again(self):
        await self.hear("q3", self.reply("again", "again-item", "小明，你来了呀"), "你再说一遍")
        self.assertNotIn({"type": "conversation.item.delete", "item_id": "again-item"},
                         self.client.socket.outbound)
        self.assertFalse(any("已经撤回" in str(item) for item in self.client.socket.outbound))
