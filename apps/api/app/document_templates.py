"""Metadata templates for common users, kept independent from processing profiles."""
from __future__ import annotations

import re
import uuid
import json
from copy import deepcopy
from datetime import date
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import Document, DocumentMetadataTemplate

PROCESSING_PROFILES = frozenset({"general", "legal", "regulation", "contract"})

SYSTEM_TEMPLATES: tuple[dict[str, Any], ...] = (
    {"id": "system:general", "code": "general", "name": "General document", "description": "Search, citations, and knowledge graph.", "base_document_type": "general", "fields": [], "version": 1, "is_active": True, "is_system": True},
    {"id": "system:legal", "code": "legal", "name": "Legal document", "description": "Automatically extracts legal metadata.", "base_document_type": "legal", "fields": [{"key": "issuer", "label": "หน่วยงานผู้ออก", "field_type": "text", "required": False, "filterable": True, "graph_entity_type": "Organization", "graph_relationship": "ISSUED_BY"}, {"key": "effective_date", "label": "วันที่มีผลใช้บังคับ", "field_type": "date", "required": False, "filterable": True}], "version": 1, "is_active": True, "is_system": True},
    {"id": "system:regulation", "code": "regulation", "name": "Regulation / policy", "description": "For notifications, regulations, policies, and related legal documents.", "base_document_type": "regulation", "fields": [{"key": "issuer", "label": "หน่วยงานผู้ออก", "field_type": "text", "required": False, "filterable": True, "graph_entity_type": "Organization", "graph_relationship": "ISSUED_BY"}, {"key": "effective_date", "label": "วันที่มีผลใช้บังคับ", "field_type": "date", "required": False, "filterable": True}, {"key": "reference_number", "label": "เลขที่ประกาศ/เอกสาร", "field_type": "text", "required": False, "filterable": True}], "version": 1, "is_active": True, "is_system": True},
    {"id": "system:contract", "code": "contract", "name": "Contract", "description": "Automatically extracts parties, obligations, and terms.", "base_document_type": "contract", "fields": [{"key": "contract_number", "label": "เลขที่สัญญา", "field_type": "text", "required": False, "filterable": True}, {"key": "effective_date", "label": "วันที่มีผลใช้บังคับ", "field_type": "date", "required": False, "filterable": True}], "version": 1, "is_active": True, "is_system": True},
)

SYSTEM_TEMPLATE_NAMES = frozenset(item["name"].casefold() for item in SYSTEM_TEMPLATES)
SYSTEM_TEMPLATE_CODES = frozenset(item["code"].casefold() for item in SYSTEM_TEMPLATES)


def normalize_field_definitions(fields: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Return a stable field contract for old and new template records."""
    normalized = []
    for field in fields or []:
        if not isinstance(field, dict):
            continue
        item = dict(field)
        item.setdefault("searchable", True)
        item.setdefault("filterable", False)
        item.setdefault("graph_entity_type", None)
        item.setdefault("graph_relationship", None)
        normalized.append(item)
    return normalized


def profile_default_fields(profile: str) -> list[dict[str, Any]]:
    template = next((item for item in SYSTEM_TEMPLATES if item["base_document_type"] == profile), None)
    return normalize_field_definitions(deepcopy(template.get("fields", [])) if template else [])


def merge_profile_fields(profile: str, fields: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Inherit profile fields while allowing a custom type to override them."""
    merged = {field["key"]: field for field in profile_default_fields(profile) if field.get("key")}
    order = list(merged)
    for field in normalize_field_definitions(fields):
        key = field.get("key")
        if not key:
            continue
        if key not in merged:
            order.append(key)
        merged[key] = field
    return [merged[key] for key in order]


def custom_template_fields(row: DocumentMetadataTemplate) -> list[dict[str, Any]]:
    """Return administrator-owned fields, with a legacy fallback."""
    return normalize_field_definitions(row.custom_fields if row.custom_fields is not None else row.fields)


def metadata_search_text(fields: list[dict[str, Any]] | None, values: dict[str, Any] | None) -> str:
    """Build the searchable projection without indexing non-searchable fields."""
    values = values or {}
    # Preserve the template order so the projection is deterministic. This is
    # useful for reproducible indexing, audits, and tests even though the
    # retrieval predicate treats it as plain text.
    keys = [field.get("key") for field in normalize_field_definitions(fields) if field.get("searchable", True)]
    parts = [str(values[key]).strip() for key in keys if key and key in values and values[key] not in (None, "")]
    return " ".join(parts)[:10000]


def list_templates(db: Session, knowledge_base_id: str, *, include_inactive: bool = False) -> list[dict[str, Any]]:
    query = db.query(DocumentMetadataTemplate).filter_by(knowledge_base_id=knowledge_base_id)
    if not include_inactive:
        query = query.filter_by(is_active=True)
    usage_rows = (db.query(Document.metadata_template_id, Document.document_type, func.count(Document.id))
                  .filter(Document.knowledge_base_id == knowledge_base_id, Document.deleted_at.is_(None))
                  .group_by(Document.metadata_template_id, Document.document_type).all())
    usage_by_template = {template_id: int(count) for template_id, _, count in usage_rows if template_id}
    usage_by_profile = {profile: int(count) for template_id, profile, count in usage_rows if not template_id}
    system_templates = []
    for template in SYSTEM_TEMPLATES:
        system_templates.append({**template, "fields": normalize_field_definitions(template.get("fields")), "usage_count": usage_by_template.get(template["id"], 0) + usage_by_profile.get(template["base_document_type"], 0)})
    custom = []
    for row in query.order_by(DocumentMetadataTemplate.name.asc()).all():
        custom.append({"id": row.id, "code": row.code, "name": row.name, "description": row.description,
                       "base_document_type": row.base_document_type, "fields": merge_profile_fields(row.base_document_type, custom_template_fields(row)), "version": row.version,
                       "is_active": row.is_active, "is_system": False, "usage_count": usage_by_template.get(row.id, 0)})
    return [*system_templates, *custom]


def resolve_template(db: Session, knowledge_base_id: str, template_id: str | None, fallback_profile: str) -> dict[str, Any]:
    if template_id:
        if template_id.startswith("system:"):
            template = next((item for item in SYSTEM_TEMPLATES if item["id"] == template_id), None)
            if template:
                return dict(template)
        else:
            row = db.get(DocumentMetadataTemplate, template_id)
            if row and row.knowledge_base_id == knowledge_base_id and row.is_active:
                return {"id": row.id, "code": row.code, "name": row.name, "description": row.description,
                        "base_document_type": row.base_document_type, "fields": merge_profile_fields(row.base_document_type, custom_template_fields(row)), "version": row.version,
                        "is_active": row.is_active, "is_system": False}
        raise ValueError("DOCUMENT_TEMPLATE_NOT_FOUND")
    template = next((item for item in SYSTEM_TEMPLATES if item["base_document_type"] == fallback_profile), None)
    if not template:
        raise ValueError("DOCUMENT_TYPE_INVALID")
    result = dict(template)
    result["fields"] = normalize_field_definitions(result.get("fields"))
    return result


def validate_metadata_values(fields: list[dict[str, Any]], values: dict[str, Any] | None) -> dict[str, Any]:
    values = values or {}
    if not isinstance(values, dict):
        raise ValueError("DOCUMENT_METADATA_INVALID")
    allowed = {field.get("key"): field for field in fields if isinstance(field, dict) and field.get("key")}
    unknown = set(values) - set(allowed)
    if unknown:
        raise ValueError("DOCUMENT_METADATA_FIELD_UNKNOWN")
    if len(json.dumps(values, ensure_ascii=False)) > 100_000:
        raise ValueError("DOCUMENT_METADATA_TOO_LARGE")
    result: dict[str, Any] = {}
    for key, field in allowed.items():
        value = values.get(key)
        if value in (None, ""):
            if field.get("required"):
                raise ValueError("DOCUMENT_METADATA_REQUIRED")
            continue
        kind = field.get("field_type", "text")
        if kind == "boolean" and not isinstance(value, bool):
            raise ValueError("DOCUMENT_METADATA_INVALID")
        if kind == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ValueError("DOCUMENT_METADATA_INVALID")
        if kind in {"text", "textarea"} and (not isinstance(value, str) or len(value) > (50_000 if kind == "textarea" else 10_000)):
            raise ValueError("DOCUMENT_METADATA_INVALID")
        if kind == "date":
            if not isinstance(value, str) or len(value) != 10:
                raise ValueError("DOCUMENT_METADATA_INVALID")
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("DOCUMENT_METADATA_INVALID") from exc
        if kind == "select" and value not in field.get("options", []):
            raise ValueError("DOCUMENT_METADATA_INVALID")
        result[key] = value
    return result


def template_code(name: str) -> str:
    candidate = re.sub(r"[^a-z0-9]+", "-", (name or "").casefold()).strip("-")
    return candidate[:90] if len(candidate) >= 2 else f"type-{uuid.uuid4().hex[:10]}"
