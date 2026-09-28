"""Native outlines of the source files: PDF bookmarks, EPUB navigation, DjVu outline — rule ``outline_v1``.

Runs on the WORKSTATION, where the PRIVATE files are (``$VKM_RESOURCES_ROOT``); CORE only receives the JSON. One file
per snapshot::

    {"format": "vkm-nav-outlines-v1", "rule_version": "outline_v1", "extractors": {...}, "stats": {...},
     "sources": {"<source_id>": {"kind": "PDF_OUTLINE", "page_count": N, "size_bytes": S, "error": null}},
     "outlines": {"<source_id>": [{"level": 1, "title": "...", "page_index": 7}, ...]}}

``outlines`` is exactly the mapping ``{source_id: [{level, title, page_index}]}`` that
:func:`vkm_corpus.navigation.sections.build` takes; :func:`load_outlines` also accepts a bare mapping. Page indexes are
physical and 1-based, as ``pages.page_index`` of the canon: the PDF page tree, the EPUB spine position of the target
XHTML file, the DjVu page number. Entries without a resolvable target keep ``page_index = null``; junk outlines are
NOT filtered here (the section builder filters them and counts the reasons), so the file is a faithful dump.
"""
from __future__ import annotations

import csv
import json
import posixpath
import re
import subprocess
import zipfile
from pathlib import Path
from typing import Any, Iterable

RULE_VERSION = "outline_v1"
FORMAT = "vkm-nav-outlines-v1"
KIND_BY_EXT = {".pdf": "PDF_OUTLINE", ".epub": "EPUB_NAV", ".djvu": "DJVU_OUTLINE", ".djv": "DJVU_OUTLINE"}
_OPS_NS = "http://www.idpf.org/2007/ops"
_LS_LINE = re.compile(r"^\s*(\d+)\s+P\s+\d+\s+(.+?)\s*$")


def _entry(level: int, title: Any, page_index: int | None) -> dict[str, Any]:
    t = re.sub(r"\s+", " ", str(title or "")).strip()
    return {"level": int(level), "title": t, "page_index": int(page_index) if page_index else None}


# ---------------------------------------------------------------------------------------------------------- PDF
def pdf_outline(path: Path) -> tuple[list[dict[str, Any]], int]:
    """PDF bookmarks by PyMuPDF (``get_toc``: level, title, 1-based page; -1 = no destination)."""
    import pymupdf

    with pymupdf.open(str(path)) as doc:
        toc = doc.get_toc(simple=True)
        n = doc.page_count
    return [_entry(lvl, title, page if page and page > 0 else None) for lvl, title, page, *_ in toc], n


# --------------------------------------------------------------------------------------------------------- EPUB
def _li_entries(ol: Any, level: int, resolve, out: list[dict[str, Any]]) -> None:
    for li in ol:
        if not isinstance(li.tag, str) or li.tag.split("}")[-1] != "li":
            continue
        label, href, sub = None, None, None
        for child in li:
            if not isinstance(child.tag, str):
                continue
            name = child.tag.split("}")[-1]
            if name in ("a", "span") and label is None:
                label = "".join(child.itertext())
                href = child.get("href")
            elif name == "ol":
                sub = child
        if label is not None:
            out.append(_entry(level, label, resolve(href)))
        if sub is not None:
            _li_entries(sub, level + 1, resolve, out)


def _ncx_points(parent: Any, level: int, resolve, out: list[dict[str, Any]]) -> None:
    for np_ in parent:
        if not isinstance(np_.tag, str) or np_.tag.split("}")[-1] != "navPoint":
            continue
        label = "".join(t for el in np_ if isinstance(el.tag, str) and el.tag.split("}")[-1] == "navLabel"
                        for t in el.itertext())
        src = next((el.get("src") for el in np_ if isinstance(el.tag, str)
                    and el.tag.split("}")[-1] == "content"), None)
        out.append(_entry(level, label, resolve(src)))
        _ncx_points(np_, level + 1, resolve, out)


def epub_outline(path: Path) -> tuple[list[dict[str, Any]], int]:
    """EPUB 3 ``nav[epub:type=toc]`` (fallback: NCX ``navMap``); a target is the spine position of its XHTML file."""
    from vkm_corpus.extract.epub import _xml, read_structure

    with zipfile.ZipFile(path) as z:
        _, _, manifest, spine = read_structure(z)
        page_of: dict[str, int] = {}
        for n, (idref, _) in enumerate(spine, 1):
            href = manifest.get(idref, ("",))[0]
            if href and href not in page_of:
                page_of[href] = n

        def resolver(doc_href: str):
            base = posixpath.dirname(doc_href)

            def resolve(href: str | None) -> int | None:
                if href is None:
                    return None
                target = href.split("#", 1)[0]
                target = doc_href if not target else posixpath.normpath(posixpath.join(base, target))
                return page_of.get(target)
            return resolve

        entries: list[dict[str, Any]] = []
        nav_href = next((h for h, _, props in manifest.values() if "nav" in props.split()), None)
        if nav_href:
            root = _xml(z.read(nav_href))
            navs = [el for el in root.iter() if isinstance(el.tag, str) and el.tag.split("}")[-1] == "nav"]
            toc = [el for el in navs if (el.get(f"{{{_OPS_NS}}}type") or el.get("type") or "") == "toc"] or navs[:1]
            for nav in toc[:1]:
                ol = next((el for el in nav if isinstance(el.tag, str) and el.tag.split("}")[-1] == "ol"), None)
                if ol is not None:
                    _li_entries(ol, 1, resolver(nav_href), entries)
        if not entries:
            ncx_href = next((h for h, media, _ in manifest.values() if media == "application/x-dtbncx+xml"), None)
            if ncx_href:
                root = _xml(z.read(ncx_href))
                nav_map = next((el for el in root.iter() if isinstance(el.tag, str)
                                and el.tag.split("}")[-1] == "navMap"), None)
                if nav_map is not None:
                    _ncx_points(nav_map, 1, resolver(ncx_href), entries)
    return entries, len(spine)


# --------------------------------------------------------------------------------------------------------- DjVu
def _djvused(path: Path, command: str, timeout: float) -> bytes:
    from vkm_corpus.extract.djvu import tool_path

    out = subprocess.run([tool_path("djvused"), str(path), "-e", command], capture_output=True, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError(f"djvused {command} exit {out.returncode}: {out.stderr.decode('utf-8', 'replace')[:200]}")
    return out.stdout


def djvu_outline(path: Path, timeout: float = 120.0) -> tuple[list[dict[str, Any]], int]:
    """``djvused -e print-outline``: ``(bookmarks ("title" "#12" (children…)) …)``; ``#N`` is a page number, other
    targets are component names resolved through ``djvused -e ls``."""
    from vkm_corpus.extract.djvu import parse_sexpr

    listing = _djvused(path, "ls", timeout).decode("utf-8", "replace")
    by_name: dict[str, int] = {}
    n_pages = 0
    for line in listing.splitlines():
        m = _LS_LINE.match(line)
        if m:
            n_pages = max(n_pages, int(m.group(1)))
            by_name.setdefault(m.group(2), int(m.group(1)))
    raw = _djvused(path, "print-outline", timeout)
    entries: list[dict[str, Any]] = []
    if not raw.strip():
        return entries, n_pages

    def target(url: str) -> int | None:
        u = url[1:] if url.startswith("#") else url
        if u.isdigit():
            return int(u)
        return by_name.get(u)

    def walk(items: Iterable[Any], level: int) -> None:
        for it in items:
            if not isinstance(it, list) or len(it) < 2 or not isinstance(it[0], tuple):
                continue
            title = it[0][1]
            url = it[1][1] if isinstance(it[1], tuple) else ""
            entries.append(_entry(level, title, target(url)))
            walk(it[2:], level + 1)

    for top in parse_sexpr(raw):
        if isinstance(top, list) and top and top[0] == "bookmarks":
            walk(top[1:], 1)
    return entries, n_pages


# ------------------------------------------------------------------------------------------------------ driver
def extract_file(path: Path) -> tuple[str | None, list[dict[str, Any]], int | None]:
    """(kind, entries, page_count) of one source file; kind None for formats without a native outline."""
    kind = KIND_BY_EXT.get(path.suffix.lower())
    if kind == "PDF_OUTLINE":
        return (kind, *pdf_outline(path))
    if kind == "EPUB_NAV":
        return (kind, *epub_outline(path))
    if kind == "DJVU_OUTLINE":
        return (kind, *djvu_outline(path))
    return None, [], None


def extractor_versions() -> dict[str, str]:
    out: dict[str, str] = {"rule_version": RULE_VERSION}
    try:
        import pymupdf

        out["pymupdf"] = str(pymupdf.VersionBind)
    except Exception:  # pragma: no cover - optional on CORE
        out["pymupdf"] = "unavailable"
    try:
        from vkm_corpus.extract.djvu import djvulibre_version

        out["djvulibre"] = djvulibre_version()
    except Exception:  # pragma: no cover
        out["djvulibre"] = "unavailable"
    return out


def read_register(resources_root: Path) -> list[dict[str, str]]:
    with open(resources_root / "00_registry" / "SOURCE_REGISTER.csv", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def extract_all(resources_root: Path, *, only: set[str] | None = None, log=None) -> dict[str, Any]:
    """Outlines of every registered file present in the PRIVATE clone (read only)."""
    sources: dict[str, dict[str, Any]] = {}
    outlines: dict[str, list[dict[str, Any]]] = {}
    stats = {"registered": 0, "missing": 0, "no_outline_format": 0, "errors": 0, "with_entries": 0}
    for row in read_register(resources_root):
        sid = row["resource_id"]
        if only and sid not in only:
            continue
        stats["registered"] += 1
        path = resources_root / row["canonical_path"]
        info: dict[str, Any] = {"kind": KIND_BY_EXT.get(path.suffix.lower()), "page_count": None,
                                "size_bytes": None, "size_matches_register": None, "n_entries": 0, "error": None}
        if not path.is_file():
            stats["missing"] += 1
            info["error"] = "MISSING"
            sources[sid] = info
            continue
        info["size_bytes"] = path.stat().st_size
        if row.get("size_bytes", "").isdigit():
            info["size_matches_register"] = info["size_bytes"] == int(row["size_bytes"])
        if info["kind"] is None:
            stats["no_outline_format"] += 1
            sources[sid] = info
            continue
        try:
            _, entries, n = extract_file(path)
        except Exception as exc:  # a broken file must not stop the run; recorded per source
            stats["errors"] += 1
            info["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
            sources[sid] = info
            continue
        info["page_count"] = n
        info["n_entries"] = len(entries)
        sources[sid] = info
        if entries:
            stats["with_entries"] += 1
            outlines[sid] = entries
        if log:
            log(f"{sid} {info['kind']} entries={len(entries)} pages={n}")
    return {"format": FORMAT, "rule_version": RULE_VERSION, "extractors": extractor_versions(), "stats": stats,
            "sources": sources, "outlines": outlines}


def load_outlines(path: str | Path) -> dict[str, list[dict[str, Any]]]:
    """``{source_id: [{level, title, page_index}]}`` from an outlines file (wrapped format or a bare mapping)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict) and data.get("format") == FORMAT:
        return data.get("outlines", {})
    return data
