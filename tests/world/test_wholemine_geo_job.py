"""Restart and profile contracts on a small actual P1 reconstruction."""
from hashlib import sha256
import json
import shutil

import numpy as np
import pytest
from shapely.geometry import box, mapping

from vkm_world.geometry_build import wholemine_geo_job as job
from vkm_world.geometry_build.wholemine_foundation import Foundation, SpatialConfiguration


ROOF = {"id": "roof", "kind": "roof_plane", "contact_id": "TOP", "origin_xy": [0, 0],
        "gradient": [0, 0], "intercept_m": 50, "scale_m": 1, "basis": "synthetic"}
GAP = {"id": "gap", "kind": "thickness", "contact_ids": ["TOP", "BASE"],
       "center_m": 20, "scale_m": 1, "basis": "synthetic"}


def position(key, xy):
    return {"object_id": key, "xy": xy, "membership": "TARGET", "frame": "SH1",
            "role": "IDENTIFIED_IN_DECLARED_FRAME", "epistemic_status": "DERIVATION",
            "source_factor_ids": ["local-anchor"],
            "scientific_use_mask": "CONDITIONAL_LOCAL_MAP_CONTROL"}


def configuration(key="cfg", profiles=()):
    return SpatialConfiguration(key, "spatial-choice", "SH1",
                                (position("A", [1, 1]), position("N", None)), profiles, {})


def test_profile_uses_only_qualified_frame_transform():
    reg = {"origin": [0, 0], "angle_rad": 0, "parent": "native-registration"}
    stations = [{"station": 1}, {"station": 2}]
    local = {"id": "p", "frame": "NATIVE", "registration": reg, "stations": stations}
    same = {**local, "id": "q", "frame": "SH1"}
    cfg = configuration(profiles=(local, same))
    nodes = [[0, 0], [4, 0], [0, 4]]
    problem = job.compile_model(nodes, [[0, 1, 2]], ["TOP", "BASE"], [], frame_id="SH1",
                                configuration_id=cfg.id, priors=[ROOF, GAP], scenario_basis="synthetic")
    field = {"contacts": np.array([[0, 4, 0], [-10, -6, -10]]), "contact_ids": ["TOP", "BASE"]}
    domain = box(0, 0, 3, 3)
    unstated = job.profiles(problem, field, cfg, domain, "geo")
    assert {p["id"] for p in unstated["profiles"]} == {"FIELD-EW", "FIELD-SN", "q"}
    assert unstated["native_context_profiles"][0]["native"]["registration"] == reg
    bridge = {"id": "qualified-native-axis", "from_frame": "NATIVE", "to_frame": "SH1",
              "registration_sha256": job.digest({"frame": "NATIVE", "registration": reg, "stations": stations}),
              "admission": "QUALIFIED_PROFILE_BRIDGE", "source_refs": [{"source_id": "synthetic-plan", "locator": "tie"}],
              "affine": [[1, 0, 1], [0, 1, 0]]}
    with_bridge = job.profiles(problem, field, cfg, domain, "geo", {"p": bridge})
    translated = next(row for row in with_bridge["profiles"] if row["id"] == "p")
    assert translated["native"]["stations"] == stations
    assert translated["native_station_m"][0] == 1
    assert translated["xy"][0] == [2, 0]
    assert translated["values_m"]["TOP"][-1] == pytest.approx(3)
    with pytest.raises(ValueError, match="qualify"):
        job.profiles(problem, field, cfg, domain, "geo", {"p": {**bridge, "registration_sha256": "old"}})
    for override in ({"source_refs": "untyped"}, {"affine": [[1, 0, "1"], [0, True, 0]]}):
        with pytest.raises(ValueError, match="Profile bridge"):
            job.profiles(problem, field, cfg, domain, "geo", {"p": {**bridge, **override}})


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    manifest = tmp_path / "MANIFEST.json"
    manifest.write_text("{}")
    inputs, field, out = (tmp_path / name for name in ("inputs", "field", "run"))
    inputs.mkdir()
    field.mkdir()
    configs = tuple(configuration(name) for name in ("first", "second"))
    foundation = Foundation(("A", "N"), (), configs, {"A": ["source-record"]}, "UNKNOWN")
    monkeypatch.setattr(job, "load_foundation", lambda _: foundation)
    job.write(field / "BHJ_INDEX.json", {
        "bhj_manifest_sha256": sha256(manifest.read_bytes()).hexdigest(),
        "selected": [{"id": cfg.id, "reason": "FARTHEST_INPUT_CONFIGURATION"} for cfg in configs],
    })
    job.write(field / "FOOTPRINT.geojson", mapping(box(0, 0, 2, 2)))
    np.savez(field / "CARRIER.npz", xy=np.array([[0, 0], [2, 0], [0, 2], [2, 2]], dtype=float),
             triangles=np.array([[0, 1, 2], [1, 3, 2]]))
    job.write(inputs / "PRIORS.json", {"contact_ids": ["TOP", "BASE"], "baseline": [ROOF, GAP]})
    job.write(inputs / "FIELD_POLICY.json", {
        "primary_interpretation": "ROOF", "alternative_interpretation": "FLOOR", "job_wall_time_s": 60,
        "solver": {"slope_length_m": 2, "contact_weight": 1, "thickness_weight": 1,
                   "time_limit_s": 10, "max_iterations": 100, "hard_atol_m": 1e-7},
    })
    skipped = {"factor_id": "native-md", "physical_id": "A", "interpretation_id": "ROOF", "admission": "HARD",
               "quantity": "DEPTH_MD", "epistemic_status": "FACT", "support": {"kind": "NONE"},
               "contact_ids": ["TOP"], "source_refs": [{"record_id": "source-record"}],
               "bounds": {"lower": 50, "upper": 50, "unit": "m"}, "independence_group": "source"}
    job.jsonl(inputs / "TYPED_VERTICAL_FACTORS.jsonl", [skipped])
    job.write(inputs / "CONDITIONAL_AREA_RECORDS.json", [])
    return manifest, inputs, field, out


def inventory(directory):
    return {file.name: file.read_bytes() for file in directory.iterdir() if file.is_file()}


def test_changed_footprint_policy_and_code_refuse_resume_without_writes(prepared, monkeypatch):
    _, inputs, field, out = prepared
    assert job.execute(*prepared, stop_after=1) == 2
    before = inventory(out)
    footprint = field / "FOOTPRINT.geojson"
    original = footprint.read_bytes()
    job.write(footprint, mapping(box(0, 0, 3, 2)))
    with pytest.raises(ValueError, match="new generation"):
        job.execute(*prepared, resume=True)
    footprint.write_bytes(original)
    policy = inputs / "FIELD_POLICY.json"
    original = policy.read_bytes()
    job.write(policy, {**job.read(policy), "job_wall_time_s": 61})
    with pytest.raises(ValueError, match="new generation"):
        job.execute(*prepared, resume=True)
    policy.write_bytes(original)
    source = field / "changed_implementation.py"
    source.write_text("# distinct implementation")
    monkeypatch.setattr(job, "__file__", str(source))
    with pytest.raises(ValueError, match="new generation"):
        job.execute(*prepared, resume=True)
    assert inventory(out) == before


@pytest.mark.parametrize("damaged", ["npz", "fit", "queries", "selected"])
def test_resume_validates_committed_payload_and_can_restore_explicitly(prepared, tmp_path, damaged):
    out = prepared[-1]
    assert job.execute(*prepared, require_all=True) == 0
    state = job.read(out / "SCENARIO_INDEX.json")
    snapshot = tmp_path / "snapshot"
    shutil.copytree(out, snapshot)
    stem = state["jobs"][0]["stem"]
    name = {"npz": stem + ".npz", "fit": stem + "-FIT.json", "queries": stem + "-QUERIES.jsonl",
            "selected": "selected/SELECTED_FIELD.npz"}[damaged]
    if damaged == "npz":
        (out / name).unlink()
    else:
        (out / name).write_bytes(b"stale bytes")
    if damaged != "selected":
        with pytest.raises(ValueError, match=name):
            job.execute(*prepared, resume=True, require_all=True)
    else:
        with pytest.raises(ValueError, match="SELECTED_FIELD"):
            job.execute(*prepared, resume=True, require_all=True)
    assert job.read(out / "SCENARIO_INDEX.json") == state
    assert job.execute(*prepared, resume=True, restore_from=snapshot, require_all=True) == 0
    assert inventory(out) == inventory(snapshot)
    assert job.execute(*prepared, resume=True, restore_from=snapshot, require_all=True) == 0


def test_partial_write_without_marker_reruns_only_uncommitted_job(prepared, monkeypatch):
    out = prepared[-1]
    assert job.execute(*prepared, stop_after=1) == 2
    first = job.read(out / "SCENARIO_INDEX.json")["jobs"][0]
    original_publish = job._publish_stage

    def interrupted(stage, directory, names):
        field = next(name for name in names if name.endswith(".npz"))
        original_publish(stage, directory, {field})
        raise RuntimeError("interrupted before the job commit marker")

    with monkeypatch.context() as patch:
        patch.setattr(job, "_publish_stage", interrupted)
        with pytest.raises(RuntimeError, match="commit marker"):
            job.execute(*prepared, resume=True, require_all=True)
    assert job.read(out / "SCENARIO_INDEX.json")["jobs"] == [first]
    assert job.execute(*prepared, resume=True, require_all=True) == 0
    assert job.read(out / "SCENARIO_INDEX.json")["jobs"][0] == first
    query = json.loads((job.read_committed_selection(out) / "TARGET_QUERIES.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert query["bound_factor_ids"] == ["native-md"]
    assert query["own_factor_ids"] == []
    assert query["source_attachment_count"] == 1
    assert query["spatial_role"] == "CONDITIONAL_MODEL_CONTINUATION"

def test_selected_bundle_is_complete_before_marker_and_hidden_from_consumer(prepared, monkeypatch):
    out = prepared[-1]
    original_write = job.write

    def interrupted(path, value):
        if path == out / "SCENARIO_INDEX.json" and value["selected"] is not None:
            raise RuntimeError("crash before selected commit")
        original_write(path, value)

    with monkeypatch.context() as patch:
        patch.setattr(job, "write", interrupted)
        with pytest.raises(RuntimeError, match="selected commit"):
            job.execute(*prepared, stop_after=1)
    assert not (out / "SELECTED_FIELD.npz").exists()
    assert set(path.name for path in (out / "selected").iterdir()) == job._SELECTED_FILES
    with pytest.raises(ValueError, match="No committed primary field"):
        job.read_committed_selection(out)
    assert job.execute(*prepared, resume=True, require_all=True) == 0
    assert job.read_committed_selection(out) == out / "selected"


def test_duplicate_design_fails_before_any_generation_write(prepared):
    field, out = prepared[-2:]
    path = field / "BHJ_INDEX.json"
    design = job.read(path)
    design["selected"].append(design["selected"][0])
    job.write(path, design)
    with pytest.raises(ValueError, match="Duplicate BHJ"):
        job.execute(*prepared, require_all=True)
    assert not out.exists()

def test_job_refuses_wrong_query_provenance_even_before_artifact_hashes(prepared, monkeypatch):
    original_queries = job.query_rows

    def wrong_query(*args):
        rows = original_queries(*args)
        rows[0]["frame"] = "UNREGISTERED"
        rows[0]["applied_operator_ids"] = ["invented"]
        rows[0]["values_m"] = {"TOP": "not a height", "BASE": 0}
        return rows

    monkeypatch.setattr(job, "query_rows", wrong_query)
    with pytest.raises(ValueError, match="query rows differ"):
        job.execute(*prepared, require_all=True)
    state = job.read(prepared[-1] / "SCENARIO_INDEX.json")
    assert not state["jobs"] and state["selected"] is None
