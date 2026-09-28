"""DOCX via python-docx + raw WordprocessingML (lxml).

* blocks: body paragraphs in order; the stable anchor is ``docx_paragraph_path`` (XPath of the ``w:p``), the page is a
  DERIVATION through the pinned LibreOffice render (``render_page_id``, status RENDER_DEPENDENT; H-50);
* formulas: every ``m:oMath`` (121 in VKM-SRC-023) is kept as raw OMML (``raw_format = OMML``); no LaTeX is invented
  – ``normalized_latex`` stays NULL until a pinned converter is decided;
* tables: body ``w:tbl`` with the raw XML and a cell grid (gridSpan / vMerge);
* images: ``a:blip r:embed`` → media bytes as they are (EMF/PNG/JPEG/GIF), not transcoded.
"""
from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EXTRACTOR_ID = "docx-xml"
NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "m": "http://schemas.openxmlformats.org/officeDocument/2006/math",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "v": "urn:schemas-microsoft-com:vml",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}
W = "{%s}" % NS["w"]
M = "{%s}" % NS["m"]
MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
               ".emf": "image/emf", ".wmf": "image/wmf", ".tif": "image/tiff", ".tiff": "image/tiff",
               ".bmp": "image/bmp", ".svg": "image/svg+xml"}


@dataclass
class DocxParagraph:
    order: int
    path: str
    style: str | None
    block_type: str
    text: str
    n_math: int
    has_page_break_before: bool
    last_rendered_page_breaks: int


@dataclass
class DocxMath:
    order: int
    paragraph_path: str
    display: bool
    omml: str
    linear_text: str


@dataclass
class DocxTable:
    order: int
    path: str
    xml: str
    cells: list[dict[str, Any]]
    n_rows: int
    n_cols: int
    text: str


@dataclass
class DocxImage:
    order: int
    paragraph_path: str
    rel_id: str
    member: str
    media_type: str
    descr: str | None


@dataclass
class DocxDoc:
    paragraphs: list[DocxParagraph] = field(default_factory=list)
    maths: list[DocxMath] = field(default_factory=list)
    tables: list[DocxTable] = field(default_factory=list)
    images: list[DocxImage] = field(default_factory=list)
    app_properties: dict[str, str | None] = field(default_factory=dict)
    core_properties: dict[str, str | None] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)


def _style_block_type(style: str | None, has_num: bool) -> str:
    s = (style or "").lower()
    if s.startswith(("heading", "заголовок", "title", "название")) and "caption" not in s:
        return "HEADING"
    if "caption" in s or "название объекта" in s or "подпись" in s:
        return "CAPTION"
    if s.startswith(("toc", "оглавление")) or "содержание" in s:
        return "TABLE_OF_CONTENTS"
    if "footnote" in s or "сноск" in s:
        return "FOOTNOTE"
    if has_num or "list" in s or "спис" in s:
        return "LIST_ITEM"
    return "TEXT"


def _para_text(p: Any) -> str:
    """Visible run text of a paragraph (w:t, tabs, breaks), excluding math and deleted text."""
    parts = []
    for el in p.iter():
        tag = el.tag
        if tag == W + "t":
            anc_del = any(a.tag in (W + "del", M + "oMath") for a in el.iterancestors())
            if not anc_del:
                parts.append(el.text or "")
        elif tag == W + "tab":
            parts.append("\t")
        elif tag in (W + "br", W + "cr"):
            parts.append("\n")
    return "".join(parts)


def _math_linear(m: Any) -> str:
    return "".join(t.text or "" for t in m.iter(M + "t"))


def _grid(tbl: Any) -> tuple[list[dict[str, Any]], int, int]:
    """Cells of a w:tbl with row/col spans (gridSpan, vMerge restart/continue)."""
    rows = [tr for tr in tbl if tr.tag == W + "tr"]
    cells: list[dict[str, Any]] = []
    open_vmerge: dict[int, dict[str, Any]] = {}
    ncols = 0
    for r, tr in enumerate(rows):
        c = 0
        for tc in tr:
            if tc.tag != W + "tc":
                continue
            pr = tc.find(W + "tcPr")
            span = 1
            vmerge = None
            if pr is not None:
                gs = pr.find(W + "gridSpan")
                if gs is not None:
                    span = int(gs.get(W + "val", "1"))
                vm = pr.find(W + "vMerge")
                if vm is not None:
                    vmerge = vm.get(W + "val", "continue")
            text = "\n".join(_para_text(p) for p in tc.iter(W + "p")).strip()
            if vmerge == "continue" and c in open_vmerge:
                open_vmerge[c]["row_span"] += 1
            else:
                cell = {"row": r, "col": c, "row_span": 1, "col_span": span, "is_header": r == 0, "text": text}
                cells.append(cell)
                if vmerge == "restart":
                    open_vmerge[c] = cell
                else:
                    open_vmerge.pop(c, None)
            c += span
        ncols = max(ncols, c)
    return cells, len(rows), ncols


def read_docx(path: Path) -> DocxDoc:
    from lxml import etree

    doc = DocxDoc()
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        root = etree.fromstring(z.read("word/document.xml"))
        tree = root.getroottree()
        rels: dict[str, str] = {}
        if "word/_rels/document.xml.rels" in names:
            rr = etree.fromstring(z.read("word/_rels/document.xml.rels"))
            for rel in rr:
                target = rel.get("Target") or ""
                rels[rel.get("Id")] = target if target.startswith("/") else "word/" + target
        styles: dict[str, str] = {}
        if "word/styles.xml" in names:
            st = etree.fromstring(z.read("word/styles.xml"))
            for s in st.iter(W + "style"):
                name = s.find(W + "name")
                styles[s.get(W + "styleId")] = name.get(W + "val") if name is not None else s.get(W + "styleId")
        for part, target in (("docProps/app.xml", doc.app_properties), ("docProps/core.xml", doc.core_properties)):
            if part in names:
                x = etree.fromstring(z.read(part))
                for el in x:
                    target[etree.QName(el).localname] = (el.text or "").strip() or None
        body = root.find(W + "body")
        order = 0
        for child in body:
            if child.tag == W + "p":
                order += 1
                p = child
                ppr = p.find(W + "pPr")
                style_id = None
                has_num = False
                pb_before = False
                if ppr is not None:
                    ps = ppr.find(W + "pStyle")
                    style_id = ps.get(W + "val") if ps is not None else None
                    has_num = ppr.find(W + "numPr") is not None
                    pb_before = ppr.find(W + "pageBreakBefore") is not None
                style = styles.get(style_id, style_id) if style_id else None
                maths = list(p.iter(M + "oMath"))
                ppath = tree.getpath(p)
                doc.paragraphs.append(DocxParagraph(
                    order=order, path=ppath, style=style, block_type=_style_block_type(style, has_num),
                    text=_para_text(p), n_math=len(maths), has_page_break_before=pb_before,
                    last_rendered_page_breaks=sum(1 for _ in p.iter(W + "lastRenderedPageBreak"))))
                for mth in maths:
                    doc.maths.append(DocxMath(order=len(doc.maths) + 1, paragraph_path=ppath,
                                              display=any(a.tag == M + "oMathPara" for a in mth.iterancestors()),
                                              omml=etree.tostring(mth, encoding="unicode"),
                                              linear_text=_math_linear(mth)))
                for blip in p.iter("{%s}blip" % NS["a"]):
                    rid = blip.get("{%s}embed" % NS["r"])
                    if not rid or rid not in rels:
                        continue
                    member = rels[rid]
                    ext = Path(member).suffix.lower()
                    descr = None
                    for anc in blip.iterancestors():
                        dp = anc.find("{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}docPr")
                        if dp is not None:
                            descr = dp.get("descr") or dp.get("title")
                            break
                    doc.images.append(DocxImage(order=len(doc.images) + 1, paragraph_path=ppath, rel_id=rid,
                                                member=member, media_type=MEDIA_TYPES.get(ext,
                                                                                          "application/octet-stream"),
                                                descr=descr))
            elif child.tag == W + "tbl":
                order += 1
                cells, nr, nc = _grid(child)
                # maths inside tables belong to the table cell paragraphs
                for mth in child.iter(M + "oMath"):
                    par = next((a for a in mth.iterancestors() if a.tag == W + "p"), None)
                    doc.maths.append(DocxMath(order=len(doc.maths) + 1,
                                              paragraph_path=tree.getpath(par) if par is not None else
                                              tree.getpath(child),
                                              display=any(a.tag == M + "oMathPara" for a in mth.iterancestors()),
                                              omml=etree.tostring(mth, encoding="unicode"),
                                              linear_text=_math_linear(mth)))
                doc.tables.append(DocxTable(order=order, path=tree.getpath(child),
                                            xml=etree.tostring(child, encoding="unicode"), cells=cells, n_rows=nr,
                                            n_cols=nc, text="\n".join(c["text"] for c in cells if c["text"])))
        docxml = z.read("word/document.xml")
        doc.counts = {
            "omath": len(root.findall(".//" + M + "oMath")),
            "omath_para": len(root.findall(".//" + M + "oMathPara")),
            "tables_body": len(doc.tables),
            "tables_all": len(root.findall(".//" + W + "tbl")),
            "paragraphs_body": len(doc.paragraphs),
            "images": len(doc.images),
            "media_files": sum(1 for n in names if n.startswith("word/media/")),
            "last_rendered_page_breaks": docxml.count(b"<w:lastRenderedPageBreak/>"),
            "explicit_page_breaks": len(re.findall(rb'<w:br [^>]*w:type="page"', docxml)),
        }
    return doc


def read_member(path: Path, member: str) -> bytes:
    with zipfile.ZipFile(path) as z:
        return z.read(member)


# ---------------------------------------------------------------------------------------------------- page alignment
def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s).lower().replace("ё", "е")


def align_to_pages(texts: list[str], page_texts: list[str], probe: int = 40) -> list[tuple[int | None, float]]:
    """Assign each block text to a page of the pinned render by a sequential search of its first ``probe``
    non-space characters in the concatenated page text (DERIVATION; returns (page_index or None, score))."""
    norm_pages = [_norm(t) for t in page_texts]
    starts, pos = [], 0
    for t in norm_pages:
        starts.append(pos)
        pos += len(t)
    full = "".join(norm_pages)

    def page_of(offset: int) -> int:
        lo, hi = 0, len(starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if starts[mid] <= offset:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    out: list[tuple[int | None, float]] = []
    cursor = 0
    for t in texts:
        key = _norm(t)[:probe]
        if len(key) < 4:
            out.append((None, 0.0))
            continue
        k = full.find(key, cursor)
        if k < 0:
            k2 = full.find(key)
            if k2 < 0:
                short = key[: max(8, probe // 3)]
                k3 = full.find(short, cursor)
                out.append((page_of(k3), 0.5) if k3 >= 0 else (None, 0.0))
                continue
            out.append((page_of(k2), 0.7))
            continue
        out.append((page_of(k), 1.0))
        cursor = k + len(key)
    return out
