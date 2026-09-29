import os
import tempfile
import uuid
from pathlib import Path
import pytest

_TEST_ROOT = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_ROOT}/skip.db"
os.environ["FILE_STORAGE_PATH"] = f"{_TEST_ROOT}/files"
os.environ["INITIAL_ADMIN_PASSWORD"] = "correct-horse-battery-staple"
os.environ["LIGHTRAG_BASE_URL"] = ""
os.environ["REDIS_URL"] = ""
os.environ["OPENROUTER_API_KEY"] = ""
os.environ["EXT_OCR_KEY"] = ""

from fastapi.testclient import TestClient
from app.main import app
from app.db import SessionLocal
from app.models import Document, ProcessingJob
from app.services import build_structured_references, enrich_sources_with_file_links

INGEST_SCOPE = "documents:write"


def client():
    with TestClient(app) as test_client:
        assert test_client.post("/api/v1/auth/login", json={"username": "admin", "password": "correct-horse-battery-staple"}).status_code == 200
        yield test_client


def _knowledge_base(test_client, code: str) -> str:
    """Create an active Knowledge Base under a code unique to this file.

    Every test module shares one sqlite database, so codes are namespaced and all
    assertions look up documents through their Knowledge Base.
    """
    kb = test_client.post("/api/v1/knowledge-bases", json={"name": code, "code": code}).json()
    assert test_client.post(f"/api/v1/knowledge-bases/{kb['id']}/activate").status_code == 200
    return kb["id"]


_UNSET = object()


def _token(test_client, kb_ids, scopes=(INGEST_SCOPE,), tools=(), allowed_ingest_kb_id=_UNSET, **overrides) -> str:
    kb_ids = list(kb_ids)
    scopes = list(scopes)
    # allowed_ingest_knowledge_base_id is a dedicated write-scope axis, separate
    # from allowed_knowledge_base_ids (the MCP read axis); default it to the
    # first requested KB only when a caller is actually asking for write access.
    if allowed_ingest_kb_id is _UNSET:
        allowed_ingest_kb_id = kb_ids[0] if (INGEST_SCOPE in scopes and kb_ids) else None
    payload = {"name": "ingest-agent", "allowed_knowledge_base_ids": kb_ids,
               "allowed_tools": list(tools), "allowed_scopes": scopes,
               "allowed_ingest_knowledge_base_id": allowed_ingest_kb_id, **overrides}
    response = test_client.post("/api/v1/tokens", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["token"]


def _headers(secret: str) -> dict:
    return {"Authorization": f"Bearer {secret}"}


def _drain(test_client) -> None:
    while test_client.post("/api/v1/internal/process-next").json()["processed"]:
        pass


def _upload(test_client, secret: str, kb_id: str, name: str = "note.txt", body: bytes = b"Customer Portal runs on APP-01."):
    return test_client.post(f"/api/v1/ingest/knowledge-bases/{kb_id}/documents",
                            headers=_headers(secret), files={"file": (name, body, "text/plain")})


def test_markdown_and_reference_pdf_are_one_document_with_pdf_citation():
    test_client = next(client())
    kb_id = _knowledge_base(test_client, f"ingest-md-evidence-{uuid.uuid4().hex[:8]}")
    secret = _token(test_client, [kb_id])
    markdown = b"# Land law\nAtlas-741 runs on NODE-42."
    pdf = b"%PDF-1.4\nreference-only evidence"
    uploaded = test_client.post(
        f"/api/v1/ingest/knowledge-bases/{kb_id}/documents",
        headers=_headers(secret),
        files={"file": ("land-law.md", markdown, "text/markdown"),
               "reference_pdf": ("land-law.pdf", pdf, "application/pdf")},
    )
    assert uploaded.status_code == 202, uploaded.text
    doc_id = uploaded.json()["document_id"]
    assert uploaded.json()["reference_pdf"]["filename"] == "land-law.pdf"
    with SessionLocal() as db:
        docs = db.query(Document).filter_by(knowledge_base_id=kb_id).all()
        assert len(docs) == 1 and docs[0].id == doc_id
        assert db.query(ProcessingJob).filter_by(document_id=doc_id).count() == 1
    assert test_client.get(f"/api/v1/ingest/documents/{doc_id}/file", headers=_headers(secret)).content == markdown
    pdf_response = test_client.get(f"/api/v1/ingest/documents/{doc_id}/file?variant=reference_pdf", headers=_headers(secret))
    assert pdf_response.status_code == 200 and pdf_response.content == pdf
    assert pdf_response.headers["content-type"].startswith("application/pdf")
    _drain(test_client)
    detail = test_client.get(f"/api/v1/documents/{doc_id}/text").json()
    assert detail["status"] == "completed"
    assert detail["text"] == markdown.decode()
    assert detail["reference_pdf"]["filename"] == "land-law.pdf"
    with SessionLocal() as db:
        sources = enrich_sources_with_file_links(db, [{"document_id": doc_id, "citation_id": "S1", "title": "Land law"}])
    assert sources[0]["download_url"].endswith("?variant=reference_pdf")
    reference = build_structured_references(sources)[0]
    assert reference["file"]["role"] == "reference_pdf"
    assert reference["file"]["name"] == "land-law.pdf"
    assert reference["content_file"]["name"] == "land-law.md"
    mcp_token = test_client.post("/api/v1/tokens", json={
        "name": "reference-pdf-reader", "allowed_knowledge_base_ids": [kb_id],
        "allowed_tools": ["search_knowledge", "get_sources"],
    }).json()["token"]
    reply = test_client.post("/mcp", headers=_headers(mcp_token), json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "search_knowledge", "arguments": {"query": "What runs on NODE-42?"}},
    })
    assert reply.status_code == 200, reply.text
    structured = reply.json()["result"]["structuredContent"]
    cited = next(item for item in structured["references"] if item["document_id"] == doc_id)
    assert cited["file"]["role"] == "reference_pdf"
    assert cited["file"]["download_url"].endswith("?variant=reference_pdf")
    assert cited["content_file"]["name"] == "land-law.md"
    pdf_for_agent = test_client.get(cited["file"]["download_url"], headers=_headers(mcp_token))
    assert pdf_for_agent.status_code == 200 and pdf_for_agent.content == pdf


def test_reference_pdf_can_attach_after_text_ingest_without_creating_pdf_job():
    test_client = next(client())
    kb_id = _knowledge_base(test_client, f"ingest-text-evidence-{uuid.uuid4().hex[:8]}")
    secret = _token(test_client, [kb_id])
    uploaded = test_client.post(
        f"/api/v1/ingest/knowledge-bases/{kb_id}/documents/text",
        headers=_headers(secret), json={"title": "Land act", "text": "# Land act\nSource text."},
    )
    assert uploaded.status_code == 202
    doc_id = uploaded.json()["document_id"]
    attached = test_client.post(f"/api/v1/ingest/documents/{doc_id}/reference-pdf",
                                headers=_headers(secret), files={"file": ("land-act.pdf", b"%PDF-1.4\nevidence", "application/pdf")})
    assert attached.status_code == 200, attached.text
    assert attached.json()["reference_pdf"]["download_url"].startswith("/api/v1/ingest/")
    assert test_client.post(f"/api/v1/ingest/documents/{doc_id}/reference-pdf",
                            headers=_headers(secret), files={"file": ("again.pdf", b"%PDF-1.4\nagain", "application/pdf")}).status_code == 409
    with SessionLocal() as db:
        assert db.query(Document).filter_by(knowledge_base_id=kb_id).count() == 1
        assert db.query(ProcessingJob).filter_by(document_id=doc_id).count() == 1


@pytest.mark.parametrize("extension,mime,body", [
    ("pdf", "application/pdf", b"%PDF-1.4\nevidence"),
    ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", b"PK\x03\x04docx"),
    ("doc", "application/msword", b"\xd0\xcf\x11\xe0doc"),
    ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", b"PK\x03\x04xlsx"),
    ("xls", "application/vnd.ms-excel", b"\xd0\xcf\x11\xe0xls"),
    ("txt", "text/plain", b"Original evidence text"),
])
def test_reference_file_types_share_markdown_document_without_processing(extension, mime, body):
    test_client = next(client())
    kb_id = _knowledge_base(test_client, f"ref-{extension}-{uuid.uuid4().hex[:8]}")
    secret = _token(test_client, [kb_id])
    uploaded = test_client.post(f"/api/v1/ingest/knowledge-bases/{kb_id}/documents",
                                headers=_headers(secret), files={
                                    "file": ("source.md", b"# Indexed Markdown", "text/markdown"),
                                    "reference_file": (f"evidence.{extension}", body, mime),
                                })
    assert uploaded.status_code == 202, uploaded.text
    doc_id = uploaded.json()["document_id"]
    assert uploaded.json()["reference_file"]["mime_type"] == mime
    assert (uploaded.json()["reference_pdf"] is not None) == (extension == "pdf")
    with SessionLocal() as db:
        assert db.query(Document).filter_by(knowledge_base_id=kb_id).count() == 1
        assert db.query(ProcessingJob).filter_by(document_id=doc_id).count() == 1
        sources = enrich_sources_with_file_links(db, [{"document_id": doc_id, "citation_id": "S1"}])
    assert sources[0]["reference_file"]["mime_type"] == mime
    reference = build_structured_references(sources)[0]
    assert reference["file"]["role"] == ("reference_pdf" if extension == "pdf" else "reference_file")
    assert reference["file"]["mime_type"] == mime
    assert reference["content_file"]["name"] == "source.md"
    downloaded = test_client.get(f"/api/v1/ingest/documents/{doc_id}/file?variant=reference_file", headers=_headers(secret))
    assert downloaded.status_code == 200 and downloaded.content == body
    assert downloaded.headers["content-type"].startswith(mime)
    legacy = test_client.get(f"/api/v1/ingest/documents/{doc_id}/file?variant=reference_pdf", headers=_headers(secret))
    assert legacy.status_code == (200 if extension == "pdf" else 404)
    if extension == "docx":
        mcp_token = test_client.post("/api/v1/tokens", json={
            "name": "docx-reference-reader", "allowed_knowledge_base_ids": [kb_id],
            "allowed_tools": ["search_knowledge"],
        }).json()["token"]
        agent_file = test_client.get(reference["file"]["download_url"], headers=_headers(mcp_token))
        assert agent_file.status_code == 200 and agent_file.content == body


def test_reference_file_can_attach_after_json_text_ingest():
    test_client = next(client())
    kb_id = _knowledge_base(test_client, f"ref-attach-{uuid.uuid4().hex[:8]}")
    secret = _token(test_client, [kb_id])
    uploaded = test_client.post(f"/api/v1/ingest/knowledge-bases/{kb_id}/documents/text",
                                headers=_headers(secret), json={"title": "Policy", "text": "# Policy\nIndexed content"})
    assert uploaded.status_code == 202
    doc_id = uploaded.json()["document_id"]
    attached = test_client.post(f"/api/v1/ingest/documents/{doc_id}/reference-file",
                                headers=_headers(secret), files={"file": ("policy.doc", b"\xd0\xcf\x11\xe0doc", "application/msword")})
    assert attached.status_code == 200, attached.text
    assert attached.json()["reference_file"]["mime_type"] == "application/msword"
    assert attached.json()["reference_pdf"] is None
    assert test_client.post(f"/api/v1/ingest/documents/{doc_id}/reference-file",
                            headers=_headers(secret), files={"file": ("again.txt", b"again", "text/plain")}).status_code == 409
    with SessionLocal() as db:
        assert db.query(ProcessingJob).filter_by(document_id=doc_id).count() == 1


def test_saved_answer_and_references_refresh_when_pdf_is_attached_later():
    test_client = next(client())
    kb_id = _knowledge_base(test_client, f"ingest-late-evidence-{uuid.uuid4().hex[:8]}")
    secret = _token(test_client, [kb_id])
    uploaded = test_client.post(
        f"/api/v1/ingest/knowledge-bases/{kb_id}/documents/text",
        headers=_headers(secret), json={"title": "QRA 927", "text": "# QRA 927\nThe docket code is QRA-927."},
    )
    assert uploaded.status_code == 202
    doc_id = uploaded.json()["document_id"]
    _drain(test_client)
    mcp_token = test_client.post("/api/v1/tokens", json={
        "name": "late-pdf-reader", "allowed_knowledge_base_ids": [kb_id],
        "allowed_tools": ["search_knowledge", "get_sources"],
    }).json()["token"]
    searched = test_client.post("/mcp", headers=_headers(mcp_token), json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "search_knowledge", "arguments": {"query": "What is the docket code QRA-927?"}},
    }).json()["result"]["structuredContent"]
    assert "/file" in searched["answer"]
    assert "?variant=reference_pdf" not in searched["answer"]

    attached = test_client.post(f"/api/v1/ingest/documents/{doc_id}/reference-pdf",
                                headers=_headers(secret), files={"file": ("qra.pdf", b"%PDF-1.4\nevidence", "application/pdf")})
    assert attached.status_code == 200
    result_id = searched["result_id"]
    for refreshed in (
        test_client.get(f"/api/v1/query/results/{result_id}/sources").json(),
        test_client.post("/mcp", headers=_headers(mcp_token), json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "get_sources", "arguments": {"result_id": result_id}},
        }).json()["result"]["structuredContent"],
    ):
        assert "?variant=reference_pdf" in refreshed["answer"]
        assert refreshed["references"][0]["file"]["role"] == "reference_pdf"
        assert refreshed["references"][0]["file"]["download_url"] in refreshed["answer"]


def test_reference_pdf_remains_citable_if_markdown_blob_is_missing():
    test_client = next(client())
    kb_id = _knowledge_base(test_client, f"ingest-missing-md-{uuid.uuid4().hex[:8]}")
    secret = _token(test_client, [kb_id])
    uploaded = test_client.post(f"/api/v1/ingest/knowledge-bases/{kb_id}/documents",
                                headers=_headers(secret), files={
                                    "file": ("source.md", b"# Source", "text/markdown"),
                                    "reference_pdf": ("source.pdf", b"%PDF-1.4\nevidence", "application/pdf"),
                                })
    assert uploaded.status_code == 202
    doc_id = uploaded.json()["document_id"]
    with SessionLocal() as db:
        Path(db.get(Document, doc_id).storage_path).unlink()
        sources = enrich_sources_with_file_links(db, [{"document_id": doc_id, "citation_id": "S1"}])
    assert sources[0]["content_file"] is None
    assert sources[0]["reference_pdf"]["available"] is True
    assert sources[0]["download_url"].endswith("?variant=reference_pdf")
    assert build_structured_references(sources)[0]["file"]["role"] == "reference_pdf"


def test_reference_pdf_rejects_non_markdown_and_invalid_pdf():
    test_client = next(client())
    kb_id = _knowledge_base(test_client, f"ingest-evidence-invalid-{uuid.uuid4().hex[:8]}")
    secret = _token(test_client, [kb_id])
    route = f"/api/v1/ingest/knowledge-bases/{kb_id}/documents"
    non_md = test_client.post(route, headers=_headers(secret), files={
        "file": ("notes.txt", b"notes", "text/plain"),
        "reference_pdf": ("evidence.pdf", b"%PDF-1.4\nevidence", "application/pdf")})
    assert non_md.status_code == 400
    assert non_md.json()["error"]["code"] == "REFERENCE_PDF_REQUIRES_MARKDOWN"
    invalid = test_client.post(route, headers=_headers(secret), files={
        "file": ("notes.md", b"notes", "text/markdown"),
        "reference_pdf": ("fake.pdf", b"not a pdf", "application/pdf")})
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "REFERENCE_PDF_INVALID"
    unsupported = test_client.post(route, headers=_headers(secret), files={
        "file": ("notes.md", b"notes", "text/markdown"),
        "reference_file": ("script.exe", b"unsafe", "application/octet-stream")})
    assert unsupported.status_code == 400
    assert unsupported.json()["error"]["code"] == "REFERENCE_FILE_TYPE_NOT_SUPPORTED"
    conflict = test_client.post(route, headers=_headers(secret), files={
        "file": ("notes.md", b"notes", "text/markdown"),
        "reference_file": ("source.txt", b"source", "text/plain"),
        "reference_pdf": ("source.pdf", b"%PDF-1.4\nevidence", "application/pdf")})
    assert conflict.status_code == 400
    assert conflict.json()["error"]["code"] == "REFERENCE_FILE_CONFLICT"
    with SessionLocal() as db:
        assert db.query(Document).filter_by(knowledge_base_id=kb_id).count() == 0


def test_token_without_ingest_scope_cannot_write():
    test_client = next(client())
    kb_id = _knowledge_base(test_client, "ingest-api-scope")
    # A token with no tools used to mean "every tool" before the wildcard was
    # removed; create_token() now rejects that combination outright (it would
    # otherwise mint a credential with zero capability), so only a token that
    # is granted an MCP tool can even be issued here, and it still may not
    # reach the ingest surface without documents:write.
    response = test_client.post("/api/v1/tokens", json={
        "name": "no-capability", "allowed_knowledge_base_ids": [kb_id], "allowed_tools": [], "allowed_scopes": []})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "TOKEN_NO_CAPABILITY"
    secret = _token(test_client, [kb_id], scopes=(), tools=["search_knowledge"])
    response = _upload(test_client, secret, kb_id)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUTH_SCOPE_NOT_ALLOWED"
    documents = test_client.get(f"/api/v1/knowledge-bases/{kb_id}/documents").json()
    assert documents == []


def test_ingest_token_requires_explicit_knowledge_base_scope():
    test_client = next(client())
    _knowledge_base(test_client, "ingest-api-unscoped")
    # documents:write with no ingest Knowledge Base is now rejected at issue
    # time, not at upload time, since the field is required whenever the scope
    # is requested.
    response = test_client.post("/api/v1/tokens", json={
        "name": "unscoped", "allowed_knowledge_base_ids": [], "allowed_scopes": [INGEST_SCOPE]})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INGEST_KNOWLEDGE_BASE_REQUIRED"


def test_ingest_write_scope_is_independent_of_mcp_read_kb_list():
    test_client = next(client())
    mcp_kb = _knowledge_base(test_client, "ingest-api-mcp-axis")
    write_kb = _knowledge_base(test_client, "ingest-api-write-axis")
    # A write-only token (no allowed_tools) has its allowed_knowledge_base_ids
    # (the MCP read axis) force-cleared at issue time, since authorize() would
    # never reach that list without a granted tool. Passing extra ids here must
    # not leak into ingest scope, which stays pinned to exactly one Knowledge Base.
    secret = _token(test_client, [mcp_kb, write_kb], allowed_ingest_kb_id=write_kb)
    assert _upload(test_client, secret, write_kb).status_code == 202
    denied = _upload(test_client, secret, mcp_kb)
    assert denied.status_code == 404
    called = test_client.post("/mcp", headers=_headers(secret), json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "search_knowledge", "arguments": {"query": "anything"}}})
    assert called.json()["error"]["code"] == "AUTH_TOOL_NOT_ALLOWED"


def test_token_cannot_have_both_mcp_tools_and_ingest_scope():
    test_client = next(client())
    kb_id = _knowledge_base(test_client, "ingest-api-capability-conflict")
    kb_ids = [kb_id]
    payload = {"name": "mixed", "allowed_knowledge_base_ids": kb_ids, "allowed_tools": ["search_knowledge"],
               "allowed_scopes": [INGEST_SCOPE], "allowed_ingest_knowledge_base_id": kb_id}
    response = test_client.post("/api/v1/tokens", json=payload)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "TOKEN_CAPABILITY_CONFLICT"


def test_ingest_knowledge_base_must_be_active_and_cannot_be_set_without_scope():
    test_client = next(client())
    kb_id = _knowledge_base(test_client, "ingest-api-inactive-target")
    assert test_client.post(f"/api/v1/knowledge-bases/{kb_id}/disable").status_code == 200
    inactive = test_client.post("/api/v1/tokens", json={
        "name": "inactive-target", "allowed_knowledge_base_ids": [], "allowed_scopes": [INGEST_SCOPE],
        "allowed_ingest_knowledge_base_id": kb_id})
    assert inactive.status_code == 400
    assert inactive.json()["error"]["code"] == "KNOWLEDGE_BASE_INACTIVE"

    other_kb = _knowledge_base(test_client, "ingest-api-no-scope-target")
    no_scope = test_client.post("/api/v1/tokens", json={
        "name": "no-scope", "allowed_knowledge_base_ids": [], "allowed_scopes": [],
        "allowed_ingest_knowledge_base_id": other_kb})
    assert no_scope.status_code == 400
    assert no_scope.json()["error"]["code"] == "INGEST_KNOWLEDGE_BASE_NOT_ALLOWED"


def test_ingest_uploads_and_reports_completion():
    test_client = next(client())
    kb_id = _knowledge_base(test_client, "ingest-api-happy")
    secret = _token(test_client, [kb_id])
    response = _upload(test_client, secret, kb_id, "architecture.txt")
    assert response.status_code == 202
    queued = response.json()
    assert queued["status"] == "queued" and queued["document_type"] == "general"

    pending = test_client.get(f"/api/v1/ingest/documents/{queued['document_id']}", headers=_headers(secret)).json()
    assert pending["status"] == "queued" and pending["latest_job"]["status"] == "queued"
    _drain(test_client)

    done = test_client.get(f"/api/v1/ingest/documents/{queued['document_id']}", headers=_headers(secret)).json()
    assert done["status"] == "completed" and done["knowledge_base_id"] == kb_id
    assert done["latest_job"]["progress_percent"] == 100
    jobs = test_client.get(f"/api/v1/ingest/documents/{queued['document_id']}/jobs", headers=_headers(secret)).json()
    assert any(job["id"] == queued["job_id"] and job["status"] == "completed" for job in jobs)
    listed = test_client.get(f"/api/v1/ingest/knowledge-bases/{kb_id}/documents?status=completed", headers=_headers(secret)).json()
    assert listed["total"] == 1 and listed["items"][0]["document_id"] == queued["document_id"]


def test_ingest_token_cannot_reach_another_knowledge_base():
    test_client = next(client())
    granted = _knowledge_base(test_client, "ingest-api-granted")
    other = _knowledge_base(test_client, "ingest-api-other")
    secret = _token(test_client, [granted])
    assert _upload(test_client, secret, other).status_code == 404

    other_secret = _token(test_client, [other])
    foreign = _upload(test_client, other_secret, other, "foreign.txt", b"Foreign knowledge.").json()
    leaked = test_client.get(f"/api/v1/ingest/documents/{foreign['document_id']}", headers=_headers(secret))
    assert leaked.status_code == 404
    assert leaked.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"
    assert test_client.get(f"/api/v1/ingest/documents/{foreign['document_id']}/jobs", headers=_headers(secret)).status_code == 404
    assert test_client.delete(f"/api/v1/ingest/documents/{foreign['document_id']}", headers=_headers(secret)).status_code == 404


def test_ingest_token_soft_deletes_its_own_document_and_queues_index_purge():
    test_client = next(client())
    kb_id = _knowledge_base(test_client, "ingest-api-delete")
    secret = _token(test_client, [kb_id])
    uploaded = _upload(test_client, secret, kb_id, "remove-me.txt", b"Remove this source.").json()

    deleted = test_client.delete(f"/api/v1/ingest/documents/{uploaded['document_id']}", headers=_headers(secret))
    assert deleted.status_code == 200
    body = deleted.json()
    assert body["status"] == "deleted" and body["document_id"] == uploaded["document_id"]
    assert body["purge_job_id"]

    assert test_client.get(f"/api/v1/ingest/documents/{uploaded['document_id']}", headers=_headers(secret)).status_code == 404
    listed = test_client.get(f"/api/v1/ingest/knowledge-bases/{kb_id}/documents", headers=_headers(secret)).json()
    assert listed["total"] == 0 and listed["items"] == []
    audit = test_client.get("/api/v1/audit-logs?limit=100").json()
    entry = next(row for row in audit if row["action"] == "document.delete" and row["target_id"] == uploaded["document_id"])
    assert entry["metadata"]["transport"] == "ingest_api" and entry["metadata"]["token_name"] == "ingest-agent"


def test_ingest_batch_isolates_per_file_failures():
    test_client = next(client())
    kb_id = _knowledge_base(test_client, "ingest-api-batch")
    secret = _token(test_client, [kb_id])
    response = test_client.post(f"/api/v1/ingest/knowledge-bases/{kb_id}/documents/batch", headers=_headers(secret), files=[
        ("files", ("one.txt", b"First document.", "text/plain")),
        ("files", ("two.md", b"# Second document", "text/markdown")),
        ("files", ("three.exe", b"binary", "application/octet-stream")),
    ])
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "partial" and body["queued_count"] == 2 and body["failed_count"] == 1
    rejected = next(item for item in body["results"] if item["filename"] == "three.exe")
    assert rejected["error_code"] == "FILE_TYPE_NOT_SUPPORTED"

    too_many = test_client.post(f"/api/v1/ingest/knowledge-bases/{kb_id}/documents/batch", headers=_headers(secret),
                                files=[("files", (f"file-{index}.txt", b"body", "text/plain")) for index in range(21)])
    assert too_many.status_code == 400
    assert too_many.json()["error"]["code"] == "BATCH_TOO_MANY_FILES"


def test_ingest_rejects_duplicate_and_unsupported_files():
    test_client = next(client())
    kb_id = _knowledge_base(test_client, "ingest-api-rejects")
    secret = _token(test_client, [kb_id])
    assert _upload(test_client, secret, kb_id, "same.txt", b"Identical body.").status_code == 202
    duplicate = _upload(test_client, secret, kb_id, "same.txt", b"Identical body.")
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "FILE_DUPLICATE"
    unsupported = _upload(test_client, secret, kb_id, "payload.exe", b"binary")
    assert unsupported.status_code == 400
    assert unsupported.json()["error"]["code"] == "FILE_TYPE_NOT_SUPPORTED"
    rejections = test_client.get("/api/v1/audit-logs?limit=100").json()
    assert any(row["action"] == "document.ingest.rejected" and row["metadata"].get("error_code") == "FILE_DUPLICATE" for row in rejections)


def test_ingest_rejects_disabled_knowledge_base():
    test_client = next(client())
    kb_id = _knowledge_base(test_client, "ingest-api-disabled")
    secret = _token(test_client, [kb_id])
    assert test_client.post(f"/api/v1/knowledge-bases/{kb_id}/disable").status_code == 200
    response = _upload(test_client, secret, kb_id)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "KNOWLEDGE_BASE_DISABLED"


def test_ingest_lists_only_its_own_knowledge_base():
    test_client = next(client())
    kb_id = _knowledge_base(test_client, "ingest-api-kb-list")
    other_kb_id = _knowledge_base(test_client, "ingest-api-kb-list-other")
    secret = _token(test_client, [kb_id])
    listed = test_client.get("/api/v1/ingest/knowledge-bases", headers=_headers(secret)).json()
    assert listed == {"items": [{"id": kb_id, "code": "ingest-api-kb-list", "name": "ingest-api-kb-list", "status": "active"}]}
    assert other_kb_id not in [item["id"] for item in listed["items"]]

    # A disabled KB is still reported (with its status) rather than hidden, so a
    # client can tell "nothing configured" apart from "configured but paused".
    assert test_client.post(f"/api/v1/knowledge-bases/{kb_id}/disable").status_code == 200
    disabled_listing = test_client.get("/api/v1/ingest/knowledge-bases", headers=_headers(secret)).json()
    assert disabled_listing["items"][0]["status"] == "disabled"


def test_ingest_lists_document_types_and_accepts_document_type_id():
    test_client = next(client())
    kb_id = _knowledge_base(test_client, "ingest-api-document-types")
    other_kb_id = _knowledge_base(test_client, "ingest-api-document-types-other")
    secret = _token(test_client, [kb_id])
    created = test_client.post(f"/api/v1/knowledge-bases/{kb_id}/document-templates", json={
        "name": "Supplier invoice", "code": "supplier-invoice", "fields": [{"key": "invoice_no", "label": "Invoice number"}],
    }).json()

    listed = test_client.get(f"/api/v1/ingest/knowledge-bases/{kb_id}/document-types", headers=_headers(secret))
    assert listed.status_code == 200
    item = next(row for row in listed.json()["items"] if row["id"] == created["id"])
    assert item["name"] == "Supplier invoice" and item["fields"][0]["key"] == "invoice_no"
    assert test_client.get(f"/api/v1/ingest/knowledge-bases/{other_kb_id}/document-types", headers=_headers(secret)).status_code == 404

    uploaded = test_client.post(f"/api/v1/ingest/knowledge-bases/{kb_id}/documents", headers=_headers(secret),
                                files={"file": ("invoice.txt", b"Invoice INV-2026-001", "text/plain")},
                                data={"document_type_id": created["id"], "metadata_json": '{"invoice_no":"INV-2026-001"}'}).json()
    assert uploaded["document_type_id"] == created["id"] and uploaded["template_id"] == created["id"]
    view = test_client.get(f"/api/v1/ingest/documents/{uploaded['document_id']}", headers=_headers(secret)).json()
    assert view["document_type_id"] == created["id"]

    conflict = test_client.post(f"/api/v1/ingest/knowledge-bases/{kb_id}/documents", headers=_headers(secret),
                                files={"file": ("conflict.txt", b"Conflict", "text/plain")},
                                data={"document_type_id": created["id"], "template_id": "system:general"})
    assert conflict.status_code == 400 and conflict.json()["error"]["code"] == "DOCUMENT_TYPE_ID_CONFLICT"


def test_ingest_rejects_invalid_credentials():
    test_client = next(client())
    kb_id = _knowledge_base(test_client, "ingest-api-credentials")
    missing = test_client.post(f"/api/v1/ingest/knowledge-bases/{kb_id}/documents", files={"file": ("a.txt", b"body", "text/plain")})
    assert missing.status_code == 401 and missing.json()["error"]["code"] == "AUTH_TOKEN_MISSING"
    assert _upload(test_client, "skik_live_not-a-real-token", kb_id).json()["error"]["code"] == "AUTH_TOKEN_INVALID"

    revoked = test_client.post("/api/v1/tokens", json={
        "name": "revoked", "allowed_knowledge_base_ids": [kb_id], "allowed_scopes": [INGEST_SCOPE],
        "allowed_ingest_knowledge_base_id": kb_id}).json()
    assert test_client.post(f"/api/v1/tokens/{revoked['id']}/revoke").status_code == 200
    assert _upload(test_client, revoked["token"], kb_id).json()["error"]["code"] == "AUTH_TOKEN_REVOKED"

    disabled = test_client.post("/api/v1/tokens", json={
        "name": "disabled", "allowed_knowledge_base_ids": [kb_id], "allowed_scopes": [INGEST_SCOPE],
        "allowed_ingest_knowledge_base_id": kb_id}).json()
    assert test_client.post(f"/api/v1/tokens/{disabled['id']}/disable").status_code == 200
    assert _upload(test_client, disabled["token"], kb_id).json()["error"]["code"] == "AUTH_TOKEN_INVALID"


def test_write_only_token_cannot_call_mcp_tools():
    test_client = next(client())
    kb_id = _knowledge_base(test_client, "ingest-api-writeonly")
    secret = _token(test_client, [kb_id])
    listed = test_client.post("/mcp", headers=_headers(secret), json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()
    assert listed["result"]["tools"] == []
    called = test_client.post("/mcp", headers=_headers(secret), json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "search_knowledge", "arguments": {"query": "anything"}}})
    assert called.json()["error"]["code"] == "AUTH_TOOL_NOT_ALLOWED"


def test_rotation_preserves_ingest_scope_and_retires_old_secret():
    test_client = next(client())
    kb_id = _knowledge_base(test_client, "ingest-api-rotate")
    created = test_client.post("/api/v1/tokens", json={
        "name": "rotating", "allowed_knowledge_base_ids": [kb_id], "allowed_scopes": [INGEST_SCOPE],
        "allowed_ingest_knowledge_base_id": kb_id}).json()
    rotated = test_client.post(f"/api/v1/tokens/{created['id']}/rotate").json()
    assert rotated["allowed_scopes"] == [INGEST_SCOPE]
    assert rotated["allowed_ingest_knowledge_base_id"] == kb_id
    assert _upload(test_client, rotated["token"], kb_id, "rotated.txt", b"After rotation.").status_code == 202
    assert _upload(test_client, created["token"], kb_id).json()["error"]["code"] == "AUTH_TOKEN_REVOKED"


def test_unknown_tool_or_scope_is_rejected_at_issue_time():
    test_client = next(client())
    kb_id = _knowledge_base(test_client, "ingest-api-validation")
    bad_tool = test_client.post("/api/v1/tokens", json={"name": "typo", "allowed_knowledge_base_ids": [kb_id], "allowed_tools": ["serch_knowledge"]})
    assert bad_tool.status_code == 400 and bad_tool.json()["error"]["code"] == "TOKEN_TOOL_UNKNOWN"
    bad_scope = test_client.post("/api/v1/tokens", json={"name": "typo", "allowed_knowledge_base_ids": [kb_id], "allowed_scopes": ["documents:admin"]})
    assert bad_scope.status_code == 400 and bad_scope.json()["error"]["code"] == "TOKEN_SCOPE_UNKNOWN"


def test_ingest_rate_limit_is_charged_per_token():
    test_client = next(client())
    kb_id = _knowledge_base(test_client, "ingest-api-ratelimit")
    secret = _token(test_client, [kb_id], requests_per_minute=1)
    assert _upload(test_client, secret, kb_id, "first.txt", b"First body.").status_code == 202
    limited = _upload(test_client, secret, kb_id, "second.txt", b"Second body.")
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "MCP_RATE_LIMITED"
    assert limited.json()["error"]["retryable"] is True


def test_ingest_observability_records_attribution_without_the_secret():
    test_client = next(client())
    kb_id = _knowledge_base(test_client, "ingest-api-observability")
    secret = _token(test_client, [kb_id])
    uploaded = _upload(test_client, secret, kb_id, "audited.txt", b"Audited body.").json()

    audit = test_client.get("/api/v1/audit-logs?limit=100").json()
    entry = next(row for row in audit if row["action"] == "document.upload" and row["target_id"] == uploaded["document_id"])
    assert entry["metadata"]["transport"] == "ingest_api" and entry["metadata"]["token_name"] == "ingest-agent"
    assert secret not in str(audit)

    transactions = test_client.get("/api/v1/logs/transactions?limit=100").json()
    ingest_transaction = next(row for row in transactions if row["path"].startswith("/api/v1/ingest"))
    assert ingest_transaction["authentication"] == "ingest_token"
    assert secret not in str(transactions)
