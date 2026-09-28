"""draw.io file model: reader (plain or compressed ``.drawio``, embedded ``.svg``/``.png``) and a deterministic writer.

The same spec or the same file content always gives the same bytes:

* ``<mxfile host="vkm-drawio" agent="vkm-drawio writer/1" compressed="false" pages="N">``; no ``modified``, ``etag``,
  ``version`` or view state (``dx``/``dy``);
* fixed attribute order per element, numbers without trailing zeros (at most 2 decimals, ``-0`` → ``0``), zero
  coordinates omitted (draw.io defaults), canonical style strings;
* UTF-8, LF, two-space indentation, final new line.

Cell order is the order in the file (it is the z-order in draw.io); only a spec decides the order of new cells.
Unknown attributes and child elements are kept (sorted), so canonicalising a GUI-edited file loses nothing that draw.io
needs.
"""
from __future__ import annotations

import base64
import struct
import urllib.parse
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass, field

from vkm_drawio.styles import canonical_style

WRITER_AGENT = "vkm-drawio writer/1"
MAX_INPUT_BYTES = 20 * 1024 * 1024
MAX_INFLATED_BYTES = 64 * 1024 * 1024
MODEL_ATTR_ORDER = ("grid", "gridSize", "guides", "tooltips", "connect", "arrows", "fold", "page", "pageScale",
                    "pageWidth", "pageHeight", "math", "shadow")
MODEL_DROP = frozenset({"dx", "dy"})                      # view offsets: UI state, not content
MODEL_DEFAULTS = {"grid": "1", "gridSize": "10", "guides": "1", "tooltips": "1", "connect": "1", "arrows": "1",
                  "fold": "1", "page": "1", "pageScale": "1", "pageWidth": "1169", "pageHeight": "827", "math": "0",
                  "shadow": "0"}
CELL_ATTR_ORDER = ("id", "value", "style", "vertex", "edge", "connectable", "parent", "source", "target")
GEOMETRY_NUMERIC = ("x", "y", "width", "height")
WRAPPER_TAGS = ("UserObject", "object")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_MODEL_KEYS = (b"mxGraphModel", b"mxfile")


class DiagramParseError(ValueError):
    """The input is not a draw.io diagram this reader understands."""


# ---------------------------------------------------------------------------------------------------- data model
@dataclass
class Geometry:
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0
    relative: bool = False
    points: list[tuple[float, float]] = field(default_factory=list)
    source_point: tuple[float, float] | None = None
    target_point: tuple[float, float] | None = None
    offset: tuple[float, float] | None = None
    other_attrs: dict[str, str] = field(default_factory=dict)
    other_children: list[ET.Element] = field(default_factory=list)


@dataclass
class Cell:
    id: str
    value: str | None = None
    style: str | None = None
    vertex: bool = False
    edge: bool = False
    parent: str | None = None
    source: str | None = None
    target: str | None = None
    cell_attrs: dict[str, str] = field(default_factory=dict)        # other mxCell attributes
    wrapper: str | None = None                                       # "UserObject" | "object"
    wrapper_attrs: dict[str, str] = field(default_factory=dict)     # tooltip, link, placeholders, custom data
    geometry: Geometry | None = None
    extra_children: list[ET.Element] = field(default_factory=list)

    @property
    def kind(self) -> str:
        if self.id == "0" and self.parent is None:
            return "root"
        if self.vertex:
            return "vertex"
        if self.edge:
            return "edge"
        if self.parent == "0":
            return "layer"
        return "other"


@dataclass
class Page:
    id: str
    name: str
    model_attrs: dict[str, str] = field(default_factory=lambda: dict(MODEL_DEFAULTS))
    cells: list[Cell] = field(default_factory=list)
    compressed_in_file: bool = False

    def cell(self, cell_id: str) -> Cell | None:
        return next((c for c in self.cells if c.id == cell_id), None)


@dataclass
class Diagram:
    pages: list[Page] = field(default_factory=list)


# ---------------------------------------------------------------------------------------------------- numbers, text
def fmt_num(value: float) -> str:
    v = round(float(value), 2)
    if v == 0:
        return "0"
    if v == int(v):
        return str(int(v))
    return f"{v:.2f}".rstrip("0").rstrip(".")


def _num(text: str | None, where: str) -> float:
    if text is None or text == "":
        return 0.0
    try:
        return float(text)
    except ValueError as exc:
        raise DiagramParseError(f"non-numeric {where}: {text!r}") from exc


def esc_attr(value: str) -> str:
    return (value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
            .replace("\r", "&#13;").replace("\n", "&#10;").replace("\t", "&#9;"))


def esc_text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------------------------------- compression
def compress_model(xml_text: str) -> str:
    """draw.io compressed diagram: encodeURIComponent → raw deflate → base64."""
    encoded = urllib.parse.quote(xml_text, safe="-_.!~*'()")
    comp = zlib.compressobj(9, zlib.DEFLATED, -15)
    return base64.b64encode(comp.compress(encoded.encode("ascii")) + comp.flush()).decode("ascii")


def _inflate(data: bytes, wbits: int) -> bytes:
    d = zlib.decompressobj(wbits)
    out = d.decompress(data, MAX_INFLATED_BYTES)
    if d.unconsumed_tail:
        raise DiagramParseError("compressed diagram inflates beyond the size limit")
    return out


def decompress_model(text: str) -> str:
    try:
        raw = base64.b64decode("".join(text.split()), validate=True)
        inflated = _inflate(raw, -15).decode("utf-8")
    except (ValueError, zlib.error, UnicodeDecodeError) as exc:
        raise DiagramParseError(f"cannot decompress diagram page: {exc}") from exc
    return inflated if inflated.lstrip().startswith("<") else urllib.parse.unquote(inflated)


# ---------------------------------------------------------------------------------------------------- reading
def _parse_xml(text: str) -> ET.Element:
    if "<!DOCTYPE" in text[:2000] and "<!ENTITY" in text:
        raise DiagramParseError("XML entity declarations are not accepted")
    try:
        return ET.fromstring(text)
    except ET.ParseError as exc:
        raise DiagramParseError(f"invalid XML: {exc}") from exc


def _point(el: ET.Element) -> tuple[float, float]:
    return (_num(el.get("x"), "mxPoint x"), _num(el.get("y"), "mxPoint y"))


def _read_geometry(el: ET.Element) -> Geometry:
    g = Geometry(x=_num(el.get("x"), "x"), y=_num(el.get("y"), "y"), width=_num(el.get("width"), "width"),
                 height=_num(el.get("height"), "height"), relative=el.get("relative") == "1")
    for key, value in el.attrib.items():
        if key not in GEOMETRY_NUMERIC and key not in ("relative", "as"):
            g.other_attrs[key] = value
    for child in el:
        role = child.get("as")
        if child.tag == "mxPoint" and role == "sourcePoint":
            g.source_point = _point(child)
        elif child.tag == "mxPoint" and role == "targetPoint":
            g.target_point = _point(child)
        elif child.tag == "mxPoint" and role == "offset":
            g.offset = _point(child)
        elif child.tag == "Array" and role == "points":
            g.points = [_point(p) for p in child if p.tag == "mxPoint"]
        else:
            g.other_children.append(child)
    return g


def _read_cell(el: ET.Element) -> Cell:
    wrapper = None
    wrapper_attrs: dict[str, str] = {}
    if el.tag in WRAPPER_TAGS:
        wrapper = el.tag
        inner = el.find("mxCell")
        if inner is None:
            raise DiagramParseError(f"{el.tag} without mxCell")
        cell_id = el.get("id")
        value = el.get("label")
        wrapper_attrs = {k: v for k, v in el.attrib.items() if k not in ("id", "label")}
        cell_el = inner
    elif el.tag == "mxCell":
        cell_el = el
        cell_id = el.get("id")
        value = el.get("value")
    else:
        raise DiagramParseError(f"unsupported element in <root>: {el.tag}")
    if not cell_id:
        raise DiagramParseError("cell without id")
    cell = Cell(id=cell_id, value=value, style=cell_el.get("style"), vertex=cell_el.get("vertex") == "1",
                edge=cell_el.get("edge") == "1", parent=cell_el.get("parent"), source=cell_el.get("source"),
                target=cell_el.get("target"), wrapper=wrapper, wrapper_attrs=wrapper_attrs)
    for key, val in cell_el.attrib.items():
        if key not in ("id", "value", "style", "vertex", "edge", "parent", "source", "target"):
            cell.cell_attrs[key] = val
    if wrapper is not None and cell_el.get("value") is not None and value is None:
        cell.value = cell_el.get("value")
    for child in cell_el:
        if child.tag == "mxGeometry" and child.get("as", "geometry") == "geometry" and cell.geometry is None:
            cell.geometry = _read_geometry(child)
        else:
            cell.extra_children.append(child)
    return cell


def _read_model(model: ET.Element, page_id: str, name: str, compressed: bool) -> Page:
    if model.tag != "mxGraphModel":
        raise DiagramParseError(f"expected mxGraphModel, got {model.tag}")
    root = model.find("root")
    cells = [] if root is None else [_read_cell(el) for el in root]
    attrs = {k: v for k, v in model.attrib.items() if k not in MODEL_DROP}
    page = Page(id=page_id, name=name, model_attrs=attrs, cells=cells, compressed_in_file=compressed)
    ids = [c.id for c in cells]
    if len(set(ids)) != len(ids):
        dup = sorted({i for i in ids if ids.count(i) > 1})
        raise DiagramParseError(f"duplicate cell ids on page {name!r}: {dup[:10]}")
    return page


def _read_mxfile(root: ET.Element) -> Diagram:
    if root.tag == "mxGraphModel":
        return Diagram(pages=[_read_model(root, "page-1", "Page-1", False)])
    if root.tag != "mxfile":
        raise DiagramParseError(f"not a draw.io file (root element {root.tag})")
    pages: list[Page] = []
    for n, dia in enumerate(root.findall("diagram"), 1):
        page_id = dia.get("id") or f"page-{n}"
        name = dia.get("name") or f"Page-{n}"
        model = dia.find("mxGraphModel")
        if model is not None:
            pages.append(_read_model(model, page_id, name, False))
            continue
        text = (dia.text or "").strip()
        if not text:
            pages.append(Page(id=page_id, name=name, cells=[]))
            continue
        pages.append(_read_model(_parse_xml(decompress_model(text)), page_id, name, True))
    if not pages:
        raise DiagramParseError("mxfile without pages")
    return Diagram(pages=pages)


def _png_model_text(data: bytes) -> str:
    pos = len(PNG_SIGNATURE)
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        ctype = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype not in (b"tEXt", b"zTXt", b"iTXt") or b"\x00" not in body:
            continue
        key, rest = body.split(b"\x00", 1)
        if key not in PNG_MODEL_KEYS:
            continue
        if ctype == b"zTXt":
            raw = _inflate(rest[1:], zlib.MAX_WBITS)
        elif ctype == b"iTXt":
            flag = rest[0]                                  # rest[1]: compression method (deflate)
            rest = rest[2:].split(b"\x00", 2)[-1]          # language tag, translated keyword
            raw = _inflate(rest, zlib.MAX_WBITS) if flag else rest
        else:
            raw = rest
        text = raw.decode("utf-8", errors="strict") if ctype == b"iTXt" else raw.decode("latin-1")
        return urllib.parse.unquote(text) if text.startswith("%") else text
    raise DiagramParseError("PNG has no embedded draw.io model (export with --embed-diagram)")


def read_diagram_bytes(data: bytes) -> Diagram:
    """Parse ``.drawio`` (plain or compressed pages), ``.svg`` with an embedded model or ``.png`` with one."""
    if len(data) > MAX_INPUT_BYTES:
        raise DiagramParseError("file is larger than the reader limit")
    if data.startswith(PNG_SIGNATURE):
        return _read_mxfile(_parse_xml(_png_model_text(data)))
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DiagramParseError("not UTF-8 text") from exc
    root = _parse_xml(text)
    if root.tag.endswith("svg"):
        content = root.get("content")
        if not content:
            raise DiagramParseError("SVG has no embedded draw.io model (export with --embed-diagram)")
        content = content.strip()
        if not content.startswith("<"):                # compressed whole-file form
            content = decompress_model(content)
        return _read_mxfile(_parse_xml(content))
    return _read_mxfile(root)


# ---------------------------------------------------------------------------------------------------- writing
def _attrs(pairs: list[tuple[str, str]]) -> str:
    return "".join(f' {k}="{esc_attr(v)}"' for k, v in pairs)


def _ordered(attrs: dict[str, str], first: tuple[str, ...]) -> list[tuple[str, str]]:
    head = [(k, attrs[k]) for k in first if k in attrs]
    tail = [(k, attrs[k]) for k in sorted(attrs) if k not in first]
    return head + tail


def _generic(el: ET.Element, indent: int, out: list[str]) -> None:
    """Unknown child elements: tag, sorted attributes, children, stripped text."""
    pad = "  " * indent
    attrs = _attrs(sorted(el.attrib.items()))
    children = list(el)
    text = (el.text or "").strip()
    if not children and not text:
        out.append(f"{pad}<{el.tag}{attrs} />")
        return
    if not children:
        out.append(f"{pad}<{el.tag}{attrs}>{esc_text(text)}</{el.tag}>")
        return
    out.append(f"{pad}<{el.tag}{attrs}>")
    for child in children:
        _generic(child, indent + 1, out)
    out.append(f"{pad}</{el.tag}>")


def _point_xml(point: tuple[float, float], role: str | None) -> str:
    pairs: list[tuple[str, str]] = []
    if point[0] != 0:
        pairs.append(("x", fmt_num(point[0])))
    if point[1] != 0:
        pairs.append(("y", fmt_num(point[1])))
    if role:
        pairs.append(("as", role))
    return f"<mxPoint{_attrs(pairs)} />"


def _geometry(g: Geometry, indent: int, out: list[str]) -> None:
    pad = "  " * indent
    pairs: list[tuple[str, str]] = []
    for key, value in (("x", g.x), ("y", g.y), ("width", g.width), ("height", g.height)):
        if value != 0:
            pairs.append((key, fmt_num(value)))
    if g.relative:
        pairs.append(("relative", "1"))
    pairs += sorted(g.other_attrs.items())
    pairs.append(("as", "geometry"))
    children: list[str] = []
    if g.source_point is not None:
        children.append(_point_xml(g.source_point, "sourcePoint"))
    if g.target_point is not None:
        children.append(_point_xml(g.target_point, "targetPoint"))
    if g.points:
        children.append("<Array as=\"points\">")
        children += ["  " + _point_xml(p, None) for p in g.points]
        children.append("</Array>")
    if g.offset is not None:
        children.append(_point_xml(g.offset, "offset"))
    if not children and not g.other_children:
        out.append(f"{pad}<mxGeometry{_attrs(pairs)} />")
        return
    out.append(f"{pad}<mxGeometry{_attrs(pairs)}>")
    out += [f"{pad}  {line}" for line in children]
    for child in g.other_children:
        _generic(child, indent + 1, out)
    out.append(f"{pad}</mxGeometry>")


def _cell(c: Cell, indent: int, out: list[str]) -> None:
    pad = "  " * indent
    core: dict[str, str] = dict(c.cell_attrs)
    if c.style:
        core["style"] = canonical_style(c.style)
    if c.vertex:
        core["vertex"] = "1"
    if c.edge:
        core["edge"] = "1"
    if c.parent is not None:
        core["parent"] = c.parent
    if c.source is not None:
        core["source"] = c.source
    if c.target is not None:
        core["target"] = c.target
    inner_indent = indent
    if c.wrapper:
        wrapper = {**c.wrapper_attrs}
        head = [("id", c.id)] + ([("label", c.value)] if c.value is not None else [])
        out.append(f"{pad}<{c.wrapper}{_attrs(head + sorted(wrapper.items()))}>")
        inner_indent = indent + 1
    else:
        core["id"] = c.id
        if c.value is not None:
            core["value"] = c.value
    ipad = "  " * inner_indent
    attrs = _attrs(_ordered(core, CELL_ATTR_ORDER))
    if c.geometry is None and not c.extra_children:
        out.append(f"{ipad}<mxCell{attrs} />")
    else:
        out.append(f"{ipad}<mxCell{attrs}>")
        if c.geometry is not None:
            _geometry(c.geometry, inner_indent + 1, out)
        for child in c.extra_children:
            _generic(child, inner_indent + 1, out)
        out.append(f"{ipad}</mxCell>")
    if c.wrapper:
        out.append(f"{pad}</{c.wrapper}>")


def _model_attrs(attrs: dict[str, str]) -> list[tuple[str, str]]:
    merged = {**MODEL_DEFAULTS, **{k: v for k, v in attrs.items() if k not in MODEL_DROP}}
    return _ordered(merged, MODEL_ATTR_ORDER)


def write_diagram(diagram: Diagram) -> str:
    """Deterministic, uncompressed draw.io XML."""
    if not diagram.pages:
        raise ValueError("a diagram needs at least one page")
    head = [("host", "vkm-drawio"), ("agent", WRITER_AGENT), ("compressed", "false"),
            ("pages", str(len(diagram.pages)))]
    out = [f"<mxfile{_attrs(head)}>"]
    for page in diagram.pages:
        out.append(f"  <diagram{_attrs([('id', page.id), ('name', page.name)])}>")
        out.append(f"    <mxGraphModel{_attrs(_model_attrs(page.model_attrs))}>")
        out.append("      <root>")
        for cell in page.cells:
            _cell(cell, 4, out)
        out.append("      </root>")
        out.append("    </mxGraphModel>")
        out.append("  </diagram>")
    out.append("</mxfile>")
    return "\n".join(out) + "\n"


def canonicalize_text(data: bytes) -> str:
    """Any readable draw.io content → canonical uncompressed ``.drawio`` text (geometry and cell order kept)."""
    return write_diagram(read_diagram_bytes(data))


# ---------------------------------------------------------------------------------------------------- validation
_CYCLE_LIMIT = 10_000


def validate_page(page: Page) -> list[str]:
    """Structural problems of a page (empty list = valid)."""
    problems: list[str] = []
    ids = [c.id for c in page.cells]
    if len(set(ids)) != len(ids):
        problems.append("duplicate cell ids")
    by_id = {c.id: c for c in page.cells}
    for c in page.cells:
        if c.parent is not None and c.parent not in by_id:
            problems.append(f"cell {c.id!r}: missing parent {c.parent!r}")
        if c.edge:
            for end in (c.source, c.target):
                if end is not None and end not in by_id:
                    problems.append(f"edge {c.id!r}: missing endpoint {end!r}")
        seen, cur, steps = {c.id}, c.parent, 0
        while cur is not None and cur in by_id and steps < _CYCLE_LIMIT:
            if cur in seen:
                problems.append(f"parent cycle through {c.id!r}")
                break
            seen.add(cur)
            cur = by_id[cur].parent
            steps += 1
    return problems


def content_bbox(page: Page) -> tuple[float, float, float, float] | None:
    """Absolute bounding box (x0, y0, x1, y1) of vertices and edge waypoints; ``None`` for an empty page."""
    by_id = {c.id: c for c in page.cells}

    def origin(cell: Cell) -> tuple[float, float]:
        x = y = 0.0
        cur, steps = by_id.get(cell.parent or ""), 0
        while cur is not None and cur.vertex and cur.geometry is not None and steps < _CYCLE_LIMIT:
            x, y = x + cur.geometry.x, y + cur.geometry.y
            cur, steps = by_id.get(cur.parent or ""), steps + 1
        return x, y

    xs: list[float] = []
    ys: list[float] = []
    for c in page.cells:
        if c.geometry is None or c.geometry.relative and c.vertex:
            continue
        ox, oy = origin(c)
        if c.vertex:
            g = c.geometry
            xs += [ox + g.x, ox + g.x + g.width]
            ys += [oy + g.y, oy + g.y + g.height]
        elif c.edge:
            for px, py in c.geometry.points:
                xs.append(ox + px)
                ys.append(oy + py)
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def page_summary(page: Page) -> dict[str, int]:
    kinds = [c.kind for c in page.cells]
    return {"nodes": kinds.count("vertex"), "edges": kinds.count("edge"), "layers": kinds.count("layer")}
