"""Guard for the optional PostgreSQL tests: they drop and recreate every table.

Run them against a throwaway database whose name ends in ``_test``, e.g.::

    make test-postgres
    # or by hand:
    docker run -d --rm --name ski-pgtest -e POSTGRES_DB=queue_test -e POSTGRES_USER=queue_test \\
        -e POSTGRES_PASSWORD=queue_test -p 127.0.0.1:55439:5432 pgvector/pgvector:pg16
    cd apps/api && TEST_POSTGRES_DATABASE_URL=postgresql+psycopg://queue_test:queue_test@127.0.0.1:55439/queue_test \\
        .venv/bin/pytest -q tests/test_job_queue_postgres.py tests/test_pgvector_integration.py
    docker stop ski-pgtest
"""
import os

import pytest
from sqlalchemy.engine import make_url


def postgres_test_url() -> str | None:
    """TEST_POSTGRES_DATABASE_URL, refused unless it names a ``*_test`` database."""
    url = os.getenv("TEST_POSTGRES_DATABASE_URL")
    if not url:
        return None
    name = make_url(url).database or ""
    if not name.endswith("_test"):
        pytest.fail(f"Refusing to run: these tests drop every table, and database {name!r} does not end in '_test'.", pytrace=False)
    return url
