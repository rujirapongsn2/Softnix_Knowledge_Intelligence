"""Import Markdown documents with their reference files through the Ingest API, resumably.

    INGEST_TOKEN=... python3 deploy/bulk-import/import_documents.py --kb KB_ID --dir CORPUS_DIR [--wait]

CORPUS_DIR holds pairs that share a name: `law-001.md` is the document and `law-001.pdf` (or .docx, .doc,
.xlsx, .xls, .txt) is its reference file, kept as evidence and offered for download but not processed.
A Markdown file without a reference is imported alone.

Progress is kept in CORPUS_DIR/.import-state.json, so a run that stops (network, restart, Ctrl-C) is
simply run again: documents already queued are not sent twice, and references not yet attached are.
A file the API reports as a duplicate is matched to the document already in the Knowledge Base by
its filename. --wait then polls until every imported document has settled and prints the result.
"""
import argparse
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

REFERENCE_SUFFIXES = (".pdf", ".docx", ".doc", ".xlsx", ".xls", ".txt")
SETTLED = {"completed", "failed", "ocr_required"}


def retry_after(exc, attempt):
    """Seconds to wait before sending again: the server's Retry-After when it gave one, else a doubling backoff."""
    try:
        return min(120, max(1, int(exc.headers.get("Retry-After"))))
    except (TypeError, ValueError):
        return min(60, 2 ** attempt)


class Api:
    def __init__(self, base, token):
        self.base, self.token = base.rstrip("/"), token

    def call(self, method, path, body=None, content_type=None):
        """JSON reply and HTTP status. Rate limits and outages are waited out; other errors are returned."""
        headers = {"Authorization": f"Bearer {self.token}", **({"Content-Type": content_type} if content_type else {})}
        for attempt in range(10):
            request = urllib.request.Request(f"{self.base}{path}", data=body, method=method, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=300) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 502, 503, 504) and attempt < 9:
                    time.sleep(retry_after(exc, attempt))
                    continue
                try:
                    return exc.code, json.load(exc)
                except ValueError:
                    return exc.code, {}
            except urllib.error.URLError:
                if attempt == 9:
                    raise
                time.sleep(min(60, 2 ** attempt))

    def upload(self, path, field_files):
        boundary = uuid.uuid4().hex
        parts = []
        for field, file in field_files:
            kind = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{file.name}"\r\n'
                         f"Content-Type: {kind}\r\n\r\n".encode() + file.read_bytes() + b"\r\n")
        return self.call("POST", path, b"".join(parts) + f"--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}")

    def documents(self, kb):
        """Every live document of the Knowledge Base, 100 per request."""
        offset, rows = 0, []
        while True:
            status, page = self.call("GET", f"/api/v1/ingest/knowledge-bases/{kb}/documents?limit=100&offset={offset}")
            if status != 200:
                sys.exit(f"listing documents failed: HTTP {status} {page}")
            rows += page["items"]
            offset += 100
            if offset >= page["total"]:
                return rows


def pairs(corpus):
    for document in sorted(corpus.glob("*.md")):
        reference = next((document.with_suffix(s) for s in REFERENCE_SUFFIXES if document.with_suffix(s).exists()), None)
        yield document, reference


def main():
    corpus = Path(args.dir)
    state_path = corpus / ".import-state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    save = lambda: state_path.write_text(json.dumps(state, ensure_ascii=False, indent=0))
    api = Api(args.api, os.environ["INGEST_TOKEN"])
    todo = [(doc, ref) for doc, ref in pairs(corpus) if not state.get(doc.name, {}).get("document_id")]
    print(json.dumps({"pairs": sum(1 for _ in pairs(corpus)), "to_upload": len(todo)}), flush=True)

    by_filename = None
    for start in range(0, len(todo), args.batch):
        batch = todo[start:start + args.batch]
        status, reply = api.upload(f"/api/v1/ingest/knowledge-bases/{args.kb}/documents/batch",
                                   [("files", doc) for doc, _ in batch])
        if status != 202:
            sys.exit(f"batch upload failed: HTTP {status} {reply}")
        for (doc, ref), result in zip(batch, reply["results"]):
            entry = state.setdefault(doc.name, {})
            entry["reference"] = ref.name if ref else None
            if result.get("document_id"):
                entry["document_id"] = result["document_id"]
            elif result.get("error_code") == "FILE_DUPLICATE":
                # Sent before but the reply was lost: the Knowledge Base already has it under this filename.
                if by_filename is None:
                    by_filename = {row["filename"]: row["document_id"] for row in api.documents(args.kb)}
                entry["document_id"] = by_filename.get(doc.name)
                entry["error"] = None if entry["document_id"] else "FILE_DUPLICATE of another filename"
            else:
                entry["error"] = result.get("error_code")
        save()
        print(json.dumps({"uploaded": min(start + args.batch, len(todo)), "of": len(todo)}), flush=True)

    attached = rejected = 0
    for name, entry in state.items():
        if not entry.get("document_id") or not entry.get("reference") or entry.get("reference_attached"):
            continue
        status, reply = api.upload(f"/api/v1/ingest/documents/{entry['document_id']}/reference-file",
                                   [("file", corpus / entry["reference"])])
        code = (reply.get("error") or reply.get("detail") or {}).get("code") if isinstance(reply, dict) else None
        if status == 200 or code == "REFERENCE_FILE_ALREADY_EXISTS":
            entry["reference_attached"], entry["reference_error"], attached = True, None, attached + 1
        else:
            entry["reference_error"], rejected = code or f"HTTP {status}", rejected + 1
        save()
    print(json.dumps({"references_attached": attached, "references_rejected": rejected}), flush=True)

    if args.wait:
        wanted = {entry["document_id"] for entry in state.values() if entry.get("document_id")}
        started = time.monotonic()
        while True:
            statuses = {row["document_id"]: row["status"] for row in api.documents(args.kb) if row["document_id"] in wanted}
            done = sum(status in SETTLED for status in statuses.values())
            print(json.dumps({"elapsed_seconds": round(time.monotonic() - started), "settled": done, "of": len(wanted)}), flush=True)
            if done == len(wanted):
                break
            time.sleep(args.poll)
        counts = {status: list(statuses.values()).count(status) for status in sorted(set(statuses.values()))}
        failed = sorted(name for name, entry in state.items() if statuses.get(entry.get("document_id")) == "failed")
        print(json.dumps({"summary": True, "documents": len(wanted), "statuses": counts, "failed": failed[:50],
                          "upload_errors": {n: e["error"] for n, e in state.items() if e.get("error")},
                          "reference_errors": {n: e["reference_error"] for n, e in state.items() if e.get("reference_error")}},
                         ensure_ascii=False), flush=True)


parser = argparse.ArgumentParser()
parser.add_argument("--api", default="http://127.0.0.1:8001")
parser.add_argument("--kb", required=True)
parser.add_argument("--dir", required=True)
parser.add_argument("--batch", type=int, default=20)
parser.add_argument("--wait", action="store_true")
parser.add_argument("--poll", type=int, default=30)
args = parser.parse_args()
main()
