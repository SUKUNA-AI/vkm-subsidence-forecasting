"""Sections (document tree) of the navigation layer — rule ``sections_v1``, no LLM.

``build(con, *, outlines=None, stats=None)`` reads the canon (``canonical.pages``, ``canonical.blocks`` primary layer,
``canonical.documents``, ``canonical.sources``, ``canonical.source_work_links``) and returns two Arrow tables:

* ``sections`` — one row per section (fields of ``docs/corpus_platform/NAVIGATION_LAYER.md`` §2, plus the physical
  ``page_start_index`` / ``page_end_index`` for range joins);
* ``section_pages`` — for every covered page, the deepest section(s) whose range contains it.

One method per source, by priority (the first that yields a usable tree wins):

1. native outline (``PDF_OUTLINE`` / ``EPUB_NAV`` / ``DJVU_OUTLINE``, from ``nav outlines``) after the junk filter;
2. ``PRINTED_TOC`` — ``TABLE_OF_CONTENTS`` blocks parsed into (numbering, title, printed page); the printed page goes
   to a physical page through the printed labels of the pages (direct label, else the dominant label offset, else
   the dominant offset of TOC titles found as headings); a TOC entry is moved by at most 2 pages when its heading is
   found there;
3. ``HEADING_NUMBERING`` — ``HEADING`` blocks numbered «Глава 3», «3.2.1 …», «§ 5», «Chapter 3», «I. …»;
4. ``HEADING_LAYOUT`` — ``TITLE`` blocks (merged per page) as level 1, ``HEADING`` blocks below them;
5. ``WHOLE_SOURCE`` — one section over all pages when nothing else is found.

Levels come from the outline level or the numbering rank and are normalised to the depth in the tree. A section runs
until the next section of the same or a higher level starts (its page minus one; a parent also covers the pages of its
children), so siblings never overlap except on one boundary page when a section starts on the page where the previous
one's last subsection starts. ``confidence`` = method base + agreement with the other methods (same title within
±1 page) + a found heading block. The layer is DERIVED navigation, ``AUTO_EXTRACTED_UNREVIEWED``: a section is a
pointer to pages, not evidence.
"""
from __future__ import annotations

import re
import unicodedata
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Iterable

import pyarrow as pa

from vkm_corpus.navigation.ids import RULE_VERSIONS, norm_text, section_id

RULE_VERSION = RULE_VERSIONS["sections"]
OUTLINE_METHOD_BY_FORMAT = {"PDF": "PDF_OUTLINE", "EPUB": "EPUB_NAV", "DJVU": "DJVU_OUTLINE"}
OUTLINE_METHODS = frozenset(OUTLINE_METHOD_BY_FORMAT.values())
METHODS = ("PDF_OUTLINE", "EPUB_NAV", "DJVU_OUTLINE", "PRINTED_TOC", "HEADING_NUMBERING", "HEADING_LAYOUT",
           "WHOLE_SOURCE")
BASE_CONFIDENCE = {"PDF_OUTLINE": 0.85, "EPUB_NAV": 0.9, "DJVU_OUTLINE": 0.85, "PRINTED_TOC": 0.7,
                   "HEADING_NUMBERING": 0.6, "HEADING_LAYOUT": 0.4, "WHOLE_SOURCE": 0.5}
CONTAINER_CLASSES = frozenset({"journal_issue", "proceedings_volume"})
TITLE_SIM = 0.8          # fuzzy title agreement (normalised text)
MAX_TOC_SHIFT = 2        # a TOC entry may move this many pages to its heading
MIN_TOC_ANCHORED = 0.4   # a printed TOC with fewer entries anchored to a heading falls back to the next method

SECTIONS_SCHEMA = pa.schema([
    ("section_id", pa.string()), ("source_id", pa.string()), ("work_id", pa.string()),
    ("parent_section_id", pa.string()), ("level", pa.int16()), ("ordinal", pa.int32()),
    ("numbering", pa.string()), ("title", pa.string()), ("title_path", pa.string()),
    ("page_start_id", pa.string()), ("page_end_id", pa.string()),
    ("page_start_index", pa.int32()), ("page_end_index", pa.int32()),
    ("method", pa.string()), ("confidence", pa.float64()), ("heading_block_id", pa.string()),
    ("rule_version", pa.string()),
])
SECTION_PAGES_SCHEMA = pa.schema([
    ("section_id", pa.string()), ("page_id", pa.string()), ("source_id", pa.string()), ("page_index", pa.int32()),
    ("rule_version", pa.string()),
])


# ============================================================================================ text rules
_CTRL = re.compile(r"[\u0000-\u001f\u007f﻿​]+")
_WORD_RANK = {"часть": 0, "part": 0, "книга": 0, "book": 0, "раздел": 0,
              "глава": 1, "chapter": 1, "лекция": 1, "lecture": 1, "тема": 1, "приложение": 1, "appendix": 1,
              "section": 2}
_NUM = r"(?P<num>\d{1,3}(?:\.\d{1,3}){0,5}|[IVXLCDM]{1,7}|[А-ЯЁA-Z])"
RE_WORD = re.compile(r"^(?P<word>глава|chapter|часть|part|книга|book|раздел|section|приложение|appendix|лекция|"
                     r"lecture|тема)\s+" + _NUM + r"(?![\w])\.?\s*[:.\-–—]?\s*(?P<rest>.*)$", re.I)
RE_PARA = re.compile(r"^§\s*(?P<num>\d{1,3}(?:\.\d{1,3}){0,4})\.?\s*(?P<rest>.*)$")
RE_DOTTED = re.compile(r"^(?P<num>\d{1,2}(?:\.\d{1,3}){0,5})(?:\.\s*|\s+)(?P<rest>[^\d\s.,;:)\]].*)$")
RE_ROMAN = re.compile(r"^(?P<num>[IVXLCDM]{1,7})\.\s+(?P<rest>\S.*)$")
RE_NUM_ONLY = re.compile(r"^(?:(?:глава|chapter|часть|part|раздел|section|приложение|appendix|лекция|lecture|тема)"
                         r"\s+)?(?:\d{1,3}(?:\.\d{1,3}){0,5}|[IVXLCDM]{1,7}|[А-ЯЁA-Z])\.?$|^§\s*\d{1,3}(?:\.\d{1,3})*\.?$",
                         re.I)
_KEYWORDS = re.compile(
    r"^(введение|предисловие|вместо предисловия|от автора|от авторов|от редактора|от редакции|заключение|выводы|"
    r"общие выводы|основные выводы|список (использованной |использованных |цитированной )?(литературы|источников)|"
    r"литература|библиографи|библиографический список|приложени|содержание|оглавление|указатель|предметный указатель|"
    r"именной указатель|аннотация|реферат|резюме|основные обозначения|условные обозначения|список сокращений|"
    r"перечень сокращений|abstract|summary|introduction|preface|foreword|conclusion|concluding remarks|references|"
    r"bibliography|index|subject index|author index|appendix|appendices|contents|table of contents|glossary|"
    r"acknowledg|notation|nomenclature|list of symbols|list of figures|list of tables)")
_TOC_HEADER = re.compile(r"^(содержание|оглавление|contents|table of contents|стр|с|page|pages)$")
_NOISE = re.compile(r"^(удк|udc|ббк|doi|issn|isbn|гост|©|copyright|received|accepted|keywords|ключевые слова)\b")
_ROMAN_VAL = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
_JUNK_TITLE = re.compile(r"(\.(pdf|djvu?|docx?|tiff?|jpe?g|png)$|^microsoft word\b|^(page|стр|страница|с)\s*\.?\s*\d+$"
                         r"|^\d+$)", re.I)


def clean_title(text: Any) -> str:
    t = unicodedata.normalize("NFKC", _CTRL.sub(" ", str(text or "")))
    t = re.sub(r"[*#|]+", " ", t)
    return re.sub(r"\s+", " ", t).strip(" .,·…_-–—:;")


def roman_value(token: str) -> int | None:
    t = token.lower()
    if not t or any(c not in _ROMAN_VAL for c in t):
        return None
    total, prev = 0, 0
    for c in reversed(t):
        v = _ROMAN_VAL[c]
        total = total - v if v < prev else total + v
        prev = max(prev, v)
    return total if 0 < total < 400 else None


@dataclass
class Numbering:
    text: str               # as printed, without a trailing dot: «3.2.1», «Глава 3», «§ 5»
    rank: int               # 0 part, 1 chapter, 2 section, … (dotted depth; § = 2)
    key: tuple[int, ...]    # ordering key (chapter number, section number, …)
    kind: str               # WORD | PARA | DOTTED | ROMAN


def parse_numbering(text: str) -> tuple[Numbering | None, str]:
    """(numbering, rest of the title) of a heading or TOC entry text."""
    t = clean_title(text)
    m = RE_WORD.match(t)
    if m:
        word = m.group("word").lower()
        num = m.group("num")
        val = int(num.split(".")[0]) if num[0].isdigit() else (roman_value(num) or (ord(num.upper()) - 64))
        depth = num.count(".") if num[0].isdigit() else 0
        return (Numbering(f"{m.group('word')} {num}", _WORD_RANK.get(word, 1) + depth, (val,), "WORD"),
                m.group("rest").strip())
    m = RE_PARA.match(t)
    if m:
        parts = tuple(int(p) for p in m.group("num").split("."))
        return Numbering(f"§ {m.group('num')}", 2 + len(parts) - 1, parts, "PARA"), m.group("rest").strip()
    m = RE_DOTTED.match(t)
    if m:
        parts = tuple(int(p) for p in m.group("num").split("."))
        if parts[0] == 0 or parts[0] > 60:
            return None, t
        return Numbering(m.group("num"), len(parts), parts, "DOTTED"), m.group("rest").strip()
    m = RE_ROMAN.match(t)
    if m and roman_value(m.group("num")):
        return Numbering(m.group("num"), 1, (roman_value(m.group("num")),), "ROMAN"), m.group("rest").strip()
    return None, t


def is_keyword(title: str) -> bool:
    return bool(_KEYWORDS.match(norm_text(title)))


def title_sim(a: str, b: str) -> float:
    """Similarity of two normalised titles: containment of the shorter prefix or SequenceMatcher ratio."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) >= 8 and long_.startswith(short):
        return 0.95
    return SequenceMatcher(None, a, b[: len(a) + 20] if len(b) > len(a) + 20 else b, autojunk=False).ratio()


def match_key(numbering: str | None, title: str) -> str:
    """Normalised text for matching titles across methods: numbering stripped."""
    return norm_text(title) or norm_text(numbering)


# ============================================================================================ entries
@dataclass
class Entry:
    rank: int
    title: str
    numbering: str | None
    page: int | None
    order: int
    heading_block_id: str | None = None
    page_origin: str = ""           # TOC: LABEL | OFFSET | HEADING_OFFSET | HEADING_SHIFT
    level: int = 0
    parent: int | None = None
    end: int = 0
    key: tuple[int, ...] = ()

    @property
    def mkey(self) -> str:
        return match_key(self.numbering, self.title)

    @property
    def display(self) -> str:
        if self.numbering and norm_text(self.numbering) != norm_text(self.title):
            return f"{self.numbering} {self.title}".strip()
        return self.title


def make_entry(text: str, page: int | None, order: int, *, rank: int | None = None) -> Entry | None:
    num, rest = parse_numbering(text)
    title = clean_title(rest) or clean_title(text)
    if not title:
        return None
    return Entry(rank=rank if rank is not None else (num.rank if num else -1), title=title,
                 numbering=num.text if num else None, page=page, order=order, key=num.key if num else ())


def assign_ranks(entries: list[Entry]) -> list[Entry]:
    """Ranks of numbered entries stay; keywords (Введение, References…) take the top rank; other unnumbered entries
    are siblings of the previous entry. With chapter words present, single dotted numbers are sections (rank 2)."""
    has_chapter_word = any(e.numbering and e.numbering.split()[0].lower() in ("глава", "chapter", "лекция", "lecture")
                           for e in entries)
    for e in entries:
        if has_chapter_word and e.numbering and re.fullmatch(r"\d{1,3}", e.numbering):
            e.rank = 2
    numbered = [e.rank for e in entries if e.rank >= 0]
    top = min(numbered) if numbered else 1
    prev = top
    for e in entries:
        if e.rank < 0:
            e.rank = top if is_keyword(e.title) else prev
        prev = e.rank
    return entries


def lnds(values: list[Any]) -> list[int]:
    """Indexes of a longest non-decreasing subsequence (first-best, stable)."""
    tails: list[Any] = []
    tails_idx: list[int] = []
    prev: list[int] = [-1] * len(values)
    for i, v in enumerate(values):
        j = bisect_right(tails, v)
        if j == len(tails):
            tails.append(v)
            tails_idx.append(i)
        else:
            tails[j] = v
            tails_idx[j] = i
        prev[i] = tails_idx[j - 1] if j > 0 else -1
    out: list[int] = []
    k = tails_idx[-1] if tails_idx else -1
    while k >= 0:
        out.append(k)
        k = prev[k]
    return out[::-1]


def build_tree(entries: list[Entry], last_page: int) -> list[Entry]:
    """Parents and normalised levels (depth) from ranks; page ends by the next same-or-higher section; a parent covers
    its children. ``entries`` are in reading order with non-decreasing pages."""
    stack: list[int] = []
    for i, e in enumerate(entries):
        while stack and entries[stack[-1]].rank >= e.rank:
            stack.pop()
        e.parent = stack[-1] if stack else None
        e.level = len(stack) + 1
        stack.append(i)
    open_: list[int] = []
    child_end: dict[int, int] = {}

    def close(k: int, boundary: int) -> None:
        e = entries[k]
        e.end = min(max(e.page, boundary, child_end.get(k, e.page)), max(last_page, e.page))
        if e.parent is not None:
            child_end[e.parent] = max(child_end.get(e.parent, 0), e.end)

    for i, e in enumerate(entries):
        while open_ and entries[open_[-1]].level >= e.level:
            close(open_.pop(), e.page - 1)
        open_.append(i)
    while open_:
        close(open_.pop(), last_page)
    return entries


# ============================================================================================ outlines
def filter_outline(raw: Iterable[dict[str, Any]], page_count: int) -> tuple[list[Entry], str | None]:
    """Entries of a native outline, or ([], reason) when the outline is junk: collapsed destinations (≥ 10 entries on
    ≤ 2 or < 10 % distinct pages), more entries than 3× the pages, one level-1 bookmark per page, mostly file-name or
    page-number titles, fewer than 2 usable entries on 2 pages."""
    items = list(raw)
    if not items:
        return [], "EMPTY"
    entries: list[Entry] = []
    junk_titles = 0
    for n, it in enumerate(items):
        title = clean_title(it.get("title"))
        page = it.get("page_index")
        if not title or _JUNK_TITLE.search(title):
            junk_titles += 1
            continue
        if not isinstance(page, int) or not 1 <= page <= page_count:
            continue
        num, rest = parse_numbering(title)
        entries.append(Entry(rank=max(int(it.get("level") or 1), 1), title=clean_title(rest) or title,
                             numbering=num.text if num else None, page=page, order=n, key=num.key if num else ()))
    n_all = len(items)
    distinct = len({e.page for e in entries})
    if n_all > 3 * max(page_count, 1) and n_all >= 10:
        return [], "MORE_ENTRIES_THAN_3X_PAGES"
    if junk_titles > 0.5 * n_all:
        return [], "JUNK_TITLES"
    if n_all >= 10 and (distinct <= 2 or distinct < 0.1 * n_all):
        return [], "COLLAPSED_DESTINATIONS"
    if n_all >= 20 and n_all >= 0.8 * page_count and all(int(it.get("level") or 1) == 1 for it in items):
        pages = [it.get("page_index") for it in items if isinstance(it.get("page_index"), int)]
        steps = sum(1 for a, b in zip(pages, pages[1:]) if b - a == 1)
        if steps >= 0.8 * max(len(pages) - 1, 1):
            return [], "ONE_PER_PAGE"
    if len(entries) < 2 or distinct < 2:
        return [], "TOO_FEW_ENTRIES"
    return entries, None


# ============================================================================================ printed TOC
_PAGE_AT_END = re.compile(r"^(?P<body>.*?[^\s.·…_])[\s.·…_]*?(?:\s|[.·…_]{2,})\s*(?P<page>\d{1,4}|[ivxlcdm]{1,7})\s*$")
_BARE_PAGE = re.compile(r"^(?:\d{1,4}|[ivxlcdm]{1,7})$")
_LEADERS = re.compile(r"(?:\s?[.·…_]){3,}")
_STRUCT_WORD = re.compile(r"^(глава|chapter|часть|part|раздел|section|приложение|appendix|лекция|lecture|тема|книга|"
                          r"book|§)$", re.I)
_TABLE_VALUE = re.compile(r"\d+,\d+")
_TOC_HEADER_LINE = re.compile(r"^(?:[ivxlcdm\d]+ )?(содержание|оглавление|contents|table of contents)(?: [ivxlcdm\d]+)?$")


def toc_lines(texts: Iterable[str]) -> list[str]:
    out: list[str] = []
    for text in texts:
        for line in str(text or "").splitlines():
            line = unicodedata.normalize("NFKC", _CTRL.sub(" ", line))
            line = re.sub(r"[*#|]+", " ", line)
            line = _LEADERS.sub(" … ", line)
            line = re.sub(r"\s+", " ", line).strip()
            if not line:
                continue
            parts = re.split(r"\s+•\s+", line) if "•" in line else [line]
            out.extend(p.strip(" •") for p in parts if p.strip(" •"))
    return out


def _starts_numbered(text: str) -> bool:
    return parse_numbering(text)[0] is not None or bool(RE_NUM_ONLY.match(text))


def parse_toc(texts: Iterable[str]) -> list[tuple[str, str | None]]:
    """(entry text, printed page token or None) from TOC block texts: wrapped titles are joined, a numbering on its
    own line prefixes the next title, a bare number line is the page of the pending title, «•»-separated items split.
    Column layout (page numbers in their own block: most bare numbers follow another bare number): titles are
    assembled without them and the numbers are given in order to the page-less titles when both counts agree."""
    lines = toc_lines(texts)
    bare = [i for i, ln in enumerate(lines) if _BARE_PAGE.match(ln.replace("…", " ").strip())]
    bare_set = set(bare)
    column = len(bare) >= 5 and sum(1 for i in bare if i - 1 in bare_set) >= 0.5 * len(bare)
    entries = _assemble_toc([ln for i, ln in enumerate(lines) if not (column and i in bare_set)])
    if column:
        numbers = [lines[i].replace("…", " ").strip() for i in bare]
        pageless = [k for k, (_, p) in enumerate(entries) if p is None]
        if pageless and abs(len(numbers) - len(pageless)) <= max(1, 0.1 * len(pageless)):
            for k, num in zip(pageless, numbers):
                entries[k] = (entries[k][0], num)
    return entries


def _assemble_toc(lines: list[str]) -> list[tuple[str, str | None]]:
    entries: list[tuple[str, str | None]] = []
    buf: list[str] = []
    awaiting: int | None = None     # entry closed by a leader whose page may follow on the next line

    def flush() -> None:
        if buf:
            entries.append((" ".join(buf), None))
            buf.clear()

    for line in lines:
        body_line = re.sub(r"\s+", " ", line.replace("…", " ")).strip()
        normed = norm_text(body_line)
        if _TOC_HEADER.match(normed) or _TOC_HEADER_LINE.match(normed):
            continue
        if _BARE_PAGE.match(body_line):
            if buf:
                entries.append((" ".join(buf), body_line))
                buf.clear()
            elif awaiting is not None:
                entries[awaiting] = (entries[awaiting][0], body_line)
            awaiting = None
            continue
        awaiting = None
        m = _PAGE_AT_END.match(line)
        body = clean_title(m.group("body")) if m else ""
        if m and body and not RE_NUM_ONLY.match(body) and not _STRUCT_WORD.match(body) \
                and not _TOC_HEADER.match(norm_text(body)):
            page = m.group("page")
            if page.isalpha() and roman_value(page) is None:
                m = None
        else:
            m = None
        if m:
            if buf and (not _starts_numbered(body) or all(RE_NUM_ONLY.match(b) for b in buf)):
                body = " ".join([*buf, body])
                buf.clear()
            else:
                flush()
            entries.append((body, m.group("page")))
            continue
        text = clean_title(body_line)
        if not text:
            continue
        if buf and _starts_numbered(text) and not all(RE_NUM_ONLY.match(b) for b in buf):
            flush()
        buf.append(text)
        if line.rstrip().endswith("…"):     # a leader without its page: the title is complete (column layout)
            flush()
            awaiting = len(entries) - 1
        elif len(buf) > 4:          # a paragraph, not a title: drop it
            buf.clear()
    flush()
    return entries


@dataclass
class LabelMap:
    direct: dict[str, int]
    offset: int | None
    n_labelled: int


def label_map(pages: list[tuple[int, list[str], str]]) -> LabelMap:
    """Printed label → physical page (CONSISTENT_SEQUENCE labels only; a label on two pages is ambiguous) and the
    dominant arabic offset ``page_index - label`` (≥ 3 pages and ≥ 30 % of arabic labels)."""
    seen: dict[str, list[int]] = defaultdict(list)
    offsets: Counter[int] = Counter()
    n = 0
    for page_index, labels, status in pages:
        if not labels:
            continue
        n += 1
        for lab in labels:
            lab = str(lab).strip().lower()
            if status == "CONSISTENT_SEQUENCE":
                seen[lab].append(page_index)
            if lab.isdigit():
                offsets[page_index - int(lab)] += 1
    direct = {k: v[0] for k, v in seen.items() if len(v) == 1}
    offset = None
    if offsets:
        off, cnt = offsets.most_common(1)[0]
        if cnt >= 3 and cnt >= 0.3 * sum(offsets.values()):
            offset = off
    return LabelMap(direct, offset, n)


def toc_candidates(texts: list[str], labels: LabelMap, page_count: int,
                   headings: list[tuple[int, str, str]], stats: Counter) -> list[Entry]:
    """Printed-TOC entries with physical pages (``page_origin`` LABEL / OFFSET / HEADING_OFFSET)."""
    raw = parse_toc(texts)
    entries: list[Entry] = []
    for n, (text, token) in enumerate(raw):
        e = make_entry(text, None, n)
        if e is None:
            continue
        if token is None and not (e.numbering or is_keyword(e.title)):
            continue                        # a stray page-less line
        if not (e.numbering or is_keyword(e.title)) and (_TABLE_VALUE.search(e.title) or len(e.title.split()) > 30):
            continue                        # table rows / paragraphs typed as TOC by the layout model
        e.page_origin = token or ""
        entries.append(e)
    stats["toc_entries_parsed"] += len(entries)
    if not entries:
        return []
    # dominant offset of TOC titles found as headings (for sources without usable labels)
    head_by_key: dict[str, list[int]] = defaultdict(list)
    for page, key, _ in headings:
        if key:
            head_by_key[key[:60]].append(page)
    h_offsets: Counter[int] = Counter()
    for e in entries:
        if e.page_origin.isdigit() and e.mkey:
            for p in head_by_key.get(e.mkey[:60], [])[:3]:
                h_offsets[p - int(e.page_origin)] += 1
    h_offset = None
    if h_offsets:
        off, cnt = h_offsets.most_common(1)[0]
        if cnt >= 2 and cnt >= 0.5 * sum(h_offsets.values()):
            h_offset = off
    unmapped: set[int] = set()
    for e in entries:
        token, e.page_origin = e.page_origin, ""
        if not token:
            continue
        unmapped.add(id(e))          # a printed page that cannot be mapped is dropped, never guessed
        low = token.lower()
        if low in labels.direct:
            e.page, e.page_origin = labels.direct[low], "LABEL"
        elif token.isdigit() and labels.offset is not None:
            e.page, e.page_origin = int(token) + labels.offset, "OFFSET"
        elif token.isdigit() and h_offset is not None:
            e.page, e.page_origin = int(token) + h_offset, "HEADING_OFFSET"
        if e.page is not None and not 1 <= e.page <= page_count:
            e.page, e.page_origin = None, ""
        if e.page is not None:
            unmapped.discard(id(e))
    # a page-less chapter title (no printed page at all) takes the page of the next entry that has one
    nxt = None
    for e in reversed(entries):
        if e.page is None and nxt is not None and id(e) not in unmapped:
            e.page, e.page_origin = nxt, "NEXT_ENTRY"
        elif e.page is not None:
            nxt = e.page
    stats["toc_entries_unmapped_page"] += len(unmapped)
    mapped = [e for e in entries if e.page is not None]
    stats["toc_entries_mapped"] += len(mapped)
    for e in mapped:
        stats[f"toc_page_{e.page_origin.lower()}"] += 1
    return assign_ranks(mapped)


# ============================================================================================ headings
@dataclass
class Block:
    block_id: str
    page: int
    order: int
    block_type: str
    text: str

    @property
    def key(self) -> str:
        return norm_text(self.text)


def heading_is_noise(text: str) -> bool:
    t = clean_title(text)
    letters = sum(ch.isalpha() for ch in t)
    if letters < 2 or len(t) > 250:
        return True
    return bool(_NOISE.match(norm_text(t)))


def _dedupe_running(blocks: list[Block]) -> list[Block]:
    """Drop repeats of a heading text seen on ≥ 4 pages (running heads typed as headings), except keywords."""
    pages_of: dict[str, set[int]] = defaultdict(set)
    for b in blocks:
        pages_of[b.key].add(b.page)
    seen: set[str] = set()
    out = []
    for b in blocks:
        if len(pages_of[b.key]) >= 4 and not is_keyword(b.text):
            if b.key in seen:
                continue
            seen.add(b.key)
        out.append(b)
    return out


def numbering_candidates(headings: list[Block], stats: Counter) -> list[Entry]:
    """Numbered headings in a consistent order (longest non-decreasing run of numbering keys); keyword headings
    (Введение, Заключение, References…) join as top-level entries."""
    entries: list[Entry] = []
    families: dict[str, list[int]] = defaultdict(list)
    for b in headings:
        e = make_entry(b.text, b.page, b.order)
        if e is None:
            continue
        e.heading_block_id = b.block_id
        if e.numbering:
            num, _ = parse_numbering(b.text)
            family = num.kind + (":" + num.text.split()[0].lower() if num.kind == "WORD" else "")
            families[family].append(len(entries))
            entries.append(e)
        elif is_keyword(e.title):
            entries.append(e)
    numbered = [i for idx in families.values() for i in idx]
    if len(numbered) < 3:
        return []
    keep: set[int] = set()
    for idx in families.values():   # each numbering family (Глава n, § n, n.m, I.) must grow on its own
        keep.update(idx[i] for i in lnds([entries[k].key + (0,) * (6 - len(entries[k].key)) for k in idx]))
    stats["numbering_dropped_out_of_order"] += len(numbered) - len(keep)
    seen_num: set[str] = set()
    out = []
    for i, e in enumerate(entries):
        if e.numbering:
            if i not in keep or e.numbering in seen_num:
                continue
            seen_num.add(e.numbering)
        out.append(e)
    return assign_ranks(out)


def layout_candidates(titles: list[Block], headings: list[Block]) -> list[Entry]:
    """TITLE blocks (merged when consecutive on a page) at rank 1, HEADING blocks at rank 2 (+ numbering depth)."""
    merged: list[tuple[Block, str]] = []
    for b in titles:
        if merged and merged[-1][0].page == b.page and b.order - merged[-1][0].order <= 3 \
                and parse_numbering(b.text)[0] is None:
            prev, text = merged[-1]
            merged[-1] = (Block(prev.block_id, prev.page, b.order, "TITLE", prev.text), f"{text} {b.text}")
            continue
        merged.append((b, b.text))
    items: list[tuple[int, int, Entry]] = []
    for b, text in merged:      # a numbered TITLE block («II. Метод …») is a part of the title above it
        e = make_entry(text, b.page, b.order, rank=1 if parse_numbering(text)[0] is None else 2)
        if e is not None and not heading_is_noise(text):
            e.heading_block_id = b.block_id
            items.append((b.page, b.order, e))
    for b in headings:
        e = make_entry(b.text, b.page, b.order)
        if e is None:
            continue
        e.heading_block_id = b.block_id
        num, _ = parse_numbering(b.text)
        e.rank = 1 + (len(num.key) if num and num.kind in ("DOTTED", "PARA") else 1)
        items.append((b.page, b.order, e))
    items.sort(key=lambda x: (x[0], x[1]))
    return [e for _, _, e in items]


# ============================================================================================ matching
def find_heading(entry: Entry, blocks_on_page: list[Block]) -> Block | None:
    target = entry.mkey
    full = norm_text(entry.display)
    best, best_sim = None, 0.0
    for b in blocks_on_page:
        key = b.key
        if not key:
            continue
        sim = max(title_sim(target, key), title_sim(full, key))
        if sim > best_sim:
            best, best_sim = b, sim
    return best if best_sim >= TITLE_SIM else None


def agreement(entries: list[Entry], other: list[Entry], tol: int = 1) -> tuple[set[int], int]:
    """Indexes of ``entries`` confirmed by ``other`` (similar title or equal numbering within ±tol pages) and the
    number of conflicts (the same normalised title, first 40 characters, found only more than 2 pages away)."""
    by_page: dict[int, list[Entry]] = defaultdict(list)
    pages_of_key: dict[str, list[int]] = defaultdict(list)
    for o in other:
        if o.page is not None:
            by_page[o.page].append(o)
            if len(o.mkey) >= 6:
                pages_of_key[o.mkey[:40]].append(o.page)
    agree: set[int] = set()
    conflicts = 0
    for i, e in enumerate(entries):
        k = e.mkey
        hit = False
        for p in range(e.page - tol, e.page + tol + 1):
            for o in by_page.get(p, []):
                if title_sim(k, o.mkey) >= TITLE_SIM or (e.numbering and e.numbering == o.numbering):
                    hit = True
                    break
            if hit:
                break
        if hit:
            agree.add(i)
        elif len(k) >= 6 and pages_of_key.get(k[:40]) and min(abs(p - e.page) for p in pages_of_key[k[:40]]) > 2:
            conflicts += 1
    return agree, conflicts


# ============================================================================================ build
def _rows(con, sql: str, params: list[Any] | None = None) -> list[tuple]:
    return con.execute(sql, params or []).fetchall()


def build(con, *, outlines: dict[str, list[dict[str, Any]]] | None = None,
          stats: dict[str, Any] | None = None) -> dict[str, pa.Table]:
    """``sections`` and ``section_pages`` over the canonical rows of ``con`` (DuckDB)."""
    st: Counter = Counter()
    outlines = outlines or {}
    pages_by_src: dict[str, list[tuple[int, str, list[str], str]]] = defaultdict(list)
    for sid, pid, idx, labels, status in _rows(con, "SELECT source_id, page_id, page_index, printed_page_labels, "
                                                    "printed_label_status FROM canonical.pages "
                                                    "ORDER BY source_id, page_index"):
        pages_by_src[sid].append((int(idx), pid, list(labels or []), status or "NONE"))
    fmt = {sid: f for sid, f in _rows(con, "SELECT source_id, format_detected FROM canonical.documents")}
    klass = {sid: c for sid, c in _rows(con, "SELECT source_id, source_class_raw FROM canonical.sources")}
    work_primary: dict[str, str] = {}
    foreign: dict[str, list[tuple[int, int, str | None]]] = defaultdict(list)
    for sid, wid, ltype, prim, p0, p1 in _rows(con, "SELECT source_id, work_id, link_type, is_primary, page_start, "
                                                    "page_end FROM canonical.source_work_links "
                                                    "WHERE curation_status IS DISTINCT FROM 'REJECTED'"):
        if ltype == "FOREIGN_CONTENT" and p0 is not None and p1 is not None:
            foreign[sid].append((int(p0), int(p1), wid))
        elif prim and wid:
            work_primary.setdefault(sid, wid)
    work_title = {wid: t for wid, t in _rows(con, "SELECT work_id, title FROM canonical.works")}
    blocks: dict[str, dict[str, list[Block]]] = defaultdict(lambda: defaultdict(list))
    for sid, bid, btype, idx, order, text in _rows(
            con, "SELECT b.source_id, b.object_id, b.block_type, p.page_index, b.reading_order, b.text "
                 "FROM canonical.blocks b JOIN canonical.pages p ON p.page_id = b.page_id "
                 "WHERE b.is_primary_layer AND b.block_type IN ('HEADING', 'TITLE', 'TABLE_OF_CONTENTS') "
                 "ORDER BY b.source_id, p.page_index, b.reading_order"):
        blocks[sid][btype].append(Block(bid, int(idx), int(order), btype, text or ""))

    chosen: dict[str, tuple[str, list[Entry], dict[str, list[Entry]]]] = {}
    for sid, pages in pages_by_src.items():
        n_pages = pages[-1][0]
        cand: dict[str, list[Entry]] = {}
        src_blocks = blocks.get(sid, {})
        headings = [b for b in src_blocks.get("HEADING", []) if not heading_is_noise(b.text)]
        headings = _dedupe_running(headings)
        titles = src_blocks.get("TITLE", [])
        omethod = OUTLINE_METHOD_BY_FORMAT.get(fmt.get(sid, ""), "PDF_OUTLINE")
        if sid in outlines:
            ents, reason = filter_outline(outlines[sid], n_pages)
            if ents:
                cand[omethod] = ents
            else:
                st[f"outline_rejected_{reason.lower()}"] += 1
        tocs = src_blocks.get("TABLE_OF_CONTENTS", [])
        if tocs:
            lm = label_map([(p[0], p[2], p[3]) for p in pages])
            ents = toc_candidates([b.text for b in tocs], lm, n_pages,
                                  [(b.page, match_key(*_split(b.text)), b.block_id) for b in headings + titles], st)
            keep = lnds([e.page for e in ents])
            if len(keep) >= 3 and len(keep) >= 0.5 * len(ents):
                cand["PRINTED_TOC"] = [ents[i] for i in keep]
            elif ents:
                st["toc_rejected_inconsistent"] += 1
        if klass.get(sid) not in CONTAINER_CLASSES:
            ents = numbering_candidates(headings, st)
            n_num = sum(1 for e in ents if e.numbering)
            if n_num >= 3 and (n_num >= 10 or n_num >= 0.2 * len(headings)):
                cand["HEADING_NUMBERING"] = ents
        ents = layout_candidates(titles, headings)
        if ents:
            cand["HEADING_LAYOUT"] = ents
        method = next((m for m in METHODS if m in cand), "WHOLE_SOURCE")
        chosen[sid] = (method, cand.get(method, []), cand)

    # candidate blocks on the start pages (±MAX_TOC_SHIFT for TOC entries) to anchor headings
    wanted: set[tuple[str, int]] = set()
    for sid, (method, ents, _) in chosen.items():
        if method in OUTLINE_METHODS or method == "PRINTED_TOC":
            shift = MAX_TOC_SHIFT if method == "PRINTED_TOC" else 0
            for e in ents:
                for p in range(e.page - shift, e.page + shift + 1):
                    wanted.add((sid, p))
    on_page: dict[tuple[str, int], list[Block]] = defaultdict(list)
    if wanted:
        want = pa.table({"source_id": [w[0] for w in wanted], "page_index": pa.array([w[1] for w in wanted],
                                                                                      pa.int32())})
        con.register("_nav_wanted_pages", want)
        try:
            for sid, bid, btype, idx, order, text in _rows(
                    con, "SELECT b.source_id, b.object_id, b.block_type, p.page_index, b.reading_order, b.text "
                         "FROM canonical.blocks b JOIN canonical.pages p ON p.page_id = b.page_id "
                         "JOIN _nav_wanted_pages w ON w.source_id = p.source_id AND w.page_index = p.page_index "
                         "WHERE b.is_primary_layer AND (b.block_type IN ('HEADING', 'TITLE') OR "
                         "(b.block_type IN ('TEXT', 'PAGE_HEADER') AND b.char_count <= 300)) "
                         "ORDER BY b.source_id, p.page_index, b.reading_order"):
                on_page[(sid, int(idx))].append(Block(bid, int(idx), int(order), btype, text or ""))
        finally:
            con.unregister("_nav_wanted_pages")

    sec_rows: dict[str, list[Any]] = {f.name: [] for f in SECTIONS_SCHEMA}
    sp_rows: dict[str, list[Any]] = {f.name: [] for f in SECTION_PAGES_SCHEMA}
    per_source: dict[str, dict[str, Any]] = {}
    for sid, (method, ents, cand) in sorted(chosen.items()):
        pages = pages_by_src[sid]
        page_id = {p[0]: p[1] for p in pages}
        n_pages = pages[-1][0]
        if method in OUTLINE_METHODS or method == "PRINTED_TOC":
            for e in ents:
                b = find_heading(e, on_page.get((sid, e.page), []))
                if b is None and method == "PRINTED_TOC":
                    for d in (1, -1, 2, -2):
                        b = find_heading(e, on_page.get((sid, e.page + d), []))
                        if b is not None:
                            e.page, e.page_origin = e.page + d, "HEADING_SHIFT"
                            st["toc_page_shifted_to_heading"] += 1
                            break
                if b is not None:
                    e.heading_block_id = b.block_id
            keep = lnds([e.page for e in ents])
            st[f"dropped_non_monotonic_{method.lower()}"] += len(ents) - len(keep)
            ents = [ents[i] for i in keep]
            anchored = sum(1 for e in ents if e.heading_block_id)
            fallback = next((m for m in METHODS[METHODS.index("PRINTED_TOC") + 1:] if m in cand), None)
            if method == "PRINTED_TOC" and anchored < MIN_TOC_ANCHORED * len(ents) and fallback:
                st["toc_rejected_unanchored"] += 1       # the printed pages do not land on the headings
                method, ents = fallback, cand[fallback]
        if method == "WHOLE_SOURCE" or not ents:
            method = "WHOLE_SOURCE"
            first_title = next((b for b in blocks.get(sid, {}).get("TITLE", []) if b.page <= 2), None)
            title = clean_title(first_title.text) if first_title else ""
            title = title or clean_title(work_title.get(work_primary.get(sid, ""), "")) or sid
            ents = [Entry(rank=1, title=title, numbering=None, page=1, order=0,
                          heading_block_id=first_title.block_id if first_title else None)]
        ents = sorted(ents, key=lambda e: (e.page, 0))  # stable: reading order kept within a page
        build_tree(ents, n_pages)
        # agreement with the other methods
        agree_n: Counter[int] = Counter()
        src_conflicts = 0
        for m, other in cand.items():
            if m == method:
                continue
            agree, conflicts = agreement(ents, other)
            for i in agree:
                agree_n[i] += 1
            src_conflicts += conflicts
            st[f"agreement_{method.lower()}_vs_{m.lower()}_sections"] += len(ents)
            st[f"agreement_{method.lower()}_vs_{m.lower()}_agreeing"] += len(agree)
        ids: list[str] = []
        for i, e in enumerate(ents):
            ids.append(section_id(sid, method, e.level, i + 1, e.title))
        paths: list[str] = []
        for i, e in enumerate(ents):
            paths.append(e.display if e.parent is None else f"{paths[e.parent]} › {e.display}")
        base = BASE_CONFIDENCE[method]
        for i, e in enumerate(ents):
            conf = base + 0.1 * agree_n[i]
            if e.heading_block_id and method in OUTLINE_METHODS | {"PRINTED_TOC"}:
                conf += 0.05
            if e.page_origin in ("HEADING_OFFSET", "NEXT_ENTRY"):
                conf -= 0.1
            wid = work_primary.get(sid)
            for p0, p1, fw in foreign.get(sid, []):
                if p0 <= e.page <= p1:
                    wid = fw
            end = min(max(e.end, e.page), n_pages)
            sec_rows["section_id"].append(ids[i])
            sec_rows["source_id"].append(sid)
            sec_rows["work_id"].append(wid)
            sec_rows["parent_section_id"].append(ids[e.parent] if e.parent is not None else None)
            sec_rows["level"].append(e.level)
            sec_rows["ordinal"].append(i + 1)
            sec_rows["numbering"].append(e.numbering)
            sec_rows["title"].append(e.title)
            sec_rows["title_path"].append(paths[i])
            sec_rows["page_start_id"].append(page_id.get(e.page))
            sec_rows["page_end_id"].append(page_id.get(end))
            sec_rows["page_start_index"].append(e.page)
            sec_rows["page_end_index"].append(end)
            sec_rows["method"].append(method)
            sec_rows["confidence"].append(round(min(max(conf, 0.05), 0.99), 3))
            sec_rows["heading_block_id"].append(e.heading_block_id)
            sec_rows["rule_version"].append(RULE_VERSION)
        # deepest sections of every covered page
        deepest: dict[int, tuple[int, list[int]]] = {}
        for i, e in enumerate(ents):
            for p in range(e.page, min(max(e.end, e.page), n_pages) + 1):
                lvl, lst = deepest.get(p, (0, []))
                if e.level > lvl:
                    deepest[p] = (e.level, [i])
                elif e.level == lvl:
                    lst.append(i)
        for p in sorted(deepest):
            if p not in page_id:
                continue
            for i in deepest[p][1]:
                sp_rows["section_id"].append(ids[i])
                sp_rows["page_id"].append(page_id[p])
                sp_rows["source_id"].append(sid)
                sp_rows["page_index"].append(p)
                sp_rows["rule_version"].append(RULE_VERSION)
        per_source[sid] = {"method": method, "sections": len(ents), "covered_pages": len(deepest),
                           "pages": len(pages), "conflicts": src_conflicts,
                           "methods_available": sorted(cand), "levels": max(e.level for e in ents)}
        st["conflicts"] += src_conflicts
    sections = pa.Table.from_pydict(sec_rows, schema=SECTIONS_SCHEMA)
    section_pages = pa.Table.from_pydict(sp_rows, schema=SECTION_PAGES_SCHEMA)
    if stats is not None:
        stats.update(summarize(per_source, sections, section_pages, st))
    return {"sections": sections, "section_pages": section_pages}


def _split(text: str) -> tuple[str | None, str]:
    num, rest = parse_numbering(text)
    return (num.text if num else None), (clean_title(rest) or clean_title(text))


# ============================================================================================ checks
def check(sections: pa.Table, section_pages: pa.Table | None = None) -> dict[str, int]:
    """Invariants of the tree: children inside the parent, siblings disjoint (one shared boundary page allowed and
    counted), ranges ≥ 1 page, unique ids, section_pages rows point to existing sections."""
    rows = sections.to_pylist()
    by_id = {r["section_id"]: r for r in rows}
    out = Counter({"sections": len(rows), "duplicate_ids": len(rows) - len(by_id)})
    siblings: dict[tuple[str, str | None], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if r["page_end_index"] < r["page_start_index"]:
            out["empty_range"] += 1
        p = by_id.get(r["parent_section_id"]) if r["parent_section_id"] else None
        if r["parent_section_id"] and p is None:
            out["missing_parent"] += 1
        if p is not None:
            if not (p["page_start_index"] <= r["page_start_index"] and r["page_end_index"] <= p["page_end_index"]):
                out["child_outside_parent"] += 1
            if r["level"] != p["level"] + 1:
                out["level_not_parent_plus_one"] += 1
        elif r["level"] != 1:
            out["root_not_level_1"] += 1
        siblings[(r["source_id"], r["parent_section_id"])].append(r)
    for sib in siblings.values():
        sib.sort(key=lambda r: r["ordinal"])
        for a, b in zip(sib, sib[1:]):
            if a["page_end_index"] >= b["page_start_index"]:
                if a["page_end_index"] == b["page_start_index"]:
                    out["sibling_shared_boundary_page"] += 1
                else:
                    out["sibling_overlap"] += 1
    if section_pages is not None:
        ids = set(by_id)
        out["section_pages_orphans"] = sum(1 for s in section_pages.column("section_id").to_pylist() if s not in ids)
    return dict(out)


def start_page_check(con, sections: pa.Table) -> dict[str, dict[str, Any]]:
    """QA without a model: per method, the share of sections whose title (normalised, first 30 characters) occurs in
    the primary text of the start page, or whose heading block was found there."""
    rows = sections.select(["section_id", "source_id", "page_start_id", "title", "numbering", "heading_block_id",
                            "method"]).to_pylist()
    want = pa.table({"page_id": sorted({r["page_start_id"] for r in rows if r["page_start_id"]})})
    con.register("_nav_start_pages", want)
    try:
        text = {pid: norm_text(t) for pid, t in con.execute(
            "SELECT p.page_id, p.normalized_text FROM canonical.pages p JOIN _nav_start_pages w USING (page_id)"
        ).fetchall()}
    finally:
        con.unregister("_nav_start_pages")
    out: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        c = out[r["method"]]
        c["sections"] += 1
        key = norm_text(r["title"])[:30]
        page = text.get(r["page_start_id"], "")
        if r["heading_block_id"]:
            c["heading_block"] += 1
        if key and key in page:
            c["title_in_page_text"] += 1
        if r["heading_block_id"] or (key and key in page):
            c["found"] += 1
    return {m: {**dict(c), "found_share": round(c["found"] / c["sections"], 4)} for m, c in sorted(out.items())}


def summarize(per_source: dict[str, dict[str, Any]], sections: pa.Table, section_pages: pa.Table,
              st: Counter) -> dict[str, Any]:
    by_method = Counter(v["method"] for v in per_source.values())
    levels = Counter(sections.column("level").to_pylist())
    n_pages = sum(v["pages"] for v in per_source.values())
    covered = len(set(section_pages.column("page_id").to_pylist()))
    return {
        "rule_version": RULE_VERSION,
        "sources": len(per_source),
        "sources_by_method": dict(sorted(by_method.items())),
        "sections": sections.num_rows,
        "sections_by_method": dict(sorted(Counter(sections.column("method").to_pylist()).items())),
        "sections_by_level": {str(k): v for k, v in sorted(levels.items())},
        "section_pages": section_pages.num_rows,
        "pages_total": n_pages,
        "pages_covered": covered,
        "page_coverage": round(covered / n_pages, 4) if n_pages else 0.0,
        "heading_block_found": sum(1 for h in sections.column("heading_block_id").to_pylist() if h),
        "sources_with_conflicts": sum(1 for v in per_source.values() if v["conflicts"]),
        "counters": dict(sorted(st.items())),
        "checks": check(sections, section_pages),
        "per_source": per_source,
    }
