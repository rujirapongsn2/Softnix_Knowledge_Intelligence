"""Retention/pruning for high-volume observability data.

Only observability actions are eligible for deletion. Domain audit events are
kept under the longer audit retention window and are never removed by this
routine.
"""
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .config import get_settings
from .models import AuditLog, TraceRun, TraceSpan


OBSERVABILITY_ACTIONS = {"request.transaction", "retrieval.execution", "mcp.tool.call", "mcp.tool.error"}


def prune_observability(db: Session, *, trace_days: int | None = None, audit_days: int | None = None) -> dict[str, int]:
    settings = get_settings()
    trace_cutoff = datetime.utcnow() - timedelta(days=trace_days if trace_days is not None else settings.observability_retention_days)
    audit_cutoff = datetime.utcnow() - timedelta(days=audit_days if audit_days is not None else settings.audit_retention_days)
    old_runs = db.query(TraceRun.id).filter(TraceRun.created_at < trace_cutoff).all()
    run_ids = [row[0] for row in old_runs]
    spans_deleted = db.query(TraceSpan).filter(TraceSpan.created_at < trace_cutoff).delete(synchronize_session=False)
    if run_ids:
        spans_deleted += db.query(TraceSpan).filter(TraceSpan.trace_id.in_(run_ids)).delete(synchronize_session=False)
    runs_deleted = db.query(TraceRun).filter(TraceRun.created_at < trace_cutoff).delete(synchronize_session=False)
    audit_deleted = db.query(AuditLog).filter(AuditLog.action.in_(OBSERVABILITY_ACTIONS), AuditLog.created_at < audit_cutoff).delete(synchronize_session=False)
    db.commit()
    return {"trace_runs_deleted": runs_deleted, "trace_spans_deleted": spans_deleted, "audit_events_deleted": audit_deleted}
