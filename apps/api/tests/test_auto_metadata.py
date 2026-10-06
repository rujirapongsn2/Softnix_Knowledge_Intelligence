"""Custom extraction, review, retrieval and authorization regression coverage."""
import hashlib
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import inspect

from test_api import client
from app.db import SessionLocal
from app.metadata_extraction import MAX_WINDOWS, WINDOW, WINDOW_OVERLAP, extract_candidates, process_metadata_job, queue_metadata_extraction
from app.metadata_search import MetadataPredicate, apply_typed_predicates, describe_schema
from app.models import Document, DocumentMetadataValue, ProcessingJob, Relationship
from app.openrouter import OpenRouterClient
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
    result = extract_candidates([field()], "A" * (WINDOW * MAX_WINDOWS + 1), extractor({"issuer": [{"value": "A", "evidence_quote": "A"}]}))
    assert result["issuer"]["status"] == "partial" and result["issuer"]["coverage"] == "partial"


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


def queue_and_run(doc_id):
    with SessionLocal() as db:
        assert queue_metadata_extraction(db, db.get(Document, doc_id))
        assert not queue_metadata_extraction(db, db.get(Document, doc_id))
        db.commit()
        job = db.query(ProcessingJob).filter_by(document_id=doc_id, job_type="EXTRACT_DOCUMENT_METADATA").order_by(ProcessingJob.created_at.desc()).first()
        process_metadata_job(db, job)


def test_job_and_review_publish_only_verified_graph(monkeypatch):
    api = next(client())
    kb, template, doc_id = setup_document(api, [field(graph_entity_type="Organization", graph_relationship="ISSUED_BY")])
    monkeypatch.setattr(OpenRouterClient, "extract_document_metadata", lambda self, fields, text: {"issuer": [{"value": "Land Department", "evidence_quote": "Issuer: Land Department."}]})
    queue_and_run(doc_id)
    preview = api.get(f"/api/v1/documents/{doc_id}/text").json()
    assert preview["status"] == "completed" and preview["metadata_status"] == "needs_review"
    assert preview["document_metadata"] == {}
    with SessionLocal() as db:
        assert db.query(Relationship).filter_by(knowledge_base_id=kb["id"]).count() == 0
    review = {"revision": preview["metadata_revision"], "field_key": "issuer", "action": "confirm"}
    assert api.post(f"/api/v1/documents/{doc_id}/metadata-review", json=review).status_code == 200
    assert api.post(f"/api/v1/documents/{doc_id}/metadata-review", json=review).status_code == 409
    with SessionLocal() as db:
        assert db.query(Relationship).filter_by(knowledge_base_id=kb["id"], review_status="verified").count() == 1
        assert db.query(DocumentMetadataValue).filter_by(document_id=doc_id, value_text="Land Department").count() == 1
        assert not queue_metadata_extraction(db, db.get(Document, doc_id))


def test_failure_keeps_document_searchable(monkeypatch):
    api = next(client())
    _, _, doc_id = setup_document(api)
    def fail(*args):
        raise RuntimeError("OPENROUTER_API_KEY_NOT_CONFIGURED")
    monkeypatch.setattr(OpenRouterClient, "extract_document_metadata", fail)
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
    monkeypatch.setattr(OpenRouterClient, "extract_document_metadata", edit_while_extracting)
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
    monkeypatch.setattr(OpenRouterClient, "extract_document_metadata", lambda self, fields, text: {"issuer": [{"value": "Land Department", "evidence_quote": "Issuer: Land Department."}]})
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
    monkeypatch.setattr(OpenRouterClient, "extract_document_metadata", lambda self, fields, text: {
        "issuer": [{"value": "Land Department", "evidence_quote": "Issuer: Land Department."}],
    })
    queue_and_run(doc_id)
    with SessionLocal() as db:
        doc = db.get(Document, doc_id)
        doc.extracted_text = "Issuer: Revenue Department."
        assert queue_metadata_extraction(db, doc)
        db.commit()
        job = db.query(ProcessingJob).filter_by(document_id=doc_id, job_type="EXTRACT_DOCUMENT_METADATA", status="queued").one()
    monkeypatch.setattr(OpenRouterClient, "extract_document_metadata", lambda self, fields, text: {
        "issuer": [{"value": "Revenue Department", "evidence_quote": "Issuer: Revenue Department."}],
    })
    with SessionLocal() as db:
        process_metadata_job(db, db.get(ProcessingJob, job.id))
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
