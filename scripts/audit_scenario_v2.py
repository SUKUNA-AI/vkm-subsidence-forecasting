#!/usr/bin/env python3
"""Reader-facing data diagnostics. Reads public scenario v1 and new synthetic QA truth.

Does not load historical Gate B/C targets, models, predictions or holdout labels.
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
os.environ.setdefault("MPLCONFIGDIR",str(ROOT/"work/data_foundation_v2/mpl"))
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from skru1.scenario_simulation import sha256_file, verify_file, _write_json, _write_csv
from skru1.scenario_simulation_v2 import distribution_summary, shape_values, describe, YEAR_DAYS


def verify_outputs(directory):
    m=json.loads((directory/"manifest.json").read_text(encoding="utf-8"))
    for item in m["outputs"]:
        verify_file(directory/item["path"],expected_hash=item["sha256"],expected_size=item["size_bytes"])
    return m


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--dataset",default="data/scenario_simulation_v2")
    ap.add_argument("--output",default="artifacts/data_quality/scenario_simulation_v2")
    args=ap.parse_args()
    new=ROOT/args.dataset; out=ROOT/args.output; old=ROOT/"data/scenario_simulation_v1"
    if out.exists() and any(out.iterdir()): raise FileExistsError(out)
    for directory in (new,old): verify_outputs(directory)
    out.mkdir(parents=True,exist_ok=True); figures=out/"figures"; figures.mkdir()
    cfg=json.loads((new/"generation_config.json").read_text(encoding="utf-8"))
    roster=pd.read_csv(new/"point_roster.csv")
    cat=pd.read_csv(new/"scenario_catalog.csv",keep_default_na=False)
    obs=pd.read_csv(new/"evaluator/campaign_truth.csv.gz")
    features=pd.read_csv(new/"model_features.csv.gz")
    truth=pd.read_csv(new/"evaluator/next_planned_truth.csv.gz")
    samples=features.merge(truth.drop(columns=[c for c in features if c in truth and c!="sample_id"]),on="sample_id",validate="one_to_one")
    oldcat=pd.read_csv(old/"scenario_catalog.csv")
    oldobs=pd.read_csv(old/"campaign_observations.csv.gz")
    oldsamples=pd.read_csv(old/"next_planned_samples.csv.gz")
    oldcat["numeric_scale_constrained"]=False
    a=distribution_summary(oldobs,oldsamples,oldcat,roster)
    b=distribution_summary(obs,samples,cat,roster)
    _write_json(out/"old_distribution.json",a); _write_json(out/"new_distribution.json",b)
    stats=[]
    for key in a:
        if isinstance(a[key],dict) and "q50" in a[key]:
            for metric in ["n","min","q05","q50","mean","q95","q99","max"]:
                stats.append(dict(quantity=key,statistic=metric,old=a[key][metric],new=b[key][metric]))
    _write_csv(out/"differential_statistics.csv",pd.DataFrame(stats))
    counts=pd.DataFrame([
        ["base_points",42,42],["profiles",14,14],["proxy_zones",4,4],["campaigns",29,29],
        ["observation_scenarios",len(oldcat),len(cat)],["latent_worlds",5,cat.latent_world_id.nunique()],
        ["trajectory_instances",60*42,len(cat)*42],["latent_trajectory_keys",5*42,cat.latent_world_id.nunique()*42],
        ["distinct_latent_fields",5,127],
        ["campaign_rows",len(oldobs),len(obs)],["model_origins",len(oldsamples),len(samples)],
        ["targeted_rows",int(oldobs.targeted.sum()),int(obs.targeted.sum())],
        ["observed_rows",int(oldobs.observed.sum()),int(obs.observed.sum())],
        ["missing_targeted_fraction",a["missing_targeted_fraction"],b["missing_targeted_fraction"]],
        ["numerically_constrained_scale_fraction",0.,float(cat.numeric_scale_constrained.mean())],
        ["identified_real_temporal_law_fraction",0.,0.],
        ["transition_diagnostic_fraction",a["transition_diagnostic"]["fraction"],b["transition_diagnostic"]["fraction"]],
    ],columns=["quantity","old","new"])
    _write_csv(out/"counts_comparison.csv",counts)
    for name,col,fa,fb in [("process_family","dynamic_mechanism",oldcat,cat),("profile","base_profile_id",oldobs,obs),
                            ("missing_reason","missing_reason",oldobs.loc[oldobs.targeted & ~oldobs.observed],obs.loc[obs.targeted & ~obs.observed])]:
        f=pd.concat([fa[col].value_counts().rename("old"),fb[col].value_counts().rename("new")],axis=1).fillna(0).rename_axis(name).reset_index()
        _write_csv(out/f"{name}_comparison.csv",f)
    zone=roster.set_index("point_id").zone_id
    _write_csv(out/"zone_comparison.csv",pd.concat([oldobs.base_point_id.map(zone).value_counts().rename("old"),obs.base_point_id.map(zone).value_counts().rename("new")],axis=1).rename_axis("proxy_zone").reset_index())
    plt.rcParams.update({"font.family":"DejaVu Sans","font.size":9,"axes.spines.top":False,"axes.spines.right":False,"figure.dpi":120})
    chart_log=[]
    def save(fig,name,claim):
        fig.savefig(figures/f"{name}.png",dpi=150,bbox_inches="tight",facecolor="white")
        plt.close(fig); chart_log.append(dict(file=f"figures/{name}.png",claim=claim,source="new evaluator synthetic QA truth; source constraints separately labelled"))
    # Fixed random seed selects examples before plotting; extremes are shown separately.
    rng=np.random.default_rng(1729)
    selections=[]
    fig,axes=plt.subplots(5,2,figsize=(13,17),layout="constrained")
    for ax,(family,g) in zip(axes.flat,cat.groupby("dynamic_mechanism",sort=True)):
        row=g.loc[g.observation_condition.eq("ordinary_independent")].iloc[int(rng.integers(len(g.loc[g.observation_condition.eq("ordinary_independent")])))]
        point=str(rng.choice(roster.point_id))
        f=obs.loc[obs.scenario_id.eq(row.scenario_id)&obs.base_point_id.eq(point)].sort_values("date")
        selections.append(dict(family=family,scenario=row.scenario_id,point=point))
        dates=pd.to_datetime(f.date)
        ax.plot(dates,f.latent_settlement_mm,label="Synthetic latent",color="#18638e")
        ax.scatter(dates,f.observed_settlement_mm,label="Synthetic observation",s=10,color="#b35a22")
        ax.set(title=family,ylabel="Settlement, mm"); ax.tick_params(axis="x",rotation=25)
    axes[0,0].legend(fontsize=8)
    fig.suptitle("Random examples, seed 1729. All histories are synthetic; positive down.",fontsize=13)
    save(fig,"01_temporal_families","Diverse assumed temporal laws; no real monitoring history is plotted")
    _write_json(out/"random_example_selection.json",selections)
    fig,axes=plt.subplots(3,1,figsize=(12,10),layout="constrained")
    for selected in [s for s in selections if s["family"] in {"uniform","exponential_decay","saturating_acceleration","reactivation"}]:
        f=obs.loc[obs.scenario_id.eq(selected["scenario"])&obs.base_point_id.eq(selected["point"])].sort_values("date")
        t=(pd.to_datetime(f.date)-pd.to_datetime(f.date.iloc[0])).dt.days.to_numpy()/YEAR_DAYS
        v=np.diff(f.latent_settlement_mm)/np.diff(t); vt=(t[1:]+t[:-1])/2
        accel=np.diff(v)/np.diff(vt); at=(vt[1:]+vt[:-1])/2
        for ax,x,y in zip(axes,[t,vt,at],[f.latent_settlement_mm,v,accel]): ax.plot(x,y,label=selected["family"])
    for ax,ylabel in zip(axes,["Settlement, mm","Interval velocity, mm/year","Interval acceleration, mm/year²"]): ax.set(ylabel=ylabel,xlabel="Elapsed synthetic years")
    axes[0].legend(ncol=2); fig.suptitle("Settlement and finite differences on actual irregular intervals")
    save(fig,"02_settlement_velocity_acceleration","Rates refer to intervals; acceleration to spacing of interval midpoints")
    # Published graphs only on ordinal axes; no undocumented map coordinate alignment.
    croot=ROOT/"artifacts/reconstruction/scenario_constraints_v2"
    points=pd.read_csv(croot/"canonical_digitization.csv",dtype={"profile_label":str})
    inv=pd.read_csv(croot/"profile_series_inventory.csv",dtype={"profile_label":str})
    cond=pd.read_csv(new/"evaluator/conditioning_log.csv",keep_default_na=False)
    fig,axes=plt.subplots(4,2,figsize=(14,15),layout="constrained")
    for ax,donor in zip(axes.flat,inv.loc[inv.numeric_calibration].itertuples()):
        p=points.loc[points.profile_label.eq(donor.profile_label)&points.method.eq("leveling")&points.observation_interval_label.eq(donor.interval)].sort_values("figure_order")
        x=(p.figure_order-1)/max(1,p.figure_order.max()-1)
        # NaNs remain gaps, including the three unknown positions of Line 1.
        ax.errorbar(x,-p.signed_displacement_mm,yerr=p.digitization_envelope_half_width_mm,fmt="o",ms=3,color="#b35a22",label="Published digitization + reading bounds",zorder=3)
        worlds=cat.loc[cat.numeric_constraint_id.eq(donor.constraint_id)&~cat.dynamic_mechanism.eq("moving_spatial_focus"),"latent_world_id"].unique()
        logs=cond.loc[cond.latent_world_id.isin(worlds)].iloc[::max(1,len(cond.loc[cond.latent_world_id.isin(worlds)])//18)]
        dense=np.linspace(0,1,150)
        floor=donor.magnitude_lower_mm if donor.unresolved==0 else 0.
        for log in logs.itertuples():
            field=floor+(log.sampled_peak_mm-floor)*shape_values(dense,donor.structural_shape,log.shape_centre,log.shape_width,log.reversed_order)
            ax.plot(dense,field,alpha=.16,color="#18638e",lw=1)
        ax.set(title=f"Line {donor.profile_label}, {donor.interval}",xlabel="Ordinal position / synthetic normalized extent",ylabel="Positive-down interval displacement, mm")
    axes.flat[-1].axis("off"); axes.flat[-1].text(0,.9,"Published: individual digitized markers.\nBlue: randomized analogous shapes.\nNo coordinate or benchmark mapping.\nNominal 1/5-year windows, not exact dates.\nNot a pointwise reconstruction or fit.\nMissing published values remain unknown.",va="top",fontsize=11)
    axes.flat[0].legend(fontsize=7)
    fig.suptitle("Published spatial constraints and synthetic analogues; no temporal curve is inferred")
    save(fig,"03_published_profiles_and_analogues","Ordinal comparison only; simulations constrained in interval scale and broad shape, not pointwise agreement")
    fig,ax=plt.subplots(figsize=(12,6),layout="constrained")
    labels=[]
    for i,d in enumerate(inv.loc[inv.numeric_calibration].itertuples()):
        values=cond.loc[cond.constraint_id.eq(d.constraint_id),"reference_max_mm"]
        ax.scatter(np.full(len(values),i),values,s=4,alpha=.16,color="#18638e")
        ax.plot([i-.3,i+.3],[d.magnitude_upper_mm]*2,color="#b35a22",lw=2)
        labels.append(f"P{d.profile_label}\n{d.interval}")
    ax.set_xticks(range(len(labels)),labels); ax.set(ylabel="Interval displacement magnitude, mm",title="Synthetic profile maxima within separate published reading envelopes")
    ax.plot([],[],color="#b35a22",label="Largest readable magnitude + read bound"); ax.scatter([],[],color="#18638e",label="Synthetic analogue-window maxima"); ax.legend()
    save(fig,"04_empirical_vs_simulated_ranges","Do not interpret extrema of readable points as population extrema or exact annual rates")
    fig,axes=plt.subplots(1,3,figsize=(15,4.5),layout="constrained")
    for label,o,s,color in [("v1",oldobs,oldsamples,"#777777"),("v2",obs,samples,"#18638e")]:
        axes[0].hist(o.latent_settlement_mm,bins=60,density=True,histtype="step",label=label,color=color)
        axes[1].hist(s.latent_rate_mm_y,bins=60,density=True,histtype="step",label=label,color=color)
        axes[2].hist(s.current_standard_uncertainty_mm,bins=[.3,.6,.9,1.2,1.7,2.1],density=True,histtype="step",label=label,color=color)
    for ax,title in zip(axes,["Latent settlement, mm","Next-planned latent rate, mm/year","Reported ordinary sigma, mm"]): ax.set_title(title); ax.legend()
    fig.suptitle("Old/new distributions. Mixture weights differ; these are not model scores.")
    save(fig,"05_old_new_distributions","Full experimental mixtures, not paired causal effect of one change")
    fig,axes=plt.subplots(3,1,figsize=(13,11),layout="constrained")
    dates=pd.to_datetime(sorted(obs.date.unique())); gaps=np.diff(dates).astype("timedelta64[D]").astype(int)
    axes[0].bar(dates[1:],gaps,width=20,color="#18638e"); axes[0].set(ylabel="Planned gap, days",title="Same inherited campaign calendar in v1 and v2")
    for label,s,col in [("v1",oldsamples,"#777777"),("v2",samples,"#18638e")]: axes[1].hist(s.days_since_previous_observation,bins=35,density=True,histtype="step",label=label,color=col)
    axes[1].set(xlabel="Gap between successful observations, days",ylabel="Density"); axes[1].legend()
    coverage=obs.loc[obs.targeted].groupby(["measurement_error_mechanism","date"])["observed"].mean().unstack()
    im=axes[2].imshow(coverage,vmin=0,vmax=1,aspect="auto",cmap="Blues")
    axes[2].set_yticks(range(len(coverage)),coverage.index); axes[2].set_xticks(range(0,len(dates),4),[str(x.date()) for x in dates[::4]],rotation=25)
    axes[2].set_title("Availability among targeted points; conditions also differ in missingness")
    fig.colorbar(im,ax=axes[2],label="Observed / targeted")
    save(fig,"06_cadence_missingness","Snow unavailability applies only to reflector contamination scenario")
    uniform=cat.loc[cat.dynamic_mechanism.eq("uniform")].iloc[0].latent_world_id
    paired=cat.loc[cat.latent_world_id.eq(uniform)]
    fig,axes=plt.subplots(4,1,figsize=(12,11),layout="constrained")
    for ax,error in zip(axes,["ordinary_noise","gross_error","systematic_shift","thermal_reflector"]):
        row=paired.loc[paired.measurement_error_mechanism.eq(error)].iloc[0]
        f=obs.loc[obs.scenario_id.eq(row.scenario_id)]
        pt=f.groupby("base_point_id").reference_datum_error_mm.apply(lambda s:s.abs().max()).idxmax() if error=="thermal_reflector" else roster.point_id.iloc[0]
        f=f.loc[f.base_point_id.eq(pt)].sort_values("date")
        for col,label in [("ordinary_noise_mm","ordinary"),("gross_error_mm","isolated gross error"),("reference_datum_error_mm","systematic/reflector component")]: ax.plot(pd.to_datetime(f.date),f[col],marker=".",label=label)
        ax.set(title=error,ylabel="Measurement component, mm")
    axes[0].legend(ncol=3,fontsize=8); fig.suptitle("Observation-process components; 30–50 mm roof effect is not leveling precision")
    save(fig,"07_measurement_noise_components","NaN components on missing observations are not zero errors")
    fig,axes=plt.subplots(1,3,figsize=(15,4.5),layout="constrained")
    choices=[cat.loc[cat.dynamic_mechanism.eq("saturating_acceleration") & cat.observation_condition.eq("ordinary_independent")].iloc[0],
             paired.loc[paired.measurement_error_mechanism.eq("systematic_shift")].iloc[0],paired.loc[paired.measurement_error_mechanism.eq("thermal_reflector")].iloc[0]]
    for ax,row,title in zip(axes,choices,["True nonlinear acceleration","Uniform truth + datum shift","Uniform truth + reflector shift"]):
        f=obs.loc[obs.scenario_id.eq(row.scenario_id)]
        pt=f.groupby("base_point_id").reference_datum_error_mm.apply(lambda s:s.abs().max()).idxmax()
        f=f.loc[f.base_point_id.eq(pt)].sort_values("date")
        ax.plot(pd.to_datetime(f.date),f.latent_settlement_mm,label="latent",color="#18638e")
        ax.scatter(pd.to_datetime(f.date),f.observed_settlement_mm,label="observed",s=12,color="#b35a22")
        ax.set(title=title,ylabel="Settlement, mm"); ax.tick_params(axis="x",rotation=30); ax.legend()
    save(fig,"08_true_vs_pseudo_transition","Pseudo-transition cases have exactly constant latent velocity")
    extreme_rows=[]
    fig,axes=plt.subplots(2,2,figsize=(13,9),layout="constrained")
    for ax,col,title in zip(axes.flat,["latent_rate_mm_y","observed_rate_mm_y","recent_acceleration_mm_y2","days_since_previous_observation"],["Largest latent target rate","Largest absolute observed target rate","Largest absolute observed acceleration","Longest observed history gap"]):
        row=samples.loc[samples[col].abs().idxmax()]
        f=obs.loc[obs.scenario_id.eq(row.scenario_id)&obs.base_point_id.eq(row.base_point_id)].sort_values("date")
        ax.plot(pd.to_datetime(f.date),f.latent_settlement_mm,label="latent",color="#18638e")
        ax.scatter(pd.to_datetime(f.date),f.observed_settlement_mm,label="observed",s=12,color="#b35a22")
        ax.axvline(pd.Timestamp(row.current_date),ls=":",color="#444444")
        ax.set(title=f"{title}\n{row[col]:.2f}; {row.dynamic_mechanism}",ylabel="Settlement, mm"); ax.tick_params(axis="x",rotation=25)
        extreme_rows.append(dict(diagnostic=col,sample_id=row.sample_id,value=float(row[col]),mechanism=row.dynamic_mechanism))
    axes[0,0].legend(); save(fig,"09_extreme_cases","Descriptive worst data cases, not selected using model errors")
    _write_csv(out/"extreme_cases.csv",pd.DataFrame(extreme_rows))
    fig,axes=plt.subplots(1,3,figsize=(15,5),layout="constrained")
    moving=cat.loc[cat.dynamic_mechanism.eq("moving_spatial_focus") & cat.observation_condition.eq("ordinary_independent")].iloc[0]
    spatial=obs.loc[obs.scenario_id.eq(moving.scenario_id)].merge(roster[["point_id","x_local_m","y_local_m"]],left_on="base_point_id",right_on="point_id",validate="many_to_one")
    vmax=spatial.latent_settlement_mm.max()
    for ax,date in zip(axes,[spatial.date.unique()[i] for i in [5,16,28]]):
        g=spatial.loc[spatial.date.eq(date)]
        im=ax.scatter(g.x_local_m,g.y_local_m,c=g.latent_settlement_mm,vmin=0,vmax=vmax,cmap="viridis",s=35)
        ax.set(title=date,xlabel="Reconstructed local x, m",ylabel="Reconstructed local y, m",aspect="equal")
    fig.colorbar(im,ax=list(axes),label="Synthetic latent settlement, mm",shrink=.7)
    fig.suptitle("Same 42 reconstructed network points; no invented map georeference or interpolated field")
    save(fig,"10_spatial_snapshots","Point-supported surface snapshots only; not the real 2016 velocity map")
    _write_json(out/"chart_manifest.json",chart_log)
    _write_json(out/"validation_report.json",dict(status="PASS_COMPUTATIONAL",model_training_calls=0,
        old_source="public_scenario_v1_not_legacy_BC_labels",new_source="synthetic_QA_evaluator_not_external_holdout",
        chart_count=len(chart_log),visual_inspection="pending_separate_reader_check"))
    sources=[new/"manifest.json",old/"manifest.json",croot/"manifest.json",Path(__file__)]
    _write_json(out/"manifest.json",dict(inputs=[dict(path=p.relative_to(ROOT).as_posix(),sha256=sha256_file(p),size_bytes=p.stat().st_size) for p in sources],
        outputs=[dict(path=p.relative_to(out).as_posix(),sha256=sha256_file(p),size_bytes=p.stat().st_size) for p in sorted(out.rglob("*")) if p.is_file()]))
    print(counts.to_string(index=False))


if __name__=="__main__": main()
