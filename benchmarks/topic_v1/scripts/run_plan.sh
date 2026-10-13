#!/bin/bash
# Search experiments of 13.10.2026 (PREREGISTRATION_EXPERIMENTS_2026-10-13.md): run harness_plan.py inside the VKM API
# container (read-only API calls, concurrency 1; the token stays in the container). The plan (requests with query
# texts) is embedded into the piped program; raw answers (IDs only) go to a host-local, git-ignored file.
#
# usage: run_plan.sh <api-container> <plan.jsonl> <out.jsonl> [harness args...]
#   run_plan.sh vkm-core-api-1 work/bench_20261013/plans/e6e7_topic.jsonl work/bench_20261013/runs/e6e7_topic.jsonl
# The container runs on the ssh host VKM_BENCH_SSH (default: core); VKM_BENCH_SSH= (empty) runs docker locally.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
[ $# -ge 3 ] || { echo "usage: $0 <api-container> <plan.jsonl> <out.jsonl> [harness args...]" >&2; exit 64; }
CONTAINER="$1"; PLAN="$2"; OUT="$3"; shift 3
[[ "$CONTAINER" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || { echo "bad container name: $CONTAINER" >&2; exit 64; }
if grep -q '"""' "$PLAN"; then echo "plan contains a triple quote" >&2; exit 65; fi
mkdir -p "$(dirname "$OUT")"
BUNDLE=$(mktemp)
trap 'rm -f "$BUNDLE"' EXIT
{ printf 'PLAN_JSONL = r"""\n'; cat "$PLAN"; printf '"""\n'; cat "$HERE/harness_plan.py"; } > "$BUNDLE"
SSH_HOST="${VKM_BENCH_SSH-core}"
if [ -n "$SSH_HOST" ]; then
  ARGS=""
  for a in "$@"; do ARGS="$ARGS $(printf '%q' "$a")"; done
  ssh -o BatchMode=yes "$SSH_HOST" "docker exec -i $CONTAINER /opt/vkm/bin/python -$ARGS" < "$BUNDLE" > "$OUT"
else
  docker exec -i "$CONTAINER" /opt/vkm/bin/python - "$@" < "$BUNDLE" > "$OUT"
fi
echo "done: $(wc -l < "$OUT") lines, sha256 $(sha256sum "$OUT" | cut -c1-16)"
