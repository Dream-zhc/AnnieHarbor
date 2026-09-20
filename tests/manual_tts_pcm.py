"""Provider-only PCM probe. No Room publisher, microphone or speaker.

Run explicitly: uv run --locked python tests/manual_tts_pcm.py [--save-wav]
This makes one real billable TTS request with the configured voice.
"""

import argparse
import asyncio
import math
import os
import sys
import tomllib
import wave
from array import array
from pathlib import Path

from annie_adapters.tts import DEFAULT_URL, QwenTtsSocket, command, task_id
from annie_runtime.__main__ import load_local_env


async def main():
    parser = argparse.ArgumentParser(description="Isolate configured TTS from Room/audio capture")
    parser.add_argument("--save-wav", action="store_true", help="save raw provider PCM for ear comparison")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    load_local_env(root / ".env")
    key = os.getenv("DASHSCOPE_API_KEY")
    if not key:
        raise SystemExit("DASHSCOPE_API_KEY is missing")
    with (root / "config/providers.toml").open("rb") as file:
        voice = tomllib.load(file)["tts"]["qwen_audio"]["voice"]
    socket = QwenTtsSocket(os.getenv("ANNIE_TTS_URL") or DEFAULT_URL, key)
    data = bytearray()
    task = task_id()
    try:
        async with asyncio.timeout(35):
            await socket.open()
            await socket.send(command("run-task", task, voice=voice))
            while True:
                event = await socket.recv()
                if isinstance(event, bytes):
                    data.extend(event)
                    continue
                header = event.get("header") or {}
                if header.get("task_id") not in (None, task):
                    raise ValueError("TTS task mismatch")
                if header.get("event") == "task-started":
                    await socket.send(command("continue-task", task, text="你好呀，很高兴见到你。"))
                    await socket.send(command("finish-task", task))
                elif header.get("event") == "task-finished":
                    break
                elif header.get("event") in {"task-failed", "error"}:
                    raise RuntimeError(f"provider returned {header.get('event')}")
    finally:
        await socket.close()
    if len(data) % 2:
        raise ValueError("odd PCM length")
    samples = array("h")
    samples.frombytes(data)
    if sys.byteorder != "little":
        samples.byteswap()
    clipped = sum(abs(item) >= 32760 for item in samples)
    rms = math.sqrt(sum(item * item for item in samples) / len(samples)) if samples else 0
    print(f"provider_only bytes={len(data)} samples={len(samples)} duration_s={len(samples)/24000:.3f}")
    print(f"peak={max((abs(item) for item in samples), default=0)} rms={rms:.1f} near_full_scale={clipped}")
    if args.save_wav:
        out = root / "var/tmp/phase4-tts-noise/provider-only.wav"
        out.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out), "wb") as file:
            file.setnchannels(1)
            file.setsampwidth(2)
            file.setframerate(24000)
            file.writeframes(data)
        print(f"raw_provider_wav={out}")


if __name__ == "__main__":
    asyncio.run(main())
