#!/usr/bin/env bash
# Install or upgrade Softnix Knowledge Intelligence from Harbor images. Run from the release directory, which holds
# docker-compose.release.yml, .env.release.example and deploy/:
#     bash deploy/install.sh                 # first install: makes .env, logs in, pulls, starts, checks
#     bash deploy/install.sh --version v1.2.0  # upgrade: sets SKI_VERSION, pulls, migrates, restarts
# Options: --offline (images already loaded by deploy/offline-bundle.sh load; no login or pull)
#          --no-login (the registry is already logged in, or allows anonymous pull)
# Login uses HARBOR_USERNAME / HARBOR_PASSWORD from the environment when set, otherwise it prompts.
# Secrets are written to .env (mode 600) and never printed, except the first admin password, shown once.
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION="" OFFLINE=0 LOGIN=1
while [ $# -gt 0 ]; do
  case "$1" in
    --version) VERSION=${2:?--version needs a value}; shift ;;
    --offline) OFFLINE=1; LOGIN=0 ;;
    --no-login) LOGIN=0 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
  shift
done

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1" >&2; exit 1; }; }
need docker; need openssl
docker compose version >/dev/null 2>&1 || { echo "needs Docker Compose v2 (docker compose)" >&2; exit 1; }
[ -f docker-compose.release.yml ] || { echo "run from the release directory (docker-compose.release.yml not found)" >&2; exit 1; }

env_value() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'; }
set_value() {  # set_value KEY VALUE: replace or append, without echoing the value
  local key=$1 value=$2 tmp
  tmp=$(mktemp)
  if grep -qE "^$key=" .env; then
    awk -v k="$key" -v v="$value" 'BEGIN{FS=OFS="="} $1==k{print k"="v; next} {print}' .env > "$tmp"
  else
    cat .env > "$tmp"; printf '%s=%s\n' "$key" "$value" >> "$tmp"
  fi
  cat "$tmp" > .env; rm -f "$tmp"
}
secret() { openssl rand -hex "${1:-32}"; }

first_install=0
if [ ! -f .env ]; then
  first_install=1
  [ -f .env.release.example ] || { echo ".env.release.example not found" >&2; exit 1; }
  (umask 077; cp .env.release.example .env)
  echo "created .env from .env.release.example"
fi
chmod 600 .env

admin_password=""
for key in APP_SECRET_KEY TOKEN_HASH_SECRET POSTGRES_PASSWORD NEO4J_PASSWORD LIGHTRAG_API_KEY INITIAL_ADMIN_PASSWORD; do
  current=$(env_value "$key")
  if [ -z "$current" ] || [ "$current" = "<generated>" ]; then
    if [ "$key" = INITIAL_ADMIN_PASSWORD ]; then admin_password=$(secret 12); set_value "$key" "$admin_password"
    else set_value "$key" "$(secret 32)"; fi
    echo "generated $key"
  fi
done
[ -n "$VERSION" ] && set_value SKI_VERSION "$VERSION"
grep -qE '^COMPOSE_FILE=' .env || set_value COMPOSE_FILE docker-compose.release.yml
export COMPOSE_FILE=$(env_value COMPOSE_FILE)

if [ "$first_install" = 1 ]; then
  echo
  echo "Edit .env now: LLM_*, EMBEDDING_*, RERANK_* (optional), LLM_APP_URL, COOKIE_SECURE, SOFTNIX_OCR_* (optional)."
  if [ -t 0 ]; then read -r -p "Press Enter when .env is ready (Ctrl-C to stop and edit later) " _; else
    echo "Run deploy/install.sh again after editing .env."; exit 0; fi
fi
[ -n "$(env_value LLM_API_KEY)" ] || echo "warn: LLM_API_KEY is empty, so the LLM is off (use 'none' for a server without auth)"
[ -n "$(env_value EMBEDDING_BASE_URL)" ] || echo "warn: EMBEDDING_BASE_URL is empty, so vector search is off"

registry=$(env_value SKI_REGISTRY); registry=${registry:-registry.softnix.co.th/ski}
host=${registry%%/*}
if [ "$LOGIN" = 1 ]; then
  echo "logging in to $host"
  if [ -n "${HARBOR_USERNAME:-}" ] && [ -n "${HARBOR_PASSWORD:-}" ]; then
    printf '%s' "$HARBOR_PASSWORD" | docker login "$host" -u "$HARBOR_USERNAME" --password-stdin
  else
    read -r -p "Harbor username (robot account, e.g. robot\$ski+customer-pull): " user
    read -r -s -p "Harbor password/token: " pass; echo
    printf '%s' "$pass" | docker login "$host" -u "$user" --password-stdin
    unset pass
  fi
fi

docker compose config -q
if [ "$OFFLINE" = 0 ]; then docker compose pull; fi
# migrate runs before api through depends_on; --remove-orphans drops services an older release had.
docker compose up -d --remove-orphans

echo "waiting for api to become healthy"
for _ in $(seq 60); do
  state=$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q api | head -1)" 2>/dev/null || true)
  [ "$state" = healthy ] && break
  sleep 5
done
bash deploy/check_services.sh || { echo "some checks failed: docker compose logs api migrate lightrag"; exit 1; }

if [ -n "$admin_password" ]; then
  echo
  echo "first admin: $(env_value INITIAL_ADMIN_USERNAME) / $admin_password"
  echo "(shown once; also in .env as INITIAL_ADMIN_PASSWORD. Change it after the first login.)"
fi
