"""Rename the stored OPENROUTER_* error codes to the provider-neutral LLM_* codes.

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
}


def _rename(pairs) -> None:
    for table in ("documents", "processing_jobs"):
        for old, new in pairs:
            op.execute(sa.text(f"UPDATE {table} SET error_code = :new WHERE error_code = :old").bindparams(new=new, old=old))


def upgrade() -> None:
    _rename(_RENAMED.items())


def downgrade() -> None:
    _rename((new, old) for old, new in _RENAMED.items())
