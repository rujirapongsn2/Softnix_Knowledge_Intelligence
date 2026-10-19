import json

import httpx

from app.config import Settings, get_settings
from app.openrouter import OpenRouterClient


def test_llm_model_defaults_to_claude_sonnet(monkeypatch):
    monkeypatch.delenv("OPENROUTER_LLM_MODEL", raising=False)
    assert Settings(_env_file=None).openrouter_llm_model == "anthropic/claude-sonnet-5.5"


def test_openrouter_check_uses_bearer_and_reports_llm_model(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_LLM_MODEL", "provider/llm")
    get_settings.cache_clear()
    paths = []

    def handler(request: httpx.Request):
        assert request.headers["Authorization"] == "Bearer test-key"
        paths.append(request.url.path)
        return httpx.Response(200, json={"data": [{"id": "provider/llm"}]})

    client = OpenRouterClient(httpx.Client(transport=httpx.MockTransport(handler)))
    result = client.check()
    assert result == {"status": "ready", "llm_model": "provider/llm", "llm_model_available": True}
    assert paths == ["/api/v1/models"]
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


def _failing_answer(monkeypatch, status, body):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    get_settings.cache_clear()
    client = OpenRouterClient(httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(status, json=body))))
    try:
        client.answer_from_sources("q", [{"citation_id": "S1", "title": "t", "excerpt": "e"}])
    except RuntimeError as exc:
        return str(exc)
    finally:
        get_settings.cache_clear()
    raise AssertionError("expected a failure")


def test_openrouter_error_codes_separate_rejected_requests_from_outages(monkeypatch):
    assert _failing_answer(monkeypatch, 400, {"error": {"message": "bad model"}}) == "OPENROUTER_REQUEST_REJECTED"
    assert _failing_answer(monkeypatch, 404, {"error": {"message": "no endpoints"}}) == "OPENROUTER_REQUEST_REJECTED"
    assert _failing_answer(monkeypatch, 401, {"error": {"message": "No auth credentials found"}}) == "OPENROUTER_AUTH_FAILED"
    assert _failing_answer(monkeypatch, 402, {"error": {"message": "Insufficient credits"}}) == "OPENROUTER_CREDITS_EXHAUSTED"
    for status in (408, 429, 500, 502, 503):
        assert _failing_answer(monkeypatch, status, {"error": {"message": "x"}}) == "OPENROUTER_UNAVAILABLE", status


def test_openrouter_network_error_stays_unavailable(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    get_settings.cache_clear()

    def handler(request: httpx.Request):
        raise httpx.ConnectError("dns failure", request=request)

    client = OpenRouterClient(httpx.Client(transport=httpx.MockTransport(handler)))
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


def _chat_bodies(monkeypatch, effort):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    if effort is None:
        monkeypatch.delenv("OPENROUTER_REASONING_EFFORT", raising=False)
    else:
        monkeypatch.setenv("OPENROUTER_REASONING_EFFORT", effort)
    get_settings.cache_clear()
    bodies = []

    def handler(request: httpx.Request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"fields": {}}'}}]})

    client = OpenRouterClient(httpx.Client(transport=httpx.MockTransport(handler)))
    client.extract_document_metadata([{"key": "issuer"}], "text")
    client.answer_from_sources("q", [{"citation_id": "S1", "title": "t", "excerpt": "e"}])
    get_settings.cache_clear()
    return bodies


def test_every_chat_call_asks_for_low_reasoning_by_default_so_json_is_not_cut_off(monkeypatch):
    bodies = _chat_bodies(monkeypatch, None)
    assert [body.get("reasoning") for body in bodies] == [{"effort": "low"}, {"effort": "low"}]
    assert bodies[0]["max_tokens"] >= 16000


def test_an_empty_reasoning_effort_sends_no_reasoning_field(monkeypatch):
    assert all("reasoning" not in body for body in _chat_bodies(monkeypatch, ""))
