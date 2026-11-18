"""EPUB via zipfile + lxml (EbookLib is AGPL and not used).

Page unit = spine item (``bbox_space = NONE``). Printed-page candidates retain a target member and XPath. Conflicting
labels at one target remain diagnostics. Text runs are split only at block boundaries or typed objects, preserving
each source text slot's position in the XHTML. GIF images with no alt are formula candidates.
"""
from __future__ import annotations

import base64
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

EXTRACTOR_ID = "epub-xhtml"
EPUB_EXTRACTION_RULE = "epub_dom_and_pagination_v3"
BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "pre", "blockquote", "dt", "dd", "caption",
              "figcaption", "td", "th", "div", "section", "aside", "header", "footer", "address", "ul", "ol", "dl",
              "body", "article", "main", "figure", "nav"}
HEADING = {"h1", "h2", "h3", "h4", "h5", "h6"}
EPUB_TYPE = "{http://www.idpf.org/2007/ops}type"
XML_ID = "{http://www.w3.org/XML/1998/namespace}id"
XML_BASE = "{http://www.w3.org/XML/1998/namespace}base"
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
    locator: str
    text_slots: list[str]


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


@dataclass(frozen=True)
class PageAnchor:
    label: str
    origin: str
    member: str
    fragment: str | None
    xpath: str
    order: int
    source_member: str
    source_xpath: str
    source_href: str | None = None


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
    page_anchor_details: list[dict[str, Any]] = field(default_factory=list)
    _anchors: list[PageAnchor] = field(default_factory=list)
    _targets: dict[str, list[tuple[str, int]]] = field(default_factory=dict)
    _file_target: str = ""


@dataclass
class EpubDoc:
    opf_path: str
    version: str | None
    metadata: dict[str, list[str]]
    spine: list[SpineUnit]
    page_list_source: str      # NAV_PAGE_LIST | NCX_PAGE_LIST | EPUB_PAGEBREAK | CALIBRE_PAGE_ID | MIXED | NONE
    n_page_anchors: int
    manifest_media_types: dict[str, int]
    package_manifest: list[dict[str, Any]] = field(default_factory=list)
    pagination_diagnostics: list[dict[str, Any]] = field(default_factory=list)


def _xml(data: bytes):
    from lxml import etree

    try:
        return etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True))
    except etree.XMLSyntaxError:
        root = etree.fromstring(data, etree.HTMLParser(recover=True, no_network=True))
        if root is None:
            raise ValueError("EMPTY_MARKUP")
        return root


def _resolve_href(base: str, href: str) -> tuple[str, str | None]:
    """Resolve a package-relative URL without using the filesystem or network."""
    if re.search(r"[\\\x00-\x1f\x7f]|%(?![a-fA-F0-9]{2})", href):
        raise ValueError("INVALID_URI")
    uri = urlsplit(href)
    if uri.scheme or uri.netloc or uri.path.startswith("/"):
        raise ValueError("NON_PACKAGE_URI")
    if uri.query or re.search(r"%2f|%5c", uri.path, flags=re.I):
        raise ValueError("INVALID_MEMBER_URI")
    path = unquote(uri.path, errors="strict")
    if re.search(r"[\\:\x00-\x1f\x7f]", path):
        raise ValueError("INVALID_MEMBER_URI")
    if path:
        folder = base if base.endswith("/") else posixpath.dirname(base)
        member = posixpath.normpath(posixpath.join(folder, path))
        if member == ".." or member.startswith("../"):
            raise ValueError("MEMBER_ESCAPES_PACKAGE")
        if path.endswith("/") or posixpath.basename(path) in (".", ".."):
            member = "" if member == "." else member + "/"
    else:
        member = base
    return member, unquote(uri.fragment, errors="strict") or None


def _element_href(el: Any, href: str, member: str, html_base: str | None = None) -> tuple[str, str | None]:
    base = member
    if html_base is not None:
        base, _ = _resolve_href(base, html_base)
    for ancestor in [*reversed(list(el.iterancestors())), el]:
        if (xml_base := ancestor.get(XML_BASE)) is not None:
            base, _ = _resolve_href(base, xml_base)
    return _resolve_href(base, href)


def _html_base(root: Any) -> str | None:
    return next(iter(root.xpath("//*[local-name()='head']/*[local-name()='base'][@href]/@href")), None)


def read_structure(z: zipfile.ZipFile) -> tuple[str, Any, dict[str, tuple[str, str | None, str]], list[tuple[str, bool]]]:
    container = _xml(z.read("META-INF/container.xml"))
    opf_path = container.xpath("//*[local-name()='rootfile']/@full-path")[0]
    opf = _xml(z.read(opf_path))
    manifest: dict[str, tuple[str, str | None, str]] = {}
    for it in opf.xpath("//*[local-name()='manifest']/*[local-name()='item']"):
        href = ""
        if it.get("href"):
            try:
                href, fragment = _element_href(it, it.get("href"), opf_path)
                if fragment is not None:
                    href = ""
            except (UnicodeError, ValueError):
                pass  # remote/invalid resources cannot supply an admitted spine item
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


def _element_id(el: Any) -> str | None:
    return el.get("id") or el.get(XML_ID)


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
    body = next(iter(root.xpath("//*[local-name()='body']")), root)
    unit._file_target = tree.getpath(body)
    for order, el in enumerate(root.iter(), 1):
        if not isinstance(el.tag, str):
            continue
        el_id = _element_id(el)
        xpath = tree.getpath(el)
        key = el_id or (el.get("name") if _local(el.tag) == "a" else None)
        if key:
            unit._targets.setdefault(key, []).append((xpath, order))
        if el_id and el_id.startswith("BIBe"):
            unit.bib_ids.append(el_id)
        label, origin = "", ""
        if "pagebreak" in (el.get(EPUB_TYPE) or "").split():
            label = el.get("title") or el.get("aria-label") or _text_of(el) or (el_id or "")
            origin = "EPUB_PAGEBREAK"
        if not label and (match := PAGE_ANCHOR.fullmatch(el_id or "")):
            label, origin = match.group(1), "CALIBRE_PAGE_ID"
        if label.strip():
            unit._anchors.append(PageAnchor(label.strip(), origin, unit.href, el_id, xpath, order, unit.href, xpath))

    html_base = _html_base(root)
    order = 0
    owner = None
    parts: list[tuple[str, str | None]] = []
    runs: dict[str, list[EpubBlock]] = {}

    def flush() -> None:
        nonlocal order
        text = "".join(value for value, _ in parts)
        slots = [slot for _, slot in parts if slot is not None]
        parts.clear()
        if owner is None or not text.strip():
            return
        tag = _local(owner.tag)
        if tag != "pre":
            text = " ".join(text.split())
        el_id = _element_id(owner)
        btype = "HEADING" if tag in HEADING else "LIST_ITEM" if tag == "li" else (
            "CAPTION" if tag in ("caption", "figcaption") else "TEXT")
        if {"footnote", "endnote"} & set((owner.get(EPUB_TYPE) or "").split()):
            btype = "FOOTNOTE"
        if (el_id or "").startswith("BIBe"):
            btype = "REFERENCE_LIST"
        order += 1
        xpath = tree.getpath(owner)
        block_runs = runs.setdefault(xpath, [])
        block = EpubBlock(tag, text, xpath, el_id, order, btype, xpath, slots)
        block_runs.append(block)
        unit.blocks.append(block)

    def walk(el: Any, *, typed_only: bool = False) -> None:
        nonlocal order, owner
        tag = _local(el.tag)
        if not tag or tag in {"head", "script", "style", "template"}:
            return
        xpath = tree.getpath(el)
        el_id = _element_id(el)
        if tag in {"table", "math", "img", "image", "svg", "object", "iframe"}:
            flush()
            order += 1
            if tag == "table":
                unit.tables.append(EpubTable(xpath, el_id, etree.tostring(el, encoding="unicode", with_tail=False), order))
                for child in el:
                    walk(child, typed_only=True)
            elif tag == "math":
                unit.maths.append(EpubMath(xpath, el_id, etree.tostring(el, encoding="unicode", with_tail=False),
                                           _text_of(el), el.get("display") == "block", order))
            elif tag in {"svg", "object", "iframe"}:
                unit.diagnostics.append({"code": "RAW_ONLY_" + tag.upper(), "xpath": xpath})
                for child in el:
                    walk(child, typed_only=True)
            else:
                src = el.get("src") or el.get("{http://www.w3.org/1999/xlink}href") or ""
                try:
                    href, _ = _element_href(el, src, unit.href, html_base)
                    if not src or href not in z.NameToInfo or href.endswith("/"):
                        raise ValueError("IMAGE_MEMBER_NOT_FOUND")
                except (ValueError, UnicodeError) as exc:
                    unit.diagnostics.append({"code": "UNRESOLVED_IMAGE", "xpath": xpath, "href": src,
                                             "reason": str(exc)})
                    return
                media, alt = manifest_by_href.get(href), el.get("alt")
                is_gif = media == "image/gif" or href.lower().endswith(".gif")
                image_parent = next((a for a in el.iterancestors() if _local(a.tag) in BLOCK_TAGS), None)
                block_text = _text_of(image_parent) if image_parent is not None else ""
                match = EQ_LABEL.search(block_text)
                eq_label = f"({match.group(1)})" if match and is_gif else None
                eq_anchor = next((identifier for a in el.iterancestors() if
                                  (identifier := _element_id(a)) and EQN_ID.match(identifier)), None)
                unit.images.append(EpubImage(href, media, alt, is_gif and not (alt or "").strip(),
                                             len(EQ_LABEL.sub("", block_text).strip()) > 3, el_id or eq_anchor,
                                             xpath, order, eq_label))
            return
        if typed_only:
            for child in el:
                walk(child, typed_only=True)
            return
        if tag == "br":
            parts.append(("\n", None))
            return
        enclosing = owner
        if tag in BLOCK_TAGS or owner is None:
            flush()
            owner = el
        text_index = 0
        if el.text is not None:
            text_index = 1
            parts.append((el.text, f"{xpath}/text()[1]"))
        for child in el:
            walk(child)
            if child.tail is not None:
                text_index += 1
                parts.append((child.tail, f"{xpath}/text()[{text_index}]"))
        if owner is el:
            flush()
            owner = enclosing

    walk(body)
    for xpath, blocks in runs.items():
        if len(blocks) > 1:
            for ordinal, block in enumerate(blocks, 1):
                block.locator = f"{xpath}#text-run={ordinal}"


def _listed_page_anchors(z: zipfile.ZipFile, manifest: dict[str, tuple[str, str | None, str]],
                         units: list[SpineUnit], diagnostics: list[dict[str, Any]],
                         ncx_id: str | None) -> None:
    by_member: dict[str, list[SpineUnit]] = {}
    for unit in units:
        if unit.href and not unit.parse_error:
            by_member.setdefault(unit.href, []).append(unit)
    navs = [v[0] for v in manifest.values() if "nav" in v[2].split()]
    ncx = [manifest[ncx_id][0]] if ncx_id in manifest else [
        v[0] for v in manifest.values() if v[1] == "application/x-dtbncx+xml"]

    def read_listing(member: str, is_nav: bool) -> None:
        origin = "NAV_PAGE_LIST" if is_nav else "NCX_PAGE_LIST"
        try:
            root = _xml(z.read(member))
        except (KeyError, ValueError, OSError) as exc:
            diagnostics.append({"code": "PAGE_LIST_DOCUMENT_UNREADABLE", "member": member,
                                "origin": origin, "reason": str(exc)})
            return
        tree = root.getroottree()
        html_base = _html_base(root) if is_nav else None
        if is_nav:
            nodes = root.xpath("//*[local-name()='nav']//*[local-name()='a']")
            entries = [(_text_of(a), a.get("href"), a)
                       for a in nodes if any(_local(n.tag) == "nav"
                       and "page-list" in (n.get(EPUB_TYPE) or n.get("type") or "").split()
                       for n in a.iterancestors())]
        else:
            entries = []
            for pt in root.xpath("//*[local-name()='pageList']/*[local-name()='pageTarget']"):
                contents = pt.xpath("./*[local-name()='content']")
                labels = pt.xpath("./*[local-name()='navLabel']")
                entries.append((_text_of(labels[0]) if labels else "", contents[0].get("src") if contents else None,
                                contents[0] if contents else pt))
        for label, href, el in entries:
            source_xpath = tree.getpath(el)
            try:
                if not href or not label:
                    raise ValueError("EMPTY_PAGE_LABEL_OR_LINK")
                target, fragment = _element_href(el, href, member, html_base)
                matches = by_member.get(target, [])
                if not matches:
                    raise ValueError("PAGE_LINK_NOT_ADMITTED_SPINE_MEMBER")
                if len(matches) != 1:
                    raise ValueError("AMBIGUOUS_SPINE_MEMBER")
                unit, = matches
                locations = unit._targets.get(fragment, []) if fragment else [(unit._file_target, 0)]
                if not locations:
                    raise ValueError("PAGE_FRAGMENT_NOT_FOUND")
                if len(locations) != 1:
                    raise ValueError("AMBIGUOUS_PAGE_FRAGMENT")
                (xpath, order), = locations
                unit._anchors.append(PageAnchor(label, origin, target, fragment, xpath, order,
                                                member, source_xpath, href))
            except (UnicodeError, ValueError) as exc:
                diagnostics.append({"code": "UNRESOLVED_PAGE_LINK", "origin": origin, "member": member,
                                    "xpath": source_xpath, "href": href, "label": label, "reason": str(exc)})

    for member in navs:
        read_listing(member, True)
    for member in ncx:
        read_listing(member, False)


def _effective_page_anchors(units: list[SpineUnit]) -> str:
    priority = {"NAV_PAGE_LIST": 0, "NCX_PAGE_LIST": 1, "EPUB_PAGEBREAK": 2, "CALIBRE_PAGE_ID": 3}
    origins = set()
    for unit in units:
        by_location: dict[str, list[PageAnchor]] = {}
        for anchor in sorted(unit._anchors, key=lambda a: (a.order, priority[a.origin])):
            by_location.setdefault(anchor.xpath, []).append(anchor)
        for xpath, candidates in by_location.items():
            if len({a.label for a in candidates}) > 1:
                unit.diagnostics.append({"code": "PAGE_ANCHOR_CONFLICT", "member": unit.href, "xpath": xpath,
                                         "candidates": [a.__dict__ for a in candidates]})
                continue
            effective = candidates[0]
            origins.add(effective.origin)
            unit.page_anchors.append((effective.label, effective.origin))
            unit.page_anchor_details.append({**effective.__dict__, "candidates": [a.__dict__ for a in candidates]})
    return next(iter(origins)) if len(origins) == 1 else "MIXED" if origins else "NONE"


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
        diagnostics: list[dict[str, Any]] = []
        ncx_id = next(iter(opf.xpath("//*[local-name()='spine']/@toc")), None)
        _listed_page_anchors(z, manifest, units, diagnostics, ncx_id)
        source = _effective_page_anchors(units)
        return EpubDoc(opf_path=opf_path, version=opf.get("version"), metadata=md, spine=units,
                       page_list_source=source, n_page_anchors=sum(len(u.page_anchors) for u in units),
                       manifest_media_types=mt, pagination_diagnostics=diagnostics,
                       package_manifest=[{"member": info.filename, "size_bytes": info.file_size, "crc32": info.CRC,
                           "disposition": "SPINE_PARSED" if any(u.href == info.filename and not u.parse_error for u in units)
                           else "RAW_ONLY"} for info in sorted(z.infolist(), key=lambda x: x.filename) if not info.is_dir()])


def read_member(path: Path, member: str) -> bytes:
    with zipfile.ZipFile(path) as z:
        return z.read(member)
