from .models import AuditLog, TraceRun, TraceSpan


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
        item["input_summary"] = {
            "query_sha256": metadata.get("query_sha256"),
            "knowledge_base_count": len(metadata.get("knowledge_base_ids") or []),
            "max_sources": (plan or {}).get("max_sources") if isinstance(plan, dict) else None,
        }
        item["output_summary"] = {
            "result_count": item.get("result_count", 0),
            "status": item.get("status"),
        }
        if item.get("status") == "skipped":
            item["reason_code"] = "not_selected_by_plan" if "not selected" in (item.get("detail") or "") else "channel_unavailable"
        safe_trace.append(item)
    correlation_id = str(request_id or "").strip()[:36] or None
    safe_plan = {key: plan[key] for key in ("version", "intent", "planner_source", "rationale", "channels", "graph_depth", "graph_scope", "entity_subjects", "document_identifiers", "published_from", "published_to", "as_of_date", "include_historical", "legal_context", "rerank_enabled", "max_sources", "fallback_reason") if key in plan} if isinstance(plan, dict) else None
    if safe_plan is not None and metadata.get("planner_policy_version") is not None:
        safe_plan["policy_version"] = metadata.get("planner_policy_version")
    if safe_plan and safe_plan.get("fallback_reason"):
        safe_plan["fallback_reason"] = _safe_trace_detail(safe_plan["fallback_reason"])
    if safe_plan and safe_plan.get("legal_context"):
        legal_context = safe_plan["legal_context"]
        safe_plan["legal_context"] = {
            **legal_context,
            "resolution_notes": [_safe_trace_detail(note) for note in (legal_context.get("resolution_notes") or [])[:10]],
        }
    trace_status = "error" if any(item.get("status") == "unavailable" for item in safe_trace) else (
        "warning" if (safe_plan and safe_plan.get("fallback_reason")) or result.get("insufficient_evidence") else "success"
    )
    trace_metadata = {
        "trace_id": correlation_id,
        "trace_status": trace_status,
        "request_id": correlation_id,
        "transport": transport,
        "tool": tool,
        "rpc_request_id": rpc_request_id[:100] if rpc_request_id else None,
        "result_id": result.get("result_id"),
        "source_count": len(result.get("sources") or []),
        "knowledge_base_ids": metadata.get("knowledge_base_ids") or [],
        "request_summary": {
            "query_preview": _safe_trace_detail(metadata.get("query_preview")),
            "query_length": metadata.get("query_length", 0),
            "query_sha256": metadata.get("query_sha256"),
            "filter_summary": metadata.get("filter_summary") or {},
        },
        "response_summary": {
            "answer_preview": _safe_trace_detail(metadata.get("answer_preview")),
            "citation_ids": metadata.get("citation_ids") or [],
            **(metadata.get("response_summary") or {}),
        },
        "retrieval_plan": safe_plan,
        "retrieval_trace": safe_trace,
    }
    record_audit(db, "retrieval.execution", actor_id, "http_request", correlation_id, trace_metadata)

    # Keep a normalized hot index alongside the immutable audit record.  This
    # makes filtering/pagination cheap as traces grow, while preserving the
    # JSON audit payload for backwards compatibility and export.
    if correlation_id:
        run = db.get(TraceRun, correlation_id)
        if run is None:
            run = TraceRun(id=correlation_id, request_id=correlation_id)
            db.add(run)
        run.transport = transport
        run.tool = tool
        run.trace_status = trace_status
        run.source_count = len(result.get("sources") or [])
        run.duration_ms = max((int(item.get("offset_ms", 0)) + int(item.get("duration_ms", 0)) for item in safe_trace), default=0)
        run.knowledge_base_ids = metadata.get("knowledge_base_ids") or []
        run.retrieval_plan = safe_plan
        run.request_summary = trace_metadata["request_summary"]
        run.response_summary = trace_metadata["response_summary"]
        db.flush()
        db.query(TraceSpan).filter(TraceSpan.trace_id == correlation_id).delete(synchronize_session=False)
        for item in safe_trace:
            db.add(TraceSpan(
                trace_id=correlation_id,
                span_id=item.get("span_id", "span"),
                parent_span_id=item.get("parent_span_id"),
                channel=item.get("channel", "unknown"),
                system=item.get("system"),
                status=item.get("status", "unknown"),
                result_count=int(item.get("result_count", 0) or 0),
                duration_ms=int(item.get("duration_ms", 0) or 0),
                offset_ms=int(item.get("offset_ms", 0) or 0),
                reason_code=item.get("reason_code"),
                detail=item.get("detail"),
                input_summary=item.get("input_summary") or {},
                output_summary=item.get("output_summary") or {},
            ))
