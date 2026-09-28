"""DjVu through the DjVuLibre command-line tools (GPL; called as separate processes, never linked) plus a pure-Python
IFF walk as the independent second page counter.

* pages: ``djvused -e n`` and the count of ``FORM:DJVU`` components (shared ``DJVI`` dictionaries are not pages –
  VKM-SRC-053 has 384 pages and 423 DIRM files);
* text layer: ``djvused -e output-txt`` (one process for the whole document) → zones page/column/region/para/line/word
  with pixel boxes, origin bottom-left → converted to PAGE_PT_TL with the page INFO size and dpi;
* the text layer of a DjVu scan is a foreign OCR layer: ``origin = EMBEDDED_OCR``,
  ``text_layer = DJVU_EMBEDDED_OCR_LAYER``, ``embedded_layer_evidence = DJVU_TXT`` (H-02);
* render: ``ddjvu`` (``vkm_corpus.artifacts.render.render_djvu_page``).
"""
from __future__ import annotations

import re
import shutil
import struct
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EXTRACTOR_ID = "djvulibre-cli"
TOOLS = ("djvused", "djvudump", "ddjvu")


class DjvuToolMissing(RuntimeError):
    pass


def tool_path(name: str) -> str:
    p = shutil.which(name)
    if p is None:
        raise DjvuToolMissing(f"DjVuLibre tool '{name}' not found (DECODER_MISSING)")
    return p


def djvulibre_version() -> str:
    try:
        out = subprocess.run([tool_path("djvused")], capture_output=True, text=True, timeout=30)
    except (DjvuToolMissing, OSError, subprocess.TimeoutExpired):
        return "unavailable"
    m = re.search(r"DjVuLibre-([\d.]+)", out.stdout + out.stderr)
    return m.group(1) if m else "unknown"


def _run(args: list[str], timeout: float) -> str:
    proc = subprocess.run(args, capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"{Path(args[0]).name} exit {proc.returncode}: "
                           f"{proc.stderr.decode('utf-8', 'replace')[:300]}")
    return proc.stdout.decode("utf-8", "replace")


def page_count_cli(path: Path, timeout: float = 120.0) -> int:
    return int(_run([tool_path("djvused"), "-e", "n", str(path)], timeout).strip())


# ---------------------------------------------------------------------------------------------------- IFF walk
@dataclass
class DjvuPageInfo:
    index: int                     # 1-based page number
    component: str | None
    width_px: int | None = None
    height_px: int | None = None
    dpi: int | None = None
    rotation_flags: int | None = None
    chunks: dict[str, int] = field(default_factory=dict)  # chunk id -> bytes

    @property
    def has_text(self) -> bool:
        return "TXTz" in self.chunks or "TXTa" in self.chunks

    @property
    def rotation_deg(self) -> int:
        # INFO flags: 1 = 0°, 6 = 90° ccw, 2 = 180°, 5 = 90° cw (DjVu spec); others → 0
        return {1: 0, 6: 90, 2: 180, 5: 270}.get(self.rotation_flags or 1, 0)


@dataclass
class DjvuStructure:
    top_form: str
    bundled: bool | None
    dirm_files: int | None
    pages: list[DjvuPageInfo]
    shared_components: int
    component_kinds: dict[str, int]


def _chunks(buf: bytes, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        cid = buf[pos:pos + 4].decode("latin-1")
        size = struct.unpack(">I", buf[pos + 4:pos + 8])[0]
        yield cid, pos, size
        pos += 8 + size + (size & 1)


def _page_info(buf: bytes, pos: int, index: int, component: str | None) -> DjvuPageInfo:
    size = struct.unpack(">I", buf[pos + 4:pos + 8])[0]
    info = DjvuPageInfo(index=index, component=component)
    for cid, p, s in _chunks(buf, pos + 12, pos + 8 + size):
        info.chunks[cid] = info.chunks.get(cid, 0) + s
        if cid == "INFO" and s >= 5:
            d = buf[p + 8:p + 8 + s]
            info.width_px, info.height_px = struct.unpack(">HH", d[:4])
            if s >= 8:
                info.dpi = struct.unpack("<H", d[6:8])[0]
            if s >= 10:
                info.rotation_flags = d[9] & 7
    return info


def iff_structure(path: Path) -> DjvuStructure:
    """Independent page enumeration by walking the IFF container (no decoding)."""
    buf = Path(path).read_bytes()
    if buf[:8] != b"AT&TFORM":
        raise ValueError("not an AT&T FORM DjVu file")
    top_size = struct.unpack(">I", buf[8:12])[0]
    top = buf[12:16].decode("latin-1")
    if top == "DJVU":
        return DjvuStructure(top_form=top, bundled=None, dirm_files=None, pages=[_page_info(buf, 4, 1, None)],
                             shared_components=0, component_kinds={"DJVU": 1})
    comps: list[tuple[str, int]] = []
    dirm = None
    for cid, p, s in _chunks(buf, 16, 12 + top_size):
        if cid == "DIRM":
            dirm = (p, s)
        elif cid == "FORM":
            comps.append((buf[p + 8:p + 12].decode("latin-1"), p))
    bundled, nfiles = None, None
    if dirm:
        p, _ = dirm
        flags = buf[p + 8]
        nfiles = struct.unpack(">H", buf[p + 9:p + 11])[0]
        bundled = bool(flags & 0x80)
        if bundled:
            offs = struct.unpack(">" + "I" * nfiles, buf[p + 11:p + 11 + 4 * nfiles])
            comps = [(buf[o + 8:o + 12].decode("latin-1"), o) for o in offs]
    kinds: dict[str, int] = {}
    for k, _ in comps:
        kinds[k] = kinds.get(k, 0) + 1
    pages = []
    for k, o in comps:
        if k == "DJVU":
            pages.append(_page_info(buf, o, len(pages) + 1, None))
    return DjvuStructure(top_form=top, bundled=bundled, dirm_files=nfiles, pages=pages,
                         shared_components=kinds.get("DJVI", 0), component_kinds=kinds)


# ---------------------------------------------------------------------------------------------------- text layer
_TOKEN = re.compile(rb'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))', re.S)
_ESC = re.compile(rb"\\(?:([0-7]{1,3})|(.))", re.S)
_SIMPLE = {b"n": b"\n", b"t": b"\t", b"r": b"\r", b"b": b"\b", b"f": b"\f", b"v": b"\v", b"a": b"\a"}


def _unescape(raw: bytes) -> str:
    def rep(m: re.Match[bytes]) -> bytes:
        if m.group(1) is not None:
            return bytes([int(m.group(1), 8) & 0xFF])
        c = m.group(2)
        return _SIMPLE.get(c, c)

    return _ESC.sub(rep, raw).decode("utf-8", "replace")


def parse_sexpr(data: bytes) -> list[Any]:
    """Parse DjVu S-expressions: lists, atoms (bytes→str/int), C-escaped strings (UTF-8)."""
    stack: list[list[Any]] = [[]]
    pos = 0
    n = len(data)
    while pos < n:
        m = _TOKEN.match(data, pos)
        if not m or m.end() == pos:
            if data[pos:].strip():
                raise ValueError(f"S-expression parse error at byte {pos}")
            break
        pos = m.end()
        if m.group(1):
            stack.append([])
        elif m.group(2):
            if len(stack) == 1:
                raise ValueError("unbalanced ')'")
            done = stack.pop()
            stack[-1].append(done)
        elif m.group(3) is not None:
            stack[-1].append(("str", _unescape(m.group(3))))
        else:
            atom = m.group(4).decode("utf-8", "replace")
            try:
                stack[-1].append(int(atom))
            except ValueError:
                stack[-1].append(atom)
    if len(stack) != 1:
        raise ValueError("unbalanced '('")
    return stack[0]


@dataclass
class Zone:
    kind: str                      # page | column | region | para | line | word | char
    box_px: tuple[int, int, int, int]  # xmin, ymin, xmax, ymax (origin bottom-left)
    text: str | None = None
    children: list["Zone"] = field(default_factory=list)


def _zone(expr: list[Any]) -> Zone:
    kind = str(expr[0])
    box = tuple(int(v) for v in expr[1:5])
    z = Zone(kind=kind, box_px=box)  # type: ignore[arg-type]
    for item in expr[5:]:
        if isinstance(item, tuple) and item[0] == "str":
            z.text = item[1]
        elif isinstance(item, list) and item:
            z.children.append(_zone(item))
    return z


_PAGE_MARK = re.compile(rb'^select "([^"]*)" # page (\d+)\s*$', re.M)


def text_layer_all(path: Path, timeout: float = 600.0) -> dict[int, Zone]:
    """Text zones of every page that has a text layer, keyed by page number (one ``djvused`` process)."""
    out = subprocess.run([tool_path("djvused"), "-e", "output-txt", str(path)], capture_output=True, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError(f"djvused output-txt exit {out.returncode}: {out.stderr.decode('utf-8', 'replace')[:300]}")
    data = out.stdout
    marks = list(_PAGE_MARK.finditer(data))
    pages: dict[int, Zone] = {}
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(data)
        body = data[m.end():end]
        k = body.find(b"set-txt")
        if k < 0:
            continue
        body = body[k + len(b"set-txt"):]
        # the expression ends with a line holding a single '.'
        body = re.split(rb"^\.\s*$", body, maxsplit=1, flags=re.M)[0]
        exprs = parse_sexpr(body)
        if exprs and isinstance(exprs[0], list) and exprs[0] and exprs[0][0] == "page":
            pages[int(m.group(2))] = _zone(exprs[0])
    return pages


def zone_text(z: Zone) -> str:
    """Plain text of a zone: words joined by spaces, lines by LF, paragraphs/regions by blank lines."""
    if z.kind in ("word", "char"):
        return z.text or ""
    if not z.children:
        return z.text or ""
    parts = [zone_text(c) for c in z.children]
    if z.kind == "line":
        return " ".join(p for p in parts if p)
    sep = "\n" if z.kind in ("para",) else "\n"
    joined = sep.join(p for p in parts if p)
    return joined


def iter_zones(z: Zone, kind: str):
    if z.kind == kind:
        yield z
        return
    for c in z.children:
        yield from iter_zones(c, kind)


def px_to_pt(box_px: tuple[int, int, int, int], height_px: int, dpi: int) -> tuple[float, float, float, float]:
    """DjVu pixel box (origin bottom-left) → PAGE_PT_TL."""
    s = 72.0 / float(dpi)
    x0, y0, x1, y1 = box_px
    return (round(x0 * s, 3), round((height_px - y1) * s, 3), round(x1 * s, 3), round((height_px - y0) * s, 3))


def page_lines(z: Zone, height_px: int, dpi: int) -> list[dict[str, Any]]:
    """Lines of a page zone as dicts {bbox (PAGE_PT_TL), text, para, region} in layer order."""
    out = []

    def walk(node: Zone, para: int | None, region: int | None, counters: dict[str, int]) -> None:
        if node.kind in ("region", "column"):
            counters["region"] += 1
            region = counters["region"]
        if node.kind == "para":
            counters["para"] += 1
            para = counters["para"]
        if node.kind == "line" or (node.kind in ("word", "char") and node.text):
            text = zone_text(node)
            if text.strip():
                out.append({"bbox": px_to_pt(node.box_px, height_px, dpi), "text": text, "para": para,
                            "region": region})
            return
        for c in node.children:
            walk(c, para, region, counters)

    walk(z, None, None, {"region": 0, "para": 0})
    return out


def metadata(path: Path, timeout: float = 120.0) -> dict[str, Any]:
    meta = _run([tool_path("djvused"), "-e", "print-meta", str(path)], timeout)
    outline = _run([tool_path("djvused"), "-e", "print-outline", str(path)], timeout)
    return {"meta_raw": meta or None, "outline_raw": outline or None}
