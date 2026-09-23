#!/usr/bin/env python3
"""Independent data-release checks from serialized files. No model execution."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from skru1.scenario_simulation import REQUIRED_FEATURES, sha256_file, verify_file


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--dataset",default="data/scenario_simulation_v2")
    ap.add_argument("--output",default="work/data_foundation_v2/independent_validation.json")
    ap.add_argument("--baseline",default="work/data_foundation_v2/baseline_inventory.json")
    args=ap.parse_args()
    d=ROOT/args.dataset; checks={}; parsed=[]
    def read(name,**kwargs):
        parsed.append((d/name).relative_to(ROOT).as_posix())
        return pd.read_csv(d/name,**kwargs)
    def check(name,value):
        checks[name]=bool(value)
        if not value: print("FAIL",name,flush=True)
    m=json.loads((d/"manifest.json").read_text(encoding="utf-8"))
    for section in ["inputs","outputs"]:
        for item in m[section]:
            p=(ROOT if section=="inputs" else d)/item["path"]
            verify_file(p,expected_hash=item["sha256"],expected_size=item["size_bytes"])
    check("manifest_files_match",True)
    f=read("model_features.csv.gz"); h=read("causal_history.csv.gz")
    o=read("evaluator/campaign_truth.csv.gz"); truth=read("evaluator/next_planned_truth.csv.gz")
    splits=read("split_assignments.csv.gz"); cat=read("scenario_catalog.csv",keep_default_na=False)
    check("model_feature_columns_are_safe",set(f)-set(REQUIRED_FEATURES)=={"sample_id","scenario_id","point_id","base_point_id","profile_id","base_profile_id","current_campaign_id","current_date","target_campaign_id","target_date"})
    check("origin_truth_keys_identical",f.sample_id.equals(truth.sample_id))
    check("split_keys_identical",f.sample_id.equals(splits.sample_id))
    check("all_origins_have_at_least_three_real_tokens",(f.n_history>=3).all())
    h=h.sort_values(["point_id","current_date"]).reset_index(drop=True)
    group=h.groupby("point_id",sort=False)
    dt=pd.to_datetime(h.current_date).groupby(h.point_id).diff().dt.days/365.25
    rate=group.last_settlement_mm.diff()/dt
    acceleration=rate.groupby(h.point_id).diff()/((dt+dt.groupby(h.point_id).shift(1))/2)
    check("history_rates_recomputed_from_observations",np.allclose(rate,h.last_rate_mm_y,atol=2e-7,equal_nan=True))
    check("history_accelerations_recomputed_at_interval_midpoints",np.allclose(acceleration,h.recent_acceleration_mm_y2,atol=2e-6,equal_nan=True))
    check("history_counts_recomputed",np.array_equal(group.cumcount()+1,h.n_history))
    obs=o.loc[o.observed].copy()
    observed_keys=set(zip(obs.entity_point_id,obs.date)); history_keys=set(zip(h.point_id,h.current_date))
    check("history_contains_exactly_observed_campaigns",observed_keys==history_keys)
    direct=f.merge(h,on=["point_id","current_date"],suffixes=("_origin","_history"),validate="many_to_one")
    for col in ["last_settlement_mm","last_rate_mm_y","recent_acceleration_mm_y2","n_history","current_standard_uncertainty_mm","missing_campaigns_since_previous","std_last_3_rates_mm_y"]:
        check("origin_matches_causal_history_"+col,np.allclose(direct[col+"_origin"],direct[col+"_history"],equal_nan=True))
    # Check current profile aggregates from observations available at that date only.
    agg=h.groupby(["profile_id","current_date"]).agg(s=("last_settlement_mm","mean"),r=("last_rate_mm_y","mean"),n=("point_id","size"),sd=("last_rate_mm_y",lambda s:s.std(ddof=0)))
    joined=f.merge(agg,on=["profile_id","current_date"],validate="many_to_one")
    for src,dst in [("profile_mean_settlement_mm","s"),("profile_mean_rate_mm_y","r"),("profile_n_observed","n"),("profile_rate_std_mm_y","sd")]:
        check("contemporaneous_"+src,np.allclose(joined[src],joined[dst],atol=2e-7,equal_nan=True))
    # Independent planned-target and observed/latent label arithmetic.
    targeted=o.loc[o.targeted].sort_values(["entity_point_id","date"]).copy()
    targeted["next_planned_campaign"]=targeted.groupby("entity_point_id").campaign_id.shift(-1)
    current=f.merge(targeted,left_on=["point_id","current_campaign_id"],right_on=["entity_point_id","campaign_id"],validate="many_to_one")
    target=f.merge(o,left_on=["point_id","target_campaign_id"],right_on=["entity_point_id","campaign_id"],validate="many_to_one")
    check("next_planned_target_not_next_successful",current.next_planned_campaign.equals(f.target_campaign_id))
    horizon=(pd.to_datetime(f.target_date)-pd.to_datetime(f.current_date)).dt.days/365.25
    expected_latent=(target.latent_settlement_mm-current.latent_settlement_mm)/horizon
    expected_observed=(target.observed_settlement_mm-current.observed_settlement_mm)/horizon
    check("latent_target_math",np.allclose(expected_latent,truth.latent_rate_mm_y,atol=3e-7,equal_nan=True))
    check("observed_target_math_with_missingness",np.allclose(expected_observed,truth.observed_rate_mm_y,atol=3e-7,equal_nan=True))
    check("future_target_not_feature",not any("latent" in c or "observed_rate" in c or "error_mm" in c for c in f))
    for role in ["train","calibration"]:
        labels=read(f"targets/{role}_observed.csv.gz")
        ids=set(splits.loc[splits.model_role.eq(role),"sample_id"])
        check(role+"_labels_exact_split",set(labels.sample_id)==ids and labels.observed_rate_mm_y.notna().all())
    cfg=json.loads((d/"generation_config.json").read_text(encoding="utf-8"))
    train=splits.loc[splits.model_role.eq("train")]; cal=splits.loc[splits.model_role.eq("calibration")]
    check("train_target_date_boundary",train.target_date.le(cfg["time_split"]["train_target_end"]).all())
    check("calibration_target_date_boundary",cal.target_date.between(cfg["time_split"]["calibration_target_start"],cfg["time_split"]["calibration_target_end"]).all())
    check("challenge_absent_from_train_calibration",not splits.loc[splits.model_role.isin(["train","calibration"]),"experiment_role"].eq("heldout_mechanism").any())
    windows=read("sequence_windows.csv.gz")
    histories={point:(g.history_id.tolist(),dict(zip(g.current_date,range(len(g))))) for point,g in h.groupby("point_id",sort=False)}
    sequence_ok=True
    for w in windows.itertuples(index=False):
        ids,positions=histories[w.point_id]; end=positions[w.current_date]+1
        expected=ids[max(0,end-16):end]
        if json.loads(w.history_ids_json)!=expected or w.sequence_length!=len(expected) or w.left_padding+len(expected)!=16:
            sequence_ok=False; break
    check("all_sequence_windows_are_own_latest_past_tokens",sequence_ok and windows.sample_id.equals(f.sample_id))
    # Only compare paired observation replicas, independently of measurement availability.
    oo=o.merge(cat[["scenario_id","latent_world_id"]],on="scenario_id",validate="many_to_one")
    check("measurement_conditions_preserve_identical_latent_truth",oo.groupby(["latent_world_id","base_point_id","date"]).latent_settlement_mm.nunique().eq(1).all())
    pseudo_ids=cat.loc[cat.pseudo_transition,"scenario_id"]
    pseudo=o.loc[o.scenario_id.isin(pseudo_ids)&o.date.ne(o.date.min())]
    check("pseudo_transition_has_constant_true_velocity",pseudo.groupby(["scenario_id","base_point_id"]).latent_rate_mm_y.std().fillna(0).lt(1e-7).all())
    baseline=json.loads((ROOT/args.baseline).read_text(encoding="utf-8")); changed=[]
    for item in baseline:
        p=ROOT/item["path"]
        if not p.is_file() or p.stat().st_size!=item["size_bytes"] or sha256_file(p)!=item["sha256"]: changed.append(item["path"])
    check("all_preexisting_protected_files_unchanged",not changed)
    report=dict(status="PASS" if all(checks.values()) else "FAIL",checks=checks,
        rows_recomputed=len(f),sequence_windows_checked=len(windows),preexisting_files_verified=len(baseline),
        changed_preexisting_files=changed,parsed_data_paths=parsed,legacy_holdout_labels_parsed=0,models_executed=0,
        source_lineage="generator manifest inputs + constraints manifest source/page/reading lineage; hash checked before generation",
        note="This reads only the newly generated synthetic QA labels and authorized public scenario artifacts; not a sealed external holdout.")
    out=ROOT/args.output; out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({k:report[k] for k in ["status","rows_recomputed","sequence_windows_checked","preexisting_files_verified","changed_preexisting_files"]},indent=2))
    if not all(checks.values()): raise SystemExit(1)


if __name__=="__main__": main()
