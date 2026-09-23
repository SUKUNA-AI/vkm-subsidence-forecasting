#!/usr/bin/env python3
"""Generate data only. No estimator, benchmark or model-selection imports."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from skru1.scenario_simulation_v2 import generate

if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--root",type=Path,default=Path("."))
    p.add_argument("--config",default="configs/scenario_simulation_v2.json")
    p.add_argument("--output",default="data/scenario_simulation_v2")
    a=p.parse_args()
    print(json.dumps(generate(a.root,a.root/a.config,a.root/a.output),ensure_ascii=False,indent=2))
