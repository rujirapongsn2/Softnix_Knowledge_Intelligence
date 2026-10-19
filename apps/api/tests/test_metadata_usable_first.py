"""AI-extracted metadata is usable before a person checks it, carries its trust, and guides the review."""
import hashlib

import pytest

from test_api import client
from test_auto_metadata import field, queue_and_run, setup_document
from app.db import SessionLocal
from app.metadata_extraction import trust_of
from app.metadata_search import decorate_sources
from app.models import Document, DocumentMetadataValue, ProcessingJob, Relationship
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


def test_a_value_kept_from_an_older_source_is_shown_but_not_used_once_the_new_extraction_conflicts(monkeypatch):
    api = next(client())
    kb, _, doc_id = setup_document(api)
    monkeypatch.setattr(OpenRouterClient, "extract_document_metadata", answer(("Land Dept", "Issuer: Land Department.")))
    queue_and_run(doc_id)
    with SessionLocal() as db:
        db.get(Document, doc_id).extracted_text = "Issuer: Land Department. Issuer: Land Office."
        db.commit()
    monkeypatch.setattr(OpenRouterClient, "extract_document_metadata", answer(("Land Department", "Issuer: Land Department."),
                                                                              ("Land Office", "Issuer: Land Office.")))
    queue_and_run(doc_id)
    preview = api.get(f"/api/v1/documents/{doc_id}/text").json()
    assert preview["document_metadata"] == {"issuer": "Land Dept"} and preview["metadata_field_states"] == {"issuer": "blocked"}
    with SessionLocal() as db:
        assert db.query(DocumentMetadataValue).filter_by(document_id=doc_id).count() == 0
        assert "Land Dept" not in db.get(Document, doc_id).metadata_search_text
        sources = [{"document_id": doc_id}]
        decorate_sources(db, sources, [kb["id"]])
    assert sources[0]["document_metadata"] == {} and sources[0]["metadata_provenance"] == {}


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


def add_document(api, kb, template, title):
    response = api.post(f"/api/v1/knowledge-bases/{kb['id']}/documents", data={"template_id": template["id"], "title": title},
                        files={"file": (f"{title}.txt", f"Issuer: Land Department. {title} {kb['id']}".encode(), "text/plain")})
    assert response.status_code == 200, response.text
    doc_id = response.json()["document_id"]
    with SessionLocal() as db:
        document = db.get(Document, doc_id)
        document.extracted_text, document.status, document.title = "Issuer: Land Department.", "completed", title
        db.query(ProcessingJob).filter_by(document_id=doc_id).update({"status": "completed"})
        db.commit()
    return doc_id


def extract(monkeypatch, doc_id, **answers):
    quote = "Issuer: Land Department."
    monkeypatch.setattr(OpenRouterClient, "extract_document_metadata",
                        lambda self, fields, text: {key: [{"value": value, "evidence_quote": quote} for value in values] for key, values in answers.items()})
    queue_and_run(doc_id)


def review(api, doc_id, field_key, action, **extra):
    revision = api.get(f"/api/v1/documents/{doc_id}/text").json()["metadata_revision"]
    response = api.post(f"/api/v1/documents/{doc_id}/metadata-review", json={"revision": revision, "field_key": field_key, "action": action, **extra})
    assert response.status_code == 200, response.text
    return api.get(f"/api/v1/documents/{doc_id}/text").json()["metadata_observations"][field_key]


def test_reviews_record_whether_the_ai_was_right_and_the_quality_view_ranks_what_to_fix(monkeypatch):
    api = next(client())
    kb, template, first = setup_document(api, [field("issuer"), field("office")])
    second, third, fourth = (add_document(api, kb, template, title) for title in ("b-second", "c-third", "d-fourth"))
    extract(monkeypatch, first, issuer=["Land Dept"], office=["A", "B"])
    extract(monkeypatch, second, issuer=["Land Dept"], office=["X"])
    extract(monkeypatch, third, issuer=["Land Dept", "Land Office"], office=["Y"])
    extract(monkeypatch, fourth, issuer=["Land Dept", "Land Office"], office=["Z"])
    corrected = review(api, first, "issuer", "set", value="Land Department")
    assert (corrected["review_outcome"], corrected["ai_value"]) == ("corrected", "Land Dept")
    assert review(api, second, "issuer", "confirm")["review_outcome"] == "confirmed"
    assert review(api, second, "office", "not_found")["review_outcome"] == "rejected"

    report = api.get(f"/api/v1/knowledge-bases/{kb['id']}/metadata-quality").json()
    # The field the AI gets wrong most often comes first, even though the other has more blocked values.
    office, issuer = report["fields"]
    assert (office["field_key"], office["ai_accuracy"], office["outcomes"]) == ("office", 0.0, {"confirmed": 0, "corrected": 0, "rejected": 1})
    assert {k: v for k, v in office["counts"].items() if v} == {"blocked": 1, "not_found": 1, "unverified": 2}
    assert (issuer["field_key"], issuer["ai_accuracy"]) == ("issuer", 0.5)
    assert {k: v for k, v in issuer["counts"].items() if v} == {"verified": 2, "blocked": 2}
    assert [(a["document_id"], a["field_key"], a["state"], a["value"]) for a in report["next_actions"]] == [
        (first, "office", "blocked", "A"), (third, "issuer", "blocked", "Land Dept"), (fourth, "issuer", "blocked", "Land Dept"),
        (third, "office", "unverified", "Y"), (fourth, "office", "unverified", "Z")]


def test_saving_the_whole_form_records_an_outcome_only_for_fields_not_reviewed_before(monkeypatch):
    api = next(client())
    _, _, doc_id = setup_document(api, [field("issuer"), field("office")])
    extract(monkeypatch, doc_id, issuer=["Land Dept"], office=["X"])
    review(api, doc_id, "office", "confirm")
    # A person changing their own earlier answer says nothing new about the AI.
    assert api.patch(f"/api/v1/documents/{doc_id}/metadata", json={"values": {"issuer": "Land Department", "office": "Z"}}).status_code == 200
    observations = api.get(f"/api/v1/documents/{doc_id}/text").json()["metadata_observations"]
    assert (observations["issuer"]["review_outcome"], observations["issuer"]["ai_value"]) == ("corrected", "Land Dept")
    assert observations["office"]["review_outcome"] == "confirmed"



def test_saving_the_form_marks_only_the_changed_field_as_reviewed(monkeypatch):
    api = next(client())
    kb, _, doc_id = setup_document(api, [field("issuer", graph_entity_type="Organization", graph_relationship="ISSUED_BY"),
                                         field("office", graph_entity_type="Office", graph_relationship="FILED_AT")])
    extract(monkeypatch, doc_id, issuer=["Land Dept"], office=["Land Office"])
    response = api.patch(f"/api/v1/documents/{doc_id}/metadata", json={"values": {"issuer": "Land Department", "office": "Land Office"}})
    assert response.status_code == 200, response.text
    preview = api.get(f"/api/v1/documents/{doc_id}/text").json()
    issuer, office = preview["metadata_observations"]["issuer"], preview["metadata_observations"]["office"]
    assert (issuer["status"], issuer["review_outcome"], issuer["locked"]) == ("human_verified", "corrected", True)
    assert office["status"] == "suggested" and "review_outcome" not in office and not office["locked"]
    assert preview["metadata_field_states"] == {"issuer": "verified", "office": "unverified"}
    with SessionLocal() as db:
        assert {r.relationship_type for r in db.query(Relationship).filter_by(knowledge_base_id=kb["id"], origin="metadata")} == {"ISSUED_BY"}
    office_quality = next(f for f in api.get(f"/api/v1/knowledge-bases/{kb['id']}/metadata-quality").json()["fields"] if f["field_key"] == "office")
    assert office_quality["outcomes"] == {"confirmed": 0, "corrected": 0, "rejected": 0} and office_quality["ai_accuracy"] is None


def test_removing_an_ai_value_from_the_form_rejects_it(monkeypatch):
    api = next(client())
    _, _, doc_id = setup_document(api, [field("issuer"), field("office")])
    extract(monkeypatch, doc_id, issuer=["Land Dept"], office=["Land Office"])
    assert api.patch(f"/api/v1/documents/{doc_id}/metadata", json={"values": {"office": "Land Office"}}).status_code == 200
    observations = api.get(f"/api/v1/documents/{doc_id}/text").json()["metadata_observations"]
    assert (observations["issuer"]["status"], observations["issuer"]["review_outcome"]) == ("not_found_confirmed", "rejected")
    assert observations["office"]["status"] == "suggested"
