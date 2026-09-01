#!/usr/bin/env python
"""CLI for Gate C0 sequence-protocol freeze, audit, and validation.

The runner intentionally exposes no data-manifest arguments.  Its only
model-facing source is the frozen ``t1_v1/train`` manifest from
``configs/gate_c.yaml``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from skru1.gate_c import (
    load_gate_c_config,
    run_gate_c_analysis,
    run_gate_c_freeze,
    run_gate_c_validation,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("freeze", "analyze", "validate", "all"), default="all")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root, config = load_gate_c_config()
    results = []
    if args.phase in {"freeze", "all"}:
        results.append(run_gate_c_freeze(root, config))
    if args.phase in {"analyze", "all"}:
        results.append(run_gate_c_analysis(root, config))
    if args.phase in {"validate", "all"}:
        results.append(run_gate_c_validation(root, config))
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
