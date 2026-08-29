from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import shutil
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
import pandas as pd

VERSION = "SKRU1_Data_Foundation_v3_2_1"
SEED = 3201
RNG = np.random.default_rng(SEED)


# ----------------------------- utilities ---------------------------------

def norm(s: str) -> str:
    return str(s).strip().lower()


def sha256(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, low_memory=False)


def write_csv(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig", lineterminator="\n")


def find_file(root: Path, filename: str, required: bool = True) -> Path | None:
    matches = list(root.rglob(filename))
    if not matches:
        if required:
            raise FileNotFoundError(f"Could not find {filename} under {root}")
        return None
    return sorted(matches, key=lambda p: (len(p.parts), str(p)))[0]


def find_col(df: pd.DataFrame, exact: Sequence[str] = (), contains: Sequence[str] = (),
             exclude: Sequence[str] = (), required: bool = True) -> str | None:
    lower = {norm(c): c for c in df.columns}
    for c in exact:
        if norm(c) in lower:
            return lower[norm(c)]
    for c in df.columns:
        lc = norm(c)
        if all(x.lower() in lc for x in contains) and not any(x.lower() in lc for x in exclude):
            return c
    if required:
        raise KeyError(f"Column not found: exact={exact}, contains={contains}, columns={list(df.columns)}")
    return None


def as_bool(series: pd.Series) -> pd.Series:
    if series.dtype == bool:
        return series.fillna(False)
    return series.astype(str).str.strip().str.lower().isin({"true", "1", "yes", "y", "да"})


def parse_dates(df: pd.DataFrame, candidates: Sequence[str]) -> str:
    c = find_col(df, exact=candidates, required=False)
    if c is None:
        for col in df.columns:
            lc = norm(col)
            if "date" in lc or "дата" in lc:
                c = col
                break
    if c is None:
        raise KeyError(f"No date column in {list(df.columns)}")
    df[c] = pd.to_datetime(df[c], errors="coerce")
    return c


def make_id(df: pd.DataFrame, candidates: Sequence[str], fallback_cols: Sequence[str]) -> tuple[str, pd.Series]:
    for c in candidates:
        if c in df.columns and df[c].notna().all() and df[c].astype(str).is_unique:
            return c, df[c].astype(str)
    vals = df[list(fallback_cols)].astype(str).agg("|".join, axis=1)
    return "sample_id", vals.map(lambda s: hashlib.sha1(s.encode("utf-8")).hexdigest()[:20])


def copytree_clean(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def value_col(df: pd.DataFrame, patterns: Sequence[Sequence[str]], required=True) -> str | None:
    for pats in patterns:
        c = find_col(df, contains=pats, required=False)
        if c is not None:
            return c
    if required:
        raise KeyError(f"Could not resolve value column from patterns {patterns}")
    return None


def infer_key_columns(a: pd.DataFrame, b: pd.DataFrame) -> list[str]:
    common = [c for c in a.columns if c in b.columns]
    preferred = [
        ["sample_id"], ["origin_id"], ["target_id"],
        ["campaign_id", "point_id"], ["cycle_id", "point_id"],
        ["point_id", "origin_date"], ["point_id", "current_date"],
    ]
    for cols in preferred:
        if all(c in common for c in cols):
            return cols
    ids = [c for c in common if c.endswith("_id")]
    if ids:
        return [ids[0]]
    raise KeyError(f"No common key columns. Common={common}")


def json_dump(obj, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


# ----------------------------- hotfix 0.1 --------------------------------

def reconcile_membership(snapshot: Path) -> dict:
    membership_path = find_file(snapshot, "campaign_point_membership.csv")
    adjusted_path = find_file(snapshot, "leveling_adjusted_epochs.csv")
    membership = read_csv(membership_path)
    adjusted = read_csv(adjusted_path)

    point_m = find_col(membership, exact=["point_id"])
    point_a = find_col(adjusted, exact=["point_id"])
    camp_m = find_col(membership, exact=["campaign_id", "cycle_id"], required=False)
    camp_a = find_col(adjusted, exact=["campaign_id", "cycle_id"], required=False)

    if camp_m and camp_a:
        membership["__key"] = membership[camp_m].astype(str) + "|" + membership[point_m].astype(str)
        adjusted["__key"] = adjusted[camp_a].astype(str) + "|" + adjusted[point_a].astype(str)
    else:
        date_m = parse_dates(membership, ["campaign_date", "date", "observation_date"])
        date_a = parse_dates(adjusted, ["campaign_date", "date", "observation_date"])
        membership["__key"] = membership[point_m].astype(str) + "|" + membership[date_m].dt.strftime("%Y-%m-%d")
        adjusted["__key"] = adjusted[point_a].astype(str) + "|" + adjusted[date_a].dt.strftime("%Y-%m-%d")

    # An adjusted epoch is admissible unless it is explicitly rejected/failed.
    accepted = pd.Series(True, index=adjusted.index)
    qc_col = find_col(adjusted, exact=["qc_status", "status", "adjustment_status"], required=False)
    if qc_col:
        bad = adjusted[qc_col].astype(str).str.lower().str.contains("reject|fail|invalid|discard")
        accepted &= ~bad
    accepted_col = find_col(adjusted, exact=["accepted", "is_accepted"], required=False)
    if accepted_col:
        accepted &= as_bool(adjusted[accepted_col])
    adjusted_keys = set(adjusted.loc[accepted, "__key"].astype(str))

    obs_col = find_col(membership, exact=["observed", "is_observed"])
    targeted_col = find_col(membership, exact=["targeted", "is_targeted"], required=False)
    miss_col = find_col(membership, exact=["missing_reason", "absence_reason"], required=False)
    if miss_col is None:
        miss_col = "missing_reason"
        membership[miss_col] = pd.NA
    status_col = find_col(membership, exact=["membership_status", "status"], required=False)

    old_obs = as_bool(membership[obs_col])
    new_obs = membership["__key"].isin(adjusted_keys)
    changed_true_to_false = int((old_obs & ~new_obs).sum())
    changed_false_to_true = int((~old_obs & new_obs).sum())
    membership[obs_col] = new_obs

    targeted = as_bool(membership[targeted_col]) if targeted_col else pd.Series(True, index=membership.index)
    rejected = targeted & ~new_obs & old_obs
    membership.loc[rejected, miss_col] = "rejected_after_qc"
    membership.loc[new_obs, miss_col] = pd.NA
    if status_col:
        membership.loc[new_obs, status_col] = "observed_accepted"
        membership.loc[rejected, status_col] = "rejected_after_qc"

    membership = membership.drop(columns=["__key"])
    write_csv(membership, membership_path)

    # Hard invariant after writing.
    check = read_csv(membership_path)
    if camp_m and camp_a:
        check_key = check[camp_m].astype(str) + "|" + check[point_m].astype(str)
    else:
        date_m = parse_dates(check, ["campaign_date", "date", "observation_date"])
        check_key = check[point_m].astype(str) + "|" + check[date_m].dt.strftime("%Y-%m-%d")
    assert as_bool(check[obs_col]).equals(check_key.isin(adjusted_keys)), \
        "observed_membership != adjusted_epoch_exists"

    return {
        "rows": len(membership),
        "changed_true_to_false": changed_true_to_false,
        "changed_false_to_true": changed_false_to_true,
        "accepted_adjusted_keys": len(adjusted_keys),
        "invariant": "observed == accepted_adjusted_epoch_exists",
    }


# ----------------------------- hotfix 0.2 --------------------------------

def locate_truth_tables(snapshot: Path) -> list[Path]:
    candidates = []
    for path in snapshot.rglob("*.csv"):
        rel = str(path.relative_to(snapshot)).lower()
        if not ("evaluation_only" in rel or "private_generation" in rel):
            continue
        try:
            df = pd.read_csv(path, nrows=10)
        except Exception:
            continue
        cols = [norm(c) for c in df.columns]
        has_point = any(c == "point_id" for c in cols)
        has_date = any("date" in c for c in cols)
        has_settlement = any("settlement" in c for c in cols)
        has_velocity = any("velocity" in c or "rate_mm" in c for c in cols)
        if has_point and has_date and has_settlement and has_velocity:
            candidates.append(path)
    return candidates


def continue_velocity(family: str, stage: str, last_v: float, month_idx: int,
                      last_accel: float, rng: np.random.Generator) -> tuple[float, str]:
    fam = norm(family)
    st = norm(stage)
    m = month_idx
    noise = rng.normal(0.0, 0.35)
    last_v = max(0.0, float(last_v))
    if "stable" in fam:
        v = max(0.0, last_v * math.exp(-m / 10.0) + noise * 0.25)
        new_stage = "stable"
    elif "decay" in fam:
        v = max(0.0, last_v * math.exp(-m / 22.0) + noise)
        new_stage = "residual" if v < 8 else "decaying"
    elif "uniform" in fam or "creep" in fam:
        seasonal = 0.9 * math.sin(2 * math.pi * m / 12.0)
        v = max(0.0, last_v + seasonal + noise)
        new_stage = "uniform_creep"
    elif "acceler" in fam:
        cap = min(220.0, max(last_v + 35.0, last_v * 1.25))
        v = last_v + (cap - last_v) * (1 - math.exp(-m / 7.0)) + noise
        new_stage = "accelerating" if v < cap * 0.93 else "active"
    elif "reactivat" in fam:
        if "quies" in st or last_v < 12:
            onset = 2
            if m < onset:
                v = max(0.0, last_v * 0.95 + noise * 0.2)
                new_stage = "quiescent"
            else:
                cap = min(210.0, max(70.0, last_v + 70.0))
                v = last_v + (cap - last_v) * (1 - math.exp(-(m-onset+1)/4.0)) + noise
                new_stage = "reactivated"
        else:
            v = max(0.0, last_v * math.exp(-m / 28.0) + noise)
            new_stage = "active_secondary" if v > 20 else "decaying"
    elif "step" in fam:
        if m == 1 and ("background" in st or "uniform" in st):
            v = min(200.0, last_v + max(25.0, abs(last_accel) * 0.25))
            new_stage = "step_transition"
        else:
            v = max(0.0, last_v * math.exp(-m / 35.0) + noise)
            new_stage = "post_step"
    else:
        v = max(0.0, last_v + last_accel / 12.0 + noise)
        new_stage = stage or "continued"
    return float(np.clip(v, 0.0, 220.0)), new_stage


def extend_truth_table(path: Path, endpoint: pd.Timestamp) -> dict:
    df = read_csv(path)
    point_col = find_col(df, exact=["point_id"])
    date_col = parse_dates(df, ["date", "month", "timestamp", "observation_date"])
    settle_col = value_col(df, [["true", "settlement"], ["settlement"]])
    vel_col = value_col(df, [["true", "velocity"], ["velocity"], ["rate", "mm_y"]])
    accel_col = value_col(df, [["true", "acceleration"], ["acceleration"]], required=False)
    family_col = find_col(df, exact=["process_family", "regime_family", "family"], required=False)
    stage_col = find_col(df, exact=["regime_stage", "process_stage", "stage"], required=False)

    df = df.sort_values([point_col, date_col]).reset_index(drop=True)
    current_max = df[date_col].max()
    if current_max >= endpoint:
        return {"path": str(path), "rows_added": 0, "old_endpoint": current_max, "new_endpoint": current_max}

    additions = []
    for point_id, g in df.groupby(point_col, sort=False):
        g = g.sort_values(date_col)
        last = g.iloc[-1].copy()
        last_date = pd.Timestamp(last[date_col])
        if last_date >= endpoint:
            continue
        dates = pd.date_range(last_date + pd.offsets.MonthBegin(1), endpoint, freq="MS")
        if len(dates) == 0:
            # Support month-end truth tables.
            dates = pd.date_range(last_date + pd.offsets.MonthEnd(1), endpoint, freq="ME")
        recent = g.tail(min(6, len(g)))
        last_v = float(pd.to_numeric(last[vel_col], errors="coerce") or 0.0)
        if accel_col and pd.notna(last.get(accel_col)):
            last_a = float(last[accel_col])
        elif len(recent) >= 2:
            rv = pd.to_numeric(recent[vel_col], errors="coerce").to_numpy(float)
            rd = pd.to_datetime(recent[date_col]).to_numpy()
            dt_y = max((pd.Timestamp(rd[-1]) - pd.Timestamp(rd[0])).days / 365.25, 1/12)
            last_a = float((rv[-1] - rv[0]) / dt_y)
        else:
            last_a = 0.0
        family = str(last.get(family_col, "uniform_creep")) if family_col else "uniform_creep"
        stage = str(last.get(stage_col, family)) if stage_col else family
        settlement = float(pd.to_numeric(last[settle_col], errors="coerce") or 0.0)
        prev_date = last_date
        prev_v = last_v
        prev_stage = stage
        for idx, dt in enumerate(dates, start=1):
            v, new_stage = continue_velocity(family, prev_stage, last_v, idx, last_a, RNG)
            dt_y = (pd.Timestamp(dt) - pd.Timestamp(prev_date)).days / 365.25
            settlement += max(0.0, 0.5 * (prev_v + v) * dt_y)
            row = last.copy()
            row[date_col] = pd.Timestamp(dt)
            row[settle_col] = settlement
            row[vel_col] = v
            if accel_col:
                row[accel_col] = (v - prev_v) / max(dt_y, 1e-6)
            if stage_col:
                row[stage_col] = new_stage
            # Explicitly tag the hotfix extension if a provenance-like field exists.
            for c in df.columns:
                if "provenance" in norm(c) and pd.api.types.is_object_dtype(df[c]):
                    row[c] = "S_hotfix_extension_v3_2_1"
            additions.append(row)
            prev_date = pd.Timestamp(dt)
            prev_v = v
            prev_stage = new_stage
    if additions:
        add_df = pd.DataFrame(additions, columns=df.columns)
        out = pd.concat([df, add_df], ignore_index=True).sort_values([point_col, date_col])
    else:
        out = df
    write_csv(out, path)
    return {
        "path": str(path),
        "rows_added": int(len(out) - len(df)),
        "old_endpoint": str(current_max.date()),
        "new_endpoint": str(out[date_col].max().date()),
        "points": int(out[point_col].nunique()),
    }


def extend_hidden_truth(snapshot: Path) -> dict:
    endpoint = pd.Timestamp("2026-06-30")
    tables = locate_truth_tables(snapshot)
    if not tables:
        raise RuntimeError("No evaluation/private monthly truth tables found")
    results = []
    for path in tables:
        try:
            results.append(extend_truth_table(path, endpoint))
        except Exception as e:
            # Some matching tables may be sparse event catalogs, not monthly truth.
            results.append({"path": str(path), "skipped": True, "reason": str(e)})
    extended = [r for r in results if r.get("rows_added", 0) > 0]
    if not extended:
        # At least one table must already have or reach endpoint.
        endpoints = [r.get("new_endpoint") for r in results if r.get("new_endpoint")]
        if not endpoints or max(endpoints) < "2026-06-30":
            raise AssertionError("Hidden truth did not reach 2026-06-30")
    return {"endpoint": "2026-06-30", "tables": results}


# ----------------------------- hotfix 0.3 --------------------------------

def add_missing_indicators_to_df(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    specs = [
        ("terrain_TRI_relative", "terrain_TRI_relative_is_missing"),
        ("terrain_roughness_relative", "terrain_roughness_relative_is_missing"),
        ("lithology__standard_uncertainty", "lithology_uncertainty_is_unknown"),
    ]
    added = []
    for source, flag in specs:
        if source in df.columns:
            df[flag] = df[source].isna().astype("int8")
            added.append(flag)
    return df, added


def add_missing_indicators(snapshot: Path) -> dict:
    modified = []
    all_added = set()
    for subdir in [snapshot / "model_ready", snapshot / "targets"]:
        if not subdir.exists():
            continue
        for path in subdir.rglob("*.csv"):
            try:
                df = read_csv(path)
            except Exception:
                continue
            before = set(df.columns)
            df, added = add_missing_indicators_to_df(df)
            if added and set(df.columns) != before:
                write_csv(df, path)
                modified.append(str(path.relative_to(snapshot)))
                all_added.update(added)
    return {"modified_files": modified, "added_flags": sorted(all_added)}


def update_feature_contract(snapshot: Path, added_flags: Sequence[str]) -> dict:
    paths = []
    for name in ["feature_contract.csv", "formal_feature_contract.csv"]:
        paths.extend(snapshot.rglob(name))
    updated = []
    for path in sorted(set(paths)):
        df = read_csv(path)
        feature_col = find_col(df, exact=["feature", "feature_name", "column"], required=False)
        if feature_col is None:
            continue
        allowed_col = find_col(df, exact=["allowed", "model_feature_allowed", "use_for_model"], required=False)
        role_col = find_col(df, exact=["role", "feature_role", "group"], required=False)
        reason_col = find_col(df, exact=["reason", "notes", "description"], required=False)
        rows = []
        existing = set(df[feature_col].astype(str))
        for flag in added_flags:
            if flag in existing:
                continue
            row = {c: pd.NA for c in df.columns}
            row[feature_col] = flag
            if allowed_col:
                row[allowed_col] = True
            if role_col:
                row[role_col] = "missingness_indicator"
            if reason_col:
                row[reason_col] = "Explicit distinction between unknown/missing and zero value"
            rows.append(row)
        if rows:
            df = pd.concat([df, pd.DataFrame(rows)], ignore_index=True)
            write_csv(df, path)
        updated.append(str(path.relative_to(snapshot)))
    return {"updated_contracts": updated}


# ------------------------- targets and frozen snapshot --------------------

def copy_eda_targets(eda_root: Path, snapshot: Path) -> Path:
    src = eda_root / "target_tables"
    dst = snapshot / "targets"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    # Copy selected metadata as provenance.
    meta_dst = snapshot / "metadata" / "eda_targets_v1"
    meta_dst.mkdir(parents=True, exist_ok=True)
    for name in ["eda_issue_register.csv", "target_validation_checks.csv"]:
        p = eda_root / "metadata" / name
        if p.exists():
            shutil.copy2(p, meta_dst / name)
    for name in ["EDA_REPORT_RU.md", "TARGET_SPECIFICATION_RU.md"]:
        p = eda_root / name
        if p.exists():
            shutil.copy2(p, snapshot / "documentation" / name)
    return dst


def canonicalize_t1(snapshot: Path) -> dict:
    features_path = find_file(snapshot / "targets", "next_planned_features.csv")
    ops_path = find_file(snapshot / "targets", "next_planned_operational_targets.csv")
    eval_path = find_file(snapshot / "targets", "next_planned_evaluation_truth.csv", required=False)
    features = read_csv(features_path)
    ops = read_csv(ops_path)
    features, added = add_missing_indicators_to_df(features)
    write_csv(features, features_path)

    keys = infer_key_columns(features, ops)
    merged = features.merge(ops, on=keys, how="left", suffixes=("", "__target"), validate="one_to_one")

    point_col = find_col(merged, exact=["point_id"])
    origin_col = find_col(merged, exact=["origin_date", "current_date", "feature_date", "date"])
    target_date_col = find_col(merged, exact=["target_date", "next_planned_date"], contains=["target", "date"])
    merged[origin_col] = pd.to_datetime(merged[origin_col], errors="coerce")
    merged[target_date_col] = pd.to_datetime(merged[target_date_col], errors="coerce")
    sample_col, sample_ids = make_id(merged, ["sample_id", "origin_id", "target_id"], [point_col, origin_col, target_date_col])
    if sample_col not in merged.columns:
        merged[sample_col] = sample_ids
    elif sample_col != "sample_id":
        merged["sample_id"] = merged[sample_col].astype(str)
        sample_col = "sample_id"

    available_col = find_col(merged, exact=["target_available", "label_available", "available"], required=False)
    if available_col is None:
        available = merged[target_date_col].notna()
    else:
        available = as_bool(merged[available_col])

    rate_col = None
    for c in merged.columns:
        lc = norm(c)
        if "rate" in lc and ("target" in lc or "next" in lc or "observed" in lc) and "true" not in lc and "last" not in lc:
            rate_col = c
            break
    inc_col = None
    for c in merged.columns:
        lc = norm(c)
        if "increment" in lc and ("target" in lc or "next" in lc or "observed" in lc):
            inc_col = c
            break
    next_settle_col = None
    for c in merged.columns:
        lc = norm(c)
        if "settlement" in lc and ("target" in lc or "next" in lc) and "true" not in lc and "current" not in lc:
            next_settle_col = c
            break
    current_settle_col = find_col(merged, exact=["current_settlement_mm", "settlement_current_mm", "last_settlement_mm"], required=False)
    horizon_col = find_col(merged, exact=["horizon_days", "target_horizon_days", "dt_days"], required=False)
    if horizon_col is None:
        merged["horizon_days"] = (merged[target_date_col] - merged[origin_col]).dt.days
        horizon_col = "horizon_days"
    if rate_col is None and inc_col is not None:
        merged["target_rate_mm_y"] = pd.to_numeric(merged[inc_col], errors="coerce") * 365.25 / pd.to_numeric(merged[horizon_col], errors="coerce")
        rate_col = "target_rate_mm_y"
    if inc_col is None and rate_col is not None:
        merged["target_increment_mm"] = pd.to_numeric(merged[rate_col], errors="coerce") * pd.to_numeric(merged[horizon_col], errors="coerce") / 365.25
        inc_col = "target_increment_mm"
    if next_settle_col is None and current_settle_col and inc_col:
        merged["target_next_settlement_mm"] = pd.to_numeric(merged[current_settle_col], errors="coerce") + pd.to_numeric(merged[inc_col], errors="coerce")
        next_settle_col = "target_next_settlement_mm"
    if rate_col is None:
        raise KeyError("Could not identify or derive T1 target rate")

    # Update availability after campaign hotfix using actual adjusted epochs.
    membership_path = find_file(snapshot, "campaign_point_membership.csv")
    adjusted_path = find_file(snapshot, "leveling_adjusted_epochs.csv")
    membership = read_csv(membership_path)
    adjusted = read_csv(adjusted_path)
    mp = find_col(membership, exact=["point_id"])
    ap = find_col(adjusted, exact=["point_id"])
    mc = find_col(membership, exact=["campaign_id", "cycle_id"], required=False)
    ac = find_col(adjusted, exact=["campaign_id", "cycle_id"], required=False)
    target_camp_col = find_col(merged, exact=["target_campaign_id", "next_campaign_id", "campaign_id_target"], required=False)
    actual_available = available.copy()
    if mc and ac and target_camp_col:
        adj_keys = set(adjusted[ac].astype(str) + "|" + adjusted[ap].astype(str))
        target_keys = merged[target_camp_col].astype(str) + "|" + merged[point_col].astype(str)
        actual_available = target_keys.isin(adj_keys)
    else:
        ad = parse_dates(adjusted, ["campaign_date", "date", "observation_date"])
        adj_keys = set(adjusted[ap].astype(str) + "|" + adjusted[ad].dt.strftime("%Y-%m-%d"))
        target_keys = merged[point_col].astype(str) + "|" + merged[target_date_col].dt.strftime("%Y-%m-%d")
        actual_available = target_keys.isin(adj_keys)
    merged["target_available"] = actual_available
    label_status_col = find_col(merged, exact=["label_status", "target_status"], required=False)
    if label_status_col is None:
        label_status_col = "label_status"
        merged[label_status_col] = pd.NA
    merged.loc[actual_available, label_status_col] = "available"
    merged.loc[~actual_available & merged[target_date_col].notna(), label_status_col] = "censored_rejected_or_missing_after_qc"

    # Canonical feature table: original feature columns plus identifiers/split context, no target columns.
    feature_cols = list(features.columns)
    id_cols = [sample_col, point_col, origin_col, target_date_col, horizon_col]
    for c in [find_col(merged, exact=["profile_id"], required=False),
              find_col(merged, exact=["origin_campaign_id", "current_campaign_id"], required=False),
              target_camp_col,
              find_col(merged, exact=["campaign_type", "target_campaign_type"], required=False)]:
        if c:
            id_cols.append(c)
    out_feature_cols = []
    for c in id_cols + feature_cols:
        if c in merged.columns and c not in out_feature_cols:
            out_feature_cols.append(c)
    feature_out = merged[out_feature_cols].copy()
    if "sample_id" not in feature_out.columns:
        feature_out.insert(0, "sample_id", merged[sample_col].astype(str))
    feature_out[origin_col] = pd.to_datetime(feature_out[origin_col]).dt.strftime("%Y-%m-%d")
    feature_out[target_date_col] = pd.to_datetime(feature_out[target_date_col]).dt.strftime("%Y-%m-%d")

    labels = pd.DataFrame({
        "sample_id": merged[sample_col].astype(str),
        "point_id": merged[point_col].astype(str),
        "origin_date": merged[origin_col].dt.strftime("%Y-%m-%d"),
        "target_date": merged[target_date_col].dt.strftime("%Y-%m-%d"),
        "horizon_days": pd.to_numeric(merged[horizon_col], errors="coerce"),
        "target_available": actual_available,
        "label_status": merged[label_status_col].astype(str),
        "target_rate_mm_y": pd.to_numeric(merged[rate_col], errors="coerce"),
        "target_increment_mm": pd.to_numeric(merged[inc_col], errors="coerce") if inc_col else np.nan,
        "target_next_settlement_mm": pd.to_numeric(merged[next_settle_col], errors="coerce") if next_settle_col else np.nan,
    })
    for c in [find_col(merged, exact=["target_standard_uncertainty_rate_mm_y", "target_rate_standard_uncertainty", "sigma_rate_mm_y"], required=False),
              find_col(merged, exact=["target_standard_uncertainty_increment_mm", "sigma_increment_mm"], required=False),
              find_col(merged, exact=["sample_weight", "target_weight"], required=False)]:
        if c:
            labels[c] = pd.to_numeric(merged[c], errors="coerce")
    labels.loc[~labels["target_available"], ["target_rate_mm_y", "target_increment_mm", "target_next_settlement_mm"]] = np.nan

    model_dir = snapshot / "model_ready"
    eval_dir = snapshot / "evaluation_only"
    model_dir.mkdir(parents=True, exist_ok=True)
    eval_dir.mkdir(parents=True, exist_ok=True)
    write_csv(feature_out, model_dir / "T1_next_planned_features.csv")
    write_csv(labels, eval_dir / "T1_next_planned_labels.csv")

    # Evaluation-only hidden truth copied/canonicalized if available.
    hidden_rows = 0
    if eval_path:
        ev = read_csv(eval_path)
        ev_keys = infer_key_columns(features, ev)
        e = features[ev_keys].merge(ev, on=ev_keys, how="left")
        if sample_col in features.columns:
            e.insert(0, "sample_id", features[sample_col].astype(str))
        elif "sample_id" not in e.columns:
            e.insert(0, "sample_id", merged[sample_col].astype(str))
        write_csv(e, eval_dir / "T1_hidden_truth_labels.csv")
        hidden_rows = len(e)

    return {
        "features_rows": len(feature_out),
        "labels_rows": len(labels),
        "available_labels": int(labels["target_available"].sum()),
        "censored_labels": int((~labels["target_available"]).sum()),
        "hidden_rows": hidden_rows,
        "sample_id_column": "sample_id",
        "point_column": point_col,
        "origin_column": origin_col,
        "target_date_column": target_date_col,
        "rate_source_column": rate_col,
    }


def find_main_truth(snapshot: Path) -> Path:
    best = None
    best_score = -1
    for p in locate_truth_tables(snapshot):
        try:
            df = pd.read_csv(p, nrows=100)
        except Exception:
            continue
        score = 0
        rel = str(p.relative_to(snapshot)).lower()
        if "monthly" in p.name.lower(): score += 5
        if "ground" in p.name.lower() or "truth" in p.name.lower(): score += 4
        if "evaluation_only" in rel: score += 3
        if "private_generation" in rel: score += 2
        score += min(len(df.columns), 20) / 20
        if score > best_score:
            best = p; best_score = score
    if best is None:
        raise RuntimeError("No main truth table")
    return best


def rebuild_early_warning(snapshot: Path) -> dict:
    features_path = snapshot / "model_ready" / "T1_next_planned_features.csv"
    features = read_csv(features_path)
    point_col = find_col(features, exact=["point_id"])
    origin_col = find_col(features, exact=["origin_date", "current_date"])
    features[origin_col] = pd.to_datetime(features[origin_col], errors="coerce")
    if "sample_id" not in features.columns:
        _, ids = make_id(features, [], [point_col, origin_col])
        features["sample_id"] = ids

    truth_path = find_main_truth(snapshot)
    truth = read_csv(truth_path)
    tp = find_col(truth, exact=["point_id"])
    td = parse_dates(truth, ["date", "month", "timestamp"])
    tv = value_col(truth, [["true", "velocity"], ["velocity"], ["rate", "mm_y"]])
    ta = value_col(truth, [["true", "acceleration"], ["acceleration"]], required=False)
    ts = find_col(truth, exact=["regime_stage", "process_stage", "stage"], required=False)
    truth = truth.sort_values([tp, td])
    truth_by_point = {str(k): g.copy() for k, g in truth.groupby(tp)}
    truth_endpoint = truth[td].max()

    rows = []
    onset_stages = {"accelerating", "reactivated", "step_transition", "onset", "active_secondary"}
    for _, r in features.iterrows():
        pid = str(r[point_col])
        origin = pd.Timestamp(r[origin_col])
        horizon_end = origin + pd.Timedelta(days=180)
        g = truth_by_point.get(pid)
        if g is None or g.empty:
            rows.append({"sample_id": r["sample_id"], "point_id": pid, "origin_date": origin.date(),
                         "horizon_end": horizon_end.date(), "label_available": False,
                         "label_status": "missing_truth", "T4_activity_180d": np.nan,
                         "T5_onset_180d": np.nan, "event_onset_date": pd.NaT})
            continue
        available = truth_endpoint >= horizon_end and g[td].max() >= horizon_end
        if not available:
            rows.append({"sample_id": r["sample_id"], "point_id": pid, "origin_date": origin.date(),
                         "horizon_end": horizon_end.date(), "label_available": False,
                         "label_status": "right_censored", "T4_activity_180d": np.nan,
                         "T5_onset_180d": np.nan, "event_onset_date": pd.NaT})
            continue
        prior = g[g[td] <= origin]
        future = g[(g[td] > origin) & (g[td] <= horizon_end)].copy()
        if prior.empty or future.empty:
            rows.append({"sample_id": r["sample_id"], "point_id": pid, "origin_date": origin.date(),
                         "horizon_end": horizon_end.date(), "label_available": False,
                         "label_status": "insufficient_truth", "T4_activity_180d": np.nan,
                         "T5_onset_180d": np.nan, "event_onset_date": pd.NaT})
            continue
        v0 = float(pd.to_numeric(prior.iloc[-1][tv], errors="coerce"))
        fv = pd.to_numeric(future[tv], errors="coerce").to_numpy(float)
        if ta:
            fa = pd.to_numeric(future[ta], errors="coerce").fillna(0).to_numpy(float)
        else:
            tyear = future[td].map(pd.Timestamp.toordinal).to_numpy(float) / 365.25
            fa = np.gradient(fv, tyear) if len(fv) > 1 else np.array([0.0])
        above = fv >= (v0 + 20.0)
        sustained = bool(np.any(above[:-1] & above[1:])) if len(above) >= 2 else False
        activity = bool((np.nanmax(fv - v0) >= 25.0) and (np.nanmax(fa) >= 15.0) and sustained)
        onset = False
        onset_date = pd.NaT
        if activity and ts:
            current_stage = str(prior.iloc[-1][ts]).lower()
            fstage = future[ts].astype(str).str.lower().tolist()
            for j, st in enumerate(fstage):
                if st in onset_stages and current_stage not in onset_stages:
                    onset = True
                    onset_date = future.iloc[j][td]
                    break
                current_stage = st
        elif activity:
            # Dynamics-only onset if no stage field exists.
            idx = np.where((fv - v0 >= 25.0) & (fa >= 15.0))[0]
            if len(idx):
                onset = True
                onset_date = future.iloc[int(idx[0])][td]
        rows.append({
            "sample_id": r["sample_id"], "point_id": pid,
            "origin_date": origin.date(), "horizon_end": horizon_end.date(),
            "label_available": True, "label_status": "available",
            "T4_activity_180d": int(activity), "T5_onset_180d": int(onset),
            "event_onset_date": onset_date.date() if pd.notna(onset_date) else pd.NaT,
            "origin_true_velocity_mm_y": v0,
            "max_future_velocity_mm_y": float(np.nanmax(fv)),
            "max_future_acceleration_mm_y2": float(np.nanmax(fa)),
        })
    out = pd.DataFrame(rows)
    out["split"] = np.select(
        [pd.to_datetime(out["horizon_end"]) <= pd.Timestamp("2023-12-31"),
         pd.to_datetime(out["horizon_end"]).dt.year == 2024],
        ["train", "validation"], default="test"
    )
    write_csv(out, snapshot / "evaluation_only" / "T5_early_warning_labels.csv")
    return {
        "rows": len(out),
        "available": int(as_bool(out["label_available"]).sum()),
        "right_censored": int((out["label_status"] == "right_censored").sum()),
        "T4_positive": int(pd.to_numeric(out["T4_activity_180d"], errors="coerce").fillna(0).sum()),
        "T5_positive": int(pd.to_numeric(out["T5_onset_180d"], errors="coerce").fillna(0).sum()),
        "test_T5_positive": int(pd.to_numeric(out.loc[out["split"] == "test", "T5_onset_180d"], errors="coerce").fillna(0).sum()),
        "truth_endpoint": str(truth_endpoint.date()),
    }


def freeze_splits(snapshot: Path) -> dict:
    labels = read_csv(snapshot / "evaluation_only" / "T1_next_planned_labels.csv")
    features = read_csv(snapshot / "model_ready" / "T1_next_planned_features.csv")
    merged = labels[["sample_id", "point_id", "origin_date", "target_date", "horizon_days", "target_available"]].merge(
        features[[c for c in ["sample_id", "profile_id", "zone_id", "spatial_zone", "target_campaign_type", "campaign_type"] if c in features.columns]],
        on="sample_id", how="left"
    )
    target_date = pd.to_datetime(merged["target_date"], errors="coerce")
    merged["split"] = np.select(
        [target_date <= pd.Timestamp("2023-12-31"), target_date.dt.year == 2024],
        ["train", "validation"], default="test"
    )
    merged["split_rule"] = "target_date"
    merged["dataset_version"] = VERSION
    write_csv(merged, snapshot / "metadata" / "frozen_splits.csv")
    return merged["split"].value_counts(dropna=False).to_dict()


def update_target_contract(snapshot: Path) -> dict:
    src = find_file(snapshot / "targets", "target_contract.json", required=False)
    if src:
        obj = read_json(src)
    else:
        obj = {}
    obj.update({
        "dataset_version": VERSION,
        "primary_target": {"id": "T1_RATE_NEXT_PLANNED", "unit": "mm_per_year"},
        "secondary_target": {"id": "T5_EW_ONSET_180D", "unit": "binary"},
        "derived_outputs": ["T1B_INCREMENT_NEXT_PLANNED", "T1C_CUMULATIVE_SETTLEMENT", "T6_PROFILE_KINEMATICS"],
        "splits": {"train_target_end": "2023-12-31", "validation_target_year": 2024, "test_target_start": "2025-01-01"},
        "censoring": {"next_planned_missing": "retain_origin_exclude_from_regression_loss", "right_censored_early_warning": "exclude_from_classification_loss"},
        "forbidden": {
            "random_row_split": True,
            "test_hyperparameter_tuning": True,
            "hidden_truth_as_feature": True,
            "terminal_map_as_feature": True,
            "generator_parameters_as_features": True,
        },
        "snapshot_frozen_at_utc": pd.Timestamp.utcnow().isoformat(),
    })
    path = snapshot / "metadata" / "target_contract.json"
    json_dump(obj, path)
    return obj


def write_protocol(snapshot: Path) -> None:
    text = f"""dataset_version: {VERSION}
random_seed: {SEED}

primary_target:
  id: T1_RATE_NEXT_PLANNED
  unit: mm_per_year

secondary_target:
  id: T5_EW_ONSET_180D
  unit: binary

derived_outputs:
  - T1B_INCREMENT_NEXT_PLANNED
  - T1C_CUMULATIVE_SETTLEMENT
  - T6_PROFILE_KINEMATICS

splits:
  train_target_end: 2023-12-31
  validation_target_year: 2024
  test_target_start: 2025-01-01
  split_time_field: target_date

forbidden:
  random_row_split: true
  test_hyperparameter_tuning: true
  hidden_truth_as_feature: true
  terminal_map_as_feature: true
  generator_parameters_as_features: true

external_validation:
  retraining_allowed: false
  threshold_retuning_allowed: false
  status: READY_PENDING_REAL_DATA
"""
    (snapshot / "metadata" / "experiment_protocol.yaml").write_text(text, encoding="utf-8")


def build_manifest(snapshot: Path) -> tuple[pd.DataFrame, Path]:
    rows = []
    for p in sorted(snapshot.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(snapshot).as_posix()
        if rel in {"metadata/data_manifest.csv", "metadata/data_checksums.sha256"}:
            continue
        rows.append({
            "relative_path": rel,
            "size_bytes": p.stat().st_size,
            "sha256": sha256(p),
            "role": rel.split("/", 1)[0] if "/" in rel else "root",
        })
    df = pd.DataFrame(rows)
    manifest_path = snapshot / "metadata" / "data_manifest.csv"
    write_csv(df, manifest_path)
    checksum_path = snapshot / "metadata" / "data_checksums.sha256"
    checksum_path.write_text("\n".join(f"{r['sha256']}  {r['relative_path']}" for r in rows) + "\n", encoding="utf-8")
    return df, checksum_path


def validate_hotfix(snapshot: Path, summaries: dict) -> pd.DataFrame:
    checks = []
    def add(cid, desc, observed, expected, passed):
        checks.append({"check_id": cid, "description": desc, "observed": observed, "expected": expected,
                       "status": "PASS" if bool(passed) else "FAIL"})

    # 1 membership equivalence
    m = read_csv(find_file(snapshot, "campaign_point_membership.csv"))
    a = read_csv(find_file(snapshot, "leveling_adjusted_epochs.csv"))
    pm = find_col(m, exact=["point_id"]); pa = find_col(a, exact=["point_id"])
    cm = find_col(m, exact=["campaign_id", "cycle_id"], required=False)
    ca = find_col(a, exact=["campaign_id", "cycle_id"], required=False)
    if cm and ca:
        mk = m[cm].astype(str)+"|"+m[pm].astype(str); ak=set(a[ca].astype(str)+"|"+a[pa].astype(str))
    else:
        dm=parse_dates(m,["campaign_date","date"]); da=parse_dates(a,["campaign_date","date"])
        mk=m[pm].astype(str)+"|"+m[dm].dt.strftime("%Y-%m-%d"); ak=set(a[pa].astype(str)+"|"+a[da].dt.strftime("%Y-%m-%d"))
    obs=as_bool(m[find_col(m, exact=["observed","is_observed"])])
    add("H001", "observed iff adjusted epoch exists", int((obs != mk.isin(ak)).sum()), 0, (obs == mk.isin(ak)).all())

    # 2 truth endpoint
    truth = read_csv(find_main_truth(snapshot)); td=parse_dates(truth,["date","month"])
    add("H002", "hidden truth endpoint", str(truth[td].max().date()), "2026-06-30 or later", truth[td].max() >= pd.Timestamp("2026-06-30"))

    # 3 missing flags
    f=read_csv(snapshot/"model_ready"/"T1_next_planned_features.csv")
    flags=["terrain_TRI_relative_is_missing","terrain_roughness_relative_is_missing","lithology_uncertainty_is_unknown"]
    add("H003", "explicit missingness flags", ",".join(c for c in flags if c in f.columns), ",".join(flags), all(c in f.columns for c in flags))

    # 4 contracts/snapshot
    required=[snapshot/"metadata/data_manifest.csv",snapshot/"metadata/data_checksums.sha256",
              snapshot/"targets/formal_feature_contract.csv",snapshot/"metadata/target_contract.json",
              snapshot/"metadata/frozen_splits.csv",snapshot/"metadata/experiment_protocol.yaml"]
    add("H004", "frozen snapshot artifacts", sum(p.exists() for p in required), len(required), all(p.exists() for p in required))

    # 5 leakage physical separation
    forbidden_tokens=["true_settlement","true_velocity","base_rate","event_amp","decay_tau","settlement_anchor_map"]
    leaks=[]
    for p in (snapshot/"model_ready").rglob("*.csv"):
        cols=[norm(c) for c in pd.read_csv(p,nrows=2).columns]
        leaks += [f"{p.name}:{c}" for c in cols if any(t in c for t in forbidden_tokens)]
    add("H005", "no hidden/generator/terminal-map columns in model_ready", len(leaks), 0, len(leaks)==0)

    # 6 T1 ids/splits
    lab=read_csv(snapshot/"evaluation_only"/"T1_next_planned_labels.csv")
    add("H006", "T1 sample IDs unique", int(lab["sample_id"].duplicated().sum()), 0, not lab["sample_id"].duplicated().any())
    splits=read_csv(snapshot/"metadata"/"frozen_splits.csv")
    add("H007", "frozen split coverage", len(splits), len(lab), len(splits)==len(lab))

    # 7 T5 horizon availability after extension
    ew=read_csv(snapshot/"evaluation_only"/"T5_early_warning_labels.csv")
    test=ew[ew["split"]=="test"]
    add("H008", "T5 test positive events", int(pd.to_numeric(test["T5_onset_180d"],errors="coerce").fillna(0).sum()), ">=2", pd.to_numeric(test["T5_onset_180d"],errors="coerce").fillna(0).sum()>=2)
    add("H009", "T5 late origins no endpoint censoring", int((test["label_status"]=="right_censored").sum()), 0, (test["label_status"]=="right_censored").sum()==0)

    out=pd.DataFrame(checks)
    write_csv(out,snapshot/"metadata"/"hotfix_validation_checks.csv")
    return out


def write_hotfix_report(snapshot: Path, summaries: dict, checks: pd.DataFrame) -> None:
    failed=checks.loc[checks.status!="PASS"]
    report=f"""# SKRU-1 Data Foundation v3.2.1 — hotfix report

## Status

- Dataset version: `{VERSION}`
- Checks passed: {int((checks.status=='PASS').sum())}/{len(checks)}
- Failed checks: {len(failed)}
- Truth endpoint: 2026-06-30
- Model-ready/private/evaluation layers are physically separated.

## Applied changes

1. `campaign_point_membership.observed` is now true only when an accepted adjusted leveling epoch exists.
2. Evaluation/private monthly truth was extended to 2026-06-30 without creating future observed campaigns.
3. Explicit missingness indicators were added for TRI, roughness and unknown lithology uncertainty.
4. Formal targets, feature contract, target contract, frozen temporal splits, manifest and checksums were frozen.

## Membership reconciliation

```json
{json.dumps(summaries['membership'], ensure_ascii=False, indent=2, default=str)}
```

## Truth extension

```json
{json.dumps(summaries['truth_extension'], ensure_ascii=False, indent=2, default=str)}
```

## Target rebuild

```json
{json.dumps(summaries['T1'], ensure_ascii=False, indent=2, default=str)}
```

## Early-warning labels

```json
{json.dumps(summaries['T5'], ensure_ascii=False, indent=2, default=str)}
```

## Research boundary

The snapshot is suitable for reproducible research experiments on reconstructed/synthetic data. It is not a production record of SKRU-1 and cannot establish operational accuracy without a frozen external test on real cycles.
"""
    (snapshot/"HOTFIX_REPORT_RU.md").write_text(report,encoding="utf-8")


def main() -> None:
    parser=argparse.ArgumentParser()
    parser.add_argument("--source",default="/mnt/data/SKRU1_data_reconstruction_v3_2")
    parser.add_argument("--eda",default="/mnt/data/SKRU1_v3_2_EDA_targets_v1")
    parser.add_argument("--output",default="/mnt/data/SKRU1_Data_Foundation_v3_2_1")
    args=parser.parse_args()
    source=Path(args.source); eda=Path(args.eda); snapshot=Path(args.output)
    if not source.exists(): raise FileNotFoundError(source)
    if not eda.exists(): raise FileNotFoundError(eda)
    copytree_clean(source,snapshot)
    (snapshot/"documentation").mkdir(exist_ok=True)
    copy_eda_targets(eda,snapshot)

    summaries={"version":VERSION}
    summaries["membership"]=reconcile_membership(snapshot)
    summaries["truth_extension"]=extend_hidden_truth(snapshot)
    summaries["missing_indicators"]=add_missing_indicators(snapshot)
    summaries["feature_contract"]=update_feature_contract(snapshot,summaries["missing_indicators"]["added_flags"])
    summaries["T1"]=canonicalize_t1(snapshot)
    summaries["T5"]=rebuild_early_warning(snapshot)
    summaries["splits"]=freeze_splits(snapshot)
    summaries["target_contract"]=update_target_contract(snapshot)
    write_protocol(snapshot)

    # Ensure formal feature contract exists in canonical location.
    src_contract=find_file(snapshot/"targets","formal_feature_contract.csv",required=False)
    if src_contract:
        shutil.copy2(src_contract,snapshot/"metadata"/"formal_feature_contract.csv")
    else:
        shutil.copy2(find_file(snapshot,"feature_contract.csv"),snapshot/"metadata"/"formal_feature_contract.csv")

    checks=validate_hotfix(snapshot,summaries)
    if not (checks.status=="PASS").all():
        raise AssertionError("Hotfix checks failed:\n"+checks.loc[checks.status!="PASS"].to_string(index=False))
    json_dump(summaries,snapshot/"metadata"/"hotfix_validation_report.json")
    write_hotfix_report(snapshot,summaries,checks)
    build_manifest(snapshot)
    # Rebuild manifest one final time so report/check artifacts are included.
    build_manifest(snapshot)
    (snapshot/"SUCCESS_V3_2_1.txt").write_text("PASS\n",encoding="utf-8")
    print(json.dumps({"status":"PASS","output":str(snapshot),"checks":len(checks),"summary":summaries},ensure_ascii=False,default=str))


if __name__=="__main__":
    try:
        main()
    except Exception:
        out=Path("/mnt/data/SKRU1_Data_Foundation_v3_2_1_FAILED.txt")
        out.write_text(traceback.format_exc(),encoding="utf-8")
        traceback.print_exc()
        sys.exit(1)
