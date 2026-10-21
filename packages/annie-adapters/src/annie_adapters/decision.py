"""Decision model (TypeSafe System One). Transport only, no robot policy.

One POST returns typed answers (choice / noul / score) with probabilities and
confidence; it never generates text. Two endpoints speak this wire: Model
Studio's decision-model-preview and a self-hosted d3 gateway (vllm-sr,
``/v1/systemone``). Keep-alive matters: the gateway adds most of the observed
~0.3s, so the client is long-lived and every call is bounded.
"""

from urllib.parse import urlparse

import httpx

MODEL = "decision-model-preview"
PATH = "/compatible-mode/v1/systemone"
D3_MODEL = "d3-NVFP4"
D3_PATH = "/v1/systemone"
QUESTION_TYPES = frozenset({"choice", "noul", "score"})


def validate_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path != PATH:
        raise ValueError(f"decision endpoint must be https://<workspace host>{PATH}")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("decision endpoint must not contain credentials, query or fragment")
    return url


def gateway_url(url: str) -> str:
    """The systemone URL of a self-hosted gateway given as http(s)://host[:port]."""
    parsed = urlparse(url.strip())
    try:
        parsed.port  # raises on a malformed port
    except ValueError as exc:
        raise ValueError("d3 endpoint has an invalid port") from exc
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.path.rstrip("/") not in {"", D3_PATH}):
        raise ValueError(f"d3 endpoint must be http(s)://<host>[:port], optionally ending in {D3_PATH}")
    if parsed.query or parsed.fragment or parsed.params or parsed.username or parsed.password:
        raise ValueError("decision endpoint must not contain credentials, query or fragment")
    return f"{parsed.scheme}://{parsed.netloc}{D3_PATH}"


def error_kind(exc: BaseException) -> str:
    """Loggable failure: httpx messages carry the URL, which stays out of logs."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


class DecisionClient:
    def __init__(self, url: str, key: str, *, model: str = MODEL, timeout: float = 1.5,
                 client: httpx.AsyncClient | None = None, gateway: bool = False):
        if not key:
            raise ValueError("decision model requires an API key")
        if not 0 < timeout <= 10:
            raise ValueError("decision timeout must be within 10 seconds")
        self.url = gateway_url(url) if gateway else validate_url(url)
        # vLLM serves /health next to the API; Model Studio has none.
        self.health_url = self.url[:-len(D3_PATH)] + "/health" if gateway else None
        self.model = model
        self.timeout = timeout
        self._client = client or httpx.AsyncClient(
            timeout=timeout, headers={"Authorization": f"Bearer {key}"},
            # The default 5 s keep-alive closed the connection between turns and
            # added a TLS handshake to every question.
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4,
                                keepalive_expiry=300),
            # d3 is reached directly: the shell's http(s) proxy added 100-250 ms
            # to every d3 question on 10-21.
            trust_env=not gateway,
        )
        self._owns_client = client is None

    async def ask(self, state, questions: dict, *, timeout: float | None = None) -> dict:
        """Return ``answers`` keyed like ``questions``; raise on any failure."""
        if not isinstance(questions, dict) or not questions or len(questions) > 16:
            raise ValueError("ask 1..16 questions")
        for question in questions.values():
            if not isinstance(question, dict) or question.get("type") not in QUESTION_TYPES:
                raise ValueError("unsupported decision question")
        response = await self._client.post(self.url, json={
            "model": self.model, "state": state, "questions": questions,
        }, timeout=timeout or self.timeout)
        response.raise_for_status()
        answers = response.json().get("answers")
        if not isinstance(answers, dict) or set(answers) != set(questions):
            raise ValueError("decision response does not answer every question")
        return answers

    async def health(self):
        """Raise unless a self-hosted gateway reports healthy.

        A gateway without /health (404) is judged by the warm question alone.
        """
        if self.health_url is None:
            return
        response = await self._client.get(self.health_url, timeout=self.timeout)
        if response.status_code != 404:
            response.raise_for_status()

    async def warm(self, *, timeout: float | None = None):
        """Open the keep-alive connection before the first real question.

        A freshly started d3 container needs about 25 s for its first answer,
        so its probe passes a longer ``timeout`` than real questions get.
        """
        await self.ask("预热", {"warm": {"type": "noul", "instructions": "这是一条预热请求吗？"}},
                       timeout=timeout)

    async def close(self):
        if self._owns_client:
            await self._client.aclose()
