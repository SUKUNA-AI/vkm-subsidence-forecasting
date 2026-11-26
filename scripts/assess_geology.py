"""Object/trace-selected source and collar-anchor diagnostics through ContactPredictor.

Real inputs and outputs belong in PRIVATE/ignored work. No geological values
are embedded here. Comparisons are retrospective source-consistency checks,
not independent field validation or forecasts.
"""
from __future__ import annotations
import argparse
from pathlib import Path
import json
import hashlib
import sys
import time
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from vkm_world.geometry_build.contact_predictor import (ContactPredictor, PredictorSpec, cross_validate,
    observations_by_borehole, supports_from_borehole_rows)
from vkm_world.geometry_build.height_frames import declared_coordinate_frame
from vkm_world.geometry_build.pipeline import write_json
from shapely.geometry import shape
from vkm_world.geometry_build.column_display import TINSurface
from vkm_world.geometry_build.relative_constraints import withdraw_absolute_anchors


def hash_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def paired_scores(records, reference, wells, metric="level_error_m"):
    """Exact fold/group/well/contact keys; position fixed by the reference run."""
    keys = {}
    for row in records:
        if row["well"] not in wells:
            continue
        for c, value in enumerate(row.get(metric, [])):
            if value is not None and np.isfinite(value):
                key = (row["fold"], row["label"], row.get("physical_well", row["well"]), row["well"], c)
                if key in keys:
                    raise ValueError("duplicate comparison key")
                keys[key] = (float(value), row["position"])
    common = keys.keys() & reference.keys()
    result = {}
    for fold in ("leave_one_physical_borehole_out", "spatial_block"):
        for position in ("ALL", "INTERPOLATION", "EXTRAPOLATION"):
            selected = sorted(k for k in common if k[0] == fold and
                              (position == "ALL" or reference[k][1] == position))
            values = np.array([keys[k][0] for k in selected])
            result[f"{fold}/{position}"] = {"n_pairs": len(selected),
                "physical_wells": sorted(set(k[2] for k in selected)),
                "keys": [list(k) for k in selected],
                "bias_m": float(values.mean()) if len(values) else None,
                "mae_m": float(np.abs(values).mean()) if len(values) else None,
                "rmse_m": float(np.sqrt(np.mean(values ** 2))) if len(values) else None,
                "per_contact_mae_m": {str(c): float(np.mean([abs(keys[k][0]) for k in selected if k[-1] == c]))
                     for c in sorted(set(k[-1] for k in selected))}}
    return result, keys


def run(args):
    started = time.monotonic()
    out = args.output.resolve()
    if not out.is_relative_to((ROOT / "work").resolve()) or out.exists():
        raise ValueError("use a new output directory under ignored work/")
    source, cfg = read(args.contacts), read(args.config)
    frame = declared_coordinate_frame(read(args.frame), ("contacts", "placements", "surface", "boundary", "objects"))
    placement_rows = read(args.placements)["rows"]
    placements = {r["physical_id"]: r for r in placement_rows}
    if len(placements) != len(placement_rows):
        raise ValueError("duplicate placement identity")
    with np.load(args.surface, allow_pickle=False) as terrain:
        if terrain["xy"].shape != (len(terrain["z"]), 2):
            raise ValueError("surface requires matching XY and Z arrays")
        surface = TINSurface(np.column_stack([terrain["xy"], terrain["z"]]), terrain["triangles"])
    boundaries = [f for f in read(args.boundary)["features"] if f["properties"].get("object_id") == args.object_id]
    if len(boundaries) != 1:
        raise ValueError("select one uniquely identified boundary object")
    polygon = shape(boundaries[0]["geometry"])
    if polygon.geom_type not in ("Polygon", "MultiPolygon") or not polygon.is_valid or polygon.is_empty or polygon.area <= 0:
        raise ValueError("valid nonempty object boundary required")
    rows = [{**r, **placements[r["physical_id"]],
             "collar_scenario_m": placements[r["physical_id"]]["terrain_height_m"] + cfg["site_offset_m"]}
            for r in source["rows"] if r["physical_id"] in placements and
            placements[r["physical_id"]].get("x") is not None and
            placements[r["physical_id"]].get("terrain_height_m") is not None]
    names = source["ordered_contacts"]
    extra = observations_by_borehole(read(args.observations), names)
    absences = {key: [e for e in values if e["kind"] == "absent"] for key, values in extra.items()}
    partial = [r for r in rows if any(v is not None for v in r["depths_m"]) or r["physical_id"] in extra]
    full = [r for r in rows if all(v is not None for v in r["depths_m"])]
    common_wells = sorted(r["physical_id"] for r in full if r["physical_id"] != args.withdraw_collar)
    geo = read(args.objects)
    features = {f["properties"]["object_id"]: f for f in geo["features"]}
    if len(features) != len(geo["features"]):
        raise ValueError("duplicate object identity")
    if args.withdraw_collar not in {r["physical_id"] for r in partial}:
        raise ValueError("withdraw-collar must identify an included support")
    if args.trace_start == args.trace_end:
        raise ValueError("distinct trace endpoints required")
    if any(features[key]["geometry"]["type"] != "Point" for key in (args.trace_start, args.trace_end)):
        raise ValueError("trace endpoints must be Point features")
    ends = np.asarray([features[key]["geometry"]["coordinates"][:2] for key in (args.trace_start, args.trace_end)], float)
    if ends.shape != (2, 2) or not np.isfinite(ends).all() or np.linalg.norm(ends[1]-ends[0]) <= 0:
        raise ValueError("finite distinct trace endpoints required")
    query = np.linspace(ends[0], ends[1], args.query_points)
    variants = {}
    fields = {"xy": query, "contacts": np.asarray(names)}
    for label, extras in (("B", absences), ("C", extra)):
        parent = supports_from_borehole_rows(partial, names, extras, cfg)
        for anchor in ("baseline", "relative_only"):
            sid = f"{label}_{anchor}"
            supports, ledger = (parent, []) if anchor == "baseline" else withdraw_absolute_anchors(parent, [args.withdraw_collar])
            predictor = ContactPredictor(PredictorSpec(args.method, "SURFACE", cfg["site_offset_m"],
                extrapolate=True, allow_partial=True), surface=surface.sample, support=polygon)
            cv = cross_validate(lambda method, training: predictor, supports, [args.method], block_size_m=cfg["cv_block_size_m"])
            pred = predictor.predict(supports, query)
            fields[sid] = pred["levels"]
            variants[sid] = {"supports": len(supports.ids), "relative_ledger": ledger,
                "cv": cv, "trace_masks": pred["masks"].tolist(),
                "undetermined_rows": pred["undetermined_contact_rows"]}
            print(sid, "completed", flush=True)
    for metric in ("level_error_m", "thickness_error_m"):
        keyed = {sid: paired_scores(v["cv"]["records"], {}, common_wells, metric)[1] for sid, v in variants.items()}
        shared = set.intersection(*(set(v) for v in keyed.values()))
        reference = {key: keyed["B_baseline"][key] for key in shared}
        for sid, variant in variants.items():
            score, _ = paired_scores(variant["cv"]["records"], reference, common_wells, metric)
            variant["paired_source_scores" if metric == "level_error_m" else "paired_thickness_scores"] = score
    pairs = {}
    for label in ("B", "C"):
        delta = fields[f"{label}_relative_only"] - fields[f"{label}_baseline"]
        pairs[f"{label}_withdraw_collar"] = {"per_contact_mean_shift_m": delta.mean(0).tolist(),
            "per_contact_max_abs_shift_m": np.max(np.abs(delta), 0).tolist()}
    delta = fields["C_relative_only"] - fields["B_relative_only"]
    pairs["C_minus_B_relative_only"] = {"per_contact_mean_shift_m": delta.mean(0).tolist(),
        "per_contact_max_abs_shift_m": np.max(np.abs(delta), 0).tolist()}
    out.mkdir(parents=True)
    np.savez_compressed(out / "trace_fields.npz", **fields)
    report = {"schema": "vkm.geology_assessment/2", "object_id": args.object_id, "coordinate_declaration": frame, "requirements": ["SUR-003", "BH-005", "MOD-004", "VAL-003", "INT-003"],
        "purpose": "retrospective paired source-consistency diagnosis; not independent field validation",
        "operator": f"ContactPredictor SURFACE/{args.method}/extrapolate/partial",
        "withdraw_collar": args.withdraw_collar, "common_evaluation_wells": common_wells,
        "position_policy": "fixed by B_baseline per fold; own masks also retained",
        "trace": {"start_id": args.trace_start, "end_id": args.trace_end, "frame": frame["horizontal_frame"], "height_frame": frame["height_frame"],
            "length_m": float(np.linalg.norm(query[-1] - query[0])), "points": len(query),
            "geometry_role": "DERIVATION_FROM_SOURCE_PLAN; absolute datum UNKNOWN"},
        "paired_trace_differences": pairs, "variants": variants,
        "mechanical_QoI": {"status": "NOT_RUN", "reason": "admissibility and physical local history gates required"},
        "inputs": {key: {"sha256": hash_file(value), "filename": value.name,
                    "path": value.resolve().relative_to(ROOT).as_posix() if value.resolve().is_relative_to(ROOT) else value.name}
                   for key, value in vars(args).items() if isinstance(value, Path) and key != "output"},
        "code": {str(p.relative_to(ROOT)).replace('\\', '/'): hash_file(p) for p in [Path(__file__),
            ROOT / "src/vkm_world/geometry_build/height_frames.py", ROOT / "src/vkm_world/geometry_build/contact_predictor.py",
            ROOT / "src/vkm_world/geometry_build/relative_constraints.py", ROOT / "src/vkm_world/geometry_build/column_display.py",
            ROOT / "src/vkm_world/geometry_build/constrained_geology.py"]},
        "outputs": {"trace_fields.npz": hash_file(out / "trace_fields.npz")}, "seconds": time.monotonic() - started}
    write_json(out / "assessment.json", finite_json(report))
    print(json.dumps(pairs), flush=True)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def finite_json(value):
    if isinstance(value, dict):
        return {k: finite_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [finite_json(v) for v in value]
    if isinstance(value, (float, np.floating)) and not np.isfinite(value):
        return None
    return value


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("config", "contacts", "placements", "observations", "surface", "boundary", "objects", "frame", "output"):
        parser.add_argument("--" + key, type=Path, required=True)
    parser.add_argument("--object-id", required=True)
    parser.add_argument("--method", required=True, choices=["idw2"], help="explicit partial-observation predictor; alternative methods need a compatible experiment")
    parser.add_argument("--withdraw-collar", required=True)
    parser.add_argument("--trace-start", required=True)
    parser.add_argument("--trace-end", required=True)
    parser.add_argument("--query-points", type=int, default=121)
    args = parser.parse_args()
    if args.query_points < 2:
        parser.error("at least two query points required")
    run(args)
