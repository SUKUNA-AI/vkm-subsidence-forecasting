#!/usr/bin/env python3
"""Run Gate B4 entirely inside the frozen T1 train manifest."""

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


from skru1.gate_b4 import (
    load_gate_b4_config,
    run_gate_b4_development,
    run_gate_b4_validation,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Student-t robust IMM selected only by nested rolling/profile/zone "
            "resampling inside t1_v1/train. Validation and test have no phase."
        )
    )
    parser.add_argument(
        "--phase",
        choices=("develop", "validate", "all"),
        default="all",
    )
    parser.add_argument("--root", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root, config = load_gate_b4_config(args.root)
    results: list[dict[str, Any]] = []
    if args.phase in {"develop", "all"}:
        results.append(run_gate_b4_development(root, config))
    if args.phase in {"validate", "all"}:
        results.append(run_gate_b4_validation(root, config))
    print(json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
