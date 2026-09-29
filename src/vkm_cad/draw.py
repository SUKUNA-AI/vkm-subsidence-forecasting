"""Drawings from a JSON spec with ezdxf (no Autodesk needed), and DXF writers for derived geometry.

Spec (``cad_draw``)::

    {"units": "m" | "mm" | "unitless",
     "layers": [{"name", "color", "linetype", "lineweight"}],
     "blocks": [{"name", "base": [x, y], "entities": [...], "attdefs": [{"tag", "at", "height", "prompt", "default"}]}],
     "entities": [
       {"type": "point", "at": [x, y, z?]},            {"type": "line", "start": [..], "end": [..]},
       {"type": "polyline", "points": [[x, y], ...], "closed": false, "elevation": 0},
       {"type": "polyline3d", "points": [[x, y, z], ...], "closed": false},
       {"type": "circle", "center": [..], "radius": r},  {"type": "arc", "center", "radius", "start_angle", "end_angle"},
       {"type": "text", "at": [..], "text": "…", "height": h, "rotation": 0, "align": "LEFT"},
       {"type": "mtext", "at": [..], "text": "…", "height": h, "width": w},
       {"type": "hatch", "boundary": [[x, y], ...], "pattern": "SOLID" | "ANSI31" …, "scale": 1, "angle": 0},
       {"type": "insert", "block": "NAME", "at": [..], "scale": 1, "rotation": 0, "attributes": {"TAG": "value"}},
       {"type": "dimension", "kind": "linear" | "aligned", "p1": [..], "p2": [..], "base": [..], "angle": 0,
        "distance": 5}
     ]}

Every entity takes ``layer``, ``color`` (ACI 1…255), ``linetype``, ``lineweight`` (1/100 mm). Text uses the style
``VKM_ARIAL`` (Arial TrueType: Cyrillic renders in AutoCAD). Output bytes are deterministic (fixed header dates).
``$INSUNITS`` follows ``units``; coordinates stay DRAWING_UNITS with ``crs_status = UNKNOWN_CRS`` unless the caller
passed an explicit transform (recorded as MODEL_CHOICE by the service).
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Sequence

from vkm_cad import dxf
from vkm_cad.errors import ToolFailure

INSUNITS = {"unitless": 0, "mm": 4, "cm": 5, "m": 6, "km": 7}
MAX_ENTITIES = 200_000
TEXT_STYLE = "VKM_ARIAL"
ALIGN = {"LEFT", "CENTER", "RIGHT", "MIDDLE_LEFT", "MIDDLE_CENTER", "MIDDLE_RIGHT", "TOP_LEFT", "TOP_CENTER",
         "TOP_RIGHT", "BOTTOM_LEFT", "BOTTOM_CENTER", "BOTTOM_RIGHT"}


def new_world_document(units: str = "unitless", metadata: dict[str, str] | None = None,
                       dxf_version: str = "R2018") -> Any:
    if units not in INSUNITS:
        raise ToolFailure("INVALID_ARGUMENT", f"units is one of {sorted(INSUNITS)}")
    if dxf_version not in dxf.DXF_VERSIONS:
        raise ToolFailure("INVALID_ARGUMENT", f"dxf_version must be one of {sorted(dxf.DXF_VERSIONS)}")
    ezdxf = dxf.ezdxf_module()
    doc = ezdxf.new(dxfversion=dxf.DXF_VERSIONS[dxf_version], setup=True)
    doc.header["$INSUNITS"] = INSUNITS[units]
    doc.header["$MEASUREMENT"] = 1
    if dxf.APPID not in doc.appids:
        doc.appids.add(dxf.APPID)
    if TEXT_STYLE not in doc.styles:
        doc.styles.add(TEXT_STYLE, font="arial.ttf")
    dxf.set_metadata(doc, {"VKM_UNITS": units.upper(), "VKM_COORDINATE_SPACE": "DRAWING_UNITS",
                           "VKM_CRS_STATUS": "UNKNOWN_CRS", **(metadata or {})})
    return doc


def _num(value: Any, what: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise ToolFailure("INVALID_ARGUMENT", f"{what}: not a number") from exc
    if not math.isfinite(out):
        raise ToolFailure("INVALID_ARGUMENT", f"{what}: not finite")
    return out


def _pt(value: Any, what: str, dim: int = 2) -> tuple[float, ...]:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        raise ToolFailure("INVALID_ARGUMENT", f"{what}: a point is [x, y] or [x, y, z]")
    coords = [_num(v, what) for v in value[:3]]
    if dim == 3:
        coords += [0.0] * (3 - len(coords))
        return tuple(coords[:3])
    return tuple(coords[:max(2, min(len(coords), 3))])


def _attribs(item: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"layer": str(item.get("layer") or "0")[:255]}
    if item.get("color") is not None:
        color = int(item["color"])
        if not 1 <= color <= 256:
            raise ToolFailure("INVALID_ARGUMENT", "color is an ACI index 1…256")
        out["color"] = color
    if item.get("linetype"):
        out["linetype"] = str(item["linetype"])
    if item.get("lineweight") is not None:
        out["lineweight"] = int(item["lineweight"])
    return out


def ensure_layers(doc: Any, layers: Iterable[dict[str, Any]]) -> None:
    for layer in layers:
        name = str(layer.get("name") or "").strip()
        if not name:
            raise ToolFailure("INVALID_ARGUMENT", "a layer needs a name")
        attrs: dict[str, Any] = {}
        if layer.get("color") is not None:
            attrs["color"] = int(layer["color"])
        if layer.get("linetype"):
            if layer["linetype"] not in doc.linetypes:
                raise ToolFailure("INVALID_ARGUMENT", f"unknown linetype {layer['linetype']!r}")
            attrs["linetype"] = layer["linetype"]
        if layer.get("lineweight") is not None:
            attrs["lineweight"] = int(layer["lineweight"])
        if name in doc.layers:
            entry = doc.layers.get(name)
            for k, v in attrs.items():
                entry.dxf.set(k, v)
        else:
            doc.layers.add(name, **attrs)


def add_entities(doc: Any, space: Any, items: Sequence[dict[str, Any]], counts: dict[str, int]) -> None:
    for n, item in enumerate(items):
        kind = str(item.get("type", "")).lower()
        attrs = _attribs(item)
        layer = attrs["layer"]
        if layer not in doc.layers:
            doc.layers.add(layer)
        where = f"entities[{n}] ({kind})"
        if kind == "point":
            space.add_point(_pt(item.get("at"), where, 3), dxfattribs=attrs)
        elif kind == "line":
            space.add_line(_pt(item.get("start"), where, 3), _pt(item.get("end"), where, 3), dxfattribs=attrs)
        elif kind == "polyline":
            pts = [_pt(p, where)[:2] for p in item.get("points") or []]
            if len(pts) < 2:
                raise ToolFailure("INVALID_ARGUMENT", f"{where}: at least two points")
            e = space.add_lwpolyline(pts, close=bool(item.get("closed")), dxfattribs=attrs)
            if item.get("elevation") is not None:
                e.dxf.elevation = _num(item["elevation"], where)
        elif kind == "polyline3d":
            pts = [_pt(p, where, 3) for p in item.get("points") or []]
            if len(pts) < 2:
                raise ToolFailure("INVALID_ARGUMENT", f"{where}: at least two points")
            e = space.add_polyline3d(pts, dxfattribs=attrs)
            if item.get("closed"):
                e.close(True)
        elif kind == "circle":
            space.add_circle(_pt(item.get("center"), where, 3), _num(item.get("radius"), where), dxfattribs=attrs)
        elif kind == "arc":
            space.add_arc(_pt(item.get("center"), where, 3), _num(item.get("radius"), where),
                          _num(item.get("start_angle", 0), where), _num(item.get("end_angle", 90), where),
                          dxfattribs=attrs)
        elif kind == "text":
            align = str(item.get("align") or "LEFT").upper()
            if align not in ALIGN:
                raise ToolFailure("INVALID_ARGUMENT", f"{where}: align is one of {sorted(ALIGN)}")
            from ezdxf.enums import TextEntityAlignment

            e = space.add_text(str(item.get("text", "")), height=_num(item.get("height", 2.5), where),
                               rotation=_num(item.get("rotation", 0), where),
                               dxfattribs={**attrs, "style": TEXT_STYLE})
            e.set_placement(_pt(item.get("at"), where, 3), align=TextEntityAlignment[align])
        elif kind == "mtext":
            e = space.add_mtext(str(item.get("text", "")), dxfattribs={**attrs, "style": TEXT_STYLE,
                                                                         "char_height": _num(item.get("height", 2.5),
                                                                                             where)})
            e.set_location(_pt(item.get("at"), where, 3))
            if item.get("width") is not None:
                e.dxf.width = _num(item["width"], where)
        elif kind == "hatch":
            boundaries = item.get("boundaries") or ([item["boundary"]] if item.get("boundary") else [])
            if not boundaries:
                raise ToolFailure("INVALID_ARGUMENT", f"{where}: boundary [[x, y], ...] is required")
            e = space.add_hatch(color=attrs.get("color", 256), dxfattribs={"layer": layer})
            for ring in boundaries:
                pts = [_pt(p, where)[:2] for p in ring]
                if len(pts) < 3:
                    raise ToolFailure("INVALID_ARGUMENT", f"{where}: a boundary needs three points")
                e.paths.add_polyline_path(pts, is_closed=True)
            pattern = str(item.get("pattern") or "SOLID").upper()
            if pattern != "SOLID":
                e.set_pattern_fill(pattern, scale=_num(item.get("scale", 1), where),
                                   angle=_num(item.get("angle", 0), where))
        elif kind == "insert":
            name = str(item.get("block") or "")
            if name not in doc.blocks:
                raise ToolFailure("INVALID_ARGUMENT", f"{where}: unknown block {name!r}")
            scale = _num(item.get("scale", 1), where)
            e = space.add_blockref(name, _pt(item.get("at"), where, 3),
                                   dxfattribs={**attrs, "xscale": scale, "yscale": scale, "zscale": scale,
                                               "rotation": _num(item.get("rotation", 0), where)})
            values = {str(k): str(v) for k, v in (item.get("attributes") or {}).items()}
            if values or any(True for _ in doc.blocks[name].attdefs()):
                e.add_auto_attribs(values)
        elif kind == "dimension":
            dim_kind = str(item.get("kind") or "linear").lower()
            p1, p2 = _pt(item.get("p1"), where)[:2], _pt(item.get("p2"), where)[:2]
            if dim_kind == "linear":
                dim = space.add_linear_dim(base=_pt(item.get("base") or p1, where)[:2], p1=p1, p2=p2,
                                           angle=_num(item.get("angle", 0), where), dimstyle="EZDXF",
                                           dxfattribs=attrs)
            elif dim_kind == "aligned":
                dim = space.add_aligned_dim(p1=p1, p2=p2, distance=_num(item.get("distance", 5), where),
                                            dimstyle="EZDXF", dxfattribs=attrs)
            else:
                raise ToolFailure("INVALID_ARGUMENT", f"{where}: kind is linear or aligned")
            dim.render()
        else:
            raise ToolFailure("INVALID_ARGUMENT", f"{where}: unknown entity type {kind!r}")
        counts[kind] = counts.get(kind, 0) + 1


def draw_spec(spec: dict[str, Any], metadata: dict[str, str] | None = None) -> tuple[Any, dict[str, Any]]:
    if not isinstance(spec, dict):
        raise ToolFailure("INVALID_ARGUMENT", "spec is a JSON object")
    entities = spec.get("entities") or []
    blocks = spec.get("blocks") or []
    total = len(entities) + sum(len(b.get("entities") or []) for b in blocks)
    if total > MAX_ENTITIES:
        raise ToolFailure("PAYLOAD_TOO_LARGE", f"more than {MAX_ENTITIES} entities")
    doc = new_world_document(str(spec.get("units") or "unitless"), metadata, str(spec.get("dxf_version") or "R2018"))
    ensure_layers(doc, spec.get("layers") or [])
    counts: dict[str, int] = {}
    for block in blocks:
        name = str(block.get("name") or "").strip()
        if not name or name in doc.blocks:
            raise ToolFailure("INVALID_ARGUMENT", f"block name {name!r} is empty or exists")
        blk = doc.blocks.new(name, base_point=_pt(block.get("base") or [0, 0], f"block {name}", 3))
        add_entities(doc, blk, block.get("entities") or [], {})
        for att in block.get("attdefs") or []:
            blk.add_attdef(str(att.get("tag")), _pt(att.get("at") or [0, 0], f"block {name}", 3),
                           dxfattribs={"height": _num(att.get("height", 2.5), "attdef"), "style": TEXT_STYLE,
                                       "prompt": str(att.get("prompt") or att.get("tag")),
                                       "text": str(att.get("default") or "")})
        counts["block"] = counts.get("block", 0) + 1
    add_entities(doc, doc.modelspace(), entities, counts)
    return doc, {"counts": dict(sorted(counts.items())), "layers": sorted(layer.dxf.name for layer in doc.layers),
                 "insunits": int(doc.header["$INSUNITS"])}


# ------------------------------------------------------------------------------------------------ derived geometry
def geometry_document(units: str, metadata: dict[str, str]) -> Any:
    return new_world_document(units, metadata)


def add_points(doc: Any, rows: Iterable[dict[str, Any]], layer: str = "VKM_POINTS", labels: bool = True,
               text_height: float = 1.0) -> int:
    msp = doc.modelspace()
    for name in (layer, layer + "_LABELS"):
        if name not in doc.layers:
            doc.layers.add(name, color=2)
    n = 0
    for row in rows:
        z = row.get("z")
        msp.add_point((row["x"], row["y"], z if z is not None else 0.0), dxfattribs={"layer": layer})
        if labels and row.get("name"):
            msp.add_text(str(row["name"]), height=text_height,
                         dxfattribs={"layer": layer + "_LABELS", "style": TEXT_STYLE,
                                     "insert": (row["x"] + text_height * 0.5, row["y"] + text_height * 0.5)})
        n += 1
    return n


def add_triangles(doc: Any, triangles: Iterable[Sequence[Sequence[float]]], layer: str) -> int:
    msp = doc.modelspace()
    if layer not in doc.layers:
        doc.layers.add(layer, color=3)
    n = 0
    for a, b, c in triangles:
        msp.add_3dface([tuple(a), tuple(b), tuple(c), tuple(c)], dxfattribs={"layer": layer})
        n += 1
    return n


def add_contours(doc: Any, polylines: Iterable[dict[str, Any]], minor_layer: str = "VKM_CONTOUR_MINOR",
                 major_layer: str = "VKM_CONTOUR_MAJOR") -> int:
    msp = doc.modelspace()
    for name, color in ((minor_layer, 8), (major_layer, 5)):
        if name not in doc.layers:
            doc.layers.add(name, color=color)
    n = 0
    for pl in polylines:
        pts = [tuple(p[:2]) for p in pl["points"]]
        if len(pts) < 2:
            continue
        closed = bool(pl.get("closed"))
        if closed and len(pts) > 2 and pts[0] == pts[-1]:
            pts = pts[:-1]
        e = msp.add_lwpolyline(pts, close=closed, dxfattribs={"layer": major_layer if pl.get("major")
                                                                else minor_layer})
        if pl.get("elevation") is not None:
            e.dxf.elevation = float(pl["elevation"])
        n += 1
    return n


def add_profile(doc: Any, rows: list[dict[str, Any]], surfaces: list[str], exaggeration: float = 10.0,
                layer_prefix: str = "VKM_PROFILE") -> int:
    """Profile curves in a separate drawing frame: x = station, y = z × exaggeration (gaps where z is unknown)."""
    msp = doc.modelspace()
    n = 0
    for k, surface in enumerate(surfaces):
        layer = f"{layer_prefix}_{k + 1}"
        if layer not in doc.layers:
            doc.layers.add(layer, color=1 + k % 6)
        run: list[tuple[float, float]] = []
        for row in rows + [None]:  # type: ignore[list-item]
            z = None if row is None else row.get(surface)
            if z is None:
                if len(run) >= 2:
                    msp.add_lwpolyline(run, dxfattribs={"layer": layer})
                    n += 1
                run = []
                continue
            run.append((float(row["station"]), float(z) * exaggeration))
    return n
