"""Spec → file model. Cell order: root ``0``, default layer ``1``, nodes (parents before children, otherwise spec
order), edges (spec order). Unplaced nodes of a ``grid`` or ``drawio:*`` page get grid positions — the draw.io CLI
then re-lays ``drawio:*`` pages (:mod:`vkm_drawio.drawio_cli`)."""
from __future__ import annotations

import math

from vkm_drawio.model import DiagramSpec, EdgeSpec, NodeSpec, PageSpec
from vkm_drawio.styles import is_html, merge_style, text_to_value
from vkm_drawio.xmlio import MODEL_DEFAULTS, Cell, Diagram, Geometry, Page

GRID_ORIGIN = (40.0, 40.0)
GRID_GAP = 60.0
CONTAINER_PAD = (20.0, 40.0)            # left, top padding for children placed inside a container
CONTAINER_GROW_PAD = 20.0


def node_style(node: NodeSpec) -> str:
    return merge_style(node.base_style(), node.style)


def edge_style(edge: EdgeSpec) -> str:
    return merge_style(edge.base_style(), edge.style)


def _topological(nodes: list[NodeSpec]) -> list[NodeSpec]:
    """Parents before children; otherwise the spec order (stable)."""
    placed: set[str] = set()
    remaining = list(nodes)
    ordered: list[NodeSpec] = []
    while remaining:
        progressed = False
        rest: list[NodeSpec] = []
        for n in remaining:
            if n.parent is None or n.parent in placed:
                ordered.append(n)
                placed.add(n.id)
                progressed = True
            else:
                rest.append(n)
        if not progressed:                       # cycles are rejected by the spec validator; guard anyway
            raise ValueError("container cycle")
        remaining = rest
    return ordered


def grid_positions(page: PageSpec) -> dict[str, tuple[float, float]]:
    """Deterministic positions for nodes without coordinates, per parent: a near-square grid below explicitly placed
    siblings. Not a graph layout: edges are ignored."""
    out: dict[str, tuple[float, float]] = {}
    by_parent: dict[str | None, list[NodeSpec]] = {}
    for n in page.nodes:
        by_parent.setdefault(n.parent, []).append(n)
    for parent, siblings in by_parent.items():
        unplaced = [n for n in siblings if n.x is None or n.y is None]
        if not unplaced:
            continue
        origin_x, origin_y = CONTAINER_PAD if parent is not None else GRID_ORIGIN
        placed_bottom = max(((n.y or 0) + n.h for n in siblings if n.x is not None and n.y is not None), default=None)
        start_y = origin_y if placed_bottom is None else placed_bottom + GRID_GAP
        cols = max(1, math.ceil(math.sqrt(len(unplaced))))
        col_w = max(n.w for n in unplaced) + GRID_GAP
        row_h = max(n.h for n in unplaced) + GRID_GAP
        for i, n in enumerate(unplaced):
            row, col = divmod(i, cols)
            x = n.x if n.x is not None else origin_x + col * col_w
            y = n.y if n.y is not None else start_y + row * row_h
            out[n.id] = (x, y)
    return out


def _grow_containers(page: Page, auto_parents: set[str]) -> None:
    by_id = {c.id: c for c in page.cells}
    children: dict[str, list[Cell]] = {}
    for c in page.cells:
        if c.vertex and c.parent in by_id and by_id[c.parent].vertex:
            children.setdefault(c.parent, []).append(c)
    for c in reversed(page.cells):              # deepest containers first
        if c.id not in auto_parents or c.geometry is None:
            continue
        kids = [k for k in children.get(c.id, []) if k.geometry is not None]
        if not kids:
            continue
        need_w = max(k.geometry.x + k.geometry.width for k in kids) + CONTAINER_GROW_PAD
        need_h = max(k.geometry.y + k.geometry.height for k in kids) + CONTAINER_GROW_PAD
        c.geometry.width = max(c.geometry.width, need_w)
        c.geometry.height = max(c.geometry.height, need_h)


def node_cell(node: NodeSpec, x: float, y: float) -> Cell:
    style = node_style(node)
    value = text_to_value(node.label, is_html(style))
    wrapper_attrs: dict[str, str] = {}
    if node.tooltip:
        wrapper_attrs["tooltip"] = node.tooltip
    if node.link:
        wrapper_attrs["link"] = node.link
    wrapper_attrs.update(node.props)
    return Cell(id=node.id, value=value, style=style, vertex=True, parent=node.parent or "1",
                wrapper="UserObject" if wrapper_attrs else None, wrapper_attrs=wrapper_attrs,
                geometry=Geometry(x=x, y=y, width=node.w, height=node.h))


def edge_cell(edge: EdgeSpec) -> Cell:
    style = edge_style(edge)
    return Cell(id=edge.id, value=text_to_value(edge.label, is_html(style)), style=style, edge=True, parent="1",
                source=edge.source, target=edge.target,
                geometry=Geometry(relative=True, points=[(float(a), float(b)) for a, b in edge.waypoints]))


def build_page(page: PageSpec) -> Page:
    positions = grid_positions(page) if page.layout != "none" else {}
    auto_parents = {n.parent for n in page.nodes if n.parent is not None and n.id in positions
                    and (n.x is None or n.y is None)}
    attrs = dict(MODEL_DEFAULTS)
    attrs.update({"grid": "1" if page.grid else "0", "pageWidth": str(page.page_width),
                  "pageHeight": str(page.page_height)})
    if page.background:
        attrs["background"] = page.background.lower()
    cells = [Cell(id="0"), Cell(id="1", parent="0")]
    for n in _topological(page.nodes):
        x, y = positions.get(n.id, (n.x, n.y))
        cells.append(node_cell(n, float(x or 0.0), float(y or 0.0)))
    cells += [edge_cell(e) for e in page.edges]
    result = Page(id=page.id, name=page.name, model_attrs=attrs, cells=cells)
    _grow_containers(result, auto_parents)
    return result


def build_diagram(spec: DiagramSpec) -> Diagram:
    return Diagram(pages=[build_page(p) for p in spec.pages])
