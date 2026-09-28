"""DXF with ``ezdxf`` (MIT) — no Autodesk software involved.

Rules for every DXF written by the bridge (H-20):

* ``$INSUNITS = 0`` (unitless): the numbers are PDF points of the source page, not millimetres or inches. The unit is
  stated in XDATA ``VKM_UNITS=PAGE_PT`` (application ``VKM``) on every entity and in the document metadata;
* fixed header dates and GUIDs (``ezdxf`` fixed-metadata mode) → the same input gives the same bytes;
* coordinate space ``DRAWING_UNITS`` with ``crs_status`` ``UNKNOWN_CRS`` (``SCHEMATIC`` only with a rationale); no EPSG.

Vector input is the ``vkm.vector_paths/1`` artifact of the document pipeline (``get_drawings()`` of PyMuPDF in
PAGE_PT_TL: y grows downwards). The Y axis is flipped for CAD: ``y = page_height_pt − y_page`` when the page height is
known, otherwise ``y = −y_page``; the choice is written into the manifest.
"""
from __future__ import annotations

import gzip
import io
import json
import threading
from typing import Any, Iterable

from vkm_cad.errors import ToolFailure

# R12 is not offered: it has no $INSUNITS header variable and no document metadata dictionary
DXF_VERSIONS = {"R2000": "AC1015", "R2004": "AC1018", "R2007": "AC1021", "R2010": "AC1024", "R2013": "AC1027",
                "R2018": "AC1032"}
APPID = "VKM"
UNITS_XDATA = "VKM_UNITS=PAGE_PT"
PATH_LAYERS = {"s": "VKM_STROKE", "f": "VKM_FILL", "fs": "VKM_FILL_STROKE", "clip": "VKM_CLIP"}
DEFAULT_LAYER = "VKM_PATH"
VECTOR_SCHEMA = "vkm.vector_paths/1"
MAX_VECTOR_BYTES = 256 * 1024 * 1024
MAX_PATHS = 500_000
_SAVE_LOCK = threading.Lock()


def ezdxf_module() -> Any:
    try:
        import ezdxf
    except ImportError as exc:
        raise ToolFailure("EZDXF_UNAVAILABLE", "ezdxf is not installed (extra 'desktop')") from exc
    return ezdxf


def ezdxf_version() -> str | None:
    try:
        return ezdxf_module().__version__
    except ToolFailure:
        return None


def new_document(dxf_version: str = "R2013", metadata: dict[str, str] | None = None) -> Any:
    if dxf_version not in DXF_VERSIONS:
        raise ToolFailure("INVALID_ARGUMENT", f"dxf_version must be one of {sorted(DXF_VERSIONS)}")
    ezdxf = ezdxf_module()
    doc = ezdxf.new(dxfversion=DXF_VERSIONS[dxf_version], setup=False)
    doc.header["$INSUNITS"] = 0
    if APPID not in doc.appids:
        doc.appids.add(APPID)
    for name in [*PATH_LAYERS.values(), DEFAULT_LAYER]:
        if name not in doc.layers:
            doc.layers.add(name)
    set_metadata(doc, {"VKM_UNITS": "PAGE_PT", "VKM_COORDINATE_SPACE": "DRAWING_UNITS",
                       "VKM_CRS_STATUS": "UNKNOWN_CRS", **(metadata or {})})
    return doc


def set_metadata(doc: Any, values: dict[str, str]) -> None:
    if doc.dxfversion <= "AC1009":           # R12 files read by the bridge have no metadata dictionary
        return
    meta = doc.ezdxf_metadata()
    for key, value in sorted(values.items()):
        meta[key] = str(value)[:250]


def to_bytes(doc: Any) -> bytes:
    """Deterministic DXF text (fixed dates and GUIDs, LF)."""
    ezdxf = ezdxf_module()
    from ezdxf.document import CONST_MARKER_STRING, CREATED_BY_EZDXF

    with _SAVE_LOCK:
        previous = ezdxf.options.write_fixed_meta_data_for_testing
        ezdxf.options.write_fixed_meta_data_for_testing = True
        try:
            if doc.dxfversion > "AC1009":      # the creation stamp is set by ezdxf.new(): replace it with the constant
                meta = doc.ezdxf_metadata()
                if CREATED_BY_EZDXF in meta:
                    meta[CREATED_BY_EZDXF] = CONST_MARKER_STRING
            stream = io.StringIO()
            doc.write(stream)
        finally:
            ezdxf.options.write_fixed_meta_data_for_testing = previous
    return stream.getvalue().encode(doc.output_encoding, errors="strict")


def read_bytes(data: bytes) -> Any:
    """ASCII DXF (LF or CRLF line ends, as AutoCAD writes them) → ezdxf document."""
    ezdxf = ezdxf_module()
    try:
        return ezdxf.read(io.StringIO(_decode_dxf(data).replace("\r\n", "\n")))
    except Exception as exc:  # noqa: BLE001 - ezdxf raises several structure errors
        raise ToolFailure("DXF_READ_ERROR", f"cannot read DXF: {type(exc).__name__}") from exc


def _decode_dxf(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


# ---------------------------------------------------------------------------------------------------- vectors in
def load_vector_json(data: bytes) -> dict[str, Any]:
    if len(data) > MAX_VECTOR_BYTES:
        raise ToolFailure("PAYLOAD_TOO_LARGE", "vector artifact is larger than the bridge limit")
    raw = data
    if data[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(data)
        except OSError as exc:
            raise ToolFailure("VECTOR_FORMAT_UNSUPPORTED", "corrupt gzip vector artifact") from exc
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ToolFailure("VECTOR_FORMAT_UNSUPPORTED", "vector artifact is not JSON") from exc
    if not isinstance(obj, dict) or obj.get("schema") != VECTOR_SCHEMA or not isinstance(obj.get("paths"), list):
        raise ToolFailure("VECTOR_FORMAT_UNSUPPORTED", f"expected a {VECTOR_SCHEMA} document with 'paths'")
    if obj.get("bbox_space", "PAGE_PT_TL") != "PAGE_PT_TL":
        raise ToolFailure("VECTOR_FORMAT_UNSUPPORTED", "vector geometry must be in PAGE_PT_TL")
    if len(obj["paths"]) > MAX_PATHS:
        raise ToolFailure("PAYLOAD_TOO_LARGE", f"more than {MAX_PATHS} paths")
    return obj


def _rgb(color: Any) -> tuple[int, int, int] | None:
    if not isinstance(color, (list, tuple)) or not color:
        return None
    try:
        c = [min(1.0, max(0.0, float(v))) for v in color]
    except (TypeError, ValueError):
        return None
    if len(c) == 1:
        c = c * 3
    elif len(c) == 4:                              # CMYK → RGB (approximate, for display only)
        k = c[3]
        c = [(1 - c[0]) * (1 - k), (1 - c[1]) * (1 - k), (1 - c[2]) * (1 - k)]
    elif len(c) != 3:
        return None
    return tuple(int(round(v * 255)) for v in c)  # type: ignore[return-value]


def add_vector_paths(doc: Any, vectors: dict[str, Any], *, page_height_pt: float | None) -> dict[str, Any]:
    """Draw ``vkm.vector_paths/1`` items: ``l`` → LINE, ``c`` → cubic SPLINE (Bezier control polygon), ``re``/``qu``
    → closed LWPOLYLINE. Returns counts and the Y transform used."""
    msp = doc.modelspace()

    def tr(point: Iterable[float]) -> tuple[float, float]:
        x, y = (float(v) for v in list(point)[:2])
        return (x, page_height_pt - y) if page_height_pt is not None else (x, -y)

    counts = {"LINE": 0, "SPLINE": 0, "LWPOLYLINE": 0, "skipped_items": 0, "paths": 0}
    for path in vectors["paths"]:
        if not isinstance(path, dict):
            counts["skipped_items"] += 1
            continue
        counts["paths"] += 1
        layer = PATH_LAYERS.get(path.get("type"), DEFAULT_LAYER)
        xdata = [(1000, UNITS_XDATA), (1000, f"seqno={path.get('seqno')}"), (1000, f"path_type={path.get('type')}")]
        if path.get("layer"):
            xdata.append((1000, f"ocg_layer={str(path['layer'])[:200]}"))
        if path.get("width") is not None:
            xdata.append((1000, f"stroke_width_pt={path['width']}"))
        fill = _rgb(path.get("fill"))
        if fill is not None:
            xdata.append((1000, "fill_rgb=#%02x%02x%02x" % fill))
        stroke = _rgb(path.get("color"))
        for item in path.get("items") or []:
            try:
                entity = _add_item(msp, item, tr, layer)
            except (TypeError, ValueError, IndexError):
                entity = None
            if entity is None:
                counts["skipped_items"] += 1
                continue
            counts[entity.dxftype()] += 1
            entity.set_xdata(APPID, xdata)
            if stroke is not None and doc.dxfversion > "AC1009":
                entity.rgb = stroke
    return counts


def _add_item(msp: Any, item: Any, tr: Any, layer: str) -> Any:
    attrs = {"layer": layer}
    op = item[0] if isinstance(item, (list, tuple)) and item else None
    if op == "l":
        return msp.add_line(tr(item[1]), tr(item[2]), dxfattribs=attrs)
    if op == "c":
        return msp.add_open_spline([tr(p) for p in item[1:5]], degree=3, dxfattribs=attrs)
    if op == "re":
        x0, y0, x1, y1 = (float(v) for v in item[1])
        return msp.add_lwpolyline([tr(p) for p in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))], close=True,
                                  dxfattribs=attrs)
    if op == "qu":
        ul, ur, ll, lr = item[1]
        return msp.add_lwpolyline([tr(p) for p in (ul, ur, lr, ll)], close=True, dxfattribs=attrs)
    return None


# ---------------------------------------------------------------------------------------------------- geometry out
def _xy(point: Any) -> list[float]:
    return [round(float(point[0]), 4), round(float(point[1]), 4)]


def entity_geometry(entity: Any) -> dict[str, Any] | None:
    kind = entity.dxftype()
    dxf = entity.dxf
    if kind == "LINE":
        return {"points": [_xy(dxf.start), _xy(dxf.end)]}
    if kind == "LWPOLYLINE":
        return {"points": [_xy(p) for p in entity.get_points("xy")], "closed": bool(entity.closed)}
    if kind == "POLYLINE":
        return {"points": [_xy(v.dxf.location) for v in entity.vertices], "closed": bool(entity.is_closed)}
    if kind == "SPLINE":
        return {"control_points": [_xy(p) for p in entity.control_points], "degree": int(dxf.degree)}
    if kind in ("CIRCLE", "ARC"):
        out = {"center": _xy(dxf.center), "radius": round(float(dxf.radius), 4)}
        if kind == "ARC":
            out.update(start_angle=round(float(dxf.start_angle), 4), end_angle=round(float(dxf.end_angle), 4))
        return out
    if kind == "POINT":
        return {"points": [_xy(dxf.location)]}
    if kind in ("TEXT", "MTEXT"):
        return {"insert": _xy(dxf.insert)}
    if kind == "INSERT":
        return {"insert": _xy(dxf.insert), "block": str(dxf.name)}
    return None


def extract_geometry(doc: Any, *, layers: Iterable[str] | None, types: Iterable[str] | None,
                     limit: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Model-space entities in file order → rows for ``geometry.jsonl`` plus a summary."""
    from ezdxf import bbox as ezbbox

    layer_set = {x.lower() for x in layers} if layers else None
    type_set = {x.upper() for x in types} if types else None
    rows: list[dict[str, Any]] = []
    by_type: dict[str, int] = {}
    by_layer: dict[str, int] = {}
    truncated = False
    for entity in doc.modelspace():
        kind = entity.dxftype()
        layer = str(entity.dxf.get("layer", "0"))
        if layer_set is not None and layer.lower() not in layer_set:
            continue
        if type_set is not None and kind not in type_set:
            continue
        if len(rows) >= limit:
            truncated = True
            break
        by_type[kind] = by_type.get(kind, 0) + 1
        by_layer[layer] = by_layer.get(layer, 0) + 1
        box = ezbbox.extents([entity], fast=True)
        row: dict[str, Any] = {"handle": entity.dxf.handle, "type": kind, "layer": layer,
                               "bbox": [_xy(box.extmin), _xy(box.extmax)] if box.has_data else None}
        geometry = entity_geometry(entity)
        if geometry is not None:
            row.update(geometry)
        xdata = _vkm_xdata(entity)
        if xdata:
            row["vkm"] = xdata
        rows.append(row)
    summary = {"entities": len(rows), "by_type": dict(sorted(by_type.items())),
               "by_layer": dict(sorted(by_layer.items())), "truncated": truncated,
               "insunits": int(doc.header.get("$INSUNITS", 0))}
    return rows, summary


def _vkm_xdata(entity: Any) -> dict[str, str]:
    try:
        tags = entity.get_xdata(APPID)
    except Exception:  # noqa: BLE001 - no XDATA for this application
        return {}
    out = {}
    for code, value in tags:
        if code == 1000 and "=" in str(value):
            key, val = str(value).split("=", 1)
            out[key] = val
    return out
