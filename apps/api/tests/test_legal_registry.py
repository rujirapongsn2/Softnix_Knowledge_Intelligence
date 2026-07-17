import os
import tempfile
from datetime import date, timedelta

_TEST_ROOT = tempfile.mkdtemp()
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TEST_ROOT}/skip.db")
os.environ.setdefault("FILE_STORAGE_PATH", f"{_TEST_ROOT}/files")
os.environ.setdefault("INITIAL_ADMIN_PASSWORD", "correct-horse-battery-staple")
os.environ.setdefault("LIGHTRAG_BASE_URL", "")
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("OPENROUTER_API_KEY", "")
os.environ.setdefault("EXT_OCR_KEY", "")

import pytest
from fastapi.testclient import TestClient

from app import services
from app.db import Base, SessionLocal, engine
from app.legal_registry import classify_kind, normalize_family_key, parse_provision_refs, parse_thai_date, resolve_instrument_statuses
from app.main import app
from app.models import Document, KnowledgeBase, LegalFamily, LegalInstrument, LegalInstrumentRelation

Base.metadata.create_all(engine)


def client():
    with TestClient(app) as test_client:
        assert test_client.post("/api/v1/auth/login", json={"username": "admin", "password": "correct-horse-battery-staple"}).status_code == 200
        yield test_client


@pytest.mark.parametrize(("title", "expected"), [
    ("รัฐธรรมนูญแห่งราชอาณาจักรไทย", "constitution"),
    ("พระราชบัญญัติคุ้มครองแรงงาน พ.ศ. 2541", "act"),
    ("พ.ร.บ. คุ้มครองข้อมูลส่วนบุคคล พ.ศ. 2562", "act"),
    ("พระราชกำหนดการบริหารราชการในสถานการณ์ฉุกเฉิน พ.ศ. 2548", "act"),
    ("พระราชกฤษฎีกาจัดตั้งสำนักงาน", "royal_decree"),
    ("กฎกระทรวงกำหนดมาตรฐาน", "ministerial_regulation"),
    ("ประกาศกระทรวงแรงงาน เรื่อง หลักเกณฑ์", "notification"),
    ("ระเบียบกรมสวัสดิการและคุ้มครองแรงงาน", "rule"),
    ("ข้อบังคับว่าด้วยการทำงาน", "rule"),
    ("หนังสือเวียนที่ 1/2566", "circular"),
    ("แนวปฏิบัติการลาป่วย", "guideline"),
    ("คู่มือการปฏิบัติงาน", "guideline"),
    ("มติคณะรัฐมนตรี", "resolution"),
    ("Labour Protection Act B.E. 2541", "act"),
    ("Ministerial Regulation on Standards", "ministerial_regulation"),
    ("Some unrelated FAQ document", "other"),
])
def test_classify_kind_covers_thai_and_english_titles(title, expected):
    assert classify_kind(title) == expected


def test_classify_kind_falls_back_to_validated_extracted_kind():
    assert classify_kind("Untitled memo", "contract") == "contract"
    assert classify_kind("Untitled memo", "not-a-real-kind") == "other"
    assert classify_kind("Untitled memo", None) == "other"


def test_normalize_family_key_groups_base_act_with_its_amendment():
    base_key, base_version, base_year = normalize_family_key("พระราชบัญญัติคุ้มครองแรงงาน พ.ศ. 2541")
    amend_key, amend_version, amend_year = normalize_family_key("พระราชบัญญัติคุ้มครองแรงงาน (ฉบับที่ 7) พ.ศ. 2562")
    assert base_key == amend_key
    assert base_version is None
    assert amend_version == "ฉบับที่ 7"
    assert base_year == 1998
    assert amend_year == 2019


def test_normalize_family_key_handles_thai_numerals_and_missing_year():
    key, version, year = normalize_family_key("ประกาศกระทรวง (ฉบับที่ ๒)")
    assert version == "ฉบับที่ 2"
    assert year is None
    assert "๒" not in key


@pytest.mark.parametrize(("value", "expected"), [
    ("1 มกราคม 2567", date(2024, 1, 1)),
    ("๑ มกราคม ๒๕๖๗", date(2024, 1, 1)),
    ("2024-01-01", date(2024, 1, 1)),
    (None, None),
    ("", None),
    ("not a date", None),
])
def test_parse_thai_date_supports_thai_numerals_and_iso(value, expected):
    assert parse_thai_date(value) == expected


def test_parse_provision_refs_finds_mixed_references():
    refs = parse_provision_refs("ให้เป็นไปตามมาตรา 15 ทวิ และ ข้อ 12 ของหมวด 3")
    kinds = [(item["kind"], item["number"]) for item in refs]
    assert ("มาตรา", "15 ทวิ") in kinds
    assert ("ข้อ", "12") in kinds
    assert ("หมวด", "3") in kinds


def _legal_document(db, kb_id, *, title, official_title=None, official_number=None, effective_date=None,
                    effective_to=None, references=None, amendments=None, document_type="legal", checksum="a" * 64):
    metadata = {
        "schema_version": 2,
        "instrument": {"kind": None, "official_title": official_title or title, "official_number": official_number,
                       "effective_date": effective_date, "effective_to": effective_to, "issuer": None},
        "provisions": [], "parties": [], "obligations": [], "rights": [], "prohibitions": [],
        "penalties": [], "definitions": [], "amendments": amendments or [], "references": references or [],
    }
    document = Document(knowledge_base_id=kb_id, original_filename=f"{title}.txt", stored_filename=f"{title}.txt",
                        storage_path=f"/tmp/{title}.txt", mime_type="text/plain", file_size=1, checksum_sha256=checksum,
                        title=title, document_type=document_type, status="completed", extracted_text=title, legal_metadata=metadata)
    db.add(document); db.flush()
    return document


def test_upsert_legal_instrument_classifies_and_assigns_family():
    with SessionLocal() as db:
        kb = KnowledgeBase(code="registry-upsert", name="Registry upsert")
        db.add(kb); db.flush()
        document = _legal_document(db, kb.id, title="พระราชบัญญัติคุ้มครองแรงงาน พ.ศ. 2541", effective_date="1 กันยายน 2541")
        instrument = services.upsert_legal_instrument(db, document)
        db.commit()
        assert instrument.kind == "act"
        assert instrument.authority_level == 90
        assert instrument.effective_from == date(1998, 9, 1)
        assert instrument.family_id is not None
        family = db.get(LegalFamily, instrument.family_id)
        assert family.normalized_key == normalize_family_key("พระราชบัญญัติคุ้มครองแรงงาน พ.ศ. 2541")[0]


def test_upsert_legal_instrument_groups_amendment_into_same_family():
    with SessionLocal() as db:
        kb = KnowledgeBase(code="registry-family", name="Registry family")
        db.add(kb); db.flush()
        base = _legal_document(db, kb.id, title="พระราชบัญญัติคุ้มครองแรงงาน พ.ศ. 2541", effective_date="1 กันยายน 2541", checksum="b" * 64)
        amendment = _legal_document(db, kb.id, title="พระราชบัญญัติคุ้มครองแรงงาน (ฉบับที่ 7) พ.ศ. 2562", effective_date="1 พฤษภาคม 2562", checksum="c" * 64)
        base_row = services.upsert_legal_instrument(db, base)
        amendment_row = services.upsert_legal_instrument(db, amendment)
        db.commit()
        assert base_row.family_id == amendment_row.family_id
        assert amendment_row.version_label == "ฉบับที่ 7"


def test_upsert_legal_instrument_never_overwrites_a_manual_row():
    with SessionLocal() as db:
        kb = KnowledgeBase(code="registry-manual", name="Registry manual")
        db.add(kb); db.flush()
        document = _legal_document(db, kb.id, title="ประกาศกระทรวง", effective_date="1 มกราคม 2560")
        row = services.upsert_legal_instrument(db, document)
        row.status_source, row.kind, row.official_title = "manual", "act", "Manually renamed"
        db.commit()
        services.upsert_legal_instrument(db, document)
        db.commit()
        db.refresh(row)
        assert row.kind == "act"
        assert row.official_title == "Manually renamed"


def _instrument(db, kb_id, document, **overrides):
    defaults = dict(document_id=document.id, knowledge_base_id=kb_id, status_source="resolver", review_status="unreviewed")
    defaults.update(overrides)
    row = LegalInstrument(**defaults)
    db.add(row); db.flush()
    return row


def test_resolver_sets_in_force_and_not_yet_effective_from_baseline_dates():
    with SessionLocal() as db:
        kb = KnowledgeBase(code="resolver-baseline", name="Resolver baseline")
        db.add(kb); db.flush()
        past = _legal_document(db, kb.id, title="Past act", checksum="d" * 64)
        future = _legal_document(db, kb.id, title="Future act", checksum="e" * 64)
        unknown = _legal_document(db, kb.id, title="Unknown act", checksum="f" * 64)
        past_row = _instrument(db, kb.id, past, effective_from=date.today() - timedelta(days=30))
        future_row = _instrument(db, kb.id, future, effective_from=date.today() + timedelta(days=30))
        unknown_row = _instrument(db, kb.id, unknown)
        db.commit()
        resolve_instrument_statuses(db, kb.id)
        db.commit()
        db.refresh(past_row); db.refresh(future_row); db.refresh(unknown_row)
        assert past_row.status == "in_force"
        assert future_row.status == "not_yet_effective"
        assert unknown_row.status == "unknown"


def test_resolver_applies_verified_repeals_and_ignores_suggested_edges():
    with SessionLocal() as db:
        kb = KnowledgeBase(code="resolver-repeal", name="Resolver repeal")
        db.add(kb); db.flush()
        old_doc = _legal_document(db, kb.id, title="Old notice", checksum="1" * 64)
        new_doc = _legal_document(db, kb.id, title="Repealing notice", checksum="2" * 64)
        old_row = _instrument(db, kb.id, old_doc, effective_from=date(2020, 1, 1))
        new_row = _instrument(db, kb.id, new_doc, effective_from=date.today() - timedelta(days=1))
        db.add(LegalInstrumentRelation(knowledge_base_id=kb.id, source_instrument_id=new_row.id, target_instrument_id=old_row.id,
                                       relation="REPEALS", review_status="suggested"))
        db.commit()
        resolve_instrument_statuses(db, kb.id)
        db.commit(); db.refresh(old_row)
        assert old_row.status == "in_force"  # suggested edge must not affect status

        relation = db.query(LegalInstrumentRelation).filter_by(source_instrument_id=new_row.id).first()
        relation.review_status = "verified"
        db.commit()
        resolve_instrument_statuses(db, kb.id)
        db.commit(); db.refresh(old_row)
        assert old_row.status == "repealed"
        assert old_row.effective_to == new_row.effective_from


def test_resolver_provision_level_repeal_does_not_repeal_whole_instrument():
    with SessionLocal() as db:
        kb = KnowledgeBase(code="resolver-provision-repeal", name="Resolver provision repeal")
        db.add(kb); db.flush()
        old_doc = _legal_document(db, kb.id, title="Notification 2563", checksum="3" * 64)
        new_doc = _legal_document(db, kb.id, title="Notification 2566", checksum="4" * 64)
        old_row = _instrument(db, kb.id, old_doc, effective_from=date(2020, 1, 1))
        new_row = _instrument(db, kb.id, new_doc, effective_from=date.today() - timedelta(days=1))
        db.add(LegalInstrumentRelation(knowledge_base_id=kb.id, source_instrument_id=new_row.id, target_instrument_id=old_row.id,
                                       relation="REPEALS", target_provision="ข้อ 5", review_status="verified"))
        db.commit()
        resolve_instrument_statuses(db, kb.id)
        db.commit(); db.refresh(old_row)
        assert old_row.status == "in_force"


def test_resolver_amends_keeps_source_in_force_and_marks_target_amended():
    with SessionLocal() as db:
        kb = KnowledgeBase(code="resolver-amend", name="Resolver amend")
        db.add(kb); db.flush()
        base_doc = _legal_document(db, kb.id, title="Base act", checksum="5" * 64)
        amendment_doc = _legal_document(db, kb.id, title="Amendment act", checksum="6" * 64)
        base_row = _instrument(db, kb.id, base_doc, effective_from=date(1998, 1, 1))
        amendment_row = _instrument(db, kb.id, amendment_doc, effective_from=date.today() - timedelta(days=1))
        db.add(LegalInstrumentRelation(knowledge_base_id=kb.id, source_instrument_id=amendment_row.id, target_instrument_id=base_row.id,
                                       relation="AMENDS", review_status="verified"))
        db.commit()
        resolve_instrument_statuses(db, kb.id)
        db.commit(); db.refresh(base_row); db.refresh(amendment_row)
        assert base_row.status == "amended"
        assert amendment_row.status == "in_force"


def test_resolver_family_ordering_supersedes_older_full_version_only():
    with SessionLocal() as db:
        kb = KnowledgeBase(code="resolver-family", name="Resolver family")
        db.add(kb); db.flush()
        family = LegalFamily(knowledge_base_id=kb.id, base_title="Code", normalized_key="code")
        db.add(family); db.flush()
        old_doc = _legal_document(db, kb.id, title="Code 2540", checksum="7" * 64)
        new_doc = _legal_document(db, kb.id, title="Code 2560", checksum="8" * 64)
        amend_doc = _legal_document(db, kb.id, title="Code amendment", checksum="9" * 64)
        old_row = _instrument(db, kb.id, old_doc, family_id=family.id, effective_from=date(1997, 1, 1), enacted_year=1997)
        new_row = _instrument(db, kb.id, new_doc, family_id=family.id, effective_from=date.today() - timedelta(days=1), enacted_year=2017)
        amend_row = _instrument(db, kb.id, amend_doc, family_id=family.id, effective_from=date.today() - timedelta(days=1),
                                version_label="ฉบับที่ 1")
        db.commit()
        resolve_instrument_statuses(db, kb.id)
        db.commit(); db.refresh(old_row); db.refresh(new_row); db.refresh(amend_row)
        assert old_row.status == "superseded"
        assert old_row.effective_to == new_row.effective_from
        assert new_row.status == "in_force"
        assert amend_row.status == "in_force"  # a version-labelled amendment is never superseded by family ordering


def test_resolver_never_overwrites_a_manual_status():
    with SessionLocal() as db:
        kb = KnowledgeBase(code="resolver-manual", name="Resolver manual")
        db.add(kb); db.flush()
        doc = _legal_document(db, kb.id, title="Manually pinned act", checksum="a1" * 32)
        row = _instrument(db, kb.id, doc, effective_from=date(2020, 1, 1), status="repealed", status_source="manual",
                          status_reason="manual note")
        db.commit()
        resolve_instrument_statuses(db, kb.id)
        db.commit(); db.refresh(row)
        assert row.status == "repealed"
        assert row.status_reason == "manual note"


def test_resolve_instrument_statuses_is_idempotent():
    with SessionLocal() as db:
        kb = KnowledgeBase(code="resolver-idempotent", name="Resolver idempotent")
        db.add(kb); db.flush()
        doc = _legal_document(db, kb.id, title="Idempotent act", checksum="b1" * 32)
        _instrument(db, kb.id, doc, effective_from=date(2020, 1, 1))
        db.commit()
        first = resolve_instrument_statuses(db, kb.id)
        db.commit()
        second = resolve_instrument_statuses(db, kb.id)
        db.commit()
        assert first["changed"] >= 1
        assert second["changed"] == 0


def test_rebuild_legal_graph_registers_instruments_and_relations_end_to_end():
    test_client = next(client())
    kb = test_client.post("/api/v1/knowledge-bases", json={"name": "Registry E2E", "code": "registry-e2e"}).json()
    base_metadata = {
        "schema_version": 2,
        "instrument": {"kind": "act", "official_title": "พระราชบัญญัติทดสอบ พ.ศ. 2541", "official_number": "2541", "effective_date": "1 มกราคม 2541"},
        "provisions": [{"kind": "article", "number": "15", "evidence_quote": "มาตรา 15 ค่าชดเชย"}],
        "parties": [], "obligations": [], "rights": [], "prohibitions": [], "penalties": [], "definitions": [], "amendments": [], "references": [],
    }
    amendment_metadata = {
        "schema_version": 2,
        "instrument": {"kind": "act", "official_title": "พระราชบัญญัติทดสอบ (ฉบับที่ 2) พ.ศ. 2562", "official_number": "2562", "effective_date": "1 มกราคม 2562"},
        "provisions": [], "parties": [], "obligations": [], "rights": [], "prohibitions": [], "penalties": [], "definitions": [],
        "amendments": [], "references": [{"relationship": "AMENDS", "target_title": "พระราชบัญญัติทดสอบ พ.ศ. 2541", "target_number": "2541",
                                          "evidence_quote": "ให้ยกเลิกความในมาตรา 15", "confidence": 0.95}],
    }
    with SessionLocal() as db:
        db.add_all([
            Document(knowledge_base_id=kb["id"], original_filename="base.txt", stored_filename="base.txt", storage_path="/tmp/base.txt",
                     mime_type="text/plain", file_size=1, checksum_sha256="c1" * 32, title="พระราชบัญญัติทดสอบ พ.ศ. 2541",
                     document_type="legal", status="completed", extracted_text="มาตรา 15 ค่าชดเชย", legal_metadata=base_metadata),
            Document(knowledge_base_id=kb["id"], original_filename="amend.txt", stored_filename="amend.txt", storage_path="/tmp/amend.txt",
                     mime_type="text/plain", file_size=1, checksum_sha256="c2" * 32, title="พระราชบัญญัติทดสอบ (ฉบับที่ 2) พ.ศ. 2562",
                     document_type="legal", status="completed", extracted_text="ให้ยกเลิกความในมาตรา 15 แห่งพระราชบัญญัติทดสอบ พ.ศ. 2541", legal_metadata=amendment_metadata),
        ])
        db.commit()
        totals = services.rebuild_legal_graph(db, kb["id"])
        assert totals["documents"] == 2
        registry = db.query(LegalInstrument).filter_by(knowledge_base_id=kb["id"]).all()
        assert len(registry) == 2
        assert {row.family_id for row in registry} == {registry[0].family_id}

    registry_response = test_client.get(f"/api/v1/knowledge-bases/{kb['id']}/legal-registry").json()
    assert len(registry_response) == 2

    with SessionLocal() as db:
        relation = db.query(LegalInstrumentRelation).filter_by(knowledge_base_id=kb["id"], relation="AMENDS").first()
        assert relation is not None
        assert relation.review_status == "suggested"
        relationship_id = relation.relationship_id

    approved = test_client.patch(f"/api/v1/relationships/{relationship_id}/legal-review", json={"status": "verified"})
    assert approved.status_code == 200

    with SessionLocal() as db:
        relation = db.query(LegalInstrumentRelation).filter_by(knowledge_base_id=kb["id"], relation="AMENDS").first()
        assert relation.review_status == "verified"
        target_row = db.get(LegalInstrument, relation.target_instrument_id)
        assert target_row.status == "amended"

    instrument_id = target_row.id
    detail = test_client.get(f"/api/v1/legal-instruments/{instrument_id}").json()
    assert len(detail["family"]) == 2
    assert any(row["relation"] == "AMENDS" for row in detail["incoming_relations"])

    override = test_client.patch(f"/api/v1/legal-instruments/{instrument_id}", json={"status": "repealed"})
    assert override.status_code == 200
    assert override.json()["status"] == "repealed"
    assert override.json()["status_source"] == "manual"

    resolve_response = test_client.post(f"/api/v1/knowledge-bases/{kb['id']}/legal-registry/resolve")
    assert resolve_response.status_code == 200
    with SessionLocal() as db:
        refreshed = db.get(LegalInstrument, instrument_id)
        assert refreshed.status == "repealed"  # manual override survives a resolver pass


def test_resolver_family_ordering_ignores_a_future_dated_full_version_without_skipping_the_family():
    with SessionLocal() as db:
        kb = KnowledgeBase(code="resolver-family-future", name="Resolver family future")
        db.add(kb); db.flush()
        family = LegalFamily(knowledge_base_id=kb.id, base_title="Code", normalized_key="code-future")
        db.add(family); db.flush()
        v1_doc = _legal_document(db, kb.id, title="Code V1", checksum="c1" * 32)
        v2_doc = _legal_document(db, kb.id, title="Code V2", checksum="c2" * 32)
        v3_doc = _legal_document(db, kb.id, title="Code V3 future", checksum="c3" * 32)
        v1 = _instrument(db, kb.id, v1_doc, family_id=family.id, effective_from=date(2010, 1, 1))
        v2 = _instrument(db, kb.id, v2_doc, family_id=family.id, effective_from=date(2020, 1, 1))
        v3 = _instrument(db, kb.id, v3_doc, family_id=family.id, effective_from=date.today() + timedelta(days=365))
        db.commit()
        resolve_instrument_statuses(db, kb.id)
        db.commit(); db.refresh(v1); db.refresh(v2); db.refresh(v3)
        # V2 is the newest version already in force and must supersede V1, even
        # though V3 (a future re-enactment) exists in the same family.
        assert v1.status == "superseded"
        assert v2.status == "in_force"
        assert v3.status == "not_yet_effective"


def test_resolver_family_ordering_uses_enacted_year_when_effective_from_is_missing():
    with SessionLocal() as db:
        kb = KnowledgeBase(code="resolver-family-year", name="Resolver family year")
        db.add(kb); db.flush()
        family = LegalFamily(knowledge_base_id=kb.id, base_title="Code", normalized_key="code-year")
        db.add(family); db.flush()
        old_doc = _legal_document(db, kb.id, title="Code old", checksum="d1" * 32)
        new_doc = _legal_document(db, kb.id, title="Code new", checksum="d2" * 32)
        # The newer instrument's effective_date failed to extract (no
        # published_at fallback either), but its enacted_year is known and is
        # later than the older instrument's real effective_from.
        old_row = _instrument(db, kb.id, old_doc, family_id=family.id, effective_from=date(1997, 1, 1), enacted_year=1997)
        new_row = _instrument(db, kb.id, new_doc, family_id=family.id, effective_from=None, enacted_year=2023)
        db.commit()
        resolve_instrument_statuses(db, kb.id)
        db.commit(); db.refresh(old_row); db.refresh(new_row)
        assert old_row.status == "superseded"
        assert new_row.status != "superseded"


def test_upsert_legal_instrument_preserves_an_explicit_zero_confidence_reference():
    with SessionLocal() as db:
        kb = KnowledgeBase(code="registry-zero-confidence", name="Registry zero confidence")
        db.add(kb); db.flush()
        source_doc = _legal_document(db, kb.id, title="Source act", checksum="e1" * 32,
                                     references=[{"relationship": "REFERS_TO", "target_title": "Target act",
                                                 "evidence_quote": "อ้างถึง", "confidence": 0.0}])
        target_doc = _legal_document(db, kb.id, title="Target act", checksum="e2" * 32)
        services.upsert_legal_instrument(db, source_doc)
        services.upsert_legal_instrument(db, target_doc)
        services.sync_legal_document_graph(db, source_doc)
        services.sync_legal_document_graph(db, target_doc)
        db.commit()
        services.build_legal_cross_document_suggestions(db, kb.id)
        db.commit()
        relation = db.query(LegalInstrumentRelation).filter_by(knowledge_base_id=kb.id, relation="REFERS_TO").first()
        assert relation is not None
        assert relation.confidence == 0.0


def test_update_legal_instrument_rejects_a_family_id_from_another_knowledge_base():
    test_client = next(client())
    kb_a = test_client.post("/api/v1/knowledge-bases", json={"name": "KB A", "code": "kb-scope-a"}).json()
    kb_b = test_client.post("/api/v1/knowledge-bases", json={"name": "KB B", "code": "kb-scope-b"}).json()
    with SessionLocal() as db:
        doc_a = _legal_document(db, kb_a["id"], title="Act A", checksum="f1" * 32)
        family_b = LegalFamily(knowledge_base_id=kb_b["id"], base_title="Family B", normalized_key="family-b")
        db.add(family_b); db.flush()
        instrument_a = services.upsert_legal_instrument(db, doc_a)
        db.commit()
        instrument_id, foreign_family_id = instrument_a.id, family_b.id
    response = test_client.patch(f"/api/v1/legal-instruments/{instrument_id}", json={"family_id": foreign_family_id})
    assert response.status_code == 400


def test_update_legal_instrument_rejects_an_invalid_kind():
    test_client = next(client())
    kb = test_client.post("/api/v1/knowledge-bases", json={"name": "Kind validation", "code": "kind-validation"}).json()
    with SessionLocal() as db:
        doc = _legal_document(db, kb["id"], title="Act", checksum="f2" * 32)
        instrument = services.upsert_legal_instrument(db, doc)
        db.commit()
        instrument_id = instrument.id
    response = test_client.patch(f"/api/v1/legal-instruments/{instrument_id}", json={"kind": "not-a-real-kind"})
    assert response.status_code == 422
