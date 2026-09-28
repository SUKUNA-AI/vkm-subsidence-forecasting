"""Update operations of ``drawio_update_diagram`` — applied in order to a copy, all or nothing.

Operations address a page by id or name (default: the first page) and cells by id. Removing a node that still has
children or connected edges needs ``cascade=true``; removing an edge also removes its label cells.
"""
from __future__ import annotations

import copy
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from vkm_drawio.build import build_page, edge_cell, node_cell
from vkm_drawio.errors import ToolFailure
from vkm_drawio.model import (ID_PATTERN, PROP_KEY_PATTERN, RESERVED_PROP_KEYS, EdgeSpec, NodeSpec, PageSpec,
                              check_link)
from vkm_drawio.styles import is_html, set_style_keys, text_to_value
from vkm_drawio.xmlio import Cell, Diagram, Geometry, Page, validate_page

AUTO_GAP = 40.0
MAX_OPS = 200


class _Op(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page: str | None = Field(None, max_length=200, description="page id or name; default: the first page")


class AddNode(_Op):
    op: Literal["add_node"]
    node: NodeSpec


class UpdateNode(_Op):
    op: Literal["update_node"]
    id: str = Field(pattern=ID_PATTERN)
    label: str | None = Field(None, max_length=2000)
    style: str | None = Field(None, max_length=2000, description="replace the whole style")
    style_set: dict[str, str | None] | None = Field(None, description="set keys; null removes a key")
    x: float | None = None
    y: float | None = None
    w: float | None = Field(None, gt=0)
    h: float | None = Field(None, gt=0)
    parent: str | None = Field(None, max_length=64, description="new container id; '1' = top level")
    tooltip: str | None = Field(None, max_length=2000, description="'' removes")
    link: str | None = Field(None, max_length=500, description="'' removes")
    props: dict[str, str | None] | None = Field(None, description="custom data; null removes a key")


class RemoveNode(_Op):
    op: Literal["remove_node"]
    id: str = Field(pattern=ID_PATTERN)
    cascade: bool = False


class AddEdge(_Op):
    op: Literal["add_edge"]
    edge: EdgeSpec


class UpdateEdge(_Op):
    op: Literal["update_edge"]
    id: str = Field(pattern=ID_PATTERN)
    label: str | None = Field(None, max_length=2000)
    style: str | None = Field(None, max_length=2000)
    style_set: dict[str, str | None] | None = None
    source: str | None = Field(None, pattern=ID_PATTERN)
    target: str | None = Field(None, pattern=ID_PATTERN)
    waypoints: list[tuple[float, float]] | None = Field(None, max_length=100)


class RemoveEdge(_Op):
    op: Literal["remove_edge"]
    id: str = Field(pattern=ID_PATTERN)


class AddPage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["add_page"]
    page_spec: PageSpec
    index: int | None = Field(None, ge=0, le=50)

    @field_validator("page_spec")
    @classmethod
    def _layout(cls, v: PageSpec) -> PageSpec:
        if v.layout not in ("none", "grid"):
            raise ValueError("add_page supports layout 'none' or 'grid' (use drawio_create_diagram for draw.io layout)")
        return v


class RenamePage(_Op):
    op: Literal["rename_page"]
    name: str = Field(min_length=1, max_length=200)


class RemovePage(_Op):
    op: Literal["remove_page"]


class Canonicalize(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["canonicalize"]


Operation = Annotated[AddNode | UpdateNode | RemoveNode | AddEdge | UpdateEdge | RemoveEdge | AddPage | RenamePage
                      | RemovePage | Canonicalize, Field(discriminator="op")]


# ---------------------------------------------------------------------------------------------------- helpers
def _fail(index: int, message: str) -> ToolFailure:
    return ToolFailure("INVALID_DIAGRAM_OP", f"operation {index}: {message}", details={"op_index": index})


def find_page(diagram: Diagram, selector: str | None, index: int = 0) -> Page:
    if selector is None:
        return diagram.pages[0]
    for page in diagram.pages:
        if page.id == selector:
            return page
    named = [p for p in diagram.pages if p.name == selector]
    if len(named) == 1:
        return named[0]
    raise _fail(index, f"page {selector!r} not found" if not named else f"page name {selector!r} is ambiguous")


def _cell(page: Page, cell_id: str, index: int, kind: str) -> Cell:
    cell = page.cell(cell_id)
    if cell is None:
        raise _fail(index, f"{kind} {cell_id!r} not found on page {page.id!r}")
    if kind == "node" and not cell.vertex:
        raise _fail(index, f"cell {cell_id!r} is not a node")
    if kind == "edge" and not cell.edge:
        raise _fail(index, f"cell {cell_id!r} is not an edge")
    return cell


def _descendants(page: Page, cell_id: str) -> list[Cell]:
    out: list[Cell] = []
    frontier = {cell_id}
    while frontier:
        children = [c for c in page.cells if c.parent in frontier]
        out += children
        frontier = {c.id for c in children}
    return out


def _auto_position(page: Page, parent: str) -> tuple[float, float]:
    siblings = [c for c in page.cells if c.vertex and c.parent == parent and c.geometry is not None]
    if parent != "1":
        bottom = max((c.geometry.y + c.geometry.height for c in siblings), default=None)
        return 20.0, 40.0 if bottom is None else bottom + AUTO_GAP / 2
    if not siblings:
        return 40.0, 40.0
    right = max(c.geometry.x + c.geometry.width for c in siblings)
    top = min(c.geometry.y for c in siblings)
    return right + AUTO_GAP, top


def _set_wrapper_attr(cell: Cell, key: str, value: str | None) -> None:
    if value is None or value == "":
        cell.wrapper_attrs.pop(key, None)
    else:
        cell.wrapper_attrs[key] = value
    if cell.wrapper_attrs and cell.wrapper is None:
        cell.wrapper = "UserObject"
    elif not cell.wrapper_attrs and cell.wrapper == "UserObject":
        cell.wrapper = None


def _check_parent_chain(page: Page, cell_id: str, new_parent: str, index: int) -> None:
    by_id = {c.id: c for c in page.cells}
    cur: str | None = new_parent
    while cur is not None and cur in by_id:
        if cur == cell_id:
            raise _fail(index, "the new parent would create a container cycle")
        cur = by_id[cur].parent


# ---------------------------------------------------------------------------------------------------- apply
def apply_ops(diagram: Diagram, ops: list[Operation]) -> tuple[Diagram, list[str]]:
    if not ops:
        raise ToolFailure("INVALID_DIAGRAM_OP", "no operations given")
    if len(ops) > MAX_OPS:
        raise ToolFailure("INVALID_DIAGRAM_OP", f"at most {MAX_OPS} operations per call")
    work = copy.deepcopy(diagram)
    applied: list[str] = []
    for i, op in enumerate(ops):
        applied.append(_apply(work, op, i))
    for page in work.pages:
        problems = validate_page(page)
        if problems:
            raise ToolFailure("INVALID_DIAGRAM_OP", f"result is invalid on page {page.id!r}: {problems[:5]}")
    return work, applied


def _apply(d: Diagram, op: Operation, i: int) -> str:  # noqa: C901 - one branch per operation kind
    if isinstance(op, Canonicalize):
        return "canonicalize"
    if isinstance(op, AddPage):
        if any(p.id == op.page_spec.id for p in d.pages):
            raise _fail(i, f"page id {op.page_spec.id!r} exists")
        position = len(d.pages) if op.index is None else min(op.index, len(d.pages))
        d.pages.insert(position, build_page(op.page_spec))
        return f"add_page {op.page_spec.id}"
    page = find_page(d, op.page, i)
    if isinstance(op, RenamePage):
        page.name = op.name
        return f"rename_page {page.id}"
    if isinstance(op, RemovePage):
        if len(d.pages) == 1:
            raise _fail(i, "cannot remove the only page")
        d.pages.remove(page)
        return f"remove_page {page.id}"
    if isinstance(op, AddNode):
        node = op.node
        if page.cell(node.id) is not None:
            raise _fail(i, f"cell id {node.id!r} exists")
        parent = node.parent or "1"
        if parent != "1":
            _cell(page, parent, i, "node")
        x, y = node.x, node.y
        if x is None or y is None:
            ax, ay = _auto_position(page, parent)
            x, y = (ax if x is None else x), (ay if y is None else y)
        page.cells.append(node_cell(node, float(x), float(y)))
        return f"add_node {node.id}"
    if isinstance(op, UpdateNode):
        cell = _cell(page, op.id, i, "node")
        if op.style is not None:
            cell.style = op.style
        if op.style_set:
            try:
                cell.style = set_style_keys(cell.style, op.style_set)
            except ValueError as exc:
                raise _fail(i, str(exc)) from exc
        if op.label is not None:
            cell.value = text_to_value(op.label, is_html(cell.style))
        geometry = cell.geometry or Geometry()
        for attr, value in (("x", op.x), ("y", op.y), ("width", op.w), ("height", op.h)):
            if value is not None:
                setattr(geometry, attr, float(value))
        cell.geometry = geometry
        if op.parent is not None:
            new_parent = op.parent or "1"
            if new_parent != "1":
                _cell(page, new_parent, i, "node")
                _check_parent_chain(page, cell.id, new_parent, i)
            cell.parent = new_parent
        if op.tooltip is not None:
            _set_wrapper_attr(cell, "tooltip", op.tooltip)
        if op.link is not None:
            try:
                _set_wrapper_attr(cell, "link", check_link(op.link) if op.link else "")
            except ValueError as exc:
                raise _fail(i, str(exc)) from exc
        if op.props:
            for key, value in op.props.items():
                if not re.match(PROP_KEY_PATTERN, key) or key in RESERVED_PROP_KEYS:
                    raise _fail(i, f"invalid or reserved property name {key!r}")
                _set_wrapper_attr(cell, key, value)
        return f"update_node {cell.id}"
    if isinstance(op, RemoveNode):
        cell = _cell(page, op.id, i, "node")
        doomed = [cell, *_descendants(page, cell.id)]
        doomed_ids = {c.id for c in doomed}
        edges = [c for c in page.cells if c.edge and (c.source in doomed_ids or c.target in doomed_ids)]
        extra = [c for c in doomed if c is not cell] + edges
        if extra and not op.cascade:
            raise _fail(i, f"node {cell.id!r} has {len(doomed) - 1} contained cells and {len(edges)} edges; "
                           "pass cascade=true")
        for e in edges:
            doomed_ids |= {e.id, *(c.id for c in _descendants(page, e.id))}
        page.cells = [c for c in page.cells if c.id not in doomed_ids]
        return f"remove_node {cell.id} ({len(doomed_ids)} cells)"
    if isinstance(op, AddEdge):
        edge = op.edge
        if page.cell(edge.id) is not None:
            raise _fail(i, f"cell id {edge.id!r} exists")
        for end in (edge.source, edge.target):
            _cell(page, end, i, "node")
        page.cells.append(edge_cell(edge))
        return f"add_edge {edge.id}"
    if isinstance(op, UpdateEdge):
        cell = _cell(page, op.id, i, "edge")
        if op.style is not None:
            cell.style = op.style
        if op.style_set:
            try:
                cell.style = set_style_keys(cell.style, op.style_set)
            except ValueError as exc:
                raise _fail(i, str(exc)) from exc
        if op.label is not None:
            cell.value = text_to_value(op.label, is_html(cell.style))
        for attr in ("source", "target"):
            end = getattr(op, attr)
            if end is not None:
                _cell(page, end, i, "node")
                setattr(cell, attr, end)
        if op.waypoints is not None:
            cell.geometry = cell.geometry or Geometry(relative=True)
            cell.geometry.points = [(float(a), float(b)) for a, b in op.waypoints]
        return f"update_edge {cell.id}"
    if isinstance(op, RemoveEdge):
        cell = _cell(page, op.id, i, "edge")
        doomed_ids = {cell.id, *(c.id for c in _descendants(page, cell.id))}
        page.cells = [c for c in page.cells if c.id not in doomed_ids]
        return f"remove_edge {cell.id}"
    raise _fail(i, "unknown operation")  # pragma: no cover
