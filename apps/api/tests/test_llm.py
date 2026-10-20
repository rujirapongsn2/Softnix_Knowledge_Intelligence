import json

import httpx

from app.config import Settings, get_settings
from app.llm import LLMClient


def test_llm_model_defaults_to_claude_sonnet(monkeypatch):
    monkeypatch.delenv("LLM_MODEL", raising=False)
    assert Settings(_env_file=None).llm_model == "anthropic/claude-sonnet-5.5"


def test_llm_check_uses_bearer_and_reports_llm_model(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "provider/llm")
    get_settings.cache_clear()
    paths = []

    def handler(request: httpx.Request):
        assert request.headers["Authorization"] == "Bearer test-key"
        paths.append(request.url.path)
        return httpx.Response(200, json={"data": [{"id": "provider/llm"}]})

    client = LLMClient(httpx.Client(transport=httpx.MockTransport(handler)))
    result = client.check()
    assert result == {"status": "ready", "llm_dialect": "openrouter", "llm_model": "provider/llm", "llm_model_available": True}
    assert paths == ["/api/v1/models"]
    get_settings.cache_clear()


def test_llm_rerank_returns_indices_and_scores(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("RERANKER_ENABLED", "true")
    monkeypatch.setenv("RERANK_MODEL", "cohere/rerank-v3.5")
    get_settings.cache_clear()

    def handler(request: httpx.Request):
        assert request.url.path == "/api/v1/rerank"
        assert json.loads(request.content)["model"] == "cohere/rerank-v3.5"
        return httpx.Response(200, json={"results": [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.4}]})

    client = LLMClient(httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.rerank("question", ["one", "two"], 2) == [(1, 0.9), (0, 0.4)]
    get_settings.cache_clear()


def test_llm_grounded_answer_receives_only_supplied_scoped_sources(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "provider/llm")
    get_settings.cache_clear()


def test_llm_legal_extraction_returns_structured_metadata(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
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

    client = LLMClient(httpx.Client(transport=httpx.MockTransport(handler)))
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

    client = LLMClient(httpx.Client(transport=httpx.MockTransport(handler)))
    answer = client.answer_from_sources("What is allowed?", [{
        "citation_id": "S1", "title": "Allowed document", "excerpt": "Authorized fact."
    }])
    assert answer == "Grounded answer [S1]."
    get_settings.cache_clear()


def test_llm_metadata_extraction_accepts_fenced_json(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    get_settings.cache_clear()

    def handler(request: httpx.Request):
        assert request.url.path == "/api/v1/chat/completions"
        content = '```json\n{"fields": {"issuer": [{"value": "Land Department", "evidence_quote": "Issuer: Land Department"}]}}\n```'
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    client = LLMClient(httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.extract_document_metadata(
        [{"key": "issuer", "extraction_description": "Official issuer"}],
        "Issuer: Land Department",
    )["issuer"][0]["value"] == "Land Department"
    get_settings.cache_clear()


def _failing_answer(monkeypatch, status, body):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    get_settings.cache_clear()
    client = LLMClient(httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(status, json=body))))
    try:
        client.answer_from_sources("q", [{"citation_id": "S1", "title": "t", "excerpt": "e"}])
    except RuntimeError as exc:
        return str(exc)
    finally:
        get_settings.cache_clear()
    raise AssertionError("expected a failure")


def test_llm_error_codes_separate_rejected_requests_from_outages(monkeypatch):
    assert _failing_answer(monkeypatch, 400, {"error": {"message": "bad model"}}) == "LLM_REQUEST_REJECTED"
    assert _failing_answer(monkeypatch, 404, {"error": {"message": "no endpoints"}}) == "LLM_REQUEST_REJECTED"
    assert _failing_answer(monkeypatch, 401, {"error": {"message": "No auth credentials found"}}) == "LLM_AUTH_FAILED"
    assert _failing_answer(monkeypatch, 402, {"error": {"message": "Insufficient credits"}}) == "LLM_CREDITS_EXHAUSTED"
    for status in (408, 429, 500, 502, 503):
        assert _failing_answer(monkeypatch, status, {"error": {"message": "x"}}) == "LLM_UNAVAILABLE", status


def test_llm_network_error_stays_unavailable(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    get_settings.cache_clear()

    def handler(request: httpx.Request):
        raise httpx.ConnectError("dns failure", request=request)

    client = LLMClient(httpx.Client(transport=httpx.MockTransport(handler)))
    try:
        client.answer_from_sources("q", [{"citation_id": "S1", "title": "t", "excerpt": "e"}])
        raise AssertionError("expected a failure")
    except RuntimeError as exc:
        assert str(exc) == "LLM_UNAVAILABLE"
    get_settings.cache_clear()


def test_llm_chat_rejection_is_not_reported_as_outage(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    get_settings.cache_clear()
    client = LLMClient(httpx.Client(transport=httpx.MockTransport(
        lambda _request: httpx.Response(400, json={"error": {"message": "maximum context length exceeded"}}))))
    try:
        client.answer_from_sources("q", [{"citation_id": "S1", "title": "t", "excerpt": "e"}])
        raise AssertionError("expected a failure")
    except RuntimeError as exc:
        assert str(exc) == "LLM_REQUEST_REJECTED"
    get_settings.cache_clear()


def _chat_bodies(monkeypatch, effort):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    if effort is None:
        monkeypatch.delenv("LLM_REASONING_EFFORT", raising=False)
    else:
        monkeypatch.setenv("LLM_REASONING_EFFORT", effort)
    get_settings.cache_clear()
    bodies = []

    def handler(request: httpx.Request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"fields": {}}'}}]})

    client = LLMClient(httpx.Client(transport=httpx.MockTransport(handler)))
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


def _first_chat(monkeypatch, env):
    for name in ("LLM_BASE_URL", "LLM_API_DIALECT", "LLM_TEMPERATURE", "LLM_REASONING_EFFORT", "OPENROUTER_BASE_URL", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    seen = []

    def handler(request: httpx.Request):
        seen.append((str(request.url), dict(request.headers), json.loads(request.content)))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"fields": {}}'}}]})

    LLMClient(httpx.Client(transport=httpx.MockTransport(handler))).extract_document_metadata([{"key": "issuer"}], "text")
    get_settings.cache_clear()
    return seen[0]


def test_an_openai_base_url_gets_openai_field_names_and_no_openrouter_headers(monkeypatch):
    url, headers, body = _first_chat(monkeypatch, {"LLM_BASE_URL": "https://api.openai.com/v1", "LLM_TEMPERATURE": ""})
    assert url == "https://api.openai.com/v1/chat/completions"
    assert body["reasoning_effort"] == "low" and "reasoning" not in body
    assert body["max_completion_tokens"] == 16000 and "max_tokens" not in body
    assert "temperature" not in body
    assert "x-openrouter-title" not in headers and "http-referer" not in headers


def test_any_other_server_is_treated_as_generic_openai_compatible(monkeypatch):
    url, headers, body = _first_chat(monkeypatch, {"LLM_BASE_URL": "http://vllm:8000/v1"})
    assert url == "http://vllm:8000/v1/chat/completions"
    assert body["reasoning_effort"] == "low" and body["max_tokens"] == 16000 and body["temperature"] == 0
    assert "x-openrouter-title" not in headers


def test_an_explicit_dialect_overrides_the_one_guessed_from_the_url(monkeypatch):
    _, headers, body = _first_chat(monkeypatch, {"LLM_BASE_URL": "https://gateway.example/v1", "LLM_API_DIALECT": "openrouter"})
    assert body["reasoning"] == {"effort": "low"} and body["max_tokens"] == 16000
    assert headers["x-openrouter-title"]


def test_the_openrouter_names_of_earlier_releases_still_configure_the_llm(monkeypatch):
    for name in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://openrouter.example/v1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "old-key")
    monkeypatch.setenv("OPENROUTER_LLM_MODEL", "old/model")
    settings = Settings(_env_file=None)
    assert (settings.llm_base_url, settings.llm_api_key, settings.llm_model) == ("https://openrouter.example/v1", "old-key", "old/model")
    monkeypatch.setenv("LLM_MODEL", "new/model")
    assert Settings(_env_file=None).llm_model == "new/model"


def test_rerank_can_use_its_own_server_without_the_llm_key(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("RERANKER_ENABLED", "true")
    monkeypatch.setenv("RERANK_MODEL", "bge-reranker")
    monkeypatch.setenv("RERANK_BASE_URL", "http://reranker:8091/v1")
    get_settings.cache_clear()
    seen = []

    def handler(request: httpx.Request):
        seen.append((str(request.url), request.headers.get("authorization")))
        return httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 0.7}]})

    client = LLMClient(httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.reranker_enabled and not client.llm_enabled
    assert client.rerank("q", ["one"], 1) == [(0, 0.7)]
    assert seen == [("http://reranker:8091/v1/rerank", None)]
    get_settings.cache_clear()


def test_rerank_sends_only_the_start_of_long_candidates_so_one_cannot_fail_the_request(monkeypatch):
    monkeypatch.setenv("RERANKER_ENABLED", "true")
    monkeypatch.setenv("RERANK_MODEL", "bge-reranker")
    monkeypatch.setenv("RERANK_BASE_URL", "http://reranker:8091/v1")
    get_settings.cache_clear()
    bodies = []

    def handler(request: httpx.Request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 0.1}, {"index": 1, "relevance_score": 0.9}]})

    client = LLMClient(httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.rerank("ถาม" * 2000, ["ก" * 20000, "short"], 2) == [(0, 0.1), (1, 0.9)]
    assert [len(document) for document in bodies[0]["documents"]] == [2000, 5]
    assert len(bodies[0]["query"]) == 1000
    get_settings.cache_clear()


def _tei_env(monkeypatch, **extra):
    monkeypatch.setenv("RERANKER_ENABLED", "true")
    monkeypatch.setenv("RERANK_API_STYLE", "tei")
    monkeypatch.setenv("RERANK_BASE_URL", "https://reranker.internal")
    monkeypatch.setenv("RERANK_API_KEY", "rerank-key")
    monkeypatch.delenv("RERANK_MODEL", raising=False)
    for name, value in extra.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()


def test_a_tei_reranker_gets_texts_and_its_bare_list_is_read(monkeypatch):
    _tei_env(monkeypatch)
    seen = []

    def handler(request: httpx.Request):
        seen.append((str(request.url), request.headers["authorization"], json.loads(request.content)))
        return httpx.Response(200, json=[{"index": 1, "score": 0.99}, {"index": 0, "score": 0.01}])

    client = LLMClient(httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.reranker_enabled, "a TEI server serves one model, so RERANK_MODEL is not required"
    assert client.rerank("เมืองหลวง", ["เชียงใหม่", "กรุงเทพ"], 2) == [(1, 0.99), (0, 0.01)]
    assert seen == [("https://reranker.internal/rerank", "Bearer rerank-key", {"query": "เมืองหลวง", "texts": ["เชียงใหม่", "กรุงเทพ"], "truncate": True})]
    get_settings.cache_clear()


def test_a_reply_in_the_other_style_is_reported_as_unavailable_not_raised(monkeypatch):
    _tei_env(monkeypatch)
    client = LLMClient(httpx.Client(transport=httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 0.5}]}))))
    try:
        client.rerank("q", ["one"], 1)
        raise AssertionError("expected a failure")
    except RuntimeError as exc:
        assert str(exc) == "RERANKER_UNAVAILABLE"
    get_settings.cache_clear()


def test_rerank_verify_ssl_false_reaches_the_http_client(monkeypatch):
    _tei_env(monkeypatch, RERANK_VERIFY_SSL="false")
    import app.llm as llm
    created = []
    real_client = httpx.Client

    def client_factory(**kwargs):
        created.append(kwargs.get("verify"))
        return real_client(transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=[{"index": 0, "score": 1.0}])))

    monkeypatch.setattr(llm.httpx, "Client", client_factory)
    assert LLMClient().rerank("q", ["one"], 1) == [(0, 1.0)]
    assert created == [False]
    get_settings.cache_clear()


def test_an_empty_llm_name_falls_back_to_the_old_name_like_compose_does(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("OPENROUTER_API_KEY", "old-key")
    monkeypatch.setenv("LLM_REASONING_EFFORT", "")
    monkeypatch.delenv("OPENROUTER_REASONING_EFFORT", raising=False)
    settings = Settings(_env_file=None)
    assert settings.llm_api_key == "old-key"
    assert settings.llm_reasoning_effort == "", "an empty effort with no old name still means send none"


def test_the_llm_key_never_goes_to_a_separate_rerank_host(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "llm-key")
    monkeypatch.setenv("RERANKER_ENABLED", "true")
    monkeypatch.setenv("RERANK_MODEL", "bge")
    monkeypatch.delenv("RERANK_API_KEY", raising=False)
    sent = {}

    def headers_for(base_url):
        monkeypatch.setenv("RERANK_BASE_URL", base_url)
        get_settings.cache_clear()

        def handler(request: httpx.Request):
            sent[base_url] = request.headers.get("authorization")
            return httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 1.0}]})
        LLMClient(httpx.Client(transport=httpx.MockTransport(handler))).rerank("q", ["one"], 1)

    headers_for("http://reranker:8091/v1")
    headers_for("https://openrouter.ai/api/v1/")
    assert sent == {"http://reranker:8091/v1": None, "https://openrouter.ai/api/v1/": "Bearer llm-key"}
    get_settings.cache_clear()


def test_a_tei_reranker_without_its_own_url_is_off_instead_of_calling_the_llm(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "llm-key")
    monkeypatch.setenv("RERANKER_ENABLED", "true")
    monkeypatch.setenv("RERANK_API_STYLE", "tei")
    monkeypatch.delenv("RERANK_BASE_URL", raising=False)
    get_settings.cache_clear()
    assert not LLMClient().reranker_enabled
    get_settings.cache_clear()
