"""AI Query Readiness scores document details from the same field review states the Document details quality tab shows."""
from test_api import client
from test_auto_metadata import field, queue_and_run, setup_document
from test_metadata_usable_first import extract, review
from app import data_quality
from app.db import SessionLocal
from app.models import Document
from app.llm import LLMClient

METADATA_WARNINGS = {"metadata_incomplete", "required_metadata_missing", "metadata_unverified", "metadata_needs_attention",
                     "metadata_evidence_conflict", "metadata_consistency_conflict"}


def quality(api, doc_id):
    return api.get(f"/api/v1/documents/{doc_id}/text").json()["quality"]


def next_actions(api, kb, doc_id):
    report = api.get(f"/api/v1/knowledge-bases/{kb['id']}/metadata-quality").json()
    return [action for action in report["next_actions"] if action["document_id"] == doc_id]


def test_a_field_resolved_as_not_in_the_document_counts_as_covered(monkeypatch):
    api = next(client())
    _, _, doc_id = setup_document(api, [field("issuer", required=False), field("office")])
    extract(monkeypatch, doc_id)
    review(api, doc_id, "office", "not_found")
    preview = api.get(f"/api/v1/documents/{doc_id}/text").json()
    assert preview["metadata_field_states"] == {"issuer": "not_found", "office": "not_found"}
    assert preview["metadata_observations"]["office"]["status"] == "not_found_confirmed"
    assert preview["quality"]["dimensions"]["metadata"] == 100
    assert not METADATA_WARNINGS & set(preview["quality"]["warnings"])


def test_a_blocked_value_kept_from_an_older_source_is_not_counted_as_populated(monkeypatch):
    api = next(client())
    _, _, doc_id = setup_document(api)
    extract(monkeypatch, doc_id, issuer=["Land Dept"])
    with SessionLocal() as db:
        db.get(Document, doc_id).extracted_text = "Issuer: Land Department. Issuer: Land Office."
        db.commit()
    monkeypatch.setattr(LLMClient, "extract_document_metadata", lambda self, fields, text: {"issuer": [
        {"value": "Land Department", "evidence_quote": "Issuer: Land Department."}, {"value": "Land Office", "evidence_quote": "Issuer: Land Office."}]})
    queue_and_run(doc_id)
    preview = api.get(f"/api/v1/documents/{doc_id}/text").json()
    assert preview["document_metadata"] == {"issuer": "Land Dept"} and preview["metadata_field_states"] == {"issuer": "blocked"}
    report = preview["quality"]
    assert {"required_metadata_missing", "metadata_evidence_conflict"} <= set(report["warnings"])
    assert (report["metrics"]["metadata_populated"], report["metrics"]["metadata_conflicts"]) == (0, ["issuer"])


def test_a_value_that_disagrees_with_the_legal_metadata_is_a_next_action_with_both_values(monkeypatch):
    api = next(client())
    effective_date = {"key": "effective_date", "label": "Effective date", "field_type": "date", "required": False, "filterable": True}
    kb, _, doc_id = setup_document(api, [effective_date, field("issuer")], {"effective_date": "2026-09-08"})
    extract(monkeypatch, doc_id, issuer=["Land Dept"])
    with SessionLocal() as db:
        db.get(Document, doc_id).legal_metadata = {"effective_date": "2026-10-01"}
        db.commit()
    actions = [(a["field_key"], a["label"], a["state"], a["value"], a.get("legal_value")) for a in next_actions(api, kb, doc_id)]
    # The disagreement needs a person now, so it comes before the unchecked AI value.
    assert actions == [("effective_date", "Effective date", "inconsistent", "2026-09-08", "2026-10-01"), ("issuer", "issuer", "unverified", "Land Dept", None)]
    report = quality(api, doc_id)
    assert "metadata_consistency_conflict" in report["warnings"] and report["metrics"]["metadata_conflicts"] == ["effective_date"]


def test_resolving_every_field_the_quality_tab_lists_brings_the_readiness_metadata_dimension_to_its_maximum(monkeypatch):
    api = next(client())
    kb, _, doc_id = setup_document(api, [field("issuer"), field("office"), field("note", required=False)])
    extract(monkeypatch, doc_id, issuer=["Land Dept"], office=["A", "B"])
    before = quality(api, doc_id)
    assert [(a["field_key"], a["state"]) for a in next_actions(api, kb, doc_id)] == [("office", "blocked"), ("issuer", "unverified")]
    assert before["dimensions"]["metadata"] == 51 and "required_metadata_missing" in before["warnings"]
    review(api, doc_id, "issuer", "confirm")
    review(api, doc_id, "office", "set", value="Land Office")
    after = quality(api, doc_id)
    assert next_actions(api, kb, doc_id) == []
    assert after["dimensions"]["metadata"] == 100 and not METADATA_WARNINGS & set(after["warnings"])


def test_a_report_cached_under_an_older_scoring_is_rebuilt(monkeypatch):
    api = next(client())
    kb, _, doc_id = setup_document(api)
    monkeypatch.setattr(data_quality, "SCORING_VERSION", data_quality.SCORING_VERSION - 1)
    api.get(f"/api/v1/knowledge-bases/{kb['id']}/quality-readiness")
    with SessionLocal() as db:
        document = db.get(Document, doc_id)
        document.quality_report = {**document.quality_report, "score": -1}
        db.commit()
    monkeypatch.undo()
    rows = api.get(f"/api/v1/knowledge-bases/{kb['id']}/quality-readiness").json()["documents"]
    assert [row["score"] for row in rows if row["document_id"] == doc_id] == [quality(api, doc_id)["score"]]


def test_the_same_effective_date_written_in_buddhist_era_or_thai_words_is_not_a_conflict():
    from types import SimpleNamespace
    from app.metadata_extraction import metadata_consistency_conflicts

    def conflicts(document_value, legal_value):
        return metadata_consistency_conflicts(SimpleNamespace(document_metadata={"effective_date": document_value},
                                                              legal_metadata={"effective_date": legal_value}))
    assert conflicts("2014-12-01", "2557-12-01") == {}
    assert conflicts("1954-12-01", "๑ ธันวาคม พ.ศ. ๒๔๙๗") == {}
    assert conflicts("2018-05-11", "๒๕๖๑-๐๕-๑๑") == {}
    assert conflicts("2018-05-14", "๒๕๖๑-๐๕-๑๑") == {"effective_date": "๒๕๖๑-๐๕-๑๑"}
