-- Remove blocked OCS-family registry URLs without rewriting document content,
-- chunks, embeddings, cached answers, or feedback.
--
-- The original values are copied to ocs_reference_url_backup before update.
-- Run with psql -v ON_ERROR_STOP=1 so any failure rolls back the transaction.

BEGIN;

CREATE TABLE IF NOT EXISTS ocs_reference_url_backup (
    entity_type text NOT NULL,
    entity_id text NOT NULL,
    source_uri text NOT NULL,
    original_updated_at timestamp without time zone,
    backed_up_at timestamp without time zone NOT NULL DEFAULT NOW(),
    PRIMARY KEY (entity_type, entity_id)
);

INSERT INTO ocs_reference_url_backup (
    entity_type,
    entity_id,
    source_uri,
    original_updated_at
)
SELECT
    'legal_instrument',
    id::text,
    source_uri,
    updated_at
FROM legal_instruments
WHERE source_uri ILIKE '%ocs.go.th%'
   OR source_uri ILIKE '%searchlaw%'
   OR source_uri ILIKE '%council-of-state%'
ON CONFLICT (entity_type, entity_id) DO NOTHING;

SELECT 'BACKED_UP legal_instruments.source_uri' AS metric, count(*)::text AS n
FROM ocs_reference_url_backup
WHERE entity_type = 'legal_instrument';

UPDATE legal_instruments
SET source_uri = NULL,
    updated_at = NOW()
WHERE source_uri ILIKE '%ocs.go.th%'
   OR source_uri ILIKE '%searchlaw%'
   OR source_uri ILIKE '%council-of-state%';

SELECT 'REMAINING legal_instruments.source_uri OCS' AS metric, count(*)::text AS n
FROM legal_instruments
WHERE source_uri ILIKE '%ocs.go.th%'
   OR source_uri ILIKE '%searchlaw%'
   OR source_uri ILIKE '%council-of-state%';

COMMIT;
