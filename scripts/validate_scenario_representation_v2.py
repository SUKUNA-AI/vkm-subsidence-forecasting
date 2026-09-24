#!/usr/bin/env python3
"""Independent serialized-input acceptance, without production transformations.

Only scorer-side loaders open synthetic evaluator payloads. No estimator imports.
"""
from __future__ import annotations
import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from skru1.scenario_boundary_v2 import DATA_DIRECTORY, DATA_SHA, CONSTRAINTS_SHA, file_sha256
from skru1.scenario_scorer_v2 import load_campaign_plan_for_qa, load_evaluator_truth


def validate_inputs(root: Path) -> dict:
    directory = root / DATA_DIRECTORY
    checks = {}
    def check(name, result):
        checks[name] = bool(result)
        if not result:
            print("FAIL", name, flush=True)
    check("dataset_identity", file_sha256(directory / "manifest.json") == DATA_SHA)
    check("constraints_identity", file_sha256(root / "artifacts/reconstruction/scenario_constraints_v2/manifest.json") == CONSTRAINTS_SHA)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    for section in ("inputs", "outputs"):
        for record in manifest[section]:
            path = (root if section == "inputs" else directory) / record["path"]
            check(f"frozen_bytes:{record['path']}", path.stat().st_size == record["size_bytes"] and file_sha256(path) == record["sha256"])
    contract = json.loads((directory / "feature_contract.json").read_text(encoding="utf-8"))
    f = pd.read_csv(directory / "model_features.csv.gz")
    h = pd.read_csv(directory / "causal_history.csv.gz").sort_values(["point_id", "current_date"]).reset_index(drop=True)
    w = pd.read_csv(directory / "sequence_windows.csv.gz")
    splits = pd.read_csv(directory / "split_assignments.csv.gz")
    check("exact_feature_allowlist", list(f) == contract["metadata"] + contract["features"] and len(contract["features"]) == 16)
    check("unique_origins_and_history", not f.sample_id.duplicated().any() and not h.history_id.duplicated().any()
          and not f.duplicated(["point_id", "current_date", "target_date"]).any())
    check("all_origin_and_window_ids", len(f) == 321766 and f.sample_id.equals(w.sample_id) and f.sample_id.equals(splits.sample_id))
    check("outer_counts", splits.model_role.value_counts().to_dict() == {
        "train": 122047, "calibration": 35148, "evaluation": 133833, "excluded": 30738})
    check("held_mechanisms_absent_from_fit_calibration", not splits.loc[splits.model_role.isin(["train", "calibration"]), "experiment_role"].eq("heldout_mechanism").any())
    group = h.groupby("point_id", sort=False)
    dt_days = pd.to_datetime(h.current_date).groupby(h.point_id).diff().dt.days
    rate = group.last_settlement_mm.diff() / (dt_days / 365.25)
    acceleration = rate.groupby(h.point_id).diff() / ((dt_days + dt_days.groupby(h.point_id).shift(1)) / (2 * 365.25))
    def equal(a, b, atol=2e-6):
        return np.allclose(a, b, rtol=1e-8, atol=atol, equal_nan=True)
    check("rate_signed_positive_down_derivation", equal(rate, h.last_rate_mm_y))
    check("acceleration_midpoint_spacing", equal(acceleration, h.recent_acceleration_mm_y2))
    h["gap_days_check"] = dt_days
    h["mean_rate_check"] = group.last_rate_mm_y.transform(lambda x: x.rolling(3, min_periods=1).mean())
    h["std_rate_check"] = group.last_rate_mm_y.transform(lambda x: x.rolling(3, min_periods=1).std(ddof=0))
    check("rate_volatility", equal(h.std_last_3_rates_mm_y, h.std_rate_check))
    check("n_history", np.array_equal(group.cumcount()+1, h.n_history))
    joined = f.merge(h, on=["point_id", "current_date"], validate="many_to_one", suffixes=("", "_history"))
    check("every_origin_has_exact_current_history", len(joined) == 321766)
    for column in ("last_settlement_mm", "last_rate_mm_y", "recent_acceleration_mm_y2", "n_history",
                   "std_last_3_rates_mm_y", "current_standard_uncertainty_mm", "missing_campaigns_since_previous"):
        check("origin_history:" + column, equal(joined[column], joined[column+"_history"]))
    check("origin_gap", equal(joined.days_since_previous_observation, joined.gap_days_check))
    check("origin_last_three_rate_mean", equal(joined.mean_last_3_rates_mm_y, joined.mean_rate_check))
    horizons = (pd.to_datetime(f.target_date) - pd.to_datetime(f.current_date)).dt.days
    check("positive_exact_horizons", horizons.gt(0).all() and np.array_equal(horizons, f.forecast_horizon_days))
    provenance = json.loads((directory / "field_provenance.json").read_text(encoding="utf-8"))
    check("positive_down_sign_authority", provenance["sign_convention"] == "positive_down_mm")
    # QA reads only plan flags from evaluator, never gives this table to a worker.
    plan = load_campaign_plan_for_qa(root).sort_values(["entity_point_id", "date"]).reset_index(drop=True)
    targeted = plan.loc[plan.targeted].copy()
    targeted["planned_index"] = targeted.groupby("entity_point_id").cumcount()
    targeted["next_campaign"] = targeted.groupby("entity_point_id").campaign_id.shift(-1)
    current = f.merge(targeted, left_on=["point_id", "current_campaign_id"], right_on=["entity_point_id", "campaign_id"], validate="many_to_one")
    check("next_planned_not_next_successful", len(current) == len(f) and current.next_campaign.equals(f.target_campaign_id))
    observed = targeted.loc[targeted.observed].copy()
    observed["missed_check"] = observed.groupby("entity_point_id").planned_index.diff() - 1
    exact = h.merge(observed, left_on=["point_id", "campaign_id"], right_on=["entity_point_id", "campaign_id"], validate="one_to_one")
    check("history_exactly_successful_observations", len(exact) == len(h) == int(plan.observed.sum()))
    check("missing_campaign_counts_not_zero_filled", equal(exact.missing_campaigns_since_previous, exact.missed_check))
    target = f.merge(plan, left_on=["point_id", "target_campaign_id"], right_on=["entity_point_id", "campaign_id"], validate="many_to_one")
    check("target_dates_match_planned_campaign", target.date.equals(f.target_date))
    truth = load_evaluator_truth(root)._truth  # scorer-side QA scope only
    check("scorer_origin_identity", truth.sample_id.equals(f.sample_id))
    check("unavailable_targets_stay_nan", truth.loc[~target.observed, "observed_rate_mm_y"].isna().all())
    # Independent observed target arithmetic from the target's history row when available.
    target_observation = f.merge(h[["point_id", "campaign_id", "last_settlement_mm"]],
                                left_on=["point_id", "target_campaign_id"], right_on=["point_id", "campaign_id"],
                                how="left", suffixes=("", "_target"), validate="many_to_one")
    expected_rate = (target_observation.last_settlement_mm_target - f.last_settlement_mm) / (horizons / 365.25)
    check("observed_target_arithmetic", equal(expected_rate, truth.observed_rate_mm_y))
    for role in ("train", "calibration"):
        labels = pd.read_csv(directory / f"targets/{role}_observed.csv.gz")
        expected_ids = splits.loc[splits.model_role.eq(role), "sample_id"]
        check(role+"_target_join_exact", labels.sample_id.is_unique and set(labels.sample_id) == set(expected_ids))
        expected = truth.set_index("sample_id").loc[labels.sample_id, "observed_rate_mm_y"].to_numpy()
        check(role+"_target_values_exact", np.array_equal(labels.observed_rate_mm_y, expected))
    lookup = {point: (g.history_id.tolist(), dict(zip(g.current_date, range(len(g))))) for point, g in h.groupby("point_id", sort=False)}
    digest = sha256()
    for row in w.itertuples(index=False):
        history_ids, dates = lookup[row.point_id]
        end = dates[row.current_date] + 1
        expected = history_ids[max(0, end-16):end]
        if json.loads(row.history_ids_json) != expected or row.sequence_length != len(expected) or row.left_padding != 16-len(expected):
            raise AssertionError("Independent authoritative-window/cutoff parity")
        digest.update((row.sample_id + "\n" + "\n".join(expected) + "\n").encode())
    check("all_windows_own_point_chronological_causal_current_final", True)
    passed = all(checks.values())
    return {"status": "PASS" if passed else "FAIL", "checks": checks,
            "origins_checked": len(f), "origins_accepted": len(f) if passed else None,
            "windows_accepted": len(w),
            "history_rows_checked": len(h), "next_planned_unavailable_target_origins": int((~target.observed).sum()),
            "window_identity_sha256": digest.hexdigest(), "dataset_sha256": DATA_SHA,
            "constraints_sha256": CONSTRAINTS_SHA, "models_executed": 0, "legacy_holdout_labels_parsed": 0,
            "method": "serialized_inputs_independent_of_adapter_generator_tensorizer_transformations"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = validate_inputs(ROOT)
    output = (ROOT / args.output).resolve()
    if not output.is_relative_to(ROOT):
        raise ValueError("Output must be repository relative")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError("Acceptance receipt is write-once")
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "checks"}))
    if result["status"] != "PASS":
        raise SystemExit(1)
