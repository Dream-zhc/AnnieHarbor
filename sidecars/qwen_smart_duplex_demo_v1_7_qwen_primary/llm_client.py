from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from config import Settings


class ExternalLLM:
    """OpenAI-compatible Chat Completions streaming client without SDK lock-in."""

    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self.client = httpx.AsyncClient(
            base_url=cfg.llm_base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {cfg.api_key}"},
            timeout=httpx.Timeout(connect=15.0, read=120.0, write=30.0, pool=15.0),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def stream(self, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        payload = {
            "model": self.cfg.llm_model,
            "messages": messages,
            "stream": True,
            "temperature": 0.7,
            "max_tokens": 500,
            "enable_thinking": False,
        }
        async with self.client.stream("POST", "chat/completions", json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                    choices = event.get("choices") or []
                    if not choices:
                        continue
                    delta = (choices[0].get("delta") or {}).get("content")
                    if delta:
                        yield str(delta)
                except json.JSONDecodeError:
                    continue
