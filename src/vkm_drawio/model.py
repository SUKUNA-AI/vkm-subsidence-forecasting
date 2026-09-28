"""Structured diagram specification (the input of ``drawio_create_diagram`` and of ``add_*`` update operations).

Coordinates are draw.io units (px at 100 % zoom); a child's ``x``/``y`` is relative to its parent container, as in
draw.io. Labels are plain text (see :func:`vkm_drawio.styles.text_to_value`).

Layout per page:

* ``none`` — every node carries explicit ``x`` and ``y``;
* ``grid`` — nodes without coordinates are placed on a deterministic grid in spec order (no graph layout);
* ``drawio:<preset>`` — the draw.io Desktop CLI lays the page out (``verticalFlow``, ``horizontalFlow``,
  ``verticalTree``, ``horizontalTree``, ``radialTree``, ``organic``, ``elkLayered`` or ``elkLayered:<RIGHT|DOWN|LEFT|
  UP>``); the result is canonicalised and nodes with explicit coordinates keep them.
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from vkm_drawio.styles import DEFAULT_EDGE_PRESET, DEFAULT_NODE_PRESET, EDGE_PRESETS, NODE_PRESETS

ID_PATTERN = r"^[A-Za-z0-9_.:-]{1,64}$"
RESERVED_CELL_IDS = frozenset({"0", "1"})
PROP_KEY_PATTERN = r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$"
RESERVED_PROP_KEYS = frozenset({"id", "label", "tooltip", "link", "placeholders", "style", "parent", "vertex", "edge",
                                "source", "target", "as", "value"})
DRAWIO_LAYOUT_PRESETS = ("verticalFlow", "horizontalFlow", "verticalTree", "horizontalTree", "radialTree", "organic")
ELK_DIRECTIONS = ("RIGHT", "DOWN", "LEFT", "UP")
LAYOUT_PATTERN = re.compile(r"^(none|grid|drawio:(?:" + "|".join(DRAWIO_LAYOUT_PRESETS)
                            + r"|elkLayered(?::(?:" + "|".join(ELK_DIRECTIONS) + r"))?))$")
MAX_PAGES = 20
MAX_NODES = 2000
MAX_EDGES = 4000
MAX_LABEL = 2000
COORD_LIMIT = 100_000.0

NodePreset = Literal["box", "rounded", "ellipse", "cylinder", "note", "document", "hexagon", "actor", "text",
                     "container", "swimlane"]
EdgePreset = Literal["arrow", "dashed", "plain", "both", "straight"]

_LINK_OK = re.compile(r"^(https://[^\s\"<>]+|data:page/id,[A-Za-z0-9_.:-]+|[A-Za-z0-9_.\-][^\s:\"<>\\]*)$")


def check_link(link: str) -> str:
    """Links: ``https://…``, a draw.io page link ``data:page/id,<id>`` or a relative path (no scheme, no drive)."""
    if not _LINK_OK.match(link) or link.startswith(("//", "/")) or re.match(r"^[A-Za-z]:", link):
        raise ValueError("link must be https://…, data:page/id,<page id> or a relative path")
    return link


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NodeSpec(_Strict):
    id: str = Field(pattern=ID_PATTERN, description="cell id, unique on the page; '0' and '1' are reserved")
    label: str = Field("", max_length=MAX_LABEL, description="plain text; new lines allowed")
    preset: NodePreset | None = Field(None, description="named style (default 'rounded'); 'style' is merged over it")
    style: str | None = Field(None, max_length=2000, description="raw draw.io style 'key=value;…' (wins per key)")
    parent: str | None = Field(None, pattern=ID_PATTERN, description="id of the containing node (container/group)")
    x: float | None = Field(None, ge=-COORD_LIMIT, le=COORD_LIMIT)
    y: float | None = Field(None, ge=-COORD_LIMIT, le=COORD_LIMIT)
    w: float = Field(120.0, gt=0, le=10_000)
    h: float = Field(50.0, gt=0, le=10_000)
    tooltip: str | None = Field(None, max_length=MAX_LABEL)
    link: str | None = Field(None, max_length=500)
    props: dict[str, str] = Field(default_factory=dict, description="custom data attributes (draw.io 'Edit Data')")

    @field_validator("id")
    @classmethod
    def _not_reserved(cls, v: str) -> str:
        if v in RESERVED_CELL_IDS:
            raise ValueError("cell ids '0' and '1' are reserved for the root and the default layer")
        return v

    @field_validator("link")
    @classmethod
    def _link(cls, v: str | None) -> str | None:
        return None if v is None else check_link(v)

    @field_validator("props")
    @classmethod
    def _props(cls, v: dict[str, str]) -> dict[str, str]:
        for key, value in v.items():
            if not re.match(PROP_KEY_PATTERN, key) or key in RESERVED_PROP_KEYS:
                raise ValueError(f"invalid or reserved property name {key!r}")
            if len(value) > MAX_LABEL:
                raise ValueError(f"property {key!r} is too long")
        return v

    def base_style(self) -> str:
        """The preset's style (default preset ``rounded``); a raw ``style`` is merged over it key by key."""
        return NODE_PRESETS[self.preset or DEFAULT_NODE_PRESET]


class EdgeSpec(_Strict):
    id: str = Field(pattern=ID_PATTERN)
    source: str = Field(pattern=ID_PATTERN)
    target: str = Field(pattern=ID_PATTERN)
    label: str = Field("", max_length=MAX_LABEL)
    preset: EdgePreset | None = None
    style: str | None = Field(None, max_length=2000)
    waypoints: list[tuple[float, float]] = Field(default_factory=list, max_length=100)

    @field_validator("id")
    @classmethod
    def _not_reserved(cls, v: str) -> str:
        if v in RESERVED_CELL_IDS:
            raise ValueError("cell ids '0' and '1' are reserved")
        return v

    def base_style(self) -> str:
        """The preset's style (default preset ``arrow``: orthogonal routing); a raw ``style`` is merged over it."""
        return EDGE_PRESETS[self.preset or DEFAULT_EDGE_PRESET]


class PageSpec(_Strict):
    id: str = Field(pattern=ID_PATTERN)
    name: str = Field(min_length=1, max_length=200)
    nodes: list[NodeSpec] = Field(default_factory=list, max_length=MAX_NODES)
    edges: list[EdgeSpec] = Field(default_factory=list, max_length=MAX_EDGES)
    layout: str = Field("none", description="none | grid | drawio:<preset> (see module docs)")
    page_width: int = Field(1169, ge=100, le=20_000)
    page_height: int = Field(827, ge=100, le=20_000)
    grid: bool = True
    background: str | None = Field(None, pattern=r"^#[0-9a-fA-F]{6}$",
                                   description="page background colour, e.g. '#ffffff' (default: none/transparent)")

    @field_validator("layout")
    @classmethod
    def _layout(cls, v: str) -> str:
        if not LAYOUT_PATTERN.match(v):
            raise ValueError("layout must be none, grid, drawio:<" + "|".join(DRAWIO_LAYOUT_PRESETS)
                             + "> or drawio:elkLayered[:RIGHT|DOWN|LEFT|UP]")
        return v

    @model_validator(mode="after")
    def _graph(self) -> "PageSpec":
        ids: set[str] = set()
        for cell_id in [n.id for n in self.nodes] + [e.id for e in self.edges]:
            if cell_id in ids:
                raise ValueError(f"duplicate cell id {cell_id!r} on page {self.id!r}")
            ids.add(cell_id)
        nodes = {n.id: n for n in self.nodes}
        for n in self.nodes:
            if n.parent is not None and n.parent not in nodes:
                raise ValueError(f"node {n.id!r}: parent {n.parent!r} is not a node of this page")
        for n in self.nodes:                                   # container cycles
            seen, cur = {n.id}, n.parent
            while cur is not None:
                if cur in seen:
                    raise ValueError(f"container cycle through {n.id!r}")
                seen.add(cur)
                cur = nodes[cur].parent
        for e in self.edges:
            for end in (e.source, e.target):
                if end not in nodes:
                    raise ValueError(f"edge {e.id!r}: endpoint {end!r} is not a node of this page")
        if self.layout == "none":
            missing = [n.id for n in self.nodes if n.x is None or n.y is None]
            if missing:
                raise ValueError(f"layout 'none' needs x and y for every node; missing: {missing[:10]}")
        return self


class DiagramSpec(_Strict):
    pages: list[PageSpec] = Field(min_length=1, max_length=MAX_PAGES)

    @model_validator(mode="after")
    def _pages(self) -> "DiagramSpec":
        ids = [p.id for p in self.pages]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate page ids")
        return self

    def needs_drawio(self) -> bool:
        return any(p.layout.startswith("drawio:") for p in self.pages)


def drawio_layout_argument(layout: str) -> str:
    """``drawio:<preset>`` → the value of the draw.io CLI ``--layout`` option (preset name or fixed JSON)."""
    if not layout.startswith("drawio:"):
        raise ValueError("not a draw.io layout")
    name = layout.split(":", 1)[1]
    if name.startswith("elkLayered"):
        direction = name.split(":", 1)[1] if ":" in name else "DOWN"
        return '[{"layout":"elkLayered","config":{"elk.direction":"' + direction + '"}}]'
    return name
