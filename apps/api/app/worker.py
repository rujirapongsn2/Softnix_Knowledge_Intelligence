import time

from .db import SessionLocal
from .services import process_next_graph_projection, process_next_job

if __name__ == "__main__":
    while True:
        with SessionLocal() as db:
            processed_document = process_next_job(db)
            processed_graph = process_next_graph_projection(db)
            processed = processed_document or processed_graph
        time.sleep(0.5 if processed else 2)
