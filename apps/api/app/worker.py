import logging
import time

from .db import SessionLocal
from .document_recovery import recover_unavailable_documents
from .job_queue import sweep_stale_jobs, try_singleton
from .retention import prune_observability
from .retrieval import LightRAGRetrievalEngine
from .services import process_next_graph_projection, process_next_job

logger = logging.getLogger(__name__)


def run_once(state: dict[str, float]) -> bool:
    """One pass of the worker loop. Returns whether any job or event was processed."""
    with SessionLocal() as db:
        processed_document = process_next_job(db)
        processed_graph = process_next_graph_projection(db)
        now = time.monotonic()
        if now - state["stale_sweep"] >= 60:
            state["stale_sweep"] = now
            try:
                released = sweep_stale_jobs(db)
                if released:
                    logger.warning("stale-job sweep re-queued %s job(s)", released)
            except Exception:  # a failed sweep must never stop document processing
                db.rollback()
                logger.exception("stale-job sweep failed")
        if now - state["retention"] >= 3600:
            state["retention"] = now
            try:
                if try_singleton(db, "observability-retention"):
                    prune_observability(db)
                db.rollback()
            except Exception:
                db.rollback()
                logger.exception("observability retention failed")
        if now - state["recovery"] >= 300:
            state["recovery"] = now
            try:
                if try_singleton(db, "unavailable-document-recovery"):
                    recover_unavailable_documents(db, LightRAGRetrievalEngine())
                db.rollback()
            except Exception:  # a failed sweep must never stop document processing
                db.rollback()
                logger.exception("recovery sweep failed")
        return processed_document or processed_graph


# Several worker replicas may run this loop: job claims are atomic
# (job_queue.claim_next_job) and each periodic sweep runs in one process at a time.
if __name__ == "__main__":
    state = {"retention": 0.0, "recovery": 0.0, "stale_sweep": 0.0}
    while True:
        try:
            processed = run_once(state)
        except Exception:
            # Database restarts, network blips and unexpected bugs must not end the
            # process; a job left running is recovered by the stale-job sweep.
            logger.exception("worker iteration failed")
            processed = False
            time.sleep(5)
        time.sleep(0.5 if processed else 2)
