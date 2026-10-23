"""Processing errors per day, stacked by group, for the status page.

A job keeps the error_code of its last failed attempt, also after a retry succeeds, so each job counts once, on
the day it last changed, as "failed" when it ended failed and as "retried" when the platform retried it on its
own (completed since, or still queued or running).
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .models import ProcessingJob

GROUPS = ("retrieval_engine", "ai_provider", "ocr_extraction", "metadata", "index_cleanup", "other")
_EXACT = {
    "RETRIEVAL_ENGINE_BUDGET_EXHAUSTED": "ai_provider",  # the LLM provider's 402, relayed by LightRAG
    "REMOTE_INDEX_PURGE_FAILED": "index_cleanup",
    "ANYDOC_UNAVAILABLE": "ocr_extraction",
}
_PREFIXES = (
    ("RETRIEVAL_ENGINE_", "retrieval_engine"), ("LLM_", "ai_provider"), ("EMBEDDING_", "ai_provider"),
    ("EXTERNAL_OCR_", "ocr_extraction"), ("OCR_", "ocr_extraction"), ("TEXT_EXTRACTION_", "ocr_extraction"),
    ("METADATA_", "metadata"),
)
# A job cancelled because its document was deleted did not fail.
NOT_ERRORS = frozenset({"DOCUMENT_DELETED"})
TIMELINE_DAYS = (7, 30, 90)


def error_group(code: str) -> str:
    if code in _EXACT:
        return _EXACT[code]
    return next((group for prefix, group in _PREFIXES if code.startswith(prefix)), "other")


def error_timeline(jobs, days: int, timezone_name: str, now: datetime | None = None) -> dict:
    """Count the jobs of ``jobs`` (a ProcessingJob query) that recorded an error, per local day and group."""
    zone = ZoneInfo(timezone_name)
    today = (now or datetime.now(timezone.utc)).astimezone(zone).date()
    first = today - timedelta(days=days - 1)
    since = datetime.combine(first, time.min, zone).astimezone(timezone.utc).replace(tzinfo=None)
    rows = jobs.filter(ProcessingJob.error_code.isnot(None), ProcessingJob.error_code.notin_(NOT_ERRORS),
                       ProcessingJob.status != "cancelled", ProcessingJob.updated_at >= since).with_entities(
        ProcessingJob.updated_at, ProcessingJob.error_code, ProcessingJob.status).all()
    series = {first + timedelta(days=offset): {"failed": dict.fromkeys(GROUPS, 0), "retried": dict.fromkeys(GROUPS, 0)}
              for offset in range(days)}
    totals: dict[str, dict] = {}
    for updated_at, code, status in rows:
        day = updated_at.replace(tzinfo=timezone.utc).astimezone(zone).date()
        if day not in series:
            continue
        outcome, group = ("failed" if status == "failed" else "retried"), error_group(code)
        series[day][outcome][group] += 1
        total = totals.setdefault(code, {"error_code": code, "group": group, "failed": 0, "retried": 0})
        total[outcome] += 1
    return {
        "days": days, "timezone": timezone_name, "groups": list(GROUPS),
        "series": [{"date": day.isoformat(), **counts} for day, counts in series.items()],
        "totals": sorted(totals.values(), key=lambda total: (-total["failed"], -total["retried"], total["error_code"])),
    }
