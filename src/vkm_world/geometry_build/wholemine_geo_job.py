"""Run auditable wholemine reconstruction branches from an accepted BHJ input."""

from __future__ import annotations
import argparse
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import asdict
from hashlib import file_digest, sha256
from importlib.metadata import version
from numbers import Real
import json
import os
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import time

from . import geology_area_operators, geology_reconstruction, joint_borehole_handoff, wholemine_foundation

import numpy as np
from shapely import from_wkt
from shapely.geometry import LineString, Point

from .geology_area_operators import AreaOperators
from .wholemine_foundation import bind_factors, load_footprint, load_foundation
from .geology_reconstruction import compile_model, evaluate_contacts, solve_model
from ..governance.publication import publication_lock


def read(path):
    return json.loads(Path(path).read_bytes())


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def write(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
                         encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def jsonl(path, rows):
    with Path(path).open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n")


def field_identity(nodes, triangles, contacts, identity, contact_ids, frame):
    """Bind numerical geometry and its geological meaning in the same identity."""
    return "C1-" + digest({
        "schema": "vkm.geological-field/2", "run_identity": identity,
        "xy": nodes.tolist(), "triangles": triangles.tolist(), "contacts": contacts.tolist(),
        "contact_ids": list(contact_ids), "frame": frame,
        "vertical_frame": "LOCAL_SCENARIO_VERTICAL_GAUGE",
    })[:24]


def pack_field(out, nodes, tri, result, identity, frame):
    coefficients = result["contacts"]
    geometry_id = field_identity(nodes, tri, coefficients, identity, result["contact_ids"], frame)
    np.savez_compressed(out, xy=nodes, triangles=tri, z=coefficients[::-1].T, contacts=coefficients,
                       horizon_names=np.asarray(result["contact_ids"][::-1]),
                       contact_ids=np.asarray(result["contact_ids"]), geometry_id=np.array(geometry_id),
                       bhj_id=np.array(result["configuration_id"]), frame=np.array(frame),
                       field_schema=np.array("vkm.geological-field/2"),
                       vertical_frame=np.array("LOCAL_SCENARIO_VERTICAL_GAUGE"))
    return geometry_id


def _interp_rows(problem, result, positions):
    numeric = [row for row in positions if row["xy"] is not None and row["frame"] == problem.frame_id]
    values = evaluate_contacts(problem, result["contacts"], [row["xy"] for row in numeric])
    return {row["object_id"]: value for row, value in zip(numeric, values, strict=True) if np.isfinite(value).all()}


def applied_operators(problem, bound):
    """Keep compiler outcomes separate from source attachments and spatial binding."""
    by_factor = {row["factor_id"]: row for row in problem.dispositions}
    operators = {row.id: asdict(row) for row in problem.rows}
    factors = []
    for row in bound:
        factor = by_factor[row["factor_id"]]
        factors.append({
            "factor_id": row["factor_id"], "physical_id": row["physical_id"],
            "source_refs": row["source_refs"], "source_epistemic_status": row["epistemic_status"],
            "support": row["support"], "compiler_disposition": factor["reason"],
            "operator_ids": factor["operator_rows"],
        })
    return {"variable_ids": list(problem.variable_ids), "factors": factors,
            "operators": list(operators.values())}


def source_attachment_count(verticals, object_id):
    """Count owned source links in the accepted records_by_object contract."""
    record = verticals.get(object_id)
    if record is None:
        return 0
    if not isinstance(record, Mapping) or record.get("object_id") != object_id:
        raise ValueError("Invalid vertical record owner")
    links = record.get("vertical_links")
    if not isinstance(links, list):
        raise ValueError("Expected typed vertical_links list")
    identifiers = set()
    for link in links:
        if (not isinstance(link, Mapping) or link.get("ownership") != object_id
                or not isinstance(link.get("link_id"), str) or not link["link_id"]
                or link["link_id"] in identifiers):
            raise ValueError("Invalid or duplicate vertical link ownership")
        identifiers.add(link["link_id"])
    return len(links)


def query_rows(problem, result, config, geometry_id, compiled, verticals):
    numeric = _interp_rows(problem, result, config.positions)
    direct = defaultdict(list)
    for condition in compiled["factors"]:
        direct[condition["physical_id"]].append(condition["factor_id"])
    applied = defaultdict(list)
    for condition in compiled["factors"]:
        if condition["operator_ids"]:
            applied[condition["physical_id"]].append(condition)
    rows = []
    for row in config.positions:
        key = row["object_id"]
        values = numeric.get(key)
        reason = "UNKNOWN_XY" if row["xy"] is None else "OUTSIDE_DOMAIN_OR_NATIVE_ONLY"
        own = applied[key]
        if own:
            conditional = all(condition["support"].get("spatial_provenance", {}).get("application_role")
                              == "CONDITIONAL_SCENARIO_OPERATOR" for condition in own)
            support_role = "CONDITIONAL_BORING_RELATIVE_INPUT" if conditional else "BORING_RELATIVE_INPUT"
        else:
            support_role = "CONDITIONAL_MODEL_CONTINUATION"
        rows.append({
            **row, "source_role": row["role"], "scenario_id": geometry_id, "bhj_id": config.id,
            "spatial_role": reason if values is None else support_role,
            "column_role": None if values is None else "MODEL_RESULT",
            "surface_reference": "LOCAL_SCENARIO_VERTICAL_GAUGE",
            "values_m": None if values is None else dict(zip(result["contact_ids"], values.tolist(), strict=True)),
            "source_attachment_count": source_attachment_count(verticals, key),
            "bound_factor_ids": direct[key],
            "own_factor_ids": [condition["factor_id"] for condition in own],
            "applied_operator_ids": [operator for condition in own for operator in condition["operator_ids"]],
            "no_numeric_query_reason": reason if values is None else None,
        })
    return rows


def _profile_bridge(bridge, native, target_frame):
    if (bridge.get("from_frame") != native["frame"] or bridge.get("to_frame") != target_frame
            or bridge.get("registration_sha256") != digest(native) or not bridge.get("id")
            or not bridge.get("source_refs") or bridge.get("admission") != "QUALIFIED_PROFILE_BRIDGE"):
        raise ValueError("Profile bridge does not qualify this native registration and field frame")
    refs = bridge["source_refs"]
    if not isinstance(refs, list) or not all(
            isinstance(ref, dict) and isinstance(ref.get("source_id"), str) and ref["source_id"].strip()
            and any(isinstance(ref.get(key), str) and ref[key].strip() for key in ("locator", "record_id"))
            for ref in refs):
        raise ValueError("Profile bridge requires typed source references")
    values = bridge.get("affine")
    if (not isinstance(values, list) or len(values) != 2
            or any(not isinstance(row, list) or len(row) != 3 for row in values)
            or any(isinstance(v, bool) or not isinstance(v, Real) for row in values for v in row)):
        raise ValueError("Profile bridge needs two numeric affine rows")
    matrix = np.asarray(values, dtype=float)
    if matrix.shape != (2, 3) or not np.isfinite(matrix).all() or np.linalg.matrix_rank(matrix[:, :2]) != 2:
        raise ValueError("Profile bridge requires an invertible finite 2D affine map")
    return matrix


def _profile_axes(config, polygon, bridges=None):
    """Derive profile inventory from inputs, independently of a solved field."""
    center = polygon.representative_point()
    xmin,ymin,xmax,ymax = polygon.bounds
    paths = [
        ("FIELD-EW", "Через западную и восточную части",
         LineString([(xmin,center.y),(xmax,center.y)]), ["SKRU1-DECLARED-C1-FOOTPRINT"], {}),
        ("FIELD-SN", "Через южную и северную части",
         LineString([(center.x,ymin),(center.x,ymax)]), ["SKRU1-DECLARED-C1-FOOTPRINT"], {}),
    ]
    contexts = []
    for profile in config.profiles:
        registration = profile["registration"]
        native = {"frame": profile["frame"], "registration": registration, "stations": profile["stations"]}
        context = {"id": profile["id"], "bhj_id": config.id, "native": native}
        if not registration or not profile["stations"]:
            contexts.append({**context, "disposition": "NATIVE_CONTEXT_WITHOUT_REGISTERED_AXIS"})
            continue
        origin = np.asarray(registration["origin"])
        angle = registration["angle_rad"]
        direction = np.array([np.cos(angle),np.sin(angle)])
        along = [row["station"] for row in profile["stations"]]
        endpoints = np.array([origin + min(along)*direction, origin + max(along)*direction])
        metadata = {"native": native, "native_direction": direction.tolist(),
                    "native_station_m": np.linspace(min(along), max(along), 257).tolist()}
        if native["frame"] != config.frame:
            bridge = (bridges or {}).get(profile["id"])
            if bridge is None:
                contexts.append({**context, "disposition": "NATIVE_CONTEXT_WITHOUT_QUALIFIED_BRIDGE"})
                continue
            matrix = _profile_bridge(bridge, native, config.frame)
            endpoints = endpoints @ matrix[:, :2].T + matrix[:, 2]
            metadata["frame_bridge"] = bridge
        path = LineString(endpoints)
        paths.append((profile["id"], "Профиль по привязке BHJ", path, [profile["id"],config.id], metadata))
    entries=[]
    for key,label,path,refs,metadata in paths:
        stations=np.linspace(0,path.length,257)
        xy = [list(path.interpolate(value).coords[0]) for value in stations]
        entries.append({
            **metadata, "id":key,"label":label,"xy":xy,"station_m":stations.tolist(),"source_refs":refs,
            "vertical_exaggeration":1.,
        })
    return entries, contexts


def profiles(problem, result, config, polygon, geometry_id, bridges=None):
    if config.frame != problem.frame_id:
        raise ValueError("BHJ field configuration frame differs from the reconstructed field")
    axes, contexts = _profile_axes(config, polygon, bridges)
    entries = []
    for axis in axes:
        result_z = evaluate_contacts(problem, result["contacts"], axis["xy"])
        entries.append({**axis, "values_m": {
                contact:[float(value) if np.isfinite(value) else None for value in result_z[:,i]]
                for i,contact in enumerate(result["contact_ids"])
            }})
    return {"scenario_id":geometry_id,"frame":config.frame,"vertical_frame":"LOCAL_SCENARIO_VERTICAL_GAUGE",
            "profiles":entries,"native_context_profiles":contexts}


def compile_area_factors(records, nodes, triangles, frame):
    ops = AreaOperators(nodes, triangles)
    output, omitted = [], []
    for row in records:
        polygon = from_wkt(row["shape_SH1"])
        stencil, measured_area = ops.stencil(polygon)
        if stencil is None:
            omitted.append({"id": row["id"], "reason": "OUTSIDE_DOMAIN", "covered_area_m2": measured_area})
            continue
        contacts = ["SURFACE", row["expected_roof"]]
        scale = {"VKM-SRC-023": 25., "VKM-SRC-045": 40., "VKM-SRC-047": 25.}[row["source"]]
        output.append({
            "factor_id": row["id"], "physical_id": None, "quantity": "THICKNESS_VERTICAL",
            "contact_ids": contacts, "bounds": {"lower": row["source_m"], "upper": row["source_m"], "unit": "m"},
            "epistemic_status": "MODEL_CHOICE", "admission": "SOFT", "scale_m": scale,
            "interpretation_id": "CONDITIONAL_AREAL_PROXY", "independence_group": row["source"],
            "source_refs": [{"source_id": row["source"], "locator": "; ".join(row["source_locators"]),
                             "record_id": row["id"], "origin_role": "DERIVED_FROM_SOURCE"}],
            "support": {"kind": "AREA_MEAN", "frame_id": frame, "node_indices": stencil[0],
                        "weights": stencil[1], "basis": row["model_operator"] + ": " + row["domain_status"]},
            "area_geometry_wkt": row["shape_SH1"], "area_m2": measured_area,
        })
    return output, omitted


def registered_native_source(row, branches):
    if row["interpretation_id"] not in branches:
        return row
    native = row["support"]
    if row["physical_id"] != native["native_geometry_ref"]:
        raise ValueError("Native profile owner does not match the declared registered object")
    # Select an explicit vertical shaft hypothesis; no historic collar is fixed.
    return {**row, "interpretation_id": "COMMON",
            "depth_basis": "DECLARED_VERTICAL_SCENARIO",
            "support": {**native, "kind": "NONE"}}


def _fit(nodes, triangle, contact_ids, config, factors, priors, policy, branch):
    problem=compile_model(nodes,triangle,contact_ids,factors,frame_id=config.frame,
                          configuration_id=config.id,priors=priors,scenario_basis=branch)
    return problem, solve_model(problem,policy)


_RUN_SCHEMA = "vkm.wholemine-geo-run/4"
_SELECTION_POLICY = "FIRST_CLOSED_PRIMARY_IN_INPUT_REPRESENTATIVE_THEN_ID_ORDER"
_STATIC_FILES = {"ALL_CONFIGURATIONS_LEDGER.jsonl", "AREAL_OPERATOR_DISPOSITION.json"}
_SELECTED_FILES = {"SELECTED_FIELD.npz", "TARGET_QUERIES.jsonl", "ACTIVE_FACTORS.jsonl",
                   "SELECTED_IDENTITY.json", "PROFILES.json", "APPLIED_OPERATORS.json"}


def _file_signature(path):
    with path.open("rb") as stream:
        checksum = file_digest(stream, "sha256").hexdigest()
        return {"sha256": checksum, "bytes": stream.tell()}


def _file_manifest(directory, names):
    return {name: _file_signature(directory / name) for name in sorted(names)}


def _read_rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _job_identity(config, branch, completion, reason, consumer_contract, material_mapping_sha256):
    contract_sha256 = digest(consumer_contract)
    stem = "FIELD-" + digest([config.id, branch, completion, contract_sha256, material_mapping_sha256])[:16]
    return {"stem": stem, "configuration_id": config.id, "spatial_branch": config.branch,
            "branch": branch, "completion": completion, "design_reason": reason,
            "consumer_contract_sha256": contract_sha256, "material_mapping_sha256": material_mapping_sha256}


def _consumer_contract(config, verticals, polygon, bridges):
    axes, contexts = _profile_axes(config, polygon, bridges)
    if (not config.positions or len({row["object_id"] for row in config.positions}) != len(config.positions)
            or len({axis["id"] for axis in axes}) != len(axes)):
        raise ValueError("Unique nonempty query and profile input inventories are required")
    links = {}
    for position in config.positions:
        key = position["object_id"]
        source_attachment_count(verticals, key)
        links[key] = {"object_id": key, "vertical_links": [
            {"link_id": link["link_id"], "ownership": link["ownership"]}
            for link in verticals.get(key, {}).get("vertical_links", [])]}
    return {"frame": config.frame, "positions": list(config.positions), "source_links": links,
            "profiles": [{"id": axis["id"], "axis_sha256": digest(axis)} for axis in axes],
            "native_context_profiles_sha256": digest(contexts)}


def _job_files(record):
    stem = record["identity"]["stem"]
    names = {stem + "-FIT.json", stem + "-OPERATORS.json"}
    if record["geometry_id"] is not None:
        names.update({stem + ".npz", stem + "-QUERIES.jsonl"})
    if record["incumbent"]:
        names.add(stem + "-INCUMBENT.npz")
    return names


def _check_files(directory, recorded, names):
    if set(recorded) != names:
        raise ValueError("Incomplete or unexpected committed artifact inventory")
    for name in sorted(names):
        path = directory / name
        if not path.is_file() or path.is_symlink() or _file_signature(path) != recorded[name]:
            raise ValueError(f"Committed artifact failed size/SHA verification: {name}")


def _check_job(directory, record, identity, config, contact_ids, nodes, triangles, verticals):
    if record["identity"] != identity or record["geometry_id"] is not None and record["status"] != "MODEL_CLOSED_WITH_ASSUMPTIONS":
        raise ValueError("Committed job identity or status differs from the plan")
    _check_files(directory, record["artifacts"], _job_files(record))
    stem = identity["stem"]
    fit = read(directory / (stem + "-FIT.json"))
    operator_ledger = read(directory / (stem + "-OPERATORS.json"))
    if (fit["run_identity"] != identity or fit["status"] != record["status"]
            or fit["configuration_id"] != config.id or fit["scenario_basis"] != identity["branch"]
            or operator_ledger["run_identity"] != identity):
        raise ValueError("FIT or operator ledger identity differs from the committed job")
    expected_ids = {row["id"] for row in operator_ledger["operators"]}
    if len(expected_ids) != len(operator_ledger["operators"]):
        raise ValueError("Committed operator ledger contains duplicate identities")
    own = defaultdict(list)
    applied_ids = defaultdict(list)
    for factor in operator_ledger["factors"]:
        if not expected_ids.issuperset(factor["operator_ids"]):
            raise ValueError("Applied factor refers to a missing compiled operator")
        if factor["operator_ids"]:
            own[factor["physical_id"]].append(factor["factor_id"])
            applied_ids[factor["physical_id"]].extend(factor["operator_ids"])
    geometry_id = record["geometry_id"]
    if geometry_id is None:
        return
    with np.load(directory / (stem + ".npz"), allow_pickle=False) as field:
        contacts = field["contacts"]
        identifier = field_identity(nodes, triangles, contacts, identity, contact_ids, config.frame)
        if (identifier != geometry_id or field["geometry_id"].item() != identifier
                or field["field_schema"].item() != "vkm.geological-field/2"
                or field["vertical_frame"].item() != "LOCAL_SCENARIO_VERTICAL_GAUGE"
                or field["bhj_id"].item() != config.id or field["frame"].item() != config.frame
                or field["contact_ids"].tolist() != contact_ids
                or not np.array_equal(field["xy"], nodes) or not np.array_equal(field["triangles"], triangles)
                or contacts.shape != (len(contact_ids), len(nodes)) or not np.isfinite(contacts).all()
                or contacts.shape != (len(contact_ids), len(field["xy"])) or not np.isfinite(contacts).all()
                or len(set(contact_ids)) != len(contact_ids)
                or not np.array_equal(field["z"], contacts[::-1].T)
                or field["horizon_names"].tolist() != contact_ids[::-1]):
            raise ValueError("Field payload differs from committed geometry identity")
    queries = _read_rows(directory / (stem + "-QUERIES.jsonl"))
    if len(queries) != len(config.positions):
        raise ValueError("Committed query inventory is incomplete")
    carrier = SimpleNamespace(nodes=nodes, triangles=triangles, contact_ids=contact_ids, frame_id=config.frame)
    values = _interp_rows(carrier, {"contacts": contacts}, config.positions)
    for row, position in zip(queries, config.positions, strict=True):
        key = position["object_id"]
        expected = dict(zip(contact_ids, values[key].tolist(), strict=True)) if key in values else None
        reported = row["values_m"]
        if (row["scenario_id"] != geometry_id or row["bhj_id"] != config.id
                or any(row.get(name) != value for name, value in position.items())
                or row["source_attachment_count"] != source_attachment_count(verticals, key)
                or row["own_factor_ids"] != own[key] or row["applied_operator_ids"] != applied_ids[key]
                or (reported is not None and any(isinstance(value, bool) for value in reported.values()))
                or reported != expected or row["column_role"] != ("MODEL_RESULT" if key in values else None)):
            raise ValueError("Committed query rows differ from field or compiled operators")


def _check_selected(directory, state, roof, by_id):
    chosen = next((row for row in state["jobs"] if row["geometry_id"] is not None
                   and row["identity"]["branch"] == roof), None)
    selected = state["selected"]
    if chosen is None:
        if selected is not None:
            raise ValueError("Selected output lacks a committed job")
        return
    if selected is None or selected["identity"] != chosen["identity"]:
        raise ValueError("Selected field does not refer to the committed primary job")
    directory = directory / "selected"
    if directory.is_symlink():
        raise ValueError("Selected bundle is not an owned generation directory")
    _check_files(directory, selected["artifacts"], _SELECTED_FILES)
    stem = chosen["identity"]["stem"]
    for source, alias in ((stem + ".npz", "SELECTED_FIELD.npz"),
                          (stem + "-QUERIES.jsonl", "TARGET_QUERIES.jsonl"),
                          (stem + "-OPERATORS.json", "APPLIED_OPERATORS.json")):
        if chosen["artifacts"][source] != selected["artifacts"][alias]:
            raise ValueError("Selected field or companion does not match committed job")
    identity = {**chosen["identity"], "geometry_id": chosen["geometry_id"]}
    profile = read(directory / "PROFILES.json")
    if (read(directory / "SELECTED_IDENTITY.json") != identity
            or profile["scenario_id"] != identity["geometry_id"]
            or profile["frame"] != by_id[identity["configuration_id"]].frame):
        raise ValueError("Selected field identity or profile frame mismatch")
    active_ids = [row["factor_id"] for row in _read_rows(directory / "ACTIVE_FACTORS.jsonl")]
    compiled = read(directory / "APPLIED_OPERATORS.json")
    if active_ids != [row["factor_id"] for row in compiled["factors"] if row["operator_ids"]]:
        raise ValueError("Selected active factors differ from compiled operators")


def read_committed_selection(directory):
    """Resolve the selected bundle through its marker before opening consumer data."""
    directory = Path(directory)
    state = read(directory / "SCENARIO_INDEX.json")
    if (state.get("schema") != _RUN_SCHEMA or state.get("selection_policy") != _SELECTION_POLICY
            or digest(state["input_components"]) != state["input_signature"]):
        raise ValueError("Unqualified generation marker")
    planned = state["input_components"]["jobs"]
    if (not planned or state["input_components"].get("selection_policy") != _SELECTION_POLICY
            or [record["identity"] for record in state["jobs"]] != planned[:len(state["jobs"])]):
        raise ValueError("Selected generation does not follow the declared input order")
    selected = state["selected"]
    if selected is None:
        raise ValueError("No committed primary field")
    primary = planned[0]["branch"]
    chosen = next((record for record in state["jobs"]
                   if record["identity"]["branch"] == primary and record["geometry_id"] is not None), None)
    if chosen is None or chosen["identity"] != selected["identity"]:
        raise ValueError("Selected generation violates its declared primary selection rule")
    identity = selected["identity"]
    contract = state["input_components"]["consumer_contracts"][identity["configuration_id"]]
    mapping = state["input_components"]["interval_semantics"]
    mapping_sha256 = digest(mapping) if mapping is not None else None
    if (identity["consumer_contract_sha256"] != digest(contract)
            or identity["material_mapping_sha256"] != mapping_sha256):
        raise ValueError("Selected input or material semantic identity mismatch")
    bundle = directory / "selected"
    if bundle.is_symlink():
        raise ValueError("Selected bundle is not an owned generation directory")
    _check_files(bundle, selected["artifacts"], _SELECTED_FILES)
    anchor = next((row for row in state["jobs"] if row["identity"] == identity), None)
    if anchor is None or anchor["geometry_id"] is None:
        raise ValueError("Selected bundle lacks a committed geometry")
    stem = identity["stem"]
    aliases = {stem + ".npz": "SELECTED_FIELD.npz", stem + "-QUERIES.jsonl": "TARGET_QUERIES.jsonl",
               stem + "-OPERATORS.json": "APPLIED_OPERATORS.json"}
    if any(anchor["artifacts"][name] != selected["artifacts"][alias] for name, alias in aliases.items()):
        raise ValueError("Selected bundle digests do not match the committed job")
    if read(bundle / "SELECTED_IDENTITY.json") != {**identity, "geometry_id": anchor["geometry_id"]}:
        raise ValueError("Selected bundle identity does not match the marker")
    # Verify semantic payloads as well as their byte signatures. A resealed but
    # inconsistent profile/query/field bundle must not reach CAD or physics.
    with np.load(bundle / "SELECTED_FIELD.npz", allow_pickle=False) as field:
        frame = field["frame"].item()
        contact_ids = field["contact_ids"].tolist()
        contacts = field["contacts"]
        identifier = field_identity(field["xy"], field["triangles"], contacts,
                                    identity, contact_ids, frame)
        if (field["field_schema"].item() != "vkm.geological-field/2"
                or identifier != anchor["geometry_id"] or field["geometry_id"].item() != identifier
                or field["bhj_id"].item() != identity["configuration_id"]
                or field["vertical_frame"].item() != "LOCAL_SCENARIO_VERTICAL_GAUGE"
                or contacts.shape != (len(contact_ids), len(field["xy"])) or not np.isfinite(contacts).all()
                or len(set(contact_ids)) != len(contact_ids)
                or not np.array_equal(field["z"], contacts[::-1].T)
                or field["horizon_names"].tolist() != contact_ids[::-1]):
            raise ValueError("Selected field semantic identity mismatch")
        carrier = SimpleNamespace(nodes=field["xy"], triangles=field["triangles"],
                                  contact_ids=contact_ids, frame_id=frame)
    if frame != contract["frame"]:
        raise ValueError("Selected field frame differs from bound inputs")
    if mapping is not None:
        wholemine_foundation.bind_interval_semantics(
            contact_ids, mapping, frame_id=frame,
            source_identity=state["input_components"]["files"]["bhj"]["sha256"])
    profile = read(bundle / "PROFILES.json")
    if (profile["scenario_id"] != identifier or profile["frame"] != frame
            or profile["vertical_frame"] != "LOCAL_SCENARIO_VERTICAL_GAUGE"):
        raise ValueError("Selected profile semantic identity mismatch")
    queries = _read_rows(bundle / "TARGET_QUERIES.jsonl")
    ledger = read(bundle / "APPLIED_OPERATORS.json")
    if ledger["run_identity"] != identity:
        raise ValueError("Selected operator identity mismatch")
    config = SimpleNamespace(id=identity["configuration_id"], positions=contract["positions"])
    expected_queries = query_rows(carrier, {"contacts": contacts, "contact_ids": contact_ids},
                                  config, identifier, ledger, contract["source_links"])
    if digest(queries) != digest(expected_queries):
        raise ValueError("Selected query semantic identity mismatch: bound inventory, source links or roles")
    axes = [{"id": item["id"], "axis_sha256": digest({key: value for key, value in item.items()
                                                       if key != "values_m"})}
            for item in profile["profiles"]]
    if (axes != contract["profiles"]
            or digest(profile["native_context_profiles"]) != contract["native_context_profiles_sha256"]):
        raise ValueError("Selected profile semantic identity mismatch: bound axis inventory")
    for item in profile["profiles"]:
        xy = np.asarray(item["xy"], dtype=float)
        stations = np.asarray(item["station_m"], dtype=float)
        if (xy.ndim != 2 or xy.shape[1] != 2 or len(xy) < 2 or stations.shape != (len(xy),)
                or not np.isfinite(xy).all() or not np.isfinite(stations).all()
                or stations[0] != 0 or np.any(np.diff(stations) <= 0)
                or not np.allclose(np.diff(stations), np.linalg.norm(np.diff(xy, axis=0), axis=1),
                                   rtol=1e-10, atol=1e-8)
                or set(item["values_m"]) != set(contact_ids)):
            raise ValueError("Selected profile station metric differs from field XY")
        expected = evaluate_contacts(carrier, contacts, item["xy"])
        for index, contact_id in enumerate(contact_ids):
            values = [float(value) if np.isfinite(value) else None for value in expected[:, index]]
            if item["values_m"].get(contact_id) != values:
                raise ValueError("Selected profile values differ from field")
    return bundle


def _check_run(directory, state, signature, planned, roof, by_id, contact_ids, nodes, triangles, verticals):
    if (state.get("schema") != _RUN_SCHEMA or state.get("input_signature") != signature
            or digest(state["input_components"]) != signature):
        raise ValueError("Resumed input, policy or implementation changed; use a new generation")
    if state["planned"] != len(planned) or len(state["jobs"]) > len(planned):
        raise ValueError("Invalid restart progress")
    _check_files(directory, state["static_artifacts"], _STATIC_FILES)
    for record, identity in zip(state["jobs"], planned):
        _check_job(directory, record, identity, by_id[identity["configuration_id"]],
                   contact_ids, nodes, triangles, verticals)
    _check_selected(directory, state, roof, by_id)
    if state["selected"] is not None:
        read_committed_selection(directory)


def _restore_run(output, backup, state):
    records = dict(state["static_artifacts"])
    for job in state["jobs"]:
        records.update(job["artifacts"])
    _restore_files(output, backup, records)
    if state["selected"] is not None:
        _restore_files(output / "selected", backup / "selected", state["selected"]["artifacts"])


def _restore_files(output, backup, records):
    if output.is_symlink():
        raise ValueError("Restore destination is not an owned generation directory")
    _check_files(backup, records, set(records))
    if not output.exists():
        with TemporaryDirectory(prefix=".restore-bundle-", dir=output.parent) as work:
            stage = Path(work) / output.name
            stage.mkdir()
            for name in records:
                shutil.copyfile(backup / name, stage / name)
            _check_files(stage, records, set(records))
            stage.rename(output)
        return
    with TemporaryDirectory(prefix=".restore-", dir=output) as work:
        stage = Path(work)
        for name, record in records.items():
            destination = output / name
            if not destination.is_file() or destination.is_symlink() or _file_signature(destination) != record:
                shutil.copyfile(backup / name, stage / name)
        files = {path.name for path in stage.iterdir()}
        _check_files(stage, {name: records[name] for name in files}, files)
        _publish_stage(stage, output, files)


def _publish_stage(stage, output, names):
    for name in sorted(names):
        os.replace(stage / name, output / name)


def execute(manifest, inputs, field, output, *, pilot=False, resume=False,
            stop_after=None, require_all=False, restore_from=None):
    input_paths = {
        "bhj": Path(manifest), "footprint": field / "FOOTPRINT.geojson", "design": field / "BHJ_INDEX.json",
        "priors": inputs / "PRIORS.json", "policy": inputs / "FIELD_POLICY.json",
        "vertical_factors": inputs / "TYPED_VERTICAL_FACTORS.jsonl",
        "areal_factors": inputs / "CONDITIONAL_AREA_RECORDS.json", "carrier": field / "CARRIER.npz",
    }
    if restore_from is not None and (not resume or output.resolve() == Path(restore_from).resolve()):
        raise ValueError("Restore requires --resume and a separate verified backup")
    input_files = {name: _file_signature(path) for name, path in input_paths.items()}
    foundation=load_foundation(manifest)
    footprint=load_footprint(field/"FOOTPRINT.geojson")
    index=read(field/"BHJ_INDEX.json")
    actual_manifest_sha = sha256(Path(manifest).read_bytes()).hexdigest()
    if actual_manifest_sha != index["bhj_manifest_sha256"]:
        raise ValueError("BHJ index belongs to a different accepted manifest")
    # Preserve the historical input representative, correcting its misleading name.
    # This is an input-only ordering and never a score against validation truth.
    design = [{**row, "reason": wholemine_foundation.representative_reason(row["reason"])}
              for row in index["selected"]]
    design.sort(key=lambda row: (row["reason"] != "WITHIN_BRANCH_MEDIAN_NEAREST", row["id"]))
    by_id={item.id:item for item in foundation.configurations}
    priors=read(inputs/"PRIORS.json")
    policy=read(inputs/"FIELD_POLICY.json")
    contact_ids=priors["contact_ids"]
    factors=[json.loads(line) for line in (inputs/"TYPED_VERTICAL_FACTORS.jsonl").read_text(encoding="utf-8").splitlines()]
    roof=policy["primary_interpretation"]
    floor=policy["alternative_interpretation"]
    branches={roof:policy.get("include_as_common",{}).get(roof,[]),
              floor:policy.get("include_as_common",{}).get(floor,[])}
    with np.load(field/"CARRIER.npz",allow_pickle=False) as geometry:
        nodes,triangles=geometry["xy"],geometry["triangles"]
    if set(config.frame for config in foundation.configurations) != {"SKRU1_LOCAL_SH1"}:
        if read(inputs / "CONDITIONAL_AREA_RECORDS.json"):
            raise ValueError("SH1 area sources require a reconstruction in SH1")
    area_rows, area_omitted = compile_area_factors(
        read(inputs/"CONDITIONAL_AREA_RECORDS.json"), nodes, triangles, "SKRU1_LOCAL_SH1"
    )
    jobs=[]
    all_bindings=[]
    bound_by_id={}
    design_ids = {row["id"] for row in design}
    if len(by_id) != len(foundation.configurations) or len(design_ids) != len(design):
        raise ValueError("Duplicate BHJ configuration in the foundation or run design")
    if len(branches) != 2 or not design_ids or not design_ids <= by_id.keys():
        raise ValueError("Run design and geological branches must match the accepted foundation")
    for config in foundation.configurations:
        states={}
        for branch,native_joins in branches.items():
            selected=[registered_native_source(x, native_joins) for x in factors]
            rows, excluded=bind_factors(selected,config,branch,footprint,
                                       allowed_supports=policy.get("explicit_vertical_supports",[]),
                                       spatial_admissions=policy.get("scenario_spatial_admissions",{}))
            rows += [{**row, "support": {**row["support"],"configuration_id":config.id}} for row in area_rows]
            states[branch]={"bound_factor_ids":[row["factor_id"] for row in rows],"excluded":excluded,
                            "binding_sha256":digest(rows)}
            if config.id in design_ids:
                bound_by_id[(config.id,branch)]=rows
        all_bindings.append({"bhj_id":config.id,"members":[p["object_id"] for p in config.positions],
                             "branch":config.branch,"frame":config.frame,"vertical_stage":states})
    for row in design[:1] if pilot else design:
        jobs.append((row["id"],roof,"baseline",row["reason"]))
        if row["reason"]=="WITHIN_BRANCH_MEDIAN_NEAREST" and not pilot:
            jobs.append((row["id"],floor,"baseline",row["reason"]))
    consumer_contracts = {
        key: _consumer_contract(by_id[key], foundation.verticals, footprint,
                               policy.get("profile_frame_bridges", {}).get(key))
        for key in sorted(design_ids)
    }
    interval_semantics = policy.get("interval_semantics")
    if interval_semantics is not None:
        for frame in {by_id[key].frame for key in design_ids}:
            wholemine_foundation.bind_interval_semantics(
                contact_ids, interval_semantics, frame_id=frame, source_identity=actual_manifest_sha)
    material_mapping_sha256 = digest(interval_semantics) if interval_semantics is not None else None
    planned = [_job_identity(by_id[key], branch, completion, reason, consumer_contracts[key],
                             material_mapping_sha256) for key, branch, completion, reason in jobs]
    if len({identity["stem"] for identity in planned}) != len(planned):
        raise ValueError("Duplicate planned geological job artifact stem")
    implementations = (geology_area_operators, geology_reconstruction, wholemine_foundation,
                       joint_borehole_handoff)
    code = {Path(__file__).name: _file_signature(Path(__file__))}
    code.update({Path(module.__file__).name: _file_signature(Path(module.__file__)) for module in implementations})
    signature_inputs = {
        "files": input_files, "code": code, "python": list(sys.version_info[:3]),
        "runtime": {name: version(name) for name in ("numpy", "scipy", "shapely")},
        "thread_limits": {name: os.getenv(name) for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS")},
        "bound_configurations_sha256": digest(all_bindings),
        "resolved_foundation_sha256": digest(asdict(foundation)),
        "effective_footprint_sha256": sha256(footprint.wkb).hexdigest(),
        "consumer_contracts": consumer_contracts, "interval_semantics": interval_semantics,
        "jobs": planned, "selection_policy": _SELECTION_POLICY,
    }
    signature = digest(signature_inputs)
    if input_files != {name: _file_signature(path) for name, path in input_paths.items()}:
        raise ValueError("An input changed during planning")
    state_path=output/"SCENARIO_INDEX.json"
    if resume:
        if not state_path.is_file() or read(state_path).get("input_signature") != signature:
            raise ValueError("Resumed input, policy or implementation changed; use a new generation")
    else:
        output.mkdir(parents=True, exist_ok=False)
    with publication_lock(output):
        if resume:
            state = read(state_path)
            if restore_from is not None:
                backup = Path(restore_from)
                if read(backup / state_path.name) != state:
                    raise ValueError("Backup does not match the committed generation")
                _check_run(backup, state, signature, planned, roof, by_id, contact_ids, nodes, triangles, foundation.verticals)
                _restore_run(output, backup, state)
            _check_run(output, state, signature, planned, roof, by_id, contact_ids, nodes, triangles, foundation.verticals)
        else:
            jsonl(output/"ALL_CONFIGURATIONS_LEDGER.jsonl",all_bindings)
            write(output/"AREAL_OPERATOR_DISPOSITION.json",
                  {"bound_ids": [row["factor_id"] for row in area_rows], "omitted": area_omitted})
            state = {"schema": _RUN_SCHEMA, "input_signature": signature, "input_components": signature_inputs,
                     "selection_policy": _SELECTION_POLICY,
                     "jobs": [], "planned": len(planned), "selected": None,
                     "static_artifacts": _file_manifest(output, _STATIC_FILES)}
            write(state_path, state)
        return _run_jobs(output, state_path, state, planned, by_id, bound_by_id, foundation.verticals,
                         footprint, nodes, triangles, contact_ids, priors, policy, roof, stop_after,
                         require_all)


def _run_jobs(output, state_path, state, planned, by_id, bindings, verticals, footprint,
              nodes, triangles, contact_ids, priors, policy, roof, stop_after, require_all):
    started=time.monotonic()
    jobs = state["jobs"]
    processed_before = sum(item["wall_time_s"] for item in jobs)
    for identity in planned[len(jobs):]:
        if (processed_before+time.monotonic()-started >= policy["job_wall_time_s"]
                or (stop_after is not None and len(jobs) >= stop_after)):
            break
        config = by_id[identity["configuration_id"]]
        branch = identity["branch"]
        rows = bindings[(config.id, branch)]
        problem, result = _fit(nodes, triangles, contact_ids, config, rows,
                               priors[identity["completion"]], policy["solver"], branch)
        with TemporaryDirectory(prefix=".job-", dir=output) as work:
            stage = Path(work)
            job, selected = _stage_job(stage, problem, result, identity, config, rows, verticals,
                                       footprint, nodes, triangles,
                                       select=state["selected"] is None and branch == roof,
                                       bridges=policy.get("profile_frame_bridges", {}).get(config.id))
            _check_job(stage, job, identity, config, contact_ids, nodes, triangles, verticals)
            names = _job_files(job)
            if selected is not None:
                _check_selected(stage, {"jobs": [job], "selected": selected}, roof, by_id)
            _publish_stage(stage, output, names)
            if selected is not None:
                bundle = output / "selected"
                if bundle.exists():
                    # The previous process may have published the complete
                    # bundle immediately before committing its marker.
                    _check_selected(output, {"jobs": [job], "selected": selected}, roof, by_id)
                else:
                    (stage / "selected").rename(bundle)
                state["selected"] = selected
            jobs.append(job)
            write(state_path, state)
        print(identity["stem"], config.id, branch, job["status"], job["wall_time_s"], flush=True)
    accepted_count = sum(job["geometry_id"] is not None for job in jobs)
    return 0 if len(jobs) == len(planned) and (accepted_count == len(jobs) if require_all else accepted_count > 0) else 2


def _stage_job(stage, problem, result, identity, config, bound, verticals, footprint, nodes, triangles, *,
               select, bridges):
    stem = identity["stem"]
    compiled = {**applied_operators(problem, bound), "run_identity": identity}
    operator_path = stage / (stem + "-OPERATORS.json")
    write(operator_path, compiled)
    accepted = result.get("contacts") is not None and result["status"] == "MODEL_CLOSED_WITH_ASSUMPTIONS"
    geometry_id, selected = None, None
    if accepted:
        field = stage / (stem + ".npz")
        geometry_id = pack_field(field, nodes, triangles, result, identity, config.frame)
        queries = stage / (stem + "-QUERIES.jsonl")
        jsonl(queries, query_rows(problem, result, config, geometry_id, compiled, verticals))
        if select:
            bundle = stage / "selected"
            bundle.mkdir()
            shutil.copyfile(field, bundle / "SELECTED_FIELD.npz")
            shutil.copyfile(queries, bundle / "TARGET_QUERIES.jsonl")
            shutil.copyfile(operator_path, bundle / "APPLIED_OPERATORS.json")
            write(bundle / "PROFILES.json", profiles(problem, result, config, footprint, geometry_id, bridges))
            active = {row["factor_id"]: row for row in bound}
            jsonl(bundle / "ACTIVE_FACTORS.jsonl",
                  [active[row["factor_id"]] for row in compiled["factors"] if row["operator_ids"]])
            write(bundle / "SELECTED_IDENTITY.json", {**identity, "geometry_id": geometry_id})
            selected = {"identity": identity, "artifacts": _file_manifest(bundle, _SELECTED_FILES)}
    incumbent = result.get("incumbent_contacts")
    if isinstance(incumbent, np.ndarray):
        np.savez_compressed(stage / (stem + "-INCUMBENT.npz"), xy=nodes, triangles=triangles, contacts=incumbent)
    fit = {key: value for key, value in result.items() if key not in {"contacts", "incumbent_contacts"}}
    write(stage / (stem + "-FIT.json"), {**fit, "run_identity": identity})
    job = {**identity, "identity": identity, "geometry_id": geometry_id,
           "incumbent": isinstance(incumbent, np.ndarray),
           "status": result["status"], "wall_time_s": result["wall_time_s"],
           "factor_count": len(bound),
           "applied_factor_count": sum(bool(row["operator_ids"]) for row in compiled["factors"]),
           "iterations": [step["iterations"] for step in result.get("optimization", [])],
           "hard_violation_m": result.get("worst_hard_residual_m"),
           "minimum_thickness_m": result.get("minimum_nodal_thickness_m")}
    job["artifacts"] = _file_manifest(stage, _job_files(job))
    return job, selected


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("inputs", type=Path)
    parser.add_argument("carrier", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--pilot", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--restore-from", type=Path)
    parser.add_argument("--stop-after", type=int)
    parser.add_argument("--require-all", action="store_true")
    args=parser.parse_args()
    if args.stop_after is not None and args.stop_after < 0:
        parser.error("--stop-after cannot be negative")
    return execute(args.manifest,args.inputs,args.carrier,args.output,pilot=args.pilot,resume=args.resume,
                   stop_after=args.stop_after,require_all=args.require_all,restore_from=args.restore_from)


if __name__=="__main__":
    raise SystemExit(main())
