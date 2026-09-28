"""Embedding units (task §17), rule ``vkm-units-v1``, and context variants (task §18).

A unit is a group of canonical objects, never an arbitrary token window:

* ``BLOCK_GROUP`` — consecutive primary-layer blocks of one page in reading order (a heading opens a group and sticks
  to the text after it); target 900, max 1600 characters; a trailing group under 200 characters joins the previous
  group of the page; a block over 1600 characters is split at sentence boundaries into parts;
* ``FIGURE`` — label + caption + explanatory text (blocks of the page that cite the figure number, else the closest
  text block before the caption);
* ``TABLE`` — label, caption, the normalised table, + the paragraph that cites the table;
* ``FORMULA`` — equation label + LaTeX (raw output unless IMAGE_ONLY) + the block above it and the explanation after
  it («где …», «where …»);
* ``BIB_ENTRY`` — one bibliography entry;
* ``PAGE`` — the page text (``page_text_v1``), a separate collection for the page-level vector experiment.

Units never cross a page boundary, so page qrels map exactly. Any behaviour change needs a new rule id.

Rows come from the canonical DuckDB views (``retrieval_lab.canon``) or from tests as plain mappings.
"""
from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping

from vkm_corpus.retrieval_lab.textproc import (label_number, pack_sentences, reference_pattern,
                                               trim_to_sentence)

UNIT_RULE = "vkm-units-v1"
MAIN_KINDS: tuple[str, ...] = ("BLOCK_GROUP", "FIGURE", "TABLE", "FORMULA", "BIB_ENTRY")
GROUPABLE_BLOCKS: frozenset[str] = frozenset({"TEXT", "LIST_ITEM", "ABSTRACT", "TITLE", "HEADING", "FOOTNOTE",
                                              "SIDE_TEXT", "CODE", "TABLE_OF_CONTENTS", "OTHER", "UNKNOWN",
                                              "CAPTION", "REFERENCE_LIST"})
HEADING_BLOCKS: frozenset[str] = frozenset({"HEADING", "TITLE"})
RUNNING_BLOCKS: frozenset[str] = frozenset({"PAGE_HEADER", "PAGE_FOOTER", "PAGE_NUMBER"})
CONTEXT_BLOCKS: frozenset[str] = frozenset({"TEXT", "LIST_ITEM", "ABSTRACT"})


@dataclass(frozen=True)
class UnitConfig:
    target_chars: int = 900
    max_chars: int = 1600
    min_chars: int = 200
    figure_context_chars: int = 600
    figure_fallback_chars: int = 400
    table_chars: int = 1500
    table_context_chars: int = 400
    formula_before_chars: int = 300
    formula_after_chars: int = 400
    page_chars: int = 4000
    section_chars: int = 200
    neighbor_chars: int = 300
    with_pages: bool = False

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class Unit:
    unit_id: str
    kind: str
    source_id: str
    page_id: str | None
    object_ids: tuple[str, ...]
    text: str
    part: int = 0
    section_title: str | None = None
    prev_text: str | None = None
    next_text: str | None = None
    order: tuple = ()                       # (page order, reading order) inside the source — for CTX windows
    language: str | None = None
    image_artifact_id: str | None = None
    flags: tuple[str, ...] = ()

    @property
    def text_sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SourceMeta:
    source_id: str
    work_id: str | None = None
    title: str | None = None
    authors: str | None = None
    year: int | None = None
    venue: str | None = None
    source_class: str | None = None
    site_scope_raw: str | None = None


def unit_id(kind: str, object_ids: Iterable[str], part: int = 0) -> str:
    key = f"{UNIT_RULE}|{kind}|{'|'.join(object_ids)}|{part}"
    return "u1-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _txt(row: Mapping[str, Any], key: str = "normalized_text") -> str:
    v = row.get(key)
    return str(v).strip() if v is not None else ""


def _ro(row: Mapping[str, Any]) -> tuple:
    ro = row.get("reading_order")
    return (ro if ro is not None else 10 ** 9, str(row.get("object_id")))


def _y(row: Mapping[str, Any], key: str) -> float | None:
    v = row.get(key)
    return float(v) if v is not None else None


# ---------------------------------------------------------------- builder
@dataclass
class _Page:
    page_id: str
    source_id: str
    index: int
    kind: str | None
    text: str
    labels: tuple[str, ...]
    blocks: list[Mapping[str, Any]] = field(default_factory=list)
    figures: list[Mapping[str, Any]] = field(default_factory=list)
    tables: list[Mapping[str, Any]] = field(default_factory=list)
    formulas: list[Mapping[str, Any]] = field(default_factory=list)
    bib: list[Mapping[str, Any]] = field(default_factory=list)


def build_units(pages: Iterable[Mapping[str, Any]], blocks: Iterable[Mapping[str, Any]],
                figures: Iterable[Mapping[str, Any]] = (), tables: Iterable[Mapping[str, Any]] = (),
                formulas: Iterable[Mapping[str, Any]] = (), bibliography: Iterable[Mapping[str, Any]] = (),
                config: UnitConfig | None = None) -> list[Unit]:
    """Units of all given pages (rule ``vkm-units-v1``), ordered by (source, page index, position)."""
    cfg = config or UnitConfig()
    by_page: dict[str, _Page] = {}
    for p in pages:
        by_page[p["page_id"]] = _Page(p["page_id"], p["source_id"], int(p.get("page_index") or 0), p.get("page_kind"),
                                      _txt(p), tuple(p.get("printed_page_labels") or ()))
    orphans = 0
    for name, rows in (("blocks", blocks), ("figures", figures), ("tables", tables), ("formulas", formulas),
                       ("bib", bibliography)):
        for r in rows:
            page = by_page.get(r.get("page_id"))
            if page is None:
                orphans += 1
                continue
            getattr(page, name).append(r)
    units: list[Unit] = []
    by_source: dict[str, list[_Page]] = defaultdict(list)
    for page in by_page.values():
        by_source[page.source_id].append(page)
    for sid in sorted(by_source):
        section: str | None = None
        for page in sorted(by_source[sid], key=lambda p: (p.index, p.page_id)):
            page_units, section = _page_units(page, section, cfg)
            units.extend(page_units)
    return units


def _page_units(page: _Page, section: str | None, cfg: UnitConfig) -> tuple[list[Unit], str | None]:
    primary = sorted((b for b in page.blocks if b.get("is_primary_layer") is not False
                      and str(b.get("block_type")) not in RUNNING_BLOCKS and _txt(b)), key=_ro)
    captions_used = {str(x.get("caption_block_id")) for x in (*page.figures, *page.tables) if x.get("caption_block_id")}
    has_bib = bool(page.bib)
    stream = [b for b in primary if str(b.get("block_type")) in GROUPABLE_BLOCKS
              and str(b.get("object_id")) not in captions_used
              and not (has_bib and str(b.get("block_type")) == "REFERENCE_LIST")]
    out: list[Unit] = []
    groups, section = _block_groups(page, stream, section, cfg)
    out.extend(groups)
    out.extend(_figure_units(page, primary, cfg))
    out.extend(_table_units(page, primary, cfg))
    out.extend(_formula_units(page, primary, cfg))
    for e in sorted(page.bib, key=_ro):
        text = _txt(e)
        if text:
            out.append(Unit(unit_id("BIB_ENTRY", [e["object_id"]]), "BIB_ENTRY", page.source_id, page.page_id,
                            (e["object_id"],), text, order=(page.index, _ro(e)), language=e.get("language")))
    if cfg.with_pages and page.text:
        text = page.text[:cfg.page_chars]
        flags = ("TRUNCATED",) if len(page.text) > cfg.page_chars else ()
        out.append(Unit(unit_id("PAGE", [page.page_id]), "PAGE", page.source_id, page.page_id, (page.page_id,), text,
                        order=(page.index, (-1, "")), flags=flags))
    return out, section


def _block_groups(page: _Page, stream: list[Mapping[str, Any]], section: str | None,
                  cfg: UnitConfig) -> tuple[list[Unit], str | None]:
    raw: list[tuple[list[Mapping[str, Any]], str, int, str | None]] = []   # (blocks, text, part, section)
    cur: list[Mapping[str, Any]] = []
    cur_len = 0
    cur_section = section

    def flush() -> None:
        nonlocal cur, cur_len
        if cur:
            raw.append((cur, "\n".join(_txt(b) for b in cur), 0, cur_section))
        cur, cur_len = [], 0

    for b in stream:
        text = _txt(b)
        heading = str(b.get("block_type")) in HEADING_BLOCKS
        if heading:
            if cur and not all(str(x.get("block_type")) in HEADING_BLOCKS for x in cur):
                flush()
            section = text[:cfg.section_chars]
            cur_section = section
        if len(text) > cfg.max_chars:
            prefix = "\n".join(_txt(x) for x in cur) if cur and all(
                str(x.get("block_type")) in HEADING_BLOCKS for x in cur) else None
            members = list(cur) if prefix else []
            if not prefix:
                flush()
            parts = pack_sentences(text, cfg.max_chars)
            for i, part in enumerate(parts):
                blocks_i = members + [b] if (i == 0 and prefix) else [b]
                body = f"{prefix}\n{part}" if (i == 0 and prefix) else part
                raw.append((blocks_i, body, i, cur_section))
            cur, cur_len = [], 0
            continue
        if cur and cur_len + 1 + len(text) > cfg.max_chars:
            flush()
        cur.append(b)
        cur_len += len(text) + (1 if cur_len else 0)
        if cur_len >= cfg.target_chars and not heading:
            flush()
    flush()
    # a short trailing group joins the previous one of the same page
    merged: list[tuple[list[Mapping[str, Any]], str, int, str | None]] = []
    for item in raw:
        if (merged and len(item[1]) < cfg.min_chars and item[2] == 0 and merged[-1][2] == 0
                and len(merged[-1][1]) + 1 + len(item[1]) <= cfg.max_chars):
            prev = merged.pop()
            merged.append((prev[0] + item[0], prev[1] + "\n" + item[1], 0, prev[3]))
        else:
            merged.append(item)
    units = []
    for blocks_i, text, part, sec in merged:
        oids = [str(b["object_id"]) for b in blocks_i]
        langs = {b.get("language") for b in blocks_i if b.get("language")}
        units.append(Unit(unit_id("BLOCK_GROUP", oids, part), "BLOCK_GROUP", page.source_id, page.page_id,
                          tuple(oids), text, part=part, section_title=sec, order=(page.index, _ro(blocks_i[0])),
                          language=langs.pop() if len(langs) == 1 else None,
                          flags=("SPLIT_PART",) if len(blocks_i) == 1 and part > 0 else ()))
    # neighbours (context variant C): previous / next group of the same page
    out = []
    for i, u in enumerate(units):
        prev_t = trim_to_sentence(units[i - 1].text, cfg.neighbor_chars, from_end=True) if i > 0 else None
        next_t = trim_to_sentence(units[i + 1].text, cfg.neighbor_chars) if i + 1 < len(units) else None
        out.append(replace(u, prev_text=prev_t, next_text=next_t))
    return out, section


def _context_blocks(primary: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [b for b in primary if str(b.get("block_type")) in CONTEXT_BLOCKS]


def _citing_blocks(kind: str, label: str | None, primary: list[Mapping[str, Any]], limit_chars: int,
                   max_blocks: int = 2) -> list[str]:
    number = label_number(label)
    if not number:
        return []
    pattern = reference_pattern(kind, number)
    out, used = [], 0
    for b in _context_blocks(primary):
        text = _txt(b)
        if pattern.search(text):
            piece = trim_to_sentence(text, max(limit_chars - used, 0))
            if piece:
                out.append(piece)
                used += len(piece) + 1
        if len(out) >= max_blocks or used >= limit_chars:
            break
    return out


def _preceding_block(obj: Mapping[str, Any], primary: list[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """The text block just before the object's caption in reading order, else the one closest above its bbox."""
    ctx = _context_blocks(primary)
    cap = obj.get("caption_block_id")
    if cap:
        order = [str(b.get("object_id")) for b in primary]
        if str(cap) in order:
            pos = order.index(str(cap))
            before = [b for b in primary[:pos] if str(b.get("block_type")) in CONTEXT_BLOCKS]
            if before:
                return before[-1]
    y0 = _y(obj, "bbox_y0")
    if y0 is None:
        return None
    above = [b for b in ctx if _y(b, "bbox_y1") is not None and _y(b, "bbox_y1") <= y0 + 1.0]
    return max(above, key=lambda b: _y(b, "bbox_y1")) if above else None


def _following_block(obj: Mapping[str, Any], primary: list[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    y1 = _y(obj, "bbox_y1")
    if y1 is None:
        return None
    below = [b for b in _context_blocks(primary) if _y(b, "bbox_y0") is not None and _y(b, "bbox_y0") >= y1 - 1.0]
    return min(below, key=lambda b: _y(b, "bbox_y0")) if below else None


def _figure_units(page: _Page, primary: list[Mapping[str, Any]], cfg: UnitConfig) -> list[Unit]:
    out = []
    for f in sorted(page.figures, key=lambda r: str(r["object_id"])):
        label, caption = _txt(f, "figure_label"), _txt(f, "caption_normalized") or _txt(f, "caption")
        head = " ".join(x for x in (label, caption) if x)
        context = _citing_blocks("FIGURE", label or caption, primary, cfg.figure_context_chars)
        flags: tuple[str, ...] = ()
        if not context:
            prev = _preceding_block(f, primary)
            if prev is not None:
                context = [trim_to_sentence(_txt(prev), cfg.figure_fallback_chars, from_end=True)]
                flags = ("CONTEXT_FALLBACK",)
        if not head and not context:
            continue                                   # visual track only
        text = "\n".join(x for x in [head, *context] if x)
        out.append(Unit(unit_id("FIGURE", [f["object_id"]]), "FIGURE", page.source_id, page.page_id,
                        (f["object_id"],), text, order=(page.index, (10 ** 8, str(f["object_id"]))),
                        image_artifact_id=f.get("image_artifact_id"),
                        flags=flags + (() if head else ("NO_CAPTION",))))
    return out


def _table_units(page: _Page, primary: list[Mapping[str, Any]], cfg: UnitConfig) -> list[Unit]:
    out = []
    for t in sorted(page.tables, key=lambda r: str(r["object_id"])):
        label, caption = _txt(t, "table_label"), _txt(t, "caption_normalized") or _txt(t, "caption")
        body = _txt(t)
        flags: tuple[str, ...] = ()
        if len(body) > cfg.table_chars:
            cut = body.rfind("\n", 0, cfg.table_chars)
            body = body[:cut if cut > cfg.table_chars // 2 else cfg.table_chars]
            flags = ("TABLE_TRUNCATED",)
        context = _citing_blocks("TABLE", label or caption, primary, cfg.table_context_chars, max_blocks=1)
        text = "\n".join(x for x in [label, caption, body, *context] if x)
        if not text:
            continue
        out.append(Unit(unit_id("TABLE", [t["object_id"]]), "TABLE", page.source_id, page.page_id, (t["object_id"],),
                        text, order=(page.index, (10 ** 8, str(t["object_id"]))),
                        image_artifact_id=t.get("image_artifact_id"), flags=flags))
    return out


_WHERE = re.compile(r"^\s*(?:где|здесь|where|here)\b", re.IGNORECASE)


def _formula_units(page: _Page, primary: list[Mapping[str, Any]], cfg: UnitConfig) -> list[Unit]:
    out = []
    for m in sorted(page.formulas, key=lambda r: str(r["object_id"])):
        latex = _txt(m, "normalized_latex")
        if not latex and str(m.get("raw_format")) != "IMAGE_ONLY":
            latex = _txt(m, "raw_output")
        if not latex:
            continue
        head = " ".join(x for x in (_txt(m, "equation_label"), latex) if x)
        before = _preceding_block(m, primary)
        after = _following_block(m, primary)
        parts = []
        if before is not None:
            parts.append(trim_to_sentence(_txt(before), cfg.formula_before_chars, from_end=True))
        parts.append(head)
        if after is not None and _WHERE.match(_txt(after)):
            parts.append(trim_to_sentence(_txt(after), cfg.formula_after_chars))
        out.append(Unit(unit_id("FORMULA", [m["object_id"]]), "FORMULA", page.source_id, page.page_id,
                        (m["object_id"],), "\n".join(p for p in parts if p),
                        order=(page.index, (10 ** 8, str(m["object_id"]))),
                        image_artifact_id=m.get("image_artifact_id")))
    return out


# ---------------------------------------------------------------- context variants (§18)
CONTEXT_VARIANTS: tuple[str, ...] = ("A", "B", "C", "D")


def meta_line(meta: SourceMeta | None, page_label: str | None = None) -> str:
    if meta is None:
        return ""
    parts = [meta.authors, str(meta.year) if meta.year else None, meta.venue, meta.source_class,
             f"область: {meta.site_scope_raw}" if meta.site_scope_raw else None,
             f"с. {page_label}" if page_label else None]
    kept = [p for p in parts if p]
    return f"[{'; '.join(kept)}]" if kept else ""


def render(unit: Unit, variant: str = "A", meta: SourceMeta | None = None, page_label: str | None = None) -> str:
    """Document-side text of a unit for context variant A–D."""
    if variant == "A":
        return unit.text
    if variant not in CONTEXT_VARIANTS:
        raise ValueError(f"unknown context variant {variant}")
    header = ". ".join(x for x in ((meta.title if meta else None), unit.section_title) if x)
    lines = [header] if header else []
    if variant == "C" and unit.prev_text:
        lines.append(unit.prev_text)
    lines.append(unit.text)
    if variant == "C" and unit.next_text:
        lines.append(unit.next_text)
    if variant == "D":
        line = meta_line(meta, page_label)
        if line:
            lines.append(line)
    return "\n".join(lines)


def ctx_windows(units: list[Unit], *, max_units: int = 8, max_chars: int = 12000) -> list[list[Unit]]:
    """Late-chunking windows for contextual models: consecutive units of one source in reading order."""
    windows: list[list[Unit]] = []
    by_source: dict[str, list[Unit]] = defaultdict(list)
    for u in units:
        by_source[u.source_id].append(u)
    for sid in sorted(by_source):
        cur: list[Unit] = []
        size = 0
        for u in sorted(by_source[sid], key=lambda x: (x.order, x.unit_id)):
            if cur and (len(cur) >= max_units or size + len(u.text) > max_chars):
                windows.append(cur)
                cur, size = [], 0
            cur.append(u)
            size += len(u.text)
        if cur:
            windows.append(cur)
    return windows


def units_summary(units: Iterable[Unit]) -> dict[str, Any]:
    counts: dict[str, int] = defaultdict(int)
    chars: dict[str, int] = defaultdict(int)
    flags: dict[str, int] = defaultdict(int)
    pages, sources = set(), set()
    for u in units:
        counts[u.kind] += 1
        chars[u.kind] += len(u.text)
        pages.add(u.page_id)
        sources.add(u.source_id)
        for f in u.flags:
            flags[f] += 1
    return {"rule": UNIT_RULE, "units": dict(sorted(counts.items())), "total": sum(counts.values()),
            "mean_chars": {k: round(chars[k] / counts[k], 1) for k in sorted(counts)}, "flags": dict(sorted(flags.items())),
            "pages": len(pages), "sources": len(sources)}
