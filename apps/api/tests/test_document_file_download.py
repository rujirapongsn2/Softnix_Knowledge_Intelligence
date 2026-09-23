"""Download original document file endpoint."""
import hashlib
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.data_quality import _has_value
from app.main import app
from app.models import Document, DocumentChunk, QueryResult
from app.services import exclude_not_queryable_sources


def client():
    with TestClient(app) as test_client:
        assert test_client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "correct-horse-battery-staple"},
        ).status_code == 200
        yield test_client


def _upload(test_client, *, payload: bytes = b"original-bytes-v1", suffix: str | None = None):
    suffix = suffix or uuid.uuid4().hex[:8]
    kb = test_client.post(
        "/api/v1/knowledge-bases",
        json={"name": f"Download Original {suffix}", "code": f"download-original-{suffix}"},
    ).json()
    assert test_client.post(f"/api/v1/knowledge-bases/{kb['id']}/activate").status_code == 200
    uploaded = test_client.post(
        f"/api/v1/knowledge-bases/{kb['id']}/documents",
        files={"file": ("sample-original.txt", payload, "text/plain")},
    ).json()
    assert uploaded["status"] == "queued"
    return uploaded["document_id"], payload


def test_document_file_download_returns_original_bytes():
    test_client = next(client())
    doc_id, payload = _upload(test_client)
    response = test_client.get(f"/api/v1/documents/{doc_id}/file")
    assert response.status_code == 200
    assert response.content == payload
    disposition = response.headers.get("content-disposition") or ""
    assert "sample-original.txt" in disposition
    assert "attachment" in disposition.lower()
    assert response.headers.get("content-type", "").startswith("text/plain")


def test_document_file_download_missing_disk_file_returns_404():
    test_client = next(client())
    doc_id, _payload = _upload(test_client, payload=b"will-be-removed")
    with SessionLocal() as db:
        doc = db.get(Document, doc_id)
        path = Path(doc.storage_path)
        assert path.is_file()
        path.unlink()
        indexed_text = "Indexed text remains usable, but its source file is missing."
        doc.status = "completed"
        doc.extracted_text = indexed_text
        db.add(DocumentChunk(
            document_id=doc.id,
            knowledge_base_id=doc.knowledge_base_id,
            chunk_index=0,
            content=indexed_text,
            content_sha256=hashlib.sha256(indexed_text.encode()).hexdigest(),
            char_start=0,
            char_end=len(indexed_text),
            token_count=10,
            embedding=[0.0] * 1536,
        ))
        db.commit()
    quality = test_client.get(f"/api/v1/documents/{doc_id}/text").json()["quality"]
    assert quality["schema_version"] == "ski.quality.v1"
    assert quality["status"] == "not_queryable"
    assert "source_file_missing" in quality["blockers"]
    response = test_client.get(f"/api/v1/documents/{doc_id}/file")
    assert response.status_code == 404


def test_document_file_download_unknown_document_returns_404():
    test_client = next(client())
    response = test_client.get("/api/v1/documents/does-not-exist/file")
    assert response.status_code == 404

def test_document_file_download_disposition_inline():
    test_client = next(client())
    doc_id, payload = _upload(test_client, payload=b"%PDF-1.4 inline-preview", suffix="inline")
    response = test_client.get(f"/api/v1/documents/{doc_id}/file?disposition=inline")
    assert response.status_code == 200
    assert response.content == payload
    disposition = response.headers.get("content-disposition") or ""
    assert "inline" in disposition.lower()
    assert "attachment" not in disposition.lower()


def test_document_file_download_invalid_disposition_rejected():
    test_client = next(client())
    doc_id, _payload = _upload(test_client, suffix="bad-disp")
    response = test_client.get(f"/api/v1/documents/{doc_id}/file?disposition=something-else")
    assert response.status_code == 422

def test_mcp_bearer_can_download_original_for_allowed_kb_and_denied_for_other():
    """MCP read tokens may GET /file for KBs in scope without documents:write."""
    test_client = next(client())
    allowed_id, payload = _upload(test_client, payload=b"mcp-allowed-bytes", suffix="mcp-ok")
    other_id, _ = _upload(test_client, payload=b"mcp-other-bytes", suffix="mcp-other")
    with SessionLocal() as db:
        allowed_doc = db.get(Document, allowed_id)
        other_doc = db.get(Document, other_id)
        allowed_kb = allowed_doc.knowledge_base_id
        other_kb = other_doc.knowledge_base_id
    token = test_client.post("/api/v1/tokens", json={
        "name": "mcp-file-reader",
        "allowed_knowledge_base_ids": [allowed_kb],
        "allowed_tools": ["search_knowledge", "get_sources"],
    }).json()
    headers = {"Authorization": f"Bearer {token['token']}"}
    ok = test_client.get(f"/api/v1/documents/{allowed_id}/file", headers=headers)
    assert ok.status_code == 200
    assert ok.content == payload
    denied = test_client.get(f"/api/v1/documents/{other_id}/file", headers=headers)
    assert denied.status_code == 404


def test_search_knowledge_sources_include_download_url():
    test_client = next(client())
    kb = test_client.post(
        "/api/v1/knowledge-bases",
        json={"name": "Citation Download", "code": f"citation-dl-{uuid.uuid4().hex[:8]}"},
    ).json()
    assert test_client.post(f"/api/v1/knowledge-bases/{kb['id']}/activate").status_code == 200
    uploaded = test_client.post(
        f"/api/v1/knowledge-bases/{kb['id']}/documents",
        files={"file": ("cited-original.txt", b"AlphaWidget runs on NODE-42.", "text/plain")},
    ).json()
    assert uploaded["status"] == "queued"
    # Shared SQLite test DB may have leftover queued jobs; drain until this doc completes.
    for _ in range(20):
        status = test_client.get(f"/api/v1/documents/{uploaded['document_id']}/text").json()["status"]
        if status == "completed":
            break
        test_client.post("/api/v1/internal/process-next")
    else:
        raise AssertionError("document did not complete processing")
    quality = test_client.get(f"/api/v1/documents/{uploaded['document_id']}/text").json()["quality"]
    assert quality["schema_version"] == "ski.quality.v1"
    assert quality["status"] in {"ai_ready", "verified"}
    assert quality["blockers"] == []
    assert quality["metrics"]["chunks"] >= 1
    assert quality["metrics"]["embedding_coverage"] == 1
    assert "pages" in quality["metrics"]
    kb_quality = test_client.get(f"/api/v1/knowledge-bases/{kb['id']}/quality-readiness").json()
    assert kb_quality["schema_version"] == "ski.kb-quality.v1"
    assert kb_quality["aggregation_method"] == "equal_weight_mean"
    assert kb_quality["document_count"] == 1
    assert kb_quality["score"] == quality["score"]
    assert kb_quality["dimensions"] == quality["dimensions"]
    assert kb_quality["documents"][0]["document_id"] == uploaded["document_id"]
    assert kb_quality["documents"][0]["score"] == quality["score"]
    token = test_client.post("/api/v1/tokens", json={
        "name": "citation-agent",
        "allowed_knowledge_base_ids": [kb["id"]],
        "allowed_tools": ["search_knowledge", "get_sources"],
    }).json()
    headers = {"Authorization": f"Bearer {token['token']}"}
    reply = test_client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "search_knowledge", "arguments": {"query": "What runs on NODE-42?"}},
    })
    assert reply.status_code == 200
    structured = reply.json()["result"]["structuredContent"]
    assert structured["schema_version"] == "ski.answer.v1"
    sources = structured["sources"]
    assert sources
    source = next(item for item in sources if item.get("document_id") == uploaded["document_id"])
    expected_download_path = f"/api/v1/documents/{uploaded['document_id']}/file"
    assert urlsplit(source["download_url"]).path == expected_download_path
    assert source["original_filename"] == "cited-original.txt"
    assert source["mime_type"].startswith("text/plain")
    reference = next(item for item in structured["references"] if item.get("document_id") == uploaded["document_id"])
    assert reference["citation_id"] == source["citation_id"]
    assert reference["file"]["download_url"] == source["download_url"]
    assert reference["file"]["access"]["authentication"] == "bearer_or_session"
    assert reference["quality"]["schema_version"] == "ski.quality.v1"
    assert reference["quality"]["blockers"] == []
    assert "pages" not in reference["quality"]["metrics"]
    assert structured["claims"]
    assert reference["id"] in structured["claims"][0]["reference_ids"]
    # get_sources must return the same enriched fields from stored result_json
    stored_result = test_client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "get_sources", "arguments": {"result_id": structured["result_id"]}},
    }).json()["result"]["structuredContent"]
    assert stored_result["schema_version"] == "ski.answer.v1"
    assert stored_result["references"][0]["file"]["download_url"]
    assert stored_result["references"][0]["quality"]["schema_version"] == "ski.quality.v1"
    stored = stored_result["sources"]
    stored_source = next(item for item in stored if item.get("document_id") == uploaded["document_id"])
    assert stored_source["download_url"] == source["download_url"]

    # Results cached before URL filtering must also be sanitized at both read
    # boundaries without mutating or deleting the cached record.
    blocked_url = "https://searchlaw.ocs.go.th/example"
    with SessionLocal() as db:
        saved = db.get(QueryResult, structured["result_id"])
        cached = dict(saved.result_json)
        cached["sources"] = [{
            **item,
            "source_uri": blocked_url,
            "provenance": {"origin": "legal_registry", "source_uri": blocked_url},
        } for item in cached["sources"]]
        saved.result_json = cached
        db.commit()
    stored = test_client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "get_sources", "arguments": {"result_id": structured["result_id"]}},
    }).json()["result"]["structuredContent"]["sources"]
    assert all(item.get("source_uri") is None for item in stored)
    assert all(item.get("provenance", {}).get("source_uri") is None for item in stored)
    admin_sources = test_client.get(f"/api/v1/query/results/{structured['result_id']}/sources").json()["sources"]
    assert all(item.get("source_uri") is None for item in admin_sources)

    file_response = test_client.get(expected_download_path, headers=headers)
    assert file_response.status_code == 200
    assert file_response.content == b"AlphaWidget runs on NODE-42."

    # A source that fails a hard readiness gate must be removed before the
    # MCP answer/reference contract is built, rather than relying on the Agent
    # to notice the quality field after it has already received an answer.
    with SessionLocal() as db:
        document = db.get(Document, uploaded["document_id"])
        document.extracted_text = ""
        db.query(DocumentChunk).filter(DocumentChunk.document_id == document.id).update({"content": " \n\t "})
        db.commit()
    blocked_reply = test_client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "search_knowledge", "arguments": {"query": "What runs on NODE-42?"}},
    }).json()["result"]["structuredContent"]
    assert blocked_reply["sources"] == []
    assert blocked_reply["references"] == []
    assert blocked_reply["insufficient_evidence"] is True


def test_quality_gate_excludes_unusable_evidence_and_preserves_verified_registry_facts():
    sources = [
        {"citation_id": "S1", "excerpt": "unusable", "quality": {"status": "not_queryable"}},
        {
            "citation_id": "S2", "excerpt": "มาตรา 7 ถูกยกเลิก",
            "quality": {"status": "not_queryable"},
            "provenance": {"origin": "verified_legal_relation", "review_status": "verified"},
        },
    ]
    warnings = []
    blocked = exclude_not_queryable_sources(sources, warnings)
    assert blocked == {"S1"}
    assert [source["citation_id"] for source in sources] == ["S2"]
    assert warnings[0]["code"] == "DATA_QUALITY_SOURCE_EXCLUDED"


def test_quality_metadata_presence_rejects_blank_and_empty_values():
    assert not _has_value(None)
    assert not _has_value("")
    assert not _has_value("   ")
    assert not _has_value([])
    assert not _has_value({})
    assert _has_value("value")
    assert _has_value(["value"])
    assert _has_value(False)


def test_empty_knowledge_base_quality_has_explicit_not_available_status():
    test_client = next(client())
    kb = test_client.post(
        "/api/v1/knowledge-bases",
        json={"name": "Empty Quality", "code": f"empty-quality-{uuid.uuid4().hex[:8]}"},
    ).json()
    quality = test_client.get(f"/api/v1/knowledge-bases/{kb['id']}/quality-readiness").json()
    assert quality["score"] == 0
    assert quality["status"] == "not_available"
    assert quality["document_count"] == 0
    assert quality["documents"] == []


def test_knowledge_base_quality_paginates_uses_cache_and_excludes_deleted_documents():
    test_client = next(client())
    suffix = uuid.uuid4().hex[:8]
    kb = test_client.post(
        "/api/v1/knowledge-bases",
        json={"name": f"Aggregate Quality {suffix}", "code": f"aggregate-quality-{suffix}"},
    ).json()
    assert test_client.post(f"/api/v1/knowledge-bases/{kb['id']}/activate").status_code == 200
    document_ids = []
    for index in range(2):
        uploaded = test_client.post(
            f"/api/v1/knowledge-bases/{kb['id']}/documents",
            files={"file": (f"quality-{index}.txt", f"Evidence document {index} with useful content.".encode(), "text/plain")},
        ).json()
        document_ids.append(uploaded["document_id"])
    for _ in range(40):
        statuses = [test_client.get(f"/api/v1/documents/{document_id}/text").json()["status"] for document_id in document_ids]
        if all(status == "completed" for status in statuses):
            break
        test_client.post("/api/v1/internal/process-next")
    else:
        raise AssertionError("aggregate quality documents did not complete processing")

    with SessionLocal() as db:
        blocked = db.get(Document, document_ids[1])
        blocked.extracted_text = ""
        db.query(DocumentChunk).filter(DocumentChunk.document_id == blocked.id).update({"content": "   "})
        db.commit()

    first = test_client.get(f"/api/v1/knowledge-bases/{kb['id']}/quality-readiness?limit=1").json()
    assert first["document_count"] == 2
    assert first["document_limit"] == 1
    assert first["document_offset"] == 0
    assert len(first["documents"]) == 1
    assert first["documents"][0]["document_id"] == document_ids[1]
    assert first["documents"][0]["status"] == "not_queryable"

    cached = test_client.get(f"/api/v1/knowledge-bases/{kb['id']}/quality-readiness?limit=1").json()
    assert cached["documents"][0]["evaluated_at"] == first["documents"][0]["evaluated_at"]
    second_page = test_client.get(f"/api/v1/knowledge-bases/{kb['id']}/quality-readiness?limit=1&offset=1").json()
    assert second_page["documents"][0]["document_id"] == document_ids[0]

    assert test_client.delete(f"/api/v1/documents/{document_ids[1]}").status_code == 200
    after_delete = test_client.get(f"/api/v1/knowledge-bases/{kb['id']}/quality-readiness?limit=1").json()
    assert after_delete["document_count"] == 1
    assert after_delete["documents"][0]["document_id"] == document_ids[0]


def test_document_file_download_soft_deleted_returns_404():
    test_client = next(client())
    doc_id, _payload = _upload(test_client, payload=b"to-delete", suffix="soft-del")
    assert test_client.delete(f"/api/v1/documents/{doc_id}").status_code == 200
    response = test_client.get(f"/api/v1/documents/{doc_id}/file")
    assert response.status_code == 404
