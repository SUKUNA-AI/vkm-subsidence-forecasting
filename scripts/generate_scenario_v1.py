#!/usr/bin/env python3
"""Generate SKRU1_SCENARIO_SIMULATION_V1 from verified corrected inputs."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT_HINT = Path(__file__).resolve().parents[1]
SRC = ROOT_HINT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from skru1.scenario_simulation import generate_scenario_dataset, repository_path


DEFAULT_CONFIG = "configs/scenario_simulation_v1.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()
    root = args.root.resolve()
    config_path = repository_path(root, args.config)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    output = repository_path(root, args.output or config["output_directory"])
    validation = generate_scenario_dataset(
        root,
        config_path,
        output,
        script_path=Path(__file__),
    )
    print(json.dumps(validation, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
