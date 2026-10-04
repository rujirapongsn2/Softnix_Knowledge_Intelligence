import logging
import time

from .db import SessionLocal
from .remote_index import recover_unavailable_documents
from .retention import prune_observability
from .retrieval import LightRAGRetrievalEngine
from .services import process_next_graph_projection, process_next_job

if __name__ == "__main__":
    last_retention = 0.0
    last_recovery = 0.0
    while True:
        with SessionLocal() as db:
            processed_document = process_next_job(db)
            processed_graph = process_next_graph_projection(db)
            processed = processed_document or processed_graph
            now = time.monotonic()
            if now - last_retention >= 3600:
                prune_observability(db)
                last_retention = now
            if now - last_recovery >= 300:
                last_recovery = now
                try:
                    recover_unavailable_documents(db, LightRAGRetrievalEngine())
                except Exception:  # a failed sweep must never stop document processing
                    logging.getLogger(__name__).exception("recovery sweep failed")
        time.sleep(0.5 if processed else 2)
