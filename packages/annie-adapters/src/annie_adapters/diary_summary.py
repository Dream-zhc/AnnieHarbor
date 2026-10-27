"""One JSON chat completion over DashScope's compatible API: her diary line.

The prompt and parsing live in annie_brain.inner_life; this module only sends
it. It runs after a conversation has ended, never on the reply path.
"""

import httpx

from annie_adapters.net import cloud_proxy

DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


class DiarySummarizer:
    def __init__(self, key: str, *, model="qwen3.8-flash", base_url=DEFAULT_BASE_URL,
                 timeout_ms=4000, client=None):
        self.key, self.model = key, model
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.client = client or httpx.AsyncClient(
            timeout=timeout_ms / 1000, trust_env=False, proxy=cloud_proxy(),
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1))
        self.owns_client = client is None

    async def complete(self, system: str, user: str, *, max_tokens=240) -> str:
        body = {"model": self.model, "temperature": 0.2, "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}]}
        if self.model.startswith("qwen3"):
            body["enable_thinking"] = False
        response = await self.client.post(self.url, json=body,
                                          headers={"Authorization": f"Bearer {self.key}"})
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]

    async def close(self):
        if self.owns_client:
            await self.client.aclose()
