"""Review Codex (PR #32): uncertain endpoints of a mining period stay bounds, not an exact printed period."""
import importlib.util
from pathlib import Path


def _module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "build_skru1_mine_history.py"
    spec = importlib.util.spec_from_file_location("mh_build", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_uncertain_endpoints_stay_bounds_not_exact_period():
    mh = _module()
    rec = mh.timing_records([{"source_id": "S", "ev_id": "E", "start_min": 1950, "start_max": 1959,
                              "end_min": 1970, "end_max": 1979}])
    assert all(r["kind"] != "PERIOD" for r in rec) and any(r["kind"] == "BOUND" for r in rec)
    exact = mh.timing_records([{"source_id": "S", "ev_id": "E", "start_min": 1950, "start_max": 1950,
                                "end_min": 1970, "end_max": 1970}])
    assert [r["kind"] for r in exact] == ["PERIOD"]
