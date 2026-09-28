#!/usr/bin/env bash
# Build the two VKM rerank images and write a build receipt (decision CP-18 (в); H-10: images by id/digest).
#   usage: build_images.sh <public_repo_root> <receipt_dir>
# The repo root may be a checkout or a copy holding infra/edge and src/vkm_corpus. Base images are pinned by digest
# in the Dockerfiles; llama.cpp by full commit. Nothing here touches other containers or images.
# EDGE is a laptop: the llama.cpp compile runs with BUILD_JOBS (default 6) jobs under `nice -n 10`, the docker client
# itself under `nice` too; rebuild only when the recipe changes (cached layers). Preferred for heavy compiles: build on
# WORKSTATION (WSL + Docker) and move the image: `docker save <tag> | ssh edge docker load`.
set -euo pipefail
REPO=${1:?public repo root}
OUT=${2:?receipt dir}
LLAMA_TAG=${LLAMA_TAG:-vkm/llama-server-cu132-sm75:b11223-4da6337767f9-p1}
GATEWAY_TAG=${GATEWAY_TAG:-vkm/rerank-gateway:0.1.1}
BUILD_JOBS=${BUILD_JOBS:-6}
mkdir -p "$OUT"

nice -n 10 docker build --progress=plain --build-arg BUILD_JOBS="$BUILD_JOBS" -t "$LLAMA_TAG" "$REPO/infra/edge/llama-server"

CTX=$(mktemp -d)
trap 'rm -rf "$CTX"' EXIT
cp "$REPO/infra/edge/gateway/Dockerfile" "$REPO/infra/edge/gateway/requirements.txt" "$CTX/"
mkdir -p "$CTX/src/vkm_corpus"
for f in __init__.py versions.py config.py logs.py cli.py; do cp "$REPO/src/vkm_corpus/$f" "$CTX/src/vkm_corpus/"; done
cp -r "$REPO/src/vkm_corpus/retrieval" "$CTX/src/vkm_corpus/"
find "$CTX" -name "__pycache__" -prune -exec rm -rf {} +
nice -n 10 docker build --progress=plain -t "$GATEWAY_TAG" "$CTX"

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
{
  echo "{"
  echo "  \"receipt\": \"vkm-rerank-build/1\", \"created_utc\": \"$STAMP\","
  echo "  \"llama_image\": {\"tag\": \"$LLAMA_TAG\", \"id\": \"$(docker image inspect -f '{{.Id}}' "$LLAMA_TAG")\","
  echo "    \"commit\": \"$(docker run --rm --entrypoint cat "$LLAMA_TAG" /app/LLAMA_CPP_COMMIT)\"},"
  echo "  \"gateway_image\": {\"tag\": \"$GATEWAY_TAG\", \"id\": \"$(docker image inspect -f '{{.Id}}' "$GATEWAY_TAG")\"},"
  echo "  \"dockerfile_sha256\": {\"llama\": \"$(sha256sum "$REPO/infra/edge/llama-server/Dockerfile" | cut -d' ' -f1)\","
  echo "    \"gateway\": \"$(sha256sum "$REPO/infra/edge/gateway/Dockerfile" | cut -d' ' -f1)\"}"
  echo "}"
} > "$OUT/build_$STAMP.json"
docker run --rm --entrypoint cat "$LLAMA_TAG" /app/SHA256SUMS > "$OUT/build_${STAMP}_llama_SHA256SUMS.txt"
docker run --rm --entrypoint cat "$LLAMA_TAG" /app/PATCHES_SHA256 > "$OUT/build_${STAMP}_llama_PATCHES_SHA256.txt"
docker run --rm --entrypoint cat "$GATEWAY_TAG" /opt/vkm/pip-freeze.txt > "$OUT/build_${STAMP}_gateway_pip_freeze.txt"
docker run --rm --entrypoint cat "$GATEWAY_TAG" /opt/vkm/SOURCE_SHA256SUMS > "$OUT/build_${STAMP}_gateway_sources.txt"
echo "receipt: $OUT/build_$STAMP.json"
