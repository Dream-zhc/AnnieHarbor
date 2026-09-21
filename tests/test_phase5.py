"""Phase 5: real restoration inputs, boundaries and no false spoken history."""

import asyncio
import tempfile
import unittest
from pathlib import Path

from annie_brain.context import EXCERPT, MAX_UNHEARD_USER_TURNS, select_restorable_context
from annie_brain.persona import load_personas, session_instructions
from annie_core import RobotConfig
from annie_runtime import HarborRuntime
from annie_web import create_app
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
PROFILES = load_personas(ROOT / "config/personas.toml")


class FakeSocket:
    instances = []

    def __init__(self, url, key):
        self.outbound = []
        self.inbound = asyncio.Queue()
        self.closed = False
        self.__class__.instances.append(self)

    async def open(self):
        await self.inbound.put({"type": "session.updated", "session": {
            "id": f"phase5-{len(self.instances)}", "modalities": ["text"],
            "turn_detection": {"type": "smart_turn"},
        }})

    async def send(self, event):
        self.outbound.append(event)

    async def recv(self):
        return await self.inbound.get()

    async def close(self):
        self.closed = True


def row(turn_id, user, reply, **overrides):
    return {"turn_id": turn_id, "user_text": user, "assistant_generated_text": reply,
            "status": "completed", "interrupted": False, "cancelled": False,
            "playback_evidence_status": "unknown", **overrides}


class ContextSelectionTests(unittest.TestCase):
    def test_three_presets_share_identity_and_do_not_invent_user_facts(self):
        self.assertEqual(set(PROFILES), {"annie", "annie-calm", "annie-lively"})
        instructions = [session_instructions(name, PROFILES, {})[0] for name in PROFILES]
        self.assertEqual(len(set(instructions)), 3)
        for text in instructions:
            self.assertIn("你是小安妮", text)
            self.assertIn("最新明确更正", text)
            self.assertIn("当前用户输入是本轮唯一待回应的问题", text)
            self.assertIn("历史只用于理解指代和更正", text)
            self.assertIn("不要描述没有发生的动作", text)
        self.assertNotIn("温和", session_instructions("unknown", PROFILES, {})[0])
        self.assertFalse(session_instructions("unknown", PROFILES, {})[1]["configured"])
        text, snapshot = session_instructions("annie", PROFILES, {
            "question_pressure": {"source": "user_setting", "value": "none"},
        })
        self.assertIn("不要用问句续话", text)
        self.assertEqual(snapshot["question_pressure"], "none")
        self.assertEqual(session_instructions("annie", PROFILES, {
            "question_pressure": {"source": "sensor", "value": "none"},
        })[1]["question_pressure"], "normal")

    def test_correction_and_negation_kept_with_newest_first_budget(self):
        history = [row("1", "喜欢茶", "好"), row("2", "不是茶，是咖啡", "收到")]
        packet = select_restorable_context(history, speech_enabled=False, char_budget=11)
        self.assertEqual([r["turn_id"] for r in packet["turns"]], ["2"])
        self.assertEqual(packet["turns"][0]["user_text"], "不是茶，是咖啡")
        self.assertEqual(packet["omitted_older_turns"], 1)
        self.assertLessEqual(packet["chars_used"], 11)
        self.assertEqual(packet["unit"], "characters_not_provider_tokens")

    def test_audio_generated_and_browser_receipt_are_not_spoken(self):
        history = [row("1", "更正是咖啡", "我答应帮你", assistant_published_text="我答应帮你",
                       playback_evidence_status="browser_test_reported_drained")]
        packet = select_restorable_context(history, speech_enabled=True)
        self.assertEqual(packet["turns"][0]["assistant_text"], "")
        self.assertEqual(packet["turns"][0]["assistant_source"], "user_only_no_speaker_drain")
        history[0]["assistant_spoken_text"] = "已经说过的承诺"
        history[0]["playback_evidence_status"] = "speaker_reported_drained"
        packet = select_restorable_context(history, speech_enabled=True)
        self.assertEqual(packet["turns"][0]["assistant_text"], "已经说过的承诺")

    def test_unheard_history_does_not_restore_twenty_unanswered_old_topics(self):
        turns = [row(str(i), f"旧问句{i}", "模型生成了却没有扬声器回执") for i in range(20)]
        packet = select_restorable_context(turns, speech_enabled=True)
        self.assertEqual([t["user_text"] for t in packet["turns"]],
                         [f"旧问句{i}" for i in range(20 - MAX_UNHEARD_USER_TURNS, 20)])
        self.assertEqual(packet["omitted_older_turns"], 20 - MAX_UNHEARD_USER_TURNS)

    def test_cancelled_and_interrupted_are_not_resurrected(self):
        packet = select_restorable_context([
            row("1", "上文", "收到"),
            row("2", "取消的话", "没有说完", cancelled=True),
            row("3", "打断的话", "没有播完", interrupted=True),
        ], speech_enabled=False)
        self.assertEqual([r["turn_id"] for r in packet["turns"]], ["1"])

    def test_oversize_assistant_is_explicit_excerpt_and_user_is_intact(self):
        packet = select_restorable_context([
            row("1", "不是茶，是咖啡", "很多字" * 100),
        ], speech_enabled=False, char_budget=40)
        self.assertEqual(packet["turns"][0]["user_text"], "不是茶，是咖啡")
        self.assertTrue(packet["turns"][0]["assistant_text"].endswith(EXCERPT))
        self.assertEqual(packet["chars_used"], 40)
        self.assertTrue(packet["turns"][0]["assistant_excerpt"])
        with self.assertRaises(ValueError):
            select_restorable_context([], speech_enabled=False, char_budget=0)


class RealtimeContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeSocket.instances.clear()
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / "var", prefix="phase5-")
        self.addCleanup(self.tmp.cleanup)
        self.runtime = HarborRuntime([
            RobotConfig("annie-a", room_id="roomv2", fake_delay_ms=0),
            RobotConfig("annie-b", room_id="roomv3", fake_delay_ms=0),
        ], Path(self.tmp.name) / "runtime.db", realtime_key="fake-test-key",
           persona_profiles=PROFILES,
           realtime_socket_factory=FakeSocket)
        await self.runtime.start()
        self.addAsyncCleanup(self.runtime.close)
        for _ in range(100):
            if all(client.ready for client in self.runtime.realtime.values()):
                break
            await asyncio.sleep(0.01)
        self.assertTrue(all(client.ready for client in self.runtime.realtime.values()))

    async def test_rebuild_restores_corrected_text_and_isolates_robot_chat_persona(self):
        a = self.runtime.realtime["annie-a"]
        b = self.runtime.realtime["annie-b"]
        for user, reply in [("我喜欢茶", "好的"), ("不是茶，是咖啡", "明白，是咖啡")]:
            await self.runtime.realtime_text("annie-a", user)
            await a.handle({"type": "response.text.delta", "delta": reply}, a.robot.generation)
            await a.handle({"type": "response.done", "response": {"status": "completed"}}, a.robot.generation)
        await self.runtime.ledger.flush()
        await a.restart()
        for _ in range(100):
            if a.ready and a.restored_context:
                break
            await asyncio.sleep(0.01)
        self.assertTrue(a.ready)
        packet = a.snapshot()["restored_context"]
        self.assertEqual([r["user_text"] for r in packet["turns"]],
                         ["我喜欢茶", "不是茶，是咖啡"])
        injected = [event for event in a.socket.outbound if event["type"] == "conversation.item.create"]
        self.assertEqual(len(injected), 4)
        self.assertEqual(injected[-2]["item"]["content"][0]["text"], "不是茶，是咖啡")
        self.assertEqual(b.snapshot()["restored_context"]["turns"], [])
        await self.runtime.switch_session("annie-a", chat_id="new", persona_id="other", persona_revision=2)
        for _ in range(100):
            if a.ready and a.restored_context and a.restored_context["chat_id"] == "new":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(a.snapshot()["restored_context"]["turns"], [])
        self.assertNotIn("我喜欢茶", str(a.socket.outbound))

    async def test_restore_with_tts_does_not_inject_unheard_reply(self):
        a = self.runtime.realtime["annie-a"]
        a.speech = object()  # selection only: never start an audio task
        try:
            a._create_turn("不要再问我")
            a.text = "我保证不再问了"
            a._finish("completed")
            self.assertEqual(a.robot.recent_context()[0]["assistant_context_text"], None)
            self.assertEqual(a.robot.recent_context()[0]["user_text"], "不要再问我")
            await self.runtime.ledger.flush()
            await a.robot._load_context_cache()
            self.assertEqual(a.robot.recent_context()[0]["user_text"], "不要再问我")
            before = len(a.socket.outbound)
            await a._restore_context()
            messages = a.socket.outbound[before:]
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0]["item"]["role"], "user")
            self.assertEqual(a.restored_context["turns"][0]["assistant_text"], "")
        finally:
            a.speech = None

    async def test_persona_switch_and_user_preference_rebuild_are_scoped(self):
        a, b = self.runtime.realtime["annie-a"], self.runtime.realtime["annie-b"]
        self.assertEqual(a.persona_snapshot["label"], "自然")
        session = self.runtime.session_state("annie-a")
        await self.runtime.update_social_state(
            "annie-a", {"question_pressure": "none"},
            source="user_setting", generation=session["generation"],
        )
        for _ in range(100):
            if a.ready and a.persona_snapshot and a.persona_snapshot["question_pressure"] == "none":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(a.persona_snapshot["question_pressure"], "none")
        self.assertIn("不要用问句续话", a.socket.outbound[0]["session"]["instructions"])
        self.assertEqual(b.persona_snapshot["question_pressure"], "normal")
        await self.runtime.switch_session(
            "annie-a", chat_id="gentle-chat", persona_id="annie-calm", persona_revision=2,
        )
        for _ in range(100):
            if a.ready and a.persona_snapshot and a.persona_snapshot["chat_id"] == "gentle-chat":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(a.persona_snapshot["label"], "温和")
        self.assertEqual(a.persona_snapshot["persona_revision"], 2)
        self.assertEqual(a.persona_snapshot["question_pressure"], "normal")
        self.assertEqual(await self.runtime.social_state("annie-a"), {})
        self.assertEqual(b.persona_snapshot["label"], "自然")
        with self.assertRaisesRegex(ValueError, "user_setting requires"):
            await self.runtime.update_social_state(
                "annie-a", {"question_pressure": "invalid"},
                source="user_setting", generation=self.runtime.session_state("annie-a")["generation"],
            )


class Phase5PageTests(unittest.TestCase):
    def test_phase5_html_is_served_from_existing_workbench(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "var", prefix="phase5-http-") as temp:
            runtime = HarborRuntime([RobotConfig("annie-a")], Path(temp) / "db.sqlite")
            with TestClient(create_app(runtime, ROOT / "web/dist", ROOT / "tests/html")) as client:
                page = client.get("/phase-tests/phase5-context.html")
                self.assertEqual(page.status_code, 200)
                self.assertIn("Phase 5", page.text)

    def test_persona_catalog_is_real_runtime_configuration(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "var", prefix="phase5-personas-") as temp:
            runtime = HarborRuntime([RobotConfig("annie-a")], Path(temp) / "db.sqlite",
                                    persona_profiles=PROFILES)
            with TestClient(create_app(runtime, ROOT / "web/dist", ROOT / "tests/html")) as client:
                response = client.get("/api/personas")
                self.assertEqual(response.status_code, 200)
                self.assertEqual([p["persona_id"] for p in response.json()], list(PROFILES))
