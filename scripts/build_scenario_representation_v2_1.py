#!/usr/bin/env python3
"""Write compact representation manifests; no tensors, models or evaluator reads."""
from __future__ import annotations
import argparse
import gzip
import json
from pathlib import Path
import sys
import pandas as pd
import numpy as np
from hashlib import sha256

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from skru1.scenario_adapter_v2_1 import load_model_data, FEATURES, METADATA
from skru1.scenario_boundary_v2_1 import DATASET, DATA_SHA, CONSTRAINTS_SHA, file_sha256, model_worker_scope
from skru1.scenario_splits_v2_1 import group_manifest, build_folds
from skru1.scenario_sequences_v2_1 import CHANNELS, SequenceTensorizer
from skru1.scenario_preprocessing_v2_1 import FoldPreprocessor


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def write_csv_gz(path, frame):
    with path.open("wb") as stream:
        with gzip.GzipFile(filename="", mode="wb", fileobj=stream, mtime=0) as archive:
            archive.write(frame.to_csv(index=False, lineterminator="\n").encode())


def build(output: Path):
    receipt_path = ROOT / "artifacts/data_quality/scenario_simulation_v2_1/correction_receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt["status"] != "PASS" or receipt["new_manifest_sha256"] != DATA_SHA:
        raise ValueError("Representation requires the accepted corrective release")
    if output.exists() and any(output.iterdir()):
        raise FileExistsError("Representation output must be new/empty")
    output.mkdir(parents=True, exist_ok=True)
    config = json.loads((ROOT / "configs/scenario_adapter_v2_1.json").read_text(encoding="utf-8"))
    sequence = json.loads((ROOT / "configs/scenario_sequences_v2_1.json").read_text(encoding="utf-8"))
    if config["dataset_manifest_sha256"] != DATA_SHA or sequence["numeric_channels"] != list(CHANNELS):
        raise ValueError("Representation config differs from the pinned authority")
    with model_worker_scope(ROOT):
        bundle = load_model_data(ROOT)
        groups = group_manifest(ROOT, bundle)
        folds = build_folds(bundle, groups, config)
        tensorizer = SequenceTensorizer(bundle)
        runtime = validate_runtime(bundle, tensorizer)
        write_json(output / "runtime_sequence_receipt.json", runtime)
        (output / "preprocessing").mkdir()
        for fold in folds:
            for kind in ("tabular", "sequence"):
                preprocessor = FoldPreprocessor(kind).fit(bundle, groups, fold,
                    frozen_record=fold.record(), tensorizer=tensorizer)
                write_json(output / "preprocessing" / f"{fold.fold_id}_{kind}.json", preprocessor.state_dict())
            print("Preprocessing frozen:", fold.fold_id, flush=True)
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
            return {"path": path.relative_to(output).as_posix(), "sha256": file_sha256(path), "size_bytes": path.stat().st_size}
        write_json(output / "split_manifest.json", {
            "dataset_sha256": DATA_SHA, "design": "forward_embargo_world_disjoint_plus_spatial_audits",
            "zone_semantics": config["zone_semantics"], "folds": [f.record() for f in folds],
            "files": [record(output / n) for n in ("group_manifest.csv.gz", "temporal_membership.csv.gz", "spatial_group_manifest.csv")],
        })
        write_json(output / "adapter_manifest.json", {
            "representation_id": config["representation_id"], "dataset_sha256": DATA_SHA,
            "dataset_id": DATASET,
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
        sources = ["configs/scenario_adapter_v2_1.json", "configs/scenario_sequences_v2_1.json",
                   "docs/governance/SCENARIO_V2_1_REPRESENTATION_PROTOCOL.md",
                   "scripts/build_scenario_representation_v2_1.py"]
        sources += [f"src/skru1/{name}.py" for name in (
            "scenario_boundary_v2_1", "scenario_adapter_v2_1", "scenario_scorer_v2_1", "scenario_splits_v2_1",
            "scenario_sequences_v2_1", "scenario_preprocessing_v2_1", "scenario_worker_v2_1")]
        write_json(output / "representation_manifest.json", {
            "representation_id": config["representation_id"], "dataset_sha256": DATA_SHA,
            "dataset_id": DATASET,
            "data_correction_receipt_sha256": file_sha256(receipt_path),
            "constraints_sha256": CONSTRAINTS_SHA, "freeze": "write_once_before_any_model_results",
            "sources": [{"path": n, "sha256": file_sha256(ROOT / n)} for n in sources],
            "outputs": [record(p) for p in sorted(output.rglob("*")) if p.is_file()],
            "models_executed": 0, "legacy_holdout_labels_parsed": 0,
        })
    print(json.dumps({"origins": len(bundle.frames), "folds": len(folds),
                      "representation_sha256": file_sha256(output / "representation_manifest.json")}))


def validate_runtime(bundle, tensorizer):
    """Exact serialized-window gather and canonical streaming digest; no tensor dump."""
    history = bundle.history.frame.sort_values(["point_id", "current_date"]).copy()
    history["days_since_previous_observation"] = history.groupby("point_id").current_date.diff().dt.days
    lookup = history.set_index("history_id")
    windows = bundle.windows.set_index("sample_id")
    origins = bundle.frames.set_index("sample_id")
    digest = sha256()
    dtype = np.dtype([("values", "<f8", (16, 5)), ("length", "<i8"),
                      ("padding", "u1", (16,)), ("observation", "u1", (16,)),
                      ("missing", "u1", (16,)), ("valid", "u1", (16, 5))], align=False)
    checked = 0
    for batch in tensorizer.batches(bundle.frames.sample_id):
        w = windows.loc[list(batch.sample_ids)]
        flat_ids = [sid for ids in w.history_ids_json for sid in json.loads(ids)]
        expected = lookup.loc[flat_ids, list(CHANNELS)].to_numpy(float)
        np.testing.assert_array_equal(batch.values[batch.observation_mask], expected)
        o = origins.loc[list(batch.sample_ids)]
        for i, name in enumerate(CHANNELS):
            np.testing.assert_array_equal(batch.values[:, -1, i], o[name].to_numpy())
        assert np.array_equal(batch.observation_mask, ~batch.padding_mask)
        assert np.array_equal(batch.lengths, w.sequence_length)
        assert np.array_equal(batch.padding_mask.sum(axis=1), 16-batch.lengths)
        assert (batch.lengths <= 16).all() and (batch.lengths >= 1).all()
        assert np.all(np.diff(batch.padding_mask.astype(int), axis=1) <= 0)
        assert (batch.values[batch.padding_mask] == 0).all()
        assert np.array_equal(batch.value_valid_mask, np.isfinite(batch.values) & batch.observation_mask[:, :, None])
        assert np.array_equal(batch.missing_campaign_mask, (batch.values[:, :, 4] > 0) & batch.observation_mask)
        records = np.empty(len(batch.sample_ids), dtype=dtype)
        records["values"] = batch.values
        records["length"] = batch.lengths
        records["padding"] = batch.padding_mask
        records["observation"] = batch.observation_mask
        records["missing"] = batch.missing_campaign_mask
        records["valid"] = batch.value_valid_mask
        digest.update(records.tobytes())
        checked += len(batch.sample_ids)
    assert checked == 321766
    return dict(status="PASS", origins_checked=checked, final_token_channel_checks=checked*5,
                final_token_mismatches=0, sequence_window_mismatches=0, channels=list(CHANNELS),
                canonical_runtime_sha256=digest.hexdigest(), canonical_record_dtype=dtype.descr,
                row_order="frozen model_features sample order", tensor_dump_stored=False,
                models_executed=0, legacy_holdout_labels_parsed=0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = (ROOT / args.output).resolve()
    if not output.is_relative_to(ROOT):
        raise ValueError("Output must be repository relative")
    build(output)
