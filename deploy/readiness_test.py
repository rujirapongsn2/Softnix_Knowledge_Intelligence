"""End-to-end test that the installation is ready for an import, through the same path a real import takes.

    ADMIN_PASSWORD=... python3 deploy/readiness_test.py [--source-kb NAME_OR_ID] [--api http://127.0.0.1:8001]

Copies the document type of an existing Knowledge Base into a temporary one, imports three of its documents (small,
medium, large; the first one with a reference file gets it attached) with deploy/bulk-import/import_documents.py, waits
for extraction, then checks the result: documents completed, text stored in full, metadata extracted, no risky field
auto-accepted, a clean legal registry, no failed jobs, and both query modes answering from the sources. The temporary
Knowledge Base, its documents and its token are removed at the end. Costs a few cents of LLM credit.
Run it from the repository root, with the stack up. Exits non-zero when a check fails.
"""
import argparse
import http.cookiejar
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

RISKY_FIELDS = ("cancel_by", "cancel_law_by", "cancle_amended_section", "cancle_provision_law", "prior_amending_act",
                "effective_date_end", "attachment")
CONSOLIDATION_NOTES = "ยกเลิกโดย|แก้ไขเพิ่มเติมโดย"

parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
parser.add_argument("--api", default="http://127.0.0.1:8001")
parser.add_argument("--source-kb", help="name or id of the Knowledge Base to copy documents and document type from (default: the largest)")
args = parser.parse_args()
PASSWORD = os.environ.get("ADMIN_PASSWORD")
if not PASSWORD:
    sys.exit("set ADMIN_PASSWORD to the admin's password")

opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
checks = []


def call(method, path, body=None, raw=False):
    request = urllib.request.Request(f"{args.api}/api/v1{path}", data=json.dumps(body).encode() if body is not None else None,
                                     method=method, headers={"Content-Type": "application/json"})
    try:
        with opener.open(request, timeout=300) as response:
            return response.status, (response.read() if raw else json.load(response))
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def in_api(code):
    """Run Python in the api container (database and application code) and return its stdout."""
    return subprocess.run(["docker", "compose", "exec", "-T", "api", "python", "-c", code], capture_output=True, text=True, check=True).stdout


def sql(query):
    out = subprocess.run(["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "skip", "-d", "skip", "-At", "-F", "|", "-c", query],
                         capture_output=True, text=True, check=True).stdout
    return [line.split("|") for line in out.splitlines() if line]


def check(ok, text):
    checks.append(bool(ok))
    print(("  ok    " if ok else "  FAIL  ") + text, flush=True)


SELECT = f"""
import json
from sqlalchemy import func
from app.db import SessionLocal
from app.models import Document, DocumentMetadataTemplate, KnowledgeBase
from app.document_templates import custom_template_fields
wanted = {args.source_kb!r}
with SessionLocal() as db:
    base = db.query(Document).filter(Document.deleted_at.is_(None), Document.status == "completed", Document.metadata_template_id.isnot(None),
                                     Document.extracted_text.isnot(None), func.length(Document.extracted_text) > 1500)
    if wanted:
        kb = db.query(KnowledgeBase).filter((KnowledgeBase.id == wanted) | (KnowledgeBase.name == wanted)).first()
        base = base.filter(Document.knowledge_base_id == (kb.id if kb else wanted))
    else:
        top = base.with_entities(Document.knowledge_base_id, func.count()).group_by(Document.knowledge_base_id).order_by(func.count().desc()).first()
        base = base.filter(Document.knowledge_base_id == top[0]) if top else base.filter(False)
    docs = base.order_by(func.length(Document.extracted_text)).all()
    if len(docs) < 3:
        print(json.dumps({{"error": "the source Knowledge Base needs at least 3 completed documents with a document type"}}))
    else:
        picks = [docs[0], docs[len(docs) // 2], docs[-1]]
        with_reference = next((d for d in docs if d.reference_file_path), None)
        template = db.get(DocumentMetadataTemplate, picks[1].metadata_template_id)
        print(json.dumps({{
            "documents": [{{"id": d.id, "text": d.extracted_text}} for d in picks],
            "reference_document": with_reference.id if with_reference else None,
            "base_document_type": template.base_document_type, "fields": custom_template_fields(template)}}, ensure_ascii=False))
"""

picked = json.loads(in_api(SELECT))
if "error" in picked:
    sys.exit(picked["error"])
settings = json.loads(in_api("import json; from app.config import get_settings as g; s = g(); print(json.dumps([s.llm_model, s.llm_answer_model or s.llm_model, s.lightrag_llm_model or s.llm_model]))"))
print(f"extraction {settings[0]} | answers {settings[1]} | lightrag {settings[2]}", flush=True)

kb = token = None
workdir = pathlib.Path(tempfile.mkdtemp(prefix="readiness-"))
try:
    call("POST", "/auth/login", {"username": os.environ.get("ADMIN_USERNAME", "admin"), "password": PASSWORD})
    _, kb = call("POST", "/knowledge-bases", {"name": "[readiness-test] temporary"})
    call("POST", f"/knowledge-bases/{kb['id']}/activate")
    status, template = call("POST", f"/knowledge-bases/{kb['id']}/document-templates",
                            {"name": "readiness test", "base_document_type": picked["base_document_type"], "fields": picked["fields"]})
    check(status == 200, f"document type copied with {len(picked['fields'])} fields (HTTP {status})")
    _, token = call("POST", "/tokens", {"name": "readiness-test", "allowed_knowledge_base_ids": [], "allowed_tools": [],
                                        "allowed_scopes": ["documents:write"], "allowed_ingest_knowledge_base_id": kb["id"]})
    check((token["requests_per_minute"], token["max_concurrent_requests"]) == (600, 10), "a new ingest token gets 600 per minute, 10 concurrent")

    sent = {}
    for index, document in enumerate(picked["documents"], start=1):
        name = f"readiness-{index}"
        # A marker makes the text new to LightRAG, which refuses text it already holds.
        (workdir / f"{name}.md").write_text(document["text"] + f"\n\n<!-- readiness test {time.time()} -->\n")
        sent[f"{name}.md"] = len(document["text"])
        if document["id"] == picked["reference_document"]:
            _, pdf = call("GET", f"/documents/{document['id']}/file?variant=reference_file", raw=True)
            (workdir / f"{name}.pdf").write_bytes(pdf)
    if picked["reference_document"] and not any(workdir.glob("*.pdf")):
        _, pdf = call("GET", f"/documents/{picked['reference_document']}/file?variant=reference_file", raw=True)
        (workdir / "readiness-1.pdf").write_bytes(pdf)

    started = time.monotonic()
    result = subprocess.run([sys.executable, "deploy/bulk-import/import_documents.py", "--api", args.api, "--kb", kb["id"], "--dir", str(workdir),
                             "--document-type-id", template["id"], "--wait", "--poll", "10"],
                            env={**os.environ, "INGEST_TOKEN": token["token"]}, capture_output=True, text=True, timeout=3600)
    if result.returncode:
        print(result.stdout[-1500:], result.stderr[-1500:])
    summary = json.loads(result.stdout.strip().splitlines()[-1]) if result.stdout.strip() else {}
    check(summary.get("statuses") == {"completed": 3}, f"3 documents completed in {round(time.monotonic() - started)} s: {summary.get('statuses')}")
    check(not summary.get("reference_errors") and (not picked["reference_document"] or any(workdir.glob("*.pdf"))), "the reference file was attached")

    pending = "1"
    for _ in range(90):
        pending = sql(f"select count(*) from processing_jobs where knowledge_base_id = '{kb['id']}' and status in ('queued', 'running')")[0][0]
        if pending == "0":
            break
        time.sleep(10)
    check(pending == "0", "metadata and legal follow-up jobs finished")
    failed = sql(f"select job_type, error_code from processing_jobs where knowledge_base_id = '{kb['id']}' and status = 'failed'")
    check(not failed, f"no failed jobs {failed or ''}")

    stored = {row[0]: int(row[1]) for row in sql(f"select original_filename, length(extracted_text) from documents where knowledge_base_id = '{kb['id']}' and deleted_at is null")}
    check(all(stored.get(name, 0) >= chars for name, chars in sent.items()), f"the full text was stored: sent {sent}, stored {stored}")

    rows = sql("select o.key, o.value->>'status', o.value::text ~ '" + CONSOLIDATION_NOTES + "' from documents d, json_each(d.metadata_observations::json) o "
               f"where d.knowledge_base_id = '{kb['id']}' and d.deleted_at is null")
    statuses = {}
    for _, status, _ in rows:
        statuses[status] = statuses.get(status, 0) + 1
    found = sum(count for status, count in statuses.items() if status != "not_found")
    expected = 3 * sum(1 for field in picked["fields"] if field.get("fill_mode") == "extract")
    check(len(rows) == expected and found >= 3, f"metadata was extracted ({len(rows)} of {expected} fields observed, {found} with a value): {statuses}")
    check(not [r for r in rows if r[0] in RISKY_FIELDS and r[1] == "auto_accepted"], "no cancellation, amendment or attachment field was auto-accepted")
    check(not [r for r in rows if r[2] == "t" and r[1] == "auto_accepted"], "no value quoting a consolidation note was auto-accepted")
    registry = "from legal_instruments i join documents d on d.id = i.document_id where d.knowledge_base_id = '" + kb["id"] + "'"
    instruments = sql("select count(*) " + registry)[0][0]
    bad_dates = sql("select count(*) " + registry + " and (i.effective_from > current_date or extract(year from i.effective_from) > 2400)")[0][0]
    check(bad_dates == "0", f"{instruments} legal instruments registered, none dated in the future or the Buddhist era")

    for mode in ("evidence", "answer"):
        started = time.monotonic()
        status, reply = call("POST", "/query", {"query": "เอกสารนี้ใช้บังคับเมื่อใด และใครเป็นผู้ลงนาม", "knowledge_base_ids": [kb["id"]],
                                                **({"response_mode": "evidence"} if mode == "evidence" else {})})
        data = reply.get("data", reply)
        sources, answer = data.get("sources") or [], data.get("answer") or ""
        answered = mode == "evidence" or (len(answer) > 40 and not data.get("insufficient_evidence") and "ไม่เพียงพอ" not in answer[:60])
        check(status == 200 and sources and answered, f"query ({mode}) took {time.monotonic() - started:.1f} s with {len(sources)} sources"
              + (f": {answer[:100]!r}" if mode == "answer" else ""))
finally:
    if kb:
        documents = call("GET", f"/knowledge-bases/{kb['id']}/documents")[1]
        for document in documents if isinstance(documents, list) else documents.get("items", []):
            call("DELETE", f"/documents/{document['id']}")
        for _ in range(40):
            if call("DELETE", f"/knowledge-bases/{kb['id']}")[0] == 200:
                print("  cleanup: the temporary Knowledge Base was deleted")
                break
            time.sleep(15)
    if token:
        call("POST", f"/tokens/{token['id']}/revoke")
    for path in workdir.glob("*"):
        path.unlink()
    workdir.rmdir()
print("READY" if checks and all(checks) else "NOT READY", f"({sum(checks)}/{len(checks)} checks)")
sys.exit(0 if checks and all(checks) else 1)
