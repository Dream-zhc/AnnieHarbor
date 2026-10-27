"""Unified CLI bootstrap: uv run --locked python -m annie_runtime."""

import argparse
import asyncio
import errno
import json
import logging
import os
import re
import signal
import socket
import sys
import time
import tomllib
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener

import uvicorn
from annie_adapters import load_robots, load_room_server, resolve_database
from annie_brain.persona import load_personas
from annie_web import create_app

from annie_runtime import HarborRuntime

# Physical scripts runnable without .env. Every motion users can name in
# dialogue (decision_policy._MOTION_NAMES) is here: 摇头/往上看 were missing.
DEFAULT_ACTION_ALLOWLIST = (
    "agree_micro_nod", "shake_head", "look_left", "look_right", "look_up",
    "fast_double_blink", "surprise", "smirk", "proud", "skeptical", "shy",
)
# 2026-10-26: the listening, speaking and reaction groups the runtime picks while
# she is awake (decision_policy.LISTEN_MOTIONS / SPEECH_MOTIONS / INNER_MOTIONS).
# Added to the default allowlist only when [motions] is enabled.
INTERACTION_MOTION_SCRIPTS = (
    "listen_tilt", "listen_turn", "micro_tilt", "think_tilt", "confused_tilt",
    "micro_pitch", "eye_glance_up", "eye_glance_down", "sigh_droop", "shy_retract",
    "wink_left",
)


def motion_options(motions: dict, environ) -> dict | None:
    """HarborRuntime motion_options from runtime_defaults [motions] and .env.

    ANNIE_INTERACTION_MOTIONS_ENABLED=0/1 overrides the file; each part can be
    turned off on its own in the file. Real motions still need ANNIE_ACTIONS_ENABLED=1.
    """
    enabled = environ.get("ANNIE_INTERACTION_MOTIONS_ENABLED",
                          "1" if motions.get("enabled", False) else "0") == "1"
    if not enabled:
        return None
    per_reply = int(motions.get("per_reply", 3))
    if not 0 <= per_reply <= 6:
        raise ValueError("[motions].per_reply must be between 0 and 6")
    return {"listening": bool(motions.get("listening", True)),
            "speaking": bool(motions.get("speaking", True)),
            "reactions": bool(motions.get("reactions", True)),
            "per_reply": per_reply}


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


def require_dashboard_build(root: Path) -> Path:
    """Fail before replacing an old Runtime if the React build is missing."""
    dashboard = root / "web/dist"
    index = dashboard / "index.html"
    if not index.is_file():
        raise ValueError("React build missing: run npm --prefix web ci && npm --prefix web run build")
    html = index.read_text(encoding="utf-8")
    assets = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', html)
    if not assets or any(not (dashboard / asset.lstrip("/")).is_file() for asset in assets):
        raise ValueError("React build incomplete: run npm --prefix web run build")
    return dashboard


def d3_options(decision: dict, environ) -> dict:
    """HarborRuntime d3 kwargs from providers.toml [decision] and .env.

    prefer = "d3" uses d3 while it answers and DashScope otherwise; "dashscope"
    never contacts d3. Without ANNIE_D3_URL nothing changes either way.
    """
    prefer = decision.get("prefer", "dashscope")
    if prefer not in {"d3", "dashscope"}:
        raise ValueError('[decision].prefer must be "d3" or "dashscope"')
    d3 = decision.get("d3", {})
    return {
        "d3_url": (environ.get("ANNIE_D3_URL") or None) if prefer == "d3" else None,
        "d3_key": environ.get("ANNIE_D3_TOKEN") or None,
        "d3_model": d3.get("model", "d3-NVFP4"),
        "d3_timeout_ms": int(d3.get("timeout_ms", 1500)),
        "d3_expression_threshold": float(d3.get("expression_threshold", .7)),
    }


def inner_life_options(inner_life: dict, llm: dict, environ) -> dict:
    """HarborRuntime kwargs for her mood and day: runtime_defaults [inner_life] and .env.

    ANNIE_INNER_LIFE_ENABLED=0/1 overrides the file. Without an API key the day
    still records events; finished conversations become “聊了N轮” lines only.
    """
    enabled = environ.get("ANNIE_INNER_LIFE_ENABLED",
                          "1" if inner_life.get("enabled", False) else "0") == "1"
    summarizer = None
    if enabled and environ.get("DASHSCOPE_API_KEY"):
        from annie_adapters.diary_summary import DiarySummarizer

        diary = llm.get("diary", {})
        summarizer = DiarySummarizer(
            environ["DASHSCOPE_API_KEY"], model=diary.get("model", "qwen3.8-flash"),
            base_url=llm["base_url"], timeout_ms=int(diary.get("timeout_ms", 8000)))
    return {
        "inner_life_enabled": enabled,
        "inner_life_summarizer": summarizer,
        "inner_life_close_seconds": float(inner_life.get("conversation_close_seconds", 120)),
        "inner_life_keep_days": int(inner_life.get("keep_days", 7)),
    }


def main():
    root = Path(__file__).resolve().parents[4]
    parser = argparse.ArgumentParser(description="Annie Harbor — Phase 4 workbench and Runtime")
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
    with (root / "config/runtime_defaults.toml").open("rb") as file:
        runtime_defaults = tomllib.load(file)
    with (root / "config/providers.toml").open("rb") as file:
        provider_defaults = tomllib.load(file)
    wake_detector = None
    if os.environ.get("ANNIE_WAKEWORD_ENABLED") == "1":
        if not os.environ.get("DASHSCOPE_API_KEY"):
            raise ValueError("wakeword requires an active Realtime provider")
        from annie_adapters.kws import SherpaWakeModel

        def asset_path(env_name, default):
            asset = Path(os.environ.get(env_name, default)).expanduser()
            return (asset if asset.is_absolute() else root / asset).resolve()

        wake_detector = SherpaWakeModel(
            asset_path("ANNIE_WAKEWORD_MODEL",
                       "var/models/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"),
            keywords=asset_path("ANNIE_WAKEWORD_KEYWORDS",
                                "config/小安妮唤醒词.tokens.txt"),
            threshold=float(os.environ.get("ANNIE_WAKEWORD_THRESHOLD", "0.35")),
        )
    idle_enabled = os.environ.get("ANNIE_IDLE_ENABLED") == "1"
    continuation_enabled = os.environ.get("ANNIE_CONTINUATION_ENABLED") == "1"
    if continuation_enabled and not idle_enabled:
        raise ValueError("short continuation requires ANNIE_IDLE_ENABLED=1")
    if continuation_enabled and os.environ.get("ANNIE_IDLE_TRUSTED_ROOM") != "1":
        raise ValueError("short continuation requires explicit private-room consent")
    if idle_enabled and (not os.environ.get("DASHSCOPE_API_KEY")
                         or os.environ.get("ANNIE_TTS_ENABLED") != "1"
                         or os.environ.get("ANNIE_IDLE_TRUSTED_ROOM") != "1"):
        raise ValueError("8C requires API key, TTS, and explicit trusted-room consent")
    idle_model = None
    if idle_enabled:
        from annie_adapters.idle import QwenIdleModel

        inquiry = provider_defaults["llm"]["inquiry"]
        idle_model = QwenIdleModel(
            os.environ["DASHSCOPE_API_KEY"], inquiry["base_url"],
            model=os.environ.get("ANNIE_IDLE_MODEL", inquiry["model"]),
            timeout=float(inquiry["timeout_sec"]),
            enable_thinking=inquiry.get("enable_thinking", False),
            reasoning_effort=inquiry.get("reasoning_effort", "low"),
        )
    logging.basicConfig(
        level=resolve_log_level(os.environ.get("ANNIE_LOG_LEVEL", config["app"].get("log_level", "INFO"))),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    speaker_embedder = None
    if os.environ.get("ANNIE_SPEAKER_ENABLED", "1") != "0":
        from annie_adapters.speakers import MODEL_FILE, SpeakerEmbedder

        speaker_path = Path(os.environ.get(
            "ANNIE_SPEAKER_MODEL", f"var/models/{MODEL_FILE}",
        )).expanduser()
        if not speaker_path.is_absolute():
            speaker_path = (root / speaker_path).resolve()
        if speaker_path.is_file():
            speaker_embedder = SpeakerEmbedder(speaker_path)
        else:
            logging.getLogger("annie.speakers").warning(
                "声纹模型未找到，注册与识别暂不可用: %s", speaker_path)
    speaker_threshold = float(os.environ.get("ANNIE_SPEAKER_THRESHOLD", "0.5"))
    speaker_margin = float(os.environ.get("ANNIE_SPEAKER_MARGIN", "0.08"))
    database = resolve_database(
        root,
        args.database or os.environ.get("ANNIE_DATABASE") or config["storage"]["database"],
    )
    motions = motion_options(runtime_defaults.get("motions", {}), os.environ)
    runtime = HarborRuntime(
        load_robots(root / "config/robots.toml"),
        database,
        load_room_server(root / "config/rooms.toml"),
        conversation_history_turns=runtime_defaults["conversation"]["history_turns"],
        realtime_history_turns=int(os.environ.get(
            "ANNIE_REALTIME_HISTORY_TURNS",
            runtime_defaults["conversation"].get("realtime_history_turns", 8),
        )),
        realtime_restore_char_budget=int(os.environ.get(
            "ANNIE_REALTIME_RESTORE_CHAR_BUDGET",
            runtime_defaults["conversation"].get("realtime_restore_char_budget", 3000),
        )),
        persona_profiles=load_personas(root / "config/personas.toml"),
        realtime_key=os.environ.get("DASHSCOPE_API_KEY") or None,
        wake_detector=wake_detector,
        wake_wait_ms=int(runtime_defaults["duplex"]["wake_wait_ms"]),
        wake_followup_ms=int(runtime_defaults["duplex"]["followup_ms"]),
        side_talk_listen_ms=int(runtime_defaults["duplex"].get("side_talk_listen_ms", 120000)),
        greet_known_cooldown_minutes=float(
            runtime_defaults["vision"].get("greet_known_cooldown_minutes", 30)),
        face_api_urls=dict(runtime_defaults["vision"].get("face", {}).get("api_urls", {})),
        face_samples=int(runtime_defaults["vision"].get("face", {}).get("samples", 18)),
        cloud_aec_robots=(tuple(item.strip() for item in os.environ.get(
            "ANNIE_CLOUD_AEC_ROBOTS", "annie-a").split(",") if item.strip())
            if os.environ.get("ANNIE_CLOUD_AEC_ENABLED") == "1" else ()),
        cloud_aec_sync_offset_ms=int(os.environ.get("ANNIE_CLOUD_AEC_SYNC_OFFSET_MS", "80")),
        cloud_aec_delay_ms=int(os.environ.get("ANNIE_CLOUD_AEC_DELAY_MS", "0")),
        realtime_url=os.environ.get("ANNIE_REALTIME_URL") or None,
        realtime_model=(os.environ.get("ANNIE_REALTIME_MODEL")
                        or provider_defaults.get("realtime", {}).get("model") or None),
        acoustic_enabled=os.environ.get("ANNIE_ACOUSTIC_SIDECAR_ENABLED", "0") == "1",
        acoustic_url=os.environ.get("ANNIE_ACOUSTIC_SIDECAR_URL") or None,
        tts_expression_enabled=os.environ.get("ANNIE_TTS_EXPRESSION_ENABLED", "0") == "1",
        tts_mixed_mode_enabled=os.environ.get("ANNIE_TTS_MIXED_MODE_ENABLED", "0") == "1",
        tts_voice=(
            provider_defaults["tts"]["qwen_audio"]["voice"]
            if os.environ.get("ANNIE_TTS_ENABLED") == "1" else None
        ),
        tts_url=os.environ.get("ANNIE_TTS_URL") or None,
        tts_first_chars=provider_defaults["tts"]["first_min_chars"],
        tts_min_chars=provider_defaults["tts"]["sentence_min_chars"],
        tts_max_chars=provider_defaults["tts"]["sentence_max_chars"],
        memory_key=(
            os.environ.get("DASHSCOPE_API_KEY") or None
            if os.environ.get("ANNIE_MEMORY_ENABLED", "1") != "0" else None
        ),
        memory_base_url=os.environ.get(
            "ANNIE_MEMORY_BASE_URL", provider_defaults["llm"]["base_url"],
        ),
        memory_extract_model=provider_defaults["memory"]["extract_model"],
        memory_embedding_model=provider_defaults["memory"]["embedding_model"],
        memory_embedding_dimensions=provider_defaults["memory"]["embedding_dimensions"],
        action_catalog=root / "config/action_scripts.json",
        idle_model=idle_model,
        continuation_enabled=continuation_enabled,
        continuation_seconds=float(os.environ.get("ANNIE_CONTINUATION_SECONDS", "15")),
        actions_enabled=os.environ.get("ANNIE_ACTIONS_ENABLED") == "1",
        action_allowlist=tuple(
            item.strip() for item in os.environ.get(
                "ANNIE_ACTION_ALLOWLIST", ",".join(
                    DEFAULT_ACTION_ALLOWLIST + (INTERACTION_MOTION_SCRIPTS if motions else ())),
            ).split(",") if item.strip()
        ),
        weather_key=os.environ.get("AMAP_WEATHER_API_KEY") or None,
        weather_timeout_ms=int(provider_defaults["weather"]["amap"]["timeout_ms"]),
        weather_cache_sec=int(provider_defaults["weather"]["amap"]["live_cache_sec"]),
        weather_default_city=provider_defaults["weather"]["amap"].get("default_city"),
        speaker_embedder=speaker_embedder,
        speaker_threshold=speaker_threshold,
        speaker_margin=speaker_margin,
        decision_url=os.environ.get("ANNIE_DECISION_URL") or None,
        addressee_model=provider_defaults["llm"].get("addressee", {}).get("model"),
        addressee_base_url=provider_defaults["llm"]["base_url"],
        addressee_timeout_ms=int(provider_defaults["llm"].get("addressee", {}).get("timeout_ms", 1200)),
        decision_timeout_ms=int(provider_defaults.get("decision", {}).get("timeout_ms", 1500)),
        **d3_options(provider_defaults.get("decision", {}), os.environ),
        addressee_enabled=os.environ.get("ANNIE_ADDRESSEE_ENABLED", "1") == "1",
        auto_expression_enabled=os.environ.get("ANNIE_AUTO_EXPRESSION_ENABLED", "1") == "1",
        expression_threshold=float(provider_defaults.get("decision", {}).get("expression_threshold", .55)),
        ambient_enabled=os.environ.get("ANNIE_AMBIENT_LISTEN_ENABLED", "0") == "1",
        motion_options=motions,
        **inner_life_options(runtime_defaults.get("inner_life", {}),
                             provider_defaults["llm"], os.environ),
    )
    try:
        runtime.preflight()
    except ValueError as exc:
        print(f"[ERROR] preflight failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    if args.check:

        async def check():
            await runtime.start()
            try:
                if runtime.room_server is not None:
                    await runtime.wait_room_gateway(runtime.room_server.connect_timeout_sec + 0.5)
                await runtime.recorder.flush()
                health = runtime.health()
                print(json.dumps(health, ensure_ascii=False, sort_keys=True))
                return 0 if health["ready"] else 1
            finally:
                await runtime.close()

        raise SystemExit(asyncio.run(check()))
    try:
        dashboard = require_dashboard_build(root)
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
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
        create_app(runtime, dashboard),
        host=host,
        port=port,
    )


if __name__ == "__main__":
    main()
