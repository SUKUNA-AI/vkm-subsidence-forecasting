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
    foundation = Foundation(("A", "N"), (), configs, {"A": {"object_id": "A", "role": "TARGET", "reason": None,
                                      "vertical_links": [{"link_id": "source-record", "ownership": "A"}]},
                                                   "N": {"object_id": "N", "role": "TARGET",
                                                         "vertical_links": [], "reason": "UNKNOWN"}}, "UNKNOWN")
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


@pytest.mark.parametrize("count", [0, 1, 3])
def test_source_count_uses_typed_owned_links(count):
    record = {"object_id": "A", "role": "TARGET", "reason": "UNKNOWN" if count == 0 else None,
              "vertical_links": [{"link_id": f"link-{i}", "ownership": "A"} for i in range(count)]}
    assert job.source_attachment_count({"A": record}, "A") == count
    assert job.source_attachment_count({"A": record}, "missing") == 0


@pytest.mark.parametrize("record", [
    ["untyped-legacy-list"],
    {"object_id": "B", "vertical_links": []},
    {"object_id": "A", "vertical_links": [{"link_id": "L", "ownership": "B"}]},
    {"object_id": "A", "vertical_links": [{"link_id": "L", "ownership": "A"}] * 2},
])
def test_source_count_rejects_wrong_owner_and_untyped_contract(record):
    with pytest.raises(ValueError, match="vertical"):
        job.source_attachment_count({"A": record}, "A")


def test_field_identity_binds_names_frame_and_run_material_context():
    nodes = np.array([[0., 0.], [1., 0.], [0., 1.]])
    triangles = np.array([[0, 1, 2]])
    contacts = np.array([[50., 50., 50.], [18., 18., 18.], [16., 16., 16.]])
    names = ["PDKS", "MG", "MG_BASE"]
    identity = {"configuration_id": "cfg", "material_mapping_digest": "mapping-a"}
    original = job.field_identity(nodes, triangles, contacts, identity, names, "SH1")
    assert original != job.field_identity(nodes, triangles, contacts, identity, names[::-1], "SH1")
    assert original != job.field_identity(nodes, triangles, contacts, identity, names, "UTM40N")
    assert original != job.field_identity(nodes, triangles, contacts,
                                           {**identity, "material_mapping_digest": "mapping-b"}, names, "SH1")


@pytest.mark.parametrize("artifact", ["PROFILES.json", "TARGET_QUERIES.jsonl", "SELECTED_FIELD.npz"])
def test_reader_rejects_resealed_semantic_mismatch(prepared, artifact):
    out = prepared[-1]
    assert job.execute(*prepared, require_all=True) == 0
    state = job.read(out / "SCENARIO_INDEX.json")
    selected = out / "selected"
    path = selected / artifact
    if artifact == "PROFILES.json":
        data = job.read(path)
        data["frame"] = "wrong-frame"
        job.write(path, data)
    elif artifact == "TARGET_QUERIES.jsonl":
        rows = job._read_rows(path)
        rows[0]["values_m"]["TOP"] += 5
        job.jsonl(path, rows)
    else:
        with np.load(path, allow_pickle=False) as stored:
            data = {key: stored[key] for key in stored.files}
        data["contact_ids"] = data["contact_ids"][::-1]
        np.savez_compressed(path, **data)
    signature = job._file_signature(path)
    state["selected"]["artifacts"][artifact] = signature
    aliases = {"SELECTED_FIELD.npz": ".npz", "TARGET_QUERIES.jsonl": "-QUERIES.jsonl"}
    if artifact in aliases:
        chosen = next(row for row in state["jobs"] if row["identity"] == state["selected"]["identity"])
        chosen["artifacts"][chosen["identity"]["stem"] + aliases[artifact]] = signature
    job.write(out / "SCENARIO_INDEX.json", state)
    with pytest.raises(ValueError, match="semantic identity"):
        job.read_committed_selection(out)


def test_primary_failure_selects_next_input_candidate_deterministically(prepared, monkeypatch):
    original = job._fit
    def fit(*args):
        problem, result = original(*args)
        if args[3].id == "first":
            result = {**result, "contacts": None, "status": "INFEASIBLE"}
        return problem, result
    monkeypatch.setattr(job, "_fit", fit)
    assert job.execute(*prepared) == 0
    state = job.read(prepared[-1] / "SCENARIO_INDEX.json")
    assert state["selection_policy"] == job._SELECTION_POLICY
    assert state["selected"]["identity"]["configuration_id"] == "second"
    assert job.read_committed_selection(prepared[-1]) == prepared[-1] / "selected"



def test_selection_order_is_input_defined_and_independent_of_manifest_listing(prepared):
    field = prepared[-2]
    path = field / "BHJ_INDEX.json"
    payload = job.read(path)
    payload["selected"].reverse()
    job.write(path, payload)
    assert job.execute(*prepared, require_all=True) == 0
    selected = job.read(prepared[-1] / "SCENARIO_INDEX.json")["selected"]
    assert selected["identity"]["configuration_id"] == "first"


@pytest.mark.parametrize("change", ["source_count", "queries_removed", "position", "membership",
                                   "column_role", "spatial_role", "unknown_reason", "profile_removed",
                                   "profile_station", "profile_id", "profile_context"])
def test_reader_requires_bound_source_queries_and_metric_profile_inventory(prepared, change):
    assert job.execute(*prepared, require_all=True) == 0
    out = prepared[-1]
    state = job.read(out / "SCENARIO_INDEX.json")
    artifact = "PROFILES.json" if change.startswith("profile_") else "TARGET_QUERIES.jsonl"
    path = out / "selected" / artifact
    if artifact == "TARGET_QUERIES.jsonl":
        rows = job._read_rows(path)
        if change == "source_count": rows[0]["source_attachment_count"] = 999
        if change == "queries_removed": rows = []
        if change == "position": rows[0]["xy"] = [0, 0]  # same values in the constant test field
        if change == "membership": rows[0]["membership"] = "SUPPORT"
        if change == "column_role": rows[0]["column_role"] = "MEASURED"
        if change == "spatial_role": rows[0]["spatial_role"] = "BORING_RELATIVE_INPUT"
        if change == "unknown_reason": rows[1]["no_numeric_query_reason"] = None
        job.jsonl(path, rows)
    else:
        data = job.read(path)
        if change == "profile_removed": data["profiles"] = []
        if change == "profile_station":
            data["profiles"][0]["station_m"] = [v * 1000 for v in data["profiles"][0]["station_m"]]
        if change == "profile_id": data["profiles"][0]["id"] = "substituted"
        if change == "profile_context": data["native_context_profiles"] = [{"id": "unbound"}]
        job.write(path, data)
    signature = job._file_signature(path)
    state["selected"]["artifacts"][artifact] = signature
    if artifact == "TARGET_QUERIES.jsonl":
        anchor = next(row for row in state["jobs"] if row["identity"] == state["selected"]["identity"])
        anchor["artifacts"][anchor["identity"]["stem"] + "-QUERIES.jsonl"] = signature
    job.write(out / "SCENARIO_INDEX.json", state)
    with pytest.raises(ValueError, match="semantic identity"):
        job.read_committed_selection(out)


def test_actual_planned_identity_binds_explicit_material_mapping(prepared):
    manifest, inputs, field, out = prepared
    policy_path = inputs / "FIELD_POLICY.json"
    policy = job.read(policy_path)
    mapping = {"schema": "vkm.interval-semantics/1", "frame_id": "SH1",
               "source_identity": sha256(manifest.read_bytes()).hexdigest(),
               "intervals": [{"upper_contact_id": "TOP", "lower_contact_id": "BASE",
                              "unit_id": "synthetic-rock", "material_id": "material-a",
                              "role": "GEOLOGICAL_UNIT",
                              "source_refs": [{"source_id": "synthetic", "locator": "table 1"}]}]}
    job.write(policy_path, {**policy, "interval_semantics": mapping})
    assert job.execute(*prepared, require_all=True) == 0
    first = job.read(out / "SCENARIO_INDEX.json")
    identity = first["selected"]["identity"]
    assert identity["material_mapping_sha256"] == job.digest(mapping)
    assert job.read_committed_selection(out) == out / "selected"
    mapping["intervals"][0]["material_id"] = "material-b"
    job.write(policy_path, {**policy, "interval_semantics": mapping})
    other = out.with_name("different-material")
    assert job.execute(manifest, inputs, field, other, require_all=True) == 0
    second = job.read(other / "SCENARIO_INDEX.json")
    assert identity != second["selected"]["identity"]
    assert first["jobs"][0]["geometry_id"] != second["jobs"][0]["geometry_id"]
    with np.load(out / "selected/SELECTED_FIELD.npz") as a, np.load(other / "selected/SELECTED_FIELD.npz") as b:
        np.testing.assert_array_equal(a["contacts"], b["contacts"])
    assert job.read_committed_selection(other) == other / "selected"
    # Even a newly signed marker cannot relabel a field whose job identity binds the old map.
    first["input_components"]["interval_semantics"] = mapping
    first["input_signature"] = job.digest(first["input_components"])
    job.write(out / "SCENARIO_INDEX.json", first)
    with pytest.raises(ValueError, match="material semantic identity"):
        job.read_committed_selection(out)


@pytest.mark.parametrize("change", ["source_identity", "pair"])
def test_planning_rejects_unqualified_material_mapping(prepared, change):
    manifest, inputs, _, out = prepared
    policy_path = inputs / "FIELD_POLICY.json"
    mapping = {"schema": "vkm.interval-semantics/1", "frame_id": "SH1",
               "source_identity": sha256(manifest.read_bytes()).hexdigest(),
               "intervals": [{"upper_contact_id": "TOP", "lower_contact_id": "BASE",
                              "unit_id": None, "material_id": None, "role": "UNKNOWN_LITHOLOGY",
                              "source_refs": [{"source_id": "synthetic", "locator": "unknown"}]}]}
    if change == "source_identity": mapping["source_identity"] = "another-input"
    if change == "pair": mapping["intervals"][0]["upper_contact_id"] = "BASE"
    job.write(policy_path, {**job.read(policy_path), "interval_semantics": mapping})
    with pytest.raises(ValueError, match="identity mismatch|boundary pair"):
        job.execute(*prepared)
    assert not out.exists()


@pytest.mark.parametrize("statuses,admission,expected", [
    (["FACT"], "HARD", "BORING_RELATIVE_INPUT"),
    (["DERIVATION"], "SOFT", "BORING_RELATIVE_INPUT"),
    (["MODEL_CHOICE"], "HARD", "CONDITIONAL_BORING_RELATIVE_INPUT"),
    (["MODEL_CHOICE"], "SOFT", "CONDITIONAL_BORING_RELATIVE_INPUT"),
    (["ENGINEERING_ASSUMPTION"], "HARD", "CONDITIONAL_BORING_RELATIVE_INPUT"),
    (["FACT", "MODEL_CHOICE"], "HARD", "MIXED_BORING_RELATIVE_INPUT"),
    (["MODEL_CHOICE", "FACT"], "SOFT", "MIXED_BORING_RELATIVE_INPUT"),
])
def test_query_role_uses_actual_compiled_source_conditional_and_mixed_inputs(prepared, statuses, admission, expected):
    inputs, out = prepared[1], prepared[-1]
    path = inputs / "TYPED_VERTICAL_FACTORS.jsonl"
    factors = job._read_rows(path)
    for index, status in enumerate(statuses):
        factors.append({"factor_id": f"qualified-thickness-{index}", "physical_id": "A",
                        "interpretation_id": "ROOF", "admission": admission, "scale_m": 1.,
                        "quantity": "THICKNESS_VERTICAL", "epistemic_status": status,
                        "support": {"kind": "NONE"}, "contact_ids": ["TOP", "BASE"],
                        "source_refs": [{"source_id": "synthetic", "record_id": f"source-{index}"}],
                        "bounds": {"lower": 20., "upper": 20., "unit": "m"},
                        "independence_group": f"source-{index}"})
        if status == "MODEL_CHOICE":
            # Match the actual shared-source-offset contract, rather than add
            # duplicate hard thickness equations at one point.
            factors[-1].update(quantity="ELEVATION_LOCAL", contact_ids=["TOP"],
                               datum_mode="LATENT_SOURCE_OFFSET", datum_group="synthetic-source-origin",
                               bounds={"lower": 50., "upper": 50., "unit": "m"})
    job.jsonl(path, factors)
    assert job.execute(*prepared, require_all=True) == 0
    bundle = job.read_committed_selection(out)
    query = job._read_rows(bundle / "TARGET_QUERIES.jsonl")[0]
    ledger = job.read(bundle / "APPLIED_OPERATORS.json")
    # All positions have QUALIFIED_LOCAL_MAP_CONTROL: it must not erase the
    # compiler's MODEL_CHOICE/assumption distinction or the presence of mixed data.
    assert all(f["support"]["spatial_provenance"]["application_role"] == "QUALIFIED_LOCAL_MAP_CONTROL"
               for f in ledger["factors"])
    assert query["spatial_role"] == expected
    assert query["column_role"] == "MODEL_RESULT"
    assert query["source_attachment_count"] == 1
    assert len(query["own_factor_ids"]) == len(statuses)
    if expected == "BORING_RELATIVE_INPUT":
        return
    state = job.read(out / "SCENARIO_INDEX.json")
    query_path = bundle / "TARGET_QUERIES.jsonl"
    rows = job._read_rows(query_path)
    rows[0]["spatial_role"] = "BORING_RELATIVE_INPUT"
    job.jsonl(query_path, rows)
    signature = job._file_signature(query_path)
    state["selected"]["artifacts"]["TARGET_QUERIES.jsonl"] = signature
    anchor = next(row for row in state["jobs"] if row["identity"] == state["selected"]["identity"])
    anchor["artifacts"][anchor["identity"]["stem"] + "-QUERIES.jsonl"] = signature
    job.write(out / "SCENARIO_INDEX.json", state)
    with pytest.raises(ValueError, match="query semantic identity"):
        job.read_committed_selection(out)
    # Altering both the factor disposition and operator role cannot launder an
    # explicitly conditional source status into a DATA-only input.
    changed = next(f for f in ledger["factors"] if f["source_epistemic_status"] != "FACT")
    changed["compiler_disposition"] = "DATA_HARD"
    for operator in ledger["operators"]:
        if operator["id"] in changed["operator_ids"]:
            operator["role"] = "DATA_HARD"
    with pytest.raises(ValueError, match="source-only compiled role"):
        job._input_support_roles(ledger)


def test_query_input_classification_rejects_inconsistent_compiler_ledger():
    factor = {"factor_id": "f", "physical_id": "A", "operator_ids": ["op"],
              "compiler_disposition": "CONDITIONAL_DATA_HARD", "source_epistemic_status": "MODEL_CHOICE",
              "support": {"spatial_provenance": {"application_role": "QUALIFIED_LOCAL_MAP_CONTROL"}}}
    for operators in ([{"id": "op", "role": "DATA_HARD"}], [],
                      [{"id": "op", "role": "CONDITIONAL_DATA_HARD"}] * 2):
        with pytest.raises(ValueError, match="compiled operator|Duplicate compiled"):
            job._input_support_roles({"factors": [factor], "operators": operators})
