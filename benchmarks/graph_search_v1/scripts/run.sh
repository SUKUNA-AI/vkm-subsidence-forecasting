#!/bin/bash
# GRAPH_SEARCH_V1 on the WORKSTATION (WSL), one memory-capped job at a time, CPU only (no encoding of the corpus; the
# query vectors of P-E are cached per text under $GS_WORK/qvec, encoded on the CPU once):
#   a        stage A — G3 wordings with the served expand_query (NAV venv, pymorphy3) → stage_a.json
#   b        stage B — E and every grid variant of G1–G5 (lab venv) → runs.jsonl
#   select   dev choice and GC's composition → selection.json
#   gc       GC (when composed) → runs_gc.jsonl
#   pool     top-up pool and blind packets (judging by hand, then: pool-ingest)
#   ingest   judgments → benchmarks/graph_search_v1/qrels_gs_pooled.tsv
#   latency  the served stage code per query (+ lab BM25 legs, extra late targets) → latency.json
#   eval     test decision + description → benchmarks/graph_search_v1/results_v1.json
#   posthoc  analyses after the results were read (labelled) → benchmarks/graph_search_v1/results_v1_posthoc.json
# environment: GS_WORK, GS_NAV_ROOT, GS_CANON, J_V1, V2_WORK, VKM_MODELS_DIR; VENV_NAV (pymorphy3), VENV_LAB (numpy,
# scipy, duckdb; torch + sentence-transformers only when a query vector is not cached yet)
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../../.." && pwd)
export PYTHONPATH="$REPO/src" PYTHONIOENCODING=utf-8 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
mkdir -p "$GS_WORK"
CAP="systemd-run --user --scope -p MemoryMax=${GS_MEM:-24G} -p MemorySwapMax=0 --quiet"
case "${1:-}" in
  a)      $CAP "$VENV_NAV/bin/python" "$HERE/prepare.py" > "$GS_WORK/stage_a.out" 2> "$GS_WORK/stage_a.log" ;;
  b)      $CAP "$VENV_LAB/bin/python" "$HERE/run.py" variants 2> "$GS_WORK/stage_b.log" ;;
  select) $CAP "$VENV_LAB/bin/python" "$HERE/run.py" select > "$GS_WORK/select.out" 2> "$GS_WORK/select.log" ;;
  gc)     $CAP "$VENV_LAB/bin/python" "$HERE/run.py" combo 2> "$GS_WORK/stage_gc.log" ;;
  pool)   $CAP "$VENV_LAB/bin/python" "$HERE/pool.py" build ;;
  ingest) "$VENV_LAB/bin/python" "$HERE/pool.py" ingest ;;
  latency) $CAP "$VENV_LAB/bin/python" "$HERE/latency.py" > "$GS_WORK/latency.out" 2> "$GS_WORK/latency.log" ;;
  eval)   $CAP "$VENV_LAB/bin/python" "$HERE/run.py" evaluate > "$GS_WORK/eval.out" 2> "$GS_WORK/eval.log" ;;
  posthoc) $CAP "$VENV_LAB/bin/python" "$HERE/posthoc.py" > "$GS_WORK/posthoc.out" 2> "$GS_WORK/posthoc.log" ;;
  *) echo "usage: run.sh a|b|select|gc|pool|ingest|latency|eval|posthoc"; exit 2 ;;
esac
