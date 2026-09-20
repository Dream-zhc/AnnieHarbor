"""Opt-in launch of the existing ForcedAligner listener, one isolated process per Room."""

import asyncio
import os
import subprocess
from pathlib import Path


class VisemeSidecars:
    """The legacy listener is room-scoped; it must never be shared across robots."""

    def __init__(self, root: Path, room_config, robots, *, python: str | None = None):
        self.root = root
        self.rooms = room_config
        available = tuple(robots)
        chosen = os.getenv("ANNIE_VISEME_ROBOTS", "").strip()
        if chosen:
            ids = tuple(part.strip() for part in chosen.split(",") if part.strip())
            if len(ids) != len(set(ids)) or not ids:
                raise ValueError("ANNIE_VISEME_ROBOTS must list unique robot IDs")
            unknown = set(ids) - {robot.config.robot_id for robot in available}
            if unknown:
                raise ValueError("ANNIE_VISEME_ROBOTS contains unknown robot IDs")
            self.robots = tuple(robot for robot in available if robot.config.robot_id in ids)
        else:
            # Development default: one acoustic model, even with multiple Rooms.
            # Explicit ANNIE_VISEME_ROBOTS may opt into more when measured.
            self.robots = available[:1]
        self.folder = root / "sidecars" / "tts_viseme"
        # Do NOT resolve the final executable symlink: uv venv/bin/python points
        # at the system interpreter, whose realpath bypasses the venv packages.
        self.python = (Path(python) if python else self.folder / ".venv311" / (
            "Scripts/python.exe" if os.name == "nt" else "bin/python"
        )).expanduser().absolute()
        self.processes = {}
        self.logs = []

    def preflight(self):
        if not self.python.is_file():
            raise ValueError(
                "ANNIE_VISEME_ENABLED=1 requires the ForcedAligner sidecar Python environment: "
                f"{self.python}; install sidecars/tts_viseme/requirements-v3.txt first"
            )
        if not (self.folder / "qwen_stream_room_g2pw.py").is_file():
            raise ValueError("ForcedAligner listener source is missing")
        try:
            probe = subprocess.run(
                [str(self.python), "-c",
                 "import importlib.util,platform;"
                 "names=['numpy','dotenv','websocket','pypinyin']+"
                 "(['g2pw'] if __import__('os').getenv('TTS_VISEME_G2PW_ENABLED')=='1' else [])+"
                 "["
                 "'mlx_qwen3_asr' if platform.system()=='Darwin' "
                 "and platform.machine().lower() in ('arm64','aarch64') else 'qwen_asr'];"
                 "print(','.join(name for name in names if importlib.util.find_spec(name) is None))"],
                capture_output=True, text=True, timeout=5, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("ForcedAligner sidecar Python preflight failed") from exc
        if probe.returncode != 0 or probe.stdout.strip():
            raise ValueError("ForcedAligner sidecar dependencies missing or invalid: " +
                             (probe.stdout.strip() or "Python probe failed"))

    async def start(self):
        self.preflight()
        log_dir = self.root / "var" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        try:
            for index, robot in enumerate(self.robots):
                room = robot.config.room_id
                log = (log_dir / f"viseme-{robot.config.robot_id}.log").open("ab")
                self.logs.append(log)
                # V1 listener occupies 37659 on the development host; keep V2
                # isolated instead of stealing or terminating the legacy job.
                env = {**os.environ, "TTS_VISEME_SINGLETON_PORT": str(37759 + index),
                       "TTS_VISEME_OBSERVE_URL": ""}
                env.setdefault("HF_HOME", str(self.root / "var" / "models" / "huggingface"))
                proc = subprocess.Popen(
                    [str(self.python), "qwen_stream_room_g2pw.py",
                     "--tts-url", self.rooms.url_for(room, "tts"),
                     "--sam-url", self.rooms.url_for(room, "sam"),
                     "--env", ".env"],
                    cwd=self.folder, env=env, stdout=log, stderr=subprocess.STDOUT,
                )
                self.processes[robot.config.robot_id] = proc
        except BaseException:
            await self.close()
            raise

    def snapshot(self):
        return {
            robot_id: {"state": "running" if proc.poll() is None else "exited",
                       "exit_code": proc.poll()}
            for robot_id, proc in self.processes.items()
        }

    async def close(self):
        for proc in self.processes.values():
            if proc.poll() is None:
                proc.terminate()
        for proc in self.processes.values():
            try:
                await asyncio.wait_for(asyncio.to_thread(proc.wait), 3)
            except asyncio.TimeoutError:
                proc.kill()
                await asyncio.to_thread(proc.wait)
        self.processes.clear()
        for log in self.logs:
            log.close()
        self.logs.clear()
