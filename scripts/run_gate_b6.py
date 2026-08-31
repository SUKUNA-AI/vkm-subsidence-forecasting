#!/usr/bin/env python
"""Run Gate B6 aggregation phases without exposing validation/test manifests."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from skru1.gate_b6 import (
    dispatch_gate_b6_workers,
    load_gate_b6_config,
    run_gate_b6_calibration,
    run_gate_b6_freeze,
    run_gate_b6_preflight,
    run_gate_b6_robustness,
    run_gate_b6_screen,
    run_gate_b6_validation,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase",
        choices=("preflight", "screen", "robustness", "calibrate", "freeze", "validate", "all"),
        default="all",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root, config = load_gate_b6_config()
    phases = {
        "preflight": run_gate_b6_preflight,
        "screen": run_gate_b6_screen,
        "robustness": run_gate_b6_robustness,
        "calibrate": run_gate_b6_calibration,
        "freeze": run_gate_b6_freeze,
        "validate": run_gate_b6_validation,
    }
    results = []
    if args.phase == "all":
        results.append(run_gate_b6_preflight(root, config))
        results.append(dispatch_gate_b6_workers(root, config, phase="screen"))
        results.append(run_gate_b6_screen(root, config))
        results.append(dispatch_gate_b6_workers(root, config, phase="robustness"))
        results.append(run_gate_b6_robustness(root, config))
        results.append(run_gate_b6_calibration(root, config))
        results.append(run_gate_b6_freeze(root, config))
        results.append(run_gate_b6_validation(root, config))
    elif args.phase == "screen":
        results.append(dispatch_gate_b6_workers(root, config, phase="screen"))
        results.append(run_gate_b6_screen(root, config))
    elif args.phase == "robustness":
        results.append(dispatch_gate_b6_workers(root, config, phase="robustness"))
        results.append(run_gate_b6_robustness(root, config))
    else:
        results.append(phases[args.phase](root, config))
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
