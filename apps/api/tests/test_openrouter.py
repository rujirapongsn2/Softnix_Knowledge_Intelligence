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


def test_openrouter_metadata_extraction_accepts_fenced_json(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    get_settings.cache_clear()

    def handler(request: httpx.Request):
        assert request.url.path == "/api/v1/chat/completions"
        content = '```json\n{"fields": {"issuer": [{"value": "Land Department", "evidence_quote": "Issuer: Land Department"}]}}\n```'
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    client = OpenRouterClient(httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.extract_document_metadata(
        [{"key": "issuer", "extraction_description": "Official issuer"}],
        "Issuer: Land Department",
    )["issuer"][0]["value"] == "Land Department"
    get_settings.cache_clear()


def test_embedding_batches_respect_input_and_token_limits():
    from app.openrouter import embedding_batches

    texts = [f"t{i}" for i in range(300)]
    batches = embedding_batches(texts, max_inputs=128, max_tokens=10_000)
    assert [len(batch) for batch in batches] == [128, 128, 44]
    assert [text for batch in batches for text in batch] == texts
    thai = ["ก" * 690] * 493
    batches = embedding_batches(thai, max_inputs=1000, max_tokens=250_000)
    assert all(sum(len(text) for text in batch) <= 250_000 for batch in batches)
    assert sum(len(batch) for batch in batches) == 493 and len(batches) == 2
    # One oversized input still gets its own request rather than being dropped.
    assert embedding_batches(["x" * 10, "y" * 50, "z"], max_inputs=10, max_tokens=20) == [["x" * 10], ["y" * 50], ["z"]]


def test_openrouter_embeddings_batch_large_documents_in_order(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_EMBEDDING_DIMENSION", "2")
    get_settings.cache_clear()
    sizes = []

    def handler(request: httpx.Request):
        inputs = json.loads(request.content)["input"]
        sizes.append(len(inputs))
        assert sum(len(text) for text in inputs) <= 250_000 and len(inputs) <= 128
        # Return the batch out of order; the client must restore it.
        data = [{"index": i, "embedding": [float(text.split(":")[0]), 0.0]} for i, text in enumerate(inputs)]
        return httpx.Response(200, json={"data": list(reversed(data))})

    client = OpenRouterClient(httpx.Client(transport=httpx.MockTransport(handler)))
    texts = [f"{i}:" + "ข" * 690 for i in range(493)]
    vectors = client.embed_texts(texts)
    assert sizes == [128, 128, 128, 109]
    assert [vector[0] for vector in vectors] == [float(i) for i in range(493)]
    get_settings.cache_clear()


def test_openrouter_small_embedding_is_one_request(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_EMBEDDING_DIMENSION", "1")
    get_settings.cache_clear()
    calls = []

    def handler(request: httpx.Request):
        calls.append(json.loads(request.content)["input"])
        return httpx.Response(200, json={"data": [{"index": i, "embedding": [1.0]} for i in range(len(calls[-1]))]})

    client = OpenRouterClient(httpx.Client(transport=httpx.MockTransport(handler)))
    assert len(client.embed_texts(["a"] * 40)) == 40
    assert calls == [["a"] * 40]
    get_settings.cache_clear()


def _failing_embed(monkeypatch, status, body):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    get_settings.cache_clear()
    client = OpenRouterClient(httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(status, json=body))))
    try:
        client.embed_texts(["text"])
    except RuntimeError as exc:
        return str(exc)
    finally:
        get_settings.cache_clear()
    raise AssertionError("expected a failure")


def test_openrouter_error_codes_separate_rejected_requests_from_outages(monkeypatch):
    too_large = {"error": {"message": "HTTP 400: Invalid 'input': maximum request size is 300000 tokens per request.", "code": 400}}
    assert _failing_embed(monkeypatch, 400, too_large) == "OPENROUTER_EMBEDDING_REQUEST_TOO_LARGE"
    assert _failing_embed(monkeypatch, 413, {"error": {"message": "payload"}}) == "OPENROUTER_EMBEDDING_REQUEST_TOO_LARGE"
    assert _failing_embed(monkeypatch, 400, {"error": {"message": "bad model"}}) == "OPENROUTER_REQUEST_REJECTED"
    assert _failing_embed(monkeypatch, 404, {"error": {"message": "no endpoints"}}) == "OPENROUTER_REQUEST_REJECTED"
    assert _failing_embed(monkeypatch, 401, {"error": {"message": "No auth credentials found"}}) == "OPENROUTER_AUTH_FAILED"
    assert _failing_embed(monkeypatch, 402, {"error": {"message": "Insufficient credits"}}) == "OPENROUTER_CREDITS_EXHAUSTED"
    for status in (408, 429, 500, 502, 503):
        assert _failing_embed(monkeypatch, status, {"error": {"message": "x"}}) == "OPENROUTER_UNAVAILABLE", status


def test_openrouter_network_error_stays_unavailable(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    get_settings.cache_clear()

    def handler(request: httpx.Request):
        raise httpx.ConnectError("dns failure", request=request)

    client = OpenRouterClient(httpx.Client(transport=httpx.MockTransport(handler)))
    try:
        client.embed_texts(["text"])
        raise AssertionError("expected a failure")
    except RuntimeError as exc:
        assert str(exc) == "OPENROUTER_UNAVAILABLE"
    try:
        client.answer_from_sources("q", [{"citation_id": "S1", "title": "t", "excerpt": "e"}])
        raise AssertionError("expected a failure")
    except RuntimeError as exc:
        assert str(exc) == "OPENROUTER_UNAVAILABLE"
    get_settings.cache_clear()


def test_openrouter_chat_rejection_is_not_reported_as_outage(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    get_settings.cache_clear()
    client = OpenRouterClient(httpx.Client(transport=httpx.MockTransport(
        lambda _request: httpx.Response(400, json={"error": {"message": "maximum context length exceeded"}}))))
    try:
        client.answer_from_sources("q", [{"citation_id": "S1", "title": "t", "excerpt": "e"}])
        raise AssertionError("expected a failure")
    except RuntimeError as exc:
        assert str(exc) == "OPENROUTER_REQUEST_REJECTED"
    get_settings.cache_clear()
