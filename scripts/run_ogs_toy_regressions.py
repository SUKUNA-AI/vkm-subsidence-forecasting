"""Run preserved TOY checks in a new explicit workspace, never in frozen inputs.

Requires an existing Bash/runtime activation script. It installs no runtime and
does not treat command success as scientific acceptance. Invoke with --help.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

TOYS = ("case00_install_sanity", "case01_elastic_plane_strain", "case02_gravity_layered",
        "case03_material_mapping", "case04_creep", "case04b_plasticity_mfront_mc",
        "case05_mined_region", "case06_backfill", "case07_surface_observation")
INPUT_SUFFIXES = {".py", ".sh", ".prj", ".yaml", ".yml", ".gml", ".vtu"}
RELATIVE_PACKAGE = Path("10_physics_evidence/physical_evidence_v1")


def fingerprint(root):
    rows = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("Symlink or escaping source path is not a supported input")
        if path.is_file():
            rows[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return rows


def run(source_package, output_root, bash, activation_script, cases=TOYS):
    source = Path(source_package).resolve(strict=True)
    output_arg = Path(output_root).absolute()
    output = output_arg.resolve()
    if (not source.is_dir() or output_arg.exists() or output_arg.is_symlink()
            or output == source or output.is_relative_to(source) or source.is_relative_to(output)):
        raise ValueError("Output must be a new workspace outside the source package")
    bash = Path(bash).resolve(strict=True)
    activation = Path(activation_script).resolve(strict=True)
    if not bash.is_file() or not activation.is_file() or not cases or not set(cases) <= set(TOYS):
        raise ValueError("Explicit existing Bash, activation script and TOY selection are required")
    before = fingerprint(source)
    required = [source / "ogs/tools", *(source / "ogs/cases" / name for name in TOYS)]
    selected = []
    for directory in required:
        if not directory.is_dir() or directory.is_symlink() or not directory.resolve().is_relative_to(source):
            raise ValueError("Required generic tools and TOY inputs must belong to the source package")
        for path in sorted(directory.rglob("*")):
            if path.is_symlink() or not path.resolve().is_relative_to(source):
                raise ValueError("Input symlink or path escapes the source package")
            if path.is_file() and path.suffix in INPUT_SUFFIXES:
                selected.append(path)
    if any(not (source / "ogs/cases" / name / "run.sh").is_file() for name in TOYS):
        raise ValueError("Every preserved TOY must have an explicit input launcher")
    # Exclusive creation, no recycling or recursive cleanup of a computed path.
    output.mkdir(parents=True, exist_ok=False)
    staged = output / RELATIVE_PACKAGE
    for path in selected:
        destination = staged / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(path.read_bytes())
    shim = output / "work/ogs/.venv-ogs/bin/activate"
    shim.parent.mkdir(parents=True, exist_ok=True)
    shim.write_bytes(b'. "$VKM_TOY_ACTIVATION"\n')
    environment = {**os.environ, "VKM_TOY_ACTIVATION": activation.as_posix()}
    results = []
    failure = None
    try:
        for name in cases:
            with (output / (name + ".launch.log")).open("wb") as log:
                completed = subprocess.run([str(bash), (staged / "ogs/cases" / name / "run.sh").as_posix()],
                                           cwd=output, env=environment, stdout=log, stderr=subprocess.STDOUT)
            results.append({"case": name, "exit_code": completed.returncode})
            if completed.returncode:
                break
    except OSError as exc:
        failure = type(exc).__name__
    unchanged = fingerprint(source) == before
    receipt = {"schema": "vkm.isolated-toy-execution/1", "source_unchanged": unchanged,
               "input_sha256": {path.relative_to(source).as_posix(): before[path.relative_to(source).as_posix()]
                                for path in selected},
               "results": results, "launch_error": failure, "scientific_admission": "NOT_ESTABLISHED"}
    (output / "TOY_EXECUTION.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8", newline="\n")
    if not unchanged:
        raise ValueError("Source tree changed during isolated execution; inspect the receipt")
    return 0 if not failure and len(results) == len(cases) and all(r["exit_code"] == 0 for r in results) else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-package", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--bash", type=Path, required=True)
    parser.add_argument("--activation-script", type=Path, required=True)
    parser.add_argument("--case", action="append", choices=TOYS)
    args = parser.parse_args(argv)
    return run(args.source_package, args.output_root, args.bash, args.activation_script, args.case or TOYS)


if __name__ == "__main__":
    raise SystemExit(main())
