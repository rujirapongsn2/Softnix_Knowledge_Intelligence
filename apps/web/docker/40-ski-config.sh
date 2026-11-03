#!/bin/sh
# Runs from the nginx image's /docker-entrypoint.d before nginx starts. Writes /config.js from the container
# environment so an installer changes MAX_FILE_SIZE_MB in .env and restarts web, with no rebuild.
set -eu
size="${MAX_FILE_SIZE_MB:-}"
case "$size" in ''|*[!0-9]*) size="" ;; esac
if [ -n "$size" ]; then
  printf 'window.__SKI_CONFIG__ = {"maxFileSizeMb": %s};\n' "$size" > /usr/share/nginx/html/config.js
else
  printf 'window.__SKI_CONFIG__ = {};\n' > /usr/share/nginx/html/config.js
fi
