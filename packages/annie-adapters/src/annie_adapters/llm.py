"""OpenAI-compatible streaming Chat client used by the duplex cascade.

This adapter is intentionally small: the runtime owns tool execution and turn
state; this client only streams text/tool-call deltas from DashScope compatible
chat-completions endpoints.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

PREVIEW_ACTION_TOOL = {
    "type": "function",
    "function": {
        "name": "preview_action",
        "description": "Preview an action in the developer console without moving the real robot.",
        "parameters": {
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "Action to preview"},
            },
            "required": ["description"],
        },
    },
}


@dataclass(slots=True)
class ChatDelta:
    type: str
    text: str = ""
    tool_call: dict[str, Any] | None = None


class ExternalChatLLM:
    """A persistent streaming LLM connection pool.

    The default base URL matches DashScope OpenAI-compatible mode.  The model is
    deliberately configuration-driven because Qwen model availability changes;
    the runtime will fall back to Realtime-generated text only if this adapter is
    not configured.
    """

    def __init__(self, *, api_key: str, model: str, base_url: str, timeout_sec: float = 120.0):
        if not api_key:
            raise ValueError("external LLM requires api_key")
        if not model:
            raise ValueError("external LLM requires model")
        self.model = model
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(connect=15.0, read=timeout_sec, write=30.0, pool=15.0),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def stream(self, messages: list[dict[str, Any]], *, tools: bool = True) -> AsyncIterator[ChatDelta]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "temperature": 0.7,
            "max_tokens": 500,
            "enable_thinking": False,
        }
        if tools:
            payload["tools"] = [PREVIEW_ACTION_TOOL]
            payload["tool_choice"] = "auto"
        tool_calls: dict[int, dict[str, str]] = {}
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
                except json.JSONDecodeError:
                    continue
                for choice in event.get("choices") or []:
                    delta = choice.get("delta") or {}
                    text = delta.get("content")
                    if text:
                        yield ChatDelta("text", text=str(text))
                    for call in delta.get("tool_calls") or []:
                        index = int(call.get("index", 0))
                        current = tool_calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                        if call.get("id"):
                            current["id"] += str(call["id"])
                        function = call.get("function") or {}
                        if function.get("name"):
                            current["name"] += str(function["name"])
                        if function.get("arguments"):
                            current["arguments"] += str(function["arguments"])
        for index in sorted(tool_calls):
            call = tool_calls[index]
            if call["name"]:
                yield ChatDelta("tool_call", tool_call=call)
