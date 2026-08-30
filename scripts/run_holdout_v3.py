#!/usr/bin/env python3
"""Inspect, freeze, or consume the new T1 final holdout exactly once."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT_HINT = Path(__file__).resolve().parents[1]
SRC = ROOT_HINT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


from skru1.holdout_intake import (
    evaluate_holdout_once,
    freeze_holdout,
    inspect_holdout_candidate,
    load_holdout_v3_config,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase",
        choices=("status", "freeze", "evaluate-once"),
        default="status",
        help="evaluate-once irreversibly consumes the access ledger, including on failure",
    )
    parser.add_argument("--root", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root, config = load_holdout_v3_config(args.root)
    if args.phase == "status":
        result, _, _ = inspect_holdout_candidate(root, config, write_status=True)
    elif args.phase == "freeze":
        result = freeze_holdout(root, config)
    else:
        result = evaluate_holdout_once(root, config)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
