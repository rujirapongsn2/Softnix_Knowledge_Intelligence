"""Scoped schema discovery and typed predicates shared by search and inventory."""
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr, model_validator
from sqlalchemy import func, or_
from sqlalchemy.orm import load_only

from .document_templates import list_templates
from .models import Document, DocumentMetadataValue


class MetadataPredicate(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    template_id: str = Field(min_length=1, max_length=120)
    field_key: str = Field(min_length=1, max_length=80)
    field_type: Literal["text", "textarea", "select", "boolean", "date", "number"]
    operator: Literal["eq", "in", "gte", "lte"] = "eq"
    values: list[StrictStr | StrictInt | StrictFloat | StrictBool] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def validate_operand(self):
        if self.operator != "in" and len(self.values) != 1:
            raise ValueError("Use one value except with the in operator")
        if self.operator in {"gte", "lte"} and self.field_type not in {"date", "number"}:
            raise ValueError("Range operators require date or number fields")
        for value in self.values:
            if self.field_type == "number":
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError("Expected a number")
            elif self.field_type == "boolean":
                if not isinstance(value, bool):
                    raise ValueError("Expected a boolean")
            elif not isinstance(value, str) or len(value) > 10000:
                raise ValueError("Expected a string")
            elif self.field_type == "date":
                if len(value) != 10:
                    raise ValueError("Expected ISO date")
                date.fromisoformat(value)
        return self


def template_clause(template_id):
    if template_id.startswith("system:"):
        return or_(Document.metadata_template_id == template_id,
                   (Document.metadata_template_id.is_(None)) & (Document.document_type == template_id[7:]))
    return Document.metadata_template_id == template_id


def apply_typed_predicates(rows, predicates):
    for raw in predicates:
        predicate = raw if isinstance(raw, MetadataPredicate) else MetadataPredicate.model_validate(raw)
        values = predicate.values
        column = DocumentMetadataValue.value_text
        if predicate.field_type == "date":
            column = DocumentMetadataValue.value_date
            values = [date.fromisoformat(v) for v in values]
        elif predicate.field_type == "number":
            column = DocumentMetadataValue.value_number
        else:
            values = [str(v) for v in values]
        comparison = {"eq": lambda: column == values[0], "in": lambda: column.in_(values),
                      "gte": lambda: column >= values[0], "lte": lambda: column <= values[0]}[predicate.operator]()
        match = rows.session.query(DocumentMetadataValue.id).filter(
            DocumentMetadataValue.document_id == Document.id,
            DocumentMetadataValue.knowledge_base_id == Document.knowledge_base_id,
            DocumentMetadataValue.field_key == predicate.field_key,
            DocumentMetadataValue.value_type == predicate.field_type, comparison,
        ).exists()
        rows = rows.filter(template_clause(predicate.template_id), match)
    return rows


def describe_schema(db, kb_ids, offset=0, limit=200):
    """Bound snapshot traversal; report coverage for this page, never imply completeness."""
    base = db.query(Document).filter(Document.knowledge_base_id.in_(kb_ids), Document.deleted_at.is_(None))
    total = base.count()
    documents = base.options(load_only(Document.id, Document.knowledge_base_id, Document.metadata_template_id,
                                       Document.metadata_template_version, Document.metadata_template_name,
                                       Document.document_type, Document.metadata_template_fields,
                                       Document.document_metadata)).order_by(Document.id).offset(offset).limit(limit).all()
    definitions = {}
    for kb_id in kb_ids:
        for template in list_templates(db, kb_id, include_inactive=True):
            definitions[(kb_id, template["id"], template["version"])] = {
                "knowledge_base_id": kb_id, "template_id": template["id"], "version": template["version"],
                "name": template["name"], "fields": template["fields"], "sampled_documents": 0, "coverage": {},
            }
    for doc in documents:
        identity = (doc.knowledge_base_id, doc.metadata_template_id or "system:" + doc.document_type, doc.metadata_template_version or 1)
        item = definitions.setdefault(identity, {"knowledge_base_id": identity[0], "template_id": identity[1], "version": identity[2],
                                               "name": doc.metadata_template_name, "fields": doc.metadata_template_fields or [],
                                               "sampled_documents": 0, "coverage": {}})
        item["sampled_documents"] += 1
        for field in doc.metadata_template_fields or []:
            count = item["coverage"].setdefault(field["key"], {"available": 0, "unknown": 0})
            count["available" if field["key"] in (doc.document_metadata or {}) else "unknown"] += 1
    return {"templates": list(definitions.values()), "total_documents": total, "offset": offset,
            "next_offset": offset + len(documents) if offset + len(documents) < total else None,
            "coverage_scope": "document_page", "operators": {"text": ["eq", "in"], "textarea": ["eq", "in"], "select": ["eq", "in"], "boolean": ["eq", "in"], "date": ["eq", "in", "gte", "lte"], "number": ["eq", "in", "gte", "lte"]},
            "guidance": "Use explicit user constraints as filters. Never filter on inferred document types. Unknown metadata is not evidence of absence. Follow next_offset for historical snapshots and coverage."}


def decorate_sources(db, sources, kb_ids, requested_keys=()):
    ids = {s.get("document_id") for s in sources if s.get("document_id")}
    documents = {d.id: d for d in db.query(Document).options(load_only(Document.id, Document.metadata_template_fields,
                 Document.document_metadata, Document.metadata_observations, Document.metadata_status,
                 Document.metadata_revision)).filter(Document.id.in_(ids), Document.knowledge_base_id.in_(kb_ids), Document.deleted_at.is_(None))}
    for source in sources:
        doc = documents.get(source.get("document_id"))
        if not doc:
            continue
        visible = {f["key"] for f in doc.metadata_template_fields or [] if f.get("searchable", True)}
        values = {k: v for k, v in (doc.document_metadata or {}).items() if k in visible}
        ordered = list(dict.fromkeys([k for k in requested_keys if k in values] + list(values)))[:8]
        source["document_metadata"] = {k: values[k][:1000] if isinstance(values[k], str) else values[k] for k in ordered}
        source["metadata_projection_truncated"] = len(ordered) < len(values) or any(isinstance(values[k], str) and len(values[k]) > 1000 for k in ordered)
        source["metadata_provenance"] = {}
        for key in ordered:
            observation = (doc.metadata_observations or {}).get(key, {})
            evidence = [c["evidence"] for c in observation.get("candidates", []) if c["value"] == values[key]][:1]
            source["metadata_provenance"][key] = {
                "status": observation.get("status", "legacy_manual"), "origin": observation.get("origin", "legacy_manual"),
                "content_version": observation.get("content_version"),
                "evidence": [{**e, "quote": e["quote"][:1000], "quote_truncated": len(e["quote"]) > 1000} for e in evidence],
            }
        source["metadata_status"] = doc.metadata_status
        source["metadata_revision"] = doc.metadata_revision


def metadata_coverage(db, kb_ids):
    rows = db.query(Document.metadata_status, func.count(Document.id)).filter(
        Document.knowledge_base_id.in_(kb_ids), Document.deleted_at.is_(None)).group_by(Document.metadata_status).all()
    return {"scope": "all_non_deleted_documents_in_authorized_kbs", "by_status": dict(rows)}


def predicate_coverage(db, kb_ids, predicates):
    result = []
    for raw in predicates:
        predicate = raw if isinstance(raw, MetadataPredicate) else MetadataPredicate.model_validate(raw)
        rows = db.query(Document.id).filter(Document.knowledge_base_id.in_(kb_ids), Document.deleted_at.is_(None), template_clause(predicate.template_id))
        known = db.query(DocumentMetadataValue.id).filter(
            DocumentMetadataValue.document_id == Document.id,
            DocumentMetadataValue.field_key == predicate.field_key,
            DocumentMetadataValue.value_type == predicate.field_type,
        )
        if predicate.field_type == "date":
            known = known.filter(DocumentMetadataValue.value_date.is_not(None))
        elif predicate.field_type == "number":
            known = known.filter(DocumentMetadataValue.value_number.is_not(None))
        total, available = rows.count(), rows.filter(known.exists()).count()
        result.append({"template_id": predicate.template_id, "field_key": predicate.field_key,
                       "total_in_template": total, "filter_value_available": available,
                       "unknown_or_not_filterable": total - available})
    return result
