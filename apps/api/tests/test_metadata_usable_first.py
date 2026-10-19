"""AI-extracted metadata is usable before a person checks it, carries its trust, and guides the review."""
import hashlib

import pytest

from test_api import client
from test_auto_metadata import field, queue_and_run, setup_document
from app.db import SessionLocal
from app.metadata_extraction import trust_of
from app.metadata_search import decorate_sources
from app.models import Document, DocumentMetadataValue
from app.openrouter import OpenRouterClient


def answer(*pairs):
    return lambda self, fields, text: {"issuer": [{"value": value, "evidence_quote": quote} for value, quote in pairs]}


def page(api, kb, metadata_status):
    return [d["id"] for d in api.get(f"/api/v1/knowledge-bases/{kb['id']}/documents/page?metadata_status={metadata_status}").json()["items"]]


@pytest.mark.parametrize("status, trust", [("human_verified", "verified"), ("legacy_manual", "verified"), (None, "verified"),
                                           ("auto_accepted", "auto_verified"), ("suggested", "unverified"), ("partial", "unverified"),
                                           ("conflict", "blocked"), ("invalid_evidence", "blocked")])
def test_each_observation_status_gives_its_value_one_trust(status, trust):
    assert trust_of({"status": status} if status else {}) == trust


def test_a_single_suggested_value_is_in_use_filterable_and_labelled_unverified_for_answers(monkeypatch):
    api = next(client())
    kb, _, doc_id = setup_document(api)
    # "Land Dept" is not written verbatim in its quote, so it cannot be auto-accepted.
    monkeypatch.setattr(OpenRouterClient, "extract_document_metadata", answer(("Land Dept", "Issuer: Land Department.")))
    queue_and_run(doc_id)
    preview = api.get(f"/api/v1/documents/{doc_id}/text").json()
    assert preview["metadata_observations"]["issuer"]["status"] == "suggested"
    assert preview["document_metadata"] == {"issuer": "Land Dept"} and preview["metadata_status"] == "unverified"
    assert "metadata_unverified" in preview["quality"]["warnings"] and preview["quality"]["status"] != "verified"
    with SessionLocal() as db:
        assert db.query(DocumentMetadataValue).filter_by(document_id=doc_id, field_key="issuer").one().value_text == "Land Dept"
        sources = [{"document_id": doc_id}]
        decorate_sources(db, sources, [kb["id"]])
    assert sources[0]["metadata_provenance"]["issuer"]["trust"] == "unverified"
    assert sources[0]["metadata_provenance"]["issuer"]["evidence"][0]["quote"] == "Issuer: Land Department."
    assert doc_id in page(api, kb, "needs_review") and doc_id not in page(api, kb, "needs_attention")


@pytest.mark.parametrize("pairs, status", [((("Land Department", "Issuer: Land Department."), ("Land Office", "Issuer: Land Department.")), "conflict"),
                                            ((("Land Department", "Issuer: Fisheries Department."),), "invalid_evidence")])
def test_conflicting_or_unbacked_values_are_not_used_and_the_document_needs_attention(monkeypatch, pairs, status):
    api = next(client())
    kb, _, doc_id = setup_document(api)
    monkeypatch.setattr(OpenRouterClient, "extract_document_metadata", answer(*pairs))
    queue_and_run(doc_id)
    preview = api.get(f"/api/v1/documents/{doc_id}/text").json()
    assert preview["metadata_observations"]["issuer"]["status"] == status
    assert preview["document_metadata"] == {} and preview["metadata_field_states"] == {"issuer": "blocked"}
    assert preview["metadata_status"] == "needs_attention" and "metadata_needs_attention" in preview["quality"]["warnings"]
    with SessionLocal() as db:
        assert db.query(DocumentMetadataValue).filter_by(document_id=doc_id).count() == 0
    assert doc_id in page(api, kb, "needs_review") and doc_id in page(api, kb, "needs_attention")


def test_a_document_is_complete_once_every_extracted_value_is_verified_or_auto_verified(monkeypatch):
    api = next(client())
    _, _, doc_id = setup_document(api, [field("issuer"), field("office")])
    monkeypatch.setattr(OpenRouterClient, "extract_document_metadata", lambda self, fields, text: {
        "issuer": [{"value": "Land Department", "evidence_quote": "Issuer: Land Department."}],
        "office": [{"value": "Land Office", "evidence_quote": "Issuer: Land Department."}]})
    queue_and_run(doc_id)
    preview = api.get(f"/api/v1/documents/{doc_id}/text").json()
    assert preview["metadata_field_states"] == {"issuer": "auto_verified", "office": "unverified"} and preview["metadata_status"] == "unverified"
    review = {"revision": preview["metadata_revision"], "field_key": "office", "action": "confirm"}
    assert api.post(f"/api/v1/documents/{doc_id}/metadata-review", json=review).json()["metadata_status"] == "complete"


def test_reevaluation_puts_stored_single_suggestions_into_use_once():
    api = next(client())
    kb, _, doc_id = setup_document(api, [field("issuer"), field("office")])
    with SessionLocal() as db:
        document = db.get(Document, doc_id)
        text = document.extracted_text
        evidence = {"quote": text, "char_start": 0, "char_end": len(text)}
        base = {"origin": "document_extraction", "content_version": hashlib.sha256(text.encode()).hexdigest(), "coverage": "full",
                "locked": False, "template_version": document.metadata_template_version}
        document.metadata_observations = {
            "issuer": {**base, "status": "suggested", "candidates": [{"value": "Land Dept", "evidence": evidence}]},
            "office": {**base, "status": "conflict", "candidates": [{"value": "A", "evidence": evidence}, {"value": "B", "evidence": evidence}]}}
        document.metadata_status = "needs_attention"
        db.commit()
    assert api.post(f"/api/v1/knowledge-bases/{kb['id']}/metadata-reevaluate").json() == {"documents": 1, "fields": 1}
    preview = api.get(f"/api/v1/documents/{doc_id}/text").json()
    assert preview["document_metadata"] == {"issuer": "Land Dept"} and preview["metadata_status"] == "needs_attention"
    with SessionLocal() as db:
        assert db.query(DocumentMetadataValue).filter_by(document_id=doc_id, field_key="issuer").count() == 1
    assert api.post(f"/api/v1/knowledge-bases/{kb['id']}/metadata-reevaluate").json() == {"documents": 0, "fields": 0}
    assert api.get(f"/api/v1/documents/{doc_id}/text").json()["metadata_revision"] == preview["metadata_revision"]


def test_the_review_state_migration_keeps_every_waiting_document_in_front_of_a_person():
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, text
    spec = importlib.util.spec_from_file_location("review_states", Path(__file__).parents[1] / "migrations/versions/0040_metadata_review_states.py")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with create_engine("sqlite://").begin() as connection:
        connection.execute(text("CREATE TABLE documents (id TEXT PRIMARY KEY, metadata_status TEXT)"))
        connection.execute(text("INSERT INTO documents VALUES ('a', 'needs_review'), ('b', 'complete'), ('c', 'failed')"))
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        assert dict(connection.execute(text("SELECT id, metadata_status FROM documents")).all()) == {"a": "needs_attention", "b": "complete", "c": "failed"}
        migration.downgrade()
        assert connection.execute(text("SELECT metadata_status FROM documents WHERE id = 'a'")).scalar() == "needs_review"

