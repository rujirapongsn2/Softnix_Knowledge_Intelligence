import json

import httpx

from app.retrieval import LightRAGRetrievalEngine


def test_lightrag_adapter_ingests_and_maps_citations():
    requests = []

    def handler(request: httpx.Request):
        requests.append(request)
        if request.url.path == "/documents/text":
            payload = json.loads(request.content)
            assert payload == {"text": "Customer Portal is hosted on APP-01.", "file_source": "skip/kb-1/softnix-kb=kb-1__doc=doc-1__Architecture"}
            return httpx.Response(202, json={"track_id": "track-1"})
        return httpx.Response(200, json={"response": "Customer Portal is hosted on APP-01.", "references": [{
            "reference_id": "chunk-7", "file_path": "softnix-kb=kb-1__doc=doc-1__Architecture", "content": ["Customer Portal is hosted on APP-01."], "score": 0.94,
        }, {"reference_id": "other", "file_path": "softnix-kb=other-kb__doc=other-doc__Other", "content": ["must be filtered"], "score": 1.0}]})

    engine = LightRAGRetrievalEngine(base_url="http://lightrag.test", api_key="key", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert engine.ingest("doc-1", "kb-1", "Customer Portal is hosted on APP-01.", "Architecture") == "track-1"
    evidence = engine.query("What is hosted on APP-01?", ["kb-1"], 10)
    assert evidence.answer is None
    assert evidence.sources[0]["citation_id"] == "S1"
    assert evidence.sources[0]["document_id"] == "doc-1"
    assert evidence.sources[0]["title"] == "Architecture"
    assert len(evidence.sources) == 1
    assert requests[0].headers["X-API-Key"] == "key"


def test_lightrag_adapter_reads_processing_track_status():
    def handler(request: httpx.Request):
        assert request.url.path == "/documents/track_status/track-1"
        return httpx.Response(200, json={"documents": [{"status": "PROCESSED"}]})

    engine = LightRAGRetrievalEngine(base_url="http://lightrag.test", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert engine.track_status("track-1") == "processed"


def test_lightrag_adapter_replaces_failed_source_before_retrying():
    calls = []
    failed = {"id": "remote-1", "status": "failed", "file_path": "softnix-kb=kb-1__doc=doc-1__Architecture"}

    def handler(request: httpx.Request):
        calls.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path == "/documents/text":
            if calls.count(("POST", "/documents/text")) == 1:
                return httpx.Response(409, json={"detail": "already exists"})
            return httpx.Response(202, json={"track_id": "track-2"})
        if request.method == "GET" and request.url.path == "/documents":
            # The source disappears after the delete request.
            return httpx.Response(200, json={"statuses": {"failed": [] if ("DELETE", "/documents/delete_document") in calls else [failed]}})
        if request.method == "DELETE" and request.url.path == "/documents/delete_document":
            return httpx.Response(200, json={"status": "deletion_started"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url.path}")

    engine = LightRAGRetrievalEngine(base_url="http://lightrag.test", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert engine.ingest("doc-1", "kb-1", "evidence", "Architecture") == "track-2"
    assert calls == [
        ("POST", "/documents/text"), ("GET", "/documents"),
        ("DELETE", "/documents/delete_document"), ("GET", "/documents"),
        ("POST", "/documents/text"),
    ]


def test_lightrag_adapter_reuses_processed_source_after_worker_retry():
    calls = []
    processed = {
        "id": "remote-1",
        "status": "processed",
        "track_id": "track-existing",
        "file_path": "softnix-kb=kb-1__doc=doc-1__Architecture",
    }

    def handler(request: httpx.Request):
        calls.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path == "/documents/text":
            return httpx.Response(409, json={"detail": "already exists"})
        if request.method == "GET" and request.url.path == "/documents":
            return httpx.Response(200, json={"statuses": {"processed": [processed]}})
        raise AssertionError(f"Unexpected request: {request.method} {request.url.path}")

    engine = LightRAGRetrievalEngine(base_url="http://lightrag.test", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert engine.ingest("doc-1", "kb-1", "evidence", "Architecture") == "track-existing"
    assert calls == [("POST", "/documents/text"), ("GET", "/documents")]


def test_lightrag_adapter_requires_single_workspace():
    engine = LightRAGRetrievalEngine(base_url="http://lightrag.test", client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200))))
    try:
        engine.query("question", ["kb-1", "kb-2"], 10)
    except RuntimeError as exc:
        assert str(exc) == "MULTI_KNOWLEDGE_BASE_ENGINE_QUERY_UNSUPPORTED"
    else:
        raise AssertionError("Expected a workspace-scope validation error")
