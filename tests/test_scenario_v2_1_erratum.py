"""Data-only calendar erratum regressions; no estimator imports or model calls."""
import ast
import json
from pathlib import Path
import numpy as np
import pandas as pd
import pytest
from skru1 import scenario_simulation_v2_1 as sim

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("days", [56, 112, 434, 1, 98, 217, 448, 763, 1526, 36525])
def test_direct_calendar_days(days):
    previous = np.datetime64("2018-05-15", "ns")
    current = previous + np.timedelta64(days, "D")
    assert sim.exact_elapsed_days(current, previous) == days


def test_calendar_exactness_does_not_depend_on_year_conversion(monkeypatch):
    monkeypatch.setattr(sim, "YEAR_DAYS", float("nan"))
    start = np.datetime64("2000-01-01", "ns")
    for days in range(1, 4000):
        assert sim.exact_elapsed_days(start + np.timedelta64(days, "D"), start) == days


def test_source_change_is_limited_to_calendar_correction_and_release_plumbing():
    def functions(path):
        return {n.name: ast.dump(n, include_attributes=False) for n in ast.parse(path.read_text(encoding="utf-8")).body if isinstance(n, ast.FunctionDef)}
    old = functions(ROOT / "src/skru1/scenario_simulation_v2.py")
    new = functions(ROOT / "src/skru1/scenario_simulation_v2_1.py")
    for name in old.keys() - {"scenario_frames", "generate", "dataset_card"}:
        assert old[name] == new[name], name
    source = (ROOT / "src/skru1/scenario_simulation_v2_1.py").read_text(encoding="utf-8")
    assert "days_since_previous[campaign_index, point_index] = elapsed_days" in source
    assert "years = elapsed_days / YEAR_DAYS" in source
    assert "= years * YEAR_DAYS" not in source
    assert "round(" not in source


def test_generation_semantics_config_unchanged():
    old = json.loads((ROOT / "configs/scenario_simulation_v2.json").read_text(encoding="utf-8"))
    new = json.loads((ROOT / "configs/scenario_simulation_v2_1.json").read_text(encoding="utf-8"))
    ignore = {"dataset_id", "output_directory", "code_commit", "inputs", "erratum"}
    assert {k:v for k,v in old.items() if k not in ignore} == {k:v for k,v in new.items() if k not in ignore}
    assert new["inputs"][:-1] == old["inputs"]


def test_invariant_detects_old_bug_and_does_not_allow_tolerance():
    h = pd.DataFrame(dict(point_id=["p", "p"], current_date=["2021-01-26", "2021-05-18"]))
    f = pd.DataFrame(dict(point_id=["p"], current_date=["2021-05-18"], days_since_previous_observation=[112]))
    assert sim.validate_calendar_elapsed(h, f)["mismatch_count"] == 0
    f["days_since_previous_observation"] = 111
    with pytest.raises(ValueError, match="V2-REP-001"):
        sim.validate_calendar_elapsed(h, f)


def test_frozen_destination_and_v2_are_protected(tmp_path):
    # A clean checkout need not contain the previous author's ignored work tree.
    occupied = tmp_path / "work/scenario_v2_1_erratum/occupied"
    occupied.mkdir(parents=True)
    (occupied / "existing.txt").write_text("frozen", encoding="utf-8")
    with pytest.raises(FileExistsError):
        sim.empty_destination(tmp_path, occupied, "data/scenario_simulation_v2_1")
    with pytest.raises(ValueError):
        sim.empty_destination(tmp_path, tmp_path / "data/scenario_simulation_v2", "data/scenario_simulation_v2_1")
