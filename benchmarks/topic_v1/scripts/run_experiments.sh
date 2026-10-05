#!/bin/bash
# TOPIC_BENCHMARK_V1 experiments (post hoc, not pre-registered): run harness_experiments.py inside a VKM API container
# (read-only API calls; the token stays in the container). The frozen set is embedded into the piped program; raw
# answers (IDs only) go to a host-local file.
#
# usage: run_experiments.sh <api-container> <out.jsonl> [harness args...]
#   run_experiments.sh vkm-core-api-1 "$VKM_WORK/topic_v1/experiments_v1.jsonl"                     # every variant
#   run_experiments.sh vkm-core-api-1 "$VKM_WORK/topic_v1/exp_human3.jsonl" --variants baseline,human3 --outlines none
# The container runs on the ssh host VKM_BENCH_SSH (default: core); VKM_BENCH_SSH= (empty) runs docker locally.
# Score: PYTHONPATH=src python benchmarks/topic_v1/scripts/score_experiments.py --runs <out.jsonl>
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
BENCH=$(dirname "$HERE")
[ $# -ge 2 ] || { echo "usage: $0 <api-container> <out.jsonl> [harness args...]" >&2; exit 64; }
CONTAINER="$1"; OUT="$2"; shift 2
[[ "$CONTAINER" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || { echo "bad container name: $CONTAINER" >&2; exit 64; }
mkdir -p "$(dirname "$OUT")"
( cd "$BENCH" && sha256sum -c SHA256SUMS --quiet ) || { echo "frozen files changed: see SHA256SUMS" >&2; exit 2; }
BUNDLE=$(mktemp)
trap 'rm -f "$BUNDLE"' EXIT
{ printf 'SET_JSONL = r"""\n'; cat "$BENCH/topic_set_v1.jsonl"; printf '"""\n'; cat "$HERE/harness_experiments.py"; } > "$BUNDLE"
SSH_HOST="${VKM_BENCH_SSH-core}"
if [ -n "$SSH_HOST" ]; then
  ARGS=""
  for a in "$@"; do ARGS="$ARGS $(printf '%q' "$a")"; done
  ssh -o BatchMode=yes "$SSH_HOST" "docker exec -i $CONTAINER /opt/vkm/bin/python -$ARGS" < "$BUNDLE" > "$OUT"
else
  docker exec -i "$CONTAINER" /opt/vkm/bin/python - "$@" < "$BUNDLE" > "$OUT"
fi
echo "done: $(wc -l < "$OUT") lines, sha256 $(sha256sum "$OUT" | cut -c1-16)"
