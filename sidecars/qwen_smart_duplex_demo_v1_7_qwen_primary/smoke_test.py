from __future__ import annotations

import importlib
import sys


def main() -> int:
    modules = ["fastapi", "uvicorn", "websockets", "httpx", "dotenv"]
    for module in modules:
        importlib.import_module(module)
    from config import settings
    from utils import PhraseChunker

    settings.validate_for_cloud()
    chunker = PhraseChunker(8, 14)
    assert chunker.feed("你好，我是小安妮。") == ["你好，我是小安妮。"]
    print("[OK] 依赖、配置和核心分句器检查通过")
    print(f"[OK] Audio={settings.audio_model} LLM={settings.llm_model} TTS={settings.tts_model}")
    print(f"[OK] 打开 http://{settings.host}:{settings.port}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
