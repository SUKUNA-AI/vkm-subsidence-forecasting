#!/usr/bin/env python
"""Run the frozen Gate C1 compact sequence temporal screen."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from skru1.gate_c1 import (
    load_gate_c1_config,
    run_gate_c1_aggregate,
    run_gate_c1_freeze,
    run_gate_c1_preflight,
    run_gate_c1_screen,
    run_gate_c1_validation,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase",
        choices=("freeze", "preflight", "screen", "aggregate", "validate", "all"),
        default="all",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root, config = load_gate_c1_config()
    phases = (
        ("freeze", run_gate_c1_freeze),
        ("preflight", run_gate_c1_preflight),
        ("screen", run_gate_c1_screen),
        ("aggregate", run_gate_c1_aggregate),
        ("validate", run_gate_c1_validation),
    )
    results = []
    for name, function in phases:
        if args.phase in {name, "all"}:
            result = function(root, config)
            results.append(result)
            print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    terminal = results[-1]["status"] if results else "NO_PHASE"
    return 0 if not str(terminal).startswith("FAIL") else 2


if __name__ == "__main__":
    raise SystemExit(main())
