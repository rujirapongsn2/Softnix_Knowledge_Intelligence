import os
import tempfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

_TEST_ROOT = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_ROOT}/skip.db"
os.environ["FILE_STORAGE_PATH"] = f"{_TEST_ROOT}/files"
os.environ["INITIAL_ADMIN_PASSWORD"] = "correct-horse-battery-staple"
os.environ["LIGHTRAG_BASE_URL"] = ""
os.environ["REDIS_URL"] = ""
os.environ["OPENROUTER_API_KEY"] = ""
os.environ["EXT_OCR_KEY"] = ""

from fastapi.testclient import TestClient
import httpx
from openpyxl import Workbook
from pptx import Presentation
from app import services
from app.config import Settings
from app.external_ocr import ExternalOcrClient
from app.main import app


def client():
    with TestClient(app) as test_client:
        assert test_client.post("/api/v1/auth/login", json={"username": "admin", "password": "correct-horse-battery-staple"}).status_code == 200
        yield test_client


def test_vertical_slice():
    test_client = next(client())
    kb = test_client.post("/api/v1/knowledge-bases", json={"name": "IT", "code": "enterprise-it"}).json()
    test_client.post(f"/api/v1/knowledge-bases/{kb['id']}/activate")
    uploaded = test_client.post(f"/api/v1/knowledge-bases/{kb['id']}/documents", files={"file": ("architecture.txt", b"Customer Portal runs on APP-01.", "text/plain")}).json()
    assert uploaded["status"] == "queued"
    assert test_client.post("/api/v1/internal/process-next").json()["processed"] is True
    assert test_client.get(f"/api/v1/documents/{uploaded['document_id']}/text").json()["status"] == "completed"
    token = test_client.post("/api/v1/tokens", json={"name": "agent", "allowed_knowledge_base_ids": [kb["id"]], "allowed_tools": ["search_knowledge"]}).json()
    reply = test_client.post("/mcp", headers={"Authorization": f"Bearer {token['token']}"}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "search_knowledge", "arguments": {"query": "What runs on APP-01?"}}})
    assert reply.status_code == 200
    assert reply.json()["result"]["structuredContent"]["sources"]
    assert reply.json()["result"]["structuredContent"]["request_id"] == 1


def test_token_is_not_returned_after_creation():
    test_client = next(client())
    token = test_client.post("/api/v1/tokens", json={"name": "agent"}).json()
    assert token["token"].startswith("skik_live_")
    listed = test_client.post("/mcp", headers={"Authorization": f"Bearer {token['token']}"}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()
    assert "tools" in listed["result"]


def test_refresh_cookie_restores_access_session():
    test_client = next(client())
    assert test_client.cookies.get("skip_refresh")
    test_client.cookies.delete("skip_access")
    refreshed = test_client.post("/api/v1/auth/refresh")
    assert refreshed.status_code == 200
    assert test_client.cookies.get("skip_access")
    assert test_client.get("/api/v1/auth/me").status_code == 200


def test_revoked_token_cannot_call_mcp():
    test_client = next(client())
    token = test_client.post("/api/v1/tokens", json={"name": "agent"}).json()
    assert test_client.post(f"/api/v1/tokens/{token['id']}/revoke").status_code == 200
    response = test_client.post("/mcp", headers={"Authorization": f"Bearer {token['token']}"}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 200
    assert response.json()["error"]["code"] == "AUTH_TOKEN_REVOKED"


def test_graph_impact_returns_direct_indirect_impacts_and_sources():
    test_client = next(client())
    kb = test_client.post("/api/v1/knowledge-bases", json={"name": "Graph KB", "code": "graph-kb"}).json()
    uploaded = test_client.post(f"/api/v1/knowledge-bases/{kb['id']}/documents", files={"file": ("architecture.txt", b"Architecture evidence", "text/plain")}).json()
    test_client.post("/api/v1/internal/process-next")
    app = test_client.post(f"/api/v1/knowledge-bases/{kb['id']}/entities", json={"name": "APP-01", "entity_type": "Application", "document_id": uploaded["document_id"], "excerpt": "APP-01 hosts the portal."}).json()
    portal = test_client.post(f"/api/v1/knowledge-bases/{kb['id']}/entities", json={"name": "Customer Portal", "entity_type": "Application"}).json()
    process = test_client.post(f"/api/v1/knowledge-bases/{kb['id']}/entities", json={"name": "Customer Service", "entity_type": "BusinessProcess"}).json()
    test_client.post(f"/api/v1/knowledge-bases/{kb['id']}/relationships", json={"source_entity_id": portal["id"], "target_entity_id": app["id"], "relationship_type": "RUNS_ON", "document_id": uploaded["document_id"], "excerpt": "Customer Portal runs on APP-01."})
    test_client.post(f"/api/v1/knowledge-bases/{kb['id']}/relationships", json={"source_entity_id": process["id"], "target_entity_id": portal["id"], "relationship_type": "USES", "document_id": uploaded["document_id"], "excerpt": "Customer Service uses Customer Portal."})
    result = test_client.post("/api/v1/query/impact", json={"subject": "APP-01", "scenario": "stops", "knowledge_base_ids": [kb["id"]], "max_depth": 2}).json()
    assert result["subject"]["name"] == "APP-01"
    assert [item["name"] for item in result["direct_impacts"]] == ["Customer Portal"]
    assert [item["name"] for item in result["indirect_impacts"]] == ["Customer Service"]
    assert result["sources"][0]["citation_id"] == "S1"
    assert test_client.get("/api/v1/system/graph-projection").json()["events"]["queued"] >= 3


def test_document_list_preview_and_reprocess():
    test_client = next(client())
    kb = test_client.post("/api/v1/knowledge-bases", json={"name": "Docs", "code": "docs-kb"}).json()
    uploaded = test_client.post(f"/api/v1/knowledge-bases/{kb['id']}/documents", files={"file": ("notes.txt", b"The previewable document text.", "text/plain")}).json()
    assert len(test_client.get(f"/api/v1/knowledge-bases/{kb['id']}/documents").json()) == 1
    test_client.post("/api/v1/internal/process-next")
    assert "previewable" in test_client.get(f"/api/v1/documents/{uploaded['document_id']}/text").json()["text"]
    reprocess = test_client.post(f"/api/v1/documents/{uploaded['document_id']}/reprocess").json()
    assert reprocess["status"] == "queued"
    assert len(test_client.get(f"/api/v1/documents/{uploaded['document_id']}/jobs").json()) == 2


def test_markitdown_extracts_structured_office_markdown_and_legacy_fallback(tmp_path, monkeypatch):
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[5])
    slide.shapes.title.text = "Platform Architecture"
    text_box = slide.shapes.add_textbox(100, 100, 1000, 500)
    text_box.text_frame.text = "APP-01 runs the customer portal."
    pptx_path = tmp_path / "architecture.pptx"
    presentation.save(pptx_path)
    pptx_markdown = services.extract_text(SimpleNamespace(storage_path=str(pptx_path)))
    assert "# Platform Architecture" in pptx_markdown
    assert "APP-01 runs the customer portal." in pptx_markdown

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Systems"
    worksheet.append(["System", "Owner"])
    worksheet.append(["APP-01", "Platform Team"])
    xlsx_path = tmp_path / "systems.xlsx"
    workbook.save(xlsx_path)
    xlsx_markdown = services.extract_text(SimpleNamespace(storage_path=str(xlsx_path)))
    assert "## Systems" in xlsx_markdown and "| APP-01 | Platform Team |" in xlsx_markdown

    text_path = tmp_path / "fallback.txt"
    text_path.write_text("Legacy parser keeps this extraction available.", encoding="utf-8")
    monkeypatch.setattr(services, "_markitdown_extract", lambda _: (_ for _ in ()).throw(RuntimeError("converter failed")))
    assert "Legacy parser" in services.extract_text(SimpleNamespace(storage_path=str(text_path)))

    scanned_pdf = tmp_path / "scanned.pdf"
    scanned_pdf.write_bytes(b"not a text-layer PDF")
    monkeypatch.setattr(services, "_markitdown_extract", lambda _: "")
    try:
        services.extract_text(SimpleNamespace(storage_path=str(scanned_pdf)))
    except RuntimeError as error:
        assert str(error) == "OCR_REQUIRED"
    else:
        raise AssertionError("scanned PDFs must require OCR")


def test_external_ocr_v3_returns_markdown_and_reports_progress():
    calls, progress = [], []

    def handler(request: httpx.Request):
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            assert b'disable_structure' in request.content
            return httpx.Response(200, json={"job_id": "ocr-job-1"})
        if request.url.path.endswith("/status") and len([call for call in calls if call[1].endswith("/status")]) == 1:
            return httpx.Response(200, json={"status": "processing", "progress": {"percent": 50, "stage": "ocr"}})
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={"status": "completed"})
        if request.url.path.endswith("/result"):
            return httpx.Response(200, json={"results": {"combined_markdown": "# Scanned policy\n\nArticle 1"}})
        raise AssertionError(request.url.path)

    settings = Settings(ext_ocr_key="test-key", ext_ocr_base_url="https://ocr.test", ext_ocr_poll_interval_seconds=0)
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://ocr.test")
    result = ExternalOcrClient(settings, client).extract_markdown(Path(__file__), lambda stage, percent: progress.append((stage, percent)))
    assert result == "# Scanned policy\n\nArticle 1"
    assert progress[0] == ("external_ocr_queued", 15)
    assert any(stage == "external_ocr_ocr" for stage, _ in progress)


def test_pptx_and_xlsx_uploads_are_processed_as_markdown():
    test_client = next(client())
    kb = test_client.post("/api/v1/knowledge-bases", json={"name": "Office", "code": "office-kb"}).json()
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[5])
    slide.shapes.title.text = "Service Catalog"
    pptx_stream = BytesIO(); presentation.save(pptx_stream)
    pptx = test_client.post(f"/api/v1/knowledge-bases/{kb['id']}/documents", files={"file": ("catalog.pptx", pptx_stream.getvalue(), "application/vnd.openxmlformats-officedocument.presentationml.presentation")}).json()
    for _ in range(10):
        test_client.post("/api/v1/internal/process-next")
        pptx_preview = test_client.get(f"/api/v1/documents/{pptx['document_id']}/text").json()
        if pptx_preview["status"] != "queued":
            break
    assert pptx_preview["status"] == "completed", pptx_preview
    assert "# Service Catalog" in pptx_preview["text"]

    workbook = Workbook(); worksheet = workbook.active; worksheet.title = "Inventory"; worksheet.append(["System", "Status"]); worksheet.append(["APP-01", "Active"])
    xlsx_stream = BytesIO(); workbook.save(xlsx_stream)
    xlsx = test_client.post(f"/api/v1/knowledge-bases/{kb['id']}/documents", files={"file": ("inventory.xlsx", xlsx_stream.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}).json()
    for _ in range(10):
        test_client.post("/api/v1/internal/process-next")
        xlsx_preview = test_client.get(f"/api/v1/documents/{xlsx['document_id']}/text").json()
        if xlsx_preview["status"] != "queued":
            break
    assert xlsx_preview["status"] == "completed", xlsx_preview
    assert "| APP-01 | Active |" in xlsx_preview["text"]


def test_upload_rejects_mime_type_that_does_not_match_extension():
    test_client = next(client())
    kb = test_client.post("/api/v1/knowledge-bases", json={"name": "Mime", "code": "mime-kb"}).json()
    response = test_client.post(f"/api/v1/knowledge-bases/{kb['id']}/documents", files={"file": ("unsafe.xlsx", b"not-an-excel-file", "text/plain")})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "FILE_MIME_TYPE_NOT_SUPPORTED"


def test_legal_metadata_articles_amendments_crud():
    test_client = next(client())
    kb = test_client.post("/api/v1/knowledge-bases", json={"name": "Legal", "code": "legal-kb"}).json()
    uploaded = test_client.post(f"/api/v1/knowledge-bases/{kb['id']}/documents", files={"file": ("law.txt", "มาตรา 1 applies.".encode(), "text/plain")}).json()
    test_client.post("/api/v1/internal/process-next")
    metadata = {"document_type": "ประกาศ", "articles": [{"article_number": "1", "text": "applies", "evidence_quote": "มาตรา 1 applies"}], "amendments": [{"title": "ประกาศแก้ไข", "announcement_number": "ฉบับที่ 2", "changes": "แก้ไขมาตรา 1", "evidence_quote": "ฉบับที่ 2"}]}
    saved = test_client.put(f"/api/v1/documents/{uploaded['document_id']}/legal-metadata", json={"metadata": metadata})
    assert saved.status_code == 200 and saved.json()["legal_metadata"]["articles"][0]["article_number"] == "1"
    patched = test_client.patch(f"/api/v1/documents/{uploaded['document_id']}/legal-metadata", json={"metadata": {"confidence": 0.9}})
    assert patched.json()["legal_metadata"]["confidence"] == 0.9
    assert test_client.get(f"/api/v1/documents/{uploaded['document_id']}/text").json()["legal_metadata"]["amendments"][0]["title"] == "ประกาศแก้ไข"
    assert test_client.delete(f"/api/v1/documents/{uploaded['document_id']}/legal-metadata").json()["status"] == "deleted"
    assert test_client.get(f"/api/v1/documents/{uploaded['document_id']}/text").json()["legal_metadata"] is None


def test_database_chunk_retrieval_is_scoped_to_knowledge_base():
    test_client = next(client())
    first = test_client.post("/api/v1/knowledge-bases", json={"name": "First", "code": "first-kb"}).json()
    second = test_client.post("/api/v1/knowledge-bases", json={"name": "Second", "code": "second-kb"}).json()
    test_client.post(f"/api/v1/knowledge-bases/{first['id']}/documents", files={"file": ("first.txt", b"APP-01 owns a unique primary platform.", "text/plain")})
    test_client.post(f"/api/v1/knowledge-bases/{second['id']}/documents", files={"file": ("second.txt", b"APP-01 owns a unique secondary platform.", "text/plain")})
    assert test_client.post("/api/v1/internal/process-next").json()["processed"] is True
    assert test_client.post("/api/v1/internal/process-next").json()["processed"] is True
    result = test_client.post("/api/v1/query", json={"knowledge_base_ids": [first["id"]], "query": "unique platform"}).json()
    assert result["sources"]
    assert all(source["document_id"] != "" for source in result["sources"])
    assert all("primary" in source["excerpt"] for source in result["sources"])


def test_embedding_reindex_queues_completed_documents_without_full_reprocessing():
    test_client = next(client())
    kb = test_client.post("/api/v1/knowledge-bases", json={"name": "Reindex", "code": "reindex-kb"}).json()
    uploaded = test_client.post(f"/api/v1/knowledge-bases/{kb['id']}/documents", files={"file": ("notes.txt", b"Reindex this document.", "text/plain")}).json()
    for _ in range(10):
        test_client.post("/api/v1/internal/process-next")
        if test_client.get(f"/api/v1/documents/{uploaded['document_id']}/text").json()["status"] == "completed":
            break
    queued = test_client.post(f"/api/v1/knowledge-bases/{kb['id']}/documents/reindex?force=true").json()
    assert queued == {"status": "queued", "count": 1}
    for _ in range(10):
        test_client.post("/api/v1/internal/process-next")
        jobs = test_client.get(f"/api/v1/documents/{uploaded['document_id']}/jobs").json()
        if jobs[0]["status"] == "completed":
            break
    jobs = test_client.get(f"/api/v1/documents/{uploaded['document_id']}/jobs").json()
    assert jobs[0]["status"] == "completed"
    assert jobs[0]["stage"] == "completed"


def test_metrics_and_audit_log_are_exposed():
    test_client = next(client())
    kb = test_client.post("/api/v1/knowledge-bases", json={"name": "Audit", "code": "audit-kb"}).json()
    audit = test_client.get("/api/v1/audit-logs").json()
    assert any(row["action"] == "knowledge_base.create" and row["target_id"] == kb["id"] for row in audit)
    metrics = test_client.get("/metrics")
    assert metrics.status_code == 200
    assert "softnix_http_requests_total" in metrics.text


def test_document_restore_graph_layout_and_feedback_lifecycle():
    test_client = next(client())
    kb = test_client.post("/api/v1/knowledge-bases", json={"name": "Lifecycle", "code": "lifecycle-kb"}).json()
    uploaded = test_client.post(f"/api/v1/knowledge-bases/{kb['id']}/documents", files={"file": ("notes.txt", b"Lifecycle evidence for APP-02.", "text/plain")}).json()
    document_id = uploaded["document_id"]
    assert test_client.delete(f"/api/v1/documents/{document_id}").json()["status"] == "deleted"
    assert test_client.get(f"/api/v1/knowledge-bases/{kb['id']}/documents").json() == []
    assert test_client.post(f"/api/v1/documents/{document_id}/restore").json()["status"] == "queued"
    assert test_client.get(f"/api/v1/knowledge-bases/{kb['id']}/documents?include_deleted=true").json()[0]["deleted_at"] is None
    entity = test_client.post(f"/api/v1/knowledge-bases/{kb['id']}/entities", json={"name": "APP-02", "entity_type": "Application"}).json()
    assert test_client.put(f"/api/v1/knowledge-bases/{kb['id']}/graph-layout", json={"items": [{"entity_id": entity["id"], "x": 120, "y": 80}]}).json()["status"] == "success"
    assert test_client.get(f"/api/v1/knowledge-bases/{kb['id']}/graph-layout").json()["items"][0]["x"] == 120
    assert test_client.patch(f"/api/v1/entities/{entity['id']}", json={"name": "APP-02 API"}).json()["name"] == "APP-02 API"
    test_client.post("/api/v1/internal/process-next")
    result = test_client.post("/api/v1/query", json={"knowledge_base_ids": [kb["id"]], "query": "APP-02"}).json()
    feedback = test_client.post(f"/api/v1/query/results/{result['result_id']}/feedback", json={"rating": 1, "comment": "useful"})
    assert feedback.status_code == 200
    assert feedback.json()["status"] == "success"


def test_mcp_rate_limit_returns_jsonrpc_error():
    test_client = next(client())
    token = test_client.post("/api/v1/tokens", json={"name": "limited", "requests_per_minute": 1}).json()
    request = {"jsonrpc": "2.0", "id": 7, "method": "tools/list"}
    assert test_client.post("/mcp", headers={"Authorization": f"Bearer {token['token']}"}, json=request).json()["result"]["tools"]
    response = test_client.post("/mcp", headers={"Authorization": f"Bearer {token['token']}"}, json=request)
    assert response.status_code == 200
    assert response.json()["error"]["code"] == "MCP_RATE_LIMITED"
