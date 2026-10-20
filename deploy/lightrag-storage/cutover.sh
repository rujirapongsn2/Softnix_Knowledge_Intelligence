#!/usr/bin/env bash
# Move LightRAG from its file-backed stores to Postgres + Neo4j and re-index every searchable document.
# Run from the repository root:  bash deploy/lightrag-storage/cutover.sh [BACKUP_DIR]
# LightRAG search is incomplete until the re-ingest finishes. Rollback: deploy/lightrag-storage/rollback.sh
set -euo pipefail

BACKUP=${1:-$HOME/softnix-backups/$(date +%F)-lightrag-storage}
HERE=deploy/lightrag-storage
mkdir -p "$BACKUP"
log() { printf '[%s] %s\n' "$(date +%T)" "$*" | tee -a "$BACKUP/cutover.log"; }
lightrag_config() {
  docker compose exec -T lightrag python -c "import json,os,urllib.request
r=urllib.request.Request('http://127.0.0.1:9621/health',headers={'X-API-Key':os.environ.get('LIGHTRAG_API_KEY','')})
c=json.load(urllib.request.urlopen(r))['configuration']
print(json.dumps({k:c[k] for k in ('kv_storage','vector_storage','doc_status_storage','graph_storage','max_async','max_parallel_insert')}))"
}

log "preflight"
docker compose config -q
curl -sf -m 5 http://127.0.0.1:8090/health >/dev/null || { log "embedding server is not answering on :8090"; exit 1; }
for name in LIGHTRAG_KV_STORAGE LIGHTRAG_VECTOR_STORAGE LIGHTRAG_DOC_STATUS_STORAGE LIGHTRAG_GRAPH_STORAGE; do
  if grep -q "^$name=" .env; then log ".env sets $name; remove it so the Postgres/Neo4j defaults apply"; exit 1; fi
done

log "backup to $BACKUP"
docker compose exec -T postgres pg_dump -U skip -d skip -Fc > "$BACKUP/postgres-skip.dump"
# The volume behind the running container's /app/data, whatever the compose project is called.
volume=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/app/data"}}{{.Name}}{{end}}{{end}}' "$(docker compose ps -q lightrag)")
[ -n "$volume" ] || { log "lightrag is not running with a volume on /app/data"; exit 1; }
docker run --rm -v "$volume":/data:ro -v "$BACKUP":/backup alpine tar czf /backup/lightrag-volume.tgz -C /data .
tar tzf "$BACKUP/lightrag-volume.tgz" | grep -q 'rag_storage/kv_store_doc_status.json' || { log "backup of $volume holds no LightRAG store"; exit 1; }
memory() { docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' $(docker compose ps -q lightrag postgres neo4j); }
memory | tee "$BACKUP/memory-before.txt"

log "stop lightrag and copy its LLM cache into Postgres"
docker compose stop lightrag
docker compose run --rm --no-deps -T -v "$PWD/$HERE:/migrate:ro" --entrypoint python lightrag \
  /migrate/copy_llm_cache.py /app/data/rag_storage/kv_store_llm_response_cache.json 2>&1 | grep -v -E 'INFO|does not exist' | tee -a "$BACKUP/cutover.log"

log "start lightrag on the new storage"
docker compose up -d lightrag
for _ in $(seq 60); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q lightrag)")" = healthy ] && break
  sleep 5
done
config=$(lightrag_config)
log "lightrag $config"
case "$config" in *'"PGKVStorage"'*'"PGVectorStorage"'*'"PGDocStatusStorage"'*'"Neo4JStorage"'*) ;; *) log "lightrag is not on the new storage"; exit 1;; esac

log "re-ingest searchable documents"
set +e
docker compose exec -T api python - --workers 2 < "$HERE/reingest.py" | tee "$BACKUP/reingest.jsonl"
status=${PIPESTATUS[0]}
set -e

memory | tee "$BACKUP/memory-after.txt"
docker compose exec -T postgres psql -U skip -d skip -Atc \
  "select relname, pg_size_pretty(pg_total_relation_size(oid)) from pg_class where relname like 'lightrag%' and relkind='r' order by pg_total_relation_size(oid) desc" | tee "$BACKUP/postgres-sizes.txt"
docker compose exec -T neo4j sh -c 'cypher-shell -u neo4j -p "${NEO4J_AUTH#neo4j/}" "MATCH (n:lightrag) RETURN count(n) AS lightrag_nodes"' | tee -a "$BACKUP/postgres-sizes.txt"
log "re-ingest exit status $status (details in $BACKUP/reingest.jsonl)"
exit "$status"
