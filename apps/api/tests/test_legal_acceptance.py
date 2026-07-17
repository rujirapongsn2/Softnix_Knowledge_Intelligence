"""End-to-end acceptance walkthrough for the legal registry and temporal/
relationship-aware retrieval feature, driven by fixtures/legal/*.md through the
real upload → process → legal-graph pipeline (legal extraction itself is
stubbed by setting legal_metadata directly, since tests run without an
OpenRouter key — see docs/ACCEPTANCE.md)."""
import os
import tempfile
from pathlib import Path

_TEST_ROOT = tempfile.mkdtemp()
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TEST_ROOT}/skip.db")
os.environ.setdefault("FILE_STORAGE_PATH", f"{_TEST_ROOT}/files")
os.environ.setdefault("INITIAL_ADMIN_PASSWORD", "correct-horse-battery-staple")
os.environ.setdefault("LIGHTRAG_BASE_URL", "")
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("OPENROUTER_API_KEY", "")
os.environ.setdefault("EXT_OCR_KEY", "")

from fastapi.testclient import TestClient

from app import services
from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import Document, LegalInstrumentRelation

Base.metadata.create_all(engine)

FIXTURES_DIR = Path(__file__).resolve().parents[3] / "fixtures" / "legal"


def client():
    with TestClient(app) as test_client:
        assert test_client.post("/api/v1/auth/login", json={"username": "admin", "password": "correct-horse-battery-staple"}).status_code == 200
        yield test_client


def _upload_and_process(test_client, kb_id, filename, document_type):
    text = (FIXTURES_DIR / filename).read_text(encoding="utf-8")
    uploaded = test_client.post(f"/api/v1/knowledge-bases/{kb_id}/documents", files={
        "file": (filename, text.encode("utf-8"), "text/markdown"),
    }, data={"document_type": document_type}).json()
    # Drain every job this upload queues (PROCESS_DOCUMENT, then EXTRACT_LEGAL_METADATA
    # for a legal/regulation/contract type) so nothing leaks into a later test.
    while test_client.post("/api/v1/internal/process-next").json()["processed"]:
        pass
    return uploaded["document_id"]


def _set_legal_metadata(document_id, metadata):
    with SessionLocal() as db:
        document = db.get(Document, document_id)
        document.legal_metadata = metadata
        db.commit()


LEGAL_ACT_TITLE = "พระราชบัญญัติคุ้มครองแรงงานสมมุติ พ.ศ. 2541"
LEGAL_AMENDMENT_TITLE = "พระราชบัญญัติคุ้มครองแรงงานสมมุติ (ฉบับที่ 2) พ.ศ. 2562"
NOTIFICATION_2563_TITLE = "ประกาศกระทรวงแรงงานสมมุติ เรื่อง หลักเกณฑ์การจ่ายค่าชดเชย พ.ศ. 2563"
NOTIFICATION_2566_TITLE = "ประกาศกระทรวงแรงงานสมมุติ เรื่อง หลักเกณฑ์การจ่ายค่าชดเชย (ฉบับที่ 2) พ.ศ. 2566"


def _base_metadata(kind, official_title, effective_date, provisions, references):
    return {
        "schema_version": 2,
        "instrument": {"kind": kind, "official_title": official_title, "official_number": None, "effective_date": effective_date},
        "provisions": provisions, "parties": [], "obligations": [], "rights": [], "prohibitions": [],
        "penalties": [], "definitions": [], "amendments": [], "references": references,
    }


def _seed_legal_knowledge_base(test_client, code):
    kb = test_client.post("/api/v1/knowledge-bases", json={"name": f"Legal acceptance {code}", "code": code}).json()
    act_id = _upload_and_process(test_client, kb["id"], "act-2541.md", "legal")
    amendment_id = _upload_and_process(test_client, kb["id"], "act-amendment-2562.md", "legal")
    notification_2563_id = _upload_and_process(test_client, kb["id"], "notification-2563.md", "regulation")
    notification_2566_id = _upload_and_process(test_client, kb["id"], "notification-2566.md", "regulation")
    _upload_and_process(test_client, kb["id"], "faq.md", "general")

    _set_legal_metadata(act_id, _base_metadata(
        "act", LEGAL_ACT_TITLE, "1 กันยายน 2541",
        [{"kind": "article", "number": "15", "text": "ค่าชดเชยหกสิบวัน",
          "evidence_quote": "ให้นายจ้างจ่ายค่าชดเชยแก่ลูกจ้างซึ่งเลิกจ้างเท่ากับค่าจ้างอัตราสุดท้ายหกสิบวัน"}],
        [],
    ))
    _set_legal_metadata(amendment_id, _base_metadata(
        "act", LEGAL_AMENDMENT_TITLE, "1 พฤษภาคม 2562",
        [{"kind": "article", "number": "15", "text": "ค่าชดเชยเก้าสิบวัน",
          "evidence_quote": "ให้นายจ้างจ่ายค่าชดเชยแก่ลูกจ้างซึ่งเลิกจ้างเท่ากับค่าจ้างอัตราสุดท้ายเก้าสิบวัน"}],
        [{"relationship": "AMENDS", "target_title": LEGAL_ACT_TITLE, "target_provision": "มาตรา 15",
          "evidence_quote": "ให้ยกเลิกความในมาตรา 15 แห่งพระราชบัญญัติคุ้มครองแรงงานสมมุติ พ.ศ. 2541", "confidence": 0.95}],
    ))
    _set_legal_metadata(notification_2563_id, _base_metadata(
        "notification", NOTIFICATION_2563_TITLE, "1 มกราคม 2563",
        [{"kind": "clause", "number": "5", "text": "แจ้งอัตราค่าชดเชยก่อนเจ็ดวัน",
          "evidence_quote": "นายจ้างต้องแจ้งอัตราค่าชดเชยให้ลูกจ้างทราบเป็นลายลักษณ์อักษรก่อนวันเริ่มงานไม่น้อยกว่าเจ็ดวัน"}],
        [{"relationship": "ISSUED_UNDER", "target_title": LEGAL_ACT_TITLE,
          "evidence_quote": "อาศัยอำนาจตามความในมาตรา 6 แห่งพระราชบัญญัติคุ้มครองแรงงานสมมุติ พ.ศ. 2541", "confidence": 0.9}],
    ))
    _set_legal_metadata(notification_2566_id, _base_metadata(
        "notification", NOTIFICATION_2566_TITLE, "1 มีนาคม 2566",
        [{"kind": "clause", "number": "5", "text": "แจ้งอัตราค่าชดเชยก่อนสิบห้าวัน",
          "evidence_quote": "นายจ้างต้องแจ้งอัตราค่าชดเชยให้ลูกจ้างทราบเป็นลายลักษณ์อักษรก่อนวันเริ่มงานไม่น้อยกว่าสิบห้าวัน"}],
        [{"relationship": "REPEALS", "target_title": NOTIFICATION_2563_TITLE, "target_provision": "ข้อ 5",
          "evidence_quote": "ให้ยกเลิกความในข้อ 5 แห่งประกาศกระทรวงแรงงานสมมุติ เรื่อง หลักเกณฑ์การจ่ายค่าชดเชย พ.ศ. 2563", "confidence": 0.95}],
    ))

    with SessionLocal() as db:
        services.rebuild_legal_graph(db, kb["id"])

    # Approve every suggested cross-instrument relationship: an unreviewed edge
    # must never affect retrieval (acceptance scenario 5).
    suggested = test_client.get(f"/api/v1/knowledge-bases/{kb['id']}/legal-graph?view=suggested").json()
    for edge in suggested["edges"]:
        assert test_client.patch(f"/api/v1/relationships/{edge['id']}/legal-review", json={"status": "verified"}).status_code == 200

    return kb, {
        "act": act_id, "amendment": amendment_id,
        "notification_2563": notification_2563_id, "notification_2566": notification_2566_id,
    }


def test_scenario_1_current_query_prefers_the_amended_provision_over_faq_and_old_version():
    test_client = next(client())
    kb, doc_ids = _seed_legal_knowledge_base(test_client, "legal-acceptance-s1")
    response = test_client.post("/api/v1/query", json={
        "query": "มาตรา 15 กำหนดอัตราค่าชดเชยไว้อย่างไร", "knowledge_base_ids": [kb["id"]], "max_sources": 10,
    })
    assert response.status_code == 200
    payload = response.json()
    sources = payload["sources"]
    # The old act's other provisions (มาตรา 1, มาตรา 16, ...) may still surface —
    # keyword full-text recall is intentionally broad — but the amended มาตรา 15
    # itself must never appear in both its old and current wording at once.
    assert any(s["document_id"] == doc_ids["amendment"] and s.get("section_number") == "15" for s in sources)
    assert not any(s["document_id"] == doc_ids["act"] and s.get("section_number") == "15" for s in sources)
    assert any(w["code"] in {"SUPERSEDED_VERSION_REMOVED", "PROVISION_AMENDED"} for w in payload["warnings"])
    faq_ranks = [index for index, s in enumerate(sources) if "faq" in (s.get("title") or "").casefold()]
    amendment_rank = next(index for index, s in enumerate(sources) if s["document_id"] == doc_ids["amendment"] and s.get("section_number") == "15")
    assert all(rank > amendment_rank for rank in faq_ranks)


def test_scenario_2_as_of_a_past_date_returns_the_original_1998_act():
    test_client = next(client())
    kb, doc_ids = _seed_legal_knowledge_base(test_client, "legal-acceptance-s2")
    response = test_client.post("/api/v1/query", json={
        "query": "มาตรา 15 กำหนดอัตราค่าชดเชยไว้อย่างไร", "knowledge_base_ids": [kb["id"]], "max_sources": 10,
        "filters": {"as_of_date": "2010-01-01"},
    })
    assert response.status_code == 200
    document_ids = [source["document_id"] for source in response.json()["sources"]]
    assert doc_ids["act"] in document_ids
    assert doc_ids["amendment"] not in document_ids


def test_scenario_3_repealed_provision_of_the_notification_returns_the_new_text_with_a_warning():
    test_client = next(client())
    kb, doc_ids = _seed_legal_knowledge_base(test_client, "legal-acceptance-s3")
    response = test_client.post("/api/v1/query", json={
        "query": "ข้อ 5 ของประกาศแจ้งอัตราค่าชดเชยก่อนกี่วัน", "knowledge_base_ids": [kb["id"]], "max_sources": 10,
    })
    assert response.status_code == 200
    payload = response.json()
    document_ids = [source["document_id"] for source in payload["sources"]]
    assert doc_ids["notification_2566"] in document_ids
    assert doc_ids["notification_2563"] not in document_ids
    assert any(w["code"] == "SUPERSEDED_VERSION_REMOVED" for w in payload["warnings"])


def test_scenario_5_unreviewed_suggestion_has_no_retrieval_effect_until_approved():
    test_client = next(client())
    kb = test_client.post("/api/v1/knowledge-bases", json={"name": "Legal unreviewed", "code": "legal-unreviewed"}).json()
    act_id = _upload_and_process(test_client, kb["id"], "act-2541.md", "legal")
    amendment_id = _upload_and_process(test_client, kb["id"], "act-amendment-2562.md", "legal")
    _set_legal_metadata(act_id, _base_metadata(
        "act", LEGAL_ACT_TITLE, "1 กันยายน 2541",
        [{"kind": "article", "number": "15", "text": "ค่าชดเชยหกสิบวัน", "evidence_quote": "หกสิบวัน"}], [],
    ))
    _set_legal_metadata(amendment_id, _base_metadata(
        "act", LEGAL_AMENDMENT_TITLE, "1 พฤษภาคม 2562",
        [{"kind": "article", "number": "15", "text": "ค่าชดเชยเก้าสิบวัน", "evidence_quote": "เก้าสิบวัน"}],
        [{"relationship": "AMENDS", "target_title": LEGAL_ACT_TITLE, "target_provision": "มาตรา 15",
          "evidence_quote": "ยกเลิกมาตรา 15", "confidence": 0.95}],
    ))
    with SessionLocal() as db:
        services.rebuild_legal_graph(db, kb["id"])
        relation = db.query(LegalInstrumentRelation).filter_by(knowledge_base_id=kb["id"], relation="AMENDS").first()
        assert relation.review_status == "suggested"

    response = test_client.post("/api/v1/query", json={
        "query": "มาตรา 15 กำหนดอัตราค่าชดเชยไว้อย่างไร", "knowledge_base_ids": [kb["id"]], "max_sources": 10,
    })
    assert response.status_code == 200
    payload = response.json()
    # Neither version was ever excluded/deduplicated: an unreviewed suggestion
    # must not change retrieval.
    document_ids = {source["document_id"] for source in payload["sources"]}
    assert act_id in document_ids
    assert amendment_id in document_ids
    assert payload["warnings"] == []


def test_scenario_4_regression_general_document_type_query_is_unaffected():
    test_client = next(client())
    kb = test_client.post("/api/v1/knowledge-bases", json={"name": "Legal regression fixture", "code": "legal-regression-fixture"}).json()
    text = (FIXTURES_DIR / "faq.md").read_text(encoding="utf-8")
    uploaded = test_client.post(f"/api/v1/knowledge-bases/{kb['id']}/documents", files={
        "file": ("faq.md", text.encode("utf-8"), "text/markdown"),
    }).json()
    assert test_client.post("/api/v1/internal/process-next").json()["processed"] is True
    response = test_client.post("/api/v1/query", json={"query": "ค่าชดเชยเมื่อถูกเลิกจ้าง", "knowledge_base_ids": [kb["id"]]})
    assert response.status_code == 200
    payload = response.json()
    assert payload["sources"]
    assert payload["warnings"] == []
    assert "legal_label" not in payload["sources"][0]
