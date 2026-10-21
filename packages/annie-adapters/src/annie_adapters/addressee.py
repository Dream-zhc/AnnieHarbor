"""Is this utterance for the robot? A general LLM over DashScope's compatible API.

The prompt lives in annie_brain.decision_policy; this module only sends it and
reads {"to": "robot" | "other"}. Errors raise: the caller decides that an outage
must not silence the robot.
"""

import json
import math

import httpx

from annie_adapters.net import cloud_proxy

DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


class AddresseeJudge:
    def __init__(self, key: str, *, model="qwen-flash", base_url=DEFAULT_BASE_URL,
                 timeout_ms=1200, client=None):
        self.key, self.model = key, model
        self.url = base_url.rstrip("/") + "/chat/completions"
        # Direct like every Aliyun call (annie_adapters.net): a desktop proxy
        # only adds latency to a check that sits in front of her reply.
        self.client = client or httpx.AsyncClient(
            timeout=timeout_ms / 1000, trust_env=False, proxy=cloud_proxy(),
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=2))
        self.owns_client = client is None

    async def judge(self, system: str, user: str) -> tuple[str, float | None]:
        """("robot" | "other", confidence 0-1 or None when the model gave none)."""
        body = {"model": self.model, "temperature": 0, "max_tokens": 20,
                # The model's self-reported confidence was always 0.95; the
                # token probabilities of robot/other are the real one.
                "logprobs": True, "top_logprobs": 2,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}]}
        if self.model.startswith("qwen3"):
            body["enable_thinking"] = False
        response = await self.client.post(self.url, json=body,
                                          headers={"Authorization": f"Bearer {self.key}"})
        response.raise_for_status()
        choice = response.json()["choices"][0]
        to = json.loads(choice["message"]["content"]).get("to")
        if to not in {"robot", "other"}:
            raise ValueError("addressee judge returned no choice")
        return to, _confidence(choice.get("logprobs"), to)

    async def close(self):
        if self.owns_client:
            await self.client.aclose()


def _confidence(logprobs, to):
    """P(to) between the two answers at the token that carries it; None if absent."""
    for token in (logprobs or {}).get("content") or []:
        if str(token.get("token", "")).strip() != to:
            continue
        weights = {to: math.exp(token.get("logprob", 0.0))}
        for alternative in token.get("top_logprobs") or []:
            name = str(alternative.get("token", "")).strip()
            if name in {"robot", "other"} and name not in weights:
                weights[name] = math.exp(alternative.get("logprob", -99.0))
        return round(weights[to] / sum(weights.values()), 2)
    return None
