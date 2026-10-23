"""Processing errors per hour or day, stacked by group, for the status page.

A job keeps the error_code of its last failed attempt, also after a retry succeeds, so each job counts once, in
the hour or day it last changed, as "failed" when it ended failed and as "retried" when the platform retried it
on its own (completed since, or still queued or running).
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
# The chart's periods: what one bar covers, and how many bars.
RANGES = {"24h": ("hour", 24), "7d": ("day", 7), "30d": ("day", 30), "90d": ("day", 90)}


def error_group(code: str) -> str:
    if code in _EXACT:
        return _EXACT[code]
    return next((group for prefix, group in _PREFIXES if code.startswith(prefix)), "other")


def _bucket_start(moment: datetime, bucket: str, zone: ZoneInfo) -> datetime:
    if bucket == "hour":
        return moment.replace(minute=0, second=0, microsecond=0)
    return datetime.combine(moment.date(), time.min, zone)


def error_timeline(jobs, range_key: str, timezone_name: str, now: datetime | None = None) -> dict:
    """Count the jobs of ``jobs`` (a ProcessingJob query) that recorded an error, per local hour or day and group."""
    bucket, count = RANGES[range_key]
    zone = ZoneInfo(timezone_name)
    current = _bucket_start((now or datetime.now(timezone.utc)).astimezone(zone), bucket, zone)
    step = timedelta(hours=1) if bucket == "hour" else timedelta(days=1)
    starts = [_bucket_start(current - step * (count - 1 - index), bucket, zone) for index in range(count)]
    since = starts[0].astimezone(timezone.utc).replace(tzinfo=None)
    rows = jobs.filter(ProcessingJob.error_code.isnot(None), ProcessingJob.error_code.notin_(NOT_ERRORS),
                       ProcessingJob.status != "cancelled", ProcessingJob.updated_at >= since).with_entities(
        ProcessingJob.updated_at, ProcessingJob.error_code, ProcessingJob.status).all()
    series = {start: {"failed": dict.fromkeys(GROUPS, 0), "retried": dict.fromkeys(GROUPS, 0)} for start in starts}
    totals: dict[str, dict] = {}
    for updated_at, code, status in rows:
        start = _bucket_start(updated_at.replace(tzinfo=timezone.utc).astimezone(zone), bucket, zone)
        if start not in series:
            continue
        outcome, group = ("failed" if status == "failed" else "retried"), error_group(code)
        series[start][outcome][group] += 1
        total = totals.setdefault(code, {"error_code": code, "group": group, "failed": 0, "retried": 0})
        total[outcome] += 1
    return {
        "range": range_key, "bucket": bucket, "timezone": timezone_name, "groups": list(GROUPS),
        "series": [{"start": start.isoformat(), **counts} for start, counts in series.items()],
        "totals": sorted(totals.values(), key=lambda total: (-total["failed"], -total["retried"], total["error_code"])),
    }
