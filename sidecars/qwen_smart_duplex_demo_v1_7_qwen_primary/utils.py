from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any


def now_ms() -> float:
    return time.perf_counter() * 1000.0


def compact_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


_FILLER_ONLY = re.compile(r"^[\s嗯啊呃哦唔诶额哎呀哈呵噢,.，。!?！？~～…]+$")
_CJK_OR_WORD = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]")


def meaningful_text(text: str, min_chars: int = 2) -> bool:
    cleaned = text.strip()
    if not cleaned or _FILLER_ONLY.fullmatch(cleaned):
        return False
    return len(_CJK_OR_WORD.findall(cleaned)) >= min_chars


class PhraseChunker:
    """把 LLM token 流切成适合低延迟 TTS 的自然短语。

    第一小句允许在较早的逗号处提交，降低首音频等待；后续块适当更长，
    给 TTS 足够上下文维持连贯韵律，避免把中文按固定字数切得过碎。
    """

    HARD_PUNCT = set("。！？!?；;\n")
    SOFT_PUNCT = set("，,、：:")

    def __init__(self, first_chars: int = 10, next_chars: int = 28) -> None:
        self.first_chars = max(6, first_chars)
        self.next_chars = max(12, next_chars)
        self.buffer = ""
        self.sent_any = False

    def feed(self, delta: str) -> list[str]:
        if not delta:
            return []
        self.buffer += delta
        out: list[str] = []
        while True:
            cut = self._find_cut()
            if cut <= 0:
                break
            chunk = self.buffer[:cut].strip()
            self.buffer = self.buffer[cut:].lstrip()
            if chunk:
                out.append(chunk)
                self.sent_any = True
        return out

    def flush(self) -> list[str]:
        chunk = self.buffer.strip()
        self.buffer = ""
        if not chunk:
            return []
        self.sent_any = True
        return [chunk]

    def _find_cut(self) -> int:
        if not self.buffer:
            return 0

        target = self.next_chars if self.sent_any else self.first_chars
        min_natural = 10 if self.sent_any else 6

        # 完整句号优先，但避免把单个标点或极短碎片单独送进 TTS。
        for i, ch in enumerate(self.buffer):
            if ch in self.HARD_PUNCT and i + 1 >= 4:
                return i + 1

        # 第一自然短语无需死等固定阈值；逗号后达到最小语义长度即可提交。
        for i, ch in enumerate(self.buffer):
            if ch in self.SOFT_PUNCT and i + 1 >= min_natural:
                return i + 1

        if len(self.buffer) < target:
            return 0

        # 达到目标后，优先寻找目标附近较晚的自然边界。
        hi = min(len(self.buffer), target + 10)
        lo = max(min_natural, target - 10)
        for i in range(hi - 1, lo - 2, -1):
            if self.buffer[i] in self.SOFT_PUNCT:
                return i + 1

        # 没有标点时，中文按目标长度切；英文尽量等空格。
        window = self.buffer[: target + 8]
        last_space = window.rfind(" ")
        if last_space >= max(4, target - 6):
            return last_space + 1
        return target


async def cancel_and_wait(task: asyncio.Task[Any] | None, timeout: float = 0.8) -> None:
    if task is None or task.done():
        return
    task.cancel()
    try:
        await asyncio.wait_for(task, timeout=timeout)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass
