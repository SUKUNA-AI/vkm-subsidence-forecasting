"""Execute the borehole spatial adjustment using a pinned, data-only job."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import time
from collections import Counter
from pathlib import Path

from . import joint_borehole as adjustment
from . import joint_constraints as constraints


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _process_memory():
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("page_faults", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in (
                    "peak_working", "working", "peak_paged", "paged", "peak_nonpaged",
                    "nonpaged", "pagefile", "peak_pagefile", "private",
                )
            ]

        handle = ctypes.windll.kernel32.GetCurrentProcess
        handle.restype = wintypes.HANDLE
        counters = Counters()
        counters.size = ctypes.sizeof(counters)
        read_memory = ctypes.windll.psapi.GetProcessMemoryInfo
        read_memory.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        read_memory.restype = wintypes.BOOL
        success = read_memory(handle(), ctypes.byref(counters), counters.size)
        if success:
            return {"rss_bytes": counters.working, "peak_rss_bytes": counters.peak_working}
    else:
        import resource
        import sys

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return {"rss_bytes": None, "peak_rss_bytes": peak * (1 if sys.platform == "darwin" else 1024)}
    return {"rss_bytes": None, "peak_rss_bytes": None}


def _map_residuals(problem, planes, transforms, delta):
    result = []
    for map_ in problem.maps:
        for mark in map_.marks:
            xy = planes.get(map_.output_frame, {}).get(mark.object_id)
            if xy is None:
                continue
            u, v = adjustment.plane_to_native(xy, transforms[map_.id])
            native = [mark.xy[0] - u, mark.xy[1] - v]
            norm = math.hypot(*native) / mark.scale
            result.append({"record_id": mark.record_id, "group": mark.group, "object_id": mark.object_id,
                           "map_id": map_.id, "native_delta": native, "norm_scaled": norm,
                           "weight": 1. if norm <= delta else delta / norm})
    for frame, mark in problem.anchors:
        xy = planes.get(frame, {}).get(mark.object_id)
        if xy is None:
            continue
        native = [mark.xy[0] - xy[0], mark.xy[1] - xy[1]]
        norm = math.hypot(*native) / mark.scale
        result.append({"record_id": mark.record_id, "group": mark.group, "object_id": mark.object_id,
                       "frame": frame, "map_id": None, "native_delta": native, "norm_scaled": norm,
                       "weight": 1. if norm <= delta else delta / norm})
    return result


def _profile_states(profiles, planes):
    output = []
    for profile in profiles:
        ids = profile["ids"]
        frame = profile["frame"]
        points = planes.get(frame, {})
        record = {"id": profile["id"], "frame": frame, "ids": ids,
                  "registration": None, "stations": None}
        anchors = profile.get("anchor_ids")
        if anchors is None or len(anchors) != 2 or not set(anchors).issubset(ids):
            record["reason"] = "PROFILE_AXIS_ANCHORS_NOT_DECLARED"
        elif all(points.get(key) is not None for key in ids) and points[anchors[0]] != points[anchors[1]]:
            origin = points[anchors[0]]
            end = points[anchors[1]]
            angle = math.atan2(end[1] - origin[1], end[0] - origin[0])
            cosine, sine = math.cos(angle), math.sin(angle)
            record.update({
                "registration": {"origin": origin, "angle_rad": angle, "anchor_ids": anchors,
                                 "role": "CONDITIONAL_PROFILE_AXIS_FROM_JOINT_POSITIONS",
                                 "source_metric_scale": profile.get("source_metric_scale")},
                "stations": [
                    {"object_id": key,
                     "station": (points[key][0] - origin[0]) * cosine + (points[key][1] - origin[1]) * sine,
                     "cross_track": -(points[key][0] - origin[0]) * sine + (points[key][1] - origin[1]) * cosine}
                    for key in ids
                ],
            })
        else:
            record["reason"] = profile.get("reason", "PROFILE_POSITION_OR_PLANE_UNRESOLVED")
        output.append(record)
    return output


def _configuration(problem, payload, planes, transforms, fit, relation_checks, choices,
                   method, input_hashes, working_frame, admitted_maps, search_status):
    frame_positions = []
    native_factors = {}
    point_rows = {}
    for frame, points in sorted(planes.items()):
        for key, values in sorted(points.items()):
            if values is not None:
                xy = [float(value) for value in values]
                view = {"object_id": key, "frame": frame, "xy": xy}
                frame_positions.append(view)
                point_rows.setdefault(key, {})[frame] = xy
    for map_ in problem.maps:
        for mark in map_.marks:
            native_factors.setdefault((map_.output_frame, mark.object_id), []).append(mark.record_id)
    for frame, mark in problem.anchors:
        native_factors.setdefault((frame, mark.object_id), []).append(mark.record_id)
    active_ids = sorted(row["id"] for row in relation_checks)
    relation_ids = {}
    for row in relation_checks:
        for key in row["ids"]:
            relation_ids.setdefault((row["frame"], key), []).append(row["id"])
    profiles = _profile_states(payload.get("profiles", []), planes)
    config_digest = adjustment.digest([input_hashes["problem"], choices, frame_positions, transforms, active_ids])
    positions = []
    for key in problem.target_ids + problem.support_ids:
        available = point_rows.get(key, {})
        if working_frame in available:
            frame = working_frame
        elif len(available) == 1:
            frame = next(iter(available))
        else:
            frame = None
        xy = available.get(frame)
        factors = sorted(native_factors.get((frame, key), ()))
        relative = sorted(relation_ids.get((frame, key), ()))
        known = fit.get(frame, {}).get("identified", {}).get(key, False)
        if xy is None:
            role, epistemic, reason = "UNDERIDENTIFIED", "UNKNOWN", "NO_UNAMBIGUOUS_COMPUTABLE_PLANE_POSITION"
        elif frame != working_frame:
            role, epistemic, reason = "LOCAL_FRAME_ONLY", "DERIVATION", "NO_QUALIFIED_WORKING_FRAME_BRIDGE"
        elif not factors:
            role, epistemic, reason = "PRIOR_DOMINATED", "MODEL_CHOICE", "CONDITIONAL_GEOMETRIC_MODE_ONLY"
        elif not known:
            role, epistemic, reason = "UNDERIDENTIFIED", "MODEL_CHOICE", "DATA_JACOBIAN_HAS_NULL_DIRECTIONS"
        else:
            role, epistemic, reason = "IDENTIFIED_IN_DECLARED_FRAME", "DERIVATION", None

        used_maps = {map_.id for map_ in problem.maps if map_.output_frame == frame
                     and any(mark.object_id == key for mark in map_.marks)}
        qualified = (role == "IDENTIFIED_IN_DECLARED_FRAME"
                     and bool(used_maps & admitted_maps or any(
                         mark.object_id == key and anchor_frame == frame
                         for anchor_frame, mark in problem.anchors)))
        vertical = payload.get("vertical_links", {}).get(key)
        positions.append({
            "object_id": key, "membership": "TARGET" if key in problem.target_ids else "SUPPORT",
            "xy": xy, "frame": frame, "role": role, "epistemic_status": epistemic,
            "reason": reason, "source_factor_ids": factors, "relation_ids": relative,
            "vertical_links": vertical if vertical is not None else [],
            "scientific_use_mask": "CONDITIONAL_LOCAL_MAP_CONTROL" if qualified else
            "DO_NOT_USE_AS_SPATIAL_CONTROL",
        })
    return {
        "realization_id": "BHJ-" + config_digest[:20], "problem_id": payload["problem_id"],
        "hypothesis_id": adjustment.digest(choices)[:16], "choices": choices, "parent_ids": [],
        "target_inventory_sha256": adjustment.digest(list(problem.target_ids)),
        "coordinate_frame": working_frame, "gauge_id": adjustment.digest(payload.get("gauge_policy", {})),
        "positions": positions, "frame_positions": frame_positions, "transforms": transforms,
        "profiles": profiles, "active_relation_ids": active_ids, "relation_checks": relation_checks,
        "verification": "PRODUCER_FEASIBILITY_READBACK_ONLY", "search_status": search_status,
        "generation_method": method, "probability_mode": "SET_MEMBERSHIP_ONLY",
    }


def _relation_sources(payload):
    rows = [row for key in ("relations", "domains") for row in payload.get(key, ())
            if row.get("active_in_joint", True)]
    known = set(payload["target_ids"]) | set(payload["support_ids"])
    maps = {row["id"]: row for row in payload.get("maps", ())}
    frames = {row["output_frame"] for row in maps.values()} | {row["frame"] for row in rows}
    result = {}
    for row in rows:
        if not set(row["ids"]) <= known:
            raise ValueError("Relation references an undeclared object")
        source_map = row.get("map_id")
        if source_map is not None and (
            source_map not in maps or maps[source_map]["output_frame"] != row["frame"]
        ):
            raise ValueError("Relation uses a map outside its working frame")
    for frame in sorted(frames):
        result[frame] = constraints.compile_relations([row for row in rows if row["frame"] == frame])
    return result


def run(payload, policy, output, *, resume=False, stop_after=None):
    """Produce evaluated configurations and preserve accepted incumbents across bounded restarts."""
    output = Path(output)
    problem = adjustment.compile_problem(payload)
    numerical = policy["numerical"]
    scenario = policy["scenario"]
    budget = policy["budget"]
    computed_relations = _relation_sources(payload)
    max_count = budget["max_realizations"]
    design_count = min(budget["max_starts"], int(scenario.get("pose_design_count", max_count)))
    working_frame = payload.get("working_frame", "SKRU1_LOCAL_SH1")
    admitted_maps = set(payload.get("spatial_control_map_ids", ()))
    residual_cap = float(scenario.get("max_native_residual_normalized", 5.0))
    loss_cutoff = float(scenario.get("huber_delta", 2.5))
    hashes = {
        "problem": problem.input_sha256, "policy": adjustment.digest(policy),
        "solver": _sha(adjustment.__file__), "constraints": _sha(constraints.__file__),
        "job": _sha(__file__), "gauge": adjustment.digest(payload.get("gauge_policy", {})),
    }
    prior = _read(output) if resume else None
    if output.exists() and not resume:
        raise FileExistsError("Use a new output or an explicit verified resume")
    if prior is not None and prior["input_hashes"] != hashes:
        raise ValueError("Resume rejected: input, code, policy, or gauge mismatch")

    if len([frame for frame, rows in computed_relations.items() if rows]) > 1:
        raise ValueError("Cross-plane relation exploration needs an explicit plane-by-plane hypothesis product")

    start = time.monotonic()
    memory_initial = _process_memory()
    numerical_options = {"max_evaluations": budget["max_function_evaluations"],
                         "rank_tolerance": numerical["rank_rtol"]}
    if prior:
        fitted, frozen = prior["fit"]["adjusted"], prior["fit"]["frozen"]
        configs, attempts = prior["realizations"], prior["attempts"]
    else:
        frozen = adjustment.solve_maps(problem, frozen=True, **numerical_options)
        fitted = adjustment.solve_maps(problem, **numerical_options)
        configs, attempts = [], []

    result = {
        "schema": adjustment.VERSION, "problem_id": payload["problem_id"], "input_hashes": hashes,
        "target_ids": list(problem.target_ids), "support_ids": list(problem.support_ids),
        "fit": {"adjusted": fitted, "frozen": frozen}, "realizations": configs, "attempts": attempts,
        "disposition_count": len(payload.get("dispositions", [])), "gauge_policy": payload.get("gauge_policy", {}),
    }
    accepted = {row["realization_id"] for row in configs}
    candidates = list(adjustment.registration_design(problem, fitted, design_count, scenario["seed"]))
    total_elapsed_before = prior["resources"]["total_wall_sec"] if prior else 0.
    iteration_prior = len(attempts)
    target_checkpoints = budget["checkpoint_period"]

    def store(status):
        memory = _process_memory()
        initial_rss = memory_initial["rss_bytes"]
        peak = memory["peak_rss_bytes"]
        result.update({
            "status": status, "requested_count": max_count, "accepted_count": len(configs),
            "distinct_joint_count": len(accepted), "candidate_pose_design_count": len(candidates),
            "unattempted_pose_proposals": [label for label, _ in candidates[len(attempts):]],
            "resources": {
                "device": "CPU", "precision": "float64", "cpu_threads": budget["cpu_threads"],
                "total_wall_sec": total_elapsed_before + time.monotonic() - start,
                "baseline_rss_bytes": initial_rss, **memory,
                "peak_rss_above_baseline_bytes": max(0, peak - initial_rss)
                if peak is not None and initial_rss is not None else None,
            },
        })
        adjustment.write_json(output, result)

    for index, (label, poses) in enumerate(candidates[iteration_prior:], start=iteration_prior):
        elapsed = total_elapsed_before + time.monotonic() - start
        if elapsed >= budget["wall_time_sec"] or (stop_after is not None and index >= stop_after):
            store("INCOMPLETE_RESOURCE_LIMIT")
            return result
        if len(configs) >= max_count:
            break
        if memory_initial["rss_bytes"] is not None:
            if _process_memory()["rss_bytes"] >= budget["memory_limit_bytes"]:
                store("INCOMPLETE_RESOURCE_LIMIT")
                return result
        pose_fraction = 1.
        fit = fitted
        if label != "FIT":
            center = {key: values for value in fitted.values() for key, values in value["transforms"].items()}
            for fraction in (1., .5, .25, .125, .0625):
                intermediate = {
                    key: [base + fraction * (trial - base)
                          for base, trial in zip(center[key], proposal, strict=True)]
                    for key, proposal in poses.items()
                }
                fit = adjustment.solve_maps(problem, poses=intermediate, diagnostic=False,
                                            **numerical_options)
                pose_fraction = fraction
                if all(row["norm_scaled"] <= residual_cap for value in fit.values()
                       for row in value["residuals"]):
                    break
        if any(value.get("solver") and not value["solver"]["success"] for value in fit.values()):
            attempts.append({"pose": label, "candidate_count": 0,
                             "rejections": {"MAP_OPTIMIZER_STOPPED": 1}})
            continue
        positions = {frame: value["positions"] for frame, value in fit.items()}
        transforms = {key: list(pose) for value in fit.values() for key, pose in value["transforms"].items()}
        options = [{
            "choices": {}, "positions": {}, "verification": [], "candidate_method": "MAP_ADJUSTMENT",
        }]
        mode_report = None
        for frame, relations in computed_relations.items():
            if not relations:
                continue
            options_for_pose = (scenario["mode_design_count_at_fit"] if label == "FIT"
                                else scenario["mode_design_count_at_perturbation"])
            mode_report = constraints.generate_mode_candidates(
                relations, positions.get(frame, {}), transforms, seed=scenario["seed"] + index,
                fixed=positions.get(frame, {}),
                max_hypotheses=budget["max_hypotheses"],
                max_starts=max(2, budget["max_starts"] // max(1, design_count)),
                max_realizations=options_for_pose,
                time_limit=min(budget["wall_time_sec"] - elapsed, 20.),
                max_function_evaluations=budget["max_constraint_evaluations"] // max(1, design_count),
            )
            options = mode_report["realizations"]
        rejections = Counter()
        accepted_here = 0
        for option in options:
            if len(configs) >= max_count:
                rejections["OUTPUT_LIMIT"] += 1
                break
            proposed = {frame: dict(points) for frame, points in positions.items()}
            checks = []
            for frame, relations in computed_relations.items():
                if not relations:
                    continue
                proposed.setdefault(frame, {}).update(option["positions"])
                selected = constraints.select_relations(relations, option["choices"])
                checks += constraints.relation_residuals(selected, proposed[frame], transforms)
            if any(row["hard"] and row["status"] != "SATISFIED" for row in checks):
                rejections["HARD_CONSTRAINT_RECHECK_FAILED"] += 1
                continue
            residuals = _map_residuals(problem, proposed, transforms, loss_cutoff)
            if any(row["norm_scaled"] > residual_cap for row in residuals):
                rejections["ENGINEERING_NATIVE_RESIDUAL_LIMIT"] += 1
                continue
            method = {"pose": label, "constraints": option["candidate_method"]}
            config = _configuration(problem, payload, proposed, transforms, fitted, checks,
                                    option["choices"], method, hashes, working_frame, admitted_maps,
                                    "VERIFIED_WITNESS_IN_DECLARED_DOMAIN")
            if config["realization_id"] in accepted:
                rejections["DUPLICATE_JOINT_STATE"] += 1
                continue
            config["native_residuals"] = residuals
            configs.append(config)
            accepted.add(config["realization_id"])
            accepted_here += 1
        attempts.append({
            "pose": label, "pose_fraction": pose_fraction, "candidate_count": len(options), "accepted": accepted_here,
            "rejections": dict(rejections),
            "mode_search": None if mode_report is None else {
                key: value for key, value in mode_report.items() if key not in {"realizations", "partial_incumbents"}
            },
        })
        if len(attempts) % target_checkpoints == 0:
            store("INCOMPLETE_CHECKPOINT")

    status = "COMPLETED_DECLARED_DESIGN" if len(candidates) == len(attempts) and configs else (
        "INCOMPLETE_REALIZATION_BUDGET" if configs else "UNRESOLVED_CONTINUOUS_SEARCH"
    )
    store(status)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("policy", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--stop-after", type=int)
    args = parser.parse_args()
    output = run(_read(args.input), _read(args.policy), args.output,
                 resume=args.resume, stop_after=args.stop_after)
    print(json.dumps({key: output[key] for key in ("status", "accepted_count", "distinct_joint_count", "resources")}))
    return 0 if output["status"] == "COMPLETED_DECLARED_DESIGN" else 2


if __name__ == "__main__":
    raise SystemExit(main())
