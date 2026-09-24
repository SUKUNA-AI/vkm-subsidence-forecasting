"""R2 data-only checks (no models, no scoring, no evaluator truth).

1. Paired reflector_seasonal minus ordinary_independent observed settlement by campaign
   (same latent world, same base point) from model-facing causal_history.
2. Ratio of 2015-2016 to 2011-2016 leveling values at common readable slots (AMBIGUOUS datum).
3. Campaign-calendar months versus source-documented radar season.
Run from repository root: .venv/Scripts/python.exe scripts/research/r2_checks.py
"""
import json
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "work/r2_adversarial_audit/r2_checks.json"
import hashlib
INPUT_HASHES = {'data/scenario_simulation_v2_1/causal_history.csv.gz': '353a5eb72dab8b97d6fe6915b4395b7e473bb3815c595febaa4d91c502226f42', 'data/scenario_simulation_v2_1/scenario_catalog.csv': 'a42fcfc66fb1d2a05c71eeabf62658c5d99ac9308e81ca99544986945eda4a5c', 'data/scenario_simulation_v2_1/campaign_catalog.csv': '7436eb7d0000f3ed87d3111e84f498ed541fa725a41410a3a3861b9dc1be62c2', 'artifacts/reconstruction/scenario_constraints_v2/canonical_digitization.csv': 'f33c22a8b6af4a8def6aeffc696cedc73434fb704fe174907a453c11f224abe0'}
for relative, expected in INPUT_HASHES.items():
    with (ROOT / relative).open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
            raise ValueError(f"R2 input hash mismatch: {relative}")
OUT.parent.mkdir(parents=True, exist_ok=True)

res = {}

cols = ["scenario_id", "base_point_id", "campaign_id", "current_date", "last_settlement_mm", "n_history"]
h = pd.read_csv(ROOT / "data/scenario_simulation_v2_1/causal_history.csv.gz", usecols=cols)
s = pd.read_csv(ROOT / "data/scenario_simulation_v2_1/scenario_catalog.csv", usecols=["scenario_id", "latent_world_id", "observation_condition"])
h = h.merge(s, on="scenario_id").sort_values(["scenario_id", "base_point_id", "current_date"])
h["dn"] = h.groupby(["scenario_id", "base_point_id"]).n_history.diff().fillna(1)
cur = h[h.dn > 0]  # rows where a new observation was added at current_date
a = cur[cur.observation_condition == "ordinary_independent"][["latent_world_id", "base_point_id", "campaign_id", "current_date", "last_settlement_mm"]]
b = cur[cur.observation_condition == "reflector_seasonal"][["latent_world_id", "base_point_id", "campaign_id", "last_settlement_mm"]]
m = a.merge(b, on=["latent_world_id", "base_point_id", "campaign_id"], suffixes=("_ord", "_refl"))
m["d"] = m.last_settlement_mm_refl - m.last_settlement_mm_ord
g = m.groupby(["campaign_id", "current_date"])["d"].agg(["count", "mean", "median"]).reset_index()
res["paired_reflector_minus_ordinary_by_campaign"] = g.round(3).to_dict("records")
res["paired_rows"] = int(len(m))

cal = pd.read_csv(ROOT / "data/scenario_simulation_v2_1/campaign_catalog.csv")
cal["month"] = pd.to_datetime(cal["date"]).dt.month
res["campaigns_total"] = int(len(cal))
res["campaigns_masked_by_v2_1_reflector_months_11_12"] = cal[cal.month.isin([11, 12])].campaign_id.tolist()
res["campaigns_in_source_snow_season_dec_to_apr_not_masked"] = cal[cal.month.isin([1, 2, 3, 4])].campaign_id.tolist()

d = pd.read_csv(ROOT / "artifacts/reconstruction/scenario_constraints_v2/canonical_digitization.csv")
lv = d[d.method == "leveling"]
ratios = {}
for line in ["1", "5", "17"]:
    x = lv[lv.profile_label.astype(str) == line]
    y5 = x[x.observation_interval_label == "2011-2016"].set_index("figure_order").signed_displacement_mm
    y1 = x[x.observation_interval_label == "2015-2016"].set_index("figure_order").signed_displacement_mm
    j = pd.concat([y5.rename("y5"), y1.rename("y1")], axis=1).dropna()
    j = j[j.y5 < -50]
    r = j.y1 / j.y5
    ratios[line] = dict(n=int(len(j)), median=round(float(r.median()), 3), q25=round(float(r.quantile(.25)), 3),
                        q75=round(float(r.quantile(.75)), 3), min=round(float(r.min()), 3), max=round(float(r.max()), 3))
res["ratio_2015_2016_over_2011_2016_leveling_AMBIGUOUS_DATUM"] = ratios
res["reference_uniform_ratio_if_5y_and_1y"] = 0.2
OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
print(json.dumps({k: v for k, v in res.items() if k != "paired_reflector_minus_ordinary_by_campaign"}, ensure_ascii=False, indent=1))
