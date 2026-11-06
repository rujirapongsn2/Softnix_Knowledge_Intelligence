#!/usr/bin/env bash
# Offline (air-gapped) installs. On a machine that can reach Harbor:
#     bash deploy/offline-bundle.sh save v1.0.0 [linux/amd64]   # writes dist/ski-v1.0.0-linux-amd64.tar.gz
# Copy that file to the customer's server, unpack it, then in the unpacked directory:
#     bash deploy/offline-bundle.sh load                        # docker load of every image
#     bash deploy/install.sh --offline                          # makes .env, starts, checks
# The bundle holds the images for one platform plus docker-compose.release.yml, .env.release.example, deploy/
# and docs/INSTALL_HARBOR.md. It holds no secrets. The embedding, reranker and LLM servers (and their model
# files) are installed separately and are not in the bundle.
set -euo pipefail
cd "$(dirname "$0")/.."

images_for() {  # the image references docker-compose.release.yml resolves to for SKI_VERSION=$1
  SKI_VERSION=$1 POSTGRES_PASSWORD=x NEO4J_PASSWORD=x LIGHTRAG_API_KEY=x \
    docker compose -f docker-compose.release.yml --env-file /dev/null config --images | sort -u
}

case "${1:-}" in
  save)
    version=${2:?usage: offline-bundle.sh save <version> [platform]}
    platform=${3:-linux/amd64}
    name="ski-$version-${platform//\//-}"
    work="dist/$name"
    rm -rf "$work"; mkdir -p "$work/images" "$work/deploy" "$work/docs"
    images_for "$version" > "$work/images/list.txt"
    while read -r image; do
      echo "pull $image ($platform)"
      docker pull --platform "$platform" "$image"
    done < "$work/images/list.txt"
    echo "save $(wc -l < "$work/images/list.txt" | tr -d ' ') images"
    # shellcheck disable=SC2046
    # Docker 25+ saves one platform of a multi-platform image; older Docker has no --platform and saves the pulled one.
    if docker save --help 2>/dev/null | grep -q -- '--platform'; then
      docker save --platform "$platform" $(cat "$work/images/list.txt") | gzip -1 > "$work/images/images.tar.gz"
    else
      docker save $(cat "$work/images/list.txt") | gzip -1 > "$work/images/images.tar.gz"
    fi
    cp docker-compose.release.yml .env.release.example "$work/"
    cp deploy/install.sh deploy/offline-bundle.sh deploy/check_services.sh deploy/backup.sh deploy/restore.sh "$work/deploy/"
    cp -R deploy/lightrag-patches "$work/deploy/"
    cp docs/INSTALL_HARBOR.md "$work/docs/"
    sed -i.bak "s/^SKI_VERSION=.*/SKI_VERSION=$version/" "$work/.env.release.example" && rm -f "$work/.env.release.example.bak"
    (cd "$work/images" && shasum -a 256 images.tar.gz > images.tar.gz.sha256 2>/dev/null || sha256sum images.tar.gz > images.tar.gz.sha256)
    tar -C dist -czf "dist/$name.tar.gz" "$name"
    rm -rf "$work"
    echo "wrote dist/$name.tar.gz"
    ;;
  load)
    [ -f images/images.tar.gz ] || { echo "run from the unpacked bundle directory (images/images.tar.gz not found)" >&2; exit 1; }
    if command -v sha256sum >/dev/null; then (cd images && sha256sum -c images.tar.gz.sha256)
    else (cd images && shasum -a 256 -c images.tar.gz.sha256); fi
    gunzip -c images/images.tar.gz | docker load
    echo "loaded. Next: bash deploy/install.sh --offline"
    ;;
  *) sed -n '2,10p' "$0"; exit 2 ;;
esac
