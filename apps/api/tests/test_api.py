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
from app.db import SessionLocal
from app.external_ocr import ExternalOcrClient
from app.main import app
from app.models import Document, DocumentChunk


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
    assert reply.json()["result"]["structuredContent"]["metadata"]["retrieval_plan"]["intent"] == "entity_lookup"
    activity = test_client.get("/api/v1/mcp/activity").json()
    call = next(row for row in activity if row["metadata"].get("tool") == "search_knowledge")
    assert call["metadata"]["query"] == "What runs on APP-01?"
    assert any(step["channel"] == "full_text" and step["status"] == "used" for step in call["metadata"]["route"])
    transactions = test_client.get("/api/v1/logs/transactions?limit=100").json()
    mcp_transaction = next(row for row in transactions if row["path"] == "/mcp" and row["retrieval"])
    assert mcp_transaction["retrieval"]["transport"] == "mcp"
    assert mcp_transaction["retrieval"]["retrieval_plan"]["intent"] == "entity_lookup"
    assert any(step["channel"] == "full_text" for step in mcp_transaction["retrieval"]["retrieval_trace"])


def test_knowledge_base_can_be_disabled_activated_and_safely_deleted():
    test_client = next(client())
    empty = test_client.post("/api/v1/knowledge-bases", json={"name": "Disposable", "code": "disposable-kb"}).json()
    assert test_client.post(f"/api/v1/knowledge-bases/{empty['id']}/disable").status_code == 200
    assert test_client.delete(f"/api/v1/knowledge-bases/{empty['id']}").json()["status"] == "deleted"
    assert empty["id"] not in {kb["id"] for kb in test_client.get("/api/v1/knowledge-bases").json()}

    kb = test_client.post("/api/v1/knowledge-bases", json={"name": "Managed", "code": "managed-kb"}).json()
    assert test_client.post(f"/api/v1/knowledge-bases/{kb['id']}/disable").status_code == 200
    rejected = test_client.post(f"/api/v1/knowledge-bases/{kb['id']}/documents", files={"file": ("notes.txt", b"blocked", "text/plain")})
    assert rejected.status_code == 409 and rejected.json()["error"]["code"] == "KNOWLEDGE_BASE_DISABLED"
    assert test_client.post(f"/api/v1/knowledge-bases/{kb['id']}/activate").status_code == 200
    uploaded = test_client.post(f"/api/v1/knowledge-bases/{kb['id']}/documents", files={"file": ("notes.txt", b"allowed", "text/plain")}).json()
    nonempty = test_client.delete(f"/api/v1/knowledge-bases/{kb['id']}")
    assert nonempty.status_code == 409 and nonempty.json()["error"]["code"] == "KNOWLEDGE_BASE_NOT_EMPTY"
    assert uploaded["status"] == "queued"
    assert test_client.post("/api/v1/internal/process-next").json()["processed"] is True


def test_retrieval_policy_can_be_updated_and_is_returned_in_kb_contract():
    test_client = next(client())
    kb = test_client.post("/api/v1/knowledge-bases", json={"name": "Planner policy", "code": "planner-policy"}).json()
    updated = test_client.patch(f"/api/v1/knowledge-bases/{kb['id']}/retrieval-config", json={
        "retrieval_mode": "precision", "enable_lightrag": False, "maximum_graph_depth": 2,
    })
    assert updated.status_code == 200
    assert updated.json()["retrieval_config"]["retrieval_mode"] == "precision"
    assert updated.json()["retrieval_config"]["enable_lightrag"] is False
    assert updated.json()["retrieval_config"]["maximum_graph_depth"] == 2


def test_legal_document_type_queues_automatic_metadata_extraction():
    test_client = next(client())
    kb = test_client.post("/api/v1/knowledge-bases", json={"name": "Automatic legal", "code": "automatic-legal-kb"}).json()
    uploaded = test_client.post(
        f"/api/v1/knowledge-bases/{kb['id']}/documents",
        files={"file": ("contract.txt", b"Party A shall provide support.", "text/plain")},
        data={"document_type": "contract"},
    )
    assert uploaded.status_code == 200
    assert uploaded.json()["document_type"] == "contract"
    assert uploaded.json()["legal_extraction_automatic"] is True
    assert test_client.post("/api/v1/internal/process-next").json()["processed"] is True
    document = test_client.get(f"/api/v1/documents/{uploaded.json()['document_id']}/text").json()
    assert document["status"] == "completed"
    assert document["document_type"] == "contract"
    jobs = test_client.get(f"/api/v1/documents/{uploaded.json()['document_id']}/jobs").json()
    assert any(job["type"] == "EXTRACT_LEGAL_METADATA" and job["status"] == "queued" for job in jobs)
    # Drain the follow-up job so independent tests do not consume it.
    assert test_client.post("/api/v1/internal/process-next").json()["processed"] is True


def test_upload_rejects_unknown_document_type():
    test_client = next(client())
    kb = test_client.post("/api/v1/knowledge-bases", json={"name": "Typed", "code": "invalid-type-kb"}).json()
    response = test_client.post(
        f"/api/v1/knowledge-bases/{kb['id']}/documents",
        files={"file": ("notes.txt", b"notes", "text/plain")},
        data={"document_type": "unknown"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "DOCUMENT_TYPE_INVALID"


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


def test_auto_retrieval_fixture_exercises_scopes_exact_dates_and_rerank_policy():
    test_client = next(client())
    kb = test_client.post("/api/v1/knowledge-bases", json={"name": "Auto retrieval fixture", "code": "auto-retrieval-fixture"}).json()
    test_client.patch(f"/api/v1/knowledge-bases/{kb['id']}/retrieval-config", json={
        "enable_lightrag": False, "planner_llm_fallback": False, "enable_reranker": False,
    })
    from datetime import date, datetime
    import hashlib
    with SessionLocal() as db:
        documents = []
        for index, (name, text, published_at) in enumerate([
            ("vpn.txt", "ขั้นตอนขอสิทธิ์ VPN คือ submit request and manager approval.", None),
            ("delay.txt", "ปัจจัยหลักที่ทำให้โครงการล่าช้า คือ vendor dependency.", None),
            ("abc-june.txt", "ข่าวบริษัท ABC เดือนมิถุนายน 2026 เปิดศูนย์บริการ.", date(2026, 6, 15)),
            ("abc-may.txt", "ข่าวบริษัท ABC เดือนพฤษภาคม 2026 ไม่ควรถูกคืน.", date(2026, 5, 15)),
            ("SNX-2026-001.txt", "เอกสารเลขที่ SNX-2026-001: Information Security Standard.", None),
        ]):
            digest = hashlib.sha256(f"{name}:{text}".encode()).hexdigest()
            doc = Document(knowledge_base_id=kb["id"], original_filename=name, stored_filename=name,
                           storage_path=f"/tmp/{name}", mime_type="text/plain", file_size=len(text), checksum_sha256=digest,
                           title=name.removesuffix(".txt"), document_type="general", published_at=published_at,
                           tags=[], status="completed", extracted_text=text, indexed_at=datetime.utcnow())
            db.add(doc); db.flush()
            db.add(DocumentChunk(document_id=doc.id, knowledge_base_id=kb["id"], chunk_index=0, content=text,
                                 content_sha256=hashlib.sha256(text.encode()).hexdigest(), char_start=0, char_end=len(text), token_count=len(text.split())))
            documents.append(doc)
        db.commit()
    architecture = documents[0]
    app_entity = test_client.post(f"/api/v1/knowledge-bases/{kb['id']}/entities", json={"name": "APP-01", "entity_type": "Application"}).json()
    portal = test_client.post(f"/api/v1/knowledge-bases/{kb['id']}/entities", json={"name": "Customer Portal", "entity_type": "Application"}).json()
    relationship = test_client.post(f"/api/v1/knowledge-bases/{kb['id']}/relationships", json={
        "source_entity_id": app_entity["id"], "target_entity_id": portal["id"], "relationship_type": "CONNECTS_TO",
        "document_id": architecture.id, "excerpt": "APP-01 connects to Customer Portal.",
    })
    assert relationship.status_code == 200
    cases = {
        "ขั้นตอนขอสิทธิ์ VPN คืออะไร": ["vector", "full_text"],
        "APP-01 เชื่อมต่อกับระบบใด": ["graph"],
        "ระบบใดได้รับผลกระทบหาก APP-01 ล่ม": ["graph", "vector"],
        "ปัจจัยหลักที่ทำให้โครงการล่าช้า": ["graph", "vector"],
        "ข่าวบริษัท ABC เดือนมิถุนายน 2026": ["full_text", "vector"],
        "เอกสารเลขที่ SNX-2026-001": ["exact_document", "full_text"],
        "ภาพรวมความสัมพันธ์ระหว่างหน่วยงาน": ["graph", "vector"],
    }
    results = {}
    for query, channels in cases.items():
        result = test_client.post("/api/v1/query", json={"query": query, "knowledge_base_ids": [kb["id"]]}).json()
        assert [item for item in result["metadata"]["retrieval_plan"]["channels"]] == channels
        results[query] = result
    local_trace = results["APP-01 เชื่อมต่อกับระบบใด"]["metadata"]["retrieval_trace"]
    assert next(item for item in local_trace if item["channel"] == "graph")["result_count"] == 1
    impact_trace = results["ระบบใดได้รับผลกระทบหาก APP-01 ล่ม"]["metadata"]["retrieval_trace"]
    assert next(item for item in impact_trace if item["channel"] == "graph")["result_count"] == 1
    global_trace = results["ภาพรวมความสัมพันธ์ระหว่างหน่วยงาน"]["metadata"]["retrieval_trace"]
    assert next(item for item in global_trace if item["channel"] == "graph")["result_count"] == 1
    news_sources = results["ข่าวบริษัท ABC เดือนมิถุนายน 2026"]["sources"]
    assert news_sources and {source["title"] for source in news_sources} == {"abc-june"}
    exact_trace = results["เอกสารเลขที่ SNX-2026-001"]["metadata"]["retrieval_trace"]
    assert next(item for item in exact_trace if item["channel"] == "exact_document")["result_count"] == 1
    assert all(item["channel"] != "graph" or item["status"] == "skipped" for item in exact_trace)
    rerank_trace = results["เอกสารเลขที่ SNX-2026-001"]["metadata"]["retrieval_trace"]
    assert next(item for item in rerank_trace if item["channel"] == "rerank")["detail"] == "disabled by retrieval policy"


def test_revoked_token_cannot_call_mcp():
    test_client = next(client())
    token = test_client.post("/api/v1/tokens", json={"name": "agent"}).json()
    assert test_client.post(f"/api/v1/tokens/{token['id']}/revoke").status_code == 200
    response = test_client.post("/mcp", headers={"Authorization": f"Bearer {token['token']}"}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 200
    assert response.json()["error"]["code"] == "AUTH_TOKEN_REVOKED"


def test_mcp_only_reads_active_knowledge_bases():
    test_client = next(client())
    active = test_client.post("/api/v1/knowledge-bases", json={"name": "MCP active", "code": "mcp-active-kb"}).json()
    disabled = test_client.post("/api/v1/knowledge-bases", json={"name": "MCP disabled", "code": "mcp-disabled-kb"}).json()
    test_client.post(f"/api/v1/knowledge-bases/{active['id']}/activate")
    test_client.post(f"/api/v1/knowledge-bases/{disabled['id']}/activate")
    active_doc = test_client.post(f"/api/v1/knowledge-bases/{active['id']}/documents", files={"file": ("active.txt", b"Active MCP evidence.", "text/plain")}).json()
    assert test_client.post("/api/v1/internal/process-next").json()["processed"] is True
    token = test_client.post("/api/v1/tokens", json={"name": "active-only-agent", "allowed_knowledge_base_ids": [active["id"], disabled["id"]], "allowed_tools": ["search_knowledge"]}).json()
    assert test_client.post(f"/api/v1/knowledge-bases/{disabled['id']}/disable").status_code == 200

    # The client-supplied KB value is ignored; the token scope is authoritative.
    active_response = test_client.post("/mcp", headers={"Authorization": f"Bearer {token['token']}"}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "search_knowledge", "arguments": {"query": "Active MCP evidence", "knowledge_base_ids": ["client-controlled-value"]}}})
    assert active_response.json()["result"]["structuredContent"]["sources"][0]["document_id"] == active_doc["document_id"]
    call = next(row for row in test_client.get("/api/v1/mcp/activity").json() if row["metadata"].get("request_id") == "1")
    assert call["metadata"]["knowledge_base_ids"] == [active["id"]]
    assert call["metadata"]["client_knowledge_base_ids_ignored"] is True
    disabled_response = test_client.post("/mcp", headers={"Authorization": f"Bearer {token['token']}"}, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "search_knowledge", "arguments": {"query": "anything", "knowledge_base_ids": [disabled["id"]]}}})
    assert disabled_response.json()["result"]["structuredContent"]["metadata"]["knowledge_base_ids"] == [active["id"]]
    assert test_client.post(f"/api/v1/knowledge-bases/{active['id']}/disable").status_code == 200
    no_active_scope = test_client.post("/mcp", headers={"Authorization": f"Bearer {token['token']}"}, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "search_knowledge", "arguments": {"query": "anything"}}})
    assert no_active_scope.json()["error"]["code"] == "KNOWLEDGE_BASE_INACTIVE"


def test_mcp_token_scope_must_reference_active_knowledge_bases():
    test_client = next(client())
    draft = test_client.post("/api/v1/knowledge-bases", json={"name": "Draft MCP scope", "code": "draft-mcp-scope"}).json()
    response = test_client.post("/api/v1/tokens", json={
        "name": "draft-scoped-agent",
        "allowed_knowledge_base_ids": [draft["id"]],
        "allowed_tools": ["search_knowledge"],
    })
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "KNOWLEDGE_BASE_INACTIVE"


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
    duplicate = test_client.post(f"/api/v1/documents/{uploaded['document_id']}/reprocess")
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "DOCUMENT_PROCESSING_IN_PROGRESS"
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
    uploaded = test_client.post(f"/api/v1/knowledge-bases/{kb['id']}/documents", files={"file": ("law.txt", "มาตรา 1 applies.".encode(), "text/plain")}, data={"document_type": "legal"}).json()
    test_client.post("/api/v1/internal/process-next")
    metadata = {"document_type": "ประกาศ", "articles": [{"article_number": "1", "text": "applies", "evidence_quote": "มาตรา 1 applies"}], "amendments": [{"title": "ประกาศแก้ไข", "announcement_number": "ฉบับที่ 2", "changes": "แก้ไขมาตรา 1", "evidence_quote": "ฉบับที่ 2"}]}
    saved = test_client.put(f"/api/v1/documents/{uploaded['document_id']}/legal-metadata", json={"metadata": metadata})
    assert saved.status_code == 200 and saved.json()["legal_metadata"]["articles"][0]["article_number"] == "1"
    patched = test_client.patch(f"/api/v1/documents/{uploaded['document_id']}/legal-metadata", json={"metadata": {"confidence": 0.9}})
    assert patched.json()["legal_metadata"]["confidence"] == 0.9
    graph_sync = test_client.post(f"/api/v1/knowledge-bases/{kb['id']}/graph/sync").json()
    assert graph_sync["entities"] >= 3 and graph_sync["relationships"] >= 2
    assert test_client.get(f"/api/v1/documents/{uploaded['document_id']}/text").json()["legal_metadata"]["amendments"][0]["title"] == "ประกาศแก้ไข"
    assert test_client.delete(f"/api/v1/documents/{uploaded['document_id']}/legal-metadata").json()["status"] == "deleted"
    assert test_client.get(f"/api/v1/documents/{uploaded['document_id']}/text").json()["legal_metadata"] is None


def test_legal_graph_v2_keeps_provisions_document_scoped_and_reviews_suggestions():
    test_client = next(client())
    kb = test_client.post("/api/v1/knowledge-bases", json={"name": "Legal graph v2", "code": "legal-graph-v2"}).json()
    first_metadata = {
        "schema_version": 2,
        "instrument": {"kind": "Act", "official_title": "PDPA Act", "official_number": "2562"},
        "provisions": [{"kind": "article", "number": "1", "evidence_quote": "มาตรา 1 แห่งพระราชบัญญัตินี้"}],
        "parties": [], "obligations": [], "rights": [], "prohibitions": [], "penalties": [], "definitions": [], "amendments": [], "references": [],
    }
    second_metadata = {
        "schema_version": 2,
        "instrument": {"kind": "Notification", "official_title": "PDPC Security Notification", "official_number": "1/2565"},
        "provisions": [{"kind": "article", "number": "1", "evidence_quote": "ข้อ 1 ของประกาศนี้"}],
        "parties": [], "obligations": [], "rights": [], "prohibitions": [], "penalties": [], "definitions": [], "amendments": [],
        "references": [{"relationship": "ISSUED_UNDER", "target_title": "PDPA Act", "target_number": "2562", "evidence_quote": "อาศัยอำนาจตาม PDPA Act", "confidence": 0.9}],
    }
    with SessionLocal() as db:
        db.add_all([
            Document(knowledge_base_id=kb["id"], original_filename="act.txt", stored_filename="act.txt", storage_path="/tmp/act.txt", mime_type="text/plain", file_size=1,
                     checksum_sha256="a" * 64, title="PDPA Act", document_type="legal", status="completed", extracted_text="มาตรา 1 แห่งพระราชบัญญัตินี้", legal_metadata=first_metadata),
            Document(knowledge_base_id=kb["id"], original_filename="notice.txt", stored_filename="notice.txt", storage_path="/tmp/notice.txt", mime_type="text/plain", file_size=1,
                     checksum_sha256="b" * 64, title="PDPC Security Notification", document_type="regulation", status="completed", extracted_text="อาศัยอำนาจตาม PDPA Act", legal_metadata=second_metadata),
        ])
        db.commit()
        first_build = services.rebuild_legal_graph(db, kb["id"])
        assert first_build["documents"] == 2

    verified = test_client.get(f"/api/v1/knowledge-bases/{kb['id']}/legal-graph?view=verified").json()
    provisions = [node for node in verified["nodes"] if node["entity_type"] == "Provision"]
    assert len(provisions) == 2
    assert len({node["identity_key"] for node in provisions}) == 2
    suggested = test_client.get(f"/api/v1/knowledge-bases/{kb['id']}/legal-graph?view=suggested").json()
    edge = next(edge for edge in suggested["edges"] if edge["relationship_type"] == "ISSUED_UNDER")
    assert edge["review_status"] == "suggested" and edge["sources"][0]["excerpt"] == "อาศัยอำนาจตาม PDPA Act"
    approved = test_client.patch(f"/api/v1/relationships/{edge['id']}/legal-review", json={"status": "verified", "note": "validated"})
    assert approved.status_code == 200 and approved.json()["review_status"] == "verified"
    with SessionLocal() as db:
        services.rebuild_legal_graph(db, kb["id"])
    verified_after = test_client.get(f"/api/v1/knowledge-bases/{kb['id']}/legal-graph?view=verified").json()
    assert any(item["id"] == edge["id"] and item["review_status"] == "verified" for item in verified_after["edges"])


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


def test_request_transactions_are_operator_safe_and_include_request_metadata():
    test_client = next(client())
    created = test_client.post("/api/v1/knowledge-bases", json={"name": "Transaction", "code": "transaction-kb"})
    request_id = created.headers["X-Request-ID"]
    transactions = test_client.get("/api/v1/logs/transactions?limit=100")
    assert transactions.status_code == 200
    row = next(item for item in transactions.json() if item["request_id"] == request_id)
    assert row["method"] == "POST"
    assert row["path"] == "/api/v1/knowledge-bases"
    assert row["status_code"] == 200
    assert isinstance(row["duration_ms"], (int, float))
    assert row["authentication"] == "admin_session"


def test_admin_query_transaction_includes_retrieval_executor_trace():
    test_client = next(client())
    kb = test_client.post("/api/v1/knowledge-bases", json={"name": "Trace", "code": "trace-kb"}).json()
    test_client.post(f"/api/v1/knowledge-bases/{kb['id']}/documents", files={"file": ("trace.txt", b"The trace document describes the platform.", "text/plain")})
    assert test_client.post("/api/v1/internal/process-next").json()["processed"] is True
    response = test_client.post("/api/v1/query", json={"query": "platform", "knowledge_base_ids": [kb["id"]]})
    assert response.status_code == 200
    request_id = response.headers["X-Request-ID"]
    transactions = test_client.get("/api/v1/logs/transactions?limit=100").json()
    row = next(item for item in transactions if item["request_id"] == request_id)
    assert row["retrieval"]["transport"] == "api"
    assert row["retrieval"]["retrieval_plan"]["intent"]
    assert row["retrieval"]["retrieval_trace"]
    traces = test_client.get("/api/v1/traces?transport=api").json()
    trace = next(item for item in traces if item["request_id"] == request_id)
    detail = test_client.get(f"/api/v1/traces/{trace['trace_id']}").json()
    assert detail["root_span"]["span_id"] == "root"
    assert detail["spans"]
    assert all("offset_ms" in span and "duration_ms" in span for span in detail["spans"])
    assert "query" not in detail


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
