#!/usr/bin/env bash
# Point LightRAG back at the file-backed stores that cutover.sh left untouched in the lightrag volume.
# Documents ingested after the cutover are not in those files; process them again afterwards.
set -euo pipefail
for pair in LIGHTRAG_KV_STORAGE=JsonKVStorage LIGHTRAG_VECTOR_STORAGE=NanoVectorDBStorage \
            LIGHTRAG_DOC_STATUS_STORAGE=JsonDocStatusStorage LIGHTRAG_GRAPH_STORAGE=NetworkXStorage; do
  name=${pair%%=*}
  grep -v "^$name=" .env > .env.rollback && mv .env.rollback .env
  echo "$pair" >> .env
done
docker compose up -d lightrag
