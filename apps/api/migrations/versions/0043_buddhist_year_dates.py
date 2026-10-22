"""Move dates stored with a Buddhist-era year into the Common Era.

Models answered ISO dates with the พ.ศ. year ("2550-10-01"), and the registry stored them as the year 2550, so
temporal legal retrieval treated those instruments as taking effect five centuries from now. Metadata date
fields had the same problem. Fixed here: the legal registry's dates, document date fields and their
candidates, and the indexed date projection.

The conversion is frozen here rather than imported, so later changes to the app cannot change this migration.
Downgrade does nothing: a corrected date cannot be told apart from one that was right before.

Revision ID: 0043_buddhist_year_dates
Revises: 0042_provider_holds
"""
import re
from datetime import date

import sqlalchemy as sa
from alembic import op


revision = "0043_buddhist_year_dates"
down_revision = "0042_provider_holds"
branch_labels = None
depends_on = None

_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def _gregorian(value: date) -> date | None:
    """The Common Era date of a date stored with its Buddhist year; None when it was already Common Era."""
    if value.year <= 2400:
        return None
    # Until พ.ศ. 2483 the Thai year began on 1 April, so January to March fall in the next Common Era year.
    year = value.year - 542 if value.year <= 2483 and value.month <= 3 else value.year - 543
    try:
        return date(year, value.month, value.day)
    except ValueError:
        return None


def _gregorian_text(value):
    match = _ISO.match(value) if isinstance(value, str) else None
    if not match:
        return None
    try:
        fixed = _gregorian(date(*(int(part) for part in match.groups())))
    except ValueError:
        return None
    return fixed.isoformat() if fixed else None


def upgrade() -> None:
    bind = op.get_bind()
    instruments = sa.table("legal_instruments", sa.column("id", sa.String), sa.column("effective_from", sa.Date),
                           sa.column("effective_to", sa.Date), sa.column("version_date", sa.Date))
    for row in bind.execute(sa.select(instruments)).mappings():
        changes = {name: fixed for name in ("effective_from", "effective_to", "version_date")
                   if row[name] is not None and (fixed := _gregorian(row[name]))}
        if changes:
            bind.execute(instruments.update().where(instruments.c.id == row["id"]).values(**changes))

    documents = sa.table("documents", sa.column("id", sa.String), sa.column("metadata_template_fields", sa.JSON),
                         sa.column("document_metadata", sa.JSON), sa.column("metadata_observations", sa.JSON))
    projection = sa.table("document_metadata_values", sa.column("document_id", sa.String), sa.column("field_key", sa.String),
                          sa.column("value_text", sa.String), sa.column("value_date", sa.Date))
    for row in bind.execute(sa.select(documents)).mappings():
        date_keys = {field.get("key") for field in row["metadata_template_fields"] or [] if isinstance(field, dict) and field.get("field_type") == "date"}
        if not date_keys:
            continue
        values, observations, fixed_values = dict(row["document_metadata"] or {}), dict(row["metadata_observations"] or {}), {}
        for key in date_keys:
            if (fixed := _gregorian_text(values.get(key))):
                fixed_values[key] = (values[key], fixed)
                values[key] = fixed
            observation = observations.get(key)
            if isinstance(observation, dict) and isinstance(observation.get("candidates"), list):
                candidates = [{**c, "value": _gregorian_text(c.get("value")) or c.get("value")} if isinstance(c, dict) else c
                              for c in observation["candidates"]]
                if candidates != observation["candidates"]:
                    observations[key] = {**observation, "candidates": candidates}
        if fixed_values or observations != (row["metadata_observations"] or {}):
            bind.execute(documents.update().where(documents.c.id == row["id"])
                         .values(document_metadata=values, metadata_observations=observations))
        for key, (old, new) in fixed_values.items():
            bind.execute(projection.update().where(projection.c.document_id == row["id"], projection.c.field_key == key,
                                                   projection.c.value_text == old)
                         .values(value_text=new, value_date=date.fromisoformat(new)))
    # A projection row whose document value was already fixed elsewhere still carries its Buddhist year.
    for row in bind.execute(sa.select(projection).where(projection.c.value_date.isnot(None))).mappings():
        if (fixed := _gregorian(row["value_date"])):
            bind.execute(projection.update().where(projection.c.document_id == row["document_id"], projection.c.field_key == row["field_key"],
                                                   projection.c.value_text == row["value_text"])
                         .values(value_date=fixed, value_text=fixed.isoformat()))


def downgrade() -> None:
    pass
