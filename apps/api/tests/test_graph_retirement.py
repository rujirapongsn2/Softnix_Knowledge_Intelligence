"""Retiring graph facts: bookkeeping stays right and Neo4j hears about every removal."""
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import services
from app.models import (Base, Document, Entity, EntitySource, GraphProjectionEvent, KnowledgeBase,
                        Relationship, RelationshipSource)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(KnowledgeBase(id="kb-1", name="KB", code="kb-graph"))
    for doc_id in ("doc-a", "doc-b"):
        session.add(Document(id=doc_id, knowledge_base_id="kb-1", original_filename=f"{doc_id}.md", stored_filename=f"{doc_id}.md",
                             storage_path=f"/tmp/{doc_id}.md", mime_type="text/markdown", file_size=1,
                             checksum_sha256=doc_id.ljust(64, "0"), title=doc_id))
    session.commit()
    yield session
    session.close()


def make_entity(db, name, *, origin, sources=()):
    row = Entity(knowledge_base_id="kb-1", name=name, canonical_name=name.casefold(), entity_type="concept",
                 identity_key=f"k:{name}", origin=origin, source_count=len(sources))
    db.add(row)
    db.flush()
    for document_id in sources:
        db.add(EntitySource(entity_id=row.id, document_id=document_id, excerpt="evidence"))
    return row


def make_relationship(db, source, target, *, origin, sources=()):
    row = Relationship(knowledge_base_id="kb-1", source_entity_id=source.id, target_entity_id=target.id,
                       relationship_type="RELATED_TO", origin=origin, source_count=len(sources))
    db.add(row)
    db.flush()
    for document_id in sources:
        db.add(RelationshipSource(relationship_id=row.id, document_id=document_id, excerpt="evidence"))
    return row


def projected(db):
    return {(event.event_type, event.entity_id or event.relationship_id) for event in db.query(GraphProjectionEvent)}


@pytest.mark.parametrize("origin,remove", [
    ("metadata", services._remove_metadata_graph_projection),
    ("legal_schema", services._remove_document_legal_projection),
])
def test_regenerating_a_documents_graph_projection_retires_and_projects_the_facts_it_alone_supported(db, origin, remove):
    only_a = make_entity(db, "only-a", origin=origin, sources=["doc-a"])
    shared = make_entity(db, "shared", origin=origin, sources=["doc-a", "doc-b"])
    manual = make_entity(db, "manual", origin="manual", sources=["doc-a"])
    edge_only_a = make_relationship(db, only_a, shared, origin=origin, sources=["doc-a"])
    edge_shared = make_relationship(db, shared, manual, origin=origin, sources=["doc-a", "doc-b"])
    db.commit()

    remove(db, db.get(Document, "doc-a"))
    db.commit()
    db.expire_all()

    assert db.get(Entity, only_a.id).deleted_at and db.get(Relationship, edge_only_a.id).deleted_at
    assert db.get(Entity, shared.id).deleted_at is None and db.get(Entity, shared.id).source_count == 1
    assert db.get(Relationship, edge_shared.id).deleted_at is None and db.get(Relationship, edge_shared.id).source_count == 1
    assert db.get(Entity, manual.id).deleted_at is None
    assert {("entity", only_a.id), ("relationship", edge_only_a.id)} <= projected(db)
    assert ("entity", shared.id) not in projected(db) and ("relationship", edge_shared.id) not in projected(db)


def test_explicitly_deleting_an_entity_projects_the_removal_of_its_edges_too(db):
    from app.graph_retirement import delete_entity_with_edges

    left, right = make_entity(db, "left", origin="manual"), make_entity(db, "right", origin="manual")
    edge = make_relationship(db, left, right, origin="manual")
    db.commit()
    delete_entity_with_edges(db, left)
    db.commit()
    assert db.get(Relationship, edge.id).deleted_at and db.get(Entity, left.id).deleted_at and db.get(Entity, right.id).deleted_at is None
    assert {("entity", left.id), ("relationship", edge.id)} <= projected(db)


def test_deleting_a_relationship_through_the_api_removes_it_from_neo4j():
    from fastapi.testclient import TestClient

    from app.db import SessionLocal
    from app.main import app

    with TestClient(app) as client:
        assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "correct-horse-battery-staple"}).status_code == 200
        kb = client.post("/api/v1/knowledge-bases", json={"name": "Graph delete", "code": f"graph-delete-{uuid.uuid4().hex[:8]}"}).json()
        source = client.post(f"/api/v1/knowledge-bases/{kb['id']}/entities", json={"name": "A", "entity_type": "Concept"}).json()
        target = client.post(f"/api/v1/knowledge-bases/{kb['id']}/entities", json={"name": "B", "entity_type": "Concept"}).json()
        relationship = client.post(f"/api/v1/knowledge-bases/{kb['id']}/relationships", json={
            "source_entity_id": source["id"], "target_entity_id": target["id"], "relationship_type": "RELATED_TO"}).json()
        assert client.delete(f"/api/v1/relationships/{relationship['id']}").status_code == 200
        with SessionLocal() as session:
            events = session.query(GraphProjectionEvent).filter_by(relationship_id=relationship["id"]).order_by(GraphProjectionEvent.created_at).all()
            assert len(events) >= 2  # the create projection, then the removal
            assert session.get(Relationship, relationship["id"]).deleted_at is not None
