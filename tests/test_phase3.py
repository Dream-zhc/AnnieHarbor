"""Phase 3 session, ledger, coordinator, and conversation isolation tests."""

import asyncio
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SessionRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from annie_core import RobotConfig
        from annie_runtime import HarborRuntime

        self.tmp = tempfile.TemporaryDirectory(prefix="phase3-test-", dir=ROOT / "var")
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "runtime.db"
        self.runtime = HarborRuntime(
            [
                RobotConfig("annie-a", room_id="roomv2", fake_delay_ms=0),
                RobotConfig("annie-b", room_id="roomv3", fake_delay_ms=0),
            ],
            self.db,
            conversation_history_turns=20,
        )
        await self.runtime.start()
        self.addAsyncCleanup(self.runtime.close)

    async def test_same_chat_and_persona_are_still_isolated_by_robot_identity(self):
        await self.runtime.switch_session("annie-a", chat_id="same", persona_id="annie")
        await self.runtime.switch_session("annie-b", chat_id="same", persona_id="annie")
        await self.runtime.submit("annie-a", "A only")
        await self.runtime.submit("annie-b", "B only")

        a = await self.runtime.load_conversation("annie-a", "same", "annie")
        b = await self.runtime.load_conversation("annie-b", "same", "annie")
        self.assertEqual([row["user_text"] for row in a["turns"]], ["A only"])
        self.assertEqual([row["user_text"] for row in b["turns"]], ["B only"])
        self.assertNotEqual(a["robot_id"], b["robot_id"])

    async def test_switch_session_invalidates_inflight_old_generation_without_touching_other_robot(self):
        self.runtime.robots["annie-a"].brain.config = self.runtime.robots["annie-a"].config.__class__(
            "annie-a", room_id="roomv2", fake_delay_ms=120
        )
        task_a = asyncio.create_task(self.runtime.submit("annie-a", "old chat"))
        task_b = asyncio.create_task(self.runtime.submit("annie-b", "B keeps going"))
        while self.runtime.robots["annie-a"].snapshot.current_request_id is None:
            await asyncio.sleep(0)

        old_epoch = self.runtime.session_state("annie-a")["session_epoch"]
        switched = await self.runtime.switch_session("annie-a", chat_id="new-chat", persona_id="calm")
        result_a = await task_a
        result_b = await task_b

        self.assertIn(result_a.status, {"cancelled", "discarded"})
        self.assertIsNone(result_a.reply)
        self.assertEqual(result_b.status, "completed")
        self.assertGreater(switched["session_epoch"], old_epoch)
        self.assertEqual((switched["chat_id"], switched["persona_id"]), ("new-chat", "calm"))

    async def test_full_history_persists_but_context_window_is_last_20_meaningful_turns(self):
        await self.runtime.switch_session("annie-a", chat_id="history", persona_id="annie")
        for index in range(25):
            result = await self.runtime.submit("annie-a", f"turn-{index:02d}")
            self.assertEqual(result.status, "completed")

        conversation = await self.runtime.load_conversation("annie-a", "history", "annie")
        context = await self.runtime.recent_context("annie-a")
        self.assertEqual(len(conversation["turns"]), 25)
        self.assertEqual(len(context), 20)
        self.assertEqual(context[0]["user_text"], "turn-05")
        self.assertEqual(context[-1]["user_text"], "turn-24")

    async def test_context_excludes_failed_cancelled_and_stale_results_and_prefers_spoken_text(self):
        await self.runtime.switch_session("annie-a", chat_id="meaningful", persona_id="annie")
        completed = await self.runtime.submit("annie-a", "heard question")
        await self.runtime.update_playback(
            "annie-a",
            completed.request_id,
            published_text="[FAKE annie-a] heard question",
            spoken_text="[FAKE annie-a] heard",
            evidence_status="confirmed_partial",
            playback_state="idle",
        )

        self.runtime.robots["annie-a"].brain.config = self.runtime.robots["annie-a"].config.__class__(
            "annie-a", room_id="roomv2", fake_delay_ms=120
        )
        cancelled_task = asyncio.create_task(self.runtime.submit("annie-a", "cancelled question"))
        while self.runtime.robots["annie-a"].snapshot.current_request_id is None:
            await asyncio.sleep(0)
        await self.runtime.cancel("annie-a")
        cancelled = await cancelled_task
        self.assertEqual(cancelled.status, "cancelled")

        stale_task = asyncio.create_task(self.runtime.submit("annie-a", "stale question"))
        while self.runtime.robots["annie-a"].snapshot.current_request_id is None:
            await asyncio.sleep(0)
        await self.runtime.switch_session("annie-a", chat_id="other", persona_id="annie")
        stale = await stale_task
        self.assertIn(stale.status, {"cancelled", "discarded"})
        await self.runtime.switch_session("annie-a", chat_id="meaningful", persona_id="annie")

        self.runtime.robots["annie-a"].brain.config = self.runtime.robots["annie-a"].config.__class__(
            "annie-a", room_id="roomv2", fake_delay_ms=0
        )
        failed = await self.runtime.submit("annie-a", "failed question", fail=True)
        self.assertEqual(failed.status, "failed")

        conversation = await self.runtime.load_conversation("annie-a", "meaningful", "annie")
        context = await self.runtime.recent_context("annie-a")
        self.assertEqual(len(conversation["turns"]), 4)
        self.assertEqual([row["user_text"] for row in context], ["heard question"])
        self.assertEqual(context[0]["assistant_context_text"], "[FAKE annie-a] heard")

    async def test_restart_restores_selection_bumps_epoch_and_does_not_replay_old_turns(self):
        from annie_core import RobotConfig
        from annie_runtime import HarborRuntime

        await self.runtime.switch_session("annie-a", chat_id="persisted", persona_id="annie", persona_revision=3)
        first = await self.runtime.submit("annie-a", "remember this chat")
        before = self.runtime.session_state("annie-a")
        await self.runtime.close()

        restarted = HarborRuntime([RobotConfig("annie-a", room_id="roomv2", fake_delay_ms=0)], self.db)
        await restarted.start()
        try:
            after = restarted.session_state("annie-a")
            self.assertEqual((after["chat_id"], after["persona_id"], after["persona_revision"]), ("persisted", "annie", 3))
            self.assertGreater(after["session_epoch"], before["session_epoch"])
            self.assertIsNone(restarted.robots["annie-a"].snapshot.current_request_id)
            rows = await restarted.load_conversation("annie-a", "persisted", "annie")
            self.assertEqual([row["turn_id"] for row in rows["turns"]], [first.request_id])
        finally:
            await restarted.close()

    async def test_ledger_keeps_generated_published_spoken_and_unknown_separate(self):
        await self.runtime.switch_session("annie-a", chat_id="ledger", persona_id="annie")
        result = await self.runtime.submit("annie-a", "say something")
        turn = await self.runtime.get_turn("annie-a", result.request_id)
        self.assertEqual(turn["assistant_generated_text"], "[FAKE annie-a] say something")
        self.assertIsNone(turn["assistant_published_text"])
        self.assertIsNone(turn["assistant_spoken_text"])
        self.assertEqual(turn["playback_evidence_status"], "unknown")

        updated = await self.runtime.update_playback(
            "annie-a",
            result.request_id,
            published_text=turn["assistant_generated_text"],
            spoken_text="[FAKE annie-a] say",
            evidence_status="confirmed_partial",
            playback_state="playing",
        )
        self.assertEqual(updated["assistant_spoken_text"], "[FAKE annie-a] say")
        self.assertEqual(updated["playback_evidence_status"], "confirmed_partial")
        self.assertEqual(self.runtime.coordinator_state("annie-a")["playback_state"], "playing")

    async def test_coordinator_waits_on_playback_dedupes_and_expires_requests(self):
        await self.runtime.set_playback_state("annie-a", "playing")
        first = self.runtime.request_response("annie-a", source="event", dedupe_key="touch:1", ttl_ms=30)
        duplicate = self.runtime.request_response("annie-a", source="event", dedupe_key="touch:1", ttl_ms=30)
        self.assertEqual((first["status"], first["reason"]), ("waiting", "playback_active"))
        self.assertEqual((duplicate["status"], duplicate["reason"]), ("dropped", "duplicate"))
        await asyncio.sleep(0.04)
        state = self.runtime.coordinator_state("annie-a")
        self.assertEqual(state["pending_count"], 0)
        self.assertGreaterEqual(state["expired"], 1)
        await self.runtime.set_playback_state("annie-a", "idle")
        allowed = self.runtime.request_response("annie-a", source="web", dedupe_key="manual:1", ttl_ms=100)
        allowed_duplicate = self.runtime.request_response(
            "annie-a", source="web", dedupe_key="manual:1", ttl_ms=100
        )
        self.assertEqual(allowed["status"], "allowed")
        self.assertEqual((allowed_duplicate["status"], allowed_duplicate["reason"]), ("dropped", "duplicate"))

    async def test_deleting_current_conversation_invalidates_active_turn_before_removal(self):
        self.runtime.robots["annie-a"].brain.config = self.runtime.robots["annie-a"].config.__class__(
            "annie-a", room_id="roomv2", fake_delay_ms=120
        )
        task = asyncio.create_task(self.runtime.submit("annie-a", "must not come back"))
        while self.runtime.robots["annie-a"].snapshot.current_request_id is None:
            await asyncio.sleep(0)
        deleted = await self.runtime.delete_conversation("annie-a", "default", "annie")
        result = await task
        self.assertIn(result.status, {"cancelled", "discarded"})
        self.assertEqual(deleted["deleted_turns"], 1)
        rows = await self.runtime.load_conversation("annie-a", "default", "annie")
        self.assertEqual(rows["turns"], [])

    async def test_social_state_is_scoped_correctable_expiring_and_generation_guarded(self):
        state = self.runtime.session_state("annie-a")
        await self.runtime.update_social_state(
            "annie-a",
            {"conversation_mode": "emotional_support", "advice_readiness": False},
            source="explicit_test",
            generation=state["generation"],
            ttl_ms=25,
        )
        current = await self.runtime.social_state("annie-a")
        self.assertEqual(current["conversation_mode"]["value"], "emotional_support")

        await self.runtime.update_social_state(
            "annie-a",
            {"conversation_mode": "casual"},
            source="correction",
            generation=state["generation"],
        )
        corrected = await self.runtime.social_state("annie-a")
        self.assertEqual(corrected["conversation_mode"]["value"], "casual")
        with self.assertRaisesRegex(ValueError, "stale_generation"):
            await self.runtime.update_social_state(
                "annie-a", {"humor_level": "high"}, source="late", generation=state["generation"] - 1
            )

        await self.runtime.switch_session("annie-a", chat_id="other", persona_id="annie")
        self.assertEqual(await self.runtime.social_state("annie-a"), {})

        await self.runtime.switch_session("annie-a", chat_id="default", persona_id="annie")
        await asyncio.sleep(0.03)
        expired = await self.runtime.social_state("annie-a")
        self.assertNotIn("advice_readiness", expired)


class Phase3HTTPTests(unittest.TestCase):
    def setUp(self):
        from annie_core import RobotConfig
        from annie_runtime import HarborRuntime
        from annie_web import create_app
        from fastapi.testclient import TestClient

        self.tmp = tempfile.TemporaryDirectory(prefix="phase3-http-", dir=ROOT / "var")
        self.addCleanup(self.tmp.cleanup)
        runtime = HarborRuntime(
            [RobotConfig("annie-a", room_id="roomv2", fake_delay_ms=0)],
            Path(self.tmp.name) / "api.db",
        )
        self.client = self.enterContext(TestClient(create_app(runtime, ROOT / "web/src", ROOT / "tests/html")))

    def test_frontend_session_list_load_context_and_explicit_delete_interfaces(self):
        health = self.client.get("/health").json()
        self.assertEqual(health["phase"], 3)
        self.assertEqual(health["capabilities"]["conversation"], "ledger_ready")

        switched = self.client.post(
            "/api/robots/annie-a/session",
            json={"chat_id": "web-chat", "persona_id": "annie", "persona_revision": 2},
        )
        self.assertEqual(switched.status_code, 200)
        turn = self.client.post("/api/robots/annie-a/turns", json={"text": "hello"}).json()
        self.assertEqual(turn["status"], "completed")

        conversations = self.client.get("/api/robots/annie-a/conversations").json()
        self.assertEqual(conversations[0]["chat_id"], "web-chat")
        loaded = self.client.get(
            "/api/robots/annie-a/conversations/web-chat", params={"persona_id": "annie"}
        ).json()
        self.assertEqual(loaded["turns"][0]["user_text"], "hello")
        context = self.client.get("/api/robots/annie-a/context").json()
        self.assertEqual(context[0]["user_text"], "hello")

        deleted = self.client.delete(
            "/api/robots/annie-a/conversations/web-chat", params={"persona_id": "annie"}
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json()["deleted_turns"], 1)
        self.assertEqual(
            self.client.get(
                "/api/robots/annie-a/conversations/web-chat", params={"persona_id": "annie"}
            ).json()["turns"],
            [],
        )

    def test_phase3_html_is_served(self):
        page = self.client.get("/phase-tests/phase3.html")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Phase 3", page.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
