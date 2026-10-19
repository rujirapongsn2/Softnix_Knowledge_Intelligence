"""How often the AI's metadata was right, per template field, and which values a person should look at next."""
from sqlalchemy.orm import load_only

from .metadata_extraction import FIELD_STATES, field_review_states
from .models import Document

STATES = ("verified", "auto_verified", "unverified", "blocked", "missing", "unchecked", "not_found")
OUTCOMES = ("confirmed", "corrected", "rejected")
NEXT_ACTIONS_LIMIT = 200


def _accuracy(outcomes):
    reviewed = sum(outcomes.values())
    return round(outcomes["confirmed"] / reviewed, 4) if reviewed else None


def _improvement_rank(row):
    # The field whose instruction most needs rewriting first: the AI is wrong most often, then most values are blocked.
    accuracy = row["ai_accuracy"]
    return (1.0 if accuracy is None else accuracy, -row["counts"]["blocked"], -row["counts"]["missing"], -row["counts"]["unverified"], row["label"])


def metadata_quality_report(db, kb_id):
    documents = db.query(Document).options(load_only(
        Document.id, Document.title, Document.original_filename, Document.mime_type, Document.metadata_status, Document.metadata_template_id,
        Document.metadata_template_name, Document.metadata_template_fields, Document.document_metadata, Document.metadata_observations,
    )).filter(Document.knowledge_base_id == kb_id, Document.live()).order_by(Document.created_at.desc())
    rows, actions = {}, []
    for doc in documents:
        values, observations = doc.document_metadata or {}, doc.metadata_observations or {}
        states = field_review_states(doc.metadata_template_fields, values, observations)
        for field in doc.metadata_template_fields or []:
            if field["key"] not in states:
                continue
            state, observation = states[field["key"]], observations.get(field["key"], {})
            row = rows.setdefault((doc.metadata_template_id, field["key"]), {
                "template_id": doc.metadata_template_id, "template_name": doc.metadata_template_name, "field_key": field["key"],
                "label": field.get("label") or field["key"], "filterable": bool(field.get("filterable")),
                "counts": dict.fromkeys(STATES, 0), "outcomes": dict.fromkeys(OUTCOMES, 0)})
            row["counts"][state] += 1
            if observation.get("review_outcome") in OUTCOMES:
                row["outcomes"][observation["review_outcome"]] += 1
            # A document still being extracted has nothing for a person yet.
            if doc.metadata_status in {"queued", "running", "not_started"}:
                continue
            if FIELD_STATES[state] == "needs_attention" or (state == "unverified" and row["filterable"]):
                candidates = observation.get("candidates") or [{}]
                actions.append({"document_id": doc.id, "title": doc.title or doc.original_filename, "original_filename": doc.original_filename,
                                "mime_type": doc.mime_type, "template_id": doc.metadata_template_id,
                                "field_key": field["key"], "label": row["label"], "state": state,
                                "value": values.get(field["key"], candidates[0].get("value")), "candidate_count": len(observation.get("candidates") or [])})
    for row in rows.values():
        row["ai_accuracy"] = _accuracy(row["outcomes"])
    fields = sorted(rows.values(), key=_improvement_rank)
    rank = {(row["template_id"], row["field_key"]): index for index, row in enumerate(fields)}
    actions.sort(key=lambda a: (FIELD_STATES[a["state"]] != "needs_attention", rank[(a["template_id"], a["field_key"])], a["title"]))
    return {"fields": fields, "next_actions": actions[:NEXT_ACTIONS_LIMIT], "next_actions_total": len(actions)}
