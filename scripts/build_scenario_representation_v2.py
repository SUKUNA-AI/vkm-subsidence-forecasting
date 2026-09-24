#!/usr/bin/env python3
"""Write compact representation manifests; no tensors, models or evaluator reads."""
from __future__ import annotations
import argparse
import gzip
import json
from pathlib import Path
import sys
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from skru1.scenario_adapter_v2 import load_model_data, FEATURES, METADATA
from skru1.scenario_boundary_v2 import DATA_SHA, CONSTRAINTS_SHA, file_sha256, model_worker_scope
from skru1.scenario_splits_v2 import group_manifest, build_folds
from skru1.scenario_sequences_v2 import CHANNELS


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def write_csv_gz(path, frame):
    with path.open("wb") as stream:
        with gzip.GzipFile(filename="", mode="wb", fileobj=stream, mtime=0) as archive:
            archive.write(frame.to_csv(index=False, lineterminator="\n").encode())


def build(output: Path):
    if output.exists() and any(output.iterdir()):
        raise FileExistsError("Representation output must be new/empty")
    output.mkdir(parents=True, exist_ok=True)
    config = json.loads((ROOT / "configs/scenario_adapter_v2.json").read_text(encoding="utf-8"))
    sequence = json.loads((ROOT / "configs/scenario_sequences_v2.json").read_text(encoding="utf-8"))
    with model_worker_scope(ROOT):
        bundle = load_model_data(ROOT)
        groups = group_manifest(ROOT, bundle)
        folds = build_folds(bundle, groups, config)
        write_csv_gz(output / "group_manifest.csv.gz", groups)
        membership = pd.DataFrame(
            [(f.fold_id, role, sid) for f in folds[:3]
             for role, ids in (("fit", f.fit_ids), ("validation", f.validation_ids)) for sid in ids],
            columns=["fold_id", "role", "sample_id"])
        write_csv_gz(output / "temporal_membership.csv.gz", membership)
        # Explicit group-level spatial exclusions across all replicas; row IDs are
        # reconstructed by predicates and checked against ordered hashes/counts.
        spatial = []
        point_groups = groups[["base_point_id", "base_profile_id", "zone_id"]].drop_duplicates()
        for fold in folds[3:]:
            column = "base_profile_id" if fold.kind == "profile" else "zone_id"
            for point in point_groups.itertuples(index=False):
                spatial.append({"fold_id": fold.fold_id, "base_point_id": point.base_point_id,
                                "held": getattr(point, column) == fold.held_group,
                                "replicas": "all_worlds_all_observation_conditions"})
        pd.DataFrame(spatial).to_csv(output / "spatial_group_manifest.csv", index=False, lineterminator="\n")
        def record(path):
            return {"path": path.name, "sha256": file_sha256(path), "size_bytes": path.stat().st_size}
        write_json(output / "split_manifest.json", {
            "dataset_sha256": DATA_SHA, "design": "forward_embargo_world_disjoint_plus_spatial_audits",
            "zone_semantics": config["zone_semantics"], "folds": [f.record() for f in folds],
            "files": [record(output / n) for n in ("group_manifest.csv.gz", "temporal_membership.csv.gz", "spatial_group_manifest.csv")],
        })
        write_json(output / "adapter_manifest.json", {
            "representation_id": config["representation_id"], "dataset_sha256": DATA_SHA,
            "constraints_sha256": CONSTRAINTS_SHA, "features": list(FEATURES), "metadata": list(METADATA),
            "outer_counts": config["outer_counts"], "origins": len(bundle.frames),
            "feature_contract_sha256": bundle.provenance["feature_schema_sha256"],
            "evaluator_in_model_bundle": False, "calibration_target_in_model_bundle": False,
            "split_manifest_sha256": file_sha256(output / "split_manifest.json"),
        })
        write_json(output / "sequence_manifest.json", {
            **sequence, "origins": len(bundle.windows), "channels": list(CHANNELS),
            "tensor_storage": "lazy_runtime_only", "raw_dtype": "float64", "network_dtype": "float32",
            "history_rows": len(bundle.history.frame),
            "adapter_manifest_sha256": file_sha256(output / "adapter_manifest.json"),
        })
        sources = ["configs/scenario_adapter_v2.json", "configs/scenario_sequences_v2.json",
                   "docs/governance/SCENARIO_V2_REPRESENTATION_PROTOCOL.md",
                   "scripts/build_scenario_representation_v2.py"]
        sources += [f"src/skru1/{name}.py" for name in (
            "scenario_boundary_v2", "scenario_adapter_v2", "scenario_scorer_v2", "scenario_splits_v2",
            "scenario_sequences_v2", "scenario_preprocessing_v2", "scenario_worker_v2")]
        write_json(output / "representation_manifest.json", {
            "representation_id": config["representation_id"], "dataset_sha256": DATA_SHA,
            "constraints_sha256": CONSTRAINTS_SHA, "freeze": "write_once_before_any_model_results",
            "sources": [{"path": n, "sha256": file_sha256(ROOT / n)} for n in sources],
            "outputs": [record(p) for p in sorted(output.iterdir()) if p.is_file()],
            "models_executed": 0, "legacy_holdout_labels_parsed": 0,
        })
    print(json.dumps({"origins": len(bundle.frames), "folds": len(folds),
                      "representation_sha256": file_sha256(output / "representation_manifest.json")}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = (ROOT / args.output).resolve()
    if not output.is_relative_to(ROOT):
        raise ValueError("Output must be repository relative")
    build(output)
