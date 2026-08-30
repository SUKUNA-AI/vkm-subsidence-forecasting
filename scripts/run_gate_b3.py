#!/usr/bin/env python3
"""Run the predeclared Gate B3 IMM without any current-test phase."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


ROOT_HINT = Path(__file__).resolve().parents[1]
SRC = ROOT_HINT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


from skru1.gate_b3 import (
    load_gate_b3_config,
    run_gate_b3_development,
    run_gate_b3_validation,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Two-regime IMM with nested train-only tuning, frozen B1/B5/B6 "
            "comparators, transition validation, and no executable test phase."
        )
    )
    parser.add_argument(
        "--phase",
        choices=("develop", "validate", "all"),
        default="all",
        help="develop writes artifacts; validate independently checks them",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="repository root (auto-discovered when omitted)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root, config = load_gate_b3_config(args.root)
    results: list[dict[str, Any]] = []
    if args.phase in {"develop", "all"}:
        results.append(run_gate_b3_development(root, config))
    if args.phase in {"validate", "all"}:
        results.append(run_gate_b3_validation(root, config))
    print(json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
