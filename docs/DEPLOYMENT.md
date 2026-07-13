# Deployment

1. Create a protected `.env` from `.env.example`; set unique `APP_SECRET_KEY`, `TOKEN_HASH_SECRET`, and `INITIAL_ADMIN_PASSWORD`.
   Set `OPENROUTER_API_KEY` for the initial development path and keep it outside source control. The bundled LightRAG service uses OpenRouter's OpenAI-compatible endpoint for both extraction/query LLM calls and embeddings. `LIGHTRAG_API_KEY` is a separate internal service credential.
   `LIGHTRAG_IMAGE` is pinned to the verified LightRAG image digest in the local `.env`. Update it only through a reviewed compatibility test, then record the approved digest.
   If host port `8000` is already occupied, set `API_PORT=8001` (the default in this Compose file) and use `http://localhost:8001` for the API.
   `RETRIEVAL_PLANNER_TIMEOUT_SECONDS` bounds the OpenRouter fallback planner (default 4 seconds); rule-based planning remains available when the key or provider is unavailable.
2. Start with `docker compose -f docker-compose.yml -f docker-compose.production.yml up -d --build` behind a TLS reverse proxy or Cloudflare Tunnel. For a tunnel mapping `https://knowledge.softnix.ai` to `http://localhost:8081`, keep `WEB_PORT=8081`, set `OPENROUTER_APP_URL=https://knowledge.softnix.ai`, and use the production override so `COOKIE_SECURE=true`. The `migrate` service runs `alembic upgrade head`; API startup waits for it to complete successfully.
3. Ensure PostgreSQL, Redis, Neo4j and uploaded-file volumes are persistent. Do not publish their ports.
4. Verify `/health` and `/ready`; then sign in and rotate the initial password.

Run reviewed Alembic migrations before production API upgrades; schema bootstrap is only for a clean local installation.
# Upload size limit

Set `MAX_FILE_SIZE_MB` in `.env` to the maximum supported upload size. The same
value is applied by the web reverse proxy and API; after changing it, run
`docker compose up --build -d web api`.

## Reindex existing embeddings

After enabling pgvector embeddings, select a Knowledge Base in the Admin UI and
click **Reindex embeddings**. This queues only documents that do not have vectors;
it does not re-send completed documents to LightRAG.
