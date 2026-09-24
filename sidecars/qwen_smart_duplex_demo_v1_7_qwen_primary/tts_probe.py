from __future__ import annotations

import asyncio

from config import settings
from tts_client import QwenAudioTTS


async def main() -> None:
    settings.validate_for_cloud()

    async def on_audio(_: bytes, __: int) -> None:
        pass

    async def on_event(event: dict) -> None:
        kind = event.get("type")
        if kind in {"tts.connected", "tts.probe", "tts.ready", "tts.task-failed"}:
            print(event)

    tts = QwenAudioTTS(settings, on_audio=on_audio, on_event=on_event)
    try:
        await tts.connect()
        print("[OK] TTS 端点、模型、音色与 run-task 参数预检通过")
    finally:
        await tts.close()


if __name__ == "__main__":
    asyncio.run(main())
