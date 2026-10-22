"""Run metadata and legal extraction with two models on the same documents, writing nothing to the database.

    docker compose run --rm --no-deps -T -v "$PWD/deploy/model-compare:/eval:ro" api \
      python /eval/run_eval.py --baseline anthropic/claude-sonnet-5.5 --candidate openai/gpt-6-luna \
      [--per-kb 4] [--legal 20] [--workers 8] > results.jsonl

Samples documents that already have extracted metadata (the same ones for both models) and prints one JSON line per
result and a final usage line with the tokens and the cost the provider reported. Then: python3 analyze.py results.jsonl
"""
import argparse
import concurrent.futures as cf
import json
import sys
import threading
import time

import httpx
from sqlalchemy import text as sql

from app.db import SessionLocal
from app.legal_corpus import parse_legal_corpus_metadata
from app.llm import LLMClient
from app.metadata_extraction import extract_candidates, extraction_fields
from app.models import Document
from app.services import LEGAL_DOCUMENT_TYPES

parser = argparse.ArgumentParser()
parser.add_argument("--baseline", required=True)
parser.add_argument("--candidate", required=True)
parser.add_argument("--per-kb", type=int, default=4, help="documents sampled from each Knowledge Base")
parser.add_argument("--legal", type=int, default=20, help="legal documents whose registry entry needs the LLM")
parser.add_argument("--workers", type=int, default=8)
args = parser.parse_args()
MODELS = {"baseline": args.baseline, "candidate": args.candidate}
usage = {name: {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0} for name in MODELS}
lock = threading.Lock()
out_lock = threading.Lock()


def client_for(name):
    def record(response):
        if "/chat/completions" not in str(response.request.url):
            return
        response.read()
        try:
            used = response.json().get("usage") or {}
        except ValueError:
            return
        with lock:
            usage[name]["calls"] += 1
            usage[name]["prompt_tokens"] += used.get("prompt_tokens", 0)
            usage[name]["completion_tokens"] += used.get("completion_tokens", 0)
            usage[name]["cost"] += float(used.get("cost") or 0)
    client = LLMClient(httpx.Client(timeout=180, event_hooks={"response": [record]}))
    client.settings = client.settings.model_copy(update={"llm_model": MODELS[name]})
    return client


def emit(row):
    with out_lock:
        print(json.dumps(row, ensure_ascii=False, default=str), flush=True)


def metadata_job(doc_id, name):
    with SessionLocal() as db:
        doc = db.get(Document, doc_id)
        fields, text = extraction_fields(doc), doc.extracted_text or ""
    started = time.monotonic()
    try:
        observations = extract_candidates(fields, text, client_for(name))
        error = None
    except Exception as exc:
        observations, error = {}, str(exc)
    emit({"kind": "metadata", "model": name, "document_id": doc_id, "seconds": round(time.monotonic() - started, 1),
          "error": error, "fields": {f["key"]: f.get("field_type") for f in fields}, "observations": observations})


def legal_job(doc_id, name):
    with SessionLocal() as db:
        doc = db.get(Document, doc_id)
        title, text = doc.title or doc.original_filename, doc.extracted_text or ""
    started = time.monotonic()
    try:
        result, error = client_for(name).extract_legal_metadata(title, text), None
    except Exception as exc:
        result, error = {}, str(exc)
    emit({"kind": "legal", "model": name, "document_id": doc_id, "seconds": round(time.monotonic() - started, 1),
          "error": error, "result": result})


def main():
    with SessionLocal() as db:
        metadata_ids = [row[0] for row in db.execute(sql(
            "select id from (select d.id, row_number() over (partition by d.knowledge_base_id order by md5(d.id)) n "
            "from documents d where d.deleted_at is null and d.status = 'completed' "
            "and d.metadata_observations::text <> '{}') ranked where n <= :n"), {"n": args.per_kb})]
        legal_ids = []
        for doc in db.query(Document).filter(Document.document_type.in_(LEGAL_DOCUMENT_TYPES), Document.status == "completed",
                                             Document.deleted_at.is_(None)).order_by(sql("md5(id)")):
            if parse_legal_corpus_metadata(doc.extracted_text or "", doc.title or doc.original_filename).get("instrument", {}).get("version_role") == "unknown":
                legal_ids.append(doc.id)
            if len(legal_ids) == args.legal:
                break
    emit({"kind": "sample", "metadata_documents": len(metadata_ids), "legal_documents": len(legal_ids)})
    tasks = [(metadata_job, doc_id, name) for doc_id in metadata_ids for name in MODELS]
    tasks += [(legal_job, doc_id, name) for doc_id in legal_ids for name in MODELS]
    with cf.ThreadPoolExecutor(args.workers) as pool:
        for future in cf.as_completed([pool.submit(fn, doc_id, name) for fn, doc_id, name in tasks]):
            future.result()
    emit({"kind": "usage", "usage": usage})


main()
