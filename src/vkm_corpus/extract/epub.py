"""EPUB via zipfile + lxml (EbookLib is AGPL and not used).

Page unit = spine item (``VKM-SRC-249:sNNNN``); there is no page geometry (``bbox_space = NONE``). Printed pages come
from anchors in priority order: nav ``page-list`` → NCX ``pageList`` → ``epub:type="pagebreak"`` → the calibre
convention ``id="page_N"``. Blocks are block-level XHTML elements in document order (locator: spine idref + XPath +
element id). GIF images without ``alt`` are formula candidates (OCR ``Formula Recognition:``); other images are
figures whose embedded bytes are kept as they are.
"""
from __future__ import annotations

import posixpath
import base64
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EXTRACTOR_ID = "epub-xhtml"
BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "pre", "blockquote", "dt", "dd", "caption",
              "figcaption", "td", "th", "div", "section", "aside", "header", "footer", "address"}
LEAF_BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "pre", "dt", "dd", "caption", "figcaption",
                   "address"}
HEADING = {"h1", "h2", "h3", "h4", "h5", "h6"}
PAGE_ANCHOR = re.compile(r"^page[_-]?(\w+)$", re.I)
EQN_ID = re.compile(r"^EQN", re.I)
EQ_LABEL = re.compile(r"\((\d+(?:\.\d+)*[a-z]?)\)\s*$")


def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


@dataclass
class EpubImage:
    href: str                 # zip member path
    media_type: str | None
    alt: str | None
    is_formula_candidate: bool
    inline: bool
    element_id: str | None
    xpath: str
    order: int
    equation_label: str | None = None


@dataclass
class EpubBlock:
    tag: str
    text: str
    xpath: str
    element_id: str | None
    order: int
    block_type: str


@dataclass
class EpubTable:
    xpath: str
    element_id: str | None
    html: str
    order: int


@dataclass
class EpubMath:
    xpath: str
    element_id: str | None
    markup: str
    linear_text: str
    display: bool
    order: int


@dataclass
class SpineUnit:
    index: int
    idref: str
    href: str
    linear: bool
    media_type: str | None
    blocks: list[EpubBlock] = field(default_factory=list)
    images: list[EpubImage] = field(default_factory=list)
    tables: list[EpubTable] = field(default_factory=list)
    page_anchors: list[tuple[str, str]] = field(default_factory=list)  # (label, anchor origin)
    bib_ids: list[str] = field(default_factory=list)
    parse_error: str | None = None
    maths: list[EpubMath] = field(default_factory=list)
    raw_markup: str | None = None
    raw_markup_base64: str | None = None
    diagnostics: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class EpubDoc:
    opf_path: str
    version: str | None
    metadata: dict[str, list[str]]
    spine: list[SpineUnit]
    page_list_source: str      # NAV_PAGE_LIST | NCX_PAGE_LIST | EPUB_PAGEBREAK | CALIBRE_PAGE_ID | NONE
    n_page_anchors: int
    manifest_media_types: dict[str, int]
    package_manifest: list[dict[str, Any]] = field(default_factory=list)


def _xml(data: bytes):
    from lxml import etree

    try:
        return etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True))
    except etree.XMLSyntaxError:
        return etree.fromstring(data, etree.HTMLParser(recover=True, no_network=True))


def read_structure(z: zipfile.ZipFile) -> tuple[str, Any, dict[str, tuple[str, str | None, str]], list[tuple[str, bool]]]:
    container = _xml(z.read("META-INF/container.xml"))
    opf_path = container.xpath("//*[local-name()='rootfile']/@full-path")[0]
    base = posixpath.dirname(opf_path)
    opf = _xml(z.read(opf_path))
    manifest: dict[str, tuple[str, str | None, str]] = {}
    for it in opf.xpath("//*[local-name()='manifest']/*[local-name()='item']"):
        href = posixpath.normpath(posixpath.join(base, it.get("href") or ""))
        manifest[it.get("id")] = (href, it.get("media-type"), it.get("properties") or "")
    spine = [(ir.get("idref"), (ir.get("linear") or "yes") != "no")
             for ir in opf.xpath("//*[local-name()='spine']/*[local-name()='itemref']")]
    return opf_path, opf, manifest, spine


def count_spine_stdlib(path: Path) -> int:
    """Second, independent spine count with the standard-library XML parser (page-loss guard)."""
    with zipfile.ZipFile(path) as z:
        cont = ET.fromstring(z.read("META-INF/container.xml"))
        opf_path = next(el.get("full-path") for el in cont.iter() if el.tag.endswith("rootfile"))
        opf = ET.fromstring(z.read(opf_path))
        return sum(1 for el in opf.iter() if el.tag.endswith("itemref"))


def _text_of(el: Any) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def _own_text(el: Any) -> str:
    """Text of ``el`` excluding descendants that are themselves block elements."""
    parts: list[str] = [el.text or ""]
    for ch in el:
        if _local(ch.tag) in BLOCK_TAGS or _local(ch.tag) == "table":
            parts.append(ch.tail or "")
        else:
            parts.append("".join(ch.itertext()))
            parts.append(ch.tail or "")
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def parse_unit(z: zipfile.ZipFile, unit: SpineUnit, manifest_by_href: dict[str, str | None]) -> None:
    from lxml import etree

    try:
        raw = z.read(unit.href)
        unit.raw_markup_base64 = base64.b64encode(raw).decode("ascii")
        unit.raw_markup = raw.decode("utf-8-sig", errors="replace")
        root = _xml(raw)
        if "\ufffd" in unit.raw_markup:
            unit.diagnostics.append({"code": "MARKUP_DECODE_REPLACEMENT", "member": unit.href})
    except Exception as exc:  # noqa: BLE001 - recorded on the unit
        unit.parse_error = f"{type(exc).__name__}: {exc}"[:300]
        return
    tree = root.getroottree()
    base = posixpath.dirname(unit.href)
    order = 0
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        tag = _local(el.tag)
        el_id = el.get("id")
        if el_id:
            m = PAGE_ANCHOR.match(el_id)
            if m:
                unit.page_anchors.append((m.group(1), "CALIBRE_PAGE_ID"))
            if el_id.startswith("BIBe"):
                unit.bib_ids.append(el_id)
        et = el.get("{http://www.idpf.org/2007/ops}type") or ""
        if "pagebreak" in et.split():
            label = el.get("title") or el.get("aria-label") or _text_of(el) or (el_id or "")
            unit.page_anchors.append((label, "EPUB_PAGEBREAK"))
        if tag == "math":
            order += 1
            unit.maths.append(EpubMath(tree.getpath(el), el_id, etree.tostring(el, encoding="unicode"),
                                       _text_of(el), el.get("display") == "block", order))
            continue
        if tag in ("svg", "object", "iframe"):
            unit.diagnostics.append({"code": "RAW_ONLY_" + tag.upper(), "xpath": tree.getpath(el)})
        if tag == "table":
            order += 1
            unit.tables.append(EpubTable(xpath=tree.getpath(el), element_id=el_id,
                                         html=etree.tostring(el, encoding="unicode"), order=order))
            continue
        if tag in ("img", "image"):
            src = el.get("src") or el.get("{http://www.w3.org/1999/xlink}href") or ""
            href = posixpath.normpath(posixpath.join(base, src)) if src else ""
            media = manifest_by_href.get(href)
            alt = el.get("alt")
            is_gif = (media == "image/gif") or href.lower().endswith(".gif")
            parent = el.getparent()
            block = next((a for a in el.iterancestors() if _local(a.tag) in BLOCK_TAGS), None)
            block_text = _text_of(block) if block is not None else ""
            m2 = EQ_LABEL.search(block_text)
            eq_label = f"({m2.group(1)})" if (m2 and is_gif) else None
            rest = EQ_LABEL.sub("", block_text).strip() if block_text else ""
            # inline: the image sits in running text; display: the block holds only the image (and its number)
            inline = len(rest) > 3
            anc_ids = [a.get("id") for a in el.iterancestors() if a.get("id")]
            eq_anchor = next((i for i in anc_ids if EQN_ID.match(i)), None)
            order += 1
            unit.images.append(EpubImage(href=href, media_type=media, alt=alt,
                                         is_formula_candidate=is_gif and not (alt or "").strip(),
                                         inline=inline, element_id=el_id or eq_anchor, xpath=tree.getpath(el),
                                         order=order, equation_label=eq_label))
            continue
        if tag in BLOCK_TAGS:
            inside_table = any(_local(a.tag) == "table" for a in el.iterancestors())
            if inside_table:
                continue
            has_block_child = any(_local(d.tag) in BLOCK_TAGS or _local(d.tag) == "table"
                                  for d in el.iterdescendants() if isinstance(d.tag, str))
            text = _own_text(el) if has_block_child else _text_of(el)
            if not text:
                continue
            if has_block_child and tag not in LEAF_BLOCK_TAGS and len(text) < 2:
                continue
            order += 1
            btype = "HEADING" if tag in HEADING else "LIST_ITEM" if tag == "li" else \
                "CAPTION" if tag in ("caption", "figcaption") else "TEXT"
            if "footnote" in et.split() or "endnote" in et.split():
                btype = "FOOTNOTE"
            if el_id and el_id.startswith("BIBe"):
                btype = "REFERENCE_LIST"
            unit.blocks.append(EpubBlock(tag=tag, text=text, xpath=tree.getpath(el), element_id=el_id, order=order,
                                         block_type=btype))


def _page_list_nav(z: zipfile.ZipFile, manifest: dict[str, tuple[str, str | None, str]]) -> list[tuple[str, str]]:
    navs = [v for v in manifest.values() if "nav" in v[2].split()]
    if not navs:
        return []
    nd = _xml(z.read(navs[0][0]))
    out = []
    for a in nd.xpath("//*[local-name()='nav'][@*[local-name()='type']='page-list']//*[local-name()='a']"):
        out.append((_text_of(a), a.get("href") or ""))
    return out


def _page_list_ncx(z: zipfile.ZipFile, manifest: dict[str, tuple[str, str | None, str]]) -> list[tuple[str, str]]:
    ncx = [v for v in manifest.values() if v[1] == "application/x-dtbncx+xml"]
    if not ncx:
        return []
    nc = _xml(z.read(ncx[0][0]))
    out = []
    for pt in nc.xpath("//*[local-name()='pageTarget']"):
        label = " ".join(t.strip() for t in pt.xpath(".//*[local-name()='text']/text()"))
        src = (pt.xpath(".//*[local-name()='content']/@src") or [""])[0]
        out.append((label, src))
    return out


def read_epub(path: Path) -> EpubDoc:
    with zipfile.ZipFile(path) as z:
        opf_path, opf, manifest, spine = read_structure(z)
        md: dict[str, list[str]] = {}
        for tag in ("title", "creator", "language", "date", "publisher", "identifier", "subject", "description"):
            vals = [_text_of(e) for e in opf.xpath(f"//*[local-name()='metadata']/*[local-name()='{tag}']")]
            md[tag] = [v for v in vals if v]
        by_href = {v[0]: v[1] for v in manifest.values()}
        mt: dict[str, int] = {}
        for _, media, _ in manifest.values():
            mt[media or "?"] = mt.get(media or "?", 0) + 1
        units = []
        for n, (idref, linear) in enumerate(spine, 1):
            href, media, _ = manifest.get(idref, ("", None, ""))
            unit = SpineUnit(index=n, idref=idref, href=href, linear=linear, media_type=media)
            if href:
                parse_unit(z, unit, by_href)
            else:
                unit.parse_error = "spine idref not in manifest"
            units.append(unit)
        # printed pagination source, by priority; anchors found in the units are re-assigned when a list exists
        nav = _page_list_nav(z, manifest)
        ncx = _page_list_ncx(z, manifest) if not nav else []
        source = "NONE"
        listed = nav or ncx
        if listed:
            source = "NAV_PAGE_LIST" if nav else "NCX_PAGE_LIST"
            by_file: dict[str, list[str]] = {}
            opf_base = posixpath.dirname(opf_path)
            for label, href in listed:
                f = posixpath.normpath(posixpath.join(opf_base, href.split("#", 1)[0]))
                by_file.setdefault(f, []).append(label)
            for u in units:
                u.page_anchors = [(lbl, source) for lbl in by_file.get(u.href, [])]
        elif any(o == "EPUB_PAGEBREAK" for u in units for _, o in u.page_anchors):
            source = "EPUB_PAGEBREAK"
            for u in units:
                u.page_anchors = [a for a in u.page_anchors if a[1] == "EPUB_PAGEBREAK"]
        elif any(u.page_anchors for u in units):
            source = "CALIBRE_PAGE_ID"
        return EpubDoc(opf_path=opf_path, version=opf.get("version"), metadata=md, spine=units,
                       page_list_source=source, n_page_anchors=sum(len(u.page_anchors) for u in units),
                       manifest_media_types=mt,
                       package_manifest=[{"member": info.filename, "size_bytes": info.file_size, "crc32": info.CRC,
                           "disposition": "SPINE_PARSED" if any(u.href == info.filename and not u.parse_error for u in units)
                           else "RAW_ONLY"} for info in sorted(z.infolist(), key=lambda x: x.filename) if not info.is_dir()])


def read_member(path: Path, member: str) -> bytes:
    with zipfile.ZipFile(path) as z:
        return z.read(member)
