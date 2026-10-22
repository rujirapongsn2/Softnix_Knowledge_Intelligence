#!/usr/bin/env bash
# Check that every service an installation needs is up and answering. Run from the repository root:
#     bash deploy/check_services.sh
# Exits non-zero when a required check fails. Keys and passwords are never printed.
set -u
cd "$(dirname "$0")/.."

env_value() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'; }
API="http://127.0.0.1:$(env_value API_PORT || true)"; [ "$API" = "http://127.0.0.1:" ] && API=http://127.0.0.1:8001
WEB="http://127.0.0.1:$(env_value WEB_PORT || true)"; [ "$WEB" = "http://127.0.0.1:" ] && WEB=http://127.0.0.1:8081
failed=0
ok() { printf '  ok    %s\n' "$1"; }
bad() { printf '  FAIL  %s\n' "$1"; failed=1; }

echo "containers"
states=$(docker compose ps -a --format '{{.Service}} {{.State}} {{.ExitCode}}' 2>/dev/null)
for service in api worker web postgres redis neo4j lightrag; do
  running=$(printf '%s\n' "$states" | awk -v s="$service" '$1 == s && $2 == "running"' | wc -l | tr -d ' ')
  [ "$running" -gt 0 ] && ok "$service running ($running)" || bad "$service is not running"
done
printf '%s\n' "$states" | awk '$1 == "migrate"' | grep -q ' exited 0$' && ok "migrate finished" || bad "migrate did not finish with exit code 0 (docker compose logs migrate)"

echo "http"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$API/health")" = 200 ] && ok "api $API/health" || bad "api does not answer at $API/health"
ready=$(curl -s "$API/ready")
if printf '%s' "$ready" | grep -q '"status":"ready"'; then
  ok "api /ready: $(printf '%s' "$ready" | python3 -c 'import json,sys; print(", ".join(f"{k} {v}" for k, v in json.load(sys.stdin)["dependencies"].items()))')"
else
  bad "api /ready: ${ready:-no answer}"
fi
[ "$(curl -s -o /dev/null -w '%{http_code}' "$WEB/")" = 200 ] && ok "web $WEB" || bad "web does not answer at $WEB"

echo "providers (checked from inside the api container)"
docker compose exec -T api python - <<'EOF' || failed=1
from app.embeddings import EmbeddingClient
from app.llm import LLMClient

failed = False
def report(good, text, required=True):
    global failed
    print(f"  {'ok  ' if good else ('FAIL' if required else 'warn')}  {text}")
    failed |= required and not good

try:
    llm = LLMClient().check()
    report(llm["llm_model_available"], f"llm {llm['llm_model']} ({llm['llm_dialect']})"
           + ("" if llm["llm_model_available"] else ": the provider does not list this model"))
    if "llm_answer_model" in llm:
        report(llm["llm_answer_model_available"], f"llm answers {llm['llm_answer_model']}"
               + ("" if llm["llm_answer_model_available"] else ": the provider does not list this model"))
except Exception as exc:
    report(False, f"llm: {exc}")

embedding = EmbeddingClient().check()
report(embedding["embedding_model_available"], f"embeddings {embedding['embedding_model']} at {embedding['embedding_host']}"
       + ("" if embedding["embedding_model_available"] else f": {embedding.get('embedding_error')}"))

client = LLMClient()
credits = client.credits_remaining()
if credits is not None:
    warning = client.settings.llm_credit_warning_usd
    report(credits >= warning, f"llm credits ${credits:.2f} left" + ("" if credits >= warning else f", below ${warning:g}"), required=False)
if client.reranker_enabled:
    try:
        order = client.rerank("อายุความฟ้องคดี", ["ร้านกาแฟเปิดแปดโมง", "คดีมีอายุความสิบปี"], 2)
        report(bool(order) and order[0][0] == 1, f"reranker {client.settings.rerank_model or client.settings.rerank_api_style}")
    except Exception as exc:
        report(False, f"reranker: {exc}")
else:
    report(False, "reranker is off (RERANKER_ENABLED=false); search works without it, ranked less well", required=False)
raise SystemExit(1 if failed else 0)
EOF

echo
[ "$failed" = 0 ] && echo "all required checks passed" || echo "some required checks failed"
exit "$failed"
