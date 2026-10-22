"""Custom extraction, review, retrieval and authorization regression coverage."""
import hashlib
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import inspect

from test_api import client
from app.db import SessionLocal
from app.job_queue import claim_next_job
from app.metadata_extraction import _locate_quote, MAX_WINDOWS, WINDOW, WINDOW_OVERLAP, extract_candidates, field_review_states, process_metadata_job, queue_metadata_extraction
from app.metadata_search import MetadataPredicate, apply_typed_predicates, describe_schema
from app.models import Document, DocumentMetadataValue, ProcessingJob, Relationship
from app.llm import LLMClient
from app.services import process_next_job, sync_document_metadata_values


@pytest.fixture(autouse=True)
def cancel_leftover_metadata_jobs():
    """Keep these integration tests independent of the shared worker queue."""
    with SessionLocal() as db:
        existing_job_ids = None if not inspect(db.bind).has_table("processing_jobs") else {
            row.id for row in db.query(ProcessingJob.id).filter(
                ProcessingJob.job_type == "EXTRACT_DOCUMENT_METADATA",
            )
        }
    yield
    with SessionLocal() as db:
        if not inspect(db.bind).has_table("processing_jobs"):
            return
        rows = db.query(ProcessingJob).filter(
            ProcessingJob.job_type == "EXTRACT_DOCUMENT_METADATA",
            ProcessingJob.status.in_(["queued", "running"]),
        )
        if existing_job_ids:
            rows = rows.filter(ProcessingJob.id.not_in(existing_job_ids))
        rows.update({"status": "cancelled"}, synchronize_session=False)
        db.commit()


def field(key="issuer", **extra):
    return {"key": key, "label": key, "field_type": "text", "fill_mode": "extract", "required": True,
            "extraction_description": f"Extract the {key} stated in the document.",
            "searchable": True, "filterable": True, **extra}


def extractor(result):
    return SimpleNamespace(extract_document_metadata=lambda fields, text: result)


def test_extract_field_requires_an_ai_instruction():
    api = next(client())
    response = api.post("/api/v1/knowledge-bases", json={"name": "Field contract", "code": "field-contract"})
    kb = response.json()
    response = api.post(f"/api/v1/knowledge-bases/{kb['id']}/document-templates", json={
        "name": "Unclear extraction", "base_document_type": "general",
        "fields": [{"key": "issuer", "label": "Issuer", "field_type": "text", "fill_mode": "extract"}],
    })
    assert response.status_code == 422
    assert "extraction_description" in response.text


def test_fill_missing_does_not_reconfigure_manual_fields_for_ai():
    api = next(client())
    _, _, doc_id = setup_document(api, [{
        "key": "owner", "label": "Owner", "field_type": "text",
        "fill_mode": "manual", "required": False,
    }])
    response = api.post(f"/api/v1/documents/{doc_id}/metadata-extract", json={"enable_missing_fields": True})
    assert response.status_code == 200
    assert response.json()["queued"] is False
    preview = api.get(f"/api/v1/documents/{doc_id}/text").json()
    assert preview["metadata_template_fields"][0]["fill_mode"] == "manual"


def test_literal_acceptance_and_graph_review():
    text = "Issuer: Land Department"
    candidate = {"value": "Land Department", "evidence_quote": text}
    result = extract_candidates([field()], text, extractor({"issuer": [candidate]}))
    assert result["issuer"]["status"] == "auto_accepted"
    assert result["issuer"]["candidates"][0]["evidence"]["char_start"] == 0
    assert result["issuer"]["content_version"] == hashlib.sha256(text.encode()).hexdigest()
    result = extract_candidates([field(graph_relationship="ISSUED_BY")], text, extractor({"issuer": [candidate]}))
    assert result["issuer"]["status"] == "suggested"


def test_fabricated_evidence_conflicts_and_partial_never_auto_accept():
    text = "Issuer: A. Issuer: B."
    result = extract_candidates([field()], text, extractor({"issuer": [{"value": "A", "evidence_quote": "not in source"}]}))
    assert result["issuer"]["status"] == "invalid_evidence"
    result = extract_candidates([field()], text, extractor({"issuer": [{"value": "A", "evidence_quote": text}, {"value": "B", "evidence_quote": text}]}))
    assert result["issuer"]["status"] == "conflict"
    result = extract_candidates([field()], "A" * (WINDOW * MAX_WINDOWS + WINDOW_OVERLAP + 1), extractor({"issuer": [{"value": "A", "evidence_quote": "A"}]}))
    assert result["issuer"]["status"] == "partial" and result["issuer"]["coverage"] == "partial"


def test_a_document_the_last_window_reads_to_its_end_has_full_coverage():
    # The last window extends past MAX_WINDOWS * WINDOW by the overlap, so a document that fits there was read in full.
    answer = {"issuer": [{"value": "A", "evidence_quote": "A"}]}
    result = extract_candidates([field()], "A" * (WINDOW * MAX_WINDOWS + WINDOW_OVERLAP), extractor(answer))
    assert result["issuer"]["coverage"] == "full" and result["issuer"]["status"] == "auto_accepted"


def test_dates_and_boolean_false_are_reviewable_and_invalid_types_rejected():
    fields = [field("date", field_type="date"), field("flag", field_type="boolean"), field("amount", field_type="number")]
    text = "Date 2026-09-08. Flag: false. Amount: 3"
    result = extract_candidates(fields, text, extractor({
        "date": [{"value": "2026-09-08", "evidence_quote": text}],
        "flag": [{"value": False, "evidence_quote": text}],
        "amount": [{"value": "3", "evidence_quote": text}],
    }))
    assert result["date"]["status"] == "suggested"
    assert result["flag"]["candidates"][0]["value"] is False
    assert result["amount"]["status"] == "invalid_evidence"
    with pytest.raises(ValueError):
        MetadataPredicate(template_id="x", field_key="amount", field_type="number", values=[True])
    with pytest.raises(ValueError):
        MetadataPredicate(template_id="x", field_key="date", field_type="date", values=["2026-99-99"])


def setup_document(api, fields=None, values=None):
    suffix = uuid.uuid4().hex[:10]
    kb = api.post("/api/v1/knowledge-bases", json={"name": "Metadata " + suffix, "code": "meta-" + suffix}).json()
    api.post(f"/api/v1/knowledge-bases/{kb['id']}/activate")
    template = api.post(f"/api/v1/knowledge-bases/{kb['id']}/document-templates", json={"name": "Notice " + suffix, "fields": fields or [field()]}).json()
    import json
    response = api.post(f"/api/v1/knowledge-bases/{kb['id']}/documents", data={"template_id": template["id"], "metadata_json": json.dumps(values or {})}, files={"file": ("notice.txt", b"Issuer: Land Department. Date: 2026-09-08.", "text/plain")})
    assert response.status_code == 200, response.text
    doc_id = response.json()["document_id"]
    with SessionLocal() as db:
        document = db.get(Document, doc_id)
        document.extracted_text = "Issuer: Land Department. Date: 2026-09-08."
        document.status = "completed"
        # Isolate follow-up tests from other suites' global processing queue.
        db.query(ProcessingJob).filter_by(document_id=doc_id).update({"status": "completed"})
        db.commit()
    return kb, template, doc_id


def claim(db, job_id):
    """Claim the job the way the worker does. Other modules' leftover jobs are parked first (one shared database)."""
    db.query(ProcessingJob).filter(ProcessingJob.status.in_(["queued", "running"]), ProcessingJob.id != job_id).update({"status": "cancelled"}, synchronize_session=False)
    db.commit()
    job = claim_next_job(db)
    assert job is not None and job.id == job_id
    return job


def queue_and_run(doc_id):
    with SessionLocal() as db:
        assert queue_metadata_extraction(db, db.get(Document, doc_id))
        assert not queue_metadata_extraction(db, db.get(Document, doc_id))
        db.commit()
        job = db.query(ProcessingJob).filter_by(document_id=doc_id, job_type="EXTRACT_DOCUMENT_METADATA").order_by(ProcessingJob.created_at.desc()).first()
        process_metadata_job(db, claim(db, job.id))


def test_an_unverified_graph_field_is_in_use_but_reaches_the_graph_only_once_a_person_confirms_it(monkeypatch):
    api = next(client())
    kb, template, doc_id = setup_document(api, [field(graph_entity_type="Organization", graph_relationship="ISSUED_BY")])
    monkeypatch.setattr(LLMClient, "extract_document_metadata", lambda self, fields, text: {"issuer": [{"value": "Land Department", "evidence_quote": "Issuer: Land Department."}]})
    queue_and_run(doc_id)
    preview = api.get(f"/api/v1/documents/{doc_id}/text").json()
    assert preview["status"] == "completed" and preview["metadata_status"] == "unverified"
    assert preview["document_metadata"] == {"issuer": "Land Department"} and preview["metadata_field_states"] == {"issuer": "unverified"}
    with SessionLocal() as db:
        assert db.query(Relationship).filter_by(knowledge_base_id=kb["id"]).count() == 0
    review = {"revision": preview["metadata_revision"], "field_key": "issuer", "action": "confirm"}
    assert api.post(f"/api/v1/documents/{doc_id}/metadata-review", json=review).status_code == 200
    assert api.post(f"/api/v1/documents/{doc_id}/metadata-review", json=review).status_code == 409
    with SessionLocal() as db:
        assert db.query(Relationship).filter_by(knowledge_base_id=kb["id"], review_status="verified").count() == 1
        assert db.query(DocumentMetadataValue).filter_by(document_id=doc_id, value_text="Land Department").count() == 1
        assert not queue_metadata_extraction(db, db.get(Document, doc_id))


def test_an_optional_field_not_found_in_a_fully_read_document_leaves_nothing_to_review(monkeypatch):
    api = next(client())
    _, _, doc_id = setup_document(api, [field(required=False)])
    monkeypatch.setattr(LLMClient, "extract_document_metadata", lambda self, fields, text: {"issuer": []})
    queue_and_run(doc_id)
    preview = api.get(f"/api/v1/documents/{doc_id}/text").json()
    assert preview["metadata_observations"]["issuer"]["status"] == "not_found"
    assert preview["metadata_status"] == "complete" and preview["document_metadata"] == {}


@pytest.mark.parametrize("required, coverage, state", [(False, "partial", "unchecked"), (True, "full", "missing")])
def test_a_field_not_found_still_waits_for_a_person_when_the_read_was_partial_or_the_field_is_required(required, coverage, state):
    observations = {"issuer": {"status": "not_found", "origin": "document_extraction", "coverage": coverage, "locked": False}}
    assert field_review_states([field(required=required)], {}, observations) == {"issuer": state}


def test_failure_keeps_document_searchable(monkeypatch):
    api = next(client())
    _, _, doc_id = setup_document(api)
    def fail(*args):
        raise RuntimeError("LLM_API_KEY_NOT_CONFIGURED")
    monkeypatch.setattr(LLMClient, "extract_document_metadata", fail)
    queue_and_run(doc_id)
    preview = api.get(f"/api/v1/documents/{doc_id}/text").json()
    assert preview["status"] == "completed" and preview["metadata_status"] == "failed"


def test_concurrent_edit_discards_old_extraction(monkeypatch):
    api = next(client())
    _, _, doc_id = setup_document(api)
    def edit_while_extracting(*args):
        response = api.post(f"/api/v1/documents/{doc_id}/metadata-review", json={"revision": 0, "field_key": "issuer", "action": "set", "value": "Human choice"})
        assert response.status_code == 200
        return {"issuer": [{"value": "Land Department", "evidence_quote": "Issuer: Land Department."}]}
    monkeypatch.setattr(LLMClient, "extract_document_metadata", edit_while_extracting)
    queue_and_run(doc_id)
    preview = api.get(f"/api/v1/documents/{doc_id}/text").json()
    assert preview["document_metadata"]["issuer"] == "Human choice"
    with SessionLocal() as db:
        job = db.query(ProcessingJob).filter_by(document_id=doc_id, job_type="EXTRACT_DOCUMENT_METADATA").one()
        process_metadata_job(db, job)
        assert db.get(Document, doc_id).metadata_status == "complete"


def test_typed_filters_schema_scope_and_inventory():
    api = next(client())
    kb, template, doc_id = setup_document(api, [field("amount", field_type="number"), field("date", field_type="date")], {"amount": 100, "date": "2026-09-08"})
    other_kb, _, _ = setup_document(api)
    predicates = [MetadataPredicate(template_id=template["id"], field_key="amount", field_type="number", operator="gte", values=[20]), MetadataPredicate(template_id=template["id"], field_key="date", field_type="date", operator="lte", values=["2026-12-31"])]
    with SessionLocal() as db:
        sync_document_metadata_values(db, db.get(Document, doc_id)); db.commit()
        rows = apply_typed_predicates(db.query(Document.id).filter(Document.knowledge_base_id == kb["id"]), predicates).all()
        assert rows == [(doc_id,)]
        schema = describe_schema(db, [kb["id"]])
        assert all(t["knowledge_base_id"] == kb["id"] for t in schema["templates"])
        assert other_kb["id"] not in str(schema)
        assert schema["operators"]["select"] == ["eq", "in"]
        assert schema["operators"]["boolean"] == ["eq", "in"]
    token = api.post("/api/v1/tokens", json={"name": "Metadata agent", "allowed_knowledge_base_ids": [kb["id"]], "allowed_tools": ["describe_knowledge_schema", "document_inventory_summary"]}).json()
    headers = {"Authorization": "Bearer " + token["token"]}
    result = api.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "document_inventory_summary", "arguments": {"filters": {"metadata_predicates": [p.model_dump() for p in predicates]}}}}).json()
    assert result["result"]["structuredContent"]["total_documents"] == 1, result
    denied = api.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "search_knowledge", "arguments": {"query": "test"}}}).json()
    assert "error" in denied


def test_multi_select_upload_validation_indexing_and_typed_filters():
    import json

    api = next(client())
    values = {"categories": ["Policy", "Guidance"]}
    categories = field("categories", field_type="multi_select", options=["Policy", "Guidance", "Notice"])
    kb, template, doc_id = setup_document(api, [categories], values)

    # Arrays must contain distinct values from the field's configured options.
    for invalid_values in (["Unknown"], ["Policy", "Policy"]):
        response = api.post(
            f"/api/v1/knowledge-bases/{kb['id']}/documents",
            data={"template_id": template["id"], "metadata_json": json.dumps({"categories": invalid_values})},
            files={"file": ("invalid.txt", b"Category test", "text/plain")},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "DOCUMENT_METADATA_INVALID"

    with SessionLocal() as db:
        sync_document_metadata_values(db, db.get(Document, doc_id))
        db.commit()
        indexed = db.query(DocumentMetadataValue.value_text).filter_by(
            document_id=doc_id, field_key="categories", value_type="multi_select",
        ).order_by(DocumentMetadataValue.value_text).all()
        assert indexed == [("Guidance",), ("Policy",)]

        def matching_ids(operator, operands):
            predicate = MetadataPredicate(
                template_id=template["id"], field_key="categories", field_type="multi_select",
                operator=operator, values=operands,
            )
            return apply_typed_predicates(
                db.query(Document.id).filter(Document.knowledge_base_id == kb["id"]), [predicate],
            ).all()

        assert matching_ids("eq", ["Policy"]) == [(doc_id,)]
        assert matching_ids("in", ["Notice", "Guidance"]) == [(doc_id,)]
        assert matching_ids("eq", ["Notice"]) == []
        assert describe_schema(db, [kb["id"]])["operators"]["multi_select"] == ["eq", "in"]


def test_main_pipeline_queues_metadata_after_index(monkeypatch):
    api = next(client())
    _, _, doc_id = setup_document(api)
    with SessionLocal() as db:
        # Other tests may leave unrelated retry jobs queued.
        db.query(ProcessingJob).filter(ProcessingJob.status == "queued").update({"status": "cancelled"})
        db.add(ProcessingJob(document_id=doc_id, knowledge_base_id=db.get(Document, doc_id).knowledge_base_id))
        db.commit()
        monkeypatch.setattr("app.services.extract_text", lambda doc: doc.extracted_text)
        assert process_next_job(db)
        assert db.get(Document, doc_id).status == "completed"
        assert db.query(ProcessingJob).filter_by(document_id=doc_id, job_type="EXTRACT_DOCUMENT_METADATA", status="queued").count() == 1


def test_updated_template_does_not_rewrite_an_old_manual_snapshot():
    api = next(client())
    _, template, doc_id = setup_document(api, [field(fill_mode="manual", required=False)])
    response = api.patch(f"/api/v1/document-templates/{template['id']}", json={"fields": [field(extraction_description="หน่วยงานที่ออกเอกสาร")]})
    assert response.status_code == 200, response.text
    assert response.json()["version"] == 2
    preview = api.get(f"/api/v1/documents/{doc_id}/text").json()
    assert preview["metadata_template_fields"][0]["fill_mode"] == "manual"
    response = api.post(f"/api/v1/documents/{doc_id}/metadata-extract", json={"enable_missing_fields": True})
    assert response.status_code == 200
    assert response.json()["queued"] is False
    assert api.get(f"/api/v1/documents/{doc_id}/text").json()["metadata_template_fields"][0]["fill_mode"] == "manual"


def test_source_change_keeps_auto_value_until_replacement_is_published(monkeypatch):
    api = next(client())
    _, _, doc_id = setup_document(api)
    monkeypatch.setattr(LLMClient, "extract_document_metadata", lambda self, fields, text: {"issuer": [{"value": "Land Department", "evidence_quote": "Issuer: Land Department."}]})
    queue_and_run(doc_id)
    with SessionLocal() as db:
        doc = db.get(Document, doc_id)
        assert doc.document_metadata["issuer"] == "Land Department"
        doc.extracted_text = "A different source version."
        assert queue_metadata_extraction(db, doc)
        db.commit()
        # A queue operation must not erase the last usable value or remove it
        # from structured search before a replacement has passed validation.
        assert doc.document_metadata == {"issuer": "Land Department"}
        assert db.query(DocumentMetadataValue).filter_by(document_id=doc_id).count() == 1


def test_source_change_replaces_auto_value_only_after_success(monkeypatch):
    api = next(client())
    _, _, doc_id = setup_document(api)
    monkeypatch.setattr(LLMClient, "extract_document_metadata", lambda self, fields, text: {
        "issuer": [{"value": "Land Department", "evidence_quote": "Issuer: Land Department."}],
    })
    queue_and_run(doc_id)
    with SessionLocal() as db:
        doc = db.get(Document, doc_id)
        doc.extracted_text = "Issuer: Revenue Department."
        assert queue_metadata_extraction(db, doc)
        db.commit()
        job = db.query(ProcessingJob).filter_by(document_id=doc_id, job_type="EXTRACT_DOCUMENT_METADATA", status="queued").one()
    monkeypatch.setattr(LLMClient, "extract_document_metadata", lambda self, fields, text: {
        "issuer": [{"value": "Revenue Department", "evidence_quote": "Issuer: Revenue Department."}],
    })
    with SessionLocal() as db:
        process_metadata_job(db, claim(db, job.id))
        assert db.get(Document, doc_id).document_metadata == {"issuer": "Revenue Department"}


def test_typed_projection_migration_preserves_existing_rows():
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, text
    migration_path = Path(__file__).parents[1] / "migrations/versions/0031_auto_metadata.py"
    spec = importlib.util.spec_from_file_location("auto_metadata_migration", migration_path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE documents (id TEXT PRIMARY KEY, metadata_template_fields JSON)"))
        connection.execute(text("CREATE TABLE document_metadata_values (id TEXT PRIMARY KEY, document_id TEXT, knowledge_base_id TEXT, field_key TEXT, value_text TEXT)"))
        connection.execute(text("INSERT INTO documents VALUES ('d', :fields)"), {"fields": '[{"key":"date","field_type":"date"}]'})
        connection.execute(text("INSERT INTO document_metadata_values VALUES ('v', 'd', 'kb', 'date', '2026-09-08')"))
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        migration.upgrade()  # Fresh installations already contain model columns/indexes.
        row = connection.execute(text("SELECT value_type, value_date, value_text FROM document_metadata_values")).one()
        assert tuple(row) == ("date", "2026-09-08", "2026-09-08")
        assert connection.execute(text("SELECT metadata_status, metadata_revision FROM documents")).one() == ("not_started", 0)
        migration.downgrade()
        assert connection.execute(text("SELECT value_text FROM document_metadata_values")).scalar() == "2026-09-08"


def scripted_client(*answers):
    calls = []

    def extract(fields, text):
        calls.append([f["key"] for f in fields])
        return answers[min(len(calls), len(answers)) - 1]
    return SimpleNamespace(extract_document_metadata=extract), calls


GOOD = [{"value": "to keep the register accurate", "evidence_quote": "to keep the register accurate"}]
TEXT = "Purpose: to keep the register accurate. Issuer: Land Department."


@pytest.mark.parametrize("bad", ["a bare string", [{"value": "no quote here"}], [{"value": "x", "evidence_quote": "not in the document"}]])
def test_an_unusable_answer_is_asked_again_for_that_field_only(bad):
    fields = [field("purpose", field_type="textarea"), field("issuer")]
    issuer = [{"value": "Land Department", "evidence_quote": "Land Department"}]
    client, calls = scripted_client({"purpose": bad, "issuer": issuer}, {"purpose": GOOD})
    observations = extract_candidates(fields, TEXT, client)
    assert calls == [["purpose", "issuer"], ["purpose"]]
    assert observations["purpose"]["status"] == "auto_accepted" and observations["purpose"]["candidates"][0]["value"] == GOOD[0]["value"]
    assert observations["issuer"]["status"] == "auto_accepted"


def test_an_answer_that_stays_unusable_is_reported_after_one_retry():
    client, calls = scripted_client({"purpose": "still a bare string"})
    observations = extract_candidates([field("purpose", field_type="textarea")], TEXT, client)
    assert len(calls) == 2 and observations["purpose"]["status"] == "invalid_evidence" and observations["purpose"]["candidates"] == []


def test_a_usable_answer_is_not_asked_twice():
    client, calls = scripted_client({"purpose": GOOD})
    extract_candidates([field("purpose", field_type="textarea")], TEXT, client)
    assert len(calls) == 1


DOCUMENT = "Section 7 There shall be a central committee.\nIts chair is the permanent secretary.  Issuer: Land Department."


@pytest.mark.parametrize("quote", [
    '"Section 7 There shall be a central committee."',
    "“Section 7 There shall be a central committee.”",
    "Section 7 There shall be a   central committee.",
    "Section 7 There shall be a central committee.\nIts chair is the permanent secretary. Issuer: Land Department.",
])
def test_a_quote_differing_only_in_wrapping_marks_or_spacing_is_accepted_as_the_documents_own_text(quote):
    answer = {"purpose": [{"value": "Section 7", "evidence_quote": quote}]}
    observation = extract_candidates([field("purpose", field_type="textarea")], DOCUMENT, extractor(answer))["purpose"]
    assert observation["status"] == "auto_accepted"
    stored = observation["candidates"][0]["evidence"]
    assert DOCUMENT[stored["char_start"]:stored["char_end"]] == stored["quote"]


@pytest.mark.parametrize("quote", ["Section 7 There shall be a different committee.", '"A committee that is not in the document"', '""', "   "])
def test_a_quote_whose_words_are_not_in_the_document_is_still_rejected(quote):
    answer = {"purpose": [{"value": "Section 7", "evidence_quote": quote}]}
    observation = extract_candidates([field("purpose", field_type="textarea")], DOCUMENT, extractor(answer))["purpose"]
    assert observation["status"] == "invalid_evidence" and observation["candidates"] == []


AM, NIKHAHIT_AA, BROKEN_AM = "ำ", "ํา", " า"


@pytest.mark.parametrize("window_am, quote_am", [
    (BROKEN_AM, AM), (BROKEN_AM, NIKHAHIT_AA), (AM, NIKHAHIT_AA), (NIKHAHIT_AA, AM), (AM, BROKEN_AM),
])
def test_a_quote_spelling_sara_am_differently_finds_the_documents_own_text(window_am, quote_am):
    window = f"มาตรา ๕ ให้รัฐมนตรีก{window_am}หนดค่าธรรมเนียม และท{window_am}หน้าที่อื่น"
    quote = f"ให้รัฐมนตรีก{quote_am}หนดค่าธรรมเนียม และท{quote_am}หน้าที่"
    assert _locate_quote(quote, window) == f"ให้รัฐมนตรีก{window_am}หนดค่าธรรมเนียม และท{window_am}หน้าที่"


def test_a_quote_with_both_a_line_break_and_a_broken_sara_am_is_found():
    window = f"ข้อ ๓ ค{BROKEN_AM}แนะน{BROKEN_AM}\nในการปฏิบัติงาน"
    assert _locate_quote(f"ค{AM}แนะน{AM} ในการปฏิบัติงาน", window) == f"ค{BROKEN_AM}แนะน{BROKEN_AM}\nในการปฏิบัติงาน"


@pytest.mark.parametrize("quote", [f"ก{AM}หนด", f"ก{NIKHAHIT_AA}หนด", "กำหนดเวลา"])
def test_sara_am_does_not_match_a_bare_sara_aa_or_other_words(quote):
    assert _locate_quote(quote, f"ให้กาหนดค่าธรรมเนียม และท{BROKEN_AM}หน้าที่") is None


def test_a_repaired_sara_am_quote_is_stored_as_the_documents_broken_text():
    text = f"มาตรา ๕ ให้รัฐมนตรีก{BROKEN_AM}หนดค่าธรรมเนียม"
    answer = {"purpose": [{"value": "ก" + AM + "หนดค่าธรรมเนียม", "evidence_quote": f"รัฐมนตรีก{AM}หนดค่าธรรมเนียม"}]}
    observation = extract_candidates([field("purpose", field_type="textarea")], text, extractor(answer))["purpose"]
    assert observation["status"] != "invalid_evidence" and len(observation["candidates"]) == 1
    stored = observation["candidates"][0]["evidence"]
    assert stored["quote"] == f"รัฐมนตรีก{BROKEN_AM}หนดค่าธรรมเนียม"
    assert text[stored["char_start"]:stored["char_end"]] == stored["quote"]
    # The value is spelled differently from the document's quote, so a person reviews it.
    assert observation["status"] == "suggested"


def by_window(answers):
    """An extractor that answers from what the window actually contains, like a model reading it."""
    calls = []

    def extract(fields, text):
        calls.append(len(text))
        return {f["key"]: [dict(a) for marker, a in answers.get(f["key"], []) if marker in text] for f in fields}
    return SimpleNamespace(extract_document_metadata=extract), calls


def test_a_value_deep_in_a_long_document_is_found_and_the_field_is_not_partial():
    text = ("x" * 11990 + " ") * 7 + "Purpose: to keep the register accurate. " + ("y" * 11990 + " ") * 2
    assert 4 * WINDOW < text.index("Purpose") < len(text) <= WINDOW * MAX_WINDOWS
    client, calls = by_window({"purpose": [("Purpose:", {"value": "to keep the register accurate", "evidence_quote": "to keep the register accurate"})]})
    observation = extract_candidates([field("purpose")], text, client)["purpose"]
    assert observation["status"] == "auto_accepted" and observation["coverage"] == "full"
    stored = observation["candidates"][0]["evidence"]
    assert text[stored["char_start"]:stored["char_end"]] == stored["quote"] and len(calls) == -(-len(text) // WINDOW)


def test_a_sentence_crossing_a_window_boundary_is_found_once():
    sentence = "Issuer: Land Department."
    text = "x" * (WINDOW - 8) + sentence + "z" * 100
    client, _ = by_window({"issuer": [(sentence, {"value": "Land Department", "evidence_quote": sentence})]})
    observation = extract_candidates([field("issuer")], text, client)["issuer"]
    assert observation["status"] == "auto_accepted" and len(observation["candidates"]) == 1 and WINDOW_OVERLAP > len(sentence)


ITEMS = [{"value": "land", "evidence_quote": "land"}, {"value": "tax", "evidence_quote": "tax"}, {"value": "deed", "evidence_quote": "deed"}]


def test_a_text_list_field_collects_items_into_one_value_each_backed_by_its_own_quote():
    text = "The land tax deed office."
    observation = extract_candidates([field("topics", field_type="text_list")], text, extractor({"topics": ITEMS}))["topics"]
    assert observation["status"] == "auto_accepted" and len(observation["candidates"]) == 1
    candidate = observation["candidates"][0]
    assert candidate["value"] == ["land", "tax", "deed"] and [e["quote"] for e in candidate["item_evidence"]] == ["land", "tax", "deed"]


def test_a_text_list_with_one_unusable_item_is_kept_for_review_not_discarded():
    text = "The land tax office."
    answer = {"topics": ITEMS[:2] + [{"value": "deed", "evidence_quote": "not in the document"}]}
    observation = extract_candidates([field("topics", field_type="text_list")], text, extractor(answer))["topics"]
    assert observation["status"] == "suggested" and observation["candidates"][0]["value"] == ["land", "tax"]


def test_a_text_list_in_a_document_read_only_in_part_is_partial_even_when_an_item_was_unusable():
    text = "The land tax office. " + ("x" * 11990 + " ") * 21
    assert len(text) > WINDOW * MAX_WINDOWS
    answer = ITEMS[:2] + [{"value": "deed", "evidence_quote": "not in the document"}]
    client, _ = by_window({"topics": [("The land", a) for a in answer]})
    observation = extract_candidates([field("topics", field_type="text_list")], text, client)["topics"]
    assert observation["status"] == "partial" and observation["coverage"] == "partial"


def test_text_list_values_are_validated_stored_and_searchable():
    from app.document_templates import validate_metadata_values

    topics = {"key": "topics", "field_type": "text_list"}
    assert validate_metadata_values([topics], {"topics": ["land", "tax"]}) == {"topics": ["land", "tax"]}
    for bad in ("land", ["land", "land"], ["land", ""], [1], [["a"], ["a"]], [{"x": 1}], ["x" * 1001], [f"t{i}" for i in range(51)]):
        with pytest.raises(ValueError):
            validate_metadata_values([topics], {"topics": bad})
    api = next(client())
    kb, template, doc_id = setup_document(api, [field("topics", field_type="text_list", fill_mode="manual", extraction_description=None)], {"topics": ["land", "tax"]})
    from app.services import sync_document_metadata_values

    with SessionLocal() as db:
        sync_document_metadata_values(db, db.get(Document, doc_id))
        db.commit()
        rows = db.query(DocumentMetadataValue).filter_by(document_id=doc_id, field_key="topics").all()
        assert sorted(r.value_text for r in rows) == ["land", "tax"] and {r.value_type for r in rows} == {"text_list"}


SIGNED = "ให้ไว้ ณ วันที่ ๑๖ มกราคม พ.ศ. ๒๕๕๘"
GAZETTE = "เล่ม ๑๓๒ ตอนที่ ๔ ก ราชกิจจานุเบกษา ๒๒ มกราคม ๒๕๕๘"
COMPUTED = "พระราชบัญญัตินี้ให้ใช้บังคับเมื่อพ้นกำหนดหนึ่งร้อยยี่สิบวันนับแต่วันประกาศ"


def date_field(key):
    return field(key, field_type="date", required=False)


def extract_date(key, text, *answers):
    return extract_candidates([date_field(key)], text, extractor({key: [{"value": value, "evidence_quote": quote} for value, quote in answers]}))[key]


@pytest.mark.parametrize("key, quote, value, status", [
    ("signed_date", SIGNED, "2015-01-16", "auto_accepted"),
    ("signed_date", GAZETTE, "2015-01-22", "suggested"),
    ("issued_date", GAZETTE, "2015-01-22", "auto_accepted"),
    ("effective_date", COMPUTED, "2008-06-05", "suggested"),
    # A Buddhist-year ISO answer is read as the Common Era date, which this quote writes.
    ("signed_date", "ให้ไว้ ณ วันที่ ๒๐ กันยายน พ.ศ. ๒๕๓๔", "2534-09-20", "auto_accepted"),
])
def test_a_date_is_accepted_only_when_its_quote_writes_that_date_and_the_fields_anchor(key, quote, value, status):
    assert extract_date(key, quote, (value, quote))["status"] == status


SIGNED_THEN_GAZETTE = "ให้ไว้ ณ วันที่ ๕ มกราคม พ.ศ. ๒๕๖๗ เป็นปีที่ ๗๙ ในรัชกาลปัจจุบัน ประกาศในราชกิจจานุเบกษา ๑๐ มกราคม ๒๕๖๗"


@pytest.mark.parametrize("key, value, status", [
    ("signed_date", "2024-01-10", "suggested"),
    ("signed_date", "2024-01-05", "auto_accepted"),
    ("issued_date", "2024-01-10", "auto_accepted"),
    ("issued_date", "2024-01-05", "suggested"),
])
def test_a_date_passes_only_when_its_own_anchor_introduces_it(key, value, status):
    assert extract_date(key, SIGNED_THEN_GAZETTE, (value, SIGNED_THEN_GAZETTE))["status"] == status


def test_conflicting_dates_resolve_to_the_one_the_quote_writes_with_its_anchor():
    text = SIGNED + "\n" + GAZETTE
    result = extract_date("signed_date", text, ("2015-01-16", SIGNED), ("2015-01-22", GAZETTE))
    assert result["status"] == "auto_accepted"
    assert [c["value"] for c in result["candidates"]] == ["2015-01-16"]
    two_written = extract_date("effective_date", text, ("2015-01-16", SIGNED), ("2015-01-22", GAZETTE))
    assert two_written["status"] == "conflict"


def test_a_written_date_beside_a_computed_one_stays_a_conflict_without_its_anchor():
    gazette = "ราชกิจจานุเบกษา 6 มีนาคม 2551"
    text = gazette + "\n" + COMPUTED
    result = extract_date("effective_date", text, ("2008-03-06", gazette), ("2008-07-04", COMPUTED))
    assert result["status"] == "conflict" and len(result["candidates"]) == 2
    unanchored = extract_date("effective_date_end", text, ("2008-03-06", gazette), ("2008-07-04", COMPUTED))
    assert unanchored["status"] == "conflict"


def test_an_effective_date_written_after_its_anchor_is_accepted():
    quote = "พระราชบัญญัตินี้ให้ใช้บังคับตั้งแต่วันที่ ๑ ธันวาคม พ.ศ. ๒๕๕๗ เป็นต้นไป"
    result = extract_date("effective_date", quote, ("2014-12-01", quote))
    assert result["status"] == "auto_accepted" and result["candidates"][0]["value"] == "2014-12-01"


def test_reevaluation_publishes_stored_dates_that_now_pass_and_is_idempotent():
    api = next(client())
    kb, _, doc_id = setup_document(api, [date_field("signed_date"), date_field("effective_date"), field("issuer", required=False)])
    text = SIGNED + "\n" + GAZETTE + "\n" + COMPUTED

    def stored(value, quote):
        return {"value": value, "evidence": {"quote": quote, "char_start": text.index(quote), "char_end": text.index(quote) + len(quote)}}
    with SessionLocal() as db:
        document = db.get(Document, doc_id)
        document.extracted_text = text
        base = {"origin": "document_extraction", "content_version": hashlib.sha256(text.encode()).hexdigest(), "extractor_version": "1",
                "coverage": "full", "locked": False, "template_version": document.metadata_template_version}
        document.metadata_observations = {
            "signed_date": {**base, "status": "conflict", "candidates": [stored("2015-01-16", SIGNED), stored("2015-01-22", GAZETTE)]},
            "effective_date": {**base, "status": "suggested", "candidates": [stored("2008-06-05", COMPUTED)]},
            "issuer": {**base, "status": "not_found", "candidates": []},
        }
        document.metadata_status = "needs_review"
        revision = document.metadata_revision
        db.commit()
    response = api.post(f"/api/v1/knowledge-bases/{kb['id']}/metadata-reevaluate")
    assert response.status_code == 200 and response.json() == {"documents": 1, "fields": 2}
    preview = api.get(f"/api/v1/documents/{doc_id}/text").json()
    assert preview["document_metadata"] == {"signed_date": "2015-01-16", "effective_date": "2008-06-05"}
    assert preview["metadata_observations"]["signed_date"]["status"] == "auto_accepted"
    assert preview["metadata_observations"]["effective_date"]["status"] == "suggested"
    assert preview["metadata_field_states"] == {"signed_date": "auto_verified", "effective_date": "unverified", "issuer": "not_found"}
    assert preview["metadata_status"] == "unverified" and preview["metadata_revision"] == revision + 1
    with SessionLocal() as db:
        assert db.query(DocumentMetadataValue).filter_by(document_id=doc_id, field_key="signed_date").count() == 1
        assert db.query(DocumentMetadataValue).filter_by(document_id=doc_id, field_key="effective_date").one().value_date.isoformat() == "2008-06-05"
    assert api.post(f"/api/v1/knowledge-bases/{kb['id']}/metadata-reevaluate").json() == {"documents": 0, "fields": 0}


def test_reevaluation_marks_a_document_complete_when_only_unfound_optional_fields_remained():
    api = next(client())
    kb, _, doc_id = setup_document(api, [field("issuer", required=False)])
    with SessionLocal() as db:
        document = db.get(Document, doc_id)
        document.metadata_observations = {"issuer": {"status": "not_found", "origin": "document_extraction", "coverage": "full", "locked": False, "candidates": []}}
        document.metadata_status = "needs_review"
        db.commit()
    assert api.post(f"/api/v1/knowledge-bases/{kb['id']}/metadata-reevaluate").json() == {"documents": 1, "fields": 0}
    assert api.get(f"/api/v1/documents/{doc_id}/text").json()["metadata_status"] == "complete"


class _FlakyWindows:
    """Answers per window; a window listed in `broken` replies with unusable JSON that many times."""
    def __init__(self, text, quote_by_window, broken):
        self.text, self.quote_by_window, self.broken, self.calls = text, quote_by_window, dict(broken), 0

    def extract_document_metadata(self, fields, window):
        self.calls += 1
        index = next(i for i, quote in self.quote_by_window.items() if quote in window)
        if self.broken.get(index, 0) > 0:
            self.broken[index] -= 1
            raise RuntimeError("METADATA_EXTRACTION_INVALID_RESPONSE")
        return {"issuer": [{"value": self.quote_by_window[index], "evidence_quote": self.quote_by_window[index]}]}


def _two_windows():
    first, second = "ออกโดยกรมที่ดิน", "ลงนามโดยอธิบดี"
    text = first + "ก" * (WINDOW + WINDOW_OVERLAP) + second + "ข" * 50
    return text, {0: first, 1: second}


def test_an_unusable_reply_is_asked_once_more_and_the_document_reads_in_full():
    text, quotes = _two_windows()
    client = _FlakyWindows(text, quotes, {1: 1})
    result = extract_candidates([field()], text, client)["issuer"]
    assert result["coverage"] == "full"
    assert [c["value"] for c in result["candidates"]] == [quotes[0], quotes[1]]


def test_a_window_still_unusable_after_its_retry_is_skipped_and_the_rest_is_kept_as_partial():
    text, quotes = _two_windows()
    result = extract_candidates([field()], text, _FlakyWindows(text, quotes, {1: 2}))["issuer"]
    assert (result["status"], result["coverage"]) == ("partial", "partial")
    assert [c["value"] for c in result["candidates"]] == [quotes[0]]


def test_a_document_no_window_of_which_is_readable_still_fails():
    text, quotes = _two_windows()
    with pytest.raises(RuntimeError, match="METADATA_EXTRACTION_INVALID_RESPONSE"):
        extract_candidates([field()], text, _FlakyWindows(text, quotes, {0: 2, 1: 2}))


def test_a_single_window_document_still_unusable_after_its_retry_fails():
    text = "ออกโดยกรมที่ดิน"
    with pytest.raises(RuntimeError, match="METADATA_EXTRACTION_INVALID_RESPONSE"):
        extract_candidates([field()], text, _FlakyWindows(text, {0: text}, {0: 2}))


def test_an_unusable_reply_to_the_per_field_retry_keeps_the_first_answers():
    text = "ออกโดยกรมที่ดิน ลงวันที่ 1 มกราคม"
    replies = [{"issuer": [{"value": "กรมที่ดิน", "evidence_quote": "ออกโดยกรมที่ดิน"}],
                "signer": [{"value": "อธิบดี", "evidence_quote": "ไม่มีในเอกสาร"}]}]

    def extract(fields, window):
        if replies:
            return replies.pop()
        raise RuntimeError("METADATA_EXTRACTION_INVALID_RESPONSE")

    result = extract_candidates([field(), field("signer")], text, SimpleNamespace(extract_document_metadata=extract))
    assert [c["value"] for c in result["issuer"]["candidates"]] == ["กรมที่ดิน"]
    assert result["signer"]["candidates"] == []


def test_a_buddhist_year_date_answer_is_stored_in_the_common_era():
    quote = "ให้ไว้ ณ วันที่ ๒๒ มกราคม พ.ศ. ๒๕๕๘"
    for answer in ("2558-01-22", "22 มกราคม 2558"):
        result = extract_date("signed_date", quote, (answer, quote))
        assert [c["value"] for c in result["candidates"]] == ["2015-01-22"], answer


def test_a_value_quoting_a_consolidation_note_waits_for_a_person():
    """GPT-6 Luna read "ยกเลิกโดย" backwards and auto-accepted the later law as the one this document cancels."""
    quote = "# ข้อ 1 (1) ยกเลิกโดย กฎกระทรวง ฉบับที่ 49 (พ.ศ.2544)"
    result = extract_candidates([field("cancel_law_by")], quote, extractor({"cancel_law_by": [{"value": "กฎกระทรวง ฉบับที่ 49 (พ.ศ.2544)", "evidence_quote": quote}]}))
    assert result["cancel_law_by"]["status"] == "suggested"
    plain = "ให้ยกเลิกกฎกระทรวง ฉบับที่ 31 (พ.ศ. 2530)"
    result = extract_candidates([field("cancel_law_by")], plain, extractor({"cancel_law_by": [{"value": "กฎกระทรวง ฉบับที่ 31 (พ.ศ. 2530)", "evidence_quote": plain}]}))
    assert result["cancel_law_by"]["status"] == "auto_accepted", "a document's own cancelling clause still passes"


def test_a_date_its_quote_neither_writes_nor_computes_is_dropped():
    """The template's example date 2026-09-15 came back as effective_date_end with an unrelated quote."""
    text = "ข้อ 3 ให้ใช้บังคับตั้งแต่วันถัดจากวันประกาศในราชกิจจานุเบกษาเป็นต้นไป ข้อ 4 ให้อธิบดีรักษาการ"
    result = extract_candidates([field("effective_date_end", field_type="date", required=False)], text,
                                extractor({"effective_date_end": [{"value": "2026-09-15", "evidence_quote": "ข้อ 4 ให้อธิบดีรักษาการ"}]}))
    assert result["effective_date_end"]["candidates"] == [] and result["effective_date_end"]["status"] == "invalid_evidence"
    computed = extract_candidates([field("effective_date", field_type="date", required=False)], text,
                                  extractor({"effective_date": [{"value": "2019-06-13", "evidence_quote": "ข้อ 3 ให้ใช้บังคับตั้งแต่วันถัดจากวันประกาศในราชกิจจานุเบกษาเป็นต้นไป"}]}))
    assert [c["value"] for c in computed["effective_date"]["candidates"]] == ["2019-06-13"], "a date computed from a relative clause is kept"
    assert computed["effective_date"]["status"] == "suggested", "and still waits for a person"
