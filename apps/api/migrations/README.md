# Database migrations

The schema models live in `app/models.py`. The baseline production migration is `0001_initial_schema`. Generate and review an explicit Alembic migration before each production schema change.

Run `alembic upgrade head` before starting a production API. Runtime schema creation is limited to `APP_ENV=development` or `test`.
