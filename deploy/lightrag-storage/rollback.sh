#!/usr/bin/env bash
# Point LightRAG back at the file-backed stores that cutover.sh left untouched in the lightrag volume.
# Documents ingested after the cutover are not in those files; process them again afterwards.
set -euo pipefail
kept=$(mktemp)
trap 'rm -f "$kept"' EXIT
grep -v -E '^LIGHTRAG_(KV|VECTOR|DOC_STATUS|GRAPH)_STORAGE=' .env > "$kept" || true
# Rewrite in place so .env keeps its owner and its 600 mode.
cat "$kept" > .env
printf '%s\n' LIGHTRAG_KV_STORAGE=JsonKVStorage LIGHTRAG_VECTOR_STORAGE=NanoVectorDBStorage \
  LIGHTRAG_DOC_STATUS_STORAGE=JsonDocStatusStorage LIGHTRAG_GRAPH_STORAGE=NetworkXStorage >> .env
docker compose up -d lightrag
