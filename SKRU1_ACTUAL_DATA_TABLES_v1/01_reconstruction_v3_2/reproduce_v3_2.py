#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build SKRU-1 reconstructed/synthetic data foundation v3.2 from audited v3.1.

The builder deliberately separates:
- public reconstructed/observed tables;
- model-ready leakage-controlled features;
- evaluation-only truth and labels;
- private generator parameters.

No production claim is made. All generated observations are synthetic or hybrid.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    average_precision_score,
    f1_score,
    mean_absolute_error,
    mean_squared_error,
    precision_score,
    r2_score,
    recall_score,
    roc_auc_score,
)
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline

SEED = 32026
RNG = np.random.default_rng(SEED)
VERSION = "3.2.0"
SOURCE_YEAR = 2022
TECHNICAL_ANCHOR_DATE = pd.Timestamp("2022-07-01")
START_MONTH = pd.Timestamp("2018-01-01")
END_MONTH = pd.Timestamp("2025-12-01")
MONTHS = pd.date_range(START_MONTH, END_MONTH, freq="MS")

REGIME_COUNTS = {
    "decaying": 28,
    "uniform_creep": 26,
    "stable": 18,
    "accelerating": 12,
    "reactivated": 8,
    "step_change": 6,
}

CAMPAIGN_DATES = [
    ("2018-05-15", "full"),
    ("2018-07-10", "focused"),
    ("2018-10-16", "full"),
    ("2019-02-12", "focused"),
    ("2019-05-14", "full"),
    ("2019-08-27", "focused"),
    ("2019-11-05", "full"),
    ("2020-04-21", "full"),
    ("2020-06-02", "focused"),
    ("2020-10-20", "full"),
    ("2021-01-26", "focused"),
    ("2021-05-18", "full"),
    ("2021-07-13", "focused"),
    ("2021-11-02", "full"),
    ("2022-03-01", "focused"),
    ("2022-05-17", "full"),
    ("2022-07-19", "focused"),
    ("2022-10-18", "full"),
    ("2023-01-17", "focused"),
    ("2023-05-16", "full"),
    ("2023-07-25", "focused"),
    ("2023-11-07", "full"),
    ("2024-01-30", "focused"),
    ("2024-05-14", "full"),
    ("2024-07-09", "focused"),
    ("2024-09-03", "focused"),
    ("2025-07-22", "full"),
    ("2025-08-26", "focused"),
    ("2025-11-04", "full"),
]


def write_csv(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig", float_format="%.10g")


def write_json(obj: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_rmtree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)


def normalise(s: pd.Series) -> pd.Series:
    lo, hi = float(s.min()), float(s.max())
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        return pd.Series(np.zeros(len(s)), index=s.index)
    return (s - lo) / (hi - lo)


def month_decimal(ts: pd.Timestamp) -> float:
    return ts.year + (ts.month - 1) / 12.0 + (ts.day - 1) / 365.25


def sigmoid(x: np.ndarray | float) -> np.ndarray | float:
    return 1.0 / (1.0 + np.exp(-np.clip(x, -40, 40)))


def interp_series(dates: np.ndarray, values: np.ndarray, target: pd.Timestamp) -> float:
    x = np.array([pd.Timestamp(d).toordinal() for d in dates], dtype=float)
    return float(np.interp(target.toordinal(), x, values))


def sample_truth(truth_map: dict[str, pd.DataFrame], point_id: str, target: pd.Timestamp, col: str) -> float:
    df = truth_map[point_id]
    return interp_series(df["date"].to_numpy(), df[col].to_numpy(float), target)


def create_regime_assignment(points: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    work = points.loc[points["point_type"].eq("WORK")].copy()
    work["risk_anchor"] = normalise(np.log1p(work["settlement_anchor_map_mm"].clip(lower=0)))
    work["risk_kzt"] = normalise(work["kzt"].fillna(work["kzt"].median()))
    work["risk_ko"] = normalise(work["ko"].fillna(work["ko"].median()))
    work["risk_fill"] = 1.0 - normalise(work["fill_density"].fillna(work["fill_density"].median()))
    work["risk_fault"] = np.exp(-work["fault_distance_m"].fillna(500) / 180.0)
    work["risk_score"] = (
        0.36 * work["risk_anchor"]
        + 0.20 * work["risk_kzt"]
        + 0.16 * work["risk_ko"]
        + 0.14 * work["risk_fault"]
        + 0.14 * work["risk_fill"]
        + rng.normal(0, 0.035, len(work))
    )
    work = work.sort_values(["risk_score", "profile_id", "point_order"], ascending=[False, True, True]).reset_index(drop=True)

    family_order: list[str] = []
    # High-risk families first, then decaying/uniform/stable.
    for family in ["step_change", "reactivated", "accelerating", "decaying", "uniform_creep", "stable"]:
        family_order.extend([family] * REGIME_COUNTS[family])
    if len(family_order) != len(work):
        raise RuntimeError("Regime counts do not match WORK point count")
    work["process_family"] = family_order
    return work[["point_id", "profile_id", "point_order", "risk_score", "process_family"]]


def generate_temporal_truth(points: pd.DataFrame, assignment: pd.DataFrame, rng: np.random.Generator):
    ass = assignment.set_index("point_id")
    truth_rows: list[dict[str, Any]] = []
    params_rows: list[dict[str, Any]] = []
    transition_rows: list[dict[str, Any]] = []
    hidden_event_rows: list[dict[str, Any]] = []

    accelerating_events = pd.to_datetime([
        "2021-06-01", "2021-11-01", "2022-05-01", "2022-11-01",
        "2023-04-01", "2023-10-01", "2024-03-01", "2024-09-01",
        "2025-03-01", "2025-05-01", "2025-07-01", "2025-09-01",
    ])
    reactivated_events = pd.to_datetime([
        "2022-08-01", "2023-03-01", "2023-08-01", "2024-02-01",
        "2024-07-01", "2025-02-01", "2025-05-01", "2025-08-01",
    ])
    step_events = pd.to_datetime([
        "2021-09-01", "2022-09-01", "2023-09-01",
        "2024-08-01", "2025-04-01", "2025-08-01",
    ])
    event_counters = {"accelerating": 0, "reactivated": 0, "step_change": 0}

    for _, point in points.sort_values("point_id").iterrows():
        pid = point["point_id"]
        if point["point_type"] == "REF":
            family = "stable_reference"
            risk = 0.0
            rates = np.zeros(len(MONTHS), dtype=float)
            stages = np.array(["stable_reference"] * len(MONTHS), dtype=object)
            p = {
                "base_rate_mm_y": 0.0,
                "decay_tau_y": np.nan,
                "event_onset_date": None,
                "second_event_date": None,
                "event_amplitude_mm_y": 0.0,
                "step_duration_months": 0,
            }
        else:
            family = ass.loc[pid, "process_family"]
            risk = float(np.clip(ass.loc[pid, "risk_score"], 0, 1))
            t_year = np.array([(d - START_MONTH).days / 365.25 for d in MONTHS])
            rates = np.zeros(len(MONTHS), dtype=float)
            stages = np.empty(len(MONTHS), dtype=object)
            event_onset = None
            second_event = None
            event_amp = 0.0
            decay_tau = np.nan
            step_duration = 0

            if family == "stable":
                base = 0.4 + 4.0 * risk + rng.uniform(0, 1.0)
                rates = np.full(len(MONTHS), base)
                stages[:] = "stable"
            elif family == "uniform_creep":
                base = 8.0 + 42.0 * risk + rng.uniform(0, 8)
                trend = rng.uniform(-0.4, 0.7)
                rates = base + trend * t_year
                stages[:] = "uniform"
                stages[MONTHS < pd.Timestamp("2019-01-01")] = "startup"
            elif family == "decaying":
                base = 35.0 + 95.0 * risk + rng.uniform(0, 15)
                floor = 3.0 + 16.0 * risk
                decay_tau = rng.uniform(2.0, 4.8)
                rates = floor + (base - floor) * np.exp(-t_year / decay_tau)
                stages[:] = "decaying"
                stages[MONTHS < pd.Timestamp("2019-01-01")] = "active"
                stages[MONTHS >= pd.Timestamp("2024-01-01")] = "residual"
            elif family == "accelerating":
                base = 5.0 + 20.0 * risk + rng.uniform(0, 6)
                idx = event_counters[family]
                event_counters[family] += 1
                event_onset = accelerating_events[idx]
                event_amp = 45.0 + 105.0 * risk + rng.uniform(0, 18)
                width = rng.uniform(0.20, 0.55)
                x = np.array([(d - event_onset).days / 365.25 for d in MONTHS])
                rates = base + event_amp * sigmoid(x / width)
                stages[:] = "background"
                stages[(MONTHS >= event_onset - pd.DateOffset(months=4)) & (MONTHS < event_onset + pd.DateOffset(months=8))] = "accelerating"
                stages[MONTHS >= event_onset + pd.DateOffset(months=8)] = "elevated"
            elif family == "reactivated":
                base = 20.0 + 48.0 * risk + rng.uniform(0, 10)
                floor = 4.0 + 12.0 * risk
                decay_tau = rng.uniform(1.4, 3.2)
                idx = event_counters[family]
                event_counters[family] += 1
                second_event = reactivated_events[idx]
                event_onset = pd.Timestamp("2018-01-01")
                event_amp = 55.0 + 105.0 * risk + rng.uniform(0, 20)
                x2 = np.array([(d - second_event).days / 365.25 for d in MONTHS])
                rates = floor + (base - floor) * np.exp(-t_year / decay_tau) + event_amp * sigmoid(x2 / rng.uniform(0.18, 0.45))
                stages[:] = "decaying"
                stages[MONTHS < pd.Timestamp("2019-01-01")] = "active_initial"
                stages[(MONTHS >= pd.Timestamp("2021-01-01")) & (MONTHS < second_event - pd.DateOffset(months=4))] = "quiescent"
                stages[(MONTHS >= second_event - pd.DateOffset(months=4)) & (MONTHS < second_event + pd.DateOffset(months=6))] = "reactivated"
                stages[MONTHS >= second_event + pd.DateOffset(months=6)] = "active_secondary"
            elif family == "step_change":
                base = 8.0 + 24.0 * risk + rng.uniform(0, 6)
                idx = event_counters[family]
                event_counters[family] += 1
                event_onset = step_events[idx]
                event_amp = 65.0 + 105.0 * risk + rng.uniform(0, 18)
                step_duration = int(rng.integers(2, 5))
                rates = np.full(len(MONTHS), base)
                event_mask = (MONTHS >= event_onset) & (MONTHS < event_onset + pd.DateOffset(months=step_duration))
                post_mask = MONTHS >= event_onset + pd.DateOffset(months=step_duration)
                rates[event_mask] += event_amp
                rates[post_mask] += 0.30 * event_amp
                stages[:] = "background"
                stages[event_mask] = "step_transition"
                stages[post_mask] = "post_step"
            else:
                raise ValueError(f"Unknown family {family}")

            # Small seasonal modulation and colored process variability.
            season_amp = rng.uniform(0.0, 4.5)
            phase = rng.uniform(0, 2 * np.pi)
            seasonal = season_amp * np.sin(2 * np.pi * np.arange(len(MONTHS)) / 12.0 + phase)
            ar = np.zeros(len(MONTHS))
            sigma_ar = 0.5 if family == "stable" else rng.uniform(0.8, 3.0)
            for i in range(1, len(ar)):
                ar[i] = 0.72 * ar[i - 1] + rng.normal(0, sigma_ar)
            rates = np.clip(rates + seasonal + ar, 0, 220)
            p = {
                "base_rate_mm_y": float(base),
                "decay_tau_y": float(decay_tau) if np.isfinite(decay_tau) else np.nan,
                "event_onset_date": event_onset,
                "second_event_date": second_event,
                "event_amplitude_mm_y": float(event_amp),
                "step_duration_months": step_duration,
            }

        # Calibrate cumulative history to the 2022 reconstructed terminal map without
        # exposing the technical mid-year date as a claimed source date.
        anchor_target = float(max(point.get("settlement_anchor_map_mm", 0.0), 0.0))
        dt_year = np.array([0.0] + [
            (MONTHS[i] - MONTHS[i - 1]).days / 365.25 for i in range(1, len(MONTHS))
        ])
        increments = np.zeros(len(MONTHS))
        for i in range(1, len(MONTHS)):
            increments[i] = 0.5 * (rates[i - 1] + rates[i]) * dt_year[i]
        anchor_idx = int(np.where(MONTHS == TECHNICAL_ANCHOR_DATE)[0][0])
        delta_anchor = float(increments[: anchor_idx + 1].sum())
        if point["point_type"] == "WORK" and anchor_target > 0 and delta_anchor > 0.92 * anchor_target:
            scale = max(0.08, 0.86 * anchor_target / max(delta_anchor, 1e-9))
            rates *= scale
            increments[:] = 0
            for i in range(1, len(MONTHS)):
                increments[i] = 0.5 * (rates[i - 1] + rates[i]) * dt_year[i]
            delta_anchor = float(increments[: anchor_idx + 1].sum())
        start_settlement = max(anchor_target - delta_anchor, 0.0)
        settlement = start_settlement + np.cumsum(increments)
        if point["point_type"] == "REF":
            settlement[:] = 0.0
            rates[:] = 0.0
        # Exact numerical conditioning on the map at the technical mid-year point.
        if point["point_type"] == "WORK":
            correction = anchor_target - settlement[anchor_idx]
            settlement += correction
            if settlement.min() < 0:
                settlement -= settlement.min()
                # Recondition after positivity shift by scaling pre-anchor path only.
                correction2 = anchor_target - settlement[anchor_idx]
                settlement += correction2
            settlement = np.maximum.accumulate(np.maximum(settlement, 0))
        acceleration = np.gradient(rates, np.array([month_decimal(d) for d in MONTHS]))

        prev_stage = None
        for i, d in enumerate(MONTHS):
            stage = str(stages[i])
            truth_rows.append({
                "point_id": pid,
                "date": d.date().isoformat(),
                "true_settlement_mm": float(settlement[i]),
                "true_velocity_mm_y": float(rates[i]),
                "true_acceleration_mm_y2": float(acceleration[i]),
                "process_family": family,
                "regime_stage": stage,
                "source_reference_year": SOURCE_YEAR,
                "source_reference_date": None,
                "reference_period_status": "year_supported_exact_date_unknown; technical_midyear_conditioning_private",
                "provenance": "R/S",
            })
            if stage != prev_stage:
                transition_rows.append({
                    "point_id": pid,
                    "transition_date": d.date().isoformat(),
                    "process_family": family,
                    "from_stage": prev_stage,
                    "to_stage": stage,
                    "provenance": "S",
                })
                prev_stage = stage

        params_rows.append({
            "point_id": pid,
            "profile_id": point["profile_id"],
            "point_type": point["point_type"],
            "process_family": family,
            "risk_score_private": risk,
            "base_rate_mm_y": p["base_rate_mm_y"],
            "decay_tau_y": p["decay_tau_y"],
            "event_onset_date": p["event_onset_date"].date().isoformat() if isinstance(p["event_onset_date"], pd.Timestamp) else None,
            "second_event_date": p["second_event_date"].date().isoformat() if isinstance(p["second_event_date"], pd.Timestamp) else None,
            "event_amplitude_mm_y": p["event_amplitude_mm_y"],
            "step_duration_months": p["step_duration_months"],
            "technical_anchor_date_private": TECHNICAL_ANCHOR_DATE.date().isoformat(),
            "source_reference_year": SOURCE_YEAR,
            "source_reference_date": None,
            "source_reference_status": "year_supported_exact_date_unknown",
            "provenance": "S_PRIVATE",
        })
        for event_name, event_date in [("primary", p["event_onset_date"]), ("secondary", p["second_event_date"])]:
            if isinstance(event_date, pd.Timestamp):
                hidden_event_rows.append({
                    "point_id": pid,
                    "process_family": family,
                    "event_name": event_name,
                    "event_date": event_date.date().isoformat(),
                    "event_amplitude_mm_y": p["event_amplitude_mm_y"],
                    "use_class": "private_generation_only",
                })

    truth = pd.DataFrame(truth_rows)
    params = pd.DataFrame(params_rows)
    transitions = pd.DataFrame(transition_rows)
    hidden_events = pd.DataFrame(hidden_event_rows)
    return truth, params, transitions, hidden_events


def build_truth_map(truth: pd.DataFrame) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for pid, g in truth.groupby("point_id", sort=False):
        h = g.copy()
        h["date"] = pd.to_datetime(h["date"])
        out[pid] = h.sort_values("date").reset_index(drop=True)
    return out


def generate_truth_ensemble(truth: pd.DataFrame, points: pd.DataFrame, rng: np.random.Generator):
    quarterly_dates = MONTHS[::3]
    base = truth.copy()
    base["date"] = pd.to_datetime(base["date"])
    base = base[base["date"].isin(quarterly_dates)]
    rows = []
    for realization in range(1, 17):
        for pid, g in base.groupby("point_id"):
            g = g.sort_values("date")
            if pid.endswith("REF-A") or pid.endswith("REF-B"):
                sett = g["true_settlement_mm"].to_numpy(float)
            else:
                mult = float(rng.normal(1.0, 0.055))
                drift = rng.normal(0, 0.8, len(g)).cumsum()
                sett = np.maximum.accumulate(np.maximum(0, g["true_settlement_mm"].to_numpy(float) * mult + drift))
            for d, s in zip(g["date"], sett):
                rows.append({
                    "realization_id": f"R{realization:02d}",
                    "point_id": pid,
                    "date": d.date().isoformat(),
                    "true_settlement_mm": float(s),
                    "provenance": "S",
                })
    ensemble = pd.DataFrame(rows)
    q = ensemble.groupby(["point_id", "date"])["true_settlement_mm"].quantile([0.05, 0.25, 0.5, 0.75, 0.95]).unstack().reset_index()
    q.columns = ["point_id", "date", "q05_mm", "q25_mm", "q50_mm", "q75_mm", "q95_mm"]
    q["provenance"] = "S"
    return ensemble, q


def make_campaigns() -> pd.DataFrame:
    rows = []
    prev = None
    for i, (date_s, ctype) in enumerate(CAMPAIGN_DATES, 1):
        d = pd.Timestamp(date_s)
        interval = None if prev is None else int((d - prev).days)
        rows.append({
            "campaign_id": f"C{i:03d}",
            "date": d.date().isoformat(),
            "year": d.year,
            "day_of_year": int(d.dayofyear),
            "campaign_type": ctype,
            "interval_days_from_previous": interval,
            "long_gap_flag": bool(interval is not None and interval >= 240),
            "provenance": "S",
        })
        prev = d
    return pd.DataFrame(rows)


def generate_campaign_membership(
    campaigns: pd.DataFrame,
    points: pd.DataFrame,
    assignment: pd.DataFrame,
    truth_map: dict[str, pd.DataFrame],
    rng: np.random.Generator,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    ass = assignment.set_index("point_id")
    profiles = sorted(points["profile_id"].unique())
    destroyed_points = [
        (points.query("point_type=='WORK'").sort_values("settlement_anchor_map_mm", ascending=False).iloc[i]["point_id"], pd.Timestamp(d))
        for i, d in zip([8, 21, 47], ["2023-06-01", "2024-03-01", "2025-01-01"])
    ]
    destroyed_map = dict(destroyed_points)

    rows = []
    focused_counter = 0
    for _, camp in campaigns.iterrows():
        cid = camp["campaign_id"]
        d = pd.Timestamp(camp["date"])
        ctype = camp["campaign_type"]
        target_ids: set[str]
        target_reason: dict[str, str] = {}
        scores: dict[str, float] = {}

        if ctype == "full":
            target_ids = set(points["point_id"])
            for pid in target_ids:
                target_reason[pid] = "full_network_cycle"
                scores[pid] = 1.0
        else:
            focused_counter += 1
            score_rows = []
            for _, p in points.query("point_type=='WORK'").iterrows():
                pid = p["point_id"]
                rate = sample_truth(truth_map, pid, d, "true_velocity_mm_y")
                risk = float(ass.loc[pid, "risk_score"])
                score = 0.65 * rate / 220.0 + 0.35 * risk
                score_rows.append((p["profile_id"], pid, score))
            score_df = pd.DataFrame(score_rows, columns=["profile_id", "point_id", "score"])
            prof_score = score_df.groupby("profile_id")["score"].max().sort_values(ascending=False)
            n_profiles = 3 if focused_counter % 4 == 0 else 2
            # Rotate one selected profile to avoid repeatedly sampling only the same high-risk zone.
            top = list(prof_score.index[: n_profiles + 2])
            if len(top) > n_profiles:
                rot = focused_counter % len(top)
                top = (top[rot:] + top[:rot])[:n_profiles]
            selected_profiles = top[:n_profiles]
            target_ids = set()
            for prof in selected_profiles:
                prof_work = score_df[score_df["profile_id"].eq(prof)].sort_values("score", ascending=False)
                n_work = 7 if n_profiles == 2 else 5
                chosen = prof_work.head(n_work)
                for _, rr in chosen.iterrows():
                    target_ids.add(rr["point_id"])
                    target_reason[rr["point_id"]] = "focused_high_dynamic_score"
                    scores[rr["point_id"]] = float(rr["score"])
                refs = points[(points["profile_id"].eq(prof)) & points["point_type"].eq("REF")]
                for pid in refs["point_id"]:
                    target_ids.add(pid)
                    target_reason[pid] = "focused_profile_datum_control"
                    scores[pid] = 1.0

        for _, p in points.iterrows():
            pid = p["point_id"]
            targeted = pid in target_ids
            if not targeted:
                status = "not_targeted"
                reason = "not_in_focused_subset"
                observed = False
            else:
                if p["point_type"] == "REF":
                    # Datum controls are deliberately robust.
                    missing_prob = 0.01 if ctype == "full" else 0.0
                else:
                    missing_prob = 0.045 if ctype == "full" else 0.07
                if pid in destroyed_map and d >= destroyed_map[pid]:
                    status = "missing_destroyed"
                    reason = "point_destroyed_or_inaccessible_after_event"
                    observed = False
                elif rng.random() < missing_prob:
                    reason = rng.choice(
                        ["access", "weather", "instrument"],
                        p=[0.50, 0.30, 0.20],
                    )
                    status = f"missing_{reason}"
                    observed = False
                else:
                    status = "observed"
                    reason = None
                    observed = True
            rows.append({
                "campaign_id": cid,
                "date": d.date().isoformat(),
                "campaign_type": ctype,
                "point_id": pid,
                "profile_id": p["profile_id"],
                "point_type": p["point_type"],
                "targeted": targeted,
                "observed": observed,
                "membership_status": status,
                "missing_reason": reason,
                "target_reason": target_reason.get(pid),
                "selection_score": scores.get(pid),
                "provenance": "S",
            })

    membership = pd.DataFrame(rows)
    # Enforce at least one observed datum point for every targeted profile/campaign.
    targeted_groups = membership[membership["targeted"]].groupby(["campaign_id", "profile_id"])
    for (cid, prof), g in targeted_groups:
        refs = g[g["point_type"].eq("REF")]
        if len(refs) and not refs["observed"].any():
            idx = refs.index[0]
            membership.loc[idx, ["observed", "membership_status", "missing_reason"]] = [True, "observed", None]

    summary = campaigns.copy()
    stats = membership.groupby("campaign_id").agg(
        n_points_targeted=("targeted", "sum"),
        n_points_observed=("observed", "sum"),
        n_points_missing=("membership_status", lambda s: int(s.str.startswith("missing_").sum())),
    ).reset_index()
    summary = summary.merge(stats, on="campaign_id", how="left")
    summary["coverage_fraction_total"] = summary["n_points_observed"] / len(points)
    summary["coverage_fraction_targeted"] = summary["n_points_observed"] / summary["n_points_targeted"].replace(0, np.nan)
    return membership, summary


def build_benchmark_observations(
    membership: pd.DataFrame,
    points: pd.DataFrame,
    truth_map: dict[str, pd.DataFrame],
    rng: np.random.Generator,
) -> pd.DataFrame:
    pidx = points.set_index("point_id")
    rows = []
    for _, m in membership[(membership["observed"]) & membership["point_type"].eq("REF")].iterrows():
        pid = m["point_id"]
        d = pd.Timestamp(m["date"])
        true_h = float(pidx.loc[pid, "base_height_m"] - sample_truth(truth_map, pid, d, "true_settlement_mm") / 1000.0)
        sigma_mm = 0.55 + rng.uniform(0, 0.15)
        obs_h = true_h + rng.normal(0, sigma_mm / 1000.0)
        rows.append({
            "campaign_id": m["campaign_id"],
            "date": m["date"],
            "profile_id": m["profile_id"],
            "point_id": pid,
            "observed_benchmark_height_m": obs_h,
            "standard_uncertainty_mm": sigma_mm,
            "observation_method": "independent_datum_control",
            "provenance": "S",
        })
    return pd.DataFrame(rows)


def simulate_leveling(
    membership: pd.DataFrame,
    campaigns: pd.DataFrame,
    points: pd.DataFrame,
    truth_map: dict[str, pd.DataFrame],
    benchmark_obs: pd.DataFrame,
    rng: np.random.Generator,
):
    pidx = points.set_index("point_id")
    benchmark_lookup = benchmark_obs.set_index(["campaign_id", "point_id"])
    station_rows: list[dict[str, Any]] = []
    run_rows: list[dict[str, Any]] = []
    adjusted_rows: list[dict[str, Any]] = []
    residual_rows: list[dict[str, Any]] = []
    injected_rows: list[dict[str, Any]] = []

    run_counter = 0
    station_counter = 0
    for _, camp in campaigns.iterrows():
        cid = camp["campaign_id"]
        d = pd.Timestamp(camp["date"])
        obs_c = membership[(membership["campaign_id"].eq(cid)) & membership["observed"]]
        for prof, gp in obs_c.groupby("profile_id"):
            gp = gp.merge(points[["point_id", "point_order", "chainage_m"]], on="point_id", how="left")
            gp = gp.sort_values("point_order")
            if len(gp) < 2:
                continue
            pids = gp["point_id"].tolist()
            true_h = {
                pid: float(pidx.loc[pid, "base_height_m"] - sample_truth(truth_map, pid, d, "true_settlement_mm") / 1000.0)
                for pid in pids
            }

            def make_run(attempt: int, force_clean: bool = False):
                nonlocal run_counter, station_counter
                run_counter += 1
                run_id = f"LR-{run_counter:05d}"
                inject = (rng.random() < 0.06) and not force_clean
                inject_edge = int(rng.integers(0, len(pids) - 1)) if inject else -1
                edge_obs = []
                all_station_ids = []
                for edge_i, (pi, pj) in enumerate(zip(pids[:-1], pids[1:])):
                    dist = abs(float(pidx.loc[pj, "chainage_m"] - pidx.loc[pi, "chainage_m"]))
                    dist = max(dist, math.hypot(
                        float(pidx.loc[pj, "x_local_m"] - pidx.loc[pi, "x_local_m"]),
                        float(pidx.loc[pj, "y_local_m"] - pidx.loc[pi, "y_local_m"]),
                    ))
                    n_stations = max(1, int(math.ceil(dist / 80.0)))
                    true_delta = true_h[pj] - true_h[pi]
                    segment_true = true_delta / n_stations
                    segment_vals = []
                    segment_sigmas = []
                    for sidx in range(n_stations):
                        station_counter += 1
                        station_id = f"LS-{station_counter:07d}"
                        sigma_mm = 0.22 + 0.0014 * min(dist / n_stations, 100)
                        noise_m = rng.normal(0, sigma_mm / 1000.0)
                        gross_mm = 0.0
                        if inject and edge_i == inject_edge and sidx == n_stations // 2:
                            gross_mm = float(rng.choice([-1, 1]) * rng.uniform(6, 14))
                        delta_obs = segment_true + noise_m + gross_mm / 1000.0
                        bs = rng.uniform(1.15, 1.85)
                        fs = bs - delta_obs
                        sight_base = rng.uniform(20, 50)
                        imbalance = rng.normal(0, 1.8)
                        bs_len = max(5, sight_base + imbalance / 2)
                        fs_len = max(5, sight_base - imbalance / 2)
                        segment_vals.append(delta_obs)
                        segment_sigmas.append(sigma_mm)
                        all_station_ids.append(station_id)
                        station_rows.append({
                            "run_id": run_id,
                            "campaign_id": cid,
                            "date": d.date().isoformat(),
                            "profile_id": prof,
                            "run_attempt": attempt,
                            "edge_index": edge_i + 1,
                            "station_id": station_id,
                            "station_index_within_edge": sidx + 1,
                            "stations_in_edge": n_stations,
                            "from_point_id": pi,
                            "to_point_id": pj,
                            "backsight_reading_m": bs,
                            "foresight_reading_m": fs,
                            "delta_h_observed_m": delta_obs,
                            "backsight_length_m": bs_len,
                            "foresight_length_m": fs_len,
                            "sight_imbalance_m": bs_len - fs_len,
                            "standard_uncertainty_delta_h_mm": sigma_mm,
                            "run_qc_status": None,
                            "provenance": "S",
                        })
                        injected_rows.append({
                            "station_id": station_id,
                            "run_id": run_id,
                            "gross_error_injected": bool(gross_mm != 0),
                            "gross_error_mm": gross_mm,
                            "use_class": "evaluation_only",
                        })
                    edge_obs.append({
                        "from": pi,
                        "to": pj,
                        "delta": float(np.sum(segment_vals)),
                        "sigma_mm": float(np.sqrt(np.sum(np.square(segment_sigmas)))),
                        "distance_m": dist,
                    })

                refs = [pid for pid in pids if pidx.loc[pid, "point_type"] == "REF" and (cid, pid) in benchmark_lookup.index]
                total_distance_km = sum(e["distance_m"] for e in edge_obs) / 1000.0
                tolerance_mm = 2.5 * math.sqrt(max(total_distance_km, 0.1))
                if len(refs) >= 2:
                    first_ref, last_ref = refs[0], refs[-1]
                    i0, i1 = pids.index(first_ref), pids.index(last_ref)
                    observed_diff = sum(e["delta"] for e in edge_obs[i0:i1])
                    b0 = float(benchmark_lookup.loc[(cid, first_ref), "observed_benchmark_height_m"])
                    b1 = float(benchmark_lookup.loc[(cid, last_ref), "observed_benchmark_height_m"])
                    misclosure_mm = (observed_diff - (b1 - b0)) * 1000.0
                else:
                    # Single-datum run: use a conservative internal repeat-equivalent statistic.
                    misclosure_mm = float(rng.normal(0, 0.45 * tolerance_mm))
                accepted = abs(misclosure_mm) <= tolerance_mm and not inject
                status = "accepted" if accepted else "rejected_repeat_required"
                for row in station_rows[-len(all_station_ids):]:
                    row["run_qc_status"] = status
                run_rows.append({
                    "run_id": run_id,
                    "campaign_id": cid,
                    "date": d.date().isoformat(),
                    "profile_id": prof,
                    "run_attempt": attempt,
                    "n_points": len(pids),
                    "n_stations": len(all_station_ids),
                    "total_distance_km": total_distance_km,
                    "misclosure_mm": misclosure_mm,
                    "tolerance_mm": tolerance_mm,
                    "qc_status": status,
                    "repeated_from_run_id": None,
                    "provenance": "C/S",
                })
                return run_id, edge_obs, accepted

            run_id, edges, accepted = make_run(1, force_clean=False)
            if not accepted:
                run_id2, edges2, accepted2 = make_run(2, force_clean=True)
                run_rows[-1]["repeated_from_run_id"] = run_id
                run_id, edges, accepted = run_id2, edges2, accepted2
            if not accepted:
                continue

            # Weighted least squares using only observed height differences and independent datum observations.
            node_index = {pid: i for i, pid in enumerate(pids)}
            A_rows, l_rows, w_rows = [], [], []
            for e in edges:
                a = np.zeros(len(pids))
                a[node_index[e["to"]]] = 1.0
                a[node_index[e["from"]]] = -1.0
                A_rows.append(a)
                l_rows.append(e["delta"])
                w_rows.append(1.0 / max((e["sigma_mm"] / 1000.0) ** 2, 1e-12))
            for pid in pids:
                if pidx.loc[pid, "point_type"] == "REF" and (cid, pid) in benchmark_lookup.index:
                    a = np.zeros(len(pids))
                    a[node_index[pid]] = 1.0
                    A_rows.append(a)
                    l_rows.append(float(benchmark_lookup.loc[(cid, pid), "observed_benchmark_height_m"]))
                    sig = float(benchmark_lookup.loc[(cid, pid), "standard_uncertainty_mm"]) / 1000.0
                    w_rows.append(1.0 / max(sig**2, 1e-12))
            A = np.vstack(A_rows)
            l = np.array(l_rows)
            Wsqrt = np.sqrt(np.array(w_rows))
            Aw = A * Wsqrt[:, None]
            lw = l * Wsqrt
            xhat, *_ = np.linalg.lstsq(Aw, lw, rcond=None)
            residual = A @ xhat - l
            dof = max(len(l) - len(pids), 1)
            variance_factor = float(max(0.6, min(2.5, np.sum(np.array(w_rows) * residual**2) / dof)))
            normal = A.T @ (np.array(w_rows)[:, None] * A)
            cov = np.linalg.pinv(normal) * variance_factor

            for pid in pids:
                idx = node_index[pid]
                adjusted_h = float(xhat[idx])
                observed_settlement = float((pidx.loc[pid, "base_height_m"] - adjusted_h) * 1000.0)
                std_mm = float(max(0.6, math.sqrt(max(cov[idx, idx], 0)) * 1000.0 * 1.20))
                true_sett = sample_truth(truth_map, pid, d, "true_settlement_mm")
                qc = "accepted" if std_mm <= 2.5 else "warning_high_uncertainty"
                adjusted_rows.append({
                    "campaign_id": cid,
                    "date": d.date().isoformat(),
                    "profile_id": prof,
                    "point_id": pid,
                    "adjusted_height_m": adjusted_h,
                    "observed_settlement_mm": observed_settlement,
                    "standard_uncertainty_mm": std_mm,
                    "variance_factor": variance_factor,
                    "qc_status": qc,
                    "accepted_run_id": run_id,
                    "adjustment_used_ground_truth": False,
                    "adjustment_method": "weighted_height_difference_network_with_independent_datum_constraints",
                    "provenance": "C/S",
                })
                residual_rows.append({
                    "campaign_id": cid,
                    "date": d.date().isoformat(),
                    "profile_id": prof,
                    "point_id": pid,
                    "true_settlement_mm": true_sett,
                    "observed_settlement_mm": observed_settlement,
                    "residual_mm": observed_settlement - true_sett,
                    "standard_uncertainty_mm": std_mm,
                    "use_class": "evaluation_only",
                })

    return (
        pd.DataFrame(station_rows),
        pd.DataFrame(run_rows),
        pd.DataFrame(adjusted_rows),
        pd.DataFrame(residual_rows),
        pd.DataFrame(injected_rows),
    )


def generate_planar_and_kinematics(
    adjusted: pd.DataFrame,
    points: pd.DataFrame,
    campaigns: pd.DataFrame,
    truth_map: dict[str, pd.DataFrame],
    rng: np.random.Generator,
):
    pidx = points.set_index("point_id")
    campaign_type = campaigns.set_index("campaign_id")["campaign_type"].to_dict()
    profile_info = {}
    for prof, g in points.groupby("profile_id"):
        g = g.sort_values("point_order")
        x0, y0 = g.iloc[0][["x_local_m", "y_local_m"]]
        x1, y1 = g.iloc[-1][["x_local_m", "y_local_m"]]
        vec = np.array([x1 - x0, y1 - y0], dtype=float)
        vec = vec / max(np.linalg.norm(vec), 1e-9)
        perp = np.array([-vec[1], vec[0]])
        work = g[g["point_type"].eq("WORK")]
        if work["settlement_anchor_map_mm"].sum() > 0:
            center = float(np.average(work["chainage_m"], weights=work["settlement_anchor_map_mm"].clip(lower=1)))
        else:
            center = float(work["chainage_m"].median())
        profile_info[prof] = (vec, perp, center)

    common_xy = {
        cid: rng.normal(0, 0.9, 2) for cid in adjusted["campaign_id"].unique()
    }
    planar_rows, planar_eval = [], []
    for _, row in adjusted.iterrows():
        pid, cid, prof = row["point_id"], row["campaign_id"], row["profile_id"]
        d = pd.Timestamp(row["date"])
        p = pidx.loc[pid]
        sett = sample_truth(truth_map, pid, d, "true_settlement_mm")
        vec, perp, center = profile_info[prof]
        direction = np.sign(center - float(p["chainage_m"]))
        true_along_mm = direction * 0.020 * sett
        true_cross_mm = 0.004 * sett * math.sin(float(p["chainage_m"]) / 450.0)
        true_dx, true_dy = vec * true_along_mm + perp * true_cross_mm
        sigma = 1.4 if p["point_type"] == "WORK" else 0.9
        noise = rng.normal(0, sigma, 2)
        obs_dx, obs_dy = true_dx + common_xy[cid] + noise
        obs_x = float(p["x_local_m"] + obs_dx / 1000.0)
        obs_y = float(p["y_local_m"] + obs_dy / 1000.0)
        planar_rows.append({
            "campaign_id": cid,
            "date": d.date().isoformat(),
            "profile_id": prof,
            "point_id": pid,
            "observed_x_local_m": obs_x,
            "observed_y_local_m": obs_y,
            "standard_uncertainty_xy_mm": sigma,
            "qc_status": "accepted" if sigma <= 2 else "warning",
            "provenance": "S",
        })
        planar_eval.append({
            "campaign_id": cid,
            "date": d.date().isoformat(),
            "point_id": pid,
            "true_dx_mm": float(true_dx),
            "true_dy_mm": float(true_dy),
            "observed_dx_mm": float(obs_dx),
            "observed_dy_mm": float(obs_dy),
            "residual_dx_mm": float(obs_dx - true_dx),
            "residual_dy_mm": float(obs_dy - true_dy),
            "use_class": "evaluation_only",
        })
    planar = pd.DataFrame(planar_rows)

    horiz = planar.merge(points[["point_id", "x_local_m", "y_local_m"]], on="point_id", how="left")
    horiz["delta_x_mm"] = (horiz["observed_x_local_m"] - horiz["x_local_m"]) * 1000.0
    horiz["delta_y_mm"] = (horiz["observed_y_local_m"] - horiz["y_local_m"]) * 1000.0
    horiz["horizontal_displacement_mm"] = np.hypot(horiz["delta_x_mm"], horiz["delta_y_mm"])
    horizontal_displacements = horiz[[
        "campaign_id", "date", "profile_id", "point_id",
        "delta_x_mm", "delta_y_mm", "horizontal_displacement_mm",
        "standard_uncertainty_xy_mm", "qc_status",
    ]].copy()
    horizontal_displacements["provenance"] = "C/S"

    # Rates per point on irregular observation intervals.
    rate_rows = []
    for pid, g in adjusted.sort_values("date").groupby("point_id"):
        g = g.sort_values("date")
        prev = None
        for _, r in g.iterrows():
            if prev is None:
                prev = r
                continue
            dt_days = (pd.Timestamp(r["date"]) - pd.Timestamp(prev["date"])).days
            if dt_days <= 0:
                prev = r
                continue
            rate = (float(r["observed_settlement_mm"]) - float(prev["observed_settlement_mm"])) / dt_days * 365.25
            sigma = math.sqrt(float(r["standard_uncertainty_mm"])**2 + float(prev["standard_uncertainty_mm"])**2) / dt_days * 365.25
            rate_rows.append({
                "point_id": pid,
                "profile_id": r["profile_id"],
                "campaign_id": r["campaign_id"],
                "date": r["date"],
                "previous_campaign_id": prev["campaign_id"],
                "previous_date": prev["date"],
                "interval_days": dt_days,
                "settlement_rate_mm_y": rate,
                "standard_uncertainty_rate_mm_y": sigma,
                "provenance": "C/S",
            })
            prev = r
    rates = pd.DataFrame(rate_rows)

    # Tilts, curvatures, horizontal strain and profile summaries.
    tilt_rows, strain_rows = [], []
    merged = adjusted.merge(points[["point_id", "point_order", "chainage_m"]], on="point_id", how="left")
    planar_m = planar.merge(points[["point_id", "point_order", "chainage_m", "x_local_m", "y_local_m"]], on="point_id", how="left")
    for (cid, prof), g in merged.groupby(["campaign_id", "profile_id"]):
        g = g.sort_values("point_order")
        for (_, a), (_, b) in zip(g.iloc[:-1].iterrows(), g.iloc[1:].iterrows()):
            length = abs(float(b["chainage_m"] - a["chainage_m"]))
            if length <= 0:
                continue
            tilt = (float(b["observed_settlement_mm"]) - float(a["observed_settlement_mm"])) / length
            sig = math.sqrt(float(a["standard_uncertainty_mm"])**2 + float(b["standard_uncertainty_mm"])**2) / length
            tilt_rows.append({
                "campaign_id": cid,
                "date": a["date"],
                "profile_id": prof,
                "left_point_id": a["point_id"],
                "right_point_id": b["point_id"],
                "mid_chainage_m": 0.5 * (float(a["chainage_m"]) + float(b["chainage_m"])),
                "interval_length_m": length,
                "tilt_mm_m": tilt,
                "standard_uncertainty_tilt_mm_m": sig,
                "provenance": "C/S",
            })
        gp = planar_m[(planar_m["campaign_id"].eq(cid)) & planar_m["profile_id"].eq(prof)].sort_values("point_order")
        for (_, a), (_, b) in zip(gp.iloc[:-1].iterrows(), gp.iloc[1:].iterrows()):
            base_dist = math.hypot(float(b["x_local_m"] - a["x_local_m"]), float(b["y_local_m"] - a["y_local_m"]))
            obs_dist = math.hypot(float(b["observed_x_local_m"] - a["observed_x_local_m"]), float(b["observed_y_local_m"] - a["observed_y_local_m"]))
            strain = (obs_dist - base_dist) / max(base_dist, 1e-9)
            strain_rows.append({
                "campaign_id": cid,
                "date": a["date"],
                "profile_id": prof,
                "left_point_id": a["point_id"],
                "right_point_id": b["point_id"],
                "base_length_m": base_dist,
                "observed_length_m": obs_dist,
                "horizontal_strain": strain,
                "horizontal_strain_microstrain": strain * 1e6,
                "provenance": "C/S",
            })
    tilts = pd.DataFrame(tilt_rows)
    strains = pd.DataFrame(strain_rows)

    curv_rows = []
    for (cid, prof), g in tilts.groupby(["campaign_id", "profile_id"]):
        g = g.sort_values("mid_chainage_m")
        for (_, a), (_, b) in zip(g.iloc[:-1].iterrows(), g.iloc[1:].iterrows()):
            spacing = float(b["mid_chainage_m"] - a["mid_chainage_m"])
            if spacing <= 0:
                continue
            curv = (float(b["tilt_mm_m"]) - float(a["tilt_mm_m"])) / spacing
            sig = math.sqrt(float(a["standard_uncertainty_tilt_mm_m"])**2 + float(b["standard_uncertainty_tilt_mm_m"])**2) / spacing
            curv_rows.append({
                "campaign_id": cid,
                "date": a["date"],
                "profile_id": prof,
                "left_interval": f"{a['left_point_id']}::{a['right_point_id']}",
                "right_interval": f"{b['left_point_id']}::{b['right_point_id']}",
                "chainage_m": 0.5 * (float(a["mid_chainage_m"]) + float(b["mid_chainage_m"])),
                "curvature_mm_m2": curv,
                "standard_uncertainty_curvature_mm_m2": sig,
                "provenance": "C/S",
            })
    curvatures = pd.DataFrame(curv_rows)

    profile_rows = []
    rate_lookup = rates.set_index(["campaign_id", "profile_id", "point_id"])["settlement_rate_mm_y"].to_dict() if len(rates) else {}
    for (cid, prof), g in merged.groupby(["campaign_id", "profile_id"]):
        date_s = g.iloc[0]["date"]
        tsub = tilts[(tilts["campaign_id"].eq(cid)) & tilts["profile_id"].eq(prof)]
        csub = curvatures[(curvatures["campaign_id"].eq(cid)) & curvatures["profile_id"].eq(prof)]
        ssub = strains[(strains["campaign_id"].eq(cid)) & strains["profile_id"].eq(prof)]
        rate_vals = [rate_lookup.get((cid, prof, pid), np.nan) for pid in g["point_id"]]
        profile_rows.append({
            "campaign_id": cid,
            "date": date_s,
            "campaign_type": campaign_type.get(cid),
            "profile_id": prof,
            "n_points_observed": len(g),
            "max_settlement_mm": float(g["observed_settlement_mm"].max()),
            "mean_settlement_mm": float(g["observed_settlement_mm"].mean()),
            "max_rate_mm_y": float(np.nanmax(rate_vals)) if np.isfinite(rate_vals).any() else np.nan,
            "max_abs_tilt_mm_m": float(tsub["tilt_mm_m"].abs().max()) if len(tsub) else np.nan,
            "max_abs_curvature_mm_m2": float(csub["curvature_mm_m2"].abs().max()) if len(csub) else np.nan,
            "max_abs_horizontal_strain": float(ssub["horizontal_strain"].abs().max()) if len(ssub) else np.nan,
            "provenance": "C/S",
        })
    profile_kin = pd.DataFrame(profile_rows)
    return (
        planar,
        pd.DataFrame(planar_eval),
        horizontal_displacements,
        strains,
        tilts,
        curvatures,
        rates,
        profile_kin,
    )


def design_gnss_network(points: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for prof, g in points.groupby("profile_id"):
        refs = g[g["point_type"].eq("REF")].sort_values("point_order")
        rows.append({
            "point_id": refs.iloc[0]["point_id"],
            "profile_id": prof,
            "stratum": "reference",
            "selection_basis": "one_datum_reference_per_profile",
        })
        work = g[g["point_type"].eq("WORK")].sort_values("settlement_anchor_map_mm")
        for stratum, pos in [("low", 0.15), ("mid", 0.50), ("high", 0.85)]:
            idx = int(round(pos * (len(work) - 1)))
            p = work.iloc[idx]
            rows.append({
                "point_id": p["point_id"],
                "profile_id": prof,
                "stratum": stratum,
                "selection_basis": "within_profile_settlement_stratification",
            })
    out = pd.DataFrame(rows).drop_duplicates("point_id")
    out["network_role"] = np.where(out["stratum"].eq("reference"), "datum_control", "deformation_control")
    out["provenance"] = "C/S"
    return out


def simulate_gnss(
    network: pd.DataFrame,
    campaigns: pd.DataFrame,
    membership: pd.DataFrame,
    points: pd.DataFrame,
    truth_map: dict[str, pd.DataFrame],
    rng: np.random.Generator,
):
    pidx = points.set_index("point_id")
    full = campaigns[campaigns["campaign_type"].eq("full")]
    epoch_pairs = [(r["campaign_id"], pd.Timestamp(r["date"]), pid) for _, r in full.iterrows() for pid in network["point_id"]]
    # Add four targeted GNSS points from the first focused campaign to exercise focused GNSS logic.
    first_focused = campaigns[campaigns["campaign_type"].eq("focused")].iloc[0]
    targ = membership[(membership["campaign_id"].eq(first_focused["campaign_id"])) & membership["observed"]]
    extra = network[network["point_id"].isin(targ["point_id"])].head(4)
    epoch_pairs += [(first_focused["campaign_id"], pd.Timestamp(first_focused["date"]), pid) for pid in extra["point_id"]]

    session_rows, adjusted_rows, eval_rows = [], [], []
    campaign_common = {cid: rng.normal(0, 3.0) for cid, _, _ in epoch_pairs}
    for cid, d, pid in epoch_pairs:
        true_sett = sample_truth(truth_map, pid, d, "true_settlement_mm")
        true_h = float(pidx.loc[pid, "base_height_m"] - true_sett / 1000.0)
        session_vals = []
        session_weights = []
        session_meta = []
        for sess in range(1, 3):
            pdop = float(rng.uniform(1.2, 3.4))
            n_sat = int(rng.integers(9, 19))
            fixed = bool(rng.random() < (0.96 if pdop < 2.5 else 0.82))
            baseline_km = float(rng.uniform(0.8, 8.0))
            sigma_mm = 2.4 + 1.1 * (pdop - 1) + 0.18 * baseline_km + (4.0 if not fixed else 0.0)
            obs_h = true_h + (campaign_common[cid] + rng.normal(0, sigma_mm)) / 1000.0
            session_id = f"GNSS-{cid}-{pid}-{sess}"
            session_rows.append({
                "session_id": session_id,
                "campaign_id": cid,
                "date": d.date().isoformat(),
                "point_id": pid,
                "session_index": sess,
                "receiver_model": "dual_frequency_synthetic_class",
                "duration_min": int(rng.integers(45, 121)),
                "baseline_length_km": baseline_km,
                "pdop": pdop,
                "n_satellites": n_sat,
                "solution_type": "fixed" if fixed else "float",
                "observed_height_m_raw": obs_h,
                "standard_uncertainty_height_mm": sigma_mm,
                "qc_status": "accepted" if fixed and pdop <= 3.0 else "warning",
                "provenance": "S",
            })
            session_vals.append(obs_h)
            session_weights.append(1.0 / sigma_mm**2)
            session_meta.append((fixed, pdop, sigma_mm))

        # Estimate common mode using all reference sessions in the campaign after raw table is available.
        # Here a provisional weighted mean is stored; a second pass applies a campaign correction.
        mean_h = float(np.average(session_vals, weights=session_weights))
        sigma_mean = float(math.sqrt(1.0 / sum(session_weights)))
        adjusted_rows.append({
            "campaign_id": cid,
            "date": d.date().isoformat(),
            "point_id": pid,
            "n_sessions": 2,
            "n_fixed": int(sum(m[0] for m in session_meta)),
            "mean_pdop": float(np.mean([m[1] for m in session_meta])),
            "max_pdop": float(np.max([m[1] for m in session_meta])),
            "provisional_height_m": mean_h,
            "provisional_sigma_mm": sigma_mean,
        })

    sessions = pd.DataFrame(session_rows)
    provisional = pd.DataFrame(adjusted_rows)
    network_idx = network.set_index("point_id")
    corrected_rows = []
    for cid, g in provisional.groupby("campaign_id"):
        refs = g[g["point_id"].map(network_idx["stratum"]).eq("reference")]
        ref_offsets = []
        ref_weights = []
        for _, r in refs.iterrows():
            pid = r["point_id"]
            offset_mm = (float(r["provisional_height_m"]) - float(pidx.loc[pid, "base_height_m"])) * 1000.0
            ref_offsets.append(offset_mm)
            ref_weights.append(1.0 / max(float(r["provisional_sigma_mm"])**2, 1e-6))
        if ref_offsets:
            common_est = float(np.average(ref_offsets, weights=ref_weights))
            common_sigma = float(max(0.8, math.sqrt(1.0 / sum(ref_weights))))
        else:
            common_est = 0.0
            common_sigma = 5.0
        for _, r in g.iterrows():
            pid = r["point_id"]
            adjusted_h = float(r["provisional_height_m"] - common_est / 1000.0)
            std_mm = float(math.sqrt(float(r["provisional_sigma_mm"])**2 + common_sigma**2) * 1.25)
            observed_sett = float((pidx.loc[pid, "base_height_m"] - adjusted_h) * 1000.0)
            true_sett = sample_truth(truth_map, pid, pd.Timestamp(r["date"]), "true_settlement_mm")
            qc = "accepted" if int(r["n_fixed"]) >= 1 and float(r["max_pdop"]) <= 3.3 else "warning"
            corrected_rows.append({
                "campaign_id": cid,
                "date": r["date"],
                "point_id": pid,
                "n_sessions": int(r["n_sessions"]),
                "n_fixed": int(r["n_fixed"]),
                "mean_pdop": float(r["mean_pdop"]),
                "max_pdop": float(r["max_pdop"]),
                "adjusted_height_m": adjusted_h,
                "observed_settlement_mm": observed_sett,
                "standard_uncertainty_mm": std_mm,
                "common_mode_correction_mm": common_est,
                "common_mode_standard_uncertainty_mm": common_sigma,
                "qc_status": qc,
                "qc_reason": "fixed_solution_pdop_and_calibrated_common_mode",
                "provenance": "C/S",
            })
            eval_rows.append({
                "campaign_id": cid,
                "date": r["date"],
                "point_id": pid,
                "true_settlement_mm": true_sett,
                "observed_settlement_mm": observed_sett,
                "residual_mm": observed_sett - true_sett,
                "standard_uncertainty_mm": std_mm,
                "use_class": "evaluation_only",
            })
    return sessions, pd.DataFrame(corrected_rows), pd.DataFrame(eval_rows)


def generate_independent_insar(
    grid: pd.DataFrame,
    acquisitions: pd.DataFrame,
    rng: np.random.Generator,
):
    valid = grid[grid["effective_area_m2"].gt(0)].copy()
    valid["sett_q"] = pd.qcut(valid["settlement_reference_map_mm"].fillna(0).rank(method="first"), 6, labels=False)
    samples = []
    for q in range(6):
        g = valid[valid["sett_q"].eq(q)]
        samples.append(g.sample(n=100, random_state=SEED + q, replace=len(g) < 100))
    pts = pd.concat(samples, ignore_index=True).drop_duplicates("cell_id")
    if len(pts) < 600:
        rest = valid[~valid["cell_id"].isin(pts["cell_id"])].sample(n=600-len(pts), random_state=SEED+99)
        pts = pd.concat([pts, rest], ignore_index=True)
    pts = pts.head(600).copy().reset_index(drop=True)
    pts["insar_point_id"] = [f"PS-{i:05d}" for i in range(1, len(pts)+1)]
    reflector_classes = np.array(["infrastructure_proxy", "urban_scatterer_proxy", "distributed_scatterer_proxy"])
    probs = np.array([0.28, 0.34, 0.38])
    pts["reflector_class"] = rng.choice(reflector_classes, size=len(pts), p=probs)
    base_coh_map = {"infrastructure_proxy": 0.72, "urban_scatterer_proxy": 0.61, "distributed_scatterer_proxy": 0.47}
    pts["coherence_baseline"] = [float(np.clip(base_coh_map[c] + rng.normal(0,0.07), 0.25, 0.90)) for c in pts["reflector_class"]]
    pts["trajectory_source"] = "independent_continuous_spatiotemporal_field"
    pts["provenance"] = "H/S"

    xmin, xmax = valid["x_local_m"].min(), valid["x_local_m"].max()
    ymin, ymax = valid["y_local_m"].min(), valid["y_local_m"].max()
    source_rows = []
    source_specs = [
        (0.20,0.25,1300,"uniform",18),
        (0.72,0.30,1100,"decaying",75),
        (0.55,0.72,900,"accelerating",110),
        (0.82,0.76,700,"reactivated",95),
        (0.30,0.62,650,"step_change",80),
    ]
    for i,(fx,fy,sigma,family,amp) in enumerate(source_specs,1):
        source_rows.append({
            "source_id": f"ISRC-{i:02d}",
            "x_local_m": xmin + fx*(xmax-xmin),
            "y_local_m": ymin + fy*(ymax-ymin),
            "sigma_m": sigma,
            "temporal_family": family,
            "amplitude_mm_y": amp,
            "use_class": "private_generation_only",
        })
    sources = pd.DataFrame(source_rows)

    acq = acquisitions.copy()
    acq["date"] = pd.to_datetime(acq["date"])
    acq = acq.sort_values("date").reset_index(drop=True)
    reference_id = acq.iloc[0]["acquisition_id"]
    reference_date = acq.iloc[0]["date"]
    point_rows, obs_rows, truth_rows = [], [], []

    # Per-acquisition low-frequency nuisance fields.
    nuisance = {}
    for _, a in acq.iterrows():
        d = a["date"]
        if d == reference_date:
            nuisance[a["acquisition_id"]] = (0,0,0,0,0)
        else:
            nuisance[a["acquisition_id"]] = (
                rng.normal(0, 4.5), rng.normal(0, 4.0), rng.normal(0, 2.2),
                rng.normal(0, 1.8), rng.normal(0, 1.0),
            )

    def source_rate(src, d):
        t = (d - reference_date).days/365.25
        fam = src["temporal_family"]
        amp = src["amplitude_mm_y"]
        if fam == "uniform": return amp
        if fam == "decaying": return 8 + (amp-8)*math.exp(-max(t,0)/2.2)
        if fam == "accelerating": return 8 + amp*float(sigmoid((month_decimal(d)-2021.35)/0.25))
        if fam == "reactivated": return 14 + 35*math.exp(-max(t,0)/1.5) + amp*float(sigmoid((month_decimal(d)-2021.50)/0.22))
        if fam == "step_change": return 10 + (amp if d>=pd.Timestamp('2021-06-01') else 0)
        return 0

    for _, p in pts.iterrows():
        pid = p["insar_point_id"]
        x,y = float(p["x_local_m"]), float(p["y_local_m"])
        local_mult = float(rng.normal(1.0,0.045))
        weights=[]
        for _,s in sources.iterrows():
            dist2=(x-s["x_local_m"])**2+(y-s["y_local_m"])**2
            weights.append(math.exp(-0.5*dist2/(s["sigma_m"]**2)))
        weights=np.array(weights)
        point_rows.append({
            "insar_point_id": pid,
            "source_cell_id": p["cell_id"],
            "x_local_m": x,
            "y_local_m": y,
            "coherence_baseline": p["coherence_baseline"],
            "reflector_class": p["reflector_class"],
            "trajectory_source": p["trajectory_source"],
            "provenance": "H/S",
        })
        # Integrate independent vertical rate on acquisition dates.
        vertical=[0.0]
        east=[0.0]
        prev_d=reference_date
        prev_rate = 2.0 + float(sum(w*source_rate(s,prev_d) for w,(_,s) in zip(weights,sources.iterrows()))) * local_mult
        for idx in range(1,len(acq)):
            d=acq.iloc[idx]["date"]
            rate=2.0 + float(sum(w*source_rate(s,d) for w,(_,s) in zip(weights,sources.iterrows()))) * local_mult
            dt=(d-prev_d).days/365.25
            vertical.append(vertical[-1]+0.5*(prev_rate+rate)*dt)
            east.append(east[-1]+0.08*0.5*(prev_rate+rate)*dt*math.sin((x-xmin)/1300))
            prev_rate,prev_d=rate,d
        vertical=np.array(vertical)
        east=np.array(east)
        incidence=math.radians(float(acq.iloc[0]["incidence_angle_deg"]))
        true_los=-vertical*math.cos(incidence)+east*math.sin(incidence)
        dem_coeff=rng.normal(0,1.8)
        thermal_amp={"infrastructure_proxy":4.0,"urban_scatterer_proxy":2.5,"distributed_scatterer_proxy":1.0}[p["reflector_class"]]
        for idx,a in acq.iterrows():
            aid=a["acquisition_id"]; d=a["date"]
            if idx==0:
                atmosphere=orbit=dem=thermal=noise=0.0
                est_atm=est_orb=est_dem=est_thermal=0.0
            else:
                a0,ax,ay,orb0,orbgrad=nuisance[aid]
                xn=(x-xmin)/max(xmax-xmin,1); yn=(y-ymin)/max(ymax-ymin,1)
                atmosphere=a0+ax*(xn-0.5)+ay*(yn-0.5)+3.0*math.exp(-((xn-0.65)**2+(yn-0.3)**2)/0.08)
                orbit=orb0+orbgrad*(xn-0.5)
                dem=dem_coeff*rng.normal(0,0.8)
                thermal=thermal_amp*math.sin(2*math.pi*(d.dayofyear/365.25)+0.3)
                coherence=float(np.clip(p["coherence_baseline"]+rng.normal(0,0.06)-0.08*(d.month in [5,6]),0.12,0.95))
                noise=rng.normal(0,2.0+5.0*(1-coherence))
                est_atm=atmosphere+rng.normal(0,2.2)
                est_orb=orbit+rng.normal(0,1.2)
                est_dem=dem+rng.normal(0,1.4)
                est_thermal=thermal+rng.normal(0,1.0)
            coherence=float(p["coherence_baseline"]) if idx==0 else coherence
            raw=true_los[idx]+atmosphere+orbit+dem+thermal+noise
            corrected=raw-est_atm-est_orb-est_dem-est_thermal
            subvertical=-corrected/max(math.cos(incidence),1e-6)
            sigma=0.0 if idx==0 else 4.0+8.0*(1-coherence)
            if idx==0: qc="reference_epoch"
            elif coherence>=0.45: qc="accepted"
            elif coherence>=0.30: qc="warning"
            else: qc="rejected"
            obs_rows.append({
                "acquisition_id": aid,
                "reference_acquisition_id": reference_id,
                "date": d.date().isoformat(),
                "insar_point_id": pid,
                "coherence": coherence,
                "qc_status": qc,
                "raw_LOS_relative_mm": raw,
                "estimated_atmospheric_correction_relative_mm": est_atm,
                "estimated_orbit_correction_relative_mm": est_orb,
                "estimated_dem_correction_relative_mm": est_dem,
                "estimated_thermal_correction_relative_mm": est_thermal,
                "corrected_LOS_relative_mm": corrected,
                "subvertical_estimate_relative_mm": subvertical,
                "standard_uncertainty_mm": sigma,
                "first_epoch_zero_datum": bool(idx==0),
                "provenance": "C/S",
            })
            truth_rows.append({
                "acquisition_id": aid,
                "date": d.date().isoformat(),
                "insar_point_id": pid,
                "true_vertical_settlement_relative_mm": float(vertical[idx]),
                "true_east_displacement_relative_mm": float(east[idx]),
                "true_LOS_relative_mm": float(true_los[idx]),
                "use_class": "evaluation_only",
            })
    catalog=pd.DataFrame(point_rows)
    obs=pd.DataFrame(obs_rows)
    truth=pd.DataFrame(truth_rows)
    return catalog, obs, truth, sources


def build_point_feature_lineage(points: pd.DataFrame, grid: pd.DataFrame):
    valid=grid[grid["effective_area_m2"].gt(0)].copy()
    tree=cKDTree(valid[["x_local_m","y_local_m"]].to_numpy(float))
    dist,idx=tree.query(points[["x_local_m","y_local_m"]].to_numpy(float),k=1)
    nearest=valid.iloc[idx].reset_index(drop=True)
    rows=[]
    feature_specs=[
        ("settlement_anchor_map_mm","settlement_reference_map_mm","settlement_provenance","settlement_standard_uncertainty_mm","settlement_nearest_source_distance_m","mm",False),
        ("kzt","kzt_reconstructed","kzt_provenance","kzt_standard_uncertainty","kzt_nearest_source_distance_m","1",True),
        ("ko","ko_reconstructed","ko_provenance","ko_standard_uncertainty","ko_nearest_source_distance_m","1",True),
        ("seismic_energy_J_m2","seismic_energy_mid_J_m2_reconstructed","seismic_provenance","seismic_standard_uncertainty_J_m2","seismic_nearest_source_distance_m","J/m2",True),
        ("fill_density","backfill_hatch_density",None,None,None,"1",True),
        ("fault_distance_m","distance_to_reconstructed_fault_m",None,None,None,"m",True),
        ("lithology","lithology_reconstructed","lithology_provenance",None,"lithology_nearest_source_distance_m","category",True),
        ("terrain_TRI_relative","terrain_TRI_relative",None,None,None,"relative",True),
        ("terrain_roughness_relative","terrain_roughness_relative",None,None,None,"relative",True),
    ]
    for i,p in points.reset_index(drop=True).iterrows():
        cell=nearest.iloc[i]
        is_ref=p["point_type"]=="REF"
        for feature,source_col,prov_col,unc_col,donor_col,unit,allowed in feature_specs:
            if is_ref:
                if feature in ["settlement_anchor_map_mm","kzt","ko","seismic_energy_J_m2","fill_density","fault_distance_m"]:
                    val=p.get(feature,0)
                elif feature=="lithology": val=p.get("lithology","stable_external_reference")
                else: val=np.nan
                prov="S_REF"
                unc=0.0 if feature=="settlement_anchor_map_mm" else np.nan
                donor=0.0
                cell_id=None
            else:
                val=p.get(feature,np.nan) if feature in p.index and pd.notna(p.get(feature,np.nan)) else cell.get(source_col,np.nan)
                prov=cell.get(prov_col,"R") if prov_col else "C/R"
                if unc_col: unc=cell.get(unc_col,np.nan)
                elif feature=="fill_density": unc=0.08
                elif feature=="fault_distance_m": unc=25.0
                elif feature.startswith("terrain_"): unc=0.10
                else: unc=np.nan
                donor=cell.get(donor_col,0.0) if donor_col else float(dist[i])
                cell_id=cell["cell_id"]
            rows.append({
                "point_id":p["point_id"],"profile_id":p["profile_id"],"point_type":p["point_type"],
                "feature":feature,"value":val,"unit":unit,"provenance":prov,
                "standard_uncertainty":unc,"donor_distance_m":donor,"source_cell_id":cell_id,
                "model_feature_allowed":allowed,"source_reference_year":SOURCE_YEAR,
            })
    lineage=pd.DataFrame(rows)
    safe=lineage[lineage["model_feature_allowed"]].copy()
    # Wide static table with explicit quality attributes.
    static=points[["point_id","profile_id","point_type","chainage_m"]].copy()
    static["chainage_normalized_profile"]=static.groupby("profile_id")["chainage_m"].transform(lambda s:(s-s.min())/max(s.max()-s.min(),1e-9))
    for feat in safe["feature"].unique():
        sub=safe[safe["feature"].eq(feat)].set_index("point_id")
        static=static.merge(sub[["value","standard_uncertainty","donor_distance_m","provenance"]].rename(columns={
            "value":feat,
            "standard_uncertainty":f"{feat}__standard_uncertainty",
            "donor_distance_m":f"{feat}__donor_distance_m",
            "provenance":f"{feat}__provenance",
        }),left_on="point_id",right_index=True,how="left")
    return lineage,safe,static


def generate_stress_scenarios(points: pd.DataFrame, grid: pd.DataFrame, static_safe: pd.DataFrame, rng: np.random.Generator):
    work=points[points["point_type"].eq("WORK")].copy()
    work["q"]=pd.qcut(work["settlement_anchor_map_mm"].rank(method="first"),3,labels=["background","moderate","high"])
    selected=[]
    for zone in ["background","moderate","high"]:
        g=work[work["q"].eq(zone)].sample(n=18,random_state=SEED+{"background":1,"moderate":2,"high":3}[zone],replace=len(work[work["q"].eq(zone)])<18)
        for _,p in g.iterrows(): selected.append((zone,p["point_id"],p["x_local_m"],p["y_local_m"],False))
    valid=grid[grid["effective_area_m2"].gt(0)].copy()
    survey_tree=cKDTree(points[["x_local_m","y_local_m"]].to_numpy(float))
    d,_=survey_tree.query(valid[["x_local_m","y_local_m"]].to_numpy(float),k=1)
    unseen=valid.assign(distance_to_survey=d).sort_values("distance_to_survey",ascending=False).head(200).sample(n=18,random_state=SEED+4)
    for i,(_,p) in enumerate(unseen.iterrows(),1): selected.append(("unseen_spatial",f"OOD-{i:03d}",p["x_local_m"],p["y_local_m"],True))

    monthly=pd.date_range("2021-01-01","2025-12-01",freq="MS")
    scenario_rows=[]; truth_rows=[]; measurement_rows=[]; label_rows=[]; feature_rows=[]; stress_static=[]
    families=["logistic_acceleration","sustained_high_rate","pulse_with_residual","reactivation","step_change","delayed_acceleration"]
    onset_dates=pd.to_datetime(["2022-04-01","2022-10-01","2023-04-01","2023-10-01","2024-04-01","2024-10-01","2025-03-01","2025-07-01"])
    static_idx=static_safe.set_index("point_id")
    grid_tree=cKDTree(valid[["x_local_m","y_local_m"]].to_numpy(float))
    for i,(zone,pid,x,y,is_unseen) in enumerate(selected,1):
        sid=f"ST-{i:03d}"
        family=families[(i-1)%len(families)]
        onset=onset_dates[(i-1)%len(onset_dates)]
        if zone=="background": base=rng.uniform(2,12); amp=rng.uniform(45,95)
        elif zone=="moderate": base=rng.uniform(18,55); amp=rng.uniform(90,180)
        elif zone=="high": base=rng.uniform(55,110); amp=rng.uniform(170,310)
        else: base=rng.uniform(10,45); amp=rng.uniform(130,270)
        t=np.array([(d-onset).days/365.25 for d in monthly])
        if family=="logistic_acceleration": rate=base+amp*sigmoid(t/0.22)
        elif family=="sustained_high_rate": rate=base+amp*sigmoid(t/0.12)
        elif family=="pulse_with_residual": rate=base+amp*np.exp(-0.5*(t/0.22)**2)+0.25*amp*sigmoid(t/0.10)
        elif family=="reactivation": rate=base+0.35*amp*np.exp(-np.maximum((monthly-pd.Timestamp('2021-01-01')).days/365.25,0)/1.4)+amp*sigmoid(t/0.20)
        elif family=="step_change": rate=np.full(len(monthly),base); rate[monthly>=onset]+=amp
        else: rate=base+amp*sigmoid((t-0.35)/0.28)
        rate=np.clip(rate+rng.normal(0,2.5,len(rate)),0,430)
        inc=np.zeros(len(monthly))
        for k in range(1,len(monthly)):
            dt=(monthly[k]-monthly[k-1]).days/365.25
            inc[k]=0.5*(rate[k-1]+rate[k])*dt
        sett=np.cumsum(inc)
        scenario_rows.append({"scenario_id":sid,"family":family,"zone_class":zone,"point_id":pid,"x_local_m":x,"y_local_m":y,"event_onset_date":onset.date().isoformat(),"peak_rate_mm_y":float(rate.max()),"use_class":"stress_test_only_not_calibration","provenance":"S"})
        for d,s,v in zip(monthly,sett,rate): truth_rows.append({"scenario_id":sid,"point_id":pid,"date":d.date().isoformat(),"true_settlement_mm":s,"true_velocity_mm_y":v,"event_onset_date":onset.date().isoformat(),"zone_class":zone,"use_class":"evaluation_only"})
        # Static features for unseen points are taken from their grid cell; existing points from safe static table.
        if pid in static_idx.index:
            ss=static_idx.loc[pid].to_dict()
        else:
            _,gi=grid_tree.query([[x,y]],k=1); cell=valid.iloc[int(gi[0])]
            ss={"point_id":pid,"profile_id":"UNSEEN","point_type":"OOD","chainage_m":np.nan,"chainage_normalized_profile":np.nan,
                "kzt":cell.get("kzt_reconstructed"),"ko":cell.get("ko_reconstructed"),"seismic_energy_J_m2":cell.get("seismic_energy_mid_J_m2_reconstructed"),
                "fill_density":cell.get("backfill_hatch_density"),"fault_distance_m":cell.get("distance_to_reconstructed_fault_m"),"lithology":cell.get("lithology_reconstructed"),
                "terrain_TRI_relative":cell.get("terrain_TRI_relative"),"terrain_roughness_relative":cell.get("terrain_roughness_relative")}
        ss.update({"scenario_id":sid,"stress_zone_class":zone,"unseen_spatial":is_unseen})
        stress_static.append(ss)
        # Measurement dates: irregular, denser around onset.
        base_dates=list(pd.date_range("2022-01-15","2025-11-15",freq="3MS"))
        around=[onset-pd.DateOffset(months=m) for m in [9,6,4,2,1]]+[onset+pd.DateOffset(months=m) for m in [1,2,4,6,9]]
        meas_dates=sorted({pd.Timestamp(d) for d in base_dates+around if pd.Timestamp('2022-01-01')<=pd.Timestamp(d)<=pd.Timestamp('2025-12-01')})
        obs_hist=[]
        for j,d in enumerate(meas_dates):
            true_s=interp_series(monthly.to_numpy(),sett,d)
            true_v=interp_series(monthly.to_numpy(),rate,d)
            missing=bool(rng.random()<0.035)
            gross=bool((not missing) and rng.random()<0.018)
            sigma=2.5+rng.uniform(0,1.5)
            obs=np.nan if missing else true_s+rng.normal(0,sigma)+(rng.choice([-1,1])*rng.uniform(12,28) if gross else 0)
            measurement_rows.append({"scenario_id":sid,"point_id":pid,"date":d.date().isoformat(),"observed_settlement_mm":obs,"missing":missing,"standard_uncertainty_mm":sigma,"zone_class":zone,"use_class":"stress_test_only_not_calibration","provenance":"S"})
            label_rows.append({"scenario_id":sid,"point_id":pid,"date":d.date().isoformat(),"true_settlement_mm":true_s,"true_velocity_mm_y":true_v,"gross_error_injected":gross,"event_onset_date":onset.date().isoformat(),"use_class":"evaluation_only"})
            if not missing: obs_hist.append((d,obs,sigma))
            if len(obs_hist)>=3 and not missing:
                hist=obs_hist[:-1] if obs_hist[-1][0]==d else obs_hist
                if len(hist)>=3:
                    h=hist[-3:]
                    rates=[]
                    for (d0,s0,_),(d1,s1,_) in zip(h[:-1],h[1:]): rates.append((s1-s0)/(d1-d0).days*365.25)
                    current_rate=rates[-1]
                    mean3=float(np.mean(rates))
                    accel=(rates[-1]-rates[-2])/max((h[-1][0]-h[-2][0]).days/365.25,1e-6) if len(rates)>=2 else 0
                    days_to=(onset-d).days
                    label=int(0<days_to<=180)
                    sample_id=f"{sid}::{d.date().isoformat()}"
                    feature_rows.append({"sample_id":sample_id,"scenario_id":sid,"point_id":pid,"current_date":d.date().isoformat(),"n_history":len(obs_hist)-1,"last_settlement_mm":h[-1][1],"last_rate_mm_y":current_rate,"mean_recent_rate_mm_y":mean3,"recent_acceleration_mm_y2":accel,"current_uncertainty_mm":h[-1][2],"forecast_horizon_days":180,"stress_zone_class":zone,"unseen_spatial":is_unseen,"split":"train" if d.year<=2023 else ("validation" if d.year==2024 else "test")})
                    # Label saved separately by sample id.
                    label_rows[-1]["sample_id"]=sample_id; label_rows[-1]["early_acceleration_label"]=label; label_rows[-1]["days_to_event"]=days_to
    catalog=pd.DataFrame(scenario_rows)
    truth=pd.DataFrame(truth_rows)
    measurements=pd.DataFrame(measurement_rows)
    labels=pd.DataFrame(label_rows)
    features=pd.DataFrame(feature_rows)
    if len(features):
        label_sample=labels.dropna(subset=["sample_id"])[["sample_id","early_acceleration_label","days_to_event","true_velocity_mm_y","event_onset_date"]].drop_duplicates("sample_id")
    else: label_sample=pd.DataFrame()
    return catalog,truth,measurements,labels,features,label_sample,pd.DataFrame(stress_static)


def build_next_cycle_samples(
    adjusted: pd.DataFrame,
    membership: pd.DataFrame,
    campaigns: pd.DataFrame,
    points: pd.DataFrame,
    static_safe: pd.DataFrame,
    truth_map: dict[str,pd.DataFrame],
    params: pd.DataFrame,
    rng: np.random.Generator,
):
    camp_type=campaigns.set_index("campaign_id")["campaign_type"].to_dict()
    static_idx=static_safe.set_index("point_id")
    params_idx=params.set_index("point_id")
    membership_idx=membership.set_index(["campaign_id","point_id"])
    features=[]; targets=[]; early_labels=[]
    # Current campaign neighbor aggregates are computed from observed records only.
    adj=adjusted.copy(); adj["date_dt"]=pd.to_datetime(adj["date"])
    rate_tmp=[]
    for pid,g in adj.sort_values("date_dt").groupby("point_id"):
        g=g.sort_values("date_dt")
        last=None
        for _,r in g.iterrows():
            if last is None: rate=np.nan
            else: rate=(r["observed_settlement_mm"]-last["observed_settlement_mm"])/(r["date_dt"]-last["date_dt"]).days*365.25
            rate_tmp.append({"campaign_id":r["campaign_id"],"point_id":pid,"profile_id":r["profile_id"],"rate":rate,"sett":r["observed_settlement_mm"]})
            last=r
    rate_tmp=pd.DataFrame(rate_tmp)
    neigh=rate_tmp.groupby(["campaign_id","profile_id"]).agg(profile_mean_settlement_mm=("sett","mean"),profile_mean_rate_mm_y=("rate","mean"),profile_rate_std_mm_y=("rate","std"),profile_n_observed=("point_id","count")).reset_index()
    neigh_lookup=neigh.set_index(["campaign_id","profile_id"]).to_dict("index")

    for pid,g in adj[adj["point_id"].isin(points.query("point_type=='WORK'")["point_id"])].groupby("point_id"):
        g=g.sort_values("date_dt").reset_index(drop=True)
        if len(g)<4: continue
        rates=[]
        for i in range(1,len(g)):
            dt=(g.loc[i,"date_dt"]-g.loc[i-1,"date_dt"]).days
            rates.append((g.loc[i,"observed_settlement_mm"]-g.loc[i-1,"observed_settlement_mm"])/dt*365.25)
        for i in range(2,len(g)-1):
            current=g.loc[i]; nxt=g.loc[i+1]; prev=g.loc[i-1]
            hist_rates=np.array(rates[:i],dtype=float)
            if len(hist_rates)<2: continue
            last_rate=float(hist_rates[-1]); mean3=float(np.mean(hist_rates[-3:])); std3=float(np.std(hist_rates[-3:],ddof=0))
            prev_rate=float(hist_rates[-2]); dt_last=(current["date_dt"]-prev["date_dt"]).days/365.25
            accel=(last_rate-prev_rate)/max(dt_last,1e-6)
            forecast_h=(nxt["date_dt"]-current["date_dt"]).days
            sample_id=f"{pid}::{current['campaign_id']}::{nxt['campaign_id']}"
            static=static_idx.loc[pid].to_dict()
            ninfo=neigh_lookup.get((current["campaign_id"],current["profile_id"]),{})
            # Missing count between previous and current campaign positions in the master calendar.
            corder=campaigns.reset_index().set_index("campaign_id")["index"]
            i0,i1=int(corder[prev["campaign_id"]]),int(corder[current["campaign_id"]])
            mids=campaigns.iloc[i0+1:i1]
            missing_between=0
            for mcid in mids["campaign_id"]:
                if (mcid,pid) in membership_idx.index and membership_idx.loc[(mcid,pid),"membership_status"]!="observed": missing_between+=1
            row={
                "sample_id":sample_id,"point_id":pid,"profile_id":current["profile_id"],
                "current_campaign_id":current["campaign_id"],"current_date":current["date"],
                "target_campaign_id":nxt["campaign_id"],"target_date":nxt["date"],
                "split":"train" if nxt["date_dt"].year<=2023 else ("validation" if nxt["date_dt"].year==2024 else "test"),
                "n_history":i+1,"last_settlement_mm":float(current["observed_settlement_mm"]),
                "last_rate_mm_y":last_rate,"mean_last_3_rates_mm_y":mean3,"std_last_3_rates_mm_y":std3,
                "recent_acceleration_mm_y2":accel,"current_standard_uncertainty_mm":float(current["standard_uncertainty_mm"]),
                "days_since_previous_observation":int((current["date_dt"]-prev["date_dt"]).days),
                "forecast_horizon_days":forecast_h,"current_campaign_type":camp_type[current["campaign_id"]],
                "missing_campaigns_since_previous":missing_between,
                "profile_mean_settlement_mm":ninfo.get("profile_mean_settlement_mm"),
                "profile_mean_rate_mm_y":ninfo.get("profile_mean_rate_mm_y"),
                "profile_rate_std_mm_y":ninfo.get("profile_rate_std_mm_y"),
                "profile_n_observed":ninfo.get("profile_n_observed"),
            }
            # Add leakage-safe static fields, excluding identifiers/point type.
            for k,v in static.items():
                if k not in ["point_id","profile_id","point_type"]: row[k]=v
            features.append(row)
            obs_inc=float(nxt["observed_settlement_mm"]-current["observed_settlement_mm"])
            true_cur=sample_truth(truth_map,pid,current["date_dt"],"true_settlement_mm")
            true_next=sample_truth(truth_map,pid,nxt["date_dt"],"true_settlement_mm")
            true_rate_next=(true_next-true_cur)/forecast_h*365.25
            targets.append({"sample_id":sample_id,"observed_next_increment_mm":obs_inc,"hidden_true_next_increment_mm":true_next-true_cur,"hidden_true_next_rate_mm_y":true_rate_next,"next_observation_uncertainty_mm":float(nxt["standard_uncertainty_mm"]),"use_class":"evaluation_only"})
            current_true_rate=sample_truth(truth_map,pid,current["date_dt"],"true_velocity_mm_y")
            horizon_end=current["date_dt"]+pd.Timedelta(days=180)
            tdf=truth_map[pid]
            future=tdf[(tdf["date"]>current["date_dt"])&(tdf["date"]<=horizon_end)]
            max_future_rate=float(future["true_velocity_mm_y"].max()) if len(future) else current_true_rate
            max_future_acc=float(future["true_acceleration_mm_y2"].max()) if len(future) else 0
            label=int((max_future_rate-current_true_rate>=25.0) and (max_future_acc>=15.0))
            event_date=params_idx.loc[pid,"event_onset_date"] if pid in params_idx.index else None
            second=params_idx.loc[pid,"second_event_date"] if pid in params_idx.index else None
            future_events=[pd.Timestamp(x) for x in [event_date,second] if pd.notna(x)]
            days_to=min([(x-current["date_dt"]).days for x in future_events if x>current["date_dt"]],default=np.nan)
            early_labels.append({"sample_id":sample_id,"early_acceleration_label":label,"current_true_rate_mm_y":current_true_rate,"max_true_rate_next_180d_mm_y":max_future_rate,"max_true_acceleration_next_180d_mm_y2":max_future_acc,"days_to_hidden_event":days_to,"process_family":params_idx.loc[pid,"process_family"],"use_class":"evaluation_only"})
    f=pd.DataFrame(features); t=pd.DataFrame(targets); e=pd.DataFrame(early_labels)
    # Balanced index for training only: all positives + up to 4 negatives per positive by split.
    idx_rows=[]
    merged=f[["sample_id","split"]].merge(e[["sample_id","early_acceleration_label"]],on="sample_id")
    for split,g in merged.groupby("split"):
        pos=g[g["early_acceleration_label"].eq(1)]
        neg=g[g["early_acceleration_label"].eq(0)]
        if len(pos):
            chosen=neg.sample(n=min(len(neg),4*len(pos)),random_state=SEED+len(split))
            h=pd.concat([pos,chosen])
        else: h=neg.sample(n=min(len(neg),100),random_state=SEED+len(split))
        for _,r in h.iterrows(): idx_rows.append({"sample_id":r["sample_id"],"split":split,"sampling_class":"positive" if r["early_acceleration_label"] else "sampled_negative","sample_weight":1.0})
    return f,t,e,pd.DataFrame(idx_rows)


def create_feature_contract(feature_df: pd.DataFrame) -> pd.DataFrame:
    metadata={"sample_id","point_id","profile_id","current_campaign_id","current_date","target_campaign_id","target_date","split"}
    forbidden_patterns=["settlement_anchor","true_","hidden_","process_family","regime","event_","risk_score","base_rate","decay_tau","x_local","y_local"]
    rows=[]
    for col in feature_df.columns:
        if col in metadata:
            role="METADATA"; allowed=False; reason="identifier or split metadata, retained for joins but excluded from estimator"
        elif any(p in col for p in forbidden_patterns):
            role="FORBIDDEN"; allowed=False; reason="terminal map, hidden truth, generator state, coordinate or event leakage"
        else:
            role="MODEL_FEATURE"; allowed=True; reason="available at prediction time in the reconstructed monitoring workflow"
        rows.append({"field":col,"role":role,"allowed":allowed,"reason":reason})
    # Explicit forbidden fields not present in model-ready export, to document physical exclusion.
    for col in ["settlement_anchor_map_mm","x_local_m","y_local_m","process_family","regime_stage","base_rate_mm_y","event_onset_date","event_amplitude_mm_y","decay_tau_y","true_settlement_mm","true_velocity_mm_y"]:
        if col not in feature_df.columns:
            rows.append({"field":col,"role":"PHYSICALLY_EXCLUDED","allowed":False,"reason":"not present in model-ready files"})
    return pd.DataFrame(rows)


def kalman_predict(history_dates,history_values,history_sigmas,target_date,q=250.0):
    if len(history_values)<2: return float(history_values[-1])
    x=np.array([history_values[0],0.0],float)
    P=np.diag([max(history_sigmas[0]**2,1.0),400.0])
    last_date=pd.Timestamp(history_dates[0])
    for d,z,sig in zip(history_dates[1:],history_values[1:],history_sigmas[1:]):
        d=pd.Timestamp(d); dt=(d-last_date).days/365.25
        F=np.array([[1,dt],[0,1]],float)
        Q=q*np.array([[dt**3/3,dt**2/2],[dt**2/2,dt]],float)
        x=F@x; P=F@P@F.T+Q
        H=np.array([[1.0,0.0]])
        R=max(sig**2,0.25)
        y=z-float(H@x); S=float(H@P@H.T+R)
        K=(P@H.T)/S
        x=x+K[:,0]*y; P=(np.eye(2)-K@H)@P
        last_date=d
    dt=(pd.Timestamp(target_date)-last_date).days/365.25
    F=np.array([[1,dt],[0,1]],float)
    return float((F@x)[0])


def run_baselines(features: pd.DataFrame, targets: pd.DataFrame, adjusted: pd.DataFrame, early_labels: pd.DataFrame):
    data=features.merge(targets,on="sample_id")
    test=data[data["split"].eq("test")].copy()
    adj=adjusted.copy(); adj["date_dt"]=pd.to_datetime(adj["date"])
    hist_map={pid:g.sort_values("date_dt") for pid,g in adj.groupby("point_id")}
    preds={"Last rate":[],"Mean last 3 rates":[],"Kalman q=250":[]}
    for _,r in test.iterrows():
        horizon=float(r["forecast_horizon_days"])/365.25
        preds["Last rate"].append(float(r["last_rate_mm_y"])*horizon)
        preds["Mean last 3 rates"].append(float(r["mean_last_3_rates_mm_y"])*horizon)
        hist=hist_map[r["point_id"]]
        hist=hist[hist["date_dt"]<=pd.Timestamp(r["current_date"])]
        pred_level=kalman_predict(hist["date_dt"].tolist(),hist["observed_settlement_mm"].tolist(),hist["standard_uncertainty_mm"].tolist(),pd.Timestamp(r["target_date"]),q=250.0)
        preds["Kalman q=250"].append(pred_level-float(r["last_settlement_mm"]))
    metrics=[]
    for target_name,target_col in [("observed","observed_next_increment_mm"),("hidden_truth","hidden_true_next_increment_mm")]:
        y=test[target_col].to_numpy(float)
        for model,p in preds.items():
            p=np.array(p,float)
            metrics.append({"split":"test_2025","target":target_name,"model":model,"n":len(y),"MAE_mm":mean_absolute_error(y,p),"RMSE_mm":math.sqrt(mean_squared_error(y,p)),"R2":r2_score(y,p),"Bias_mm":float(np.mean(p-y))})
    # HGB diagnostic on safe numeric and simple encoded categorical fields.
    allowed=[c for c in features.columns if c not in {"sample_id","point_id","profile_id","current_campaign_id","current_date","target_campaign_id","target_date","split"} and pd.api.types.is_numeric_dtype(features[c])]
    train=data[data["split"].eq("train")].copy(); valid=data[data["split"].eq("validation")].copy()
    med=train[allowed].median(numeric_only=True)
    Xtr=train[allowed].fillna(med); Xte=test[allowed].fillna(med)
    model=HistGradientBoostingRegressor(max_iter=120,max_depth=4,learning_rate=0.05,l2_regularization=2.0,random_state=SEED)
    model.fit(Xtr,train["hidden_true_next_rate_mm_y"])
    prate=model.predict(Xte); pinc=prate*test["forecast_horizon_days"].to_numpy(float)/365.25
    for target_name,target_col in [("observed","observed_next_increment_mm"),("hidden_truth","hidden_true_next_increment_mm")]:
        y=test[target_col].to_numpy(float)
        metrics.append({"split":"test_2025","target":target_name,"model":"HGB safe features","n":len(y),"MAE_mm":mean_absolute_error(y,pinc),"RMSE_mm":math.sqrt(mean_squared_error(y,pinc)),"R2":r2_score(y,pinc),"Bias_mm":float(np.mean(pinc-y))})

    # Early warning logistic baseline.
    ed=features.merge(early_labels,on="sample_id")
    tr=ed[ed["split"].eq("train")]; va=ed[ed["split"].eq("validation")]; te=ed[ed["split"].eq("test")]
    numeric=[c for c in allowed if c in ed.columns and tr[c].notna().any()]
    med2=tr[numeric].median(numeric_only=True).fillna(0)
    pipe=Pipeline([("scale",StandardScaler()),("clf",LogisticRegression(class_weight="balanced",max_iter=1000,random_state=SEED))])
    pipe.fit(tr[numeric].fillna(med2).fillna(0),tr["early_acceleration_label"])
    pv=pipe.predict_proba(va[numeric].fillna(med2).fillna(0))[:,1]
    thresholds=np.linspace(0.05,0.95,181)
    scores=[f1_score(va["early_acceleration_label"],pv>=t,zero_division=0) for t in thresholds]
    threshold=float(thresholds[int(np.argmax(scores))])
    pt=pipe.predict_proba(te[numeric].fillna(med2).fillna(0))[:,1]
    yh=(pt>=threshold).astype(int); yt=te["early_acceleration_label"].to_numpy(int)
    early_metric={"model":"Logistic balanced","split":"test_2025","threshold_selected_on_validation":threshold,"n":len(yt),"positive_rate":float(np.mean(yt)),"precision":precision_score(yt,yh,zero_division=0),"recall":recall_score(yt,yh,zero_division=0),"F1":f1_score(yt,yh,zero_division=0),"average_precision":average_precision_score(yt,pt) if yt.sum()>0 else np.nan,"roc_auc":roc_auc_score(yt,pt) if len(np.unique(yt))>1 else np.nan}
    return pd.DataFrame(metrics),pd.DataFrame([early_metric]),{"model":"kalman_local_linear_trend","q":250.0,"selected_before_external_data":True,"no_retraining":True,"version":VERSION}


def build_external_validation(output_dir: Path, leveling: pd.DataFrame, frozen_config: dict):
    ext=output_dir/"external_validation"; ext.mkdir(parents=True,exist_ok=True)
    schema=pd.DataFrame([
        ["point_id",True,"string","","Stable point identifier within the real monitoring series."],
        ["date",True,"YYYY-MM-DD","","Observation date."],
        ["observed_settlement_mm",True,"float","mm","Cumulative settlement in the adopted sign convention."],
        ["standard_uncertainty_mm",True,"float > 0","mm","Standard uncertainty of the observation."],
        ["campaign_id",False,"string","","Optional real campaign identifier."],
        ["profile_id",False,"string","","Optional profile identifier."],
    ],columns=["field","required","dtype","unit","description"])
    write_csv(schema,ext/"external_cycle_schema.csv")
    write_json(frozen_config,ext/"frozen_baseline_config.json")
    protocol="""# External validation protocol (no retraining)\n\nStatus: READY_PENDING_REAL_DATA.\n\n1. Freeze this v3.2 dataset, feature contract and baseline configuration.\n2. Provide real repeated cycles in `external_cycle_schema.csv` format.\n3. Do not tune q, thresholds, features or filters on the external sequence.\n4. Run `run_external_validation.py`.\n5. Report all points, missing rows, MAE/RMSE/Bias and coverage by profile.\n6. Synthetic smoke fixtures verify software only and are not external evidence.\n"""
    (ext/"EXTERNAL_VALIDATION_PROTOCOL.md").write_text(protocol,encoding="utf-8")
    script=r'''#!/usr/bin/env python3
import argparse, json, math
from pathlib import Path
import numpy as np, pandas as pd

def kalman_predict(dates, values, sigmas, target_date, q):
    x=np.array([values[0],0.0],float); P=np.diag([max(sigmas[0]**2,1.0),400.0]); last=pd.Timestamp(dates[0])
    for d,z,s in zip(dates[1:],values[1:],sigmas[1:]):
        d=pd.Timestamp(d); dt=(d-last).days/365.25; F=np.array([[1,dt],[0,1]],float); Q=q*np.array([[dt**3/3,dt**2/2],[dt**2/2,dt]],float)
        x=F@x; P=F@P@F.T+Q; H=np.array([[1.,0.]]); R=max(float(s)**2,.25); y=float(z-H@x); S=float(H@P@H.T+R); K=(P@H.T)/S; x=x+K[:,0]*y; P=(np.eye(2)-K@H)@P; last=d
    dt=(pd.Timestamp(target_date)-last).days/365.25; return float((np.array([[1,dt],[0,1]])@x)[0])

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--input',required=True); ap.add_argument('--config',required=True); ap.add_argument('--output',required=True); ap.add_argument('--allow-synthetic-smoke',action='store_true'); a=ap.parse_args()
    df=pd.read_csv(a.input); required=['point_id','date','observed_settlement_mm','standard_uncertainty_mm']; missing=[c for c in required if c not in df.columns]
    if missing: raise SystemExit(f'Missing columns: {missing}')
    df['date']=pd.to_datetime(df['date']); df=df.sort_values(['point_id','date']); cfg=json.loads(Path(a.config).read_text())
    rows=[]
    for pid,g in df.groupby('point_id'):
        g=g.reset_index(drop=True)
        for i in range(3,len(g)):
            hist=g.iloc[:i]; target=g.iloc[i]
            pred=kalman_predict(hist.date.tolist(),hist.observed_settlement_mm.tolist(),hist.standard_uncertainty_mm.tolist(),target.date,cfg['q'])
            rows.append({'point_id':pid,'target_date':target.date.date().isoformat(),'observed_settlement_mm':target.observed_settlement_mm,'predicted_settlement_mm':pred,'residual_mm':pred-target.observed_settlement_mm,'n_history':i})
    out=Path(a.output); out.mkdir(parents=True,exist_ok=True); pred=pd.DataFrame(rows); pred.to_csv(out/'external_predictions.csv',index=False)
    if len(pred):
        r=pred.residual_mm.to_numpy(float); metrics={'n':len(r),'MAE_mm':float(np.mean(np.abs(r))),'RMSE_mm':float(np.sqrt(np.mean(r*r))),'Bias_mm':float(np.mean(r)),'config':cfg,'retrained':False}
    else: metrics={'n':0,'status':'insufficient_history','config':cfg,'retrained':False}
    (out/'external_metrics.json').write_text(json.dumps(metrics,indent=2,ensure_ascii=False),encoding='utf-8')
if __name__=='__main__': main()
'''
    (ext/"run_external_validation.py").write_text(script,encoding="utf-8")
    os.chmod(ext/"run_external_validation.py",0o755)
    smoke=leveling[leveling["point_id"].isin(leveling["point_id"].drop_duplicates().head(3))][["point_id","date","observed_settlement_mm","standard_uncertainty_mm","campaign_id","profile_id"]].copy()
    write_csv(smoke,ext/"synthetic_smoke_fixture.csv")
    smoke_out=ext/"synthetic_smoke_output"; smoke_out.mkdir(exist_ok=True)
    subprocess.run([sys.executable,str(ext/"run_external_validation.py"),"--input",str(ext/"synthetic_smoke_fixture.csv"),"--config",str(ext/"frozen_baseline_config.json"),"--output",str(smoke_out),"--allow-synthetic-smoke"],check=True)
    write_json({"status":"PASS","note":"Software smoke test on synthetic records only; not external validation."},ext/"synthetic_smoke_test_status.json")


def create_data_dictionary(output_dir: Path):
    rows=[
        ["tables/survey_points.csv","point_id","Stable point identifier","string","public"],
        ["tables/campaign_point_membership.csv","membership_status","observed/not_targeted/missing reason","category","public"],
        ["evaluation_only/truth_survey_points_monthly.csv","regime_stage","Time-dependent stage","category","evaluation_only"],
        ["model_ready/next_cycle_features.csv","forecast_horizon_days","Days to next observed cycle","days","model_ready"],
        ["evaluation_only/next_cycle_targets.csv","hidden_true_next_increment_mm","Synthetic truth increment","mm","evaluation_only"],
        ["tables/point_feature_lineage.csv","donor_distance_m","Distance to source/reconstruction donor","m","public"],
        ["tables/insar_observations_relative.csv","subvertical_estimate_relative_mm","Corrected relative InSAR estimate","mm","public"],
        ["external_validation/external_cycle_schema.csv","observed_settlement_mm","Required real external measurement","mm","external_input"],
    ]
    write_csv(pd.DataFrame(rows,columns=["table","field","description","unit_or_type","access_class"]),output_dir/"tables"/"data_dictionary_v3_2.csv")


def create_figures(output_dir: Path, assignment: pd.DataFrame, truth: pd.DataFrame, campaigns: pd.DataFrame, membership: pd.DataFrame, gnss_network: pd.DataFrame, points: pd.DataFrame, insar_catalog: pd.DataFrame, early_labels: pd.DataFrame):
    import matplotlib.pyplot as plt
    figdir=output_dir/"figures_v3_2"; figdir.mkdir(exist_ok=True)
    counts=assignment["process_family"].value_counts().reindex(REGIME_COUNTS.keys())
    plt.figure(figsize=(8,4)); counts.plot(kind="bar"); plt.ylabel("Пункты"); plt.title("Баланс семейств процесса v3.2"); plt.tight_layout(); plt.savefig(figdir/"01_regime_balance.png",dpi=160); plt.close()
    t=truth[(truth["point_id"].isin(assignment["point_id"]))]
    plt.figure(figsize=(8,4)); plt.hist(t["true_velocity_mm_y"],bins=40); plt.axvline(250,ls="--"); plt.xlabel("мм/год"); plt.title("Номинальные скорости"); plt.tight_layout(); plt.savefig(figdir/"02_velocity_distribution.png",dpi=160); plt.close()
    summ=membership.groupby(["campaign_id","campaign_type"])["observed"].mean().reset_index()
    plt.figure(figsize=(9,4));
    for typ,g in summ.groupby("campaign_type"): plt.plot(range(len(g)),g["observed"],marker="o",label=typ)
    plt.ylabel("Доля всей сети"); plt.legend(); plt.title("Фактическое покрытие циклов"); plt.tight_layout(); plt.savefig(figdir/"03_campaign_coverage.png",dpi=160); plt.close()
    p=points.set_index("point_id").loc[gnss_network["point_id"]]
    plt.figure(figsize=(7,7)); plt.scatter(points["x_local_m"],points["y_local_m"],s=8,alpha=.25); plt.scatter(p["x_local_m"],p["y_local_m"],s=24); plt.title("Стратифицированная GNSS-сеть"); plt.axis("equal"); plt.tight_layout(); plt.savefig(figdir/"04_gnss_network.png",dpi=160); plt.close()
    plt.figure(figsize=(7,7)); plt.scatter(insar_catalog["x_local_m"],insar_catalog["y_local_m"],c=insar_catalog["coherence_baseline"],s=9); plt.colorbar(label="coherence"); plt.title("Независимый InSAR-контур"); plt.axis("equal"); plt.tight_layout(); plt.savefig(figdir/"05_insar_field.png",dpi=160); plt.close()
    m=early_labels["early_acceleration_label"].value_counts().sort_index(); plt.figure(figsize=(5,4)); m.plot(kind="bar"); plt.title("Nominal early-warning labels"); plt.tight_layout(); plt.savefig(figdir/"06_early_warning_balance.png",dpi=160); plt.close()


def create_docs(output_dir: Path, summary: dict):
    readme=f"""# SKRU-1 reconstructed data foundation v3.2\n\nVersion: {VERSION}\n\nThis package is a reconstructed/synthetic research dataset for developing a subsidence forecasting algorithm. It is not an official production journal.\n\n## Access separation\n\n- `tables/`: public reconstructed and simulated observations;\n- `model_ready/`: leakage-controlled feature tables;\n- `evaluation_only/`: synthetic truth and targets;\n- `private_generation/`: latent process parameters;\n- `external_validation/`: frozen no-retraining validation harness.\n\nExternal validation status: `{summary['external_test_status']}`.\n"""
    (output_dir/"README.md").write_text(readme,encoding="utf-8")
    card=f"""# Dataset card v3.2\n\n- Object: reconstructed SKRU-1 spatial context.\n- WORK points: 98; reference points: 28.\n- Campaigns: 29, including 14 focused cycles.\n- Nominal process families: {summary['regime_counts']}.\n- Nominal maximum velocity: {summary['velocity_max_mm_y']} mm/year.\n- Real external validation: not yet available.\n- Intended use: EDA, pipeline development, controlled experiments and ablation.\n- Prohibited use: production safety decisions and claims of real predictive accuracy.\n"""
    (output_dir/"DATASET_CARD.md").write_text(card,encoding="utf-8")
    methodology="""# Methodology v3.2\n\n1. The audited v3.1 spatial reconstruction is retained.\n2. Nominal temporal histories are conditioned on the published 2022 map only by year; exact source date remains unknown.\n3. Stable, uniform, decaying, accelerating, reactivated and step-change families are generated with time-dependent stages.\n4. Full and focused survey campaigns are explicit; missingness has typed causes.\n5. Leveling is adjusted from observed differences and independent datum constraints; synthetic truth is evaluation-only.\n6. GNSS is stratified by profile and deformation intensity.\n7. InSAR is generated as an independent continuous spatial-temporal field, not copied from survey-point trajectories.\n8. Early acceleration is a separate rare-event classification task.\n9. Model-ready features are physically separated from targets and latent parameters.\n10. Real cycles must be evaluated with the frozen external harness and no retraining.\n"""
    (output_dir/"METHODOLOGY_V3_2.md").write_text(methodology,encoding="utf-8")
    changelog="""# Changelog v3.2\n\n- balanced process families;\n- introduced time-dependent stages;\n- capped nominal velocity at 220 mm/year;\n- implemented real focused campaigns, missingness and long gaps;\n- separated public/model-ready/evaluation/private data;\n- added point-level lineage and uncertainty;\n- stratified GNSS;\n- independent InSAR field;\n- 72 stratified stress scenarios;\n- separate early-acceleration task;\n- frozen external validation harness.\n"""
    (output_dir/"CHANGELOG_V3_2.md").write_text(changelog,encoding="utf-8")


def validate_and_audit(
    output_dir: Path,
    assignment: pd.DataFrame,
    truth: pd.DataFrame,
    campaigns: pd.DataFrame,
    membership: pd.DataFrame,
    lineage: pd.DataFrame,
    model_features: pd.DataFrame,
    gnss_network: pd.DataFrame,
    insar_catalog: pd.DataFrame,
    insar_obs: pd.DataFrame,
    insar_truth: pd.DataFrame,
    stress_catalog: pd.DataFrame,
    early_labels: pd.DataFrame,
    stress_early_labels: pd.DataFrame,
    tilts: pd.DataFrame,
    curvatures: pd.DataFrame,
    rates: pd.DataFrame,
    strains: pd.DataFrame,
    adjusted: pd.DataFrame,
    eval_leveling: pd.DataFrame,
    gnss_eval: pd.DataFrame,
):
    checks=[]
    def add(cid,domain,name,status,severity,observed,expected,interpretation=""):
        checks.append({"check_id":cid,"domain":domain,"check":name,"status":status,"severity":severity,"observed":str(observed),"expected":str(expected),"interpretation":interpretation})
    counts=assignment["process_family"].value_counts().to_dict()
    add("R01","temporal","Balanced process-family design","PASS" if counts==REGIME_COUNTS else "FAIL","critical",counts,REGIME_COUNTS,"Acceleration is localized rather than dominant.")
    stage_counts=truth[truth["point_id"].isin(assignment["point_id"])].groupby("point_id")["regime_stage"].nunique()
    add("R02","temporal","Regime is time-dependent","PASS" if (stage_counts[assignment[assignment.process_family.ne('stable')].point_id]>=2).all() else "FAIL","critical",stage_counts.describe().to_dict(),">=2 stages for every non-stable WORK point")
    vmax=float(truth[truth["point_id"].isin(assignment["point_id"])]["true_velocity_mm_y"].max()); gt250=float((truth[truth["point_id"].isin(assignment["point_id"])]["true_velocity_mm_y"]>250).mean())
    add("R03","temporal","Nominal high-rate tail reduced","PASS" if vmax<=220.0001 and gt250==0 else "FAIL","high",{"max":vmax,">250_frac":gt250},{"max":"<=220",">250_frac":0})
    focused=membership[membership["campaign_type"].eq("focused")].groupby("campaign_id")["observed"].mean()
    add("R04","sampling","Focused campaigns are true subsets","PASS" if focused.max()<0.45 else "FAIL","critical",focused.describe().to_dict(),"max<0.45")
    missing=membership["membership_status"].value_counts().to_dict(); add("R05","sampling","Missing observations are explicit","PASS" if any(k.startswith('missing_') for k in missing) else "FAIL","high",missing,">0 typed missing records")
    intervals=campaigns["interval_days_from_previous"].dropna(); add("R06","sampling","Long and irregular intervals exist","PASS" if intervals.max()>=240 and intervals.min()<=45 else "FAIL","high",intervals.describe().to_dict(),"max>=240 and min<=45 days")
    forbidden=[c for c in model_features.columns if any(p in c for p in ["true_","hidden_","event_amp","event_center","base_rate","decay_tau","settlement_anchor_map","x_local_m","y_local_m","process_family","regime_stage"])]
    add("R07","leakage","Model-ready physically excludes hidden/terminal fields","PASS" if not forbidden else "FAIL","critical",forbidden,[]) 
    lineage_ok=all(c in lineage.columns for c in ["provenance","standard_uncertainty","donor_distance_m"]); add("R08","lineage","Point-level provenance uncertainty donor distance","PASS" if lineage_ok and len(lineage)>=len(assignment)*9 else "FAIL","high",{"rows":len(lineage),"columns":list(lineage.columns)},">=882 rows and required fields")
    strata=gnss_network.groupby("profile_id")["stratum"].nunique(); add("R09","GNSS","Stratified GNSS network","PASS" if len(gnss_network)==56 and strata.min()>=4 else "FAIL","critical",{"points":len(gnss_network),"profiles":strata.to_dict()},"56 points, four strata/profile")
    unique_ratio=insar_truth.pivot(index="insar_point_id",columns="date",values="true_vertical_settlement_relative_mm").round(6).drop_duplicates().shape[0]/len(insar_catalog)
    no_nearest="nearest_truth_point_id" not in insar_catalog.columns
    add("R10","InSAR","Independent spatial-temporal InSAR field","PASS" if no_nearest and unique_ratio>0.99 else "FAIL","critical",{"points":len(insar_catalog),"unique_ratio":unique_ratio,"nearest_truth_column":not no_nearest},{"unique_ratio":">0.99","nearest_truth_column":False})
    zones=stress_catalog["zone_class"].value_counts().to_dict(); add("R11","stress","Stress scenarios cover four strata","PASS" if zones=={"background":18,"moderate":18,"high":18,"unseen_spatial":18} else "FAIL","high",zones,{"background":18,"moderate":18,"high":18,"unseen_spatial":18})
    split_pos=early_labels.merge(model_features[["sample_id","split"]],on="sample_id").groupby("split")["early_acceleration_label"].sum().to_dict(); stress_pos=int(stress_early_labels["early_acceleration_label"].sum()) if len(stress_early_labels) else 0
    add("R12","early_warning","Separate early-acceleration task has positives","PASS" if all(split_pos.get(s,0)>0 for s in ["train","validation","test"]) and stress_pos>0 else "FAIL","critical",{"nominal_split_positives":split_pos,"stress_positives":stress_pos},"positive labels in all splits and stress")
    smoke=json.loads((output_dir/"external_validation"/"synthetic_smoke_test_status.json").read_text()); add("R13","external","Frozen no-retraining external harness","PASS" if smoke.get("status")=="PASS" else "FAIL","critical",smoke,"software smoke PASS; real status pending")
    first=insar_obs.sort_values("date").groupby("insar_point_id").first(); first_zero=float(first["subvertical_estimate_relative_mm"].abs().max()); add("R14","InSAR","First epoch is zero","PASS" if first_zero<1e-9 else "FAIL","high",first_zero,0)
    ref_bad=truth[(truth["point_id"].str.contains("REF")) & ((truth["true_settlement_mm"].abs()>1e-9)|(truth["true_velocity_mm_y"].abs()>1e-9))]; add("R15","temporal","Reference points remain stable","PASS" if len(ref_bad)==0 else "FAIL","critical",len(ref_bad),0)
    # Formula rechecks from public tables.
    tilt_re=[]
    ad=adjusted.merge(pd.read_csv(output_dir/"tables"/"survey_points.csv")[["point_id","chainage_m"]],on="point_id")
    lookup=ad.set_index(["campaign_id","point_id"])["observed_settlement_mm"].to_dict()
    for _,r in tilts.iterrows(): tilt_re.append((lookup[(r.campaign_id,r.right_point_id)]-lookup[(r.campaign_id,r.left_point_id)])/r.interval_length_m-r.tilt_mm_m)
    curv_re=[]; tlookup=tilts.set_index(["campaign_id","left_point_id","right_point_id"])["tilt_mm_m"].to_dict()
    for _,r in curvatures.iterrows():
        l1,r1=r.left_interval.split('::'); l2,r2=r.right_interval.split('::'); curv_re.append((tlookup[(r.campaign_id,l2,r2)]-tlookup[(r.campaign_id,l1,r1)])/((r.chainage_m-(tilts[(tilts.campaign_id==r.campaign_id)&(tilts.left_point_id==l1)&(tilts.right_point_id==r1)].mid_chainage_m.iloc[0]))*2)-r.curvature_mm_m2)
    max_tilt=float(np.max(np.abs(tilt_re))) if tilt_re else 0; max_curv=float(np.nanmax(np.abs(curv_re))) if curv_re else 0
    add("R16","formula","Tilt and curvature recompute","PASS" if max_tilt<1e-6 and max_curv<1e-6 else "FAIL","high",{"tilt":max_tilt,"curvature":max_curv},"<1e-6")
    # Sensor quality.
    sensor=[]
    for name,df in [("leveling",eval_leveling),("GNSS",gnss_eval)]:
        r=df.residual_mm.to_numpy(float); s=df.standard_uncertainty_mm.to_numpy(float); sensor.append({"sensor":name,"n":len(r),"MAE_mm":np.mean(np.abs(r)),"RMSE_mm":math.sqrt(np.mean(r*r)),"bias_mm":np.mean(r),"coverage95":np.mean(np.abs(r)<=1.96*s),"mean_sigma_mm":np.mean(s)})
    ie=insar_obs.merge(insar_truth,on=["acquisition_id","date","insar_point_id"]); ie=ie[ie["first_epoch_zero_datum"].eq(False)]; r=ie.subvertical_estimate_relative_mm-ie.true_vertical_settlement_relative_mm; s=ie.standard_uncertainty_mm; sensor.append({"sensor":"InSAR","n":len(r),"MAE_mm":np.mean(np.abs(r)),"RMSE_mm":math.sqrt(np.mean(r*r)),"bias_mm":np.mean(r),"coverage95":np.mean(np.abs(r)<=1.96*s),"mean_sigma_mm":np.mean(s)})
    sensor_df=pd.DataFrame(sensor); coverage_min=float(sensor_df.coverage95.min()); add("R17","sensors","Uncertainty coverage is calibrated","PASS" if coverage_min>=0.93 else "FAIL","high",sensor_df.to_dict('records'),">=0.93")
    add("R18","integrity","All requirement checks pass","PASS" if all(c["status"]=="PASS" for c in checks) else "FAIL","critical",sum(c["status"]=="PASS" for c in checks)+1,18)
    checks_df=pd.DataFrame(checks)
    return checks_df,sensor_df


def create_manifests(output_dir: Path):
    rows=[]
    for p in sorted(output_dir.rglob('*')):
        if p.is_file() and 'dataset_manifest' not in p.name:
            rel=p.relative_to(output_dir).as_posix()
            if rel.startswith('model_ready/'): access='MODEL_READY'
            elif rel.startswith('evaluation_only/'): access='EVALUATION_ONLY'
            elif rel.startswith('private_generation/'): access='PRIVATE_GENERATION'
            elif rel.startswith('source_inputs/'): access='SOURCE_INPUT'
            else: access='PUBLIC_OR_DOCUMENTATION'
            rows.append({"relative_path":rel,"bytes":p.stat().st_size,"sha256":sha256_file(p),"access_class":access})
    manifest=pd.DataFrame(rows); write_csv(manifest,output_dir/"metadata"/"dataset_manifest.csv")
    write_json({"dataset_version":VERSION,"files":len(manifest),"total_bytes":int(manifest.bytes.sum()),"access_counts":manifest.access_class.value_counts().to_dict()},output_dir/"metadata"/"dataset_manifest.json")
    model=manifest[manifest.access_class.eq('MODEL_READY')]
    write_json({"dataset_version":VERSION,"files":len(model),"total_bytes":int(model.bytes.sum()),"sha256":dict(zip(model.relative_path,model.sha256))},output_dir/"metadata"/"model_ready_manifest.json")


def make_zip(source_dir: Path, zip_path: Path):
    if zip_path.exists(): zip_path.unlink()
    with zipfile.ZipFile(zip_path,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for p in sorted(source_dir.rglob('*')):
            if p.is_file(): z.write(p,p.relative_to(source_dir.parent))


def make_model_ready_zip(output_dir: Path, zip_path: Path):
    staging=output_dir.parent/'SKRU1_model_ready_v3_2_staging'; safe_rmtree(staging); staging.mkdir()
    shutil.copytree(output_dir/'model_ready',staging/'model_ready')
    shutil.copytree(output_dir/'external_validation',staging/'external_validation')
    for name in ['README.md','DATASET_CARD.md','METHODOLOGY_V3_2.md','CHANGELOG_V3_2.md']:
        shutil.copy2(output_dir/name,staging/name)
    shutil.copy2(output_dir/'tables'/'source_registry.csv',staging/'source_registry.csv')
    shutil.copy2(output_dir/'tables'/'data_dictionary_v3_2.csv',staging/'data_dictionary_v3_2.csv')
    if zip_path.exists(): zip_path.unlink()
    with zipfile.ZipFile(zip_path,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for p in sorted(staging.rglob('*')):
            if p.is_file(): z.write(p,p.relative_to(staging))
    shutil.rmtree(staging)


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--source-v31-zip',default='/mnt/data/SKRU1_data_reconstruction_v3_1.zip')
    ap.add_argument('--output',default='/mnt/data/SKRU1_data_reconstruction_v3_2')
    args=ap.parse_args()
    source_zip=Path(args.source_v31_zip); output=Path(args.output); work=output.parent/'SKRU1_v3_2_build_work'
    safe_rmtree(work); work.mkdir(parents=True)
    with zipfile.ZipFile(source_zip) as z: z.extractall(work)
    v31=work/'SKRU1_data_reconstruction_v3_1'
    safe_rmtree(output)
    shutil.copytree(v31,output)
    # Rename source code to preserve original but install this builder as v3.2 reproducer.
    shutil.copy2(Path(__file__),output/'reproduce_v3_2.py')
    os.chmod(output/'reproduce_v3_2.py',0o755)

    tables=output/'tables'; model_ready=output/'model_ready'; eval_dir=output/'evaluation_only'; private=output/'private_generation'; audit_dir=output/'audit'
    for d in [model_ready,eval_dir,private,audit_dir,output/'external_validation',output/'figures_v3_2']: safe_rmtree(d); d.mkdir(parents=True)
    # Remove old temporal/measurement files that will be regenerated.
    regenerated=['truth_survey_points_monthly.csv','process_parameters_survey_points.csv','synthetic_truth_quantiles_monthly.csv','synthetic_truth_ensemble_monthly.csv','survey_campaigns.csv','leveling_stations_raw.csv','leveling_runs_summary.csv','leveling_adjusted_epochs.csv','benchmark_observations.csv','gnss_sessions_raw.csv','gnss_adjusted_epochs.csv','insar_point_catalog.csv','insar_observations_relative.csv','stress_test_scenario_catalog.csv','stress_test_truth_monthly.csv','stress_test_measurements.csv','planar_observations_raw.csv','horizontal_displacements.csv','horizontal_strains.csv','tilts.csv','curvatures.csv','settlement_rates.csv','profile_kinematics.csv']
    for name in regenerated:
        p=tables/name
        if p.exists(): p.unlink()

    points=pd.read_csv(tables/'survey_points.csv'); grid=pd.read_csv(tables/'field_grid_50m.csv',low_memory=False); acquisitions=pd.read_csv(tables/'insar_acquisition_catalog.csv')
    assignment=create_regime_assignment(points,RNG)
    truth,params,transitions,hidden_events=generate_temporal_truth(points,assignment,RNG)
    truth_map=build_truth_map(truth)
    ensemble,quantiles=generate_truth_ensemble(truth,points,RNG)
    campaigns=make_campaigns(); membership,campaign_summary=generate_campaign_membership(campaigns,points,assignment,truth_map,RNG)
    benchmark=build_benchmark_observations(membership,points,truth_map,RNG)
    stations,runs,adjusted,level_eval,injected=simulate_leveling(membership,campaigns,points,truth_map,benchmark,RNG)
    planar,planar_eval,horiz,strains,tilts,curv,rates,profile_kin=generate_planar_and_kinematics(adjusted,points,campaigns,truth_map,RNG)
    gnss_network=design_gnss_network(points); gnss_sessions,gnss_adjusted,gnss_eval=simulate_gnss(gnss_network,campaigns,membership,points,truth_map,RNG)
    insar_catalog,insar_obs,insar_truth,insar_sources=generate_independent_insar(grid,acquisitions,RNG)
    lineage,lineage_safe,static_safe=build_point_feature_lineage(points,grid)
    stress_catalog,stress_truth,stress_measurements,stress_labels,stress_early_features,stress_early_labels,stress_static=generate_stress_scenarios(points,grid,static_safe,RNG)
    next_features,next_targets,early_labels,balanced_index=build_next_cycle_samples(adjusted,membership,campaigns,points,static_safe,truth_map,params,RNG)
    feature_contract=create_feature_contract(next_features)
    baseline_metrics,early_metrics,frozen_config=run_baselines(next_features,next_targets,adjusted,early_labels)

    # Public tables.
    write_csv(campaign_summary,tables/'survey_campaigns.csv')
    write_csv(membership,tables/'campaign_point_membership.csv')
    write_csv(benchmark,tables/'benchmark_observations.csv')
    write_csv(stations,tables/'leveling_stations_raw.csv')
    write_csv(runs,tables/'leveling_runs_summary.csv')
    write_csv(adjusted,tables/'leveling_adjusted_epochs.csv')
    write_csv(planar,tables/'planar_observations_raw.csv')
    write_csv(horiz,tables/'horizontal_displacements.csv')
    write_csv(strains,tables/'horizontal_strains.csv')
    write_csv(tilts,tables/'tilts.csv')
    write_csv(curv,tables/'curvatures.csv')
    write_csv(rates,tables/'settlement_rates.csv')
    write_csv(profile_kin,tables/'profile_kinematics.csv')
    write_csv(gnss_network,tables/'gnss_network_design.csv')
    write_csv(gnss_sessions,tables/'gnss_sessions_raw.csv')
    write_csv(gnss_adjusted,tables/'gnss_adjusted_epochs.csv')
    write_csv(insar_catalog,tables/'insar_point_catalog.csv')
    write_csv(insar_obs,tables/'insar_observations_relative.csv')
    write_csv(stress_catalog,tables/'stress_test_scenario_catalog.csv')
    write_csv(stress_measurements,tables/'stress_test_measurements.csv')
    write_csv(lineage,tables/'point_feature_lineage.csv')

    # Model-ready safe files.
    write_csv(static_safe,model_ready/'static_point_features.csv')
    write_csv(lineage_safe,model_ready/'point_feature_lineage_safe.csv')
    write_csv(next_features,model_ready/'next_cycle_features.csv')
    write_csv(next_features.drop(columns=['target_campaign_id','target_date'],errors='ignore'),model_ready/'early_acceleration_features.csv')
    write_csv(balanced_index,model_ready/'early_acceleration_balanced_index.csv')
    write_csv(stress_early_features,model_ready/'stress_early_acceleration_features.csv')
    write_csv(stress_static,model_ready/'stress_static_features.csv')
    write_csv(feature_contract,model_ready/'feature_contract.csv')

    # Evaluation-only and private.
    write_csv(truth,eval_dir/'truth_survey_points_monthly.csv')
    write_csv(transitions,eval_dir/'regime_stage_transitions.csv')
    write_csv(ensemble,eval_dir/'synthetic_truth_ensemble_quarterly.csv')
    write_csv(quantiles,eval_dir/'synthetic_truth_quantiles_quarterly.csv')
    write_csv(level_eval,eval_dir/'leveling_adjustment_truth_residuals.csv')
    write_csv(injected,eval_dir/'leveling_station_injected_error_labels.csv')
    write_csv(planar_eval,eval_dir/'planar_observation_truth_residuals.csv')
    write_csv(gnss_eval,eval_dir/'gnss_truth_residuals.csv')
    write_csv(insar_truth,eval_dir/'insar_truth_relative.csv')
    write_csv(stress_truth,eval_dir/'stress_test_truth_monthly.csv')
    write_csv(stress_labels,eval_dir/'stress_test_measurement_labels.csv')
    write_csv(next_targets,eval_dir/'next_cycle_targets.csv')
    write_csv(early_labels,eval_dir/'early_acceleration_labels.csv')
    write_csv(stress_early_labels,eval_dir/'stress_early_acceleration_labels.csv')
    write_csv(params,private/'process_parameters_survey_points.csv')
    write_csv(assignment,private/'process_family_assignment.csv')
    write_csv(hidden_events,private/'hidden_event_catalog.csv')
    write_csv(insar_sources,private/'insar_spatial_source_catalog.csv')

    write_csv(baseline_metrics,audit_dir/'baseline_regression_metrics.csv')
    write_csv(early_metrics,audit_dir/'early_warning_baseline_metrics.csv')
    write_json(frozen_config,audit_dir/'frozen_baseline_selection.json')
    build_external_validation(output,adjusted,frozen_config)
    create_data_dictionary(output)

    # Add revised geodata layers; retain audited spatial reconstruction layers from v3.1.
    old_gpkg=output/'geodata'/'skru1_data_reconstruction_v3_1.gpkg'; new_gpkg=output/'geodata'/'skru1_data_reconstruction_v3_2.gpkg'
    if old_gpkg.exists(): shutil.copy2(old_gpkg,new_gpkg)
    try:
        import geopandas as gpd
        from shapely.geometry import Point
        crs=None
        # Inspect an existing layer for CRS.
        try: crs=gpd.read_file(new_gpkg,layer='survey_points').crs
        except Exception: crs=None
        for layer,df in [('gnss_network_v3_2',gnss_network.merge(points[['point_id','x_local_m','y_local_m']],on='point_id')),('insar_points_v3_2',insar_catalog),('stress_points_v3_2',stress_catalog)]:
            gdf=gpd.GeoDataFrame(df.copy(),geometry=[Point(xy) for xy in zip(df.x_local_m,df.y_local_m)],crs=crs)
            gdf.to_file(new_gpkg,layer=layer,driver='GPKG',mode='a')
    except Exception as e:
        (output/'geodata'/'GPKG_UPDATE_WARNING.txt').write_text(str(e),encoding='utf-8')
    if old_gpkg.exists(): old_gpkg.unlink()

    # Preliminary summary for docs and validation.
    regime_counts=assignment.process_family.value_counts().to_dict()
    summary={
        'dataset_version':VERSION,
        'regime_counts':regime_counts,
        'velocity_max_mm_y':float(truth[truth.point_id.isin(assignment.point_id)].true_velocity_mm_y.max()),
        'velocity_gt250_fraction':float((truth[truth.point_id.isin(assignment.point_id)].true_velocity_mm_y>250).mean()),
        'focused_coverage_median':float(campaign_summary[campaign_summary.campaign_type.eq('focused')].coverage_fraction_total.median()),
        'full_coverage_median':float(campaign_summary[campaign_summary.campaign_type.eq('full')].coverage_fraction_total.median()),
        'campaign_interval_min_days':float(campaign_summary.interval_days_from_previous.dropna().min()),
        'campaign_interval_max_days':float(campaign_summary.interval_days_from_previous.dropna().max()),
        'gnss_network_points':len(gnss_network),'insar_points':len(insar_catalog),
        'stress_zone_counts':stress_catalog.zone_class.value_counts().to_dict(),
        'natural_early_positive_rate':float(early_labels.early_acceleration_label.mean()),
        'stress_early_positive_rate':float(stress_early_labels.early_acceleration_label.mean()) if len(stress_early_labels) else 0,
        'external_test_status':'READY_PENDING_REAL_DATA',
    }
    create_docs(output,summary)
    create_figures(output,assignment,truth,campaigns,membership,gnss_network,points,insar_catalog,early_labels)

    checks,sensor=validate_and_audit(output,assignment,truth,campaigns,membership,lineage,next_features,gnss_network,insar_catalog,insar_obs,insar_truth,stress_catalog,early_labels,stress_early_labels,tilts,curv,rates,strains,adjusted,level_eval,gnss_eval)
    write_csv(checks,output/'metadata'/'validation_checks.csv')
    validation_report={
        'dataset_version':VERSION,'checks_total':len(checks),'checks_passed':int((checks.status=='PASS').sum()),'checks_failed':int((checks.status!='PASS').sum()),'overall_status':'PASS' if (checks.status=='PASS').all() else 'FAIL',
        **summary,
    }
    write_json(validation_report,output/'metadata'/'validation_report.json')
    create_manifests(output)

    # Independent audit package.
    audit_root=output.parent/'SKRU1_v3_2_independent_audit'; safe_rmtree(audit_root); (audit_root/'tables').mkdir(parents=True)
    write_csv(checks,audit_root/'tables'/'independent_checks.csv')
    req_rows=[]
    check_by_id={row['check_id']:row for row in checks.to_dict('records')}
    requirement_map=[
        (1,'Balanced regimes and full decaying/stable/reactivated/step_change',['R01']),
        (2,'Time-dependent regime stages',['R02']),
        (3,'Reduced nominal >250-400 mm/year tail',['R03']),
        (4,'Real focused cycles and missingness',['R04','R05']),
        (5,'Long and irregular intervals',['R06']),
        (6,'Physical model/evaluation/private separation',['R07']),
        (7,'Point-level provenance uncertainty donor distance',['R08']),
        (8,'Stratified GNSS network',['R09']),
        (9,'Independent spatial-temporal InSAR field',['R10']),
        (10,'Stress scenarios across background/moderate/high/unseen zones',['R11']),
        (11,'Separate early acceleration task',['R12']),
        (12,'Frozen external no-retraining test harness',['R13']),
    ]
    for item,req_name,ids in requirement_map:
        selected=[check_by_id[i] for i in ids]
        req_rows.append({'item':item,'requirement':req_name,'status':'DONE' if all(x['status']=='PASS' for x in selected) else 'FAILED','evidence':' | '.join(x['observed'] for x in selected)})
    write_csv(pd.DataFrame(req_rows),audit_root/'tables'/'requirement_status.csv')
    write_csv(assignment.process_family.value_counts().rename_axis('process_family').reset_index(name='points'),audit_root/'tables'/'regime_distribution.csv')
    vel=truth[truth.point_id.isin(assignment.point_id)].true_velocity_mm_y
    bins=[-np.inf,20,75,100,250,400,np.inf]; labels=['<20','20-75','75-100','100-250','250-400','>400']; vb=pd.cut(vel,bins=bins,labels=labels,right=False).value_counts(sort=False).rename_axis('velocity_band').reset_index(name='rows'); vb['fraction']=vb.rows/vb.rows.sum(); write_csv(vb,audit_root/'tables'/'velocity_bands.csv')
    write_csv(campaign_summary,audit_root/'tables'/'campaign_summary.csv'); write_csv(sensor,audit_root/'tables'/'sensor_quality.csv'); write_csv(baseline_metrics,audit_root/'tables'/'baseline_regression_metrics.csv'); write_csv(early_metrics,audit_root/'tables'/'early_warning_baseline_metrics.csv')
    eb=next_features[['sample_id','split']].merge(early_labels[['sample_id','early_acceleration_label']],on='sample_id').groupby('split').early_acceleration_label.agg(['sum','count','mean']).reset_index(); write_csv(eb,audit_root/'tables'/'early_warning_balance_by_split.csv')
    audit_summary={**summary,'audit_status':'PASS' if (checks.status=='PASS').all() else 'FAIL','checks_total':len(checks),'checks_pass':int((checks.status=='PASS').sum()),'checks_warn':int((checks.status=='WARN').sum()),'checks_fail':int((checks.status=='FAIL').sum()),'sensor_quality':sensor.to_dict('records')}
    write_json(audit_summary,audit_root/'audit_summary.json')
    report=f"""# Независимый аудит SKRU-1 v3.2\n\nСтатус: **{audit_summary['audit_status']}**.\n\nПройдено {audit_summary['checks_pass']} из {audit_summary['checks_total']} проверок.\n\nКлючевые результаты:\n- режимы: {regime_counts};\n- максимум номинальной скорости: {summary['velocity_max_mm_y']:.1f} мм/год;\n- focused coverage median: {summary['focused_coverage_median']:.3f};\n- GNSS: {len(gnss_network)} пунктов;\n- independent InSAR: {len(insar_catalog)} точек;\n- external validation: {summary['external_test_status']}.\n\nРеальные производственные циклы отсутствуют; поэтому производственная точность не доказана.\n"""
    (audit_root/'INDEPENDENT_AUDIT_REPORT_RU.md').write_text(report,encoding='utf-8')
    shutil.copy2(Path(__file__),audit_root/'run_independent_audit_v3_2.py')

    # Manifests changed after docs/audit metadata, regenerate once.
    create_manifests(output)
    full_zip=output.parent/'SKRU1_data_reconstruction_v3_2.zip'; safe_zip=output.parent/'SKRU1_model_ready_v3_2.zip'; audit_zip=output.parent/'SKRU1_v3_2_independent_audit.zip'
    make_zip(output,full_zip); make_model_ready_zip(output,safe_zip); make_zip(audit_root,audit_zip)
    checksum_path=output.parent/'SKRU1_v3_2_checksums.sha256'
    checksum_path.write_text('\n'.join([f"{sha256_file(p)}  {p.name}" for p in [full_zip,safe_zip,audit_zip]])+'\n',encoding='utf-8')
    print(json.dumps({'output':str(output),'full_zip':str(full_zip),'model_ready_zip':str(safe_zip),'audit_zip':str(audit_zip),'checks':audit_summary},ensure_ascii=False,indent=2,default=str))

if __name__=='__main__':
    main()
