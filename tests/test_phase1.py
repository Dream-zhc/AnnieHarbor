"""Phase 1 actual Fake runtime tests: no old imports or external services."""

import ast
import asyncio
import errno
import importlib.util
import json
import os
import signal
import socket
import sqlite3
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]


def require_runtime(case):
    case.assertIsNotNone(importlib.util.find_spec("annie_runtime"), "Phase 1 runtime missing")
    from annie_core import RobotConfig
    from annie_runtime import HarborRuntime

    return HarborRuntime, RobotConfig


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        Runtime, Config = require_runtime(self)
        (ROOT / "var").mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="phase1-test-", dir=ROOT / "var")
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "runtime.db"
        self.runtime = Runtime(
            [Config("annie-a", fake_delay_ms=180, queue_capacity=1), Config("annie-b", fake_delay_ms=350)],
            self.db,
        )
        await self.runtime.start()
        self.addAsyncCleanup(self.runtime.close)

    def head(self, robot_id):
        return next(h for h in self.runtime.health()["robots"] if h["robot_id"] == robot_id)

    async def active(self, *ids):
        async def wait():
            while not all(self.head(i)["current_request_id"] for i in ids):
                await asyncio.sleep(0)

        await asyncio.wait_for(wait(), 2)

    async def pair(self, fail=False):
        a = asyncio.create_task(self.runtime.submit("annie-a", "A", fail=fail))
        b = asyncio.create_task(self.runtime.submit("annie-b", "B"))
        await self.active("annie-a", "annie-b")
        return a, b, self.head("annie-b")["current_request_id"]

    async def assert_b(self, task, request_id):
        result = await asyncio.wait_for(task, 2)
        self.assertEqual(
            (result.status, result.robot_id, result.request_id), ("completed", "annie-b", request_id)
        )
        self.assertEqual(result.reply, "[FAKE annie-b] B")
        another = await self.runtime.submit("annie-b", "继续")
        self.assertEqual(another.status, "completed")
        self.assertTrue(self.runtime.health()["ready"])

    async def test_two_heads_run_concurrently_in_one_process(self):
        a, b, request_id = await self.pair()
        self.assertEqual(self.runtime.health()["pid"], os.getpid())
        self.assertEqual({h["room_id"] for h in self.runtime.health()["robots"]}, {"demo-room"})
        self.assertEqual((await a).status, "completed")
        await self.assert_b(b, request_id)

    async def test_runtime_supports_more_than_two_heads_concurrently(self):
        Runtime, Config = require_runtime(self)
        extra = Runtime(
            [
                Config("head-1", fake_delay_ms=80),
                Config("head-2", fake_delay_ms=80),
                Config("head-3", fake_delay_ms=80),
            ],
            Path(self.tmp.name) / "three-heads.db",
        )
        await extra.start()
        try:
            started = time.perf_counter()
            results = await asyncio.gather(
                extra.submit("head-1", "A"),
                extra.submit("head-2", "B"),
                extra.submit("head-3", "C"),
            )
            elapsed = time.perf_counter() - started
            self.assertEqual({result.robot_id for result in results}, {"head-1", "head-2", "head-3"})
            self.assertLess(elapsed, 0.20, "three heads should run concurrently, not serially")
            self.assertEqual(extra.health()["pid"], os.getpid())
        finally:
            await extra.close()

    async def test_stop_one_head_does_not_stop_inflight_other_head(self):
        a, b, request_id = await self.pair()
        await self.runtime.stop_robot("annie-a")
        self.assertEqual(self.head("annie-b")["current_request_id"], request_id)
        self.assertEqual((await a).status, "cancelled")
        self.assertEqual(self.head("annie-a")["state"], "stopped")
        await self.assert_b(b, request_id)
        await self.runtime.start_robot("annie-a")
        self.assertEqual((await self.runtime.submit("annie-a", "重启")).status, "completed")

    async def test_cancel_only_current_head_turn_worker_remains_usable(self):
        a, b, request_id = await self.pair()
        await self.runtime.cancel("annie-a")
        self.assertEqual(self.head("annie-b")["current_request_id"], request_id)
        self.assertEqual((await a).status, "cancelled")
        self.assertEqual(self.head("annie-a")["cancelled"], 1)
        await self.assert_b(b, request_id)
        self.assertEqual((await self.runtime.submit("annie-a", "下一轮")).status, "completed")

    async def test_real_fake_exception_is_isolated_and_restartable(self):
        a, b, request_id = await self.pair(fail=True)
        result = await a
        self.assertEqual(result.status, "failed")
        self.assertIn("Injected FakeBrain failure", result.error)
        self.assertEqual(self.head("annie-a")["state"], "failed")
        self.assertEqual(self.head("annie-b")["current_request_id"], request_id)
        await self.assert_b(b, request_id)
        self.assertEqual(self.runtime.health()["status"], "degraded")
        await self.runtime.start_robot("annie-a")
        self.assertEqual((await self.runtime.submit("annie-a", "恢复")).status, "completed")

    async def test_full_queue_and_stop_resolve_pending_without_blocking_b(self):
        from annie_core import RobotQueueFullError

        a, b, request_id = await self.pair()
        pending = asyncio.create_task(self.runtime.submit("annie-a", "queued"))
        while self.head("annie-a")["queue_depth"] != 1:
            await asyncio.sleep(0)
        with self.assertRaises(RobotQueueFullError):
            await self.runtime.submit("annie-a", "overflow")
        await self.runtime.stop_robot("annie-a")
        self.assertEqual((await asyncio.wait_for(pending, 1)).status, "discarded")
        await a
        await self.assert_b(b, request_id)

    async def test_failure_resolves_queued_requests(self):
        a = asyncio.create_task(self.runtime.submit("annie-a", "fail", fail=True))
        await self.active("annie-a")
        queued = asyncio.create_task(self.runtime.submit("annie-a", "queued"))
        self.assertEqual((await a).status, "failed")
        self.assertEqual((await asyncio.wait_for(queued, 1)).status, "discarded")

    async def test_turn_elapsed_includes_queue_wait_and_exposes_service_time(self):
        first = asyncio.create_task(self.runtime.submit("annie-a", "first"))
        await self.active("annie-a")
        second = asyncio.create_task(self.runtime.submit("annie-a", "second"))
        first_result = await first
        second_result = await second

        self.assertLess(first_result.queue_wait_ms, first_result.elapsed_ms)
        self.assertGreater(second_result.queue_wait_ms, 100)
        self.assertGreater(second_result.service_ms, 100)
        self.assertGreater(second_result.elapsed_ms, second_result.service_ms)
        self.assertAlmostEqual(
            second_result.elapsed_ms,
            second_result.queue_wait_ms + second_result.service_ms,
            delta=10,
        )

    async def test_client_task_cancellation_does_not_cancel_robot_worker(self):
        task = asyncio.create_task(self.runtime.submit("annie-a", "client gone"))
        await self.active("annie-a")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.25)
        self.assertEqual(self.head("annie-a")["completed"], 1)
        self.assertEqual((await self.runtime.submit("annie-a", "next")).status, "completed")

    async def test_shutdown_drains_active_and_queued_and_releases_workers(self):
        a, b, _ = await self.pair()
        queued = asyncio.create_task(self.runtime.submit("annie-a", "queued"))
        await asyncio.sleep(0)
        await self.runtime.close()
        self.assertEqual((await a).status, "cancelled")
        self.assertEqual((await b).status, "cancelled")
        self.assertEqual((await queued).status, "discarded")
        self.assertFalse(self.runtime.health()["ready"])
        self.assertFalse([t for t in asyncio.all_tasks() if t.get_name().startswith("harbor:")])

    async def test_sqlite_idempotent_migration_and_isolated_persistence(self):
        from annie_adapters import RuntimeStore

        await self.runtime.submit("annie-a", "one")
        await self.runtime.submit("annie-b", "two")
        await self.runtime.recorder.flush()
        store = RuntimeStore(self.db)
        store.migrate()
        store.migrate()
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute("pragma user_version").fetchone()[0], RuntimeStore.SCHEMA_VERSION)
            self.assertEqual(db.execute("pragma journal_mode").fetchone()[0], "wal")
            rows = dict(db.execute("select robot_id, snapshot from runtime_snapshots"))
            self.assertEqual(json.loads(rows["annie-a"])["last_reply"], "[FAKE annie-a] one")
            self.assertEqual(json.loads(rows["annie-b"])["last_reply"], "[FAKE annie-b] two")
            self.assertGreater(db.execute("select count(*) from runtime_events").fetchone()[0], 2)

    async def test_all_stopped_health_not_ready_and_external_capabilities_honest(self):
        await self.runtime.stop_robot("annie-a")
        await self.runtime.stop_robot("annie-b")
        health = self.runtime.health()
        self.assertFalse(health["ready"])
        self.assertEqual(health["capabilities"]["brain"], "fake")
        self.assertEqual(health["capabilities"]["tts"], "not_connected")

    async def test_configured_disabled_head_does_not_degrade_active_head(self):
        Runtime, Config = require_runtime(self)
        other = Runtime(
            [Config("active", fake_delay_ms=0), Config("disabled", enabled=False)],
            Path(self.tmp.name) / "disabled.db",
        )
        await other.start()
        try:
            health = other.health()
            self.assertTrue(health["ready"])
            self.assertEqual(health["status"], "healthy")
            disabled = next(r for r in health["robots"] if r["robot_id"] == "disabled")
            self.assertEqual(disabled["state"], "disabled")
        finally:
            await other.close()


class StorageConfigTests(unittest.TestCase):
    def setUp(self):
        self.Runtime, self.Config = require_runtime(self)
        self.tmp = tempfile.TemporaryDirectory(prefix="phase1-config-", dir=ROOT / "var")
        self.addCleanup(self.tmp.cleanup)

    def test_duplicate_robot_ids_rejected(self):
        with self.assertRaises(ValueError):
            self.Runtime([self.Config("same"), self.Config("same")], Path(self.tmp.name) / "db")

    def test_future_schema_rejected_without_reset(self):
        from annie_adapters import RuntimeStore

        path = Path(self.tmp.name) / "future.db"
        with sqlite3.connect(path) as db:
            db.execute("pragma user_version=99")
        with self.assertRaises(ValueError):
            RuntimeStore(path).migrate()
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute("pragma user_version").fetchone()[0], 99)

    def test_config_is_local_fake_only_and_path_cannot_escape(self):
        from annie_adapters import load_robots, resolve_database

        configs = load_robots(ROOT / "config/robots.toml")
        self.assertEqual([c.robot_id for c in configs], ["annie-a", "annie-b"])
        self.assertTrue(all(c.listen_enabled and not c.publish_enabled for c in configs))
        self.assertTrue(all(c.audio_queue_capacity == 25 and c.audio_max_age_ms == 500 for c in configs))
        self.assertTrue(self.Config("phase2-input", listen_enabled=True).listen_enabled)
        with self.assertRaises(ValueError):
            self.Config("bad-output", publish_enabled=True)
        with self.assertRaises(ValueError):
            resolve_database(ROOT, "../outside.db")
        with self.assertRaises(ValueError):
            resolve_database(ROOT, "runtime.db")
        self.assertEqual(resolve_database(ROOT, "var/runtime.db"), (ROOT / "var/runtime.db").resolve())

    def test_robot_config_rejects_wrong_toml_scalar_types(self):
        for kwargs in (
            {"enabled": "false"},
            {"listen_enabled": 0},
            {"publish_enabled": "false"},
            {"queue_capacity": "16"},
            {"queue_capacity": True},
            {"fake_delay_ms": "100"},
            {"fake_delay_ms": False},
            {"audio_queue_capacity": "25"},
            {"audio_queue_capacity": True},
            {"audio_max_age_ms": "500"},
            {"audio_max_age_ms": False},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.Config("bad-types", **kwargs)

    def test_workspace_has_six_members_not_third_party(self):
        root = tomllib.loads((ROOT / "pyproject.toml").read_text())
        members = root["tool"]["uv"]["workspace"]["members"]
        self.assertEqual(len(members), 6)
        self.assertFalse(any("third_party" in m or "sidecars" in m for m in members))

    def test_start_scripts_share_the_same_runtime_entrypoint(self):
        entrypoint = "uv run --locked python -m annie_runtime"
        shell = (ROOT / "start.sh").read_text()
        batch = (ROOT / "start.bat").read_text()
        self.assertIn(entrypoint, shell)
        self.assertIn(entrypoint, batch)
        self.assertIn("--replace-existing", shell)
        self.assertIn("--replace-existing", batch)
        self.assertNotIn("robot_brain", shell + batch)
        self.assertNotIn("../", shell + batch)

    def test_replace_existing_runtime_only_kills_verified_project(self):
        from annie_runtime.__main__ import replace_existing_runtime

        with patch("annie_runtime.__main__.probe_existing_runtime", return_value=None), patch(
            "annie_runtime.__main__.os.kill"
        ) as kill:
            self.assertFalse(replace_existing_runtime("127.0.0.1", 8780, "Annie Harbor"))
            kill.assert_not_called()

        with patch(
            "annie_runtime.__main__.probe_existing_runtime",
            return_value={"project": "Another Service", "pid": 12345},
        ), patch("annie_runtime.__main__.os.kill") as kill:
            self.assertFalse(replace_existing_runtime("127.0.0.1", 8780, "Annie Harbor"))
            kill.assert_not_called()

    def test_replace_existing_runtime_terminates_verified_pid_and_waits(self):
        from annie_runtime.__main__ import replace_existing_runtime

        with patch(
            "annie_runtime.__main__.probe_existing_runtime",
            return_value={"project": "Annie Harbor", "pid": 12345},
        ), patch("annie_runtime.__main__.os.kill") as kill, patch(
            "annie_runtime.__main__.wait_for_port_free", return_value=True
        ):
            self.assertTrue(replace_existing_runtime("127.0.0.1", 8780, "Annie Harbor"))
            kill.assert_called_once_with(12345, signal.SIGTERM)

    def test_invalid_database_fails_before_existing_runtime_is_replaced(self):
        from annie_runtime.__main__ import main

        bad_db = Path(self.tmp.name) / "not-sqlite.db"
        bad_db.write_text("not a sqlite database")
        relative_db = bad_db.relative_to(ROOT)
        argv = [
            "annie_runtime",
            "--root",
            str(ROOT),
            "--database",
            str(relative_db),
            "--replace-existing",
        ]
        with patch.object(sys, "argv", argv), patch(
            "annie_runtime.__main__.load_local_env"
        ), patch("annie_runtime.__main__.replace_existing_runtime") as replace, self.assertRaises(
            SystemExit
        ) as raised:
            main()
        self.assertEqual(raised.exception.code, 2)
        replace.assert_not_called()

    def test_local_env_loads_without_overriding_exported_environment(self):
        from annie_runtime.__main__ import load_local_env

        env_path = Path(self.tmp.name) / ".env"
        env_path.write_text(
            "# local config\nANNIE_HOST=0.0.0.0\nANNIE_PORT='9001'\nexport ANNIE_DATABASE=var/test.db\n"
        )
        with patch.dict(os.environ, {"ANNIE_PORT": "9999"}, clear=False):
            for key in ("ANNIE_HOST", "ANNIE_DATABASE"):
                os.environ.pop(key, None)
            load_local_env(env_path)
            self.assertEqual(os.environ["ANNIE_HOST"], "0.0.0.0")
            self.assertEqual(os.environ["ANNIE_PORT"], "9999")
            self.assertEqual(os.environ["ANNIE_DATABASE"], "var/test.db")

    def test_missing_local_env_is_allowed(self):
        from annie_runtime.__main__ import load_local_env

        missing = Path(self.tmp.name) / "missing.env"
        self.assertFalse(missing.exists())
        load_local_env(missing)

    def test_port_preflight_detects_an_occupied_port(self):
        from annie_runtime.__main__ import port_available

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            self.assertFalse(port_available("127.0.0.1", port))

    def test_port_preflight_uses_bind_and_does_not_hide_other_socket_errors(self):
        from annie_runtime.__main__ import port_available

        probe = MagicMock()
        probe.__enter__.return_value = probe
        probe.bind.side_effect = OSError(errno.EACCES, "permission denied")
        with patch("annie_runtime.__main__.socket.socket", return_value=probe):
            with self.assertRaises(OSError) as raised:
                port_available("127.0.0.1", 8780)
        self.assertEqual(raised.exception.errno, errno.EACCES)
        probe.bind.assert_called_once_with(("127.0.0.1", 8780))

    def test_tool_caches_are_kept_under_var(self):
        root = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertEqual(root["tool"]["pytest"]["ini_options"]["cache_dir"], "var/pytest")
        self.assertEqual(root["tool"]["ruff"]["cache-dir"], "var/ruff")

    def test_current_external_configs_are_parseable_and_match_verified_baseline(self):
        providers = tomllib.loads((ROOT / "config/providers.toml").read_text())
        rooms = tomllib.loads((ROOT / "config/rooms.toml").read_text())
        runtime_defaults = tomllib.loads((ROOT / "config/runtime_defaults.toml").read_text())
        self.assertEqual(providers["llm"]["main"]["model"], "qwen3.8-flash")
        self.assertEqual(
            providers["llm"]["chat_completions_url"],
            "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        )
        self.assertEqual(providers["tts"]["qwen_audio"]["sample_rate"], 24000)
        self.assertEqual(providers["vision"]["minicpm_local"]["base_url"], "http://192.168.1.47:1234/v1")
        self.assertEqual(providers["vision"]["minicpm_local"]["model"], "minicpm-v-4.6")
        self.assertEqual(rooms["urls"]["main"], "ws://192.168.1.47:8090/robot/roomv2/reb_main")
        self.assertEqual(rooms["urls"]["vision"], "ws://192.168.1.47:8090/robot/roomv2/reb_vision")
        self.assertEqual(rooms["urls"]["sam"], "ws://192.168.1.47:8090/robot/roomv2/reb_sam")
        self.assertEqual(runtime_defaults["tts"]["provider"], "qwen_audio")
        for path in (
            ROOT / "config/action_scripts.json",
            ROOT / "config/attention_scoring.json",
            ROOT / "sidecars/tts_viseme/viseme_config.json",
        ):
            self.assertIsInstance(json.loads(path.read_text()), dict, str(path))
        self.assertFalse((ROOT / "config/skills.json").exists())
        provider_text = (ROOT / "config/providers.toml").read_text()
        self.assertNotIn("qwen3-vl-flash", provider_text)
        self.assertNotIn("cosyvoice", provider_text.lower())
        self.assertNotIn("_legacy", provider_text)
        self.assertNotIn("[duplex.legacy]", provider_text)

    def test_source_tree_has_no_meaningless_empty_dirs_or_placeholder_files(self):
        excluded_roots = {".venv", "var", ".git", "node_modules", "__pycache__"}
        placeholder_names = {".gitkeep", ".keep", ".placeholder"}

        placeholders = []
        empty_dirs = []
        for path in ROOT.rglob("*"):
            relative = path.relative_to(ROOT)
            if any(part in excluded_roots for part in relative.parts):
                continue
            if path.is_file() and path.name in placeholder_names:
                placeholders.append(str(relative))
            elif path.is_dir() and not any(path.iterdir()):
                empty_dirs.append(str(relative))

        self.assertEqual(placeholders, [], f"meaningless placeholder files: {placeholders}")
        self.assertEqual(empty_dirs, [], f"meaningless empty directories: {empty_dirs}")

    def test_local_env_may_be_read_but_fake_regression_does_not_require_real_secrets(self):
        example = (ROOT / ".env.example").read_text()
        gitignore = (ROOT / ".gitignore").read_text()
        self.assertIn(".env", gitignore)
        for key in ("DASHSCOPE_API_KEY", "AMAP_WEATHER_API_KEY"):
            self.assertIn(f"{key}=\n", example)
        self.assertIn("MINICPM_API_KEY=local", example)

        env_path = ROOT / ".env"
        env_lines = {}
        if env_path.is_file():
            for raw in env_path.read_text().splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                env_lines[key] = value

        combined_toml = "\n".join(
            (ROOT / f"config/{name}").read_text()
            for name in ("providers.toml", "rooms.toml", "runtime_defaults.toml")
        )
        for key in ("DASHSCOPE_API_KEY", "AMAP_WEATHER_API_KEY"):
            self.assertIn(key, combined_toml)
            if env_lines.get(key):
                self.assertNotIn(env_lines[key], combined_toml)

    def test_core_has_no_sdk_and_no_phase1_code_imports_v1_or_real_providers(self):
        forbidden = {"robot_brain", "langgraph", "langmem", "sqlite_vec", "pipecat", "redis", "openai", "dashscope"}
        for base in (ROOT / "apps", ROOT / "packages"):
            for path in base.rglob("*.py"):
                tree = ast.parse(path.read_text())
                for node in ast.walk(tree):
                    names = (
                        [n.name for n in node.names]
                        if isinstance(node, ast.Import)
                        else ([node.module or ""] if isinstance(node, ast.ImportFrom) else [])
                    )
                    self.assertFalse(forbidden.intersection(n.split(".")[0] for n in names), str(path))
                    if "annie-core" in path.parts:
                        self.assertFalse(any(n.startswith("annie_") for n in names), str(path))


class HTTPTests(unittest.TestCase):
    def setUp(self):
        Runtime, Config = require_runtime(self)
        from annie_web import create_app
        from fastapi.testclient import TestClient

        self.tmp = tempfile.TemporaryDirectory(prefix="phase1-http-", dir=ROOT / "var")
        self.addCleanup(self.tmp.cleanup)
        runtime = Runtime(
            [Config("annie-a", fake_delay_ms=0), Config("annie-b", fake_delay_ms=0)],
            Path(self.tmp.name) / "api.db",
        )
        self.client = self.enterContext(
            TestClient(create_app(runtime, ROOT / "web/src", ROOT / "tests/html"))
        )

    def test_health_same_pid_two_heads(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["pid"], os.getpid())
        self.assertEqual(len(response.json()["robots"]), 2)
        self.assertEqual(response.json()["capabilities"]["memory"], "not_started")

    def test_local_controls_and_degraded_health(self):
        result = self.client.post("/api/robots/annie-a/turns", json={"text": "你好"})
        self.assertEqual(result.json()["reply"], "[FAKE annie-a] 你好")
        self.assertEqual(self.client.post("/api/robots/annie-a/fail").json()["status"], "failed")
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/health").json()["status"], "degraded")
        self.assertEqual(
            self.client.post("/api/robots/annie-b/turns", json={"text": "B"}).json()["status"], "completed"
        )
        self.client.post("/api/robots/annie-a/start")
        self.assertEqual(self.client.post("/api/robots/annie-a/cancel").status_code, 200)
        self.client.post("/api/robots/annie-a/stop")
        self.assertEqual(self.client.post("/api/robots/annie-a/turns", json={"text": "x"}).status_code, 409)
        self.client.post("/api/robots/annie-b/stop")
        self.assertEqual(self.client.get("/health").status_code, 503)

    def test_invalid_input_unknown_head_and_dashboard(self):
        self.assertEqual(self.client.post("/api/robots/missing/stop").status_code, 404)
        self.assertEqual(self.client.post("/api/robots/annie-a/turns", json={"text": "  "}).status_code, 422)
        self.assertEqual(
            self.client.post("/api/robots/annie-a/turns", json={"text": "ok", "token": "bad"}).status_code,
            422,
        )
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Annie Harbor", page.text)
        phase_page = self.client.get("/phase-tests/phase1.html")
        self.assertEqual(phase_page.status_code, 200)
        self.assertIn("Phase 1", phase_page.text)
        self.assertIn("操作日志", phase_page.text)
        self.assertIn("并发向全部机器人头发送", phase_page.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
