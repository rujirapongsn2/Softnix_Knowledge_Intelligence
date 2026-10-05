"""Permanent purge of deleted documents: content, graph, files, restore guard."""
import uuid
from pathlib import Path
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import services
from app.config import get_settings
from app.document_purge import PurgeBlocked, purge_deleted_documents, purge_document
from app.models import (Base, Document, DocumentChunk, DocumentMetadataValue, Entity, EntitySource, GraphProjectionEvent,
                        KnowledgeBase, LegalInstrument, LegalInstrumentRelation, ProcessingJob, Relationship, RelationshipSource)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(KnowledgeBase(id="kb-1", name="KB", code="kb-purge"))
    session.commit()
    yield session
    session.close()


def stored_file(name, content="x"):
    path = get_settings().file_root / "kb-1" / f"{uuid.uuid4()}-{name}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def make_doc(db, doc_id, *, deleted_days_ago=None, with_reference=False):
    original = stored_file(f"{doc_id}.md")
    reference = stored_file(f"{doc_id}.pdf") if with_reference else None
    doc = Document(id=doc_id, knowledge_base_id="kb-1", original_filename=f"{doc_id}.md", stored_filename=original.name,
                   storage_path=str(original), mime_type="text/markdown", file_size=1, checksum_sha256=doc_id.ljust(64, "0"),
                   title=doc_id, status="deleted" if deleted_days_ago is not None else "completed", extracted_text="flood report",
                   reference_file_path=str(reference) if reference else None,
                   deleted_at=datetime.utcnow() - timedelta(days=deleted_days_ago) if deleted_days_ago is not None else None)
    db.add(doc)
    db.add(DocumentChunk(document_id=doc_id, knowledge_base_id="kb-1", chunk_index=0, content="flood", content_sha256="c" * 64,
                         char_start=0, char_end=5, token_count=1))
    db.add(DocumentMetadataValue(document_id=doc_id, knowledge_base_id="kb-1", field_key="k", value_text="v"))
    db.add(ProcessingJob(document_id=doc_id, knowledge_base_id="kb-1", status="completed"))
    db.commit()
    return doc


def entity(db, name, *, origin="lightrag", sources=(), identity_key=None):
    row = Entity(knowledge_base_id="kb-1", name=name, canonical_name=name.casefold(), entity_type="concept",
                 identity_key=identity_key or f"generic:{name}", origin=origin, source_count=len(sources))
    db.add(row)
    db.flush()
    for document_id in sources:
        db.add(EntitySource(entity_id=row.id, document_id=document_id, excerpt="evidence"))
    return row


def relationship(db, source, target, *, origin="lightrag", sources=()):
    row = Relationship(knowledge_base_id="kb-1", source_entity_id=source.id, target_entity_id=target.id,
                       relationship_type="RELATED_TO", origin=origin, source_count=len(sources))
    db.add(row)
    db.flush()
    for document_id in sources:
        db.add(RelationshipSource(relationship_id=row.id, document_id=document_id, excerpt="evidence"))
    return row


class FakeEngine:
    enabled = True

    def __init__(self, remote=None):
        self.remote, self.deleted = remote, []

    def find_document(self, document_id, knowledge_base_id):
        return self.remote

    def delete_remote_document(self, remote_id):
        self.deleted.append(remote_id)
        self.remote = None


def test_purge_removes_content_files_and_keeps_a_tombstone(db):
    doc = make_doc(db, "gone", deleted_days_ago=40, with_reference=True)
    files = [str(Path(doc.storage_path).resolve()), str(Path(doc.reference_file_path).resolve())]
    result = purge_document(db, doc, engine=FakeEngine())
    db.expire_all()
    doc = db.get(Document, "gone")
    assert doc is not None and doc.purged_at is not None and doc.extracted_text is None
    assert db.query(DocumentChunk).filter_by(document_id="gone").count() == 0
    assert db.query(DocumentMetadataValue).filter_by(document_id="gone").count() == 0
    assert db.query(ProcessingJob).filter_by(document_id="gone").count() == 1  # history stays
    assert sorted(result["files_removed"]) == sorted(files)
    assert not any(Path(path).exists() for path in files)


def test_purge_retires_only_generated_facts_that_lose_their_last_source(db):
    make_doc(db, "gone", deleted_days_ago=40)
    make_doc(db, "kept")
    only_gone = entity(db, "SMEs", sources=["gone"])
    shared = entity(db, "Bangkok", sources=["gone", "kept"])
    manual = entity(db, "Hand made", origin="manual", sources=["gone"])
    legal = entity(db, "Provision 15", origin="legal_schema", sources=["gone"], identity_key="legal:provision:gone:15")
    edge_only_gone = relationship(db, only_gone, shared, sources=["gone"])
    edge_shared = relationship(db, shared, manual, sources=["gone", "kept"])
    db.commit()
    result = purge_document(db, db.get(Document, "gone"))
    db.expire_all()
    assert db.get(Entity, only_gone.id).deleted_at and db.get(Entity, legal.id).deleted_at
    assert db.get(Entity, shared.id).deleted_at is None and db.get(Entity, shared.id).source_count == 1
    assert db.get(Entity, manual.id).deleted_at is None and db.get(Entity, manual.id).source_count == 0
    assert db.get(Relationship, edge_only_gone.id).deleted_at and db.get(Relationship, edge_shared.id).deleted_at is None
    assert result["entities_retired"] == 2
    # edge_only_gone is retired once even though both of its endpoints are visited
    assert result["relationships_retired"] == 1
    assert db.query(GraphProjectionEvent).filter_by(relationship_id=edge_only_gone.id).count() == 1
    projected = {(e.event_type, e.entity_id or e.relationship_id) for e in db.query(GraphProjectionEvent)}
    assert {("entity", only_gone.id), ("entity", legal.id), ("relationship", edge_only_gone.id)} <= projected
    assert db.query(EntitySource).filter_by(document_id="gone").count() == 0


def test_purge_removes_legal_instruments_and_their_relations(db):
    gone, kept = make_doc(db, "gone", deleted_days_ago=40), make_doc(db, "kept")
    old = LegalInstrument(document_id=gone.id, knowledge_base_id="kb-1", official_title="old")
    new = LegalInstrument(document_id=kept.id, knowledge_base_id="kb-1", official_title="new")
    db.add_all([old, new])
    db.flush()
    db.add(LegalInstrumentRelation(knowledge_base_id="kb-1", source_instrument_id=new.id, target_instrument_id=old.id, relation="AMENDS"))
    db.commit()
    purge_document(db, gone)
    assert db.query(LegalInstrument).filter_by(document_id="gone").count() == 0
    assert db.query(LegalInstrumentRelation).count() == 0 and db.query(LegalInstrument).filter_by(document_id="kept").count() == 1


def test_purge_finishes_a_pending_remote_purge_and_refuses_unsafe_states(db):
    doc = make_doc(db, "gone", deleted_days_ago=40)
    engine = FakeEngine(remote={"id": "doc-remote"})
    db.add(ProcessingJob(document_id="gone", knowledge_base_id="kb-1", job_type="PURGE_REMOTE_INDEX", status="queued"))
    db.commit()
    purge_document(db, doc, engine=engine)
    assert engine.deleted == ["doc-remote"]

    live = make_doc(db, "live")
    with pytest.raises(PurgeBlocked):
        purge_document(db, live)
    busy = make_doc(db, "busy", deleted_days_ago=40)
    db.add(ProcessingJob(document_id="busy", knowledge_base_id="kb-1", status="running"))
    db.commit()
    with pytest.raises(PurgeBlocked):
        purge_document(db, busy)


def test_files_outside_the_storage_root_are_never_deleted(db, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("keep me")
    doc = make_doc(db, "gone", deleted_days_ago=40)
    doc.storage_path = str(outside)
    db.commit()
    assert purge_document(db, doc)["files_removed"] == [] and outside.exists()


def test_purge_is_idempotent_and_retries_leftover_files(db):
    doc = make_doc(db, "gone", deleted_days_ago=40)
    purge_document(db, doc)
    leftover = Path(doc.storage_path).resolve()
    leftover.write_text("came back")  # e.g. the first unlink failed
    result = purge_document(db, db.get(Document, "gone"))
    assert result["files_removed"] == [str(leftover)] and result["chunks_removed"] == 0


def test_batch_respects_age_and_dry_run(db):
    make_doc(db, "old", deleted_days_ago=40)
    make_doc(db, "recent", deleted_days_ago=1)
    report = purge_deleted_documents(db, min_age_days=30)
    assert [c["document_id"] for c in report["candidates"]] == ["old"] and report["purged"] == []
    assert db.get(Document, "old").purged_at is None
    report = purge_deleted_documents(db, min_age_days=0, apply=True)
    assert sorted(p["document_id"] for p in report["purged"]) == ["old", "recent"] and report["blocked"] == []


def test_deleted_rows_are_projected_as_removals(db, monkeypatch):
    calls = []

    class Store:
        enabled = True

        def delete_entity(self, entity_id):
            calls.append(("delete_entity", entity_id))

        def delete_relationship(self, relationship_id):
            calls.append(("delete_relationship", relationship_id))

        def upsert_entity(self, entity):
            calls.append(("upsert_entity", entity.id))

    monkeypatch.setattr(services, "Neo4jGraphStore", lambda: Store())
    gone = entity(db, "Gone")
    gone.deleted_at = datetime.utcnow()
    db.add(GraphProjectionEvent(event_type="entity", entity_id=gone.id))
    db.add(GraphProjectionEvent(event_type="relationship", relationship_id="missing-relationship"))
    db.commit()
    assert services.process_next_graph_projection(db) and services.process_next_graph_projection(db)
    assert calls == [("delete_entity", gone.id), ("delete_relationship", "missing-relationship")]


def test_restore_refuses_a_purged_document():
    from fastapi.testclient import TestClient

    from app.db import SessionLocal
    from app.main import app

    with TestClient(app) as client:
        assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "correct-horse-battery-staple"}).status_code == 200
        kb = client.post("/api/v1/knowledge-bases", json={"name": "Purge restore", "code": f"purge-restore-{uuid.uuid4().hex[:8]}"}).json()
        with SessionLocal() as session:
            doc = Document(knowledge_base_id=kb["id"], original_filename="a.md", stored_filename="a.md", storage_path="/tmp/none.md",
                           mime_type="text/markdown", file_size=1, checksum_sha256=uuid.uuid4().hex * 2, title="a", status="deleted",
                           deleted_at=datetime.utcnow(), purged_at=datetime.utcnow())
            session.add(doc)
            session.commit()
            doc_id = doc.id
        response = client.post(f"/api/v1/documents/{doc_id}/restore")
        assert response.status_code == 409 and response.json()["error"]["code"] == "DOCUMENT_PURGED"
        page = client.get(f"/api/v1/knowledge-bases/{kb['id']}/documents/page?include_deleted=true").json()
        assert page["items"][0]["purged_at"] is not None


def test_a_document_is_live_deleted_or_purged_and_each_state_selects_the_right_rows(db):
    from app.models import DocumentLifecycle

    live, deleted, purged = make_doc(db, "live"), make_doc(db, "deleted", deleted_days_ago=1), make_doc(db, "purged", deleted_days_ago=1)
    purged.purged_at = datetime.utcnow()
    db.commit()
    assert [live.lifecycle, deleted.lifecycle, purged.lifecycle] == [DocumentLifecycle.LIVE, DocumentLifecycle.DELETED, DocumentLifecycle.PURGED]
    assert [live.is_live, deleted.is_live, purged.is_live] == [True, False, False]
    def selected(condition):
        return sorted(doc.id for doc in db.query(Document).filter(condition))

    assert selected(Document.live()) == ["live"]
    assert selected(Document.restorable()) == ["deleted"]


def test_a_file_that_cannot_be_removed_is_reported_without_blocking_the_database_purge(db, monkeypatch):
    doc = make_doc(db, "gone", deleted_days_ago=40)
    original_unlink = Path.unlink

    def refuse(self, *args, **kwargs):
        if self == Path(doc.storage_path).resolve():
            raise PermissionError("read-only")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse)
    report = purge_deleted_documents(db, min_age_days=0, apply=True)
    assert report["blocked"] == [] and report["purged"][0]["files_failed"] == [str(Path(doc.storage_path).resolve())]
    assert db.get(Document, "gone").purged_at is not None


def test_the_documents_page_status_filter_separates_deleted_from_live_documents():
    from fastapi.testclient import TestClient

    from app.db import SessionLocal
    from app.main import app

    with TestClient(app) as client:
        assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "correct-horse-battery-staple"}).status_code == 200
        kb = client.post("/api/v1/knowledge-bases", json={"name": "Status filter", "code": f"status-filter-{uuid.uuid4().hex[:8]}"}).json()
        with SessionLocal() as session:
            for name, status, deleted in (("live", "completed", False), ("gone", "deleted", True), ("purged", "deleted", True)):
                session.add(Document(knowledge_base_id=kb["id"], original_filename=f"{name}.md", stored_filename=f"{name}.md", storage_path="/tmp/none.md",
                                     mime_type="text/markdown", file_size=1, checksum_sha256=uuid.uuid4().hex * 2, title=name, status=status,
                                     deleted_at=datetime.utcnow() if deleted else None,
                                     purged_at=datetime.utcnow() if name == "purged" else None))
            session.commit()

        def titles(query):
            return [item["title"] for item in client.get(f"/api/v1/knowledge-bases/{kb['id']}/documents/page{query}").json()["items"]]

        assert titles("?status=deleted") == ["gone"]
        assert titles("?status=deleted&include_deleted=true") == ["gone"]
        assert titles("?status=completed&include_deleted=true") == ["live"]
        assert titles("") == ["live"]
        assert sorted(titles("?include_deleted=true")) == ["gone", "live", "purged"]
