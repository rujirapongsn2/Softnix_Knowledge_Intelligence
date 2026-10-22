"""Put existing documents under the metadata guardrails: the same review rules new documents get from their template.

    docker compose exec -T api python - < deploy/metadata/apply_review_guardrails.py           # dry run: counts only
    docker compose exec -T api python - --apply < deploy/metadata/apply_review_guardrails.py   # writes

Save the affected documents first (the script prints nothing it could restore from). Safe to run again.

1. An auto-accepted value in a cancellation, amendment or attachment field, or effective_date_end, becomes
   "suggested": still in use, but waiting for a person (as the templates now ask for every new document).
2. Each document's template snapshot gets the same review_policy, so a later re-evaluation cannot promote them again.
3. An effective_date_end value its quote does not support (the "today's date" the old instruction asked for) is
   removed, and the field becomes not_found.
"""
import sys
from collections import Counter

from app.audit import record_audit
from app.db import SessionLocal
from app.job_queue import lock_knowledge_base_graph
from app.legal_registry import parse_thai_date
from app.metadata_extraction import _date_from_quote, metadata_search_text, update_status, usable_values
from app.models import Document
from app.services import sync_document_metadata_graph, sync_document_metadata_values

RISKY = {"cancel_by", "cancel_law_by", "cancle_amended_section", "cancle_provision_law", "prior_amending_act", "effective_date_end", "attachment"}
apply = "--apply" in sys.argv
counts = Counter()

with SessionLocal() as db:
    ids = [row.id for row in db.query(Document.id).filter(Document.deleted_at.is_(None), Document.metadata_template_fields.isnot(None))]
    for document_id in ids:
        document = db.get(Document, document_id)
        lock_knowledge_base_graph(db, document.knowledge_base_id)
        document = db.query(Document).filter_by(id=document_id).populate_existing().with_for_update().one()
        fields = document.metadata_template_fields or []
        if not any(f.get("key") in RISKY for f in fields):
            db.rollback()
            continue
        observations = dict(document.metadata_observations or {})
        values = dict(document.document_metadata or {})
        new_fields = [{**f, "review_policy": "always"} if f.get("key") in RISKY and f.get("review_policy") != "always" else f for f in fields]
        changed = new_fields != fields
        counts["snapshots"] += changed
        for key in RISKY:
            observation = observations.get(key)
            if not isinstance(observation, dict) or observation.get("locked") or observation.get("origin") != "document_extraction":
                continue
            if key == "effective_date_end":
                candidates = observation.get("candidates") or []
                kept = [c for c in candidates if _date_from_quote(c["value"], c["evidence"]["quote"])]
                if len(kept) != len(candidates):
                    counts["effective_date_end_candidates_removed"] += len(candidates) - len(kept)
                    status = observation.get("status")
                    if not kept:
                        status = "not_found"
                    elif status == "auto_accepted":
                        status = "suggested"
                    if values.get(key) is not None and values[key] not in {c["value"] for c in kept}:
                        values.pop(key)
                        counts["effective_date_end_values_cleared"] += 1
                    observation = observations[key] = {**observation, "candidates": kept, "status": status}
                    changed = True
            if observation.get("status") == "auto_accepted":
                observations[key] = {**observation, "status": "suggested"}
                counts[f"demoted:{key}"] += 1
                changed = True
        if not changed:
            db.rollback()
            continue
        counts["documents"] += 1
        if not apply:
            db.rollback()
            continue
        document.metadata_template_fields = new_fields
        document.metadata_observations = observations
        document.document_metadata = values
        document.metadata_revision += 1
        document.metadata_search_text = metadata_search_text(new_fields, usable_values(document))
        update_status(document)
        sync_document_metadata_values(db, document)
        sync_document_metadata_graph(db, document)
        db.commit()
    if apply:
        record_audit(db, "document_metadata.guardrails_applied", None, "knowledge_base", None, dict(counts))
        db.commit()
print(("applied " if apply else "dry run ") + str(dict(sorted(counts.items()))))
