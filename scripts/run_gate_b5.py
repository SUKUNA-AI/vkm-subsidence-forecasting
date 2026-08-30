#!/usr/bin/env python
"""CLI for Gate B5 protocol freezing, evidence analysis, and validation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from skru1.gate_b5 import (
    load_gate_b5_config,
    run_gate_b5_analysis,
    run_gate_b5_freeze,
    run_gate_b5_validation,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("freeze", "analyze", "validate", "all"), default="all")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root, config = load_gate_b5_config()
    results = []
    if args.phase in {"freeze", "all"}:
        results.append(run_gate_b5_freeze(root, config))
    if args.phase in {"analyze", "all"}:
        results.append(run_gate_b5_analysis(root, config))
    if args.phase in {"validate", "all"}:
        results.append(run_gate_b5_validation(root, config))
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
