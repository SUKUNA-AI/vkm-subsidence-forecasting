"""Publication-constrained scenario generator for the corrected SKRU-1 release.

The module keeps latent dynamics, missingness, and measurement error separate.
It produces a next-planned-campaign table compatible with the corrected formal
feature contract without exposing generator truth to estimators.
"""
from __future__ import annotations

import csv
import hashlib
import json
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd


YEAR_DAYS = 365.25
REQUIRED_FEATURES = (
    "n_history",
    "last_settlement_mm",
    "last_rate_mm_y",
    "mean_last_3_rates_mm_y",
    "std_last_3_rates_mm_y",
    "recent_acceleration_mm_y2",
    "current_standard_uncertainty_mm",
    "days_since_previous_observation",
    "forecast_horizon_days",
    "current_campaign_type",
    "missing_campaigns_since_previous",
    "profile_mean_settlement_mm",
    "profile_mean_rate_mm_y",
    "profile_rate_std_mm_y",
    "profile_n_observed",
    "target_campaign_type",
)


@dataclass(frozen=True)
class VerifiedUpstream:
    manifest_path: Path
    manifest: Mapping[str, Any]


def sha256_file(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def repository_path(root: Path, value: str) -> Path:
    relative = Path(value)
    candidate = (root / relative).resolve()
    if relative.is_absolute() or not candidate.is_relative_to(root):
        raise ValueError(f"Path must remain inside the repository: {value}")
    return candidate


def verify_file(path: Path, *, expected_hash: str, expected_size: int | None = None) -> None:
    if not path.is_file():
        raise FileNotFoundError(path)
    if expected_size is not None and path.stat().st_size != int(expected_size):
        raise ValueError(f"Input size mismatch: {path}")
    if sha256_file(path) != expected_hash:
        raise ValueError(f"Input hash mismatch: {path}")


def verify_upstream(root: Path, spec: Mapping[str, Any], id_field: str) -> VerifiedUpstream:
    manifest_path = repository_path(root, str(spec["manifest"]))
    verify_file(
        manifest_path,
        expected_hash=str(spec["sha256"]),
        expected_size=int(spec["size_bytes"]),
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get(id_field) != spec[id_field]:
        raise ValueError(f"Unexpected upstream identifier in {manifest_path}")
    for item in manifest.get("inputs", []):
        input_path = repository_path(root, str(item["path"]))
        verify_file(
            input_path,
            expected_hash=str(item["sha256"]),
            expected_size=item.get("size_bytes"),
        )
    for item in manifest.get("outputs", []):
        output_path = (manifest_path.parent / str(item["path"])).resolve()
        if not output_path.is_relative_to(root):
            raise ValueError(f"Upstream output escapes repository: {output_path}")
        verify_file(
            output_path,
            expected_hash=str(item["sha256"]),
            expected_size=item.get("size_bytes"),
        )
    return VerifiedUpstream(manifest_path=manifest_path, manifest=manifest)


def _read_bool(series: pd.Series, name: str) -> pd.Series:
    mapping = {"true": True, "false": False, "1": True, "0": False}
    normalized = series.astype(str).str.strip().str.lower().map(mapping)
    if normalized.isna().any():
        raise ValueError(f"Invalid boolean values in {name}")
    return normalized.astype(bool)


def _scenario_seed(base_seed: int, mechanism_index: int, condition_index: int) -> int:
    state = np.random.SeedSequence(
        [int(base_seed), int(mechanism_index), int(condition_index)]
    ).generate_state(1)
    return int(state[0])


def _slug(value: str) -> str:
    return value.upper().replace("_", "-")


def build_scenario_catalog(config: Mapping[str, Any]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    index = 0
    for base_seed in config["random_seeds"]:
        for mechanism_index, mechanism in enumerate(config["dynamic_mechanisms"]):
            for condition_index, condition in enumerate(config["observation_conditions"]):
                index += 1
                rows.append(
                    {
                        "scenario_id": f"SCN{index:03d}-{_slug(mechanism['mechanism'])}-{_slug(condition['condition'])}",
                        "dynamic_mechanism": mechanism["mechanism"],
                        "experiment_role": mechanism["experiment_role"],
                        "observation_condition": condition["condition"],
                        "missingness_mechanism": condition["missingness"],
                        "measurement_error_mechanism": condition["measurement_error"],
                        "base_seed": int(base_seed),
                        "generator_seed": _scenario_seed(
                            int(base_seed), mechanism_index, condition_index
                        ),
                        "amplitude_mm": float(mechanism["amplitude_mm"]),
                        "mechanism_parameters_json": json.dumps(
                            mechanism["parameters"],
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        "constraint_ids": ";".join(mechanism["constraint_ids"]),
                        "confidence": mechanism["confidence"],
                    }
                )
    return pd.DataFrame(rows)


def _temporal_factor(times: np.ndarray, mechanism: str, parameters: Mapping[str, Any]) -> np.ndarray:
    if mechanism == "uniform":
        return times.copy()
    if mechanism == "creep_decay":
        tau = float(parameters["tau_fraction"])
        return (1.0 - np.exp(-times / tau)) / (1.0 - np.exp(-1.0 / tau))
    if mechanism == "saturating_acceleration":
        steepness = float(parameters["steepness"])
        midpoint = float(parameters["midpoint_fraction"])
        raw = np.logaddexp(0.0, steepness * (times - midpoint))
        start = np.logaddexp(0.0, -steepness * midpoint)
        end = np.logaddexp(0.0, steepness * (1.0 - midpoint))
        return (raw - start) / (end - start)
    if mechanism == "reactivation":
        width = float(parameters["event_width_fraction"])
        raw = float(parameters["base_weight"]) * times
        for fraction, weight in zip(
            parameters["event_fractions"], parameters["event_weights"], strict=True
        ):
            sigmoid = 1.0 / (1.0 + np.exp(-(times - float(fraction)) / width))
            initial = 1.0 / (1.0 + np.exp(float(fraction) / width))
            raw = raw + float(weight) * (sigmoid - initial)
        return raw / raw[-1]
    raise KeyError(f"Static temporal factor is not defined for {mechanism}")


def _profile_scales(profiles: np.ndarray, config: Mapping[str, Any]) -> np.ndarray:
    unique = sorted(map(str, np.unique(profiles)))
    values = np.linspace(
        float(config["profile_scale_min"]),
        float(config["profile_scale_max"]),
        len(unique),
    )
    mapping = dict(zip(unique, values, strict=True))
    return np.asarray([mapping[str(profile)] for profile in profiles], dtype=float)


def _static_spatial_weights(
    chainage: np.ndarray,
    profiles: np.ndarray,
    config: Mapping[str, Any],
) -> np.ndarray:
    unique = sorted(map(str, np.unique(profiles)))
    indices = {profile: index for index, profile in enumerate(unique)}
    phases = np.asarray([indices[str(profile)] / max(len(unique), 1) for profile in profiles])
    centers = float(config["static_center_base"]) + float(
        config["static_center_profile_wave"]
    ) * np.sin(2.0 * np.pi * phases)
    width = float(config["static_width"])
    floor = float(config["weight_floor"])
    return floor + (1.0 - floor) * np.exp(-0.5 * np.square((chainage - centers) / width))


def _latent_surface(
    times: np.ndarray,
    chainage: np.ndarray,
    profiles: np.ndarray,
    mechanism: Mapping[str, Any],
    spatial_config: Mapping[str, Any],
) -> np.ndarray:
    profile_scale = _profile_scales(profiles, spatial_config)
    amplitude = float(mechanism["amplitude_mm"])
    name = str(mechanism["mechanism"])
    parameters = mechanism["parameters"]
    if name != "moving_spatial_focus":
        temporal = _temporal_factor(times, name, parameters)
        spatial = _static_spatial_weights(chainage, profiles, spatial_config)
        return amplitude * temporal[:, None] * (profile_scale * spatial)[None, :]

    steps = int(parameters["integration_steps"])
    if steps < 101:
        raise ValueError("moving_spatial_focus integration_steps must be at least 101")
    grid = np.linspace(0.0, 1.0, steps)
    centers = float(parameters["center_start"]) + (
        float(parameters["center_end"]) - float(parameters["center_start"])
    ) * grid
    width = float(parameters["width"])
    floor = float(parameters["weight_floor"])
    weights = floor + (1.0 - floor) * np.exp(
        -0.5 * np.square((chainage[None, :] - centers[:, None]) / width)
    )
    step = grid[1] - grid[0]
    cumulative = np.vstack(
        [
            np.zeros((1, len(chainage))),
            np.cumsum(0.5 * (weights[:-1] + weights[1:]) * step, axis=0),
        ]
    )
    normalization = float(np.max(cumulative[-1]))
    cumulative /= normalization
    interpolated = np.column_stack(
        [np.interp(times, grid, cumulative[:, point]) for point in range(len(chainage))]
    )
    return amplitude * interpolated * profile_scale[None, :]


def _campaign_rate(latent: np.ndarray, dates: np.ndarray) -> np.ndarray:
    result = np.full_like(latent, np.nan, dtype=float)
    elapsed = np.diff(dates).astype("timedelta64[D]").astype(float) / YEAR_DAYS
    result[1:] = np.diff(latent, axis=0) / elapsed[:, None]
    result[0] = result[1]
    return result


def _targeted_matrix(
    membership: pd.DataFrame,
    campaign_ids: list[str],
    point_ids: list[str],
) -> np.ndarray:
    subset = membership.loc[
        membership["campaign_id"].isin(campaign_ids)
        & membership["point_id"].isin(point_ids),
        ["campaign_id", "point_id", "targeted"],
    ].copy()
    if subset.duplicated(["campaign_id", "point_id"]).any():
        raise ValueError("Membership has duplicate campaign/point rows")
    pivot = subset.pivot(index="campaign_id", columns="point_id", values="targeted")
    pivot = pivot.reindex(index=campaign_ids, columns=point_ids)
    if pivot.isna().any().any():
        raise ValueError("Membership does not cover the complete selected campaign/point grid")
    return pivot.to_numpy(dtype=bool)


def _missingness(
    targeted: np.ndarray,
    latent_rate: np.ndarray,
    mechanism: str,
    parameters: Mapping[str, Any],
    warmup: int,
    rng: np.random.Generator,
) -> tuple[np.ndarray, np.ndarray]:
    campaigns, points = targeted.shape
    missing = np.zeros_like(targeted, dtype=bool)
    reasons = np.full((campaigns, points), "", dtype=object)
    if mechanism == "independent":
        missing = targeted & (rng.random(targeted.shape) < float(parameters["probability"]))
        reasons[missing] = "independent_missing"
    elif mechanism == "long_gap":
        background = targeted & (
            rng.random(targeted.shape) < float(parameters["background_probability"])
        )
        missing |= background
        reasons[background] = "long_gap_background_missing"
        length = int(parameters["gap_length_targeted_campaigns"])
        for point in range(points):
            planned = np.flatnonzero(targeted[:, point])
            if (
                len(planned) > warmup + length
                and rng.random() < float(parameters["affected_point_fraction"])
            ):
                low = warmup
                high = len(planned) - length + 1
                start = int(rng.integers(low, high))
                selected = planned[start : start + length]
                missing[selected, point] = True
                reasons[selected, point] = "long_gap"
    elif mechanism == "state_dependent":
        maximum = np.nanmax(np.abs(latent_rate), axis=0)
        maximum = np.where(maximum > 0, maximum, 1.0)
        normalized = np.clip(np.abs(latent_rate) / maximum[None, :], 0.0, 1.0)
        probability = float(parameters["base_probability"]) + float(
            parameters["maximum_additional_probability"]
        ) * np.power(normalized, float(parameters["rate_power"]))
        missing = targeted & (rng.random(targeted.shape) < probability)
        reasons[missing] = "state_dependent_missing"
    elif mechanism == "planned_campaign_failure":
        background = targeted & (
            rng.random(targeted.shape) < float(parameters["background_probability"])
        )
        missing |= background
        reasons[background] = "campaign_failure_background_missing"
        for fraction in parameters["campaign_fractions"]:
            index = int(round(float(fraction) * (campaigns - 1)))
            selected = targeted[index]
            missing[index, selected] = True
            reasons[index, selected] = "planned_campaign_failure"
    else:
        raise KeyError(f"Unknown missingness mechanism: {mechanism}")

    for point in range(points):
        planned = np.flatnonzero(targeted[:, point])
        protected = planned[:warmup]
        missing[protected, point] = False
        reasons[protected, point] = ""
    reasons[~targeted] = "not_targeted"
    return missing, reasons


def _measurement_error(
    campaign_types: np.ndarray,
    shape: tuple[int, int],
    mechanism: str,
    config: Mapping[str, Any],
    rng: np.random.Generator,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    base_sigma = float(config["ordinary_sigma_mm"])
    multiplier = float(config["focused_campaign_sigma_multiplier"])
    sigma_by_campaign = np.where(campaign_types == "focused", base_sigma * multiplier, base_sigma)
    sigma = np.broadcast_to(sigma_by_campaign[:, None], shape).copy()
    ordinary = rng.normal(0.0, sigma)
    gross = np.zeros(shape, dtype=float)
    datum = np.zeros(shape, dtype=float)
    gross_flag = np.zeros(shape, dtype=bool)
    datum_flag = np.zeros(shape, dtype=bool)
    if mechanism == "gross_error":
        parameters = config["gross_error"]
        gross_flag = rng.random(shape) < float(parameters["probability"])
        low, high = map(float, parameters["absolute_magnitude_mm"])
        gross[gross_flag] = (
            rng.choice(np.asarray([-1.0, 1.0]), size=int(gross_flag.sum()))
            * rng.uniform(low, high, size=int(gross_flag.sum()))
        )
    elif mechanism == "reference_datum_failure":
        parameters = config["reference_datum_failure"]
        start = int(round(float(parameters["start_fraction"]) * (shape[0] - 1)))
        progress = np.linspace(0.0, 1.0, shape[0] - start)
        campaign_bias = float(parameters["offset_mm"]) + float(
            parameters["terminal_drift_mm"]
        ) * progress
        datum[start:] = campaign_bias[:, None]
        datum_flag[start:] = True
    elif mechanism != "ordinary_noise":
        raise KeyError(f"Unknown measurement error mechanism: {mechanism}")
    return sigma, ordinary, gross, datum, gross_flag | datum_flag


def _model_role(
    target_date: pd.Timestamp,
    experiment_role: str,
    target_observed: bool,
    split: Mapping[str, Any],
) -> tuple[str, str]:
    if experiment_role == "heldout_mechanism":
        return "evaluation", "heldout_mechanism"
    train_end = pd.Timestamp(split["train_target_end"])
    calibration_start = pd.Timestamp(split["calibration_target_start"])
    calibration_end = pd.Timestamp(split["calibration_target_end"])
    evaluation_start = pd.Timestamp(split["development_evaluation_target_start"])
    if target_date <= train_end:
        return (
            ("train", "development_train")
            if target_observed
            else ("excluded", "development_train_missing_target")
        )
    if calibration_start <= target_date <= calibration_end:
        return (
            ("calibration", "development_calibration")
            if target_observed
            else ("excluded", "development_calibration_missing_target")
        )
    if target_date >= evaluation_start:
        return "evaluation", "development_future"
    return "excluded", "outside_frozen_time_roles"


def _scenario_frames(
    scenario: Mapping[str, Any],
    mechanism: Mapping[str, Any],
    config: Mapping[str, Any],
    point_roster: pd.DataFrame,
    campaign_catalog: pd.DataFrame,
    targeted: np.ndarray,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    point_ids = point_roster["point_id"].astype(str).tolist()
    profiles = point_roster["profile_id"].astype(str).to_numpy()
    chainage = pd.to_numeric(
        point_roster["chainage_normalized_profile"], errors="coerce"
    ).to_numpy(float)
    if not np.isfinite(chainage).all():
        raise ValueError("Selected point roster has invalid normalized chainage")
    dates = campaign_catalog["date"].to_numpy(dtype="datetime64[ns]")
    elapsed_days = (dates - dates[0]).astype("timedelta64[D]").astype(float)
    times = elapsed_days / elapsed_days[-1]
    latent = _latent_surface(times, chainage, profiles, mechanism, config["spatial_model"])
    latent_rate = _campaign_rate(latent, dates)
    rng = np.random.default_rng(int(scenario["generator_seed"]))
    missing_parameters = config["missingness_parameters"][scenario["missingness_mechanism"]]
    missing, missing_reasons = _missingness(
        targeted,
        latent_rate,
        str(scenario["missingness_mechanism"]),
        missing_parameters,
        int(config["warmup_protected_targeted_observations"]),
        rng,
    )
    observed = targeted & ~missing
    sigma, ordinary, gross, datum, special_error = _measurement_error(
        campaign_catalog["campaign_type"].astype(str).to_numpy(),
        latent.shape,
        str(scenario["measurement_error_mechanism"]),
        config["measurement_error_parameters"],
        rng,
    )
    total_error = ordinary + gross + datum
    measured = latent + total_error

    observation_rows: list[dict[str, Any]] = []
    campaign_ids = campaign_catalog["campaign_id"].astype(str).tolist()
    campaign_types = campaign_catalog["campaign_type"].astype(str).tolist()
    for campaign_index, campaign_id in enumerate(campaign_ids):
        date = pd.Timestamp(dates[campaign_index]).date().isoformat()
        for point_index, base_point_id in enumerate(point_ids):
            is_observed = bool(observed[campaign_index, point_index])
            observation_rows.append(
                {
                    "scenario_id": scenario["scenario_id"],
                    "entity_point_id": f"{scenario['scenario_id']}::{base_point_id}",
                    "base_point_id": base_point_id,
                    "base_profile_id": profiles[point_index],
                    "campaign_id": campaign_id,
                    "date": date,
                    "campaign_type": campaign_types[campaign_index],
                    "targeted": bool(targeted[campaign_index, point_index]),
                    "observed": is_observed,
                    "missing_reason": str(missing_reasons[campaign_index, point_index]),
                    "latent_settlement_mm": latent[campaign_index, point_index],
                    "latent_rate_mm_y": latent_rate[campaign_index, point_index],
                    "observed_settlement_mm": measured[campaign_index, point_index]
                    if is_observed
                    else np.nan,
                    "reported_standard_uncertainty_mm": sigma[campaign_index, point_index]
                    if is_observed
                    else np.nan,
                    "ordinary_noise_mm": ordinary[campaign_index, point_index]
                    if is_observed
                    else np.nan,
                    "gross_error_mm": gross[campaign_index, point_index]
                    if is_observed
                    else np.nan,
                    "reference_datum_error_mm": datum[campaign_index, point_index]
                    if is_observed
                    else np.nan,
                    "special_error_applied": bool(special_error[campaign_index, point_index])
                    if is_observed
                    else False,
                    "dynamic_mechanism": scenario["dynamic_mechanism"],
                    "missingness_mechanism": scenario["missingness_mechanism"],
                    "measurement_error_mechanism": scenario["measurement_error_mechanism"],
                    "generator_seed": int(scenario["generator_seed"]),
                    "provenance": "synthetic_scenario_v1",
                }
            )

    rates = np.full_like(measured, np.nan, dtype=float)
    accelerations = np.full_like(measured, np.nan, dtype=float)
    history_count = np.zeros_like(measured, dtype=int)
    days_since_previous = np.full_like(measured, np.nan, dtype=float)
    missing_since_previous = np.full_like(measured, np.nan, dtype=float)
    mean_last_three = np.full_like(measured, np.nan, dtype=float)
    std_last_three = np.full_like(measured, np.nan, dtype=float)
    for point_index in range(len(point_ids)):
        planned = np.flatnonzero(targeted[:, point_index])
        planned_position = {int(value): position for position, value in enumerate(planned)}
        observed_indices = np.flatnonzero(observed[:, point_index])
        observed_rates: list[float] = []
        for position, campaign_index in enumerate(observed_indices):
            history_count[campaign_index, point_index] = position + 1
            if position == 0:
                continue
            previous = int(observed_indices[position - 1])
            years = float((dates[campaign_index] - dates[previous]) / np.timedelta64(1, "D")) / YEAR_DAYS
            rate = (measured[campaign_index, point_index] - measured[previous, point_index]) / years
            rates[campaign_index, point_index] = rate
            observed_rates.append(float(rate))
            recent = np.asarray(observed_rates[-3:], dtype=float)
            mean_last_three[campaign_index, point_index] = float(np.mean(recent))
            std_last_three[campaign_index, point_index] = float(np.std(recent, ddof=0))
            days_since_previous[campaign_index, point_index] = years * YEAR_DAYS
            missing_since_previous[campaign_index, point_index] = max(
                planned_position[int(campaign_index)] - planned_position[previous] - 1,
                0,
            )
            if len(observed_rates) >= 2:
                accelerations[campaign_index, point_index] = (
                    observed_rates[-1] - observed_rates[-2]
                ) / years

    history_rows: list[dict[str, Any]] = []
    for campaign_index, campaign_id in enumerate(campaign_ids):
        for point_index, base_point_id in enumerate(point_ids):
            if not observed[campaign_index, point_index]:
                continue
            history_rows.append(
                {
                    "history_id": f"HIST::{scenario['scenario_id']}::{base_point_id}::{campaign_id}",
                    "scenario_id": scenario["scenario_id"],
                    "point_id": f"{scenario['scenario_id']}::{base_point_id}",
                    "base_point_id": base_point_id,
                    "profile_id": f"{scenario['scenario_id']}::{profiles[point_index]}",
                    "base_profile_id": profiles[point_index],
                    "campaign_id": campaign_id,
                    "current_date": pd.Timestamp(dates[campaign_index]).date().isoformat(),
                    "last_settlement_mm": measured[campaign_index, point_index],
                    "last_rate_mm_y": rates[campaign_index, point_index],
                    "current_standard_uncertainty_mm": sigma[campaign_index, point_index],
                    "recent_acceleration_mm_y2": accelerations[campaign_index, point_index],
                    "std_last_3_rates_mm_y": std_last_three[campaign_index, point_index],
                    "missing_campaigns_since_previous": missing_since_previous[campaign_index, point_index],
                    "n_history": int(history_count[campaign_index, point_index]),
                    "provenance": "synthetic_scenario_v1_origin_history",
                }
            )

    sample_rows: list[dict[str, Any]] = []
    minimum_history = int(config["minimum_observed_history"])
    for point_index, base_point_id in enumerate(point_ids):
        planned = np.flatnonzero(targeted[:, point_index])
        planned_position = {int(value): position for position, value in enumerate(planned)}
        for current_index in np.flatnonzero(observed[:, point_index]):
            current_index = int(current_index)
            if history_count[current_index, point_index] < minimum_history:
                continue
            position = planned_position[current_index]
            if position + 1 >= len(planned):
                continue
            target_index = int(planned[position + 1])
            horizon_days = int(
                (dates[target_index] - dates[current_index]) / np.timedelta64(1, "D")
            )
            target_is_observed = bool(observed[target_index, point_index])
            horizon_years = horizon_days / YEAR_DAYS
            latent_increment = latent[target_index, point_index] - latent[current_index, point_index]
            latent_target_rate = latent_increment / horizon_years
            observed_increment = (
                measured[target_index, point_index] - measured[current_index, point_index]
                if target_is_observed
                else np.nan
            )
            observed_target_rate = observed_increment / horizon_years if target_is_observed else np.nan
            target_date = pd.Timestamp(dates[target_index])
            model_role, evaluation_scope = _model_role(
                target_date,
                str(scenario["experiment_role"]),
                target_is_observed,
                config["time_split"],
            )
            profile_mask = (profiles == profiles[point_index]) & observed[current_index]
            profile_settlement = measured[current_index, profile_mask]
            profile_rates = rates[current_index, profile_mask]
            profile_rates = profile_rates[np.isfinite(profile_rates)]
            if not np.isfinite(rates[current_index, point_index]):
                raise ValueError("Eligible origin has no finite last rate")
            sample_id = (
                f"{scenario['scenario_id']}::{base_point_id}::"
                f"{campaign_ids[current_index]}::{campaign_ids[target_index]}"
            )
            sample_rows.append(
                {
                    "sample_id": sample_id,
                    "scenario_id": scenario["scenario_id"],
                    "point_id": f"{scenario['scenario_id']}::{base_point_id}",
                    "base_point_id": base_point_id,
                    "profile_id": f"{scenario['scenario_id']}::{profiles[point_index]}",
                    "base_profile_id": profiles[point_index],
                    "current_campaign_id": campaign_ids[current_index],
                    "current_date": pd.Timestamp(dates[current_index]).date().isoformat(),
                    "target_campaign_id": campaign_ids[target_index],
                    "target_date": target_date.date().isoformat(),
                    "split": model_role,
                    "model_role": model_role,
                    "evaluation_scope": evaluation_scope,
                    "experiment_role": scenario["experiment_role"],
                    "dynamic_mechanism": scenario["dynamic_mechanism"],
                    "missingness_mechanism": scenario["missingness_mechanism"],
                    "measurement_error_mechanism": scenario["measurement_error_mechanism"],
                    "generator_seed": int(scenario["generator_seed"]),
                    "n_history": int(history_count[current_index, point_index]),
                    "last_settlement_mm": measured[current_index, point_index],
                    "last_rate_mm_y": rates[current_index, point_index],
                    "mean_last_3_rates_mm_y": mean_last_three[current_index, point_index],
                    "std_last_3_rates_mm_y": std_last_three[current_index, point_index],
                    "recent_acceleration_mm_y2": accelerations[current_index, point_index],
                    "current_standard_uncertainty_mm": sigma[current_index, point_index],
                    "days_since_previous_observation": int(days_since_previous[current_index, point_index]),
                    "forecast_horizon_days": horizon_days,
                    "current_campaign_type": campaign_types[current_index],
                    "missing_campaigns_since_previous": int(missing_since_previous[current_index, point_index]),
                    "profile_mean_settlement_mm": float(np.mean(profile_settlement)),
                    "profile_mean_rate_mm_y": float(np.mean(profile_rates)),
                    "profile_rate_std_mm_y": float(np.std(profile_rates, ddof=0)),
                    "profile_n_observed": int(profile_mask.sum()),
                    "target_campaign_type": campaign_types[target_index],
                    "target_observed_available": target_is_observed,
                    "target_missing_reason": str(missing_reasons[target_index, point_index]),
                    "current_observed_settlement_mm": measured[current_index, point_index],
                    "target_observed_settlement_mm": measured[target_index, point_index]
                    if target_is_observed
                    else np.nan,
                    "observed_increment_mm": observed_increment,
                    "observed_rate_mm_y": observed_target_rate,
                    "latent_current_settlement_mm": latent[current_index, point_index],
                    "latent_target_settlement_mm": latent[target_index, point_index],
                    "latent_increment_mm": latent_increment,
                    "latent_rate_mm_y": latent_target_rate,
                    "source_release_id": config["source_release"]["dataset_id"],
                    "provenance": "synthetic_scenario_v1",
                }
            )
    return (
        pd.DataFrame(observation_rows),
        pd.DataFrame(history_rows),
        pd.DataFrame(sample_rows),
    )


def _validate(
    config: Mapping[str, Any],
    catalog: pd.DataFrame,
    observations: pd.DataFrame,
    history: pd.DataFrame,
    samples: pd.DataFrame,
    allowed_features: tuple[str, ...],
    point_count: int,
    campaign_count: int,
) -> dict[str, Any]:
    training = samples.loc[samples["model_role"].eq("train")]
    calibration = samples.loc[samples["model_role"].eq("calibration")]
    evaluation = samples.loc[samples["model_role"].eq("evaluation")]
    heldout = set(
        catalog.loc[catalog["experiment_role"].eq("heldout_mechanism"), "dynamic_mechanism"]
    )
    numeric_truth = samples[
        ["latent_current_settlement_mm", "latent_target_settlement_mm", "latent_increment_mm", "latent_rate_mm_y"]
    ].apply(pd.to_numeric, errors="coerce")
    observed_rows = observations.loc[observations["observed"]]
    error_sum = (
        observed_rows["ordinary_noise_mm"]
        + observed_rows["gross_error_mm"]
        + observed_rows["reference_datum_error_mm"]
    )
    error_actual = observed_rows["observed_settlement_mm"] - observed_rows["latent_settlement_mm"]
    expected_next: dict[tuple[str, str, str], str] = {}
    planned = observations.loc[
        observations["targeted"],
        ["scenario_id", "base_point_id", "campaign_id", "date"],
    ]
    for (scenario_id, point_id), group in planned.groupby(
        ["scenario_id", "base_point_id"], sort=False
    ):
        ordered = group.sort_values(["date", "campaign_id"], kind="mergesort")
        campaign_ids = ordered["campaign_id"].astype(str).tolist()
        for current, target in zip(campaign_ids[:-1], campaign_ids[1:], strict=True):
            expected_next[(str(scenario_id), str(point_id), current)] = target
    next_planned_exact = all(
        expected_next.get(
            (str(row.scenario_id), str(row.base_point_id), str(row.current_campaign_id))
        )
        == str(row.target_campaign_id)
        for row in samples.itertuples(index=False)
    )
    checks = {
        "all_upstream_inputs_hash_verified_before_generation": True,
        "scenario_ids_unique": not catalog["scenario_id"].duplicated().any(),
        "all_dynamic_mechanisms_present": set(catalog["dynamic_mechanism"])
        == {item["mechanism"] for item in config["dynamic_mechanisms"]},
        "all_missingness_mechanisms_present": set(catalog["missingness_mechanism"])
        == set(config["missingness_parameters"]),
        "all_measurement_error_mechanisms_present": set(catalog["measurement_error_mechanism"])
        == {"ordinary_noise", "gross_error", "reference_datum_failure"},
        "latent_surface_monotone_nondecreasing": observations.sort_values(
            ["scenario_id", "entity_point_id", "date"]
        ).groupby(["scenario_id", "entity_point_id"])["latent_settlement_mm"].diff().dropna().ge(-1e-9).all(),
        "observation_error_components_reconcile": np.allclose(error_sum, error_actual, atol=1e-9, rtol=0),
        "unobserved_measurements_are_blank": observations.loc[
            ~observations["observed"], "observed_settlement_mm"
        ].isna().all(),
        "causal_history_has_all_eligible_origins": set(
            zip(samples["point_id"], samples["current_date"], strict=False)
        ).issubset(set(zip(history["point_id"], history["current_date"], strict=False))),
        "causal_history_contains_no_latent_truth": not any(
            column.startswith("latent_") for column in history.columns
        ),
        "next_planned_targets_never_substituted": next_planned_exact
        and samples["forecast_horizon_days"].gt(0).all(),
        "missing_next_planned_targets_retained_for_latent_evaluation": (
            samples["model_role"].eq("evaluation")
            & ~samples["target_observed_available"]
        ).any(),
        "hidden_truth_complete_for_evaluation": numeric_truth.loc[evaluation.index].notna().all().all(),
        "training_and_calibration_targets_observed": training["observed_rate_mm_y"].notna().all()
        and calibration["observed_rate_mm_y"].notna().all(),
        "heldout_mechanisms_absent_from_fit_and_calibration": heldout.isdisjoint(
            set(training["dynamic_mechanism"]) | set(calibration["dynamic_mechanism"])
        ),
        "formal_feature_contract_preserved": allowed_features == REQUIRED_FEATURES,
        "all_formal_features_materialized": set(allowed_features).issubset(samples.columns),
        "prohibited_generator_fields_not_features": set(config["prohibited_model_features"]).isdisjoint(
            allowed_features
        ),
        "no_legacy_test_or_holdout_loaded": True,
    }
    return {
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": {key: bool(value) for key, value in checks.items()},
        "scenario_count": int(len(catalog)),
        "point_count": int(point_count),
        "profile_count": int(samples["base_profile_id"].nunique()),
        "campaign_count": int(campaign_count),
        "campaign_observation_rows": int(len(observations)),
        "causal_history_rows": int(len(history)),
        "targeted_rows": int(observations["targeted"].sum()),
        "observed_rows": int(observations["observed"].sum()),
        "missing_targeted_rows": int((observations["targeted"] & ~observations["observed"]).sum()),
        "sample_rows": int(len(samples)),
        "model_role_counts": {
            str(key): int(value)
            for key, value in samples["model_role"].value_counts().sort_index().items()
        },
        "evaluation_scope_counts": {
            str(key): int(value)
            for key, value in samples["evaluation_scope"].value_counts().sort_index().items()
        },
        "heldout_dynamic_mechanisms": sorted(heldout),
        "settlement_sign_convention": config["settlement_sign_convention"],
        "models_trained": 0,
        "legacy_test_rows_loaded": 0,
        "external_holdout_rows_loaded": 0,
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "pandas": pd.__version__,
        },
    }


def _write_csv(path: Path, frame: pd.DataFrame) -> None:
    compression: dict[str, Any] | None = None
    if path.suffix == ".gz":
        compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    frame.to_csv(
        path,
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
        float_format="%.9f",
        na_rep="",
        quoting=csv.QUOTE_MINIMAL,
        compression=compression,
    )


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def generate_scenario_dataset(
    root: Path,
    config_path: Path,
    output: Path,
    *,
    script_path: Path,
) -> dict[str, Any]:
    root = root.resolve()
    config_path = config_path.resolve()
    output = output.resolve()
    if not config_path.is_relative_to(root) or not output.is_relative_to(root):
        raise ValueError("Config and output must remain inside the repository")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    declared = repository_path(root, str(config["output_directory"]))
    work_root = (root / "work/scenario_simulation_v1").resolve()
    if output != declared and not output.is_relative_to(work_root):
        raise ValueError("Output must be the declared artifact or work/scenario_simulation_v1")

    source = verify_upstream(root, config["source_release"], "dataset_id")
    constraints = verify_upstream(root, config["scenario_constraints"], "artifact_id")
    source_dir = source.manifest_path.parent
    membership_path = source_dir / config["source_tables"]["membership"]
    features_path = source_dir / config["source_tables"]["features"]
    contract_path = source_dir / config["source_tables"]["feature_contract"]

    membership = pd.read_csv(membership_path, dtype=str, keep_default_na=False)
    membership["targeted"] = _read_bool(membership["targeted"], "targeted")
    membership["date"] = pd.to_datetime(membership["date"], errors="raise")
    features = pd.read_csv(features_path, dtype=str, keep_default_na=False)
    contract = pd.read_csv(contract_path, dtype=str, keep_default_na=False)
    allowed = tuple(
        contract.loc[contract["allowed"].str.lower().eq("true"), "field"].astype(str)
    )
    if allowed != REQUIRED_FEATURES:
        raise ValueError("Corrected formal feature contract changed")

    point_roster = (
        features.loc[
            features["chainage_normalized_profile"].ne(""),
            ["point_id", "profile_id", "chainage_m", "chainage_normalized_profile"],
        ]
        .drop_duplicates("point_id", keep="first")
        .sort_values(["profile_id", "chainage_normalized_profile", "point_id"], kind="mergesort")
        .reset_index(drop=True)
    )
    if point_roster["point_id"].duplicated().any() or point_roster.empty:
        raise ValueError("Unable to build a unique point roster from the corrected release")
    point_roster["_chainage_numeric"] = pd.to_numeric(
        point_roster["chainage_normalized_profile"], errors="coerce"
    )
    if point_roster["_chainage_numeric"].isna().any():
        raise ValueError("Point roster has invalid chainage")
    selected_groups: list[pd.DataFrame] = []
    points_per_profile = int(config["point_selection"]["points_per_profile"])
    for _, group in point_roster.groupby("profile_id", sort=True):
        ordered = group.sort_values(["_chainage_numeric", "point_id"], kind="mergesort")
        if len(ordered) < points_per_profile:
            raise ValueError("A profile has fewer points than the frozen selection requires")
        positions = np.rint(np.linspace(0, len(ordered) - 1, points_per_profile)).astype(int)
        if len(set(positions)) != points_per_profile:
            raise ValueError("Frozen point selection produced duplicate positions")
        selected_groups.append(ordered.iloc[positions])
    point_roster = (
        pd.concat(selected_groups, ignore_index=True)
        .drop(columns="_chainage_numeric")
        .sort_values(["profile_id", "point_id"], kind="mergesort")
        .reset_index(drop=True)
    )
    campaign_catalog = (
        membership[["campaign_id", "date", "campaign_type"]]
        .drop_duplicates()
        .sort_values(["date", "campaign_id"], kind="mergesort")
        .reset_index(drop=True)
    )
    if campaign_catalog["campaign_id"].duplicated().any():
        raise ValueError("Campaign metadata are not unique")
    campaign_ids = campaign_catalog["campaign_id"].astype(str).tolist()
    point_ids = point_roster["point_id"].astype(str).tolist()
    targeted = _targeted_matrix(membership, campaign_ids, point_ids)

    catalog = build_scenario_catalog(config)
    mechanisms = {item["mechanism"]: item for item in config["dynamic_mechanisms"]}
    observation_frames: list[pd.DataFrame] = []
    history_frames: list[pd.DataFrame] = []
    sample_frames: list[pd.DataFrame] = []
    for scenario in catalog.to_dict(orient="records"):
        observations, history, samples = _scenario_frames(
            scenario,
            mechanisms[str(scenario["dynamic_mechanism"])],
            config,
            point_roster,
            campaign_catalog,
            targeted,
        )
        observation_frames.append(observations)
        history_frames.append(history)
        sample_frames.append(samples)
    observations = pd.concat(observation_frames, ignore_index=True)
    history = pd.concat(history_frames, ignore_index=True)
    samples = pd.concat(sample_frames, ignore_index=True)
    observations = observations.sort_values(
        ["scenario_id", "date", "base_profile_id", "base_point_id"], kind="mergesort"
    ).reset_index(drop=True)
    history = history.sort_values(
        ["scenario_id", "current_date", "base_profile_id", "base_point_id"], kind="mergesort"
    ).reset_index(drop=True)
    samples = samples.sort_values(
        ["scenario_id", "current_date", "base_profile_id", "base_point_id"], kind="mergesort"
    ).reset_index(drop=True)
    if samples["sample_id"].duplicated().any():
        raise ValueError("Generated sample IDs are not unique")
    validation = _validate(
        config,
        catalog,
        observations,
        history,
        samples,
        allowed,
        len(point_roster),
        len(campaign_catalog),
    )
    if validation["status"] != "PASS":
        raise ValueError(f"Scenario validation failed: {validation['checks']}")

    output.mkdir(parents=True, exist_ok=True)
    split_assignments = samples[
        [
            "sample_id",
            "scenario_id",
            "model_role",
            "evaluation_scope",
            "experiment_role",
            "dynamic_mechanism",
            "target_observed_available",
            "target_date",
        ]
    ].copy()
    _write_csv(output / "scenario_catalog.csv", catalog)
    _write_csv(output / "campaign_observations.csv.gz", observations)
    _write_csv(output / "causal_history.csv.gz", history)
    _write_csv(output / "next_planned_samples.csv.gz", samples)
    _write_csv(output / "split_assignments.csv.gz", split_assignments)
    _write_json(output / "validation_report.json", validation)
    readme = f"""# {config['dataset_id']}

Новый сценарный выпуск на основе реестра точек, профилей и плановых кампаний
исправленного `SKRU1_RECONSTRUCTION_RESEARCH_V1`. Это искусственные наблюдения,
а не восстановленная полевая история.

Сформировано {validation['scenario_count']} сценариев: пять механизмов динамики,
четыре условия наблюдения, {validation['point_count']} точек и
{validation['campaign_count']} кампаний. Скрытая поверхность, пропуски и ошибки
измерения записаны раздельно. Реактивация и движущийся пространственный очаг не
используются для fit/калибровки и служат механизмами-вызовами.

`next_planned_samples.csv.gz` материализует ровно 16 разрешённых признаков
исправленного формального контракта. Столбцы `latent_*`, механизм и seed нужны
только для аудита/оценки и запрещены как входы модели. Пропущенная следующая
плановая цель не заменяется последующей успешной.

`causal_history.csv.gz` содержит только доступные на соответствующую дату
наблюдения и производные признаки фильтра; скрытые значения в него не входят.

Воспроизведение:

```powershell
.\\.venv\\Scripts\\python.exe scripts\\generate_scenario_v1.py --root .
```

Граница выводов и разбиения описаны в
`docs/governance/SCENARIO_EXPERIMENT_V1_PROTOCOL.md`.
"""
    (output / "README.md").write_text(readme, encoding="utf-8", newline="\n")

    module_path = Path(__file__).resolve()
    input_paths = [
        source.manifest_path,
        constraints.manifest_path,
        membership_path,
        features_path,
        contract_path,
        config_path,
        module_path,
        script_path.resolve(),
    ]
    output_names = [
        "README.md",
        "scenario_catalog.csv",
        "campaign_observations.csv.gz",
        "causal_history.csv.gz",
        "next_planned_samples.csv.gz",
        "split_assignments.csv.gz",
        "validation_report.json",
    ]
    manifest = {
        "schema_version": 1,
        "dataset_id": config["dataset_id"],
        "source_data_commit": config["source_data_commit"],
        "claim_domain": config["claim_domain"],
        "command": "python scripts/generate_scenario_v1.py --root .",
        "inputs": [
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
            for path in input_paths
        ],
        "outputs": [
            {
                "path": name,
                "sha256": sha256_file(output / name),
                "size_bytes": (output / name).stat().st_size,
                "deterministic": True,
            }
            for name in output_names
        ],
    }
    _write_json(output / "manifest.json", manifest)
    return validation
