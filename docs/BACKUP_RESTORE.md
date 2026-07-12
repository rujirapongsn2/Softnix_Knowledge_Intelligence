# Backup and restore

Back up PostgreSQL, Neo4j, Redis if durable queues are enabled, the `files` volume, and the protected deployment configuration. Restore the database and graph data before bringing API/worker up, then restore files at the same paths. Validate with `/ready` and a sampled cited query.

