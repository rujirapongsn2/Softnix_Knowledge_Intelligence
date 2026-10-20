"""Rename the stored OPENROUTER_* error codes to the provider-neutral LLM_* codes.

Error codes are renamed in documents and processing_jobs, and also inside query traces and saved query results,
where they appear as span details and planner fallback reasons, together with the old trace system names.

Revision ID: 0041_llm_error_codes
Revises: 0040_metadata_review_states
"""
import sqlalchemy as sa
from alembic import op


revision = "0041_llm_error_codes"
down_revision = "0040_metadata_review_states"
branch_labels = None
depends_on = None

_RENAMED = {
    "OPENROUTER_UNAVAILABLE": "LLM_UNAVAILABLE",
    "OPENROUTER_AUTH_FAILED": "LLM_AUTH_FAILED",
    "OPENROUTER_CREDITS_EXHAUSTED": "LLM_CREDITS_EXHAUSTED",
    "OPENROUTER_REQUEST_REJECTED": "LLM_REQUEST_REJECTED",
    "OPENROUTER_LLM_INVALID_RESPONSE": "LLM_INVALID_RESPONSE",
    "OPENROUTER_API_KEY_NOT_CONFIGURED": "LLM_API_KEY_NOT_CONFIGURED",
    "OPENROUTER_PLANNER_UNAVAILABLE": "LLM_PLANNER_UNAVAILABLE",
    "OPENROUTER_PLANNER_INVALID_RESPONSE": "LLM_PLANNER_INVALID_RESPONSE",
    "OPENROUTER_SOURCES_REQUIRED": "LLM_SOURCES_REQUIRED",
}
_SYSTEMS = {"OpenRouter LLM": "LLM", "OpenRouter reranker": "Reranker"}
_JSON_COLUMNS = (("trace_runs", "retrieval_plan"), ("trace_runs", "request_summary"),
                 ("trace_runs", "response_summary"), ("query_results", "result_json"))


def _rename(codes, systems) -> None:
    postgres = op.get_bind().dialect.name == "postgresql"
    for old, new in codes:
        for table in ("documents", "processing_jobs"):
            op.execute(sa.text(f"UPDATE {table} SET error_code = :new WHERE error_code = :old").bindparams(new=new, old=old))
        op.execute(sa.text("UPDATE trace_spans SET detail = REPLACE(detail, :old, :new) WHERE detail LIKE :pattern")
                   .bindparams(old=old, new=new, pattern=f"%{old}%"))
        if postgres:
            for table, column in _JSON_COLUMNS:
                op.execute(sa.text(f"UPDATE {table} SET {column} = REPLACE({column}::text, :old, :new)::json "
                                   f"WHERE {column}::text LIKE :pattern").bindparams(old=old, new=new, pattern=f"%{old}%"))
    for old, new in systems:
        op.execute(sa.text("UPDATE trace_spans SET system = :new WHERE system = :old").bindparams(new=new, old=old))


def upgrade() -> None:
    _rename(_RENAMED.items(), _SYSTEMS.items())


def downgrade() -> None:
    _rename([(new, old) for old, new in _RENAMED.items()], [(new, old) for old, new in _SYSTEMS.items()])
