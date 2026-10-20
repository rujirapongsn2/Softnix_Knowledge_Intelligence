"""Import one stage of a bulk load through the Ingest API and measure what it costs the stack.

Run on the docker host from the repository root:
    INGEST_TOKEN=... python3 deploy/lightrag-storage/stage_test.py --kb KB_ID --dir CORPUS_DIR --count 100

Each run sends the next --count files of CORPUS_DIR that earlier runs have not sent (recorded in
CORPUS_DIR/.stage-state.json), waits until every one of them has settled, and samples memory of
LightRAG, Postgres and Neo4j, LightRAG's Postgres tables and, with OPENROUTER_API_KEY set,
OpenRouter spend. Samples go to stdout as JSON lines, ending with a summary line. Grow --count
stage by stage (for example 100, 400, 1000) and compare the summaries before sending more.
"""
import argparse
import json
import mimetypes
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

SETTLED = {"completed", "failed", "ocr_required"}
SERVICES = ("lightrag", "postgres", "neo4j")


def request(args, method, path, body=None, headers=None):
    req = urllib.request.Request(f"{args.api.rstrip('/')}{path}", data=body, method=method,
                                 headers={"Authorization": f"Bearer {args.token}", **(headers or {})})
    for attempt in range(8):
        try:
            with urllib.request.urlopen(req, timeout=120) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 503) or attempt == 7:
                raise
            time.sleep(2 ** attempt)


def upload(args, files):
    boundary = uuid.uuid4().hex
    parts = []
    for path in files:
        kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{path.name}"\r\n'
                     f"Content-Type: {kind}\r\n\r\n".encode() + path.read_bytes() + b"\r\n")
    body = b"".join(parts) + f"--{boundary}--\r\n".encode()
    return request(args, "POST", f"/api/v1/ingest/knowledge-bases/{args.kb}/documents/batch", body,
                   {"Content-Type": f"multipart/form-data; boundary={boundary}"})["results"]


def memory_mib():
    out = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.Name}} {{.MemUsage}}"],
                         capture_output=True, text=True).stdout
    usage = {}
    for line in out.splitlines():
        name, _, mem = line.partition(" ")
        service = next((s for s in SERVICES if name.startswith(f"{args.project}-{s}-")), None)
        match = re.match(r"([\d.]+)(KiB|MiB|GiB)", mem)
        if service and match:
            usage[service] = round(float(match[1]) * {"KiB": 1 / 1024, "MiB": 1, "GiB": 1024}[match[2]])
    return usage


def lightrag_table_mib():
    out = subprocess.run(["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "skip", "-d", "skip", "-Atc",
                          "select coalesce(sum(pg_total_relation_size(oid)),0) from pg_class where relname like 'lightrag%' and relkind='r'"],
                         capture_output=True, text=True).stdout.strip()
    return round(int(out or 0) / 2 ** 20)


def openrouter_usage():
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return None
    req = urllib.request.Request("https://openrouter.ai/api/v1/key", headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.load(response)["data"]["usage"]


def main():
    corpus = Path(args.dir)
    state_path = corpus / ".stage-state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {"sent": {}}
    pending = [p for p in sorted(corpus.iterdir()) if p.is_file() and not p.name.startswith(".") and p.name not in state["sent"]]
    stage = pending[:args.count]
    if not stage:
        sys.exit("no unsent files left in the corpus")
    spend_before, started = openrouter_usage(), time.monotonic()
    ids, rejected = [], {}
    for start in range(0, len(stage), args.batch):
        for result in upload(args, stage[start:start + args.batch]):
            if result.get("document_id"):
                ids.append(result["document_id"])
                state["sent"][result["filename"]] = result["document_id"]
            else:
                rejected[result["filename"]] = result.get("error_code")
                state["sent"][result["filename"]] = None
        state_path.write_text(json.dumps(state))
    print(json.dumps({"stage_files": len(stage), "queued": len(ids), "rejected": rejected,
                      "upload_seconds": round(time.monotonic() - started, 1)}), flush=True)

    statuses, peak = {}, {}
    while True:
        # Paging the Knowledge Base costs one request per 100 documents against the token's rate limit.
        offset, wanted = 0, set(ids)
        while True:
            page = request(args, "GET", f"/api/v1/ingest/knowledge-bases/{args.kb}/documents?limit=100&offset={offset}")
            statuses.update({item["document_id"]: item["status"] for item in page["items"] if item["document_id"] in wanted})
            offset += 100
            if offset >= page["total"]:
                break
        memory = memory_mib()
        peak = {s: max(peak.get(s, 0), memory.get(s, 0)) for s in SERVICES}
        done = sum(s in SETTLED for s in statuses.values())
        print(json.dumps({"elapsed_seconds": round(time.monotonic() - started), "settled": done, "of": len(ids),
                          "memory_mib": memory, "lightrag_tables_mib": lightrag_table_mib()}), flush=True)
        if done == len(ids):
            break
        time.sleep(args.poll)

    hours = (time.monotonic() - started) / 3600
    spend_after = openrouter_usage()
    counts = {status: list(statuses.values()).count(status) for status in sorted(set(statuses.values()))}
    spent = None if spend_before is None else round(spend_after - spend_before, 3)
    print(json.dumps({"summary": True, "documents": len(ids), "statuses": counts, "hours": round(hours, 2),
                      "documents_per_hour": round(len(ids) / hours, 1), "failure_rate": round(counts.get("failed", 0) / len(ids), 3),
                      "peak_memory_mib": peak, "lightrag_tables_mib": lightrag_table_mib(),
                      "openrouter_usd": spent, "usd_per_document": None if spent is None else round(spent / len(ids), 4)}), flush=True)


parser = argparse.ArgumentParser()
parser.add_argument("--api", default="http://127.0.0.1:8001")
parser.add_argument("--kb", required=True)
parser.add_argument("--dir", required=True)
parser.add_argument("--count", type=int, default=100)
parser.add_argument("--batch", type=int, default=20)
parser.add_argument("--poll", type=int, default=30)
parser.add_argument("--project", default="softnix-knowledge-intelligence")
args = parser.parse_args()
args.token = os.environ["INGEST_TOKEN"]
main()
