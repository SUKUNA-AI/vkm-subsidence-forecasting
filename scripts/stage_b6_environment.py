#!/usr/bin/env python
"""Create one isolated Gate B6 environment and capture exact installation evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from skru1.environment_staging import refresh_environment_evidence, stage_environment
from skru1.gate_b6 import load_gate_b6_config


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment-id", required=True, choices=("b6_cpu", "b6_ngboost", "b6_torch"))
    parser.add_argument(
        "--evidence-only",
        action="store_true",
        help="Refresh sanitized durable reports from the already staged environment without network access.",
    )
    args = parser.parse_args()
    root, config = load_gate_b6_config()
    report = (
        refresh_environment_evidence(root, config, args.environment_id)
        if args.evidence_only
        else stage_environment(root, config, args.environment_id)
    )
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
