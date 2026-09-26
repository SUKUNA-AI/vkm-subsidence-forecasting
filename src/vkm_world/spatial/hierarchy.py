"""Spatial hierarchy of the world: ВКМ → district → mine → … → benchmark.

A node's parent must be coarser. The hierarchy is not a strict tree of fixed depth: e.g. a borehole
may hang directly under a mine field or under the deposit (exploration boreholes predate mines), a
survey line may cross panels. ``ALLOWED_PARENTS`` encodes the admissible parent levels.
"""
from __future__ import annotations

from pydantic import Field

from ..core.base import WorldObject, duplicate_ids
from ..core.provenance import SpatialLevel as L

ALLOWED_PARENTS: dict[L, frozenset[L]] = {
    L.DEPOSIT: frozenset(),
    L.DISTRICT: frozenset({L.DEPOSIT}),
    L.DEPOSIT_PART: frozenset({L.DEPOSIT}),
    L.EXPLORATION_AREA: frozenset({L.DEPOSIT, L.DEPOSIT_PART, L.DISTRICT}),
    L.MINE: frozenset({L.DISTRICT, L.DEPOSIT}),
    L.NEIGHBOUR_MINE: frozenset({L.DISTRICT, L.DEPOSIT}),
    L.INTERMINE_PILLAR: frozenset({L.DISTRICT, L.DEPOSIT}),
    L.MINE_FIELD: frozenset({L.MINE, L.NEIGHBOUR_MINE}),
    L.SHAFT: frozenset({L.MINE, L.NEIGHBOUR_MINE, L.MINE_FIELD}),
    L.MINE_HORIZON: frozenset({L.MINE, L.NEIGHBOUR_MINE, L.MINE_FIELD}),
    L.PANEL: frozenset({L.MINE_FIELD, L.MINE, L.NEIGHBOUR_MINE}),
    # block-type preparation (SKRU-1 blocks 2–201, GIS blocks "П") exists outside panels (evidence: MINING synthesis)
    L.BLOCK: frozenset({L.PANEL, L.MINE_FIELD, L.MINE, L.NEIGHBOUR_MINE}),
    L.SITE: frozenset({L.PANEL, L.BLOCK, L.MINE_FIELD, L.MINE, L.NEIGHBOUR_MINE, L.INTERMINE_PILLAR,
                       L.DEPOSIT, L.DISTRICT, L.EXPLORATION_AREA}),
    L.WORKING: frozenset({L.MINE, L.NEIGHBOUR_MINE, L.MINE_FIELD, L.PANEL, L.BLOCK, L.SITE, L.MINE_HORIZON, L.SHAFT}),
    L.CHAMBER: frozenset({L.BLOCK, L.PANEL, L.SITE}),
    # shaft/borehole/river/strip protective pillars are mine-field level objects (evidence: MINING synthesis)
    L.PILLAR: frozenset({L.BLOCK, L.PANEL, L.SITE, L.INTERMINE_PILLAR, L.MINE_FIELD, L.MINE, L.NEIGHBOUR_MINE}),
    L.SEAM: frozenset({L.DEPOSIT, L.DISTRICT, L.MINE, L.MINE_FIELD}),
    L.INTERSEAM: frozenset({L.DEPOSIT, L.DISTRICT, L.MINE, L.MINE_FIELD}),
    L.GEOLOGICAL_ELEMENT: frozenset({L.DEPOSIT, L.DISTRICT, L.MINE, L.MINE_FIELD, L.PANEL, L.INTERMINE_PILLAR,
                                     L.DEPOSIT_PART, L.EXPLORATION_AREA, L.NEIGHBOUR_MINE}),
    L.BOREHOLE: frozenset({L.DEPOSIT, L.DISTRICT, L.MINE, L.NEIGHBOUR_MINE, L.MINE_FIELD, L.PANEL, L.BLOCK,
                           L.SITE, L.INTERMINE_PILLAR, L.WORKING, L.EXPLORATION_AREA, L.DEPOSIT_PART}),
    L.BOREHOLE_INTERVAL: frozenset({L.BOREHOLE}),
    # published GPR profiles often do not name the mine → deposit-level container allowed, scope stays UNSTATED
    L.GPR_PROFILE: frozenset({L.WORKING, L.CHAMBER, L.PILLAR, L.PANEL, L.BLOCK, L.SITE, L.SHAFT, L.MINE,
                              L.NEIGHBOUR_MINE, L.DEPOSIT}),
    L.SURVEY_LINE: frozenset({L.MINE, L.NEIGHBOUR_MINE, L.MINE_FIELD, L.PANEL, L.BLOCK, L.SITE, L.DISTRICT,
                              L.INTERMINE_PILLAR, L.DEPOSIT}),
    L.BENCHMARK: frozenset({L.SURVEY_LINE, L.SITE, L.MINE, L.DISTRICT}),
    L.SPECIMEN: frozenset({L.BOREHOLE_INTERVAL, L.BOREHOLE, L.WORKING, L.CHAMBER, L.PILLAR, L.SEAM, L.MINE,
                           L.NEIGHBOUR_MINE, L.SITE}),
    L.POINT: frozenset(set(L) - {L.POINT, L.UNSTATED}),
    L.UNSTATED: frozenset(set(L) - {L.UNSTATED}),
}


class SpatialNode(WorldObject):
    level: L
    parent_id: str | None = None
    also_within: tuple[str, ...] = Field((), description="other containers (e.g. a survey line crossing panels)")
    geometry_ref: str | None = Field(None, description="id of a geometry object (outline/trace/point) if any")
    site_scope: str | None = Field(None, description="Scope enum value; SKRU1 only if truly site-specific")


def hierarchy_errors(nodes: list[SpatialNode]) -> list[str]:
    errs = [f"duplicate spatial node id {d}" for d in duplicate_ids(nodes)]
    by = {n.id: n for n in nodes}
    for n in nodes:
        if n.parent_id is None:
            if n.level not in (L.DEPOSIT, L.UNSTATED):
                errs.append(f"{n.id}: level {n.level.value} without parent (only the deposit is a root)")
            continue
        p = by.get(n.parent_id)
        if p is None:
            errs.append(f"{n.id}: parent '{n.parent_id}' does not exist")
            continue
        if p.level not in ALLOWED_PARENTS[n.level]:
            errs.append(f"{n.id}: parent level {p.level.value} not allowed for {n.level.value}")
        for other in n.also_within:
            if other not in by:
                errs.append(f"{n.id}: also_within '{other}' does not exist")
    # cycles
    for n in nodes:
        seen, cur = set(), n
        while cur.parent_id is not None and cur.parent_id in by:
            if cur.id in seen:
                errs.append(f"cycle in spatial hierarchy at {n.id}")
                break
            seen.add(cur.id)
            cur = by[cur.parent_id]
    return errs


def ancestors(nodes: list[SpatialNode], node_id: str) -> list[str]:
    by = {n.id: n for n in nodes}
    out, cur = [], by.get(node_id)
    while cur is not None and cur.parent_id is not None and cur.parent_id not in out:
        out.append(cur.parent_id)
        cur = by.get(cur.parent_id)
    return out
