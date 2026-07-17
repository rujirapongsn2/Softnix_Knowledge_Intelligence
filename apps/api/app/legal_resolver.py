"""Query-time legal resolution: detect a referenced instrument/provision, pick
its current version as of a date, and note what must be excluded from evidence.

Everything here is deterministic SQL and regex — no LLM call. It only runs at
all when the Knowledge Base scope actually has legal_instruments rows, so a
plain (non-legal) Knowledge Base pays no cost and gets no behavior change.
"""
import re
from datetime import date

from sqlalchemy import or_
from sqlalchemy.orm import Session

from .legal_registry import parse_provision_refs
from .models import Entity, LegalFamily, LegalInstrument, LegalInstrumentRelation
from .planner import LegalContext, RetrievalPlan, RetrievalPolicy

_AMENDMENT_HOP_TYPES = {"ISSUED_UNDER", "IMPLEMENTS"}
_LEGAL_FAMILY_PREFIXES = ("พระราชบัญญัติ", "ประมวลกฎหมาย", "กฎหมาย", "ว่าด้วย")


def _mentions(query_compact: str, candidate_key: str) -> bool:
    candidate_compact = re.sub(r"\s+", "", candidate_key.casefold())
    if len(candidate_compact) < 4:
        return False
    return candidate_compact in query_compact or query_compact in candidate_compact


def _family_matches(query_compact: str, family: LegalFamily) -> bool:
    """Match a legal family name and its human-friendly short form.

    Users commonly say ``กฎหมายกรมที่ดิน`` while the registry stores the
    family as ``ประมวลกฎหมายที่ดิน``.  Removing legal boilerplate gives us the
    safe alias ``ที่ดิน`` without broadening a query to unrelated families.
    """
    values = [family.normalized_key or "", family.base_title or ""]
    for value in list(values):
        compact = re.sub(r"\s+", "", value.casefold())
        for prefix in _LEGAL_FAMILY_PREFIXES:
            prefix_compact = re.sub(r"\s+", "", prefix.casefold())
            if compact.startswith(prefix_compact) and len(compact) > len(prefix_compact):
                values.append(compact[len(prefix_compact):])
    return any(_mentions(query_compact, value) for value in values if value)


def _is_valid(row: LegalInstrument, as_of: date) -> bool:
    if row.effective_from and row.effective_from > as_of:
        return False
    if row.effective_to and row.effective_to <= as_of:
        return False
    # Status is a current projection.  It must not erase a historical version
    # whose effective interval still contains the requested as-of date.
    if row.status in {"repealed", "superseded"}:
        return bool(row.effective_to and row.effective_to > as_of)
    return True


def resolve_legal_context(db: Session, query: str, kb_ids: list[str], plan: RetrievalPlan,
                          policy: RetrievalPolicy) -> LegalContext | None:
    if not policy.legal_awareness or not kb_ids:
        return None
    if not db.query(LegalInstrument.id).filter(LegalInstrument.knowledge_base_id.in_(kb_ids)).first():
        return None

    as_of = plan.as_of_date or date.today()
    instruments = db.query(LegalInstrument).filter(LegalInstrument.knowledge_base_id.in_(kb_ids)).all()
    by_id = {row.id: row for row in instruments}
    provision_refs = parse_provision_refs(query)
    notes: list[str] = []

    families = db.query(LegalFamily).filter(LegalFamily.knowledge_base_id.in_(kb_ids)).all()
    query_compact = re.sub(r"\s+", "", query.casefold())
    matched_family_ids = {family.id for family in families if _family_matches(query_compact, family)}
    explicitly_named_family_ids = {family.id for family in families if _mentions(query_compact, family.normalized_key)}

    matched_instruments: set[str] = set()
    explicit_version_mention = any(
        (row.official_number and row.official_number in query)
        or (row.version_label and row.version_label.casefold() in query.casefold())
        for row in instruments
    )

    # A family-only query should resolve to the current consolidated
    # expression, not to an arbitrary amendment document.  Keep the legacy
    # relation traversal for explicit amendment/version queries and for test
    # corpora that do not have consolidated expressions.
    preferred_document_ids: list[str] = []
    ambiguous_context = False
    ambiguity_reason: str | None = None
    candidate_instrument_ids: list[str] = []
    consolidated_by_family: dict[str, list[LegalInstrument]] = {}
    for row in instruments:
        if row.family_id in matched_family_ids and row.document_class == "consolidated" and _is_valid(row, as_of):
            consolidated_by_family.setdefault(row.family_id, []).append(row)

    family_only_query = bool(matched_family_ids) and not explicit_version_mention
    if family_only_query:
        for family_id in matched_family_ids:
            candidates = sorted(
                consolidated_by_family.get(family_id, []),
                key=lambda row: (row.effective_from or date.min, row.version_date or date.min),
                reverse=True,
            )
            if len(candidates) == 1:
                matched_instruments.add(candidates[0].id)
                preferred_document_ids.append(candidates[0].document_id)
                notes.append("Selected the current consolidated legal instrument for the named family.")
            elif len(candidates) > 1:
                ambiguous_context = True
                candidate_instrument_ids.extend(row.id for row in candidates)
                ambiguity_reason = "Multiple current consolidated instruments match the legal family."
        # If a family has no consolidated expression, fall through to the
        # existing family/relation logic below.
        if not matched_instruments and not ambiguous_context:
            for row in instruments:
                if row.family_id in explicitly_named_family_ids:
                    matched_instruments.add(row.id)
    else:
        for row in instruments:
            if row.family_id in explicitly_named_family_ids:
                matched_instruments.add(row.id)
            elif row.official_number and row.official_number in query:
                matched_instruments.add(row.id)

    # No explicit instrument/number mention: fall back to searching provisions
    # across the KB scope for the cited section number (still deterministic).
    if not matched_instruments and provision_refs and not ambiguous_context:
        wanted_numbers = {re.sub(r"\s+", "", ref["number"]).casefold() for ref in provision_refs}
        provisions = db.query(Entity).filter(
            Entity.knowledge_base_id.in_(kb_ids), Entity.entity_type == "Provision", Entity.deleted_at.is_(None),
        ).all()
        document_by_id = {row.document_id: row for row in instruments}
        for provision in provisions:
            number = str((provision.attributes or {}).get("provision_number") or "")
            if re.sub(r"\s+", "", number).casefold() not in wanted_numbers:
                continue
            parts = provision.identity_key.split(":")
            document_id = parts[2] if len(parts) > 2 else None
            instrument = document_by_id.get(document_id)
            if instrument:
                matched_instruments.add(instrument.id)

        # When only a provision number (or a short family alias) was given,
        # prefer one current consolidated expression.  If the corpus cannot
        # prove which expression is current, return an explicit ambiguity
        # rather than generating an answer from a random amendment.
        if matched_family_ids:
            family_candidates = [
                row for row in instruments
                if row.family_id in matched_family_ids and row.document_class == "consolidated" and _is_valid(row, as_of)
            ]
            if len(family_candidates) == 1:
                matched_instruments = {family_candidates[0].id}
                preferred_document_ids = [family_candidates[0].document_id]
                notes.append("Selected the current consolidated legal instrument for the provision query.")
            elif len(family_candidates) > 1:
                ambiguous_context = True
                candidate_instrument_ids = [row.id for row in family_candidates]
                ambiguity_reason = "The provision number matches multiple current consolidated instruments."
                matched_instruments = set()

    current_version_ids: set[str] = set()
    amending_instrument_ids: set[str] = set()
    excluded_document_ids: set[str] = set()

    def visit(row: LegalInstrument) -> None:
        if _is_valid(row, as_of):
            current_version_ids.add(row.document_id)
        else:
            excluded_document_ids.add(row.document_id)
            notes.append(f"{row.official_title or row.document_id} is {row.status} as of {as_of.isoformat()}")

    relations_by_instrument: dict[str, list[LegalInstrumentRelation]] = {}
    if matched_instruments:
        # All verified relations touching any matched instrument, fetched once
        # instead of once per instrument.
        for relation in db.query(LegalInstrumentRelation).filter(
            LegalInstrumentRelation.knowledge_base_id.in_(kb_ids), LegalInstrumentRelation.review_status == "verified",
            or_(LegalInstrumentRelation.source_instrument_id.in_(matched_instruments),
                LegalInstrumentRelation.target_instrument_id.in_(matched_instruments)),
        ).all():
            if relation.source_instrument_id in matched_instruments:
                relations_by_instrument.setdefault(relation.source_instrument_id, []).append(relation)
            if relation.target_instrument_id in matched_instruments:
                relations_by_instrument.setdefault(relation.target_instrument_id, []).append(relation)

    for instrument_id in matched_instruments:
        row = by_id.get(instrument_id)
        if not row:
            continue
        visit(row)
        for relation in relations_by_instrument.get(instrument_id, []):
            if relation.relation == "AMENDS" and relation.target_instrument_id == instrument_id:
                amending_row = by_id.get(relation.source_instrument_id)
                if amending_row and _is_valid(amending_row, as_of):
                    amending_instrument_ids.add(amending_row.document_id)
                    current_version_ids.add(amending_row.document_id)
            elif relation.relation in _AMENDMENT_HOP_TYPES:
                other_id = relation.target_instrument_id if relation.source_instrument_id == instrument_id else relation.source_instrument_id
                other_row = by_id.get(other_id)
                if other_row:
                    visit(other_row)

    if plan.include_historical or not policy.exclude_invalid:
        excluded_document_ids = set()
    elif not matched_instruments:
        # No specific match: still sweep the whole KB scope so a stale FAQ or a
        # repealed notice never wins purely on similarity.
        for row in instruments:
            if not _is_valid(row, as_of):
                excluded_document_ids.add(row.document_id)

    if preferred_document_ids and matched_instruments:
        preferred_set = set(preferred_document_ids)
        for row in instruments:
            if row.family_id in matched_family_ids and not _is_valid(row, as_of):
                excluded_document_ids.add(row.document_id)

    return LegalContext(
        matched_instrument_ids=sorted(matched_instruments),
        current_version_ids=sorted(current_version_ids),
        amending_instrument_ids=sorted(amending_instrument_ids),
        excluded_document_ids=sorted(excluded_document_ids),
        preferred_document_ids=sorted(set(preferred_document_ids)),
        provision_refs=[ref["raw"] for ref in provision_refs],
        resolution_notes=notes,
        ambiguous_context=ambiguous_context,
        ambiguity_reason=ambiguity_reason,
        candidate_instrument_ids=sorted(set(candidate_instrument_ids)),
    )
