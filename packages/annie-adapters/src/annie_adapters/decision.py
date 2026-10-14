"""Model Studio decision model (TypeSafe System One). Transport only, no robot policy.

One POST returns typed answers (choice / noul / score) with probabilities and
confidence; it never generates text. Keep-alive matters: the gateway adds most
of the observed ~0.3s, so the client is long-lived and every call is bounded.
"""

from urllib.parse import urlparse

import httpx

MODEL = "decision-model-preview"
PATH = "/compatible-mode/v1/systemone"
QUESTION_TYPES = frozenset({"choice", "noul", "score"})


def validate_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path != PATH:
        raise ValueError(f"decision endpoint must be https://<workspace host>{PATH}")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("decision endpoint must not contain credentials, query or fragment")
    return url


class DecisionClient:
    def __init__(self, url: str, key: str, *, model: str = MODEL, timeout: float = 1.5,
                 client: httpx.AsyncClient | None = None):
        if not key:
            raise ValueError("decision model requires an API key")
        if not 0 < timeout <= 10:
            raise ValueError("decision timeout must be within 10 seconds")
        self.url = validate_url(url)
        self.model = model
        self.timeout = timeout
        self._client = client or httpx.AsyncClient(
            timeout=timeout, headers={"Authorization": f"Bearer {key}"},
            # The default 5 s keep-alive closed the connection between turns and
            # added a TLS handshake to every question.
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4,
                                keepalive_expiry=300),
        )
        self._owns_client = client is None

    async def ask(self, state, questions: dict) -> dict:
        """Return ``answers`` keyed like ``questions``; raise on any failure."""
        if not isinstance(questions, dict) or not questions or len(questions) > 16:
            raise ValueError("ask 1..16 questions")
        for question in questions.values():
            if not isinstance(question, dict) or question.get("type") not in QUESTION_TYPES:
                raise ValueError("unsupported decision question")
        response = await self._client.post(self.url, json={
            "model": self.model, "state": state, "questions": questions,
        }, timeout=self.timeout)
        response.raise_for_status()
        answers = response.json().get("answers")
        if not isinstance(answers, dict) or set(answers) != set(questions):
            raise ValueError("decision response does not answer every question")
        return answers

    async def warm(self):
        """Open the keep-alive connection before the first real question."""
        await self.ask("预热", {"warm": {"type": "noul", "instructions": "这是一条预热请求吗？"}})

    async def close(self):
        if self._owns_client:
            await self._client.aclose()
