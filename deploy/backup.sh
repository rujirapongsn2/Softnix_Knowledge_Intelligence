#!/usr/bin/env bash
# Back up everything an installation needs to be restored on another machine. Run from the repository root:
#     bash deploy/backup.sh [BACKUP_DIR] [--force]
# Writes postgres.dump (pg_dump: documents, chunks, vectors, the queue, LightRAG's tables), neo4j.tgz and files.tgz
# (the volumes, copied while their services are stopped), env (the .env secrets, mode 600) and manifest.json
# (counts and checksums that deploy/restore.sh checks). api, worker, lightrag and neo4j are stopped for the
# copy, a few minutes, and started again even when a step fails. --force backs up while jobs are running;
# the workers hand their jobs back to the queue when stopped, and the copy holds them as queued.
set -euo pipefail
cd "$(dirname "$0")/.."

OUT="" FORCE=0
for arg in "$@"; do
  case "$arg" in --force) FORCE=1 ;; -*) echo "unknown option $arg" >&2; exit 2 ;; *) OUT=$arg ;; esac
done
OUT=${OUT:-$HOME/softnix-backups/$(date +%F-%H%M)-full}
if [ -e "$OUT" ] && [ -n "$(ls -A "$OUT")" ]; then echo "$OUT is not empty" >&2; exit 1; fi
mkdir -p "$OUT" && chmod 700 "$OUT"
log() { printf '[%s] %s\n' "$(date +%T)" "$*" | tee -a "$OUT/backup.log"; }
sql() { docker compose exec -T postgres psql -U skip -d skip -At -c "$1"; }
volume_of() {  # the volume mounted at $2 in service $1's container, whatever the compose project is called
  docker inspect -f "{{range .Mounts}}{{if eq .Destination \"$2\"}}{{.Name}}{{end}}{{end}}" "$(docker compose ps -aq "$1" | head -1)"
}

log "preflight"
docker compose config -q
[ -f .env ] || { log ".env is missing"; exit 1; }
running=$(sql "select count(*) from processing_jobs where status = 'running'")
if [ "$running" != 0 ] && [ "$FORCE" = 0 ]; then
  log "$running job(s) are running; wait for them or pass --force"; exit 1
fi

log "counts"
bash deploy/restore.sh --counts > "$OUT/counts.json"
cat "$OUT/counts.json" | tee -a "$OUT/backup.log"
neo4j_volume=$(volume_of neo4j /data) files_volume=$(volume_of api /data/files)
[ -n "$neo4j_volume" ] && [ -n "$files_volume" ] || { log "could not find the neo4j and files volumes"; exit 1; }

restart() { log "start services"; docker compose start neo4j lightrag api worker >/dev/null; }
trap restart EXIT
log "stop writers"
docker compose stop worker api lightrag >/dev/null

log "postgres"
docker compose exec -T postgres pg_dump -U skip -d skip -Fc > "$OUT/postgres.dump"
docker compose exec -T postgres pg_restore --list < "$OUT/postgres.dump" | grep -q "TABLE DATA public documents" \
  || { log "postgres.dump has no documents table"; exit 1; }

log "neo4j ($neo4j_volume) and files ($files_volume)"
docker compose stop neo4j >/dev/null
docker run --rm -v "$neo4j_volume":/data:ro -v "$OUT":/backup alpine tar czf /backup/neo4j.tgz -C /data .
docker run --rm -v "$files_volume":/data:ro -v "$OUT":/backup alpine tar czf /backup/files.tgz -C /data .
for archive in neo4j files; do tar tzf "$OUT/$archive.tgz" >/dev/null || { log "$archive.tgz is unreadable"; exit 1; }; done

cp .env "$OUT/env" && chmod 600 "$OUT/env"
python3 - "$OUT" "$(git rev-parse HEAD)" <<'EOF'
import hashlib, json, pathlib, sys
out = pathlib.Path(sys.argv[1])
def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()
files = {name: {"sha256": sha256(out / name), "bytes": (out / name).stat().st_size}
         for name in ("postgres.dump", "neo4j.tgz", "files.tgz", "env")}
manifest = {"git_commit": sys.argv[2], "counts": json.loads((out / "counts.json").read_text()), "files": files}
(out / "manifest.json").write_text(json.dumps(manifest, indent=2))
print(json.dumps({name: f"{entry['bytes'] / 1e6:.0f} MB" for name, entry in files.items()}))
EOF
log "backup written to $OUT"
