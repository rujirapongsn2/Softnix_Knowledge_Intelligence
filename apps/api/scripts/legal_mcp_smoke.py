"""Run the portable legal-MCP regression fixture against a live MCP endpoint.

Usage:
  python scripts/legal_mcp_smoke.py --token "$MCP_TOKEN"
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.request import Request, urlopen


DEFAULT_FIXTURE = Path(__file__).parents[1] / "tests" / "fixtures" / "land_law_mcp_regression.json"


def call_mcp(base_url: str, token: str, request_id: int, question: str) -> dict:
    body = json.dumps({
        "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
        "params": {"name": "search_knowledge", "arguments": {"query": question, "max_sources": 10}},
    }).encode("utf-8")
    request = Request(
        f"{base_url.rstrip('/')}/mcp", data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, method="POST",
    )
    with urlopen(request, timeout=120) as response:
        payload = json.load(response)
    return payload.get("result", {}).get("structuredContent", {})


def evaluate(case: dict, result: dict) -> dict:
    answer = str(result.get("answer") or "")
    normalized = answer.casefold()
    missing = [term for term in case["required_terms"] if term.casefold() not in normalized]
    sources = result.get("sources") or []
    citations_ok = not case.get("citation_required") or (
        bool(sources) and "รายละเอียดแหล่งอ้างอิง" in answer and any(source.get("source_uri") for source in sources)
    )
    if not missing and citations_ok:
        status = "pass"
    elif sources or answer:
        status = "partial"
    else:
        status = "fail"
    return {
        "id": case["id"], "status": status, "missing_terms": missing,
        "citation_ok": citations_ok, "source_count": len(sources), "answer": answer,
        "trace": result.get("metadata", {}).get("retrieval_trace", []),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", required=True, help="MCP bearer token (do not persist it)")
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--report", type=Path, help="Optional JSON report destination")
    parser.add_argument("--only", help="Comma-separated case IDs; useful for bounded live smoke batches")
    args = parser.parse_args()
    cases = json.loads(args.fixture.read_text(encoding="utf-8"))
    if args.only:
        requested = {case_id.strip() for case_id in args.only.split(",") if case_id.strip()}
        cases = [case for case in cases if case["id"] in requested]
    report = [evaluate(case, call_mcp(args.base_url, args.token, index, case["question"])) for index, case in enumerate(cases, 1)]
    summary = {status: sum(row["status"] == status for row in report) for status in ("pass", "partial", "fail")}
    output = {"summary": summary, "cases": report}
    print(json.dumps(output, ensure_ascii=False, indent=2))
    if args.report:
        args.report.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if summary["fail"] == summary["partial"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
