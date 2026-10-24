"""Metadata export: document types and metadata values, per document and per Knowledge Base."""
import json
import uuid
from urllib.parse import unquote

from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import app
from app.models import AuditLog, Document


def client():
    with TestClient(app) as test_client:
        assert test_client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "correct-horse-battery-staple"},
        ).status_code == 200
        yield test_client


def _kb(test_client, label):
    suffix = uuid.uuid4().hex[:8]
    kb = test_client.post("/api/v1/knowledge-bases", json={"name": f"{label} {suffix}", "code": f"export-{suffix}"}).json()
    assert test_client.post(f"/api/v1/knowledge-bases/{kb['id']}/activate").status_code == 200
    return kb


def _upload(test_client, kb, name, template_id, values):
    response = test_client.post(
        f"/api/v1/knowledge-bases/{kb['id']}/documents",
        files={"file": (name, f"เนื้อหา {name} {uuid.uuid4().hex}".encode(), "text/plain")},
        data={"template_id": template_id, "metadata_json": json.dumps(values, ensure_ascii=False)},
    )
    assert response.status_code == 200, response.text
    return response.json()["document_id"]


def test_knowledge_base_export_keeps_each_type_version_its_documents_use():
    test_client = next(client())
    kb = _kb(test_client, "Export KB")
    other_kb = _kb(test_client, "Other KB")
    template = test_client.post(f"/api/v1/knowledge-bases/{kb['id']}/document-templates", json={
        "name": "หนังสือเวียน", "base_document_type": "general",
        "fields": [{"key": "issuer", "label": "ผู้ออก", "field_type": "text", "required": True}],
    }).json()
    old_doc = _upload(test_client, kb, "old.txt", template["id"], {"issuer": "กรมทดสอบ"})
    renamed = test_client.patch(f"/api/v1/document-templates/{template['id']}", json={"name": "หนังสือเวียนภายใน"}).json()
    assert renamed["version"] == 1
    updated = test_client.patch(f"/api/v1/document-templates/{template['id']}", json={"fields": [
        {"key": "issuer", "label": "ผู้ออก", "field_type": "text", "required": True},
        {"key": "region", "label": "ภาค", "field_type": "select", "options": ["เหนือ", "ใต้"]},
    ]}).json()
    assert updated["version"] == 2
    new_doc = _upload(test_client, kb, "new.txt", template["id"], {"issuer": "กรมใหม่", "region": "ใต้"})
    assert test_client.patch(f"/api/v1/document-templates/{template['id']}", json={"name": "หนังสือเวียน 2569"}).json()["version"] == 2
    deleted_doc = _upload(test_client, kb, "deleted.txt", template["id"], {"issuer": "กรมลบ"})
    assert test_client.delete(f"/api/v1/documents/{deleted_doc}").status_code == 200
    _upload(test_client, other_kb, "elsewhere.txt", "system:general", {})

    response = test_client.get(f"/api/v1/knowledge-bases/{kb['id']}/metadata-export")

    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert disposition.startswith(f'attachment; filename="metadata-{kb["code"]}.json";')
    assert unquote(disposition.split("''", 1)[1]) == f"metadata-{kb['code']}.json"
    export = response.json()
    assert export["schema_version"] == "ski.metadata-export.v1"
    assert export["scope"] == "knowledge_base"
    assert export["knowledge_base"] == {"id": kb["id"], "code": kb["code"], "name": kb["name"]}
    types = {(item["id"], item["version"]): item for item in export["document_types"]}
    assert {("system:general", 1), (template["id"], 1), (template["id"], 2)} <= set(types)
    assert [field["key"] for field in types[(template["id"], 1)]["fields"]] == ["issuer"]
    assert [field["key"] for field in types[(template["id"], 2)]["fields"]] == ["issuer", "region"]
    assert types[(template["id"], 2)]["fields"][1]["options"] == ["เหนือ", "ใต้"]
    assert (types[(template["id"], 1)]["is_current"], types[(template["id"], 2)]["is_current"]) == (False, True)
    assert types[(template["id"], 1)]["code"] == template["code"]
    assert types[(template["id"], 1)]["name"] == "หนังสือเวียน"  # the name old.txt was filled in under
    assert types[(template["id"], 2)]["name"] == "หนังสือเวียนภายใน"  # renamed again since, without a new version
    alone = test_client.get(f"/api/v1/documents/{old_doc}/metadata-export").json()
    assert alone["document_types"] == [types[(template["id"], 1)]]
    documents = {item["id"]: item for item in export["documents"]}
    assert set(documents) == {old_doc, new_doc}
    assert documents[old_doc]["document_type"] == {"id": template["id"], "version": 1}
    assert documents[old_doc]["metadata"] == {"issuer": "กรมทดสอบ"}
    assert documents[new_doc]["document_type"] == {"id": template["id"], "version": 2}
    assert documents[new_doc]["metadata"] == {"issuer": "กรมใหม่", "region": "ใต้"}
    assert documents[new_doc]["original_filename"] == "new.txt"
    with SessionLocal() as db:
        audit = db.query(AuditLog).filter_by(action="metadata.export", target_id=kb["id"]).one()
        assert audit.metadata_json == {"documents": 2}


def test_document_export_holds_only_that_document_and_its_type():
    test_client = next(client())
    kb = _kb(test_client, "Export Document")
    template = test_client.post(f"/api/v1/knowledge-bases/{kb['id']}/document-templates", json={
        "name": "บันทึก", "base_document_type": "general",
        "fields": [{"key": "topic", "label": "เรื่อง", "field_type": "text"}],
    }).json()
    document_id = _upload(test_client, kb, "บันทึก ภายใน.txt", template["id"], {"topic": "งบประมาณ"})
    _upload(test_client, kb, "another.txt", "system:general", {})

    response = test_client.get(f"/api/v1/documents/{document_id}/metadata-export")

    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="metadata.json";')
    assert unquote(disposition.split("''", 1)[1]) == "metadata-บันทึก ภายใน.json"
    export = response.json()
    assert export["scope"] == "document"
    assert [(item["id"], item["version"], item["is_current"]) for item in export["document_types"]] == [(template["id"], 1, True)]
    assert [(item["id"], item["metadata"]) for item in export["documents"]] == [(document_id, {"topic": "งบประมาณ"})]


def test_export_of_a_deleted_document_or_unknown_knowledge_base_is_not_found():
    test_client = next(client())
    kb = _kb(test_client, "Export Missing")
    document_id = _upload(test_client, kb, "gone.txt", "system:general", {})
    assert test_client.delete(f"/api/v1/documents/{document_id}").status_code == 200
    assert test_client.get(f"/api/v1/documents/{document_id}/metadata-export").status_code == 404
    assert test_client.get(f"/api/v1/knowledge-bases/{uuid.uuid4()}/metadata-export").status_code == 404


def test_document_export_without_a_usable_filename_is_named_after_the_document():
    test_client = next(client())
    kb = _kb(test_client, "Export Unnamed")
    dotted = _upload(test_client, kb, "dotted.txt", "system:general", {})
    unnamed = _upload(test_client, kb, "unnamed.txt", "system:general", {})
    with SessionLocal() as db:
        db.get(Document, dotted).original_filename = ".env"
        db.get(Document, unnamed).original_filename = ""
        db.commit()
    names = {document_id: unquote(test_client.get(f"/api/v1/documents/{document_id}/metadata-export")
                                  .headers["content-disposition"].split("''", 1)[1]) for document_id in (dotted, unnamed)}
    assert names == {dotted: "metadata-env.json", unnamed: f"metadata-{unnamed}.json"}
