"""Phase 4C conversation list preserves the revision used for UI selection."""

import re
import tempfile
import unittest
from pathlib import Path

from annie_core import RobotConfig
from annie_runtime import HarborRuntime
from annie_web import create_app
from fastapi.testclient import TestClient


class StaticWorkbenchTests(unittest.TestCase):
    def test_python_serves_the_built_workbench_and_phase4c_instructions(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="phase4c-http-", dir=root / "var") as path:
            runtime = HarborRuntime(
                [RobotConfig("annie-a", room_id="roomv2", fake_delay_ms=0)],
                Path(path) / "runtime.db",
            )
            with TestClient(create_app(runtime, root / "web/dist", root / "tests/html")) as client:
                page = client.get("/")
                self.assertEqual(page.status_code, 200)
                self.assertIn('<div id="root"></div>', page.text)
                assets = re.findall(r'(?:src|href)="(/assets/[^"]+)"', page.text)
                self.assertGreaterEqual(len(assets), 2)
                for asset in assets:
                    self.assertEqual(client.get(asset).status_code, 200)
                self.assertEqual(client.get("/pcm-playback-processor.js").status_code, 404)
                instructions = client.get("/phase-tests/phase4c.html")
                self.assertEqual(instructions.status_code, 200)
                self.assertIn("Phase 4C", instructions.text)
                self.assertEqual(client.get("/phase-tests/phase4b.html").status_code, 404)
                self.assertEqual(client.get("/health").json()["robots"][0]["robot_id"], "annie-a")


class ConversationRevisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_conversation_exposes_its_revision_per_robot(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="phase4c-", dir=root / "var") as path:
            runtime = HarborRuntime(
                [
                    RobotConfig("annie-a", room_id="roomv2", fake_delay_ms=0),
                    RobotConfig("annie-b", room_id="roomv3", fake_delay_ms=0),
                ],
                Path(path) / "runtime.db",
            )
            await runtime.start()
            try:
                await runtime.switch_session(
                    "annie-a", chat_id="shared", persona_id="annie", persona_revision=4
                )
                await runtime.switch_session(
                    "annie-b", chat_id="shared", persona_id="annie", persona_revision=2
                )
                await runtime.submit("annie-a", "A")
                await runtime.submit("annie-b", "B")
                await runtime.switch_session(
                    "annie-a", chat_id="shared", persona_id="annie", persona_revision=1
                )
                await runtime.submit("annie-a", "A with lower revision")
                conversations_a = await runtime.list_conversations("annie-a")
                conversations_b = await runtime.list_conversations("annie-b")
                self.assertEqual(conversations_a[0]["persona_revision"], 1)
                self.assertEqual(conversations_b[0]["persona_revision"], 2)
                self.assertEqual(conversations_a[0]["turn_count"], 2)
                self.assertEqual(conversations_b[0]["turn_count"], 1)
            finally:
                await runtime.close()
