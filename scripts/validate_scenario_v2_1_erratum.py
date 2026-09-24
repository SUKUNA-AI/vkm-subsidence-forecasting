#!/usr/bin/env python3
"""Independent serialized v2 -> v2.1 differential audit. Data QA, no models."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OLD_SHA = "7bb67939314fbcf548686893e6868c90a2303e1303c401377f197e077fec8c13"
CONSTRAINTS_SHA = "96b88b61cf766062c1f831e32b50bdd82441da63c50ce41569a5ffad297de6ef"
FIELD = "days_since_previous_observation"


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def verify_release(root, directory):
    manifest = read_json(directory / "manifest.json")
    for section in ("inputs", "outputs"):
        for row in manifest[section]:
            path = (root if section == "inputs" else directory) / row["path"]
            assert digest(path) == row["sha256"] and path.stat().st_size == row["size_bytes"], row["path"]
    return manifest


def validate(root, new_directory):
    old = root / "data/scenario_simulation_v2"
    assert digest(old / "manifest.json") == OLD_SHA
    assert digest(root / "artifacts/reconstruction/scenario_constraints_v2/manifest.json") == CONSTRAINTS_SHA
    om, nm = verify_release(root, old), verify_release(root, new_directory)
    assert nm["dataset_id"] == "SKRU1_SCENARIO_SIMULATION_V2_1"
    assert nm["erratum"]["original_manifest_sha256"] == OLD_SHA
    a = pd.read_csv(old / "model_features.csv.gz", dtype=str, keep_default_na=False)
    b = pd.read_csv(new_directory / "model_features.csv.gz", dtype=str, keep_default_na=False)
    assert len(a) == len(b) == 321766 and a.sample_id.is_unique and b.sample_id.is_unique
    assert a.sample_id.equals(b.sample_id) and list(a) == list(b)
    assert a.drop(columns=FIELD).equals(b.drop(columns=FIELD)), "Unintended model feature/metadata difference"
    delta = b[FIELD].astype(int) - a[FIELD].astype(int)
    affected = delta.ne(0)
    assert affected.sum() == 31283 and delta.loc[affected].eq(1).all()
    history = pd.read_csv(new_directory / "causal_history.csv.gz").sort_values(["point_id", "current_date"])
    history["exact_elapsed_days"] = pd.to_datetime(history.current_date).groupby(history.point_id).diff().dt.days
    history["previous_observed_date"] = history.groupby("point_id").current_date.shift(1)
    joined = b.merge(history[["point_id", "current_date", "previous_observed_date", "exact_elapsed_days"]],
                     on=["point_id", "current_date"], validate="many_to_one", how="left")
    mismatch = joined.exact_elapsed_days.ne(joined[FIELD].astype(int))
    assert not mismatch.any(), "Calendar invariant"
    expected_affected = joined.exact_elapsed_days.ne(a[FIELD].astype(int))
    assert affected.equals(expected_affected)
    historical_path = root / "artifacts/data_quality/scenario_representation_v2/elapsed_days_mismatches.csv.gz"
    assert digest(historical_path) == "fff387f9e419d68a6ad3f21dc78c73bdb327e049cbe299b3a08b72e7618292ad"
    historical = pd.read_csv(historical_path)
    assert set(historical.sample_id) == set(a.loc[affected, "sample_id"])
    witness = historical.set_index("sample_id").loc[a.loc[affected, "sample_id"]]
    assert np.array_equal(witness.days_since_previous_observation, a.loc[affected, FIELD].astype(int))
    assert np.array_equal(witness.exact_elapsed_days, b.loc[affected, FIELD].astype(int))
    windows = pd.read_csv(new_directory / "sequence_windows.csv.gz")
    lookup = {p: (g.history_id.tolist(), dict(zip(g.current_date, range(len(g)))))
              for p, g in history.groupby("point_id", sort=False)}
    assert windows.sample_id.equals(b.sample_id)
    for row in windows.itertuples(index=False):
        ids, dates = lookup[row.point_id]
        end = dates[row.current_date] + 1
        assert json.loads(row.history_ids_json) == ids[max(0, end-16):end]
        assert row.sequence_length == min(end, 16) and row.left_padding + row.sequence_length == 16
    # JSON fields with numerical changes have a strict whitelist.
    old_stats, new_stats = read_json(old / "distribution_summary.json"), read_json(new_directory / "distribution_summary.json")
    assert {k:v for k,v in old_stats.items() if k != "observed_history_gap_days"} == {k:v for k,v in new_stats.items() if k != "observed_history_gap_days"}
    gap = b[FIELD].astype(float)
    expected_stats = {"n": len(gap), "min": float(gap.min()), "q05": float(gap.quantile(.05)),
                      "q50": float(gap.quantile(.5)), "q95": float(gap.quantile(.95)),
                      "q99": float(gap.quantile(.99)), "max": float(gap.max()), "mean": float(gap.mean()), "std": float(gap.std(ddof=1))}
    # Use the actual frozen statistic key set; arithmetic is independent.
    for k, v in new_stats["observed_history_gap_days"].items():
        assert k in expected_stats and v == expected_stats[k], (k, v)
    ov, nv = read_json(old / "validation_report.json"), read_json(new_directory / "validation_report.json")
    assert all(nv[k] == v for k,v in ov.items())
    assert set(nv)-set(ov) == {"exact_calendar_elapsed_days", "window_chronology"}
    assert nv["exact_calendar_elapsed_days"]["origins_checked"] == 321766
    assert nv["exact_calendar_elapsed_days"]["mismatch_count"] == 0
    assert nv["window_chronology"] == {"origins_checked": 321766, "mismatch_count": 0}
    oc, nc = read_json(old / "generation_config.json"), read_json(new_directory / "generation_config.json")
    metadata = {"dataset_id", "output_directory", "code_commit", "inputs", "erratum"}
    assert {k:v for k,v in oc.items() if k not in metadata} == {k:v for k,v in nc.items() if k not in metadata}
    assert nc["inputs"][:-1] == oc["inputs"]
    assert nc["inputs"][-1]["sha256"] == OLD_SHA
    names = sorted(p.relative_to(old).as_posix() for p in old.rglob("*") if p.is_file())
    assert names == sorted(p.relative_to(new_directory).as_posix() for p in new_directory.rglob("*") if p.is_file())
    parity = []
    change_types = {
        "model_features.csv.gz": ("EXPECTED_CHANGED", "exactly 31283 cells in elapsed days, +1"),
        "distribution_summary.json": ("EXPECTED_CHANGED", "only observed_history_gap_days statistics"),
        "validation_report.json": ("EXPECTED_CHANGED", "two constructive invariants added; all prior fields exact"),
        "generation_config.json": ("SEMANTICALLY_IDENTICAL_REVERSIONED", "generation parameters unchanged; identity/lineage only"),
        "manifest.json": ("SEMANTICALLY_IDENTICAL_REVERSIONED", "identity, erratum lineage, source/output hashes"),
        "README.md": ("SEMANTICALLY_IDENTICAL_REVERSIONED", "new identity and explicit erratum notice"),
    }
    for name in names:
        same = digest(old/name) == digest(new_directory/name)
        classification, reason = ("BYTE_IDENTICAL", "exact bytes, including all table values") if same else change_types.get(name, ("UNEXPECTED_CHANGED", "not permitted"))
        parity.append(dict(path=name, classification=classification, reason=reason,
                           old_sha256=digest(old/name), new_sha256=digest(new_directory/name)))
    unexpected = sum(r["classification"] == "UNEXPECTED_CHANGED" for r in parity)
    assert unexpected == 0, parity
    return dict(status="PASS", blocker_id="V2-REP-001", original_release_id=om["dataset_id"], original_manifest_sha256=OLD_SHA,
                new_release_id=nm["dataset_id"], new_manifest_sha256=digest(new_directory/"manifest.json"),
                constraints_manifest_sha256=CONSTRAINTS_SHA, origins_checked=321766, elapsed_day_mismatches=0,
                affected_origins=int(affected.sum()), differences={"+1":int(affected.sum()), "0":int((~affected).sum())},
                other_15_features_and_all_metadata="EXACT", historical_blocker_ids="EXACT", windows_checked=len(windows),
                unexpected_difference_count=unexpected, output_parity=parity, old_gap_statistics=old_stats["observed_history_gap_days"],
                new_gap_statistics=new_stats["observed_history_gap_days"], models_executed=0, model_scoring=0, legacy_labels_parsed=0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="data/scenario_simulation_v2_1")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = validate(ROOT, ROOT / args.dataset)
    output = (ROOT / args.output).resolve()
    if not output.is_relative_to(ROOT) or output.exists():
        raise ValueError("Receipt must be new and repository-relative")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({k:v for k,v in result.items() if k not in {"output_parity", "old_gap_statistics", "new_gap_statistics"}}))
