#!/usr/bin/env python3
"""Build a new evidence release, refusing to overwrite a populated destination."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from skru1.empirical_constraints_v2 import build_constraints

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, default=Path("."))
    p.add_argument("--config", default="configs/scenario_constraints_v2.json")
    p.add_argument("--output", default="artifacts/reconstruction/scenario_constraints_v2")
    a = p.parse_args()
    print(json.dumps(build_constraints(a.root.resolve(), a.root / a.config, a.root / a.output), indent=2))
