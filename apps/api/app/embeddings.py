"""Chunk and query embeddings from an OpenAI-compatible ``/embeddings`` endpoint.

The endpoint is configured separately from the chat LLM (today a llama.cpp
``llama-server`` serving embeddinggemma), so its failures carry provider-neutral
EMBEDDING_* codes instead of LLM provider account advice.
"""
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

import httpx

from .config import get_settings
from .request_budget import remaining_timeout

# Tokens are estimated as one per character: Thai text measures about 0.94
# tokens/char, so this stays conservative for Thai and generous for Latin text.
EMBEDDING_MAX_INPUTS_PER_REQUEST = 128
EMBEDDING_MAX_ESTIMATED_TOKENS_PER_REQUEST = 250_000

# llama-server answers an over-long input with a 500 whose body says "too large",
# so the markers are checked before a 5xx is treated as an outage.
_TOO_LARGE_MARKERS = ("maximum request size", "maximum context length", "too many tokens", "too large",
                      "context_length_exceeded", "max_tokens_per_request")


class EmbeddingPurpose(StrEnum):
    QUERY = "query"
    DOCUMENT = "document"


def estimate_tokens(text: str) -> int:
    return max(1, len(text))


def embedding_batches(texts: list[str], max_inputs: int = EMBEDDING_MAX_INPUTS_PER_REQUEST,
                      max_tokens: int = EMBEDDING_MAX_ESTIMATED_TOKENS_PER_REQUEST) -> list[list[str]]:
    """Split texts into ordered batches that stay under both request limits."""
    batches: list[list[str]] = []
    current: list[str] = []
    tokens = 0
    for text in texts:
        cost = estimate_tokens(text)
        if current and (len(current) >= max_inputs or tokens + cost > max_tokens):
            batches.append(current)
            current, tokens = [], 0
        current.append(text)
        tokens += cost
    if current:
        batches.append(current)
    return batches


def embedding_error_code(exc: Exception) -> str:
    if not isinstance(exc, httpx.HTTPStatusError):
        return "EMBEDDING_UNAVAILABLE"
    status = exc.response.status_code
    if status in (401, 403):
        return "EMBEDDING_AUTH_FAILED"
    if status == 402:
        return "EMBEDDING_CREDITS_EXHAUSTED"
    try:
        body = exc.response.text.casefold()
    except Exception:  # pragma: no cover - unreadable streamed body
        body = ""
    if status == 413 or any(marker in body for marker in _TOO_LARGE_MARKERS):
        return "EMBEDDING_REQUEST_TOO_LARGE"
    if status >= 500 or status in (408, 429):
        return "EMBEDDING_UNAVAILABLE"
    return "EMBEDDING_REQUEST_REJECTED"


class EmbeddingClient:
    def __init__(self, client: httpx.Client | None = None, timeout: float = 30):
        self.settings = get_settings()
        self._client = client
        self._timeout = timeout
        self._prefixes = {EmbeddingPurpose.QUERY: self.settings.embedding_query_prefix,
                          EmbeddingPurpose.DOCUMENT: self.settings.embedding_document_prefix}

    @property
    def enabled(self) -> bool:
        return bool(self.settings.embedding_base_url.strip())

    def embed_texts(self, texts: list[str], purpose: EmbeddingPurpose) -> list[list[float]]:
        if not self.enabled:
            raise RuntimeError("EMBEDDING_NOT_CONFIGURED")
        if not texts:
            return []
        prefix = self._prefixes[purpose]
        client = self._client or httpx.Client(timeout=remaining_timeout(self._timeout))
        try:
            vectors: list[list[float]] = []
            for batch in embedding_batches([prefix + text for text in texts]):
                vectors.extend(self._embed_batch(client, batch))
            return vectors
        except httpx.HTTPError as exc:
            raise RuntimeError(embedding_error_code(exc)) from exc
        except (httpx.InvalidURL, ValueError) as exc:
            # A malformed EMBEDDING_BASE_URL is a configuration fault: retrying the job cannot fix it.
            raise RuntimeError("EMBEDDING_NOT_CONFIGURED") from exc
        finally:
            if self._client is None:
                client.close()

    def _embed_batch(self, client: httpx.Client, texts: list[str]) -> list[list[float]]:
        headers = {"Authorization": f"Bearer {self.settings.embedding_api_key}"} if self.settings.embedding_api_key else {}
        response = client.post(f"{self.settings.embedding_base_url.strip().rstrip('/')}/embeddings", headers=headers,
                               json={"model": self.settings.embedding_model, "input": texts, "encoding_format": "float"})
        response.raise_for_status()
        try:
            data = sorted(response.json()["data"], key=lambda item: item.get("index", 0))
            vectors = [item.get("embedding") for item in data]
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise RuntimeError("EMBEDDING_INVALID_RESPONSE") from exc
        if len(vectors) != len(texts) or any(not isinstance(vector, list) for vector in vectors):
            raise RuntimeError("EMBEDDING_INVALID_RESPONSE")
        if any(len(vector) != self.settings.embedding_dimension for vector in vectors):
            raise RuntimeError("EMBEDDING_DIMENSION_MISMATCH")
        return vectors

    def check(self) -> dict[str, Any]:
        """Embed one probe text; never raises, so a missing embedding server does not hide the LLM result."""
        try:
            host = urlsplit(self.settings.embedding_base_url.strip()).netloc or None
        except ValueError:
            host = None
        report = {"embedding_model": self.settings.embedding_model, "embedding_dimension": self.settings.embedding_dimension,
                  "embedding_host": host}
        try:
            self.embed_texts(["ping"], EmbeddingPurpose.QUERY)
        except Exception as exc:
            code = str(exc) if isinstance(exc, RuntimeError) and str(exc).startswith("EMBEDDING_") else "EMBEDDING_UNAVAILABLE"
            return {**report, "embedding_model_available": False, "embedding_error": code}
        return {**report, "embedding_model_available": True}
