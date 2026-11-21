"""Run auditable wholemine reconstruction branches from an accepted BHJ input."""

from __future__ import annotations
import argparse
from collections import defaultdict
from hashlib import sha256
import json
import os
from pathlib import Path
import time

from . import geology_area_operators, geology_reconstruction, wholemine_foundation

import numpy as np
from shapely import from_wkt
from shapely.geometry import LineString, Point

from .geology_area_operators import AreaOperators
from .wholemine_foundation import bind_factors, load_footprint, load_foundation
from .geology_reconstruction import compile_model, evaluate_contacts, solve_model


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


def pack_field(out, nodes, tri, result, identity, frame):
    coefficients = result["contacts"]
    geometry_id = "C1-" + digest([identity, nodes.tolist(), tri.tolist(), coefficients.tolist()])[:24]
    np.savez_compressed(out, xy=nodes, triangles=tri, z=coefficients[::-1].T, contacts=coefficients,
                       horizon_names=np.asarray(result["contact_ids"][::-1]),
                       contact_ids=np.asarray(result["contact_ids"]), geometry_id=np.array(geometry_id),
                       bhj_id=np.array(result["configuration_id"]), frame=np.array(frame))
    return geometry_id


def _interp_rows(problem, result, positions):
    numeric = [row for row in positions if row["xy"] is not None and row["frame"] == problem.frame_id]
    values = evaluate_contacts(problem, result["contacts"], [row["xy"] for row in numeric])
    return {row["object_id"]: value for row, value in zip(numeric, values, strict=True) if np.isfinite(value).all()}


def query_rows(problem, result, config, geometry_id, admitted):
    numeric = _interp_rows(problem, result, config.positions)
    direct = defaultdict(list)
    for condition in admitted:
        direct[condition["physical_id"]].append(condition["factor_id"])
    rows = []
    for row in config.positions:
        key = row["object_id"]
        values = numeric.get(key)
        reason = "UNKNOWN_XY" if row["xy"] is None else "OUTSIDE_DOMAIN_OR_NATIVE_ONLY"
        support_role = ("BORING_RELATIVE_INPUT" if key in direct
                        else "CONDITIONAL_MODEL_CONTINUATION")
        rows.append({
            **row, "source_role": row["role"], "scenario_id": geometry_id, "bhj_id": config.id,
            "spatial_role": reason if values is None else support_role,
            "column_role": None if values is None else "MODEL_RESULT",
            "surface_reference": "LOCAL_SCENARIO_VERTICAL_GAUGE",
            "values_m": None if values is None else dict(zip(result["contact_ids"], values.tolist(), strict=True)),
            "own_factor_ids": direct.get(key, []),
            "no_numeric_query_reason": reason if values is None else None,
        })
    return rows


def profiles(problem, result, config, polygon, geometry_id):
    center = polygon.representative_point()
    xmin,ymin,xmax,ymax = polygon.bounds
    paths = [
        ("FIELD-EW", "Через западную и восточную части",
         LineString([(xmin,center.y),(xmax,center.y)]), ["SKRU1-DECLARED-C1-FOOTPRINT"]),
        ("FIELD-SN", "Через южную и северную части",
         LineString([(center.x,ymin),(center.x,ymax)]), ["SKRU1-DECLARED-C1-FOOTPRINT"]),
    ]
    for profile in config.profiles:
        if not profile["registration"]:
            continue
        points_by_id = config.xy_by_id()
        # Preserve the registered axis. Project scenario lateral marks to their stations.
        origin = np.asarray(profile["registration"]["origin"])
        angle = profile["registration"]["angle_rad"]
        direction = np.array([np.cos(angle),np.sin(angle)])
        along = [row["station"] for row in profile["stations"]]
        path = LineString([origin + min(along)*direction, origin + max(along)*direction])
        paths.append((profile["id"], "Профиль по привязке BHJ", path, [profile["id"],config.id]))
    entries=[]
    for key,label,path,refs in paths:
        stations=np.linspace(0,path.length,257)
        xy = [list(path.interpolate(value).coords[0]) for value in stations]
        result_z=evaluate_contacts(problem,result["contacts"],xy)
        entries.append({
            "id":key,"label":label,"xy":xy,"station_m":stations.tolist(),"source_refs":refs,
            "vertical_exaggeration":1.,"values_m":{
                contact:[float(value) if np.isfinite(value) else None for value in result_z[:,i]]
                for i,contact in enumerate(result["contact_ids"])
            },
        })
    return {"scenario_id":geometry_id,"frame":config.frame,"vertical_frame":"LOCAL_SCENARIO_VERTICAL_GAUGE",
            "profiles":entries}


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


def execute(manifest, inputs, field, output, *, pilot=False, resume=False,
            stop_after=None, require_all=False):
    foundation=load_foundation(manifest)
    footprint=load_footprint(field/"FOOTPRINT.geojson")
    index=read(field/"BHJ_INDEX.json")
    actual_manifest_sha = sha256(Path(manifest).read_bytes()).hexdigest()
    if actual_manifest_sha != index["bhj_manifest_sha256"]:
        raise ValueError("BHJ index belongs to a different accepted manifest")
    design=index["selected"]
    by_id={item.id:item for item in foundation.configurations}
    priors=read(inputs/"PRIORS.json")
    policy=read(inputs/"FIELD_POLICY.json")
    contact_ids=priors["contact_ids"]
    factors=[json.loads(line) for line in (inputs/"TYPED_VERTICAL_FACTORS.jsonl").read_text(encoding="utf-8").splitlines()]
    roof=policy["primary_interpretation"]
    floor=policy["alternative_interpretation"]
    branches={roof:policy.get("include_as_common",{}).get(roof,[]),
              floor:policy.get("include_as_common",{}).get(floor,[])}
    output.mkdir(parents=True, exist_ok=resume)
    with np.load(field/"CARRIER.npz",allow_pickle=False) as geometry:
        nodes,triangles=geometry["xy"],geometry["triangles"]
    area_rows, area_omitted = compile_area_factors(
        read(inputs/"CONDITIONAL_AREA_RECORDS.json"), nodes, triangles, "SKRU1_LOCAL_SH1"
    )
    jobs=[]
    all_bindings=[]
    bound_by_id={}
    for config in foundation.configurations:
        states={}
        for branch,native_joins in branches.items():
            selected=[registered_native_source(x, native_joins) for x in factors]
            rows, excluded=bind_factors(selected,config,branch,footprint,
                                       allowed_supports=policy.get("explicit_vertical_supports",[]))
            rows += [{**row, "support": {**row["support"],"configuration_id":config.id}} for row in area_rows]
            states[branch]={"active_factors":[row["factor_id"] for row in rows],"excluded":excluded}
            if config.id in {row["id"] for row in design}:
                bound_by_id[(config.id,branch)]=rows
        all_bindings.append({"bhj_id":config.id,"members":[p["object_id"] for p in config.positions],
                             "branch":config.branch,"vertical_stage":states})
    for row in design[:1] if pilot else design:
        jobs.append((row["id"],roof,"baseline",row["reason"]))
        if row["reason"]=="WITHIN_BRANCH_MEDOID" and not pilot:
            jobs.append((row["id"],floor,"baseline",row["reason"]))
    implementations = [str(Path(module.__file__)) for module in
                       (geology_area_operators, geology_reconstruction, wholemine_foundation)]
    code_hashes = [sha256(Path(path).read_bytes()).hexdigest() for path in [__file__, *implementations]]
    signature = digest([actual_manifest_sha, code_hashes, factors, area_rows, priors, policy,
                        nodes.tolist(), triangles.tolist(), jobs])
    state_path=output/"SCENARIO_INDEX.json"
    if resume:
        previous=read(state_path)
        if previous["input_signature"]!=signature:
            raise ValueError("Resumed input, policy or operation list changed")
        state=previous["jobs"]
        if previous["planned"] != len(jobs) or len(state) > len(jobs):
            raise ValueError("Invalid restart progress")
    else:
        state=[]
        jsonl(output/"ALL_CONFIGURATIONS_LEDGER.jsonl",all_bindings)
        write(output/"AREAL_OPERATOR_DISPOSITION.json",
              {"active_ids": [row["factor_id"] for row in area_rows], "omitted": area_omitted})
        write(state_path,{"input_signature":signature,"jobs":state,"planned":len(jobs)})
    started=time.monotonic()
    processed_before = sum(item["wall_time_s"] for item in state)
    for config_id,branch,completion,reason in jobs[len(state):]:
        if (processed_before+time.monotonic()-started >= policy["job_wall_time_s"]
                or (stop_after is not None and len(state) >= stop_after)):
            break
        config=by_id[config_id]
        rows=bound_by_id[(config.id,branch)]
        stem="FIELD-"+digest([config_id,branch,completion])[:16]
        base={"stem":stem,"configuration_id":config_id,"spatial_branch":config.branch,
              "branch":branch,"completion":completion,"design_reason":reason}
        problem,result=_fit(nodes,triangles,contact_ids,config,rows,priors[completion],policy["solver"],branch)
        accepted=result.get("contacts") is not None and result["status"]=="MODEL_CLOSED_WITH_ASSUMPTIONS"
        if accepted:
            geometry_id=pack_field(output/(stem+".npz"),nodes,triangles,result,base,config.frame)
            base["geometry_id"]=geometry_id
            all_queries=query_rows(problem,result,config,geometry_id,rows)
            jsonl(output/(stem+"-QUERIES.jsonl"),all_queries)
            if not (output/"SELECTED_FIELD.npz").exists() and branch==roof:
                field_bytes=(output/(stem+".npz")).read_bytes()
                (output/"SELECTED_FIELD.npz").write_bytes(field_bytes)
                write(output/"PROFILES.json",profiles(problem,result,config,footprint,geometry_id))
                jsonl(output/"TARGET_QUERIES.jsonl",all_queries)
                jsonl(output/"ACTIVE_FACTORS.jsonl",rows)
                write(output/"SELECTED_IDENTITY.json",base)
        if isinstance(result.get("incumbent_contacts"),np.ndarray):
            np.savez_compressed(output/(stem+"-INCUMBENT.npz"),xy=nodes,triangles=triangles,
                                contacts=result.pop("incumbent_contacts"))
        result.pop("contacts",None)
        write(output/(stem+"-FIT.json"),result)
        base|={"status":result["status"],"factor_count":len(rows),
               "iterations":[step["iterations"] for step in result.get("optimization",[])],
               "wall_time_s":result["wall_time_s"],
               "hard_violation_m":result.get("worst_hard_residual_m"),
               "minimum_thickness_m":result.get("minimum_nodal_thickness_m")}
        state.append(base)
        write(state_path,{"input_signature":signature,"jobs":state,"planned":len(jobs)})
        print(stem,config_id,branch,base["status"],base["wall_time_s"],flush=True)
    accepted_count = sum(bool(x.get("geometry_id")) for x in state)
    return 0 if len(state)==len(jobs) and (accepted_count == len(jobs) if require_all else accepted_count > 0) else 2


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("inputs", type=Path)
    parser.add_argument("carrier", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--pilot", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--stop-after", type=int)
    parser.add_argument("--require-all", action="store_true")
    args=parser.parse_args()
    if args.stop_after is not None and args.stop_after < 0:
        parser.error("--stop-after cannot be negative")
    return execute(args.manifest,args.inputs,args.carrier,args.output,pilot=args.pilot,resume=args.resume,
                   stop_after=args.stop_after,require_all=args.require_all)


if __name__=="__main__":
    raise SystemExit(main())
