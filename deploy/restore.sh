#!/usr/bin/env bash
# Restore a backup written by deploy/backup.sh. Run from the repository root of a checkout that can build the
# stack (README, install steps 1-2):
#     bash deploy/restore.sh BACKUP_DIR [--no-worker] [--force]
# The compose project is the usual one unless COMPOSE_PROJECT_NAME says otherwise, so a rehearsal can restore
# beside a live installation without touching it:
#     COMPOSE_PROJECT_NAME=skrestore API_PORT=8101 WEB_PORT=8181 bash deploy/restore.sh BACKUP_DIR --no-worker
# A project that already holds documents is refused unless --force, which replaces its data. Without a .env the
# backup's is used; a .env with different APP_SECRET_KEY or TOKEN_HASH_SECRET is refused, since every session
# and API token would stop working. --no-worker leaves the queue unprocessed, which a rehearsal wants: queued
# jobs would otherwise run and spend LLM credits.
#     bash deploy/restore.sh --counts      prints the counts backup.sh records, for the running project
set -euo pipefail
cd "$(dirname "$0")/.."

sql() { docker compose exec -T postgres psql -U skip -d skip -At -c "$1"; }
counts() {
  local neo4j
  neo4j=$(docker compose exec -T neo4j sh -c 'cypher-shell -u neo4j -p "${NEO4J_AUTH#neo4j/}" --format plain "MATCH (n) RETURN count(n)"' | tail -1)
  printf '{"documents": %s, "chunks": %s, "lightrag_processed": %s, "neo4j_nodes": %s, "files": %s}\n' \
    "$(sql "select count(*) from documents where deleted_at is null")" \
    "$(sql "select count(*) from document_chunks")" \
    "$(sql "select count(*) from lightrag_doc_status where status = 'processed'")" \
    "$neo4j" \
    "$(docker compose exec -T api sh -c 'find /data/files -type f | wc -l' | tr -d ' ')"
}
if [ "${1:-}" = --counts ]; then counts; exit 0; fi

BACKUP="" NO_WORKER=0 FORCE=0
for arg in "$@"; do
  case "$arg" in --no-worker) NO_WORKER=1 ;; --force) FORCE=1 ;; -*) echo "unknown option $arg" >&2; exit 2 ;; *) BACKUP=$arg ;; esac
done
[ -n "$BACKUP" ] && [ -f "$BACKUP/manifest.json" ] || { echo "usage: bash deploy/restore.sh BACKUP_DIR [--no-worker] [--force]" >&2; exit 2; }
PROJECT=$(docker compose config --format json | python3 -c 'import json, sys; print(json.load(sys.stdin)["name"])')
log() { printf '[%s] %s: %s\n' "$(date +%T)" "$PROJECT" "$*"; }
quiet() { local output; output=$("$@" 2>&1) || { printf '%s\n' "$output" | tail -20; return 1; }; }
env_value() { grep -E "^$1=" "$2" | tail -1 | cut -d= -f2-; }

log "verify $BACKUP"
python3 - "$BACKUP" <<'EOF'
import hashlib, json, pathlib, sys
backup = pathlib.Path(sys.argv[1])
for name, entry in json.loads((backup / "manifest.json").read_text())["files"].items():
    digest = hashlib.sha256()
    with open(backup / name, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    if digest.hexdigest() != entry["sha256"]:
        sys.exit(f"{name} does not match its checksum in manifest.json")
EOF
if [ ! -f .env ]; then
  cp "$BACKUP/env" .env && chmod 600 .env && log "using the backup's .env"
else
  for name in APP_SECRET_KEY TOKEN_HASH_SECRET; do
    [ "$(env_value $name .env)" = "$(env_value $name "$BACKUP/env")" ] \
      || { log ".env has a different $name from the backup; sessions and API tokens would stop working"; exit 1; }
  done
fi

log "postgres"
docker compose stop worker api lightrag neo4j >/dev/null 2>&1 || true
quiet docker compose up -d postgres
for _ in $(seq 60); do docker compose exec -T postgres pg_isready -U skip -d skip >/dev/null 2>&1 && break; sleep 2; done
existing=$(sql "select count(*) from information_schema.tables where table_name = 'documents'")
if [ "$existing" != 0 ] && [ "$(sql "select count(*) from documents")" != 0 ] && [ "$FORCE" = 0 ]; then
  log "this project already holds documents; pass --force to replace them"; exit 1
fi
sql "set client_min_messages = warning; drop schema public cascade; create schema public;" >/dev/null
docker compose exec -T postgres pg_restore -U skip -d skip --no-owner --exit-on-error < "$BACKUP/postgres.dump"

log "build, then neo4j and files"
quiet docker compose build
quiet docker compose create neo4j api
volume_of() {
  docker inspect -f "{{range .Mounts}}{{if eq .Destination \"$2\"}}{{.Name}}{{end}}{{end}}" "$(docker compose ps -aq "$1" | head -1)"
}
for pair in "neo4j /data neo4j.tgz" "api /data/files files.tgz"; do
  set -- $pair
  volume=$(volume_of "$1" "$2")
  [ -n "$volume" ] || { log "no volume on $1:$2"; exit 1; }
  docker run --rm -v "$volume":/data -v "$(cd "$BACKUP" && pwd)":/backup:ro alpine \
    sh -c "find /data -mindepth 1 -delete && tar xzf /backup/$3 -C /data"
done

log "start"
if [ "$NO_WORKER" = 1 ]; then quiet docker compose up -d --scale worker=0
else quiet docker compose up -d; fi
api_port=${API_PORT:-$(env_value API_PORT .env)}
for _ in $(seq 90); do curl -sf "http://127.0.0.1:${api_port:-8001}/ready" >/dev/null && break; sleep 5; done
curl -s "http://127.0.0.1:${api_port:-8001}/ready"; echo

log "compare with the backup"
counts | python3 -c '
import json, sys
expected = json.load(open(sys.argv[1]))["counts"]
actual = json.loads(sys.stdin.read())
for name, value in expected.items():
    mark = "ok  " if actual.get(name) == value else "FAIL"
    print(f"  {mark}  {name}: backup {value}, restored {actual.get(name)}")
sys.exit(0 if actual == expected else 1)' "$BACKUP/manifest.json"
log "restored"
