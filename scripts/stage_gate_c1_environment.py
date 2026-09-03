#!/usr/bin/env python
"""Create the fresh Gate C1 torch environment and capture exact evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from skru1.gate_c1 import load_gate_c1_config
from skru1.gate_c1_environment import refresh_environment_evidence, stage_fresh_environment


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-only", action="store_true")
    args = parser.parse_args()
    root, config = load_gate_c1_config()
    result = (
        refresh_environment_evidence(root, config)
        if args.evidence_only
        else stage_fresh_environment(root, config)
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
