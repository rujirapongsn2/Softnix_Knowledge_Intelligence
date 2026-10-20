"""Send every searchable document's stored text to LightRAG again, after its storage was replaced.

Run inside the api image:
    python reingest.py [--workers 2] [--knowledge-base ID]

Only LightRAG indexing and the LightRAG graph sync run. Text extraction, chunks, embeddings,
document details and legal metadata are left as they are, so nothing is sent to the metadata LLM.
A document LightRAG already holds is reported as indexed again, so the script can be rerun.
Prints one JSON line per document and a summary line.
"""
import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from app.db import SessionLocal
from app.models import Document
from app.retrieval import LightRAGRetrievalEngine
from app.services import index_in_engine, sync_lightrag_document_graph


def reingest(document_id):
    started = time.monotonic()
    with SessionLocal() as db:
        doc = db.get(Document, document_id)
        try:
            # index_in_engine reports progress on a job; nothing here is a queued job.
            index_in_engine(db, LightRAGRetrievalEngine(), doc, doc.extracted_text, SimpleNamespace(progress_percent=0))
            db.commit()
            graph = sync_lightrag_document_graph(db, doc)
            db.commit()
            return {"document_id": document_id, "ok": True, "seconds": round(time.monotonic() - started, 1),
                    "chars": len(doc.extracted_text), **graph}
        except Exception as exc:
            db.rollback()
            return {"document_id": document_id, "ok": False, "seconds": round(time.monotonic() - started, 1),
                    "error": f"{type(exc).__name__}: {exc}"[:300]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--knowledge-base")
    args = parser.parse_args()
    with SessionLocal() as db:
        query = db.query(Document.id).filter(Document.live(), Document.status == "completed", Document.extracted_text.isnot(None))
        if args.knowledge_base:
            query = query.filter(Document.knowledge_base_id == args.knowledge_base)
        ids = [row.id for row in query.order_by(Document.created_at)]
    started = time.monotonic()
    results = []
    with ThreadPoolExecutor(args.workers) as pool:
        for result in pool.map(reingest, ids):
            results.append(result)
            print(json.dumps(result), flush=True)
    failed = [r for r in results if not r["ok"]]
    print(json.dumps({"summary": True, "documents": len(results), "failed": len(failed),
                      "seconds": round(time.monotonic() - started, 1)}), flush=True)
    sys.exit(1 if failed else 0)


main()
