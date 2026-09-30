#!/bin/bash
# TERM_DICTIONARY_V1 on the WORKSTATION (WSL): stage A (NAV venv, pymorphy3) → stage B (retrieval lab venv, GPU for
# the query encoders, capped) → stage C (evaluation). Everything written outside git except results_v1.json.
#
# usage: run.sh <a|b|c|all|x>   (x: exploratory RRF-only variant, added after the preregistered results were read)
# environment (defaults for this workstation's layout are set by the caller):
#   TD_WORK       work dir (outside git)            TD_NAV_BUILD  NAV build dir of the snapshot
#   TD_DICT       term_translations.parquet          TD_CANON      canonical DuckDB of the snapshot
#   TD_COMMIT     PUBLIC commit of the catalogues    J_V1, V2_WORK, VKM_MODELS_DIR  (V1/V2 work dirs, models)
#   VENV_NAV      venv with the extra navigation     VENV_LAB      venv with torch + sentence-transformers
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../../.." && pwd)
export PYTHONPATH="$REPO/src" PYTHONIOENCODING=utf-8 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
STAGE=${1:-all}
mkdir -p "$TD_WORK"
if [[ $STAGE == a || $STAGE == all ]]; then
  "$VENV_NAV/bin/python" "$HERE/prepare.py" 2> "$TD_WORK/stage_a.log"
fi
if [[ $STAGE == b || $STAGE == all ]]; then
  TD_VRAM=${TD_VRAM:-0.25} "$VENV_LAB/bin/python" "$HERE/run_proxy.py" 2> "$TD_WORK/stage_b.log"
fi
if [[ $STAGE == c || $STAGE == all ]]; then
  "$VENV_LAB/bin/python" "$HERE/evaluate.py" > "$TD_WORK/stage_c.json" 2> "$TD_WORK/stage_c.log"
fi
if [[ $STAGE == x ]]; then
  TD_LATE=0 TD_VRAM=${TD_VRAM:-0.25} "$VENV_LAB/bin/python" "$HERE/run_proxy.py" 2> "$TD_WORK/stage_x.log"
  TD_VARIANT=nolate "$VENV_LAB/bin/python" "$HERE/evaluate.py" > "$TD_WORK/stage_x.json" 2>> "$TD_WORK/stage_x.log"
fi
tail -n 3 "$TD_WORK"/stage_*.log
