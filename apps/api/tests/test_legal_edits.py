"""Legal extraction fills in what the system can read and never replaces what a person wrote."""
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import services
from app.legal_edits import keep_manual_edits, with_manual_edits
from app.models import Base, Document, KnowledgeBase, ProcessingJob
from app.openrouter import OpenRouterClient

TEXT = "พระราชบัญญัติตัวอย่าง พ.ศ. ๒๕๖๙\nมาตรา ๑ ให้ใช้บังคับ"


def test_saving_records_only_what_changed_and_the_record_only_grows():
    old = {"schema_version": 2, "instrument": {"official_title": "A", "number": "1"}, "note": "n"}
    saved = with_manual_edits(old, {**old, "instrument": {"official_title": "A", "number": "99"}})
    assert saved["provenance"]["manual_paths"] == ["instrument.number"]
    again = with_manual_edits(saved, {**saved, "note": "changed"})
    assert again["provenance"]["manual_paths"] == ["instrument.number", "note"]
    assert with_manual_edits(old, dict(old)) == old


def test_a_client_cannot_clear_the_record_by_sending_its_own_provenance():
    saved = with_manual_edits({"note": "a"}, {"note": "b"})
    resent = with_manual_edits(saved, {**saved, "provenance": {"manual_paths": []}})
    assert resent["provenance"]["manual_paths"] == ["note"]


def test_extraction_keeps_the_edited_paths_and_replaces_the_rest():
    previous = with_manual_edits({"instrument": {"official_title": "old", "number": "1"}, "note": "n"},
                                 {"instrument": {"official_title": "old", "number": "99"}, "note": "n"})
    extracted = {"instrument": {"official_title": "fresh", "number": "2"}, "change_events": ["e"], "provenance": {"source": "text"}}
    result = keep_manual_edits(extracted, previous)
    assert result["instrument"] == {"official_title": "fresh", "number": "99"}
    assert result["change_events"] == ["e"] and "note" not in result
    assert result["provenance"] == {"source": "text", "manual_paths": ["instrument.number"]}
    assert keep_manual_edits(extracted, None) == extracted


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(KnowledgeBase(id="kb-1", name="KB", code="kb-legal"))
    session.commit()
    yield session
    session.close()


def test_the_extraction_job_keeps_what_a_person_edited(db, monkeypatch):
    monkeypatch.setattr(OpenRouterClient, "extract_legal_metadata", lambda self, title, text: {})
    edited = with_manual_edits({}, {"instrument": {"number": "99/2569"}, "note": "reviewer note"})
    doc = Document(id=str(uuid.uuid4()), knowledge_base_id="kb-1", original_filename="a.md", stored_filename="a.md", storage_path="/tmp/a.md",
                   mime_type="text/markdown", file_size=1, checksum_sha256="a" * 64, title="a", status="completed",
                   extracted_text=TEXT, legal_metadata=edited)
    job = ProcessingJob(document_id=doc.id, knowledge_base_id="kb-1", job_type="EXTRACT_LEGAL_METADATA")
    db.add_all([doc, job])
    db.commit()
    services._extract_legal_metadata(db, job, doc, TEXT)
    db.expire_all()
    stored = db.get(Document, doc.id).legal_metadata
    assert stored["instrument"]["number"] == "99/2569" and stored["note"] == "reviewer note"
    assert stored["schema_version"] == 2 and stored["instrument"]["official_title"]


def test_saving_legal_metadata_through_the_api_records_the_edit():
    from fastapi.testclient import TestClient

    from app.db import SessionLocal
    from app.main import app

    with TestClient(app) as api:
        assert api.post("/api/v1/auth/login", json={"username": "admin", "password": "correct-horse-battery-staple"}).status_code == 200
        kb = api.post("/api/v1/knowledge-bases", json={"name": "Legal edits", "code": f"legal-edits-{uuid.uuid4().hex[:8]}"}).json()
        with SessionLocal() as session:
            doc = Document(knowledge_base_id=kb["id"], original_filename="a.md", stored_filename="a.md", storage_path="/tmp/none.md", mime_type="text/markdown",
                           file_size=1, checksum_sha256=uuid.uuid4().hex * 2, title="a", status="completed", legal_metadata={"note": "first"})
            session.add(doc)
            session.commit()
            doc_id = doc.id
        put = api.put(f"/api/v1/documents/{doc_id}/legal-metadata", json={"metadata": {"note": "second"}}).json()["legal_metadata"]
        assert put["provenance"]["manual_paths"] == ["note"]
        patched = api.patch(f"/api/v1/documents/{doc_id}/legal-metadata", json={"metadata": {"label": "x"}}).json()["legal_metadata"]
        assert patched["provenance"]["manual_paths"] == ["note", "label"] and patched["note"] == "second"
