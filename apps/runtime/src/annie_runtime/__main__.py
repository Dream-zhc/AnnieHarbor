"""Unified CLI bootstrap: uv run --locked python -m annie_runtime."""

import argparse
import asyncio
import errno
import json
import logging
import os
import signal
import socket
import sys
import time
import tomllib
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener

import uvicorn
from annie_adapters import load_robots, resolve_database
from annie_web import create_app

from annie_runtime import HarborRuntime


def load_local_env(path: Path) -> None:
    """Load a tiny .env subset without adding a dependency.

    Existing process environment always wins. Supported syntax is intentionally
    small: KEY=VALUE, optional `export ` prefix, blank lines and # comments.
    """
    if not path.is_file():
        return
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key or not (key[0].isalpha() or key[0] == "_") or not all(
            char.isalnum() or char == "_" for char in key
        ):
            raise ValueError(f"invalid .env line {line_number}: expected KEY=VALUE")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def port_available(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                return False
            raise
        return True


def probe_existing_runtime(host: str, port: int) -> dict | None:
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    if ":" in host:
        return None
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(f"http://{host}:{port}/health", timeout=0.4) as response:
            return json.loads(response.read())
    except HTTPError as exc:
        try:
            return json.loads(exc.read())
        except (ValueError, json.JSONDecodeError):
            return None
    except (OSError, URLError, ValueError, json.JSONDecodeError):
        return None


def wait_for_port_free(host: str, port: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if port_available(host, port):
            return True
        time.sleep(0.05)
    return port_available(host, port)


def replace_existing_runtime(host: str, port: int, project_name: str) -> bool:
    """Stop an existing Annie Harbor on the requested port.

    Safety rule: never kill a process only because it owns the port. The
    existing HTTP endpoint must identify itself as this project and provide a
    valid PID first.
    """
    existing = probe_existing_runtime(host, port)
    if not existing or existing.get("project") != project_name:
        return False
    pid = existing.get("pid")
    if not isinstance(pid, int) or pid <= 1 or pid == os.getpid():
        raise RuntimeError(f"invalid existing runtime pid: {pid!r}")

    print(f"[INFO] stopping existing Annie Harbor pid={pid} on {host}:{port} ...")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError as exc:
        raise RuntimeError(f"cannot stop existing Annie Harbor pid={pid}: permission denied") from exc

    if wait_for_port_free(host, port, timeout=3.0):
        print(f"[INFO] old Annie Harbor pid={pid} stopped; starting replacement.")
        return True

    if hasattr(signal, "SIGKILL"):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if wait_for_port_free(host, port, timeout=1.5):
            print(f"[WARN] old Annie Harbor pid={pid} required forced termination; starting replacement.")
            return True
    raise RuntimeError(f"existing Annie Harbor pid={pid} did not release {host}:{port}")


def resolve_log_level(value: str) -> int:
    level = getattr(logging, value.upper(), None)
    if not isinstance(level, int):
        raise ValueError(f"invalid log level: {value}")
    return level


def main():
    root = Path(__file__).resolve().parents[4]
    parser = argparse.ArgumentParser(description="Annie Harbor Phase 1 — two local Fake robot heads")
    parser.add_argument("--root", type=Path, default=root)
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--database")
    parser.add_argument("--check", action="store_true", help="start Fake runtime, print health, close")
    parser.add_argument(
        "--replace-existing",
        action="store_true",
        help="replace an existing Annie Harbor bound to the requested address",
    )
    args = parser.parse_args()
    root = args.root.resolve()
    load_local_env(root / ".env")
    with (root / "config/app.toml").open("rb") as file:
        config = tomllib.load(file)
    logging.basicConfig(
        level=resolve_log_level(os.environ.get("ANNIE_LOG_LEVEL", config["app"].get("log_level", "INFO"))),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    database = resolve_database(
        root,
        args.database or os.environ.get("ANNIE_DATABASE") or config["storage"]["database"],
    )
    runtime = HarborRuntime(load_robots(root / "config/robots.toml"), database)
    try:
        runtime.preflight()
    except ValueError as exc:
        print(f"[ERROR] database preflight failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    if args.check:

        async def check():
            await runtime.start()
            try:
                await runtime.recorder.flush()
                health = runtime.health()
                print(json.dumps(health, ensure_ascii=False, sort_keys=True))
                return 0 if health["ready"] else 1
            finally:
                await runtime.close()

        raise SystemExit(asyncio.run(check()))
    host = args.host or os.environ.get("ANNIE_HOST", config["app"]["host"])
    port = args.port if args.port is not None else int(os.environ.get("ANNIE_PORT", config["app"]["port"]))
    if not 1 <= port <= 65535:
        raise SystemExit(f"[ERROR] invalid port: {port}")
    if not port_available(host, port):
        existing = probe_existing_runtime(host, port)
        display_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
        if existing and existing.get("project") == config["app"]["name"]:
            if args.replace_existing:
                try:
                    replace_existing_runtime(host, port, config["app"]["name"])
                except RuntimeError as exc:
                    print(f"[ERROR] {exc}", file=sys.stderr)
                    raise SystemExit(2) from exc
            else:
                print(
                    f"[INFO] Annie Harbor already running at http://{display_host}:{port} "
                    f"(pid={existing.get('pid', 'unknown')}); no second process started."
                )
                raise SystemExit(0)
        else:
            print(
                f"[ERROR] {host}:{port} is already in use by another process. "
                f"Refusing to kill an unidentified service; use --port <free-port> or set ANNIE_PORT in .env.",
                file=sys.stderr,
            )
            raise SystemExit(2)
    uvicorn.run(
        create_app(runtime, root / "web/src", root / "tests/html"),
        host=host,
        port=port,
    )


if __name__ == "__main__":
    main()
