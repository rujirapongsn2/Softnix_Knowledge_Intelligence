import time

from .db import SessionLocal
from .retention import prune_observability
from .services import process_next_graph_projection, process_next_job

if __name__ == "__main__":
    last_retention = 0.0
    while True:
        with SessionLocal() as db:
            processed_document = process_next_job(db)
            processed_graph = process_next_graph_projection(db)
            processed = processed_document or processed_graph
            now = time.monotonic()
            if now - last_retention >= 3600:
                prune_observability(db)
                last_retention = now
        time.sleep(0.5 if processed else 2)
