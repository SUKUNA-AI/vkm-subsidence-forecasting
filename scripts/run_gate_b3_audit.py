#!/usr/bin/env python3
"""Run the authoritative post-run Gate B3 artifact audit."""

from __future__ import annotations

import json
from pathlib import Path
import sys


ROOT_HINT = Path(__file__).resolve().parents[1]
SRC = ROOT_HINT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


from skru1.gate_b3 import load_gate_b3_config
from skru1.gate_b3_audit import run_gate_b3_authoritative_audit


def main() -> int:
    root, config = load_gate_b3_config(ROOT_HINT)
    result = run_gate_b3_authoritative_audit(root, config)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
