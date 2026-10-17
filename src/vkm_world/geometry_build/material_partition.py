"""Disjoint material/void/backfill states in a shared roof-relative frame.

This is a geometric scenario partition, not an excavation calendar, material
law, or FEM qualification. All solids share the same roof function; depths are
vertical distances below it. Missing extraction heights are never inferred.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

from shapely import union_all


@dataclass(frozen=True)
class MaterialInterval:
    name: str
    top_depth_m: float
    bottom_depth_m: float


@dataclass(frozen=True)
class PartitionSolid:
    name: str
    role: str
    footprint: object
    top_depth_m: float
    bottom_depth_m: float

    @property
    def volume_m3(self):
        return float(self.footprint.area*(self.bottom_depth_m-self.top_depth_m))


def audit_partition(domain, solids, total_depth_m, tolerance=1e-7):
    """Independently measure 3D overlap, missing volume and volume balance.

    Constant roof-relative depth intervals permit exact integration by slabs.
    The roof may vary in XY; its common displacement cancels from thickness.
    """
    if (not domain.is_valid or domain.is_empty or domain.geom_type not in ("Polygon", "MultiPolygon") or
            not math.isfinite(total_depth_m) or total_depth_m <= 0):
        raise ValueError("valid domain and positive finite total depth required")
    if not math.isfinite(tolerance) or tolerance <= 0:
        raise ValueError("positive finite tolerance required")
    for solid in solids:
        if (solid.role not in ("MATERIAL", "VOID", "BACKFILL") or
                solid.footprint.is_empty or not solid.footprint.is_valid or
                not all(math.isfinite(v) for v in (solid.top_depth_m, solid.bottom_depth_m)) or
                solid.bottom_depth_m <= solid.top_depth_m):
            raise ValueError("valid nonempty solid with positive finite depth interval required")
    cuts = sorted({0., total_depth_m, *(s.top_depth_m for s in solids), *(s.bottom_depth_m for s in solids)})
    missing = outside = overlap = 0.
    for low, high in zip(cuts, cuts[1:]):
        middle = (low+high)/2
        occupied = [s.footprint for s in solids if s.top_depth_m <= middle < s.bottom_depth_m]
        coverage = union_all(occupied)
        slab_height = high-low
        overlap += max(0., sum(p.area for p in occupied)-coverage.area)*slab_height
        if 0 <= middle < total_depth_m:
            missing += domain.difference(coverage).area*slab_height
            outside += coverage.difference(domain).area*slab_height
        else:
            outside += coverage.area*slab_height
    expected = float(domain.area*total_depth_m)
    volume_tolerance = tolerance*max(float(domain.length)*total_depth_m, float(domain.area), 1.)
    volumes = {role: sum(s.volume_m3 for s in solids if s.role == role) for role in ("MATERIAL", "VOID", "BACKFILL")}
    result = {"domain_area_m2": float(domain.area), "total_depth_m": total_depth_m,
              "expected_volume_m3": expected, "volume_by_role_m3": volumes,
              "volume_balance_error_m3": abs(sum(s.volume_m3 for s in solids)-expected),
              "overlap_volume_m3": float(overlap), "missing_volume_m3": float(missing),
              "outside_volume_m3": float(outside), "volume_tolerance_m3": volume_tolerance}
    result["valid_partition"] = max(result[key] for key in (
        "volume_balance_error_m3", "overlap_volume_m3", "missing_volume_m3", "outside_volume_m3")) <= volume_tolerance
    return result


def partition_material_state(domain, chamber_footprints, material_intervals, *, state,
                             scenario_id, backfill_fraction=None, tolerance=1e-7):
    """Remove the chamber volume from rock before adding void or backfill.

    Explicit states: ``before``, ``excavated``, ``backfilled``. The last needs a
    declared fill fraction; fill rises from the extraction floor. Intervals must
    tile the full excavation depth. An extra roof/floor interval must therefore
    be provided explicitly when extraction exceeds the geological seam height.
    """
    if state not in ("before", "excavated", "backfilled") or not scenario_id:
        raise ValueError("explicit scenario id and supported state required")
    if not domain.is_valid or domain.is_empty or domain.geom_type not in ("Polygon", "MultiPolygon"):
        raise ValueError("valid nonempty polygon domain required")
    layers = list(material_intervals)
    if not layers or layers[0].top_depth_m != 0:
        raise ValueError("material intervals must start at the shared roof")
    for index, layer in enumerate(layers):
        if (not layer.name or not all(math.isfinite(v) for v in (layer.top_depth_m, layer.bottom_depth_m)) or
                layer.bottom_depth_m <= layer.top_depth_m or
                (index and layer.top_depth_m != layers[index-1].bottom_depth_m)):
            raise ValueError("ordered contiguous positive material intervals required")
    if state == "backfilled":
        if backfill_fraction is None or not math.isfinite(backfill_fraction) or not 0 < backfill_fraction <= 1:
            raise ValueError("backfilled scenario requires explicit fraction in (0, 1]")
    elif backfill_fraction is not None:
        raise ValueError("fill fraction only belongs to a backfilled scenario")
    chambers = list(chamber_footprints)
    if not chambers or any(p.is_empty or not p.is_valid or p.geom_type not in ("Polygon", "MultiPolygon") for p in chambers):
        raise ValueError("valid nonempty chamber polygons required")
    excavated = union_all(chambers)
    area_tolerance = tolerance*max(float(domain.length), 1.)
    if sum(p.area for p in chambers)-excavated.area > area_tolerance:
        raise ValueError("overlapping chamber scenarios must be resolved explicitly")
    if excavated.difference(domain).area > area_tolerance:
        raise ValueError("chamber scenario leaves the declared host domain")
    excavated = excavated.intersection(domain)
    rock = domain if state == "before" else domain.difference(excavated)
    solids = [PartitionSolid(layer.name, "MATERIAL", rock, layer.top_depth_m, layer.bottom_depth_m)
              for layer in layers if not rock.is_empty]
    depth = layers[-1].bottom_depth_m
    if state == "excavated":
        solids.append(PartitionSolid("excavation_void", "VOID", excavated, 0., depth))
    elif state == "backfilled":
        fill_top = depth*(1-backfill_fraction)
        if fill_top > 0:
            solids.append(PartitionSolid("remaining_void", "VOID", excavated, 0., fill_top))
        solids.append(PartitionSolid("scenario_backfill", "BACKFILL", excavated, fill_top, depth))
    audit = audit_partition(domain, solids, depth, tolerance)
    if not audit["valid_partition"]:
        raise ValueError(f"invalid material partition: {audit}")
    return {"scenario_id": scenario_id, "state": state, "physical_date": None,
            "epistemic_status": "MODEL_CHOICE", "backfill_fraction": backfill_fraction,
            "backfill_placement": "from extraction floor upward" if state == "backfilled" else None,
            "solids": solids, "audit": audit}
