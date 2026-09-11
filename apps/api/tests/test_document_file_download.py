"""Download original document file endpoint."""
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import app
from app.models import Document


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
    sources = structured["sources"]
    assert sources
    source = next(item for item in sources if item.get("document_id") == uploaded["document_id"])
    assert source["download_url"] == f"/api/v1/documents/{uploaded['document_id']}/file"
    assert source["original_filename"] == "cited-original.txt"
    assert source["mime_type"].startswith("text/plain")
    # get_sources must return the same enriched fields from stored result_json
    stored = test_client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "get_sources", "arguments": {"result_id": structured["result_id"]}},
    }).json()["result"]["structuredContent"]["sources"]
    stored_source = next(item for item in stored if item.get("document_id") == uploaded["document_id"])
    assert stored_source["download_url"] == source["download_url"]
    file_response = test_client.get(source["download_url"], headers=headers)
    assert file_response.status_code == 200
    assert file_response.content == b"AlphaWidget runs on NODE-42."


def test_document_file_download_soft_deleted_returns_404():
    test_client = next(client())
    doc_id, _payload = _upload(test_client, payload=b"to-delete", suffix="soft-del")
    assert test_client.delete(f"/api/v1/documents/{doc_id}").status_code == 200
    response = test_client.get(f"/api/v1/documents/{doc_id}/file")
    assert response.status_code == 404

