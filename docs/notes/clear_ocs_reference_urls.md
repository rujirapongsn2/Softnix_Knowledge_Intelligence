# Clear OCS reference URLs

SKI citations must not emit `searchlaw.ocs.go.th` / `ocs.go.th` / `council-of-state` links.

## Registry cleanup

The SQL script backs up matching `legal_instruments.source_uri` values and then
sets those registry URLs to `NULL`. It does not rewrite original extracted text,
chunks, embeddings, cached answers, feedback, or audit logs.

```bash
docker compose -f docker-compose.yml -f docker-compose.production.yml exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U skip -d skip < apps/api/scripts/clear_ocs_reference_urls.sql
```

Backups are stored in `ocs_reference_url_backup`. To restore a row deliberately:

```sql
UPDATE legal_instruments AS target
SET source_uri = backup.source_uri,
    updated_at = backup.original_updated_at
FROM ocs_reference_url_backup AS backup
WHERE backup.entity_type = 'legal_instrument'
  AND backup.entity_id = target.id::text;
```

## Citation code

`_citation_reference_url` prefers SKI `download_url` for any ingested file (PDF or text).
`sanitize_source_reference_urls` also removes blocked URLs from structured `sources`
and nested provenance at write and read boundaries, including results cached before
this policy was deployed.
