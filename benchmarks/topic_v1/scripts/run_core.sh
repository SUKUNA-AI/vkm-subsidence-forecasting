#!/bin/bash
# TOPIC_BENCHMARK_V1: run the collector inside the VKM API container on CORE (read-only API calls; the token stays in
# the container). The frozen set is embedded into the piped program; raw answers (IDs only) go to a host-local file.
#
# usage: run_core.sh <out.jsonl> [harness args...]
#   run_core.sh "$VKM_WORK/topic_v1/runs_v1.jsonl" --outlines all              # bm25, hybrid_late, hybrid_nolate, nav
#   run_core.sh "$VKM_WORK/topic_v1/dossier_v1.jsonl" --systems dossier --outlines none \
#       --dossier-route /v1/nav/topic --dossier-method GET --dossier-param q      # after reconstruct_topic is deployed
# post-hoc diagnostics: --systems hybrid_late_pool --pool-sources <ids>; --systems hybrid_late_kinds,hybrid_late_drill
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
BENCH=$(dirname "$HERE")
OUT="$1"; shift
mkdir -p "$(dirname "$OUT")"
( cd "$BENCH" && sha256sum -c SHA256SUMS --quiet ) || { echo "frozen files changed: see SHA256SUMS" >&2; exit 2; }
BUNDLE=$(mktemp)
trap 'rm -f "$BUNDLE"' EXIT
{ printf 'SET_JSONL = r"""\n'; cat "$BENCH/topic_set_v1.jsonl"; printf '"""\n'; cat "$HERE/harness_core.py"; } > "$BUNDLE"
ARGS=""
for a in "$@"; do ARGS="$ARGS $(printf '%q' "$a")"; done
ssh -o BatchMode=yes core "docker exec -i vkm-core-api-1 /opt/vkm/bin/python -$ARGS" < "$BUNDLE" > "$OUT"
echo "done: $(wc -l < "$OUT") lines, sha256 $(sha256sum "$OUT" | cut -c1-16)"
