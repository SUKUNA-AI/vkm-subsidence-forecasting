"""Empirical-envelope-conditioned simulation, deliberately independent of IMM.

Only geometry, planned membership, published constraints and explicit assumptions
are inputs. Evaluation truth never enters the separately written model view.
"""
from __future__ import annotations

import json
import platform
import subprocess
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

from .empirical_constraints_v2 import checked_inputs, empty_destination, inventory, NUMERIC
from .scenario_simulation import (
    YEAR_DAYS, REQUIRED_FEATURES, repository_path, sha256_file, verify_file,
    _read_bool, _targeted_matrix, _missingness, _model_role, _write_csv, _write_json, verify_upstream,
)


def derive_seed(*parts: int) -> int:
    return int(np.random.SeedSequence(list(parts)).generate_state(1)[0])


def temporal_integral(t: np.ndarray, family: str, p: dict) -> np.ndarray:
    """Dimensionless displacement laws; derivatives are nonnegative.

    These are explicit stress laws, not samples from the IMM state equations.
    """
    t = np.asarray(t, dtype=float)
    if family in {"stable", "uniform", "moving_spatial_focus"}:
        return t.copy()
    if family == "exponential_decay":
        return p["base"]*t + p["tau"]*(1-np.exp(-t/p["tau"]))
    if family == "logarithmic_decay":
        return p["base"]*t + p["tau"]*np.log1p(t/p["tau"])
    if family == "smooth_acceleration":
        return p["base"]*t + np.power(t, p["power"])
    if family == "saturating_acceleration":
        k, c = p["steepness"], p["onset"]
        return p["base"]*t + (np.logaddexp(0,k*(t-c))-np.logaddexp(0,-k*c))/k
    if family in {"temporary_acceleration", "reactivation", "smooth_step"}:
        out = p["base"]*t
        for centre, width, weight in p["events"]:
            out = out + weight*(1/(1+np.exp(-(t-centre)/width))-1/(1+np.exp(centre/width)))
        return out
    raise ValueError(f"Unknown process family {family}")


def random_parameters(rng: np.random.Generator, cfg: dict) -> dict:
    ranges = cfg["temporal_parameter_ranges"]
    sample = lambda key: float(rng.uniform(*ranges[key]))
    first, second = sample("event1"), sample("event2")
    width = sample("event_width")
    return dict(base=sample("base"), tau=sample("tau"), power=sample("power"),
                steepness=sample("steepness"), onset=sample("onset"),
                events=[[first,width,.45],[second,width*1.3,.40]])


def catalog(config: dict, profiles: pd.DataFrame) -> pd.DataFrame:
    rows=[]; world=0
    for seed in config["random_seeds"]:
        for fi, family in enumerate(config["process_families"]):
            donors = [None] if family["name"] == "stable" else list(profiles.loc[profiles.numeric_calibration].to_dict("records"))
            for di, donor in enumerate(donors):
                world += 1
                latent_seed=derive_seed(seed,fi,di,100)
                p=random_parameters(np.random.default_rng(latent_seed),config)
                if family["name"]=="temporary_acceleration": p["events"]=p["events"][:1]
                if family["name"]=="smooth_step":
                    p["events"]= [[p["onset"],config["smooth_step_width_fraction"],.5]]
                for ci, condition in enumerate(config["observation_conditions"]):
                    rows.append(dict(
                        scenario_id=f"V2-W{world:03d}-O{ci+1}", latent_world_id=f"V2-W{world:03d}",
                        dynamic_mechanism=family["name"], experiment_role=family["role"],
                        observation_condition=condition["name"], missingness_mechanism=condition["missingness"],
                        measurement_error_mechanism=condition["error"], base_seed=seed,
                        latent_seed=latent_seed, generator_seed=derive_seed(seed,fi,di,ci,200),
                        numeric_constraint_id=donor["constraint_id"] if donor else "",
                        empirical_profile=donor["profile_label"] if donor else "none",
                        empirical_period=donor["interval"] if donor else "none",
                        nominal_interval_years=donor["nominal_interval_years"] if donor else 1,
                        readable_min_magnitude_mm=donor["magnitude_lower_mm"] if donor else 0.,
                        reference_upper_mm=donor["magnitude_upper_mm"] if donor else 0.,
                        # Unknown plotted positions are not filled or treated as minima.
                        reference_floor_mm=(donor["magnitude_lower_mm"] if donor and donor["unresolved"]==0 else 0.),
                        structural_shape=donor["structural_shape"] if donor else "null_control",
                        temporal_parameters_json=json.dumps(p,sort_keys=True,separators=(",",":")),
                        process_motivation=family["motivation"],
                        observation_modality="synthetic_reflector_contamination" if condition["error"]=="thermal_reflector" else "synthetic_leveling_proxy",
                        pseudo_transition=family["name"] in {"stable","uniform"} and condition["error"] in {"thermal_reflector","systematic_shift"},
                        numeric_scale_constrained=donor is not None,
                        temporal_law_empirically_identified=False,
                    ))
    return pd.DataFrame(rows)


def shape_values(x: np.ndarray, kind: str, centre: float, width: float, reverse: bool) -> np.ndarray:
    x = 1-x if reverse else x
    if kind=="monotone_gradient": return np.clip(x,0,1)
    if kind=="two_lobes":
        return np.maximum(np.exp(-.5*((x-(centre-.37))/width)**2),
                          .95*np.exp(-.5*((x-(centre+.37))/width)**2))
    return np.exp(-.5*((x-centre)/width)**2)


def latent_surface(times: np.ndarray, roster: pd.DataFrame, scenario: dict, config: dict,
                   span_years: float) -> tuple[np.ndarray, list[dict]]:
    """Condition an assumed analogue interval, never copy a digitized trajectory.

    One empirical cohort per latent world; no pairing/subtracting different
    published periods. Scale is fixed before any missingness or split operation.
    """
    family=scenario["dynamic_mechanism"]
    p=json.loads(scenario["temporal_parameters_json"])
    duration=float(scenario["nominal_interval_years"])
    a=.5-duration/(2*span_years); b=.5+duration/(2*span_years)
    rng=np.random.default_rng(derive_seed(int(scenario["latent_seed"]),300))
    latent=np.zeros((len(times),len(roster))); log=[]
    for profile, group in roster.groupby("profile_id",sort=True):
        pos=group.index.to_numpy(); x=group.chainage_normalized_profile.to_numpy(float)
        centre=float(rng.uniform(*config["spatial_parameters"]["centre"]))
        width=float(rng.uniform(*config["spatial_parameters"]["width"]))
        if scenario["structural_shape"]=="localized_bowl": width*=config["spatial_parameters"]["localized_width_multiplier"]
        reverse=bool(rng.integers(0,2))
        floor=float(scenario["reference_floor_mm"])
        upper=float(scenario["reference_upper_mm"])
        peak=floor+(upper-floor)*float(rng.uniform(*config["spatial_parameters"]["peak_fraction"]))
        if family=="stable":
            # Exact zero-motion negative control: not a fabricated measured stable point.
            field=np.zeros(len(pos)); values=np.zeros((len(times),len(pos)))
        elif family=="moving_spatial_focus":
            grid=np.linspace(0,1,config["integration_steps"])
            centres=centre+config["spatial_parameters"]["moving_distance"]*(grid-.5)
            v=config["spatial_parameters"]["moving_floor"]+np.exp(-.5*((x[None,:]-centres[:,None])/width)**2)
            cumulative=np.vstack([np.zeros((1,len(pos))),np.cumsum((v[1:]+v[:-1])*.5*np.diff(grid)[:,None],axis=0)])
            wa=np.array([np.interp(a,grid,cumulative[:,j]) for j in range(len(pos))])
            wb=np.array([np.interp(b,grid,cumulative[:,j]) for j in range(len(pos))])
            # Common profile multiplier preserves propagation instead of per-point normalization.
            scale=peak/float((wb-wa).max())
            values=np.column_stack([np.interp(times,grid,cumulative[:,j]) for j in range(len(pos))])*scale
            field=(wb-wa)*scale
        else:
            field=floor+(peak-floor)*shape_values(x,scenario["structural_shape"],centre,width,reverse)
            ref=np.diff(temporal_integral(np.array([a,b]),family,p))[0]
            if not np.isfinite(ref) or ref<=0: raise ValueError("Nonpositive analogue interval")
            values=temporal_integral(times,family,p)[:,None]*field[None,:]/ref
        latent[:,pos]=values
        log.append(dict(latent_world_id=scenario["latent_world_id"],profile_id=profile,
            constraint_id=scenario["numeric_constraint_id"],reference_start_elapsed_years=a*span_years,
            reference_end_elapsed_years=b*span_years,nominal_duration_only=True,
            reference_min_mm=float(field.min()),reference_max_mm=float(field.max()),
            source_readable_upper_with_bound_mm=upper,shape_centre=centre,shape_width=width,
            reversed_order=reverse,sampled_peak_mm=peak,spatial_mapping="none_analogue_shape_only"))
    return latent,log


def observation_arrays(latent: np.ndarray, dates: np.ndarray, types: np.ndarray,
                       targeted: np.ndarray, scenario: dict, config: dict):
    years=np.diff(dates).astype("timedelta64[D]").astype(float)/YEAR_DAYS
    rate=np.vstack([np.zeros((1,latent.shape[1])),np.diff(latent,axis=0)/years[:,None]])
    rng=np.random.default_rng(int(scenario["generator_seed"]))
    name=scenario["missingness_mechanism"]
    if name=="state_dependent":
        # Absolute, preregistered rate scale. No future trajectory maximum.
        p=config["missingness_parameters"][name]
        prob=p["base_probability"]+p["maximum_additional_probability"]*np.clip(np.abs(rate)/p["rate_scale_mm_y"],0,1)
        missing=targeted & (rng.random(targeted.shape)<prob)
        reasons=np.where(missing,"state_dependent_missing","").astype(object)
        reasons[~targeted]="not_targeted"
    else:
        base="independent" if name=="seasonal_reflector" else name
        missing,reasons=_missingness(targeted,rate,base,config["missingness_parameters"][base],
                                    config["warmup_protected_targeted_observations"],rng)
        if name=="seasonal_reflector":
            winter=np.isin(pd.DatetimeIndex(dates).month,config["reflector_missing_months"])
            missing[winter]=targeted[winter]; reasons[missing & winter[:,None]]="reflector_snow_unavailability"
    if name != "seasonal_reflector":
        for j in range(targeted.shape[1]):
            k=np.flatnonzero(targeted[:,j])[:config["warmup_protected_targeted_observations"]]
            missing[k,j]=False; reasons[k,j]=""
    observed=targeted & ~missing
    sigma_rng=np.random.default_rng(derive_seed(int(scenario["latent_seed"]),900))
    m=config["measurement_parameters"]
    base_sigma=float(sigma_rng.choice(m["ordinary_sigma_options_mm"]))
    sigma=np.broadcast_to(np.where(types=="focused",base_sigma*m["focused_multiplier"],base_sigma)[:,None],latent.shape).copy()
    ordinary=rng.normal(0,sigma)
    gross=np.zeros_like(latent); datum=np.zeros_like(latent)
    error=scenario["measurement_error_mechanism"]
    if error=="gross_error":
        flags=rng.random(latent.shape)<m["outlier_probability"]
        gross[flags]=rng.choice([-1.,1.],flags.sum())*rng.uniform(*m["outlier_magnitude_mm"],flags.sum())
    elif error=="systematic_shift":
        t=(dates-dates[0]).astype("timedelta64[D]").astype(float)/YEAR_DAYS
        onset=m["systematic_onset_fraction"]*t[-1]
        bias=np.where(t>=onset,m["systematic_offset_mm"]+m["systematic_drift_mm_y"]*(t-onset),0.)
        datum[:]=bias[:,None]
    elif error=="thermal_reflector":
        # Rise then saturation in spring; true surface is unchanged. No synthetic
        # winter reversal asserted: source has no Nov-Dec data.
        day=pd.DatetimeIndex(dates).dayofyear.to_numpy(float)
        phase=np.clip((day-m["thermal_start_day"])/m["thermal_rise_days"],0,1)
        wave=phase*phase*(3-2*phase)
        amp=float(rng.uniform(*config["thermal_amplitude_source_bounds_mm"]))
        affected=rng.random(latent.shape[1])<m["reflector_fraction"]
        datum[:]=-amp*wave[:,None]*affected[None,:]
    elif error != "ordinary_noise": raise ValueError(error)
    return rate,observed,reasons,sigma,ordinary,gross,datum,(gross!=0)|(datum!=0)


# Origin materialization follows the frozen v1 contract; v2 fixes acceleration time support.
def scenario_frames(
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
    latent, _ = latent_surface(times, point_roster, scenario, config, elapsed_days[-1]/YEAR_DAYS)
    latent_rate, observed, missing_reasons, sigma, ordinary, gross, datum, special_error = observation_arrays(
        latent, dates, campaign_catalog["campaign_type"].astype(str).to_numpy(), targeted, scenario, config)
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
                    "provenance": "SYNTHETIC_OBSERVATION",
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
                ) / ((years + float((dates[previous] - dates[int(observed_indices[position-2])]) / np.timedelta64(1, "D")) / YEAR_DAYS) / 2)

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
                    "provenance": "DERIVED_FROM_SYNTHETIC_OBSERVATION",
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
                    "provenance": "SYNTHETIC_OBSERVATION",
                }
            )
    return (
        pd.DataFrame(observation_rows),
        pd.DataFrame(history_rows),
        pd.DataFrame(sample_rows),
    )


def load_foundation(root: Path, config: dict):
    """Column allowlists avoid every legacy measurement/target value."""
    p=root/config["foundation"]["geometry"]
    geometry=pd.read_csv(p,usecols=["point_id","profile_id","point_type","chainage_m","x_local_m","y_local_m"])
    work=geometry.loc[geometry.point_type.eq("WORK")].copy()
    cuts=(float(work.x_local_m.median()),float(work.y_local_m.median()))
    work["zone_id"]="GEO_"+work.y_local_m.ge(cuts[1]).map({True:"N",False:"S"})+work.x_local_m.ge(cuts[0]).map({True:"E",False:"W"})
    chain=pd.read_csv(root/config["foundation"]["chainage"],usecols=["point_id","chainage_normalized_profile"]).drop_duplicates("point_id")
    work=work.merge(chain,on="point_id",validate="one_to_one")
    groups=[]
    for _,g in work.groupby("profile_id",sort=True):
        g=g.sort_values(["chainage_m","point_id"])
        positions=np.rint(np.linspace(0,len(g)-1,config["points_per_profile"])).astype(int)
        groups.append(g.iloc[positions])
    roster=pd.concat(groups,ignore_index=True).sort_values(["profile_id","point_id"]).reset_index(drop=True)
    membership=pd.read_csv(root/config["foundation"]["membership"],
        usecols=["point_id","campaign_id","date","campaign_type","targeted"])
    membership["targeted"]=_read_bool(membership.targeted,"targeted")
    membership["date"]=pd.to_datetime(membership.date)
    campaigns=membership[["campaign_id","date","campaign_type"]].drop_duplicates().sort_values("date").reset_index(drop=True)
    if campaigns.campaign_id.duplicated().any(): raise ValueError("Duplicate campaign metadata")
    targeted=_targeted_matrix(membership,campaigns.campaign_id.tolist(),roster.point_id.tolist())
    return roster,campaigns,targeted,cuts


MODEL_METADATA=("sample_id","scenario_id","point_id","base_point_id","profile_id","base_profile_id",
                "current_campaign_id","current_date","target_campaign_id","target_date")


def sequence_windows(history: pd.DataFrame, features: pd.DataFrame, max_length: int) -> pd.DataFrame:
    lookup={k:g.sort_values("current_date") for k,g in history.groupby("point_id",sort=False)}
    rows=[]
    for row in features.itertuples(index=False):
        g=lookup[row.point_id]
        end=int(np.searchsorted(g.current_date.to_numpy(str),str(row.current_date),side="right"))
        window=g.iloc[max(0,end-max_length):end]
        ids=window.history_id.tolist()
        rows.append(dict(sample_id=row.sample_id,point_id=row.point_id,current_date=row.current_date,
            sequence_length=len(ids),left_padding=max_length-len(ids),
            history_ids_json=json.dumps(ids,separators=(",",":")),
            representation="causal_observed_tokens_no_interpolation_no_global_normalization"))
    return pd.DataFrame(rows)


def validate_frames(config,cat,obs,hist,samples,features,conditioning,roster,campaigns) -> dict:
    train=samples.loc[samples.model_role.isin(["train","calibration"])]
    errors=(obs.observed_settlement_mm-obs.latent_settlement_mm-obs.ordinary_noise_mm-obs.gross_error_mm-obs.reference_datum_error_mm).dropna()
    observed=obs.loc[obs.observed]
    allowed=list(MODEL_METADATA)+list(REQUIRED_FEATURES)
    planned=obs.loc[obs.targeted,["scenario_id","base_point_id","campaign_id","date"]].sort_values("date")
    planned["next"]=planned.groupby(["scenario_id","base_point_id"]).campaign_id.shift(-1)
    joined=samples.merge(planned,left_on=["scenario_id","base_point_id","current_campaign_id"],right_on=["scenario_id","base_point_id","campaign_id"],validate="many_to_one")
    conditioning=conditioning.loc[conditioning.constraint_id.ne("")]
    checks=dict(
        model_view_is_exact_allowlist=list(features)==allowed,
        sample_ids_unique=samples.sample_id.is_unique,
        observation_keys_unique=not obs.duplicated(["scenario_id","base_point_id","campaign_id"]).any(),
        hidden_truth_absent_from_model_and_history=not any("latent" in c or c.endswith("seed") or "mechanism" in c for c in [*features,*hist]),
        observations_reconcile=bool((errors.abs()<1e-8).all()),
        missing_values_remain_missing=bool(obs.loc[~obs.observed,"observed_settlement_mm"].isna().all()),
        next_planned_target_exact=bool((joined.target_campaign_id==joined["next"]).all()),
        positive_forecast_horizons=bool((samples.forecast_horizon_days>0).all()),
        train_calibration_observed_targets=bool(train.observed_rate_mm_y.notna().all()),
        challenge_mechanisms_not_in_fit=not train.experiment_role.eq("heldout_mechanism").any(),
        history_only_observed_rows=len(hist)==len(observed),
        analogue_interval_upper_bounds=bool((conditioning.reference_max_mm<=conditioning.source_readable_upper_with_bound_mm+1e-8).all()),
        four_proxy_zones=len(roster.zone_id.unique())==4,
        no_legacy_labels_parsed=True,
        no_model_import_or_training=True,
    )
    checks["latent_nondecreasing"]=bool((obs.sort_values("date").groupby(["scenario_id","base_point_id"]).latent_settlement_mm.diff().dropna()>=-1e-8).all())
    report=dict(status="PASS" if all(checks.values()) else "FAIL",checks=checks,
        scenarios=len(cat),latent_worlds=int(cat.latent_world_id.nunique()),points=len(roster),profiles=int(roster.profile_id.nunique()),
        campaigns=len(campaigns),trajectory_instances=len(cat)*len(roster),latent_trajectory_keys=int(cat.latent_world_id.nunique())*len(roster),
        distinct_nonzero_latent_worlds=int(cat.loc[cat.dynamic_mechanism.ne("stable"),"latent_world_id"].nunique()),
        null_controls_share_identical_zero_truth=True,
        campaign_rows=len(obs),targeted_rows=int(obs.targeted.sum()),observed_rows=len(observed),
        missing_targeted_rows=int((obs.targeted & ~obs.observed).sum()),model_origins=len(samples),
        model_role_counts=samples.model_role.value_counts().sort_index().to_dict(),
        process_family_counts=cat.dynamic_mechanism.value_counts().sort_index().to_dict(),
        numeric_scale_constrained_scenario_fraction=float(cat.numeric_scale_constrained.mean()),
        empirically_identified_temporal_law_fraction=0.,real_observation_rows=0,
        temporal_coverage=[str(campaigns.date.min().date()),str(campaigns.date.max().date())],
        models_trained=0,legacy_holdout_labels_loaded=0,field_accuracy_claim=False)
    if report["status"]!="PASS": raise ValueError(report)
    return report


def generate(root: Path, config_path: Path, output: Path) -> dict:
    root=root.resolve(); config_path=config_path.resolve(); output=output.resolve()
    config=json.loads(config_path.read_text(encoding="utf-8"))
    empty_destination(root,output,config["output_directory"])
    inputs=checked_inputs(root,config)
    verify_upstream(root,config["constraints_manifest"],"artifact_id")
    verify_upstream(root,config["source_release"],"dataset_id")
    constraints=pd.read_csv(root/config["constraints_table"],keep_default_na=False)
    profiles=pd.read_csv(root/config["profile_inventory"],dtype={"profile_label":str})
    accepted=set(constraints.loc[(constraints.status==NUMERIC)&(constraints.allowed_use=="interval_displacement_envelope"),"constraint_id"])
    if set(profiles.loc[profiles.numeric_calibration,"constraint_id"])!=accepted:
        raise ValueError("Numeric donor whitelist differs from accepted leveling constraints")
    thermal=constraints.set_index("constraint_id").loc["BAB-THERMAL-SHIFT"]
    if thermal.status!=NUMERIC or thermal.allowed_use!="reflector_contamination_stress_amplitude": raise ValueError("Thermal donor not allowed")
    # Read the numeric empirical value, not an unused manifest reference.
    config["thermal_amplitude_source_bounds_mm"]=[float(thermal.lower_bound),float(thermal.upper_bound)]
    roster,campaigns,targeted,cuts=load_foundation(root,config)
    cat=catalog(config,profiles)
    observations=[]; histories=[]; sample_frames=[]; condition_log=[]
    span=(campaigns.date.iloc[-1]-campaigns.date.iloc[0]).days/YEAR_DAYS
    for i,row in enumerate(cat.to_dict("records")):
        obs,hist,samples=scenario_frames(row,{},config,roster,campaigns,targeted)
        observations.append(obs); histories.append(hist); sample_frames.append(samples)
        if row["observation_condition"]==config["observation_conditions"][0]["name"]:
            _,logs=latent_surface(np.array([0.,1.]),roster,row,config,span); condition_log.extend(logs)
        if (i+1)%100==0: print(f"Generated {i+1}/{len(cat)} scenarios",flush=True)
    obs=pd.concat(observations,ignore_index=True).sort_values(["scenario_id","date","base_point_id"]).reset_index(drop=True)
    hist=pd.concat(histories,ignore_index=True).sort_values(["scenario_id","current_date","base_point_id"]).reset_index(drop=True)
    samples=pd.concat(sample_frames,ignore_index=True).sort_values(["scenario_id","current_date","base_point_id"]).reset_index(drop=True)
    conditioning=pd.DataFrame(condition_log)
    features=samples[[*MODEL_METADATA,*REQUIRED_FEATURES]].copy()
    validation=validate_frames(config,cat,obs,hist,samples,features,conditioning,roster,campaigns)
    sequences=sequence_windows(hist,features,config["sequence_max_length"])
    output.mkdir(parents=True,exist_ok=True)
    (output/"evaluator").mkdir(); (output/"targets").mkdir()
    _write_csv(output/"scenario_catalog.csv",cat)
    _write_csv(output/"point_roster.csv",roster)
    _write_csv(output/"campaign_catalog.csv",campaigns)
    _write_csv(output/"model_features.csv.gz",features)
    _write_csv(output/"causal_history.csv.gz",hist)
    _write_csv(output/"sequence_windows.csv.gz",sequences)
    _write_csv(output/"evaluator/campaign_truth.csv.gz",obs)
    truth_cols=[c for c in samples if c not in REQUIRED_FEATURES]
    _write_csv(output/"evaluator/next_planned_truth.csv.gz",samples[truth_cols])
    _write_csv(output/"evaluator/conditioning_log.csv",conditioning)
    split_cols=["sample_id","scenario_id","model_role","evaluation_scope","experiment_role","target_date"]
    splits=samples[split_cols].copy()
    # Group identity is evaluator metadata and never an estimator input.
    splits["zone_id"]=samples.base_point_id.map(roster.set_index("point_id").zone_id)
    _write_csv(output/"split_assignments.csv.gz",splits)
    for role in ["train","calibration"]:
        _write_csv(output/f"targets/{role}_observed.csv.gz",samples.loc[samples.model_role.eq(role),["sample_id","observed_rate_mm_y"]])
    _write_json(output/"validation_report.json",validation)
    _write_json(output/"distribution_summary.json",distribution_summary(obs,samples,cat,roster))
    _write_json(output/"generation_config.json",config)
    _write_json(output/"feature_contract.json",dict(features=list(REQUIRED_FEATURES),metadata=list(MODEL_METADATA),
        target="average_rate_until_next_planned_targeted_campaign",sequence_max_length=config["sequence_max_length"],
        sequence_padding="left; observation and padding masks required; no interpolation",normalization="future_training_fold_only",
        prohibited="all columns outside the feature allowlist; no IDs, source, parameters, labels or truth as predictors"))
    _write_json(output/"field_provenance.json",dict(
        PUBLISHED="source files only, not rows of the model dataset",
        DIGITIZED="constraints_v2/canonical_digitization.csv; signed source values and reading bounds",
        RECONSTRUCTED="point_roster local x/y geometry, designed point IDs and normalized chainage",
        DERIVED="model_features and causal_history finite differences of synthetic observations",
        SYNTHETIC_LATENT_TRUTH="evaluator/campaign_truth.csv.gz latent_* and next_planned_truth latent_*",
        SYNTHETIC_OBSERVATION="observed_settlement_mm; latent + ordinary + gross + systematic error",
        campaign_calendar="inherited scenario design, not real survey journals",proxy_zone_cuts=dict(x=cuts[0],y=cuts[1]),
        source_profile_to_point_mapping=None,sign_convention="positive_down_mm",source_sign_conversion="minus signed displacement only for magnitude envelopes"))
    _write_json(output/"environment.json",dict(python=platform.python_version(),numpy=np.__version__,pandas=pd.__version__,platform=platform.platform()))
    (output/"README.md").write_text(dataset_card(config,validation),encoding="utf-8",newline="\n")
    code=[Path(__file__),root/"src/skru1/empirical_constraints_v2.py",root/"src/skru1/scenario_simulation.py",root/"scripts/generate_scenario_v2.py"]
    manifest=dict(schema_version=2,dataset_id=config["dataset_id"],source_release_id=config["source_release"]["dataset_id"],
        constraints_version="SKRU1_SCENARIO_CONSTRAINTS_V2",claim_domain="synthetic_publication_envelope_conditioned_not_field_validation",
        source_data_commit=config["source_data_commit"],code_commit=config["code_commit"],code_state="uncommitted_source_hashes_authoritative",
        random_seeds=config["random_seeds"],inputs=inventory(root,inputs+[config_path]+code),
        outputs=[dict(path=p.relative_to(output).as_posix(),sha256=sha256_file(p),size_bytes=p.stat().st_size,deterministic=True) for p in sorted(output.rglob("*")) if p.is_file()],
        release_policy="append_only_refuse_nonempty_destination",models_trained=0,
        legacy_label_access="none; only source geometry/planned membership columns were parsed",
        command="python scripts/generate_scenario_v2.py --root .")
    _write_json(output/"manifest.json",manifest)
    return validation


def dataset_card(config, v):
    return f'''# {config["dataset_id"]}

Готовый data release; обучение моделей не выполнялось. Версия v2 следует за
отдельным сценарием v1 и не заменяет исторический SKRU1_Data_Foundation_v3_2_1.

{v["scenarios"]} сценариев наблюдений, {v["latent_worlds"]} latent-world IDs
(126 ненулевых вариантов и два повтора нулевого контроля), {v["points"]} точек / {v["profiles"]} профилей,
{v["campaigns"]} кампаний, {v["model_origins"]} origin rows. Реплики observation
conditions одной latent world зависимы; число строк не равно числу независимых опытов.

Период: {v["temporal_coverage"]}; positive_down_mm. Геометрия и расписание взяты из
исправленной реконструкции. Все временные значения синтетические. Семь отдельных
leveling profiles/periods Мусихина задают амплитудные огибающие с границами чтения.
Длительности 1 и 5 лет — условная интерпретация годовых подписей. Условное окно
расположено в середине моделируемого времени. Это не восстановление реальных дат,
реперов, скорости или общего datum. Разности опубликованных периодов не вычисляются.

Статическая spatial shape допускает случайные центр, ширину, отражение порядка,
амплитуду; точечные значения опубликованного графика не копируются. Moving focus
интегрирует движущееся поле скорости. Temporal laws не используют IMM, матрицу его
переходов или исторические prediction scores. Их параметры и доли — design assumptions.

model_features.csv.gz — только 16 разрешённых признаков и metadata. causal_history
содержит только реально сгенерированные доступные наблюдения. sequence_windows
содержит ID последних максимум 16 доступных токенов до origin; padding указан явно.
Нормализация и обучение любых sequence controls отложены до отдельного протокола.
targets/train_observed и calibration_observed отделены от evaluator. Пропущенная
следующая плановая цель не заменяется следующей успешной; latent target доступен
только evaluator. Исторические validation/test/final holdout не являются входами.

Сценарии reflector contamination отдельно помечены: 30–50 mm мотивированы
Бабаянцем для отражателей на зданиях, это не ошибка нивелирования. sigma 0.4/0.8/1.6
mm, gross outliers, generic datum shift, waveform/timing и missingness вероятности
остаются инженерными допущениями. Зимние пропуски отражателя не перенесены на обычное
нивелирование. Нулевой stable control намеренно не калибруется по опубликованным точкам.

Разбиение по target date сохранено как отдельный synthetic protocol: train до
2022-10-18, calibration 2023-01-17…2023-11-07, future evaluation с 2024-01-30.
Реактивация и moving focus исключены из fit/calibration. Для будущей nested selection
нужны группировка по latent_world и temporal/profile/zone embargo, см. migration plan.
Открытый при QA synthetic evaluator не является новым запечатанным внешним holdout.

distribution_summary.json содержит распределения оседаний, скоростей, ускорений,
targets, интервалов, uncertainty, missingness, профилей, proxy zones и механизмов.
manifest.json фиксирует входы, выходы, code hashes и seeds. Команда не перезаписывает
заполненный каталог. Для повторения задайте новый --output work/data_foundation_v2/<run>.
См. docs/reports/DATA_FOUNDATION_V2_RU.md, docs/governance/SCENARIO_EXPERIMENT_V2_PROTOCOL.md.
'''


def describe(series: pd.Series) -> dict:
    x=pd.to_numeric(series,errors="coerce").dropna()
    if len(x)==0: return {"n":0}
    return dict(n=len(x),min=float(x.min()),q05=float(x.quantile(.05)),q50=float(x.quantile(.5)),
                mean=float(x.mean()),std=float(x.std()),q95=float(x.quantile(.95)),q99=float(x.quantile(.99)),max=float(x.max()))


def distribution_summary(obs,samples,cat,roster) -> dict:
    ordered=obs.sort_values(["scenario_id","base_point_id","date"]).copy()
    groups=ordered.groupby(["scenario_id","base_point_id"],sort=False)
    dt=pd.to_datetime(ordered.date).groupby([ordered.scenario_id,ordered.base_point_id]).diff().dt.days/YEAR_DAYS
    prevdt=dt.groupby([ordered.scenario_id,ordered.base_point_id]).shift(1)
    acc=groups.latent_rate_mm_y.diff()/((dt+prevdt)/2)
    obs_counts=obs.groupby("base_profile_id")["observed"].agg(["count","sum"])
    return dict(
        grain="campaign-point-condition; paired observation conditions are dependent",
        latent_settlement_mm=describe(obs.latent_settlement_mm),observed_settlement_mm=describe(obs.observed_settlement_mm),
        latent_interval_rate_mm_y=describe(ordered.loc[dt.notna(),"latent_rate_mm_y"]),
        latent_interval_acceleration_mm_y2=describe(acc),target_observed_rate_mm_y=describe(samples.observed_rate_mm_y),
        target_latent_rate_mm_y=describe(samples.latent_rate_mm_y),reported_uncertainty_mm=describe(obs.reported_standard_uncertainty_mm),
        observed_history_gap_days=describe(samples.days_since_previous_observation),forecast_horizon_days=describe(samples.forecast_horizon_days),
        missing_targeted_fraction=float((obs.targeted & ~obs.observed).sum()/obs.targeted.sum()),
        missing_reasons=obs.loc[obs.targeted & ~obs.observed].missing_reason.value_counts().sort_index().to_dict(),
        process_families=cat.dynamic_mechanism.value_counts().sort_index().to_dict(),
        measurement_conditions=cat.measurement_error_mechanism.value_counts().sort_index().to_dict(),
        profile_observation_rows=obs_counts.to_dict(orient="index"),proxy_zone_point_counts=roster.zone_id.value_counts().sort_index().to_dict(),
        transition_diagnostic=dict(definition="absolute change of adjacent interval rates divided by midpoint spacing > 10 mm/year^2; design threshold, not regime label",
                                   fraction=float(acc.abs().gt(10).sum()/acc.notna().sum())),
        empirical_scale_fraction=float(cat.numeric_scale_constrained.mean()),temporal_law_empirically_identified_fraction=0.)

