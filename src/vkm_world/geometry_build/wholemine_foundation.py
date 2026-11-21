"""Compile BHJ configurations for the whole-field geological reconstruction."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
import json

import numpy as np
from shapely import covers, points, prepare, union_all
from shapely.geometry import shape

from .joint_borehole_handoff import read_foundation_bundle


@dataclass(frozen=True)
class SpatialConfiguration:
    id: str
    branch: str
    frame: str
    positions: tuple[dict, ...]
    profiles: tuple[dict, ...]
    transforms: dict

    def xy_by_id(self):
        return {row["object_id"]: row["xy"] for row in self.positions if row["xy"] is not None}


@dataclass(frozen=True)
class Foundation:
    targets: tuple[str, ...]
    supports: tuple[str, ...]
    configurations: tuple[SpatialConfiguration, ...]
    verticals: dict
    datum: object


def _read(path):
    return json.loads(Path(path).read_bytes())


def load_foundation(manifest):
    """Read the accepted bundle and keep every identity and its disposition."""
    manifest = Path(manifest)
    result = read_foundation_bundle(manifest, require_acceptance=True)
    inventory = _read(manifest)
    solution = _read(manifest.parent / inventory["files"]["solution"]["path"])
    vertical = _read(manifest.parent / inventory["files"]["vertical"]["path"])
    grouped = defaultdict(list)
    for row in result["positions"]:
        grouped[row["realization_id"]].append(row)
    configurations = []
    for row in solution["realizations"]:
        config_id = row["realization_id"]
        configurations.append(SpatialConfiguration(
            config_id, row["hypothesis_id"], row["coordinate_frame"],
            tuple(sorted(grouped[config_id], key=lambda position: position["object_id"])),
            tuple(row["profiles"]), row["transforms"]
        ))
    return Foundation(tuple(result["target_ids"]), tuple(result["support_ids"]),
                      tuple(configurations), vertical["records_by_object"], result["datum"])


def load_footprint(path):
    document = _read(path)
    geometries = ([shape(feature["geometry"]) for feature in document["features"]]
                  if document["type"] == "FeatureCollection" else
                  [shape(document["geometry"] if document["type"] == "Feature" else document)])
    if any(item.geom_type not in {"Polygon", "MultiPolygon"} or not item.is_valid
           or item.is_empty for item in geometries):
        raise ValueError("The declared field boundary includes invalid parts")
    polygon = union_all(geometries)
    if polygon.geom_type not in {"Polygon", "MultiPolygon"} or polygon.is_empty or not polygon.is_valid:
        raise ValueError("A valid explicitly versioned field boundary is required")
    return polygon


def target_coverage(foundation, polygon):
    prepare(polygon)
    rows = []
    for config in foundation.configurations:
        target_positions = [row for row in config.positions if row["membership"] == "TARGET"]
        spatial = [row for row in target_positions if row["xy"] is not None]
        inside = covers(polygon, points([row["xy"] for row in spatial]))
        rows.append({
            "configuration_id": config.id, "branch": config.branch, "frame": config.frame,
            "target_ids": list(foundation.targets), "support_ids": list(foundation.supports),
            "unlocated": [row["object_id"] for row in target_positions if row["xy"] is None],
            "outside": [row["object_id"] for row, ok in zip(spatial, inside, strict=True) if not ok],
            "count_inside": int(np.count_nonzero(inside)),
        })
    return rows


def choose_design(foundation, additional=12):
    """Choose input-only within-branch medoids plus physical-space extremes."""
    configs = sorted(foundation.configurations, key=lambda config: config.id)
    active = [key for key in foundation.targets if all(key in c.xy_by_id() for c in configs)]
    xy = np.asarray([[c.xy_by_id()[key] for key in active] for c in configs])
    indices = defaultdict(list)
    for index, config in enumerate(configs):
        indices[config.branch].append(index)
    # XY variation is compared in metres per target, with the same scale for both axes.
    # Floor of one metre prevents an invariant cartographic identity dominating via roundoff.
    scales = np.maximum(np.ptp(xy, axis=0).max(axis=1), 1.)
    features = (xy - np.median(xy, axis=0)) / scales[:, None]
    branches = []
    for branch, candidates in sorted(indices.items()):
        midpoint = np.median(features[candidates], axis=0)
        distances = ((features[candidates] - midpoint) ** 2).sum(axis=(1, 2))
        selected = candidates[int(np.argmin(distances))]
        branches.append((selected, "WITHIN_BRANCH_MEDOID"))
    remaining = set(range(len(configs))) - {index for index, _ in branches}
    for _ in range(min(additional, len(remaining))):
        distances = np.min([
            ((features - features[index]) ** 2).sum(axis=(1, 2))
            for index, _ in branches
        ], axis=0)
        selected = max(remaining, key=lambda index: (distances[index], configs[index].id))
        branches.append((selected, "FARTHEST_INPUT_CONFIGURATION"))
        remaining.remove(selected)
    return [(configs[index], reason) for index, reason in branches]


def bind_factors(templates, config, interpretation, polygon, allowed_supports=()):
    """Bind only approved horizontal supports without manufacturing XY for any factor."""
    point_map = {row["object_id"]: row for row in config.positions}
    records = []
    excluded = {}
    template_ids = set()
    for row in sorted(templates, key=lambda factor: factor["factor_id"]):
        factor_id = row["factor_id"]
        if factor_id in template_ids:
            raise ValueError(f"Duplicate vertical factor: {factor_id}")
        template_ids.add(factor_id)
        point = point_map.get(row["physical_id"])
        branch = row["interpretation_id"]
        support = row["support"]
        if row["admission"] not in {"HARD", "SOFT", "INTERVAL"}:
            excluded[factor_id] = "NON_NUMERICAL_DISPOSITION"
        elif branch not in {interpretation, "COMMON"}:
            excluded[factor_id] = "OTHER_INTERPRETATION_OR_NATIVE_ONLY"
        elif point is None:
            excluded[factor_id] = "OWNER_OUTSIDE_BHJ_ADJUSTMENT_NETWORK"
        elif point["membership"] == "SUPPORT" and point["object_id"] not in allowed_supports:
            excluded[factor_id] = "NO_SKRU1_VERTICAL_ADMISSION_FOR_EXTERNAL_SUPPORT"
        elif point["xy"] is None:
            excluded[factor_id] = "UNKNOWN_XY"
        elif point["frame"] != config.frame:
            excluded[factor_id] = "NO_REGISTERED_WORKING_FRAME"
        elif not covers(polygon, points(point["xy"])):
            excluded[factor_id] = "OUTSIDE_FIELD_DOMAIN"
        elif support["kind"] not in {"NONE", "POINT"}:
            excluded[factor_id] = "NEEDS_EXPLICIT_PROFILE_LINE_OR_AREA_OPERATOR"
        else:
            records.append({
                **row, "support": {**support, "kind": "POINT", "xy": point["xy"],
                                    "frame_id": config.frame, "configuration_id": config.id}
            })
    return records, excluded
