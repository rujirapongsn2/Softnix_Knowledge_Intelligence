import hashlib
import json
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main as main_module, services
from app.config import get_settings
from app.db import SessionLocal
from app.embeddings import EmbeddingClient, EmbeddingPurpose, embedding_batches
from app.main import app
from app.models import Document, DocumentChunk, JobType, KnowledgeBase, ProcessingJob
from app.llm import LLMClient


@pytest.fixture
def embedding_env(monkeypatch):
    monkeypatch.setenv("EMBEDDING_BASE_URL", "http://embed.local:8090/v1")
    monkeypatch.setenv("EMBEDDING_API_KEY", "")
    monkeypatch.setenv("EMBEDDING_MODEL", "gemma-test")
    monkeypatch.setenv("EMBEDDING_DIMENSION", "3")
    monkeypatch.setenv("EMBEDDING_QUERY_PREFIX", "Q> ")
    monkeypatch.setenv("EMBEDDING_DOCUMENT_PREFIX", "D> ")
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


def _client(handler) -> EmbeddingClient:
    return EmbeddingClient(httpx.Client(transport=httpx.MockTransport(handler)))


def _echo(requests: list[httpx.Request], dimension: int = 3):
    def handler(request: httpx.Request):
        requests.append(request)
        inputs = json.loads(request.content)["input"]
        return httpx.Response(200, json={"data": [{"index": i, "embedding": [float(i)] * dimension} for i in range(len(inputs))]})
    return handler


def test_request_goes_to_configured_server_without_auth_or_dimensions(embedding_env):
    requests = []
    assert _client(_echo(requests)).embed_texts(["hello"], EmbeddingPurpose.QUERY) == [[0.0, 0.0, 0.0]]
    (request,) = requests
    assert str(request.url) == "http://embed.local:8090/v1/embeddings"
    assert "authorization" not in request.headers
    assert json.loads(request.content) == {"model": "gemma-test", "input": ["Q> hello"], "encoding_format": "float"}


def test_api_key_is_sent_as_bearer_when_set(embedding_env):
    embedding_env.setenv("EMBEDDING_API_KEY", "secret")
    get_settings.cache_clear()
    requests = []
    _client(_echo(requests)).embed_texts(["hello"], EmbeddingPurpose.DOCUMENT)
    assert requests[0].headers["authorization"] == "Bearer secret"


def test_each_purpose_carries_its_configured_prefix(embedding_env):
    requests = []
    client = _client(_echo(requests))
    client.embed_texts(["ค่าธรรมเนียม", "b"], EmbeddingPurpose.QUERY)
    client.embed_texts(["ค่าธรรมเนียม"], EmbeddingPurpose.DOCUMENT)
    assert [json.loads(request.content)["input"] for request in requests] == [["Q> ค่าธรรมเนียม", "Q> b"], ["D> ค่าธรรมเนียม"]]


def test_vectors_follow_input_order_across_batches(embedding_env):
    embedding_env.setenv("EMBEDDING_DIMENSION", "2")
    get_settings.cache_clear()
    sizes = []

    def handler(request: httpx.Request):
        inputs = json.loads(request.content)["input"]
        sizes.append(len(inputs))
        assert sum(len(text) for text in inputs) <= 250_000 and len(inputs) <= 128
        data = [{"index": i, "embedding": [float(text.removeprefix("Q> ").split(":")[0]), 0.0]} for i, text in enumerate(inputs)]
        return httpx.Response(200, json={"data": list(reversed(data))})

    vectors = _client(handler).embed_texts([f"{i}:" + "ข" * 690 for i in range(493)], EmbeddingPurpose.QUERY)
    assert sizes == [128, 128, 128, 109]
    assert [vector[0] for vector in vectors] == [float(i) for i in range(493)]


def test_small_embedding_is_one_request(embedding_env):
    requests = []
    assert len(_client(_echo(requests)).embed_texts(["a"] * 40, EmbeddingPurpose.DOCUMENT)) == 40
    assert len(requests) == 1


def test_embedding_batches_respect_input_and_token_limits():
    texts = [f"t{i}" for i in range(300)]
    batches = embedding_batches(texts, max_inputs=128, max_tokens=10_000)
    assert [len(batch) for batch in batches] == [128, 128, 44]
    assert [text for batch in batches for text in batch] == texts
    # One oversized input still gets its own request rather than being dropped.
    assert embedding_batches(["x" * 10, "y" * 50, "z"], max_inputs=10, max_tokens=20) == [["x" * 10], ["y" * 50], ["z"]]


def _failure(handler) -> str:
    with pytest.raises(RuntimeError) as raised:
        _client(handler).embed_texts(["text"], EmbeddingPurpose.QUERY)
    return str(raised.value)


def _status(status: int, message: str = "x"):
    return lambda _request: httpx.Response(status, json={"error": {"message": message}})


@pytest.mark.parametrize(("status", "message", "code"), [
    (401, "invalid api key", "EMBEDDING_AUTH_FAILED"),
    (403, "forbidden", "EMBEDDING_AUTH_FAILED"),
    (402, "insufficient credits", "EMBEDDING_CREDITS_EXHAUSTED"),
    (413, "payload", "EMBEDDING_REQUEST_TOO_LARGE"),
    (400, "maximum request size is 300000 tokens", "EMBEDDING_REQUEST_TOO_LARGE"),
    (500, "input (12003 tokens) is too large to process. increase the physical batch size (current batch size: 8192)", "EMBEDDING_REQUEST_TOO_LARGE"),
    (400, "bad model", "EMBEDDING_REQUEST_REJECTED"),
    (404, "no such route", "EMBEDDING_REQUEST_REJECTED"),
    (408, "timeout", "EMBEDDING_UNAVAILABLE"),
    (429, "slow down", "EMBEDDING_UNAVAILABLE"),
    (500, "x", "EMBEDDING_UNAVAILABLE"),
    (503, "loading model", "EMBEDDING_UNAVAILABLE"),
])
def test_http_failures_map_to_embedding_codes(embedding_env, status, message, code):
    assert _failure(_status(status, message)) == code


def test_unreachable_server_is_unavailable(embedding_env):
    def handler(request: httpx.Request):
        raise httpx.ConnectError("connection refused", request=request)
    assert _failure(handler) == "EMBEDDING_UNAVAILABLE"


def test_wrong_dimension_is_a_mismatch(embedding_env):
    assert _failure(_echo([], dimension=4)) == "EMBEDDING_DIMENSION_MISMATCH"


@pytest.mark.parametrize("response", [
    httpx.Response(200, text="not json"),
    httpx.Response(200, json={"data": []}),
    httpx.Response(200, json={"object": "list"}),
])
def test_unusable_body_is_invalid_response(embedding_env, response):
    assert _failure(lambda _request: response) == "EMBEDDING_INVALID_RESPONSE"


def test_empty_base_url_disables_embeddings(embedding_env):
    embedding_env.setenv("EMBEDDING_BASE_URL", "")
    get_settings.cache_clear()
    client = _client(lambda _request: pytest.fail("no request expected"))
    assert client.enabled is False
    with pytest.raises(RuntimeError, match="EMBEDDING_NOT_CONFIGURED"):
        client.embed_texts(["text"], EmbeddingPurpose.QUERY)
    assert client.check()["embedding_model_available"] is False


def test_check_reports_model_dimension_and_host(embedding_env):
    assert _client(_echo([])).check() == {"embedding_model": "gemma-test", "embedding_dimension": 3,
                                          "embedding_host": "embed.local:8090", "embedding_model_available": True}
    report = _client(_echo([], dimension=5)).check()
    assert report["embedding_model_available"] is False and report["embedding_error"] == "EMBEDDING_DIMENSION_MISMATCH"


def test_system_check_reports_unconfigured_embeddings_without_failing_the_llm_check(embedding_env):
    embedding_env.setenv("EMBEDDING_BASE_URL", "")
    get_settings.cache_clear()
    embedding_env.setattr(LLMClient, "check", lambda _self: {"status": "ready", "llm_model": "m", "llm_model_available": True})
    with TestClient(app) as client:
        assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "correct-horse-battery-staple"}).status_code == 200
        response = client.post("/api/v1/system/test-llm")
    assert response.status_code == 200
    body = response.json()
    assert body["llm_model_available"] is True
    assert body["embedding_model_available"] is False and body["embedding_error"] == "EMBEDDING_NOT_CONFIGURED"


def _record_purpose(monkeypatch, calls: list, result):
    def embed_texts(_self, texts, purpose):
        calls.append((texts, purpose))
        if isinstance(result, Exception):
            raise result
        return [result] * len(texts)
    monkeypatch.setattr(EmbeddingClient, "embed_texts", embed_texts)


def test_document_chunks_are_embedded_as_documents(embedding_env):
    calls = []
    vector = [0.5] * get_settings().embedding_dimension
    _record_purpose(embedding_env, calls, vector)
    suffix = uuid.uuid4().hex[:8]
    with TestClient(app), SessionLocal() as db:
        kb = KnowledgeBase(code=f"embedding-purpose-{suffix}", name=f"Embedding purpose {suffix}", status="active", retrieval_config={})
        db.add(kb); db.flush()
        document = Document(knowledge_base_id=kb.id, original_filename="purpose.txt", stored_filename="purpose.txt",
                            storage_path="/tmp/purpose.txt", mime_type="text/plain", file_size=5, checksum_sha256=suffix * 8,
                            title="Purpose", document_type="general", tags=[], status="completed", extracted_text="chunk")
        db.add(document); db.flush()
        db.add(DocumentChunk(document_id=document.id, knowledge_base_id=kb.id, chunk_index=0, content="chunk text",
                             content_sha256=hashlib.sha256(b"chunk text").hexdigest(), char_start=0, char_end=10, token_count=2))
        db.flush()
        services.embed_document_chunks(db, document.id)
        assert calls == [(["chunk text"], EmbeddingPurpose.DOCUMENT)]
        assert db.query(DocumentChunk).filter_by(document_id=document.id).one().embedding == vector
        db.rollback()


def test_semantic_query_is_embedded_as_query(embedding_env):
    calls = []
    _record_purpose(embedding_env, calls, RuntimeError("EMBEDDING_UNAVAILABLE"))
    postgres = SimpleNamespace(get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")))
    trace = []
    result = services.query_database_vectors(postgres, "ภาษีที่ดิน", [], 5, trace)
    assert calls == [(["ภาษีที่ดิน"], EmbeddingPurpose.QUERY)]
    assert result.sources == [] and trace[-1]["status"] == "unavailable" and trace[-1]["detail"] == "EMBEDDING_UNAVAILABLE"


def test_system_check_still_reports_embeddings_when_the_llm_check_fails(embedding_env):
    def failing_llm_check(_self):
        raise RuntimeError("LLM_AUTH_FAILED")
    embedding_env.setattr(LLMClient, "check", failing_llm_check)
    embedding_env.setattr(EmbeddingClient, "embed_texts", lambda _self, texts, _purpose: [[0.0] * 3 for _ in texts])
    with TestClient(app) as client:
        assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "correct-horse-battery-staple"}).status_code == 200
        response = client.post("/api/v1/system/test-llm")
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "LLM_AUTH_FAILED"
    assert error["embedding_model_available"] is True and error["embedding_host"] == "embed.local:8090"


def test_embedding_stage_is_committed_before_the_embedding_calls(embedding_env):
    seen_stages = []

    def embed_texts(_self, texts, _purpose):
        with SessionLocal() as other:
            seen_stages.append(other.get(ProcessingJob, job_id).current_stage)
        return [[0.5] * 3 for _ in texts]
    embedding_env.setattr(EmbeddingClient, "embed_texts", embed_texts)
    embedding_env.setattr(services.LightRAGRetrievalEngine, "enabled", property(lambda _self: False))
    suffix = uuid.uuid4().hex[:8]
    with TestClient(app), SessionLocal() as db:
        kb = KnowledgeBase(code=f"embedding-stage-{suffix}", name=f"Embedding stage {suffix}", status="active", retrieval_config={})
        db.add(kb); db.flush()
        document = Document(knowledge_base_id=kb.id, original_filename="stage.txt", stored_filename="stage.txt",
                            storage_path="/tmp/stage.txt", mime_type="text/plain", file_size=5, checksum_sha256=suffix * 8,
                            title="Stage", document_type="general", tags=[], status="completed", extracted_text="stage text " * 20)
        db.add(document); db.flush()
        job = ProcessingJob(document_id=document.id, knowledge_base_id=kb.id, job_type=JobType.REINDEX_EMBEDDINGS)
        db.add(job); db.commit()
        job_id = job.id
        while services.process_next_job(db) and not seen_stages:
            pass
    assert seen_stages and seen_stages[0] == "embedding"


def _completed_document(db, suffix):
    kb = KnowledgeBase(code=f"embedding-reindex-{suffix}", name=f"Embedding reindex {suffix}", status="active", retrieval_config={})
    db.add(kb); db.flush()
    document = Document(knowledge_base_id=kb.id, original_filename="reindex.txt", stored_filename="reindex.txt",
                        storage_path="/tmp/reindex.txt", mime_type="text/plain", file_size=5, checksum_sha256=suffix * 8,
                        title="Reindex", document_type="general", tags=[], status="completed", extracted_text="reindex text " * 20)
    db.add(document); db.flush()
    return document


def _run_until_settled(db, job_id):
    for _ in range(100):
        db.expire_all()
        if db.get(ProcessingJob, job_id).status not in ("queued", "running"):
            return db.get(ProcessingJob, job_id)
        assert services.process_next_job(db), "the job is still queued but nothing is claimable"
    pytest.fail("the job never settled")


def test_a_reindex_without_an_embedding_endpoint_fails_instead_of_completing(embedding_env):
    embedding_env.setenv("EMBEDDING_BASE_URL", "")
    get_settings.cache_clear()
    embedding_env.setattr(services.LightRAGRetrievalEngine, "enabled", property(lambda _self: False))
    with TestClient(app), SessionLocal() as db:
        document = _completed_document(db, uuid.uuid4().hex[:8])
        job = ProcessingJob(document_id=document.id, knowledge_base_id=document.knowledge_base_id, job_type=JobType.REINDEX_EMBEDDINGS)
        db.add(job); db.commit()
        document_id, job_id = document.id, job.id
        job = _run_until_settled(db, job_id)
        assert (job.status, job.error_code) == ("failed", "EMBEDDING_NOT_CONFIGURED")
        db.expire_all()
        assert (db.get(Document, document_id).status, db.get(Document, document_id).error_code) == ("completed", None)
        assert not any(chunk.embedding for chunk in db.query(DocumentChunk).filter_by(document_id=document_id))


def test_the_pipeline_still_skips_embeddings_when_no_endpoint_is_set(embedding_env):
    embedding_env.setenv("EMBEDDING_BASE_URL", "")
    get_settings.cache_clear()
    embedding_env.setattr(EmbeddingClient, "embed_texts", lambda *_: pytest.fail("no embedding call expected"))
    with TestClient(app), SessionLocal() as db:
        services.embed_document_chunks(db, _completed_document(db, uuid.uuid4().hex[:8]).id)
        db.rollback()


def _system_status(client):
    main_module._PROBE_CACHE.clear()
    assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "correct-horse-battery-staple"}).status_code == 200
    return client.get("/api/v1/system/status")


def test_system_status_reports_unconfigured_embeddings_without_degrading(embedding_env):
    embedding_env.setenv("EMBEDDING_BASE_URL", "")
    get_settings.cache_clear()
    with TestClient(app) as client:
        response = _system_status(client)
    assert response.status_code == 200
    assert response.json()["dependencies"]["embeddings"] == "not_configured"


def test_system_status_probes_a_configured_embedding_endpoint(embedding_env):
    embedding_env.setattr(EmbeddingClient, "embed_texts", lambda _self, texts, _purpose: [[0.0] * 3 for _ in texts])
    with TestClient(app) as client:
        response = _system_status(client)
        assert response.status_code == 200 and response.json()["dependencies"]["embeddings"] == "ready"

        def down(_self, _texts, _purpose):
            raise RuntimeError("EMBEDDING_UNAVAILABLE")
        embedding_env.setattr(EmbeddingClient, "embed_texts", down)
        response = _system_status(client)
    assert response.status_code == 503
    assert response.json()["error"]["details"]["embeddings"] == "unavailable"


def test_a_reindex_waiting_to_retry_keeps_its_document_searchable(embedding_env):
    def unavailable(_self, _texts, _purpose):
        raise RuntimeError("EMBEDDING_UNAVAILABLE")
    embedding_env.setattr(EmbeddingClient, "embed_texts", unavailable)
    embedding_env.setattr(services.LightRAGRetrievalEngine, "enabled", property(lambda _self: False))
    with TestClient(app), SessionLocal() as db:
        document = _completed_document(db, uuid.uuid4().hex[:8])
        job = ProcessingJob(document_id=document.id, knowledge_base_id=document.knowledge_base_id, job_type=JobType.REINDEX_EMBEDDINGS)
        db.add(job); db.commit()
        document_id, job_id = document.id, job.id
        while db.get(ProcessingJob, job_id).attempt_count == 0:
            assert services.process_next_job(db), "the reindex was never claimed"
            db.expire_all()
        job = db.get(ProcessingJob, job_id)
        assert (job.status, job.current_stage, job.error_code) == ("queued", "retry_wait", "EMBEDDING_UNAVAILABLE")
        assert db.get(Document, document_id).status == "completed"


@pytest.mark.parametrize("base_url", ["http://[bad/v1", "http://embed.local:port/v1", "http://" + "a" * 300 + ".local/v1"])
def test_system_status_reports_a_malformed_embedding_url_as_unavailable(embedding_env, base_url):
    embedding_env.setenv("EMBEDDING_BASE_URL", base_url)
    get_settings.cache_clear()
    assert EmbeddingClient().check()["embedding_model_available"] is False
    with TestClient(app) as client:
        response = _system_status(client)
    assert response.status_code == 503
    assert response.json()["error"]["details"]["embeddings"] == "unavailable"


def test_system_status_does_not_wait_long_for_a_silent_embedding_server(embedding_env):
    import socket
    import time

    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        embedding_env.setenv("EMBEDDING_BASE_URL", f"http://127.0.0.1:{server.getsockname()[1]}/v1")
        get_settings.cache_clear()
        with TestClient(app) as client:
            started = time.monotonic()
            response = _system_status(client)
            elapsed = time.monotonic() - started
    assert response.status_code == 503 and response.json()["error"]["details"]["embeddings"] == "unavailable"
    assert elapsed < 10


def test_a_base_url_httpx_cannot_parse_is_a_configuration_fault_not_an_outage(embedding_env):
    embedding_env.setenv("EMBEDDING_BASE_URL", "http://embed.local:port/v1")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="EMBEDDING_NOT_CONFIGURED"):
        EmbeddingClient().embed_texts(["text"], EmbeddingPurpose.DOCUMENT)
