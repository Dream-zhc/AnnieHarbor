"""Phase 5: real restoration inputs, boundaries and no false spoken history."""

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from annie_adapters.realtime import session_update
from annie_brain.context import EXCERPT, MAX_UNHEARD_USER_TURNS, select_restorable_context
from annie_brain.persona import load_personas, session_instructions
from annie_core import RobotConfig
from annie_runtime import HarborRuntime
from annie_runtime.realtime import IDLE_SESSION_GAP_SECONDS
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
        event = await self.inbound.get()
        if isinstance(event, BaseException):
            raise event
        return event

    async def close(self):
        self.closed = True


def row(turn_id, user, reply, **overrides):
    return {"turn_id": turn_id, "user_text": user, "assistant_generated_text": reply,
            "status": "completed", "interrupted": False, "cancelled": False,
            "playback_evidence_status": "unknown", **overrides}


class ContextSelectionTests(unittest.TestCase):
    def test_styles_do_not_contain_fixed_dialogue_or_promote_non_user_settings(self):
        for style_id, profile in PROFILES.items():
            self.assertNotIn("用户：", profile["style"], style_id)
            self.assertNotIn("小安妮：", profile["style"], style_id)
            text, snapshot = session_instructions("annie", PROFILES, {
                "persona_style": {"source": "user_setting", "value": style_id},
            })
            self.assertEqual(snapshot["style_id"], style_id)
            self.assertIn("只生成文字，不调用任何工具", text)
            self.assertIn("不写舞台旁白", text)
        self.assertEqual(session_instructions("annie", PROFILES, {
            "persona_style": {"source": "sensor", "value": "annie-lively"},
        })[1]["style_id"], "annie")
        preview = session_update("人格测试")["session"]["tools"][0]["function"]
        self.assertIn("user explicitly asks", preview["description"])
        self.assertIn("Never call for conversational text", preview["description"])

    def test_four_presets_share_identity_and_do_not_invent_user_facts(self):
        self.assertEqual(set(PROFILES), {"annie", "annie-calm", "annie-lively", "annie-roast"})
        instructions = [session_instructions(name, PROFILES, {})[0] for name in PROFILES]
        self.assertEqual(len(set(instructions)), 4)
        for text in instructions:
            self.assertIn("你是小安妮", text)
            self.assertIn("最新明确更正", text)
            self.assertIn("当前用户输入是本轮唯一待回应的问题", text)
            self.assertIn("历史只用于理解指代和更正", text)
            self.assertIn("不要描述没有发生的动作", text)
            self.assertIn("一次性要求在当轮执行后即结束", text)
            self.assertIn("用户再次开口或换话题时", text)
            self.assertIn("回复长度由当轮需要决定", text)
            self.assertIn("不为短而漏掉重点", text)
            self.assertNotIn("普通闲聊通常一到三句", text)
        self.assertIn("有主见的熟人", instructions[0])
        self.assertIn("知心朋友", instructions[1])
        self.assertIn("反差、双关或荒诞", instructions[2])
        self.assertIn("有来有回的损友", instructions[3])
        self.assertIn("不人身评价", instructions[3])
        self.assertIn("邀请你怼也不是允许辱骂", instructions[3])
        self.assertIn("工作焦虑后", instructions[3])
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

    def test_confirmed_interrupted_user_is_preserved_without_unspoken_assistant(self):
        history = [
            row("1", "我喜欢茶", "一大段没说完的茶建议",
                status="cancelled", cancelled=True, interrupted=True,
                cancel_source="valid_smart_turn",
                assistant_spoken_text="误写的回复",
                playback_evidence_status="speaker_reported_drained"),
            row("2", "不是茶，是咖啡", "只生成还没播的答复"),
        ]
        packet = select_restorable_context(history, speech_enabled=True)
        self.assertEqual([item["user_text"] for item in packet["turns"]],
                         ["我喜欢茶", "不是茶，是咖啡"])
        self.assertEqual(packet["turns"][0]["assistant_text"], "")
        self.assertEqual(packet["turns"][0]["assistant_source"], "interrupted_user_only")
        self.assertEqual(packet["turns"][1]["assistant_text"], "")
        self.assertEqual(select_restorable_context(history, speech_enabled=False)["turns"][
            0]["turn_id"], "2")
        for source in ("local", "session_replaced", "cascade_llm_error"):
            history[0]["cancel_source"] = source
            self.assertEqual([item["turn_id"] for item in
                              select_restorable_context(history, speech_enabled=True)["turns"]],
                             ["2"])

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

    async def test_one_hour_old_command_is_labelled_history_without_changing_ledger(self):
        a = self.runtime.realtime["annie-a"]
        old_ms = time.time_ns() // 1_000_000 - 61 * 60_000
        a.robot.context_cache.append(row(
            "old-command", "退下吧，退下吧。", "好嘞，这就退下。",
            created_ms=old_ms, assistant_context_text="好嘞，这就退下。",
        ))
        before = len(a.socket.outbound)
        await a._restore_context()
        outgoing = a.socket.outbound[before:]
        self.assertEqual(outgoing[0]["item"]["role"], "user")
        labelled = outgoing[0]["item"]["content"][0]["text"]
        self.assertIn("约1小时前", labelled)
        self.assertIn("非当前指令", labelled)
        self.assertTrue(labelled.endswith("退下吧，退下吧。"))
        self.assertEqual(a.robot.context_cache[-1]["user_text"], "退下吧，退下吧。")
        self.assertEqual(a.restored_context["turns"][-1]["user_text"], "退下吧，退下吧。")
        self.assertEqual(a.restored_context["turns"][-1]["provider_user_text"], labelled)
        self.assertLessEqual(a.restored_context["chars_used"], a.restore_char_budget)
        self.assertEqual(self.runtime.realtime["annie-b"].restored_context["turns"], [])

    async def test_idle_session_rotates_before_next_topic_without_extra_llm(self):
        a = self.runtime.realtime["annie-a"]
        b = self.runtime.realtime["annie-b"]
        a.robot.context_cache.append(row(
            "old-command", "退下吧，退下吧。", "这就退下。",
            created_ms=time.time_ns() // 1_000_000 - 60 * 60_000,
            assistant_context_text="这就退下。",
        ))
        old_socket = a.socket
        a._last_conversation_activity_t -= IDLE_SESSION_GAP_SECONDS + 1
        self.assertTrue(a._idle_recycle_due())
        self.assertFalse(b._idle_recycle_due())
        # Exercise the real recv timeout → close → new WS Session path.
        await old_socket.inbound.put(asyncio.TimeoutError())
        for _ in range(100):
            if a.socket is not old_socket and a.ready and a.restored_context:
                break
            await asyncio.sleep(.01)
        self.assertIsNot(a.socket, old_socket)
        self.assertTrue(old_socket.closed)
        self.assertTrue(a.ready)
        self.assertEqual(b.socket.closed, False)
        self.assertIn("非当前指令", a.socket.outbound[1]["item"]["content"][0]["text"])
        self.assertTrue(any(e["kind"] == "session.idle_recycle" for e in a.events))
        self.assertFalse(a._idle_recycle_due())
        a._last_conversation_activity_t -= IDLE_SESSION_GAP_SECONDS + 1
        self.assertFalse(a._idle_recycle_due())  # once per idle period, not every 15 min
        await self.runtime.realtime_text("annie-a", "我喜欢喝茶")
        self.assertEqual(a.socket.outbound[-2]["item"]["content"][0]["text"], "我喜欢喝茶")
        self.assertEqual(a.socket.outbound[-1]["type"], "response.create")
        self.assertFalse(a._idle_recycle_due())

    async def test_busy_playback_does_not_recycle_old_session(self):
        a = self.runtime.realtime["annie-a"]
        a._last_conversation_activity_t -= IDLE_SESSION_GAP_SECONDS + 1
        a._create_turn("新消息")
        self.assertFalse(a._idle_recycle_due())
        a._finish("completed")
        self.assertFalse(a._idle_recycle_due())
        a._last_conversation_activity_t -= IDLE_SESSION_GAP_SECONDS + 1
        worker = asyncio.Future()
        a.speech = type("ActiveSpeaker", (), {"turn_id": "playing", "worker": worker,
                                             "control_state": "unknown"})()
        try:
            self.assertFalse(a._idle_recycle_due())
            worker.set_result(None)
            self.assertTrue(a._idle_recycle_due())  # SpeechOutput retains the last turn_id
            a.speech.control_state = "held_confirmed"
            self.assertFalse(a._idle_recycle_due())
        finally:
            a.speech = None

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
            # This is a past completed utterance, not another pending user
            # request. No unsaid assistant reply and no fresh user item.
            self.assertEqual(messages, [])
            self.assertEqual(a.restored_context["turns"][0]["assistant_text"], "")
            self.assertEqual(a.restored_context["turns"][0]["provider_delivery"],
                             "instructions_history_only")
        finally:
            a.speech = None

    async def test_realtime_cloud_window_is_separate_from_saved_twenty_turns(self):
        a = self.runtime.realtime["annie-a"]
        self.assertEqual(a.history_turns, 8)
        self.assertEqual(a.restore_char_budget, 3000)
        self.assertEqual(a.socket.outbound[0]["session"]["max_history_turns"], 8)
        for index in range(20):
            a.robot.context_cache.append(row(
                str(index), f"问题{index}", f"回答{index}",
                assistant_context_text=f"回答{index}",
            ))
        a.robot.context_cache = a.robot.context_cache[-20:]
        before = len(a.socket.outbound)
        await a._restore_context()
        restored = a.restored_context
        self.assertEqual(len(restored["turns"]), 8)
        self.assertEqual(restored["turns"][0]["user_text"], "问题12")
        self.assertEqual(restored["turns"][-1]["user_text"], "问题19")
        self.assertEqual(restored["max_turns"], 8)
        self.assertEqual(restored["char_budget"], 3000)
        self.assertLessEqual(restored["chars_used"], 3000)
        self.assertEqual(len(a.socket.outbound) - before, 16)
        # A new turn must not trigger another history injection.
        await self.runtime.realtime_text("annie-a", "当前问题")
        self.assertEqual(len(a.socket.outbound) - before, 18)
        self.assertEqual(a.snapshot()["provider_history_turns"], 8)
        self.assertIsNone(a.snapshot()["provider_history_turns_confirmed"])
        self.assertEqual(a.snapshot()["restore_char_budget"], 3000)
        self.assertEqual(self.runtime.realtime["annie-b"].restored_context["turns"], [])

    async def test_startup_history_char_budget_is_not_a_token_count(self):
        a = self.runtime.realtime["annie-a"]
        a.robot.context_cache = [row(
            str(index), f"问题{index}"+"中"*850, "无声回复",
            assistant_context_text="无声回复",
        ) for index in range(8)]
        a.speech = object()  # Selection only: no TTS task.
        try:
            await a._restore_context()
            self.assertLessEqual(a.restored_context["chars_used"], 3000)
            self.assertEqual(a.restored_context["unit"], "characters_not_provider_tokens")
            self.assertLess(len(a.restored_context["turns"]), 8)
            self.assertTrue(a.restored_context["turns"][-1]["user_text"].startswith("问题7"))
        finally:
            a.speech = None

    async def test_confirmed_interruption_survives_sqlite_reload_as_user_only(self):
        a = self.runtime.realtime["annie-a"]
        a.speech = object()
        try:
            a._create_turn("不是茶，是咖啡")
            a.text = "旧答复还没有播完"
            a._finish("cancelled", cancel_source="valid_smart_turn")
            self.assertEqual(a.robot.recent_context()[-1]["user_text"],
                             "不是茶，是咖啡")
            await self.runtime.ledger.flush()
            await a.robot._load_context_cache()
            packet = select_restorable_context(a.robot.recent_context(),
                                                speech_enabled=True)
            self.assertEqual(packet["turns"][-1]["user_text"], "不是茶，是咖啡")
            self.assertEqual(packet["turns"][-1]["assistant_text"], "")
            self.assertEqual(packet["turns"][-1]["assistant_source"], "interrupted_user_only")
        finally:
            a.speech = None

    async def test_persona_switch_and_user_preference_rebuild_are_scoped(self):
        a, b = self.runtime.realtime["annie-a"], self.runtime.realtime["annie-b"]
        self.assertEqual(a.persona_snapshot["label"], "自然陪伴")
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
        self.assertEqual(a.persona_snapshot["label"], "温柔知心")
        self.assertEqual(a.persona_snapshot["persona_revision"], 2)
        self.assertEqual(a.persona_snapshot["question_pressure"], "normal")
        self.assertEqual(await self.runtime.social_state("annie-a"), {})
        self.assertEqual(b.persona_snapshot["label"], "自然陪伴")
        with self.assertRaisesRegex(ValueError, "user_setting requires"):
            await self.runtime.update_social_state(
                "annie-a", {"question_pressure": "invalid"},
                source="user_setting", generation=self.runtime.session_state("annie-a")["generation"],
            )

    async def test_current_chat_style_switch_keeps_history_and_rebuilds_provider(self):
        a, b = self.runtime.realtime["annie-a"], self.runtime.realtime["annie-b"]
        original = self.runtime.session_state("annie-a")
        previous_socket = a.socket
        await self.runtime.realtime_text("annie-a", "今天喜欢喝茶")
        await a.handle({"type": "response.text.delta", "delta": "茶很有意思"}, a.robot.generation)
        await a.handle({"type": "response.done", "response": {"status": "completed"}}, a.robot.generation)
        await self.runtime.ledger.flush()
        earlier = await self.runtime.load_conversation(
            "annie-a", original["chat_id"], original["persona_id"])
        self.assertEqual(len(earlier["turns"]), 1)

        # The style is a chat-scoped setting, not a destructive persona_id migration.
        await self.runtime.update_social_state(
            "annie-a", {"persona_style": "annie-lively"},
            source="user_setting", generation=original["generation"],
        )
        for _ in range(100):
            if a.ready and a.persona_snapshot and a.persona_snapshot["style_id"] == "annie-lively":
                break
            await asyncio.sleep(.01)
        self.assertTrue(a.ready)
        self.assertIsNot(a.socket, previous_socket)
        self.assertEqual(self.runtime.session_state("annie-a"), original)
        self.assertEqual(a.persona_snapshot["label"], "活泼俏皮")
        self.assertIn("不写舞台动作或旁白", a.socket.outbound[0]["session"]["instructions"])
        self.assertEqual(b.persona_snapshot["style_id"], "annie")
        current = await self.runtime.load_conversation(
            "annie-a", original["chat_id"], original["persona_id"])
        self.assertEqual(current["turns"], earlier["turns"])
        self.assertEqual(a.restored_context["turns"][0]["user_text"], "今天喜欢喝茶")
        self.assertEqual((await self.runtime.social_state("annie-a"))[
            "persona_style"]["value"], "annie-lively")

        await self.runtime.switch_session(
            "annie-a", chat_id="other-chat", persona_id=original["persona_id"],
        )
        for _ in range(100):
            if a.ready and a.persona_snapshot and a.persona_snapshot["chat_id"] == "other-chat":
                break
            await asyncio.sleep(.01)
        self.assertEqual(a.persona_snapshot["style_id"], "annie")
        await self.runtime.switch_session(
            "annie-a", chat_id=original["chat_id"], persona_id=original["persona_id"],
        )
        for _ in range(100):
            if a.ready and a.persona_snapshot and a.persona_snapshot["chat_id"] == original["chat_id"]:
                break
            await asyncio.sleep(.01)
        self.assertEqual(a.persona_snapshot["style_id"], "annie-lively")
        await self.runtime.update_social_state(
            "annie-a", {"persona_style": "annie-roast"},
            source="user_setting", generation=a.robot.generation,
        )
        for _ in range(100):
            if a.ready and a.persona_snapshot and a.persona_snapshot["style_id"] == "annie-roast":
                break
            await asyncio.sleep(.01)
        self.assertEqual(a.persona_snapshot["label"], "机灵互怼")
        self.assertIn("邀请你怼也不是允许辱骂", a.socket.outbound[0]["session"]["instructions"])
        self.assertEqual(b.persona_snapshot["style_id"], "annie")
        self.assertEqual((await self.runtime.load_conversation(
            "annie-a", original["chat_id"], original["persona_id"]))["turns"], earlier["turns"])
        with self.assertRaisesRegex(ValueError, "user_setting requires"):
            await self.runtime.update_social_state(
                "annie-a", {"persona_style": "unconfigured"},
                source="user_setting", generation=a.robot.generation,
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
