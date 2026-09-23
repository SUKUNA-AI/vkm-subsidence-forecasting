"""Data-only tests: never fit or import an estimator, never read legacy labels."""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from skru1.empirical_constraints_v2 import canonical_digitization, checked_inputs, empty_destination
from skru1 import scenario_simulation_v2 as sim

ROOT=Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def foundation():
    cfg=json.loads((ROOT/"configs/scenario_simulation_v2.json").read_text(encoding="utf-8"))
    cfg["thermal_amplitude_source_bounds_mm"]=[30.,50.]
    roster,campaigns,targeted,_=sim.load_foundation(ROOT,cfg)
    profiles=pd.read_csv(ROOT/cfg["profile_inventory"],dtype={"profile_label":str})
    return cfg,roster,campaigns,targeted,sim.catalog(cfg,profiles)


def test_canonical_reading_preserves_unknowns_and_accepted_detailed_revision():
    points=canonical_digitization(ROOT,{})
    line=points.loc[points.profile_label.eq("1") & points.method.eq("leveling") & points.observation_interval_label.eq("2011-2016")]
    assert len(line)==14
    assert line.signed_displacement_mm.isna().sum()==3
    assert line.reading_status.value_counts().to_dict()=={"digitized_marker_or_visible_curve":7,"partially_occluded_approximation":4,"unresolved_occlusion":3}
    assert points.signed_displacement_mm.notna().sum()==253
    assert set(points.loc[points.profile_label.eq("6"),"observation_interval_label"])=={"2015-2016"}


def test_numeric_donors_exclude_radar_unknown_datum_and_unknown_curve(foundation):
    cfg,_,_,_,cat=foundation
    evidence=pd.read_csv(ROOT/cfg["constraints_table"],keep_default_na=False).set_index("constraint_id")
    ids=set(cat.numeric_constraint_id)-{""}
    assert len(ids)==7
    assert evidence.loc[list(ids),"method"].eq("leveling").all()
    assert evidence.loc[list(ids),"status"].eq("ACCEPTED_NUMERIC_CONSTRAINT").all()
    assert "MUS-UNIDENTIFIED-TIME-CURVE" not in ids
    assert "MUS-PERIOD-DATUM" not in ids


def test_bad_input_fails_before_transformation():
    with pytest.raises(ValueError,match="hash mismatch"):
        checked_inputs(ROOT,{"inputs":[dict(path="configs/scenario_simulation_v2.json",sha256="0"*64,size_bytes=(ROOT/"configs/scenario_simulation_v2.json").stat().st_size)]})


def test_released_destination_cannot_be_overwritten():
    with pytest.raises(FileExistsError):
        empty_destination(ROOT,ROOT/"artifacts/reconstruction/scenario_constraints_v2","artifacts/reconstruction/scenario_constraints_v2")
    with pytest.raises(ValueError):
        empty_destination(ROOT,ROOT/"inputs/sources/new","data/scenario_simulation_v2")


@pytest.mark.parametrize("name",["uniform","exponential_decay","logarithmic_decay","smooth_acceleration","saturating_acceleration","temporary_acceleration","reactivation","smooth_step","moving_spatial_focus"])
def test_bounds_and_monotonicity_do_not_depend_on_imm(foundation,name):
    cfg,roster,campaigns,_,cat=foundation
    row=cat.loc[cat.dynamic_mechanism.eq(name)].iloc[0].to_dict()
    t=np.linspace(0,1,1001)
    span=(campaigns.date.iloc[-1]-campaigns.date.iloc[0]).days/sim.YEAR_DAYS
    truth,logs=sim.latent_surface(t,roster,row,cfg,span)
    assert np.isfinite(truth).all()
    assert (np.diff(truth,axis=0)>=-1e-8).all()
    assert all(x["reference_max_mm"]<=x["source_readable_upper_with_bound_mm"]+1e-8 for x in logs)


def test_constraint_changes_really_change_generated_values(foundation):
    cfg,roster,campaigns,_,cat=foundation
    row=cat.loc[cat.dynamic_mechanism.eq("uniform")].iloc[0].to_dict()
    span=(campaigns.date.iloc[-1]-campaigns.date.iloc[0]).days/sim.YEAR_DAYS
    a,_=sim.latent_surface(np.array([0,.4,1.]),roster,row,cfg,span)
    changed=dict(row)
    changed["reference_upper_mm"]*=1.5; changed["reference_floor_mm"]*=1.5
    b,_=sim.latent_surface(np.array([0,.4,1.]),roster,changed,cfg,span)
    np.testing.assert_allclose(b,1.5*a)


def test_pseudo_transition_changes_measurement_only(foundation):
    cfg,roster,campaigns,targeted,cat=foundation
    pair=cat.loc[cat.dynamic_mechanism.eq("uniform") & cat.latent_world_id.eq(cat.loc[cat.dynamic_mechanism.eq("uniform"),"latent_world_id"].iloc[0])]
    normal=pair.iloc[0].to_dict(); pseudo=pair.loc[pair.pseudo_transition].iloc[0].to_dict()
    a,_,_=sim.scenario_frames(normal,{},cfg,roster,campaigns,targeted)
    b,_,_=sim.scenario_frames(pseudo,{},cfg,roster,campaigns,targeted)
    np.testing.assert_allclose(a.latent_settlement_mm,b.latent_settlement_mm)
    assert b.reference_datum_error_mm.abs().max()>0
    rates=a.loc[a.date.ne(a.date.min())].groupby("base_point_id").latent_rate_mm_y.std()
    assert (rates<1e-9).all()


def test_future_observation_perturbation_cannot_change_origin_features(foundation,monkeypatch):
    cfg,roster,campaigns,targeted,cat=foundation
    scenario=cat.loc[cat.dynamic_mechanism.eq("uniform")].iloc[0].to_dict()
    _,_,before=sim.scenario_frames(scenario,{},cfg,roster,campaigns,targeted)
    original=sim.observation_arrays
    cutoff=18
    def perturb(*args,**kwargs):
        parts=list(original(*args,**kwargs))
        parts[4][cutoff:]+=10000 # only future observation noise
        return tuple(parts)
    monkeypatch.setattr(sim,"observation_arrays",perturb)
    _,_,after=sim.scenario_frames(scenario,{},cfg,roster,campaigns,targeted)
    cutoff_date=str(campaigns.iloc[cutoff].date.date())
    left=before.loc[before.current_date<cutoff_date,[*sim.MODEL_METADATA,*sim.REQUIRED_FEATURES]]
    right=after.loc[after.current_date<cutoff_date,[*sim.MODEL_METADATA,*sim.REQUIRED_FEATURES]]
    pd.testing.assert_frame_equal(left.reset_index(drop=True),right.reset_index(drop=True))


def test_acceleration_uses_interval_midpoints(foundation):
    cfg,roster,campaigns,targeted,cat=foundation
    row=cat.loc[cat.dynamic_mechanism.eq("uniform")].iloc[0].to_dict()
    _,hist,_=sim.scenario_frames(row,{},cfg,roster,campaigns,targeted)
    for _,g in hist.groupby("point_id"):
        g=g.sort_values("current_date")
        dt=pd.to_datetime(g.current_date).diff().dt.days/sim.YEAR_DAYS
        expected=g.last_rate_mm_y.diff()/((dt+dt.shift(1))/2)
        np.testing.assert_allclose(g.recent_acceleration_mm_y2,expected,equal_nan=True)


def test_sequence_windows_have_only_own_past_observations(foundation):
    cfg,roster,campaigns,targeted,cat=foundation
    _,hist,samples=sim.scenario_frames(cat.iloc[0].to_dict(),{},cfg,roster,campaigns,targeted)
    windows=sim.sequence_windows(hist,samples,16)
    h=hist.set_index("history_id")
    for w in windows.itertuples():
        ids=json.loads(w.history_ids_json); selected=h.loc[ids]
        assert len(ids)==w.sequence_length and len(ids)+w.left_padding==16
        assert selected.point_id.eq(w.point_id).all()
        assert selected.current_date.le(w.current_date).all()
        assert selected.iloc[-1].current_date==w.current_date


def test_state_dependent_missingness_not_normalized_by_future_max(foundation):
    cfg,_,campaigns,targeted,cat=foundation
    row=cat.loc[cat.missingness_mechanism.eq("state_dependent")].iloc[0].to_dict()
    dates=campaigns.date.to_numpy(dtype="datetime64[ns]")
    times=(dates-dates[0]).astype("timedelta64[D]").astype(float)/sim.YEAR_DAYS
    latent=np.broadcast_to(times[:,None]*20.,targeted.shape).copy()
    a=sim.observation_arrays(latent,dates,campaigns.campaign_type.to_numpy(),targeted,row,cfg)
    latent[20:]+=10000
    b=sim.observation_arrays(latent,dates,campaigns.campaign_type.to_numpy(),targeted,row,cfg)
    np.testing.assert_array_equal(a[1][:20],b[1][:20])


def test_distribution_summary_is_serializable_and_defines_denominators(foundation):
    cfg,roster,campaigns,targeted,cat=foundation
    obs,_,samples=sim.scenario_frames(cat.iloc[0].to_dict(),{},cfg,roster,campaigns,targeted)
    summary=sim.distribution_summary(obs,samples,cat.iloc[:1],roster)
    assert 0<=summary["missing_targeted_fraction"]<=1
    assert sum(v["count"] for v in summary["profile_observation_rows"].values())==len(obs)
    json.dumps(summary,allow_nan=False)
