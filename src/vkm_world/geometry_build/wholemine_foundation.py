"""Compile BHJ configurations for the whole-field geological reconstruction."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from numbers import Real
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


def bind_interval_semantics(contact_ids, document, *, frame_id, source_identity):
    """Validate explicit upper/lower body meanings against one field generation.

    No unit or material is inferred from either boundary name. Unknown lithology
    remains null; numerical buffers cannot masquerade as geological units.
    The returned rows follow the top-to-bottom contact order.
    """
    if (document.get("schema") != "vkm.interval-semantics/1"
            or document.get("frame_id") != frame_id
            or not isinstance(source_identity, str) or not source_identity.strip()
            or document.get("source_identity") != source_identity):
        raise ValueError("Interval semantics source/frame identity mismatch")
    if len(contact_ids) < 2 or len(set(contact_ids)) != len(contact_ids):
        raise ValueError("Unique ordered contact identities are required")
    expected = list(zip(contact_ids[:-1], contact_ids[1:]))
    rows = document.get("intervals")
    if not isinstance(rows, list):
        raise ValueError("Explicit interval semantics list is required")
    by_pair = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("Typed interval semantics are required")
        pair = (row.get("upper_contact_id"), row.get("lower_contact_id"))
        if pair not in expected or pair in by_pair:
            raise ValueError("Invalid or duplicate ordered boundary pair")
        refs = row.get("source_refs")
        if (not isinstance(refs, list) or not refs
                or not all(isinstance(ref, Mapping) and ref.get("source_id")
                           and (ref.get("locator") or ref.get("record_id")) for ref in refs)):
            raise ValueError("Each interval requires locatable source references")
        role = row.get("role")
        if role not in {"GEOLOGICAL_UNIT", "UNKNOWN_LITHOLOGY", "NUMERICAL_BUFFER"}:
            raise ValueError("Explicit geological or numerical role is required")
        if role == "GEOLOGICAL_UNIT" and not row.get("unit_id"):
            raise ValueError("A geological unit requires its explicit unit identity")
        if role != "GEOLOGICAL_UNIT" and (row.get("unit_id") is not None or row.get("material_id") is not None):
            raise ValueError("Unknown lithology or numerical buffer cannot acquire a unit/material")
        by_pair[pair] = dict(row)
    if set(by_pair) != set(expected):
        raise ValueError("Incomplete interval semantics coverage")
    return [by_pair[pair] for pair in expected]


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


def representative_reason(reason):
    """Translate the historical label without changing the input-only criterion."""
    return "WITHIN_BRANCH_MEDIAN_NEAREST" if reason == "WITHIN_BRANCH_MEDOID" else reason


def choose_design(foundation, additional=12):
    """Choose nearest observed configurations to branchwise component medians.

    Distance is squared L2 over target XY, scaled by the all-configuration range
    per target (at least one metre). Equal distances choose the lowest config ID.
    This criterion preserves the historical selected inputs; it is not a medoid.
    """
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
        branches.append((selected, "WITHIN_BRANCH_MEDIAN_NEAREST"))
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


_LOCAL_MAP_CONTROL = "CONDITIONAL_LOCAL_MAP_CONTROL"
_NO_CONTROL = "DO_NOT_USE_AS_SPATIAL_CONTROL"
_CONDITIONAL_POSITION_ROLES = {
    "IDENTIFIED_IN_DECLARED_FRAME", "UNDERIDENTIFIED", "PRIOR_DOMINATED",
}


def _same_xy(value, xy):
    return (isinstance(value, (list, tuple)) and len(value) == 2
            and all(isinstance(v, Real) and not isinstance(v, bool) for v in value)
            and list(value) == list(xy))


def _qualified_local_map(point):
    return (point.get("role") == "IDENTIFIED_IN_DECLARED_FRAME"
            and point.get("epistemic_status") == "DERIVATION"
            and bool(point.get("source_factor_ids")))


def _scenario_admission(admission, config, point, factor_id):
    if not (
        isinstance(admission, Mapping)
        and admission.get("physical_id") == point["object_id"]
        and admission.get("frame_id") == config.frame
        and _same_xy(admission.get("xy"), point["xy"])
        and isinstance(admission.get("reason"), str)
        and admission["reason"].strip()
        and isinstance(admission.get("source_refs"), (list, tuple))
        and admission["source_refs"]
        and all(
            (isinstance(ref, str) and ref.strip())
            or (isinstance(ref, Mapping) and ref.get("source_id")
                and (ref.get("locator") or ref.get("record_id")))
            for ref in admission["source_refs"]
        )
        and point.get("role") in _CONDITIONAL_POSITION_ROLES
    ):
        raise ValueError(f"Invalid scenario spatial admission for {config.id}/{factor_id}")
    return dict(admission)


def bind_factors(templates, config, interpretation, polygon, allowed_supports=(), *,
                 spatial_admissions=None):
    """Bind local-map or explicitly conditional XY, preserving independent vertical provenance.

    Spatial admissions are indexed by configuration ID and factor ID. External
    supports also require their own vertical admission in allowed_supports.
    """
    if spatial_admissions is not None and not isinstance(spatial_admissions, Mapping):
        raise ValueError("Scenario spatial admissions must be indexed by configuration")
    if not isinstance(allowed_supports, (list, tuple, set, frozenset)) or any(
        not isinstance(owner, str) or not owner.strip() for owner in allowed_supports
    ):
        raise ValueError("External vertical admissions require exact owner IDs")
    point_map = {row["object_id"]: row for row in config.positions}
    admissions = {} if spatial_admissions is None else spatial_admissions.get(config.id, {})
    if not isinstance(admissions, Mapping):
        raise ValueError(f"Invalid scenario spatial admissions for {config.id}")
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
        elif support["kind"] == "POINT" and (
            support.get("frame_id") != config.frame
            or support.get("configuration_id") not in (None, config.id)
            or not _same_xy(support.get("xy"), point["xy"])
        ):
            excluded[factor_id] = "POINT_SUPPORT_DOES_NOT_MATCH_BHJ_BINDING"
        else:
            mask = point.get("scientific_use_mask")
            if mask == _LOCAL_MAP_CONTROL and _qualified_local_map(point):
                role = "QUALIFIED_LOCAL_MAP_CONTROL"
                admission = None
            elif mask == _NO_CONTROL:
                if factor_id not in admissions:
                    excluded[factor_id] = "NO_CONDITIONAL_SPATIAL_ADMISSION"
                    continue
                role = "CONDITIONAL_SCENARIO_OPERATOR"
                admission = _scenario_admission(admissions[factor_id], config, point, factor_id)
            else:
                excluded[factor_id] = "UNQUALIFIED_BHJ_SPATIAL_CONTROL"
                continue
            provenance = {
                "application_role": role,
                "membership": point["membership"],
                "scientific_use_mask": mask,
                "position_role": point.get("role"),
                "position_epistemic_status": point.get("epistemic_status"),
                "position_reason": point.get("reason"),
                "source_factor_ids": point.get("source_factor_ids", []),
                "relation_ids": point.get("relation_ids", []),
                "vertical_template_support": dict(support),
                "scenario_admission": admission,
            }
            records.append({
                **row, "support": {**support, "kind": "POINT", "xy": point["xy"],
                                    "frame_id": config.frame, "configuration_id": config.id,
                                    "spatial_provenance": provenance},
            })
    return records, excluded
