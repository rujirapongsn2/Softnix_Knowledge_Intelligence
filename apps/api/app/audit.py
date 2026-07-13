from .models import AuditLog


def record_audit(db, action: str, actor_id: str | None = None, target_type: str | None = None,
                 target_id: str | None = None, metadata: dict | None = None) -> None:
    db.add(AuditLog(actor_user_id=actor_id, action=action, target_type=target_type,
                    target_id=target_id, metadata_json=metadata or {}))


def _safe_trace_detail(value) -> str | None:
    """Keep an operator hint without persisting provider payloads or credentials."""
    if not value:
        return None
    text = str(value).replace("\n", " ")
    for marker in ("Bearer ", "bearer ", "sk-or-", "skik_live_"):
        if marker in text:
            text = text.split(marker, 1)[0] + "[redacted]"
    return text[:240]


def record_retrieval_execution(db, request_id: str | None, result: dict, *, actor_id: str | None = None,
                               transport: str = "api", tool: str | None = None, rpc_request_id: str | None = None) -> None:
    """Write a transaction-correlated, secret-free RetrievalExecutor trace."""
    metadata = result.get("metadata") or {}
    plan = metadata.get("retrieval_plan")
    trace = metadata.get("retrieval_trace") or []
    if not plan and not trace:
        return
    raw_starts = [step.get("started_at_ms") for step in trace if isinstance(step, dict) and isinstance(step.get("started_at_ms"), (int, float))]
    trace_start = min(raw_starts) if raw_starts else 0
    safe_trace = []
    for step in trace:
        if not isinstance(step, dict):
            continue
        item = {key: step[key] for key in ("channel", "system", "status", "result_count", "duration_ms") if key in step}
        item["span_id"] = f"span-{len(safe_trace) + 1}"
        item["parent_span_id"] = "root"
        raw_start = step.get("started_at_ms")
        item["offset_ms"] = max(0, round(raw_start - trace_start)) if isinstance(raw_start, (int, float)) else sum(previous.get("duration_ms", 0) for previous in safe_trace)
        detail = _safe_trace_detail(step.get("detail"))
        if detail:
            item["detail"] = detail
        safe_trace.append(item)
    correlation_id = str(request_id or "").strip()[:36] or None
    safe_plan = {key: plan[key] for key in ("intent", "planner_source", "channels", "graph_depth", "graph_scope", "entity_subjects", "document_identifiers", "published_from", "published_to", "rerank_enabled", "max_sources", "fallback_reason") if key in plan} if isinstance(plan, dict) else None
    if safe_plan and safe_plan.get("fallback_reason"):
        safe_plan["fallback_reason"] = _safe_trace_detail(safe_plan["fallback_reason"])
    trace_status = "error" if any(item.get("status") == "unavailable" for item in safe_trace) else (
        "warning" if any(item.get("status") == "skipped" for item in safe_trace) else "success"
    )
    record_audit(db, "retrieval.execution", actor_id, "http_request", correlation_id, {
        "trace_id": correlation_id,
        "trace_status": trace_status,
        "request_id": correlation_id,
        "transport": transport,
        "tool": tool,
        "rpc_request_id": rpc_request_id[:100] if rpc_request_id else None,
        "result_id": result.get("result_id"),
        "source_count": len(result.get("sources") or []),
        "knowledge_base_ids": metadata.get("knowledge_base_ids") or [],
        "retrieval_plan": safe_plan,
        "retrieval_trace": safe_trace,
    })
