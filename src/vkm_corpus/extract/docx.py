"""DOCX via python-docx + raw WordprocessingML (lxml).

* blocks: body, textboxes and auxiliary Word parts; ``docx_paragraph_path`` is a part-qualified XPath; the page is a
  DERIVATION through the pinned LibreOffice render (``render_page_id``, status RENDER_DEPENDENT; H-50);
* formulas: every ``m:oMath`` (121 in VKM-SRC-023) is kept as raw OMML (``raw_format = OMML``); no LaTeX is invented
  – ``normalized_latex`` stays NULL until a pinned converter is decided;
* tables: every ``w:tbl``, including nested tables, with raw XML and gridSpan / vMerge provenance;
* images: ``a:blip r:embed`` → media bytes as they are (EMF/PNG/JPEG/GIF), not transcoded.
"""
from __future__ import annotations

import re
import base64
import posixpath
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
    part: str = "word/document.xml"
    container: str = "BODY"


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
    raw_parts: dict[str, str] = field(default_factory=dict)
    raw_parts_base64: dict[str, str] = field(default_factory=dict)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    package_manifest: list[dict[str, Any]] = field(default_factory=list)


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
        # A text box contains its own paragraphs; they are extracted separately.
        if next((a for a in el.iterancestors() if a.tag == W + "p"), p) is not p:
            continue
        if any(a.tag in (W + "del", M + "oMath") for a in el.iterancestors()):
            continue
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
        row_pr = tr.find(W + "trPr")
        before = row_pr.find(W + "gridBefore") if row_pr is not None else None
        after = row_pr.find(W + "gridAfter") if row_pr is not None else None
        header = row_pr.find(W + "tblHeader") if row_pr is not None else None
        c = max(0, int(before.get(W + "val", "0"))) if before is not None else 0
        is_header = header is not None and header.get(W + "val", "1").lower() not in ("0", "false", "off")
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
            text = "\n".join(_para_text(p) for p in tc.iter(W + "p")
                             if next((a for a in p.iterancestors() if a.tag == W + "tbl"), None) is tbl
                             and not any(a.tag == W + "txbxContent" for a in p.iterancestors())).strip()
            cell_path = tbl.getroottree().getpath(tc)
            if vmerge == "continue" and c in open_vmerge and open_vmerge[c]["col_span"] == span:
                open_vmerge[c]["row_span"] += 1
                open_vmerge[c]["merge_fragments"].append({"row": r, "raw_locator": cell_path, "text": text})
                if text:
                    open_vmerge[c]["text"] += "\n" + text
            else:
                cell = {"row": r, "col": c, "row_span": 1, "col_span": span, "is_header": is_header, "text": text,
                        "raw_locator": cell_path, "merge_fragments": [],
                        "structural_diagnostics": ["ORPHAN_VERTICAL_MERGE"] if vmerge == "continue" else []}
                cells.append(cell)
                if vmerge == "restart":
                    open_vmerge[c] = cell
                else:
                    open_vmerge.pop(c, None)
            c += span
        ncols = max(ncols, c + (max(0, int(after.get(W + "val", "0"))) if after is not None else 0))
    return cells, len(rows), ncols


def read_docx(path: Path) -> DocxDoc:
    """Read all Word text-bearing parts, keeping exact XML and part-qualified locators.

    Table-cell paragraphs are represented by their tables; their formulas/images
    are still separate objects. Text boxes, notes, headers and footers are blocks.
    Deleted content and unsupported markup remain in raw_parts with diagnostics.
    """
    from lxml import etree

    doc = DocxDoc()
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        styles = {}
        if 'word/styles.xml' in names:
            for style in etree.fromstring(z.read('word/styles.xml'), parser).iter(W + 'style'):
                name = style.find(W + 'name')
                styles[style.get(W + 'styleId')] = name.get(W + 'val') if name is not None else style.get(W + 'styleId')
        for part, target in [('docProps/app.xml', doc.app_properties), ('docProps/core.xml', doc.core_properties)]:
            if part in names:
                for el in etree.fromstring(z.read(part), parser):
                    target[etree.QName(el).localname] = (el.text or '').strip() or None
        parts = ['word/document.xml'] + sorted(n for n in names if re.fullmatch(
            r'word/(?:footnotes|endnotes|comments|header[0-9]*|footer[0-9]*)\.xml', n))
        counts = {'omath': 0, 'omath_para': 0, 'tables_body': 0, 'tables_all': 0,
                  'paragraphs_body': 0, 'paragraphs_all': 0, 'last_rendered_page_breaks': 0,
                  'explicit_page_breaks': 0}
        for part in parts:
            raw = z.read(part)
            root = etree.fromstring(raw, parser)
            tree = root.getroottree()
            # Exact original bytes remain available even for UTF-16 XML declarations.
            doc.raw_parts_base64[part] = base64.b64encode(raw).decode('ascii')
            doc.raw_parts[part] = etree.tostring(root, encoding='unicode')
            def locator(el):
                xpath = tree.getpath(el)
                return xpath if part == 'word/document.xml' else part + '#' + xpath
            rels = {}
            relpart = posixpath.join(posixpath.dirname(part), '_rels', posixpath.basename(part) + '.rels')
            if relpart in names:
                for rel in etree.fromstring(z.read(relpart), parser):
                    target = rel.get('Target') or ''
                    if rel.get('TargetMode') == 'External':
                        doc.diagnostics.append({'part': part, 'code': 'EXTERNAL_RELATIONSHIP_NOT_FETCHED',
                                                'relationship_id': rel.get('Id')})
                        continue
                    member = posixpath.normpath(posixpath.join(posixpath.dirname(part), target))
                    if target.startswith('/'):
                        member = target.lstrip('/')
                    if member.startswith('../') or member not in names:
                        doc.diagnostics.append({'part': part, 'code': 'UNRESOLVED_RELATIONSHIP',
                                                'relationship_id': rel.get('Id')})
                        continue
                    rels[rel.get('Id')] = member
            for el in root.iter():
                if not isinstance(el.tag, str):
                    continue
                deleted = any(a.tag == W + 'del' for a in el.iterancestors())
                if el.tag in (W + 'del', W + 'altChunk', W + 'object', W + 'fldSimple', W + 'instrText'):
                    doc.diagnostics.append({'part': part, 'locator': locator(el),
                                            'code': 'RAW_ONLY_' + etree.QName(el).localname.upper()})
                if deleted:
                    continue
                if el.tag == W + 'p':
                    counts['paragraphs_all'] += 1
                    parent_table = next((a for a in el.iterancestors() if a.tag == W + 'tbl'), None)
                    textbox = any(a.tag == W + 'txbxContent' for a in el.iterancestors())
                    if parent_table is not None and not textbox:
                        continue
                    pr = el.find(W + 'pPr')
                    sid = pr.find(W + 'pStyle') if pr is not None else None
                    style_id = sid.get(W + 'val') if sid is not None else None
                    style = styles.get(style_id, style_id)
                    container = 'TEXTBOX' if textbox else 'BODY' if part == 'word/document.xml' else posixpath.basename(part).split('.')[0].upper()
                    kind = 'FOOTNOTE' if container in ('FOOTNOTES', 'ENDNOTES') else _style_block_type(style, pr is not None and pr.find(W + 'numPr') is not None)
                    maths = [m for m in el.iter(M + 'oMath') if next((a for a in m.iterancestors() if a.tag == W + 'p'), None) is el]
                    doc.paragraphs.append(DocxParagraph(len(doc.paragraphs) + 1, locator(el), style, kind,
                        _para_text(el), len(maths), pr is not None and pr.find(W + 'pageBreakBefore') is not None,
                        sum(1 for _ in el.iter(W + 'lastRenderedPageBreak')), part, container))
                    if container == 'BODY':
                        counts['paragraphs_body'] += 1
                elif el.tag == W + 'tbl':
                    cells, nr, nc = _grid(el)
                    doc.tables.append(DocxTable(len(doc.tables) + 1, locator(el),
                        etree.tostring(el, encoding='unicode'), cells, nr, nc,
                        '\n'.join(c['text'] for c in cells if c['text'])))
                    counts['tables_all'] += 1
                    if part == 'word/document.xml' and el.getparent().tag == W + 'body':
                        counts['tables_body'] += 1
                elif el.tag == M + 'oMath':
                    par = next((a for a in el.iterancestors() if a.tag == W + 'p'), el)
                    display = any(a.tag == M + 'oMathPara' for a in el.iterancestors())
                    doc.maths.append(DocxMath(len(doc.maths) + 1, locator(par), display,
                        etree.tostring(el, encoding='unicode'), _math_linear(el)))
                    counts['omath'] += 1
                elif el.tag == M + 'oMathPara':
                    counts['omath_para'] += 1
                elif el.tag in ('{%s}blip' % NS['a'], '{%s}imagedata' % NS['v']):
                    rid = el.get('{%s}embed' % NS['r']) or el.get('{%s}id' % NS['r'])
                    if rid not in rels:
                        doc.diagnostics.append({'part': part, 'locator': locator(el), 'code': 'UNRESOLVED_IMAGE', 'relationship_id': rid})
                        continue
                    par = next((a for a in el.iterancestors() if a.tag == W + 'p'), el)
                    member = rels[rid]
                    drawing = next((a for a in el.iterancestors() if a.tag == W + 'drawing'), None)
                    props = drawing.xpath('.//*[local-name()="docPr"]') if drawing is not None else []
                    descr = next((p.get('descr') or p.get('title') for p in props if p.get('descr') or p.get('title')), None)
                    doc.images.append(DocxImage(len(doc.images) + 1, locator(par), rid, member,
                        MEDIA_TYPES.get(Path(member).suffix.lower(), 'application/octet-stream'), descr or el.get('title')))
                elif el.tag == W + 'lastRenderedPageBreak':
                    counts['last_rendered_page_breaks'] += 1
                elif el.tag == W + 'br' and el.get(W + 'type') == 'page':
                    counts['explicit_page_breaks'] += 1
        counts.update(images=len(doc.images), media_files=sum(n.startswith('word/media/') for n in names))
        doc.counts = counts
        referenced_images = {im.member for im in doc.images}
        doc.package_manifest = [{"member": info.filename, "size_bytes": info.file_size, "crc32": info.CRC,
            "disposition": "STRUCTURED_AND_RAW" if info.filename in doc.raw_parts else
                           "EMBEDDED_REFERENCED" if info.filename in referenced_images else "RAW_ONLY"}
            for info in sorted(z.infolist(), key=lambda x: x.filename) if not info.is_dir()]
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
