"""Native PDF extraction with PyMuPDF 1.28.2 (CP-14); pypdfium2 is the independent second page counter.

Geometry: PyMuPDF reports text, images and paths in the *unrotated* page space relative to the CropBox origin;
multiplying by ``page.rotation_matrix`` gives PAGE_PT_TL (points, origin top-left of the displayed page, y down) –
verified on synthetic rotated/cropped pages. All bboxes leaving this module are PAGE_PT_TL.

Resource rules learnt in phase 0 (R6): never call ``get_image_info(xrefs=True)`` (it decodes every image – 22 s per
page on shared-JBIG2 scans); image hashing and stream extraction are a separate, budgeted step for figure regions.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vkm_corpus.extract.text_repair import FUNC_WORDS, WORD_RE, char_stats, remap_cp1251

GRID = 64
EXTRACTOR_ID = "pymupdf-native"


def pymupdf_version() -> str:
    import pymupdf

    return str(pymupdf.VersionBind)


def pdfium_version() -> str:
    import pypdfium2

    return str(getattr(pypdfium2, "V_PYPDFIUM2", None) or getattr(pypdfium2, "__version__", "?"))


def open_pdf(path: Path):
    """Open a PDF from memory: one sequential read of the file instead of MuPDF's many small random reads (which
    are very slow on network-like mounts such as the Windows drive seen from WSL)."""
    import pymupdf

    pymupdf.TOOLS.mupdf_warnings()  # reset the warning buffer
    return pymupdf.open(stream=Path(path).read_bytes(), filetype="pdf")


def count_pages_pdfium(path: Path) -> int:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(Path(path).read_bytes())
    try:
        return len(pdf)
    finally:
        pdf.close()


# ---------------------------------------------------------------------------------------------------- geometry
def _r(v: float) -> float:
    return round(float(v), 3)


def rot_bbox(bbox: Any, matrix: Any) -> tuple[float, float, float, float]:
    import pymupdf

    r = pymupdf.Rect(bbox) * matrix
    r.normalize()
    return (_r(r.x0), _r(r.y0), _r(r.x1), _r(r.y1))


def rot_point(p: Any, matrix: Any) -> list[float]:
    import pymupdf

    q = pymupdf.Point(p) * matrix
    return [_r(q.x), _r(q.y)]


def derotate_bbox(bbox: tuple[float, float, float, float], page: Any) -> Any:
    """PAGE_PT_TL → unrotated page coordinates (for ``clip=`` arguments)."""
    import pymupdf

    r = pymupdf.Rect(bbox) * page.derotation_matrix
    r.normalize()
    return r


def coverage(rects: list[tuple[float, float, float, float]], W: float, H: float) -> tuple[float, float]:
    """Union coverage (64x64 grid) and the largest single coverage of rectangles on a W x H page."""
    import numpy as np

    if not rects or W <= 0 or H <= 0:
        return 0.0, 0.0
    g = np.zeros((GRID, GRID), dtype=bool)
    xs = (np.arange(GRID) + 0.5) * W / GRID
    ys = (np.arange(GRID) + 0.5) * H / GRID
    mx = 0.0
    for (x0, y0, x1, y1) in rects:
        x0, x1 = max(0.0, min(x0, x1)), min(W, max(x0, x1))
        y0, y1 = max(0.0, min(y0, y1)), min(H, max(y0, y1))
        if x1 <= x0 or y1 <= y0:
            continue
        mx = max(mx, (x1 - x0) * (y1 - y0) / (W * H))
        g |= np.outer((ys >= y0) & (ys <= y1), (xs >= x0) & (xs <= x1))
    return float(g.mean()), float(mx)


# ---------------------------------------------------------------------------------------------------- document
def document_info(doc: Any, path: Path) -> dict[str, Any]:
    """Document-level native metadata (becomes the document NATIVE_RAW artifact)."""
    import pymupdf

    info: dict[str, Any] = {"schema": "vkm.native_raw.pdf_document/1", "page_count": doc.page_count,
                            "extractor": f"pymupdf {pymupdf_version()}"}
    try:
        info["page_count_pdfium"] = count_pages_pdfium(path)
        info["pdfium_version"] = pdfium_version()
    except Exception as exc:  # noqa: BLE001 - recorded, never silent
        info["page_count_pdfium"] = None
        info["pdfium_error"] = f"{type(exc).__name__}: {exc}"[:300]
    md = doc.metadata or {}
    info["metadata"] = {k: (v if v not in ("",) else None) for k, v in md.items()}
    info["is_repaired"] = bool(doc.is_repaired)
    info["is_encrypted"] = bool(doc.is_encrypted)
    info["needs_pass"] = bool(doc.needs_pass)
    try:
        info["permissions"] = int(doc.permissions)
    except Exception:  # noqa: BLE001
        info["permissions"] = None
    try:
        xmp = doc.get_xml_metadata() or ""
        info["xmp_bytes"] = len(xmp.encode("utf-8"))
        info["xmp_sha256"] = hashlib.sha256(xmp.encode("utf-8")).hexdigest() if xmp else None
        info["xmp"] = xmp if len(xmp) <= 200_000 else None
    except Exception:  # noqa: BLE001
        info["xmp_bytes"] = None
    try:
        info["toc"] = [[lvl, title, page] for lvl, title, page in doc.get_toc(simple=True)]
    except Exception:  # noqa: BLE001
        info["toc"] = None
    try:
        info["page_label_rules"] = doc.get_page_labels()
    except Exception:  # noqa: BLE001
        info["page_label_rules"] = None
    try:
        info["embedded_files"] = doc.embfile_count()
    except Exception:  # noqa: BLE001
        info["embedded_files"] = None
    info["warnings"] = (pymupdf.TOOLS.mupdf_warnings() or "")[:4000]
    return info


# ---------------------------------------------------------------------------------------------------- page
@dataclass
class NativePage:
    """Result of the native pass over one page (raw dict + derived features)."""

    raw: dict[str, Any]
    features: dict[str, Any]
    plain_text: str


def _font_table(doc: Any, cache: dict[int, tuple[bool, str, str]], xref: int) -> tuple[bool, str, str]:
    if xref not in cache:
        tu = doc.xref_get_key(xref, "ToUnicode")[0] != "null"
        sub = doc.xref_get_key(xref, "Subtype")[1]
        enc = doc.xref_get_key(xref, "Encoding")
        cache[xref] = (tu, sub, enc[1] if enc[0] != "null" else "")
    return cache[xref]


def extract_page(doc: Any, index0: int, font_cache: dict[int, tuple[bool, str, str]] | None = None,
                 with_drawings_count: bool = True) -> NativePage:
    """Native extraction of page ``index0`` (0-based): text dict, fonts, images (no hashing), path counts, labels."""
    import pymupdf

    font_cache = {} if font_cache is None else font_cache
    page = doc[index0]
    m = page.rotation_matrix
    rect = page.rect
    W, H = float(rect.width), float(rect.height)
    raw: dict[str, Any] = {
        "schema": "vkm.native_raw.pdf_page/1",
        "page_index": index0 + 1,
        "width_pt": _r(W), "height_pt": _r(H), "rotation": int(page.rotation),
        "mediabox": [_r(v) for v in page.mediabox], "cropbox": [_r(v) for v in page.cropbox],
        "rotation_matrix": [_r(v) for v in tuple(m)],
    }
    try:
        raw["label"] = page.get_label()
    except Exception:  # noqa: BLE001
        raw["label"] = None
    f: dict[str, Any] = {"page_index": index0 + 1}
    # -- glyph statistics (texttrace: render mode / opacity reveal hidden OCR layers)
    codes: list[int] = []
    invisible = 0
    for s in page.get_texttrace():
        hidden = s.get("type") == 3 or s.get("opacity") == 0
        for ch in s["chars"]:
            c = ch[0]
            if c in (0x20, 0x09, 0x0A, 0x0D, 0x0C, 0xA0) or c < 0:
                continue
            codes.append(c)
            if hidden:
                invisible += 1
    f.update(char_stats(codes))
    f["chars_invisible"] = invisible
    plain = page.get_text()  # default flags: byte-compatible with the Phase-1 text manifest (K-11)
    raw["plain_text_sha256"] = hashlib.sha256(plain.encode("utf-8")).hexdigest()
    words = WORD_RE.findall(plain)
    f["word_tokens"] = len(words)
    f["func_hits"] = sum(1 for w in words if w.lower() in FUNC_WORDS)
    if f["chars"] and f["latext"] / f["chars"] > 0.2:
        rt = remap_cp1251(plain)
        rw = WORD_RE.findall(rt)
        rn = sum(1 for c in rt if not c.isspace()) or 1
        f["remap_cyr_share"] = round(sum(1 for c in rt if "Ѐ" <= c <= "ӿ") / rn, 4)
        f["remap_words"] = len(rw)
        f["remap_func_rate"] = round(sum(1 for w in rw if w.lower() in FUNC_WORDS) / max(1, len(rw)), 4)
    # -- text dict (blocks → lines → spans) in PAGE_PT_TL
    flags = pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES
    d = page.get_text("dict", flags=flags)
    blocks = []
    for n, b in enumerate(d.get("blocks", [])):
        if b.get("type") != 0:
            continue
        lines = []
        for ln in b.get("lines", []):
            spans = [{"text": s.get("text", ""), "font": s.get("font"), "size": _r(s.get("size", 0)),
                      "flags": s.get("flags"), "color": s.get("color"), "alpha": s.get("alpha"),
                      "bbox": list(rot_bbox(s["bbox"], m)), "origin": rot_point(s.get("origin", (0, 0)), m)}
                     for s in ln.get("spans", [])]
            lines.append({"bbox": list(rot_bbox(ln["bbox"], m)), "wmode": ln.get("wmode", 0),
                          "dir": [_r(v) for v in ln.get("dir", (1, 0))], "spans": spans})
        blocks.append({"n": n, "bbox": list(rot_bbox(b["bbox"], m)), "lines": lines})
    raw["blocks"] = blocks
    # -- fonts
    fonts, n_no_tu, n_type3 = [], 0, 0
    for fx in page.get_fonts(full=True):
        xref = fx[0]
        entry = {"xref": xref, "ext": fx[1], "type": fx[2], "basefont": fx[3], "name": fx[4], "encoding": fx[5]}
        if xref:
            tu, sub, enc = _font_table(doc, font_cache, xref)
            entry["to_unicode"] = tu
            if sub == "/Type3":
                n_type3 += 1
            if not tu and (sub in ("/Type0", "/Type3") or enc in ("", "null")):
                n_no_tu += 1
        fonts.append(entry)
    raw["fonts"] = fonts
    f["n_fonts"] = len(fonts)
    f["n_fonts_nonstd_no_tounicode"] = n_no_tu
    f["n_type3_fonts"] = n_type3
    # -- images: geometry and parameters only (no decoding, no hashing)
    images = []
    for im in page.get_image_info():
        images.append({"number": im.get("number"), "bbox": list(rot_bbox(im["bbox"], m)),
                       "width": im.get("width"), "height": im.get("height"), "bpc": im.get("bpc"),
                       "colorspace": im.get("colorspace"), "cs_name": im.get("cs-name"),
                       "xres": im.get("xres"), "yres": im.get("yres"), "size": im.get("size")})
    raw["images"] = images
    rects = [tuple(im["bbox"]) for im in images]
    cov, mx = coverage(rects, W, H)
    f["n_images"] = len(images)
    f["img_cov"] = round(cov, 4)
    f["img_max"] = round(mx, 4)
    best = max(images, key=lambda im: (im["bbox"][2] - im["bbox"][0]) * (im["bbox"][3] - im["bbox"][1]),
               default=None)
    if best is not None:
        bw = max(1e-6, best["bbox"][2] - best["bbox"][0])
        f["img_main_dpi"] = round(float(best["width"] or 0) / (bw / 72.0), 1) if best.get("width") else None
        f["img_main_bpc"] = best.get("bpc")
    # -- vector paths: count only (full paths are a separate step for figure regions / vector pages)
    if with_drawings_count:
        try:
            cd = page.get_cdrawings()
            f["n_paths"] = len(cd)
            f["n_path_items"] = sum(len(x.get("items", ())) for x in cd)
            raw["path_rects"] = [list(rot_bbox(x["rect"], m)) for x in cd] if len(cd) <= 20000 else None
        except Exception as exc:  # noqa: BLE001
            f["n_paths"] = -1
            raw["drawings_error"] = f"{type(exc).__name__}: {exc}"[:200]
    try:
        raw["n_links"] = len(page.get_links())
        raw["n_annots"] = sum(1 for _ in page.annots())
    except Exception:  # noqa: BLE001
        pass
    w = pymupdf.TOOLS.mupdf_warnings()
    if w:
        raw["warnings"] = w[:2000]
    raw["features"] = f
    return NativePage(raw=raw, features=f, plain_text=plain)


def block_text(block: dict[str, Any]) -> str:
    """Text of a raw native block: spans joined within a line, lines joined with LF (as PyMuPDF plain text)."""
    return "\n".join("".join(s["text"] for s in ln["spans"]) for ln in block["lines"])


def text_in_bbox(raw: dict[str, Any], bbox: tuple[float, float, float, float], min_overlap: float = 0.5) -> str:
    """Native glyph text of spans whose box lies mostly inside ``bbox`` (formula ``native_glyph_text``)."""
    x0, y0, x1, y1 = bbox
    out_lines = []
    for b in raw.get("blocks", []):
        for ln in b["lines"]:
            parts = []
            for s in ln["spans"]:
                sx0, sy0, sx1, sy1 = s["bbox"]
                area = max(1e-6, (sx1 - sx0) * (sy1 - sy0))
                ix = max(0.0, min(x1, sx1) - max(x0, sx0))
                iy = max(0.0, min(y1, sy1) - max(y0, sy0))
                if ix * iy / area >= min_overlap:
                    parts.append(s["text"])
            if parts:
                out_lines.append("".join(parts))
    return "\n".join(out_lines)


# ---------------------------------------------------------------------------------------------------- vectors
def drawings_json(page: Any, clip: tuple[float, float, float, float] | None = None,
                  max_paths: int = 200_000) -> dict[str, Any]:
    """``get_drawings()`` serialised in PAGE_PT_TL (items l/c/re/qu with points; style; seqno; layer)."""
    m = page.rotation_matrix
    paths = []
    n_total = 0
    for dr in page.get_drawings():
        n_total += 1
        rect = rot_bbox(dr["rect"], m)
        if clip is not None and (rect[2] < clip[0] or rect[0] > clip[2] or rect[3] < clip[1] or rect[1] > clip[3]):
            continue
        items = []
        for it in dr.get("items", []):
            op = it[0]
            if op == "l":
                items.append(["l", rot_point(it[1], m), rot_point(it[2], m)])
            elif op == "c":
                items.append(["c", rot_point(it[1], m), rot_point(it[2], m), rot_point(it[3], m),
                              rot_point(it[4], m)])
            elif op == "re":
                items.append(["re", list(rot_bbox(it[1], m)), it[2] if len(it) > 2 else None])
            elif op == "qu":
                q = it[1]
                items.append(["qu", [rot_point(p, m) for p in (q.ul, q.ur, q.ll, q.lr)]])
        paths.append({"seqno": dr.get("seqno"), "type": dr.get("type"), "rect": list(rect), "items": items,
                      "fill": _color(dr.get("fill")), "color": _color(dr.get("color")),
                      "width": _num(dr.get("width")), "dashes": dr.get("dashes"),
                      "even_odd": dr.get("even_odd"), "close_path": dr.get("closePath"),
                      "fill_opacity": _num(dr.get("fill_opacity")), "stroke_opacity": _num(dr.get("stroke_opacity")),
                      "line_cap": _jsonable(dr.get("lineCap")), "line_join": _num(dr.get("lineJoin")),
                      "layer": dr.get("layer")})
        if len(paths) >= max_paths:
            break
    return {"schema": "vkm.vector_paths/1", "coordinate_space": "PAGE_SPACE", "bbox_space": "PAGE_PT_TL",
            "page_rotation": int(page.rotation), "rotation_matrix": [_r(v) for v in tuple(m)],
            "clip": list(clip) if clip else None, "n_paths_page": n_total, "n_paths": len(paths),
            "truncated": len(paths) >= max_paths, "paths": paths}


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) or math.isinf(x) else round(x, 4)


def _color(v: Any) -> list[float] | None:
    return None if v is None else [round(float(c), 4) for c in v]


def _jsonable(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, float):
        return _num(v)
    return v


def page_svg(page: Any) -> bytes:
    return page.get_svg_image(text_as_path=False).encode("utf-8")


# ---------------------------------------------------------------------------------------------------- images
def embedded_image_for(doc: Any, page: Any, raw_page: dict[str, Any], bbox: tuple[float, float, float, float],
                       min_cover: float = 0.8) -> dict[str, Any] | None:
    """If ``bbox`` is essentially one XObject image, return its stream (original bytes when possible).

    The xref is resolved by matching (width, height, bpc) of ``get_image_info`` against ``get_images(full=True)``
    – no image is decoded to find it. Ambiguous matches return None (flagged by the caller).
    """
    x0, y0, x1, y1 = bbox
    area = max(1e-6, (x1 - x0) * (y1 - y0))
    cands = []
    for im in raw_page.get("images", []):
        ix0, iy0, ix1, iy1 = im["bbox"]
        inter = max(0.0, min(x1, ix1) - max(x0, ix0)) * max(0.0, min(y1, iy1) - max(y0, iy0))
        iarea = max(1e-6, (ix1 - ix0) * (iy1 - iy0))
        if inter / iarea >= min_cover and inter / area >= 0.5:
            cands.append(im)
    if len(cands) != 1:
        return None
    im = cands[0]
    matches = [x for x in page.get_images(full=True)
               if x[2] == im.get("width") and x[3] == im.get("height") and x[4] == im.get("bpc")]
    xrefs = sorted({x[0] for x in matches})
    if len(xrefs) != 1:
        return {"ambiguous": True, "n_candidates": len(xrefs)}
    xref = xrefs[0]
    filt = next((x[8] for x in matches if x[0] == xref), None)
    ext = doc.extract_image(xref)
    if not ext or not ext.get("image"):
        return None
    data = ext["image"]
    try:
        raw_stream = doc.xref_stream_raw(xref)
    except Exception:  # noqa: BLE001
        raw_stream = None
    return {"xref": xref, "bytes": data, "ext": ext.get("ext"), "width": ext.get("width"),
            "height": ext.get("height"), "original_filter": filt or None,
            "transcoded": raw_stream is None or raw_stream != data, "smask": ext.get("smask")}
