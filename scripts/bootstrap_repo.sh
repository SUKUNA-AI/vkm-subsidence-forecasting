#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
python scripts/verify_inputs.py --root .
echo "Input verification passed. Open Codex in: $ROOT"
