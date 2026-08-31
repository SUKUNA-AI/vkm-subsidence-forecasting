#!/usr/bin/env python
"""Execute one frozen Gate B6 model shard in its declared environment."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from skru1.b6_worker import run_environment_worker
from skru1.gate_b6 import load_gate_b6_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment-id", required=True, choices=("b6_cpu", "b6_ngboost", "b6_torch"))
    parser.add_argument("--phase", required=True, choices=("screen", "robustness"))
    parser.add_argument("--model-id", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root, config = load_gate_b6_config()
    result = run_environment_worker(
        root,
        config,
        environment_id=args.environment_id,
        phase=args.phase,
        model_id=args.model_id,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
