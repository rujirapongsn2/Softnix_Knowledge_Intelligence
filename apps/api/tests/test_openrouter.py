import json

import httpx

from app.config import get_settings
from app.openrouter import OpenRouterClient


def test_openrouter_check_uses_bearer_and_reports_models(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_LLM_MODEL", "provider/llm")
    monkeypatch.setenv("OPENROUTER_EMBEDDING_MODEL", "provider/embed")
    get_settings.cache_clear()

    def handler(request: httpx.Request):
        assert request.headers["Authorization"] == "Bearer test-key"
        if request.url.path == "/api/v1/models":
            return httpx.Response(200, json={"data": [{"id": "provider/llm"}]})
        assert request.url.path == "/api/v1/embeddings/models"
        return httpx.Response(200, json={"data": [{"id": "provider/embed"}]})

    client = OpenRouterClient(httpx.Client(transport=httpx.MockTransport(handler)))
    result = client.check()
    assert result["status"] == "ready"
    assert result["llm_model_available"] is True
    assert result["embedding_model_available"] is True
    get_settings.cache_clear()


def test_openrouter_embeddings_are_sorted_and_dimension_checked(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_EMBEDDING_DIMENSION", "3")
    get_settings.cache_clear()

    def handler(request: httpx.Request):
        assert request.url.path == "/api/v1/embeddings"
        assert request.headers["Authorization"] == "Bearer test-key"
        return httpx.Response(200, json={"data": [{"index": 1, "embedding": [4, 5, 6]}, {"index": 0, "embedding": [1, 2, 3]}]})

    client = OpenRouterClient(httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.embed_texts(["first", "second"]) == [[1, 2, 3], [4, 5, 6]]
    get_settings.cache_clear()


def test_openrouter_rerank_returns_indices_and_scores(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("RERANKER_ENABLED", "true")
    monkeypatch.setenv("OPENROUTER_RERANK_MODEL", "cohere/rerank-v3.5")
    get_settings.cache_clear()

    def handler(request: httpx.Request):
        assert request.url.path == "/api/v1/rerank"
        assert json.loads(request.content)["model"] == "cohere/rerank-v3.5"
        return httpx.Response(200, json={"results": [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.4}]})

    client = OpenRouterClient(httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.rerank("question", ["one", "two"], 2) == [(1, 0.9), (0, 0.4)]
    get_settings.cache_clear()


def test_openrouter_grounded_answer_receives_only_supplied_scoped_sources(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_LLM_MODEL", "provider/llm")
    get_settings.cache_clear()


def test_openrouter_legal_extraction_returns_structured_metadata(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    get_settings.cache_clear()

    def handler(request: httpx.Request):
        assert request.url.path == "/api/v1/chat/completions"
        payload = json.loads(request.content)
        assert "evidence_quote" in payload["messages"][1]["content"]
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "document_type": "service agreement", "parties": [{"name": "Acme", "role": "supplier"}],
            "obligations": [{"party": "Acme", "obligation": "Provide support", "evidence_quote": "shall provide support"}],
            "articles": [{"article_number": "1", "text": "Support", "evidence_quote": "shall provide support"}],
            "amendments": [{"title": "Amendment 1", "announcement_number": "1", "changes": "Updated support", "evidence_quote": "shall provide support"}],
            "confidence": 0.86,
        })}}]})

    client = OpenRouterClient(httpx.Client(transport=httpx.MockTransport(handler)))
    result = client.extract_legal_metadata("Agreement", "Acme shall provide support.")
    assert result["document_type"] == "service agreement"
    assert result["obligations"][0]["party"] == "Acme"
    assert result["articles"][0]["article_number"] == "1"
    assert result["amendments"][0]["title"] == "Amendment 1"
    assert result["governing_law"] is None
    get_settings.cache_clear()

    def handler(request: httpx.Request):
        assert request.url.path == "/api/v1/chat/completions"
        payload = json.loads(request.content)
        prompt = payload["messages"][1]["content"]
        assert "authorized evidence" in prompt.lower()
        assert "Allowed document" in prompt
        assert "other-kb" not in prompt
        return httpx.Response(200, json={"choices": [{"message": {"content": "Grounded answer [S1]."}}]})

    client = OpenRouterClient(httpx.Client(transport=httpx.MockTransport(handler)))
    answer = client.answer_from_sources("What is allowed?", [{
        "citation_id": "S1", "title": "Allowed document", "excerpt": "Authorized fact."
    }])
    assert answer == "Grounded answer [S1]."
    get_settings.cache_clear()
