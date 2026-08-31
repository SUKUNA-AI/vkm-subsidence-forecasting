#!/usr/bin/env python
"""Freeze the Gate B6 primary on all train rows in its declared environment."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from skru1.b6_worker import freeze_full_train_model
from skru1.gate_b6 import load_gate_b6_config


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-id", required=True)
    args = parser.parse_args()
    root, config = load_gate_b6_config()
    result = freeze_full_train_model(root, config, model_id=args.model_id)
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
