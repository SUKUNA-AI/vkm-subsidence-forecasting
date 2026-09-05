#!/usr/bin/env python3
"""Build the versioned reconstruction-research tables without fitting any model."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from skru1.reconstruction_data import build_release

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".", type=Path)
    ap.add_argument("--output", help="Repository-relative output; default comes from the versioned policy")
    args = ap.parse_args()
    print(json.dumps(build_release(args.root, args.output), ensure_ascii=False, indent=2))
