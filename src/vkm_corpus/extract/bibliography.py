"""Reference lists → BibliographyEntry (task §24; C design §6.5; D contract §1.16; CP-38).

Input: the canonical block rows of one source. Only the primary text layer is read. Output: entries with the printed
label, the blocks they were assembled from, the entry text as extracted and a rule-based parse (authors, title, year,
venue, volume, issue, pages, URL, DOI, ISBN-13). The parse is a DERIVED guess of the text: ``parse_method`` names the
rule set and ``parse_confidence`` the share of core fields found. No model is called. An entry keeps the text origin,
text layer and region of its first fragment (NATIVE / EMBEDDED_OCR / OCR).

**Zones.** A reference zone is a run of blocks in reading order, pages in order. The layout labelled these blocks
REFERENCE_LIST, and a heading («Список литературы», «Литература», «References», …) may open the run.
- Page furniture (running heads, footers, page numbers, footnotes) does not interrupt a zone.
- Other blocks close it. Two exceptions come from layout noise: a TEXT block between reference blocks, and a TEXT
  block that carries the next expected printed number.
- A heading followed only by TEXT blocks opens a zone for the blocks that look like entries.

**Segmentation.**
- Numbered lists («12.», «[12]», «53568.»): an entry starts at each plausible next number (last + 1 … last + 5, or a
  restart at 1). Every other line continues the current entry, across blocks and pages.
- Unnumbered lists (author–year, alphabetical): an entry starts at any of:
  - a blank line (OCR);
  - a block that starts at the left edge of its column with an author-like token (hanging indent);
  - a line with an author pattern right after a line that completed an entry.
- Dropped fragments: no year and no bibliographic marker; imprint or copyright text; bare headings.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from vkm_corpus.contracts.text_rules import normalize_text_v1
from vkm_corpus.ids import normalize_doi, normalize_isbn, parse_page_id

EXTRACTOR_ID = "bib-segmenter"
EXTRACTOR_VERSION = "0.1.0"
RULES_VERSION = "bib_rules_v1"

# the rule configuration: its hash is the raw_config_hash of every entry (part of the object id, H-14) — change it
# (or the rules) only together with a new extraction_generation
CONFIG: dict[str, Any] = {
    "rules": RULES_VERSION,
    "zone_block_types": ["REFERENCE_LIST"],
    "admitted_in_zone": ["TEXT", "LIST_ITEM", "ABSTRACT", "SIDE_TEXT"],
    "furniture": ["PAGE_HEADER", "PAGE_FOOTER", "PAGE_NUMBER", "FOOTNOTE", "FORMULA_NUMBER"],
    "label_step_max": 5,
    "edge_tolerance_pt": 3.0,
    "column_window_pt": 40.0,
    "min_entry_chars": 20,
    "year_range": [1800, 2030],
}
CONFIG_HASH = hashlib.sha256(json.dumps(CONFIG, sort_keys=True, ensure_ascii=False,
                                        separators=(",", ":")).encode("utf-8")).hexdigest()

_FURNITURE = frozenset(CONFIG["furniture"])
_ADMITTED = frozenset(CONFIG["admitted_in_zone"])
_NOT_HEADING = frozenset({"TABLE_OF_CONTENTS", "CAPTION", *CONFIG["furniture"]})

# ------------------------------------------------------------------------------------------------ patterns
BIB_HEADING = re.compile(
    r"^\s*(?:(?:глава|chapter|раздел)?\s*[\dIVX]+(?:\.\d+)*\.?\s+)?"
    r"(?:список\s+(?:использованн\w+\s+|цитированн\w+\s+|рекомендуем\w+\s+|основн\w+\s+)?(?:литературы|источников)"
    r"(?:\s+и\s+источников)?"
    r"|(?:рекомендуем\w+\s+|использованн\w+\s+|цитированн\w+\s+|основн\w+\s+|дополнительн\w+\s+)?литература"
    r"(?:\s+к\s+(?:главе|разделу)\s+[\dIVX]+)?"
    r"|библиографическ\w+\s+список|библиография|список\s+публикаций|cписок\s+литературы"
    r"|references(?:\s+cited)?|bibliography|literature(?:\s+cited)?|literatur(?:verzeichnis)?|quellenverzeichnis)"
    r"\s*[:.]?\s*$", re.IGNORECASE)
LABEL = re.compile(r"^\s*(?:\[(\d{1,5})\]|(\d{1,5})\s?[.)](?![\d]))\s*")

_RU_SURNAME = r"[А-ЯЁ][а-яё]+(?:[-‐][А-ЯЁ]?[а-яё]+){0,3}"
_LAT_UP = "A-ZÀ-ÖØ-Þ"
_LAT_ANY = r"A-Za-zÀ-ÖØ-öø-ÿ'’\-¨´`"
_EN_SURNAME = rf"(?:(?:van|von|de|der|den|du|da|la|le|di|mc|mac|o')\s?)?[{_LAT_UP}][{_LAT_ANY}]+"
_RU_INIT = r"(?:[А-ЯЁ]\s?\.\s?-?){1,2}"
_EN_INIT = r"(?:[A-Z]\.\s?-?){1,3}(?:[A-Z](?=[,;]))?"
_SUFFIX = r"(?:,?\s(?:Jr|Sr|II|III|IV)\.?(?=[\s,;]))?"
AUTHOR_PATTERNS: dict[str, str] = {
    # Фамилия И.О. | Фамилия, И. О.
    "ru_si": rf"{_RU_SURNAME},?\s{{0,2}}{_RU_INIT}",
    # И.О. Фамилия
    "ru_is": rf"{_RU_INIT}\s?{_RU_SURNAME}",
    # SURNAME, I. J.
    "en_caps": rf"[{_LAT_UP}][{_LAT_UP}'’\-]{{2,}}(?:\s[{_LAT_UP}][{_LAT_UP}'’\-]{{2,}})?,\s?{_EN_INIT}{_SUFFIX}",
    # Surname, I. J.
    "en_si": rf"{_EN_SURNAME}(?:\s[{_LAT_UP}][a-zà-öø-ÿ'’\-]+)?,\s?{_EN_INIT}{_SUFFIX}",
    # Surname I.J.
    "en_si2": rf"{_EN_SURNAME}\s{_EN_INIT}{_SUFFIX}",
    # Surname IJ (author-year, Springer)
    "en_spr": rf"{_EN_SURNAME}(?:\s[{_LAT_UP}][a-zà-öø-ÿ'’\-]+)?\s(?:[A-Z](?:\s[A-Z]){{1,2}}|[A-Z]{{1,3}})"
              rf"(?=[,.(\s;:]|$)",
    # I. J. Surname
    "en_is": rf"{_EN_INIT}\s?{_EN_SURNAME}{_SUFFIX}",
}
# a list keeps its script family but may mix forms («Adler, R. J., and J. E. Taylor»)
_FAMILY = {"ru_si": ("ru_si", "ru_is"), "ru_is": ("ru_is", "ru_si"),
           "en_caps": ("en_caps", "en_is"), "en_si": ("en_si", "en_si2", "en_is"),
           "en_si2": ("en_si2", "en_si", "en_is"), "en_spr": ("en_spr",), "en_is": ("en_is", "en_si", "en_si2")}
_AUTHOR_SEP = r"(?:\s*[,;]\s*(?:(?:и|and|&|und)\s+)?|\s*(?:и|and|&|und)\s+)"
_ET_AL = r"(?:\s*,?\s*(?:и\s+др\.?|et\s+al\.?|и\s+др))?"
AUTHOR_LIST = {k: re.compile(rf"^(?:{AUTHOR_PATTERNS[k]})(?:{_AUTHOR_SEP}(?:"
                             + "|".join(AUTHOR_PATTERNS[f] for f in fam) + rf"))*{_ET_AL}")
               for k, fam in _FAMILY.items()}
AUTHOR_ONE = {k: re.compile("|".join(AUTHOR_PATTERNS[f] for f in fam)) for k, fam in _FAMILY.items()}
AUTHOR_START = re.compile("^(?:" + "|".join(AUTHOR_PATTERNS[k] for k in ("ru_si", "ru_is", "en_caps", "en_si",
                                                                         "en_si2", "en_spr")) + ")")

_INLINE_START = re.compile(
    r"(?:(?<=[\d)\]]\.)|(?<=[^\W\d_]{2}\.))\s+(?=(?:" + AUTHOR_PATTERNS["en_si"] + "|" + AUTHOR_PATTERNS["en_caps"]
    + "|" + AUTHOR_PATTERNS["ru_si"] + r")[\s,])"
    # Springer author–year: «… 43:4553–4576. https://doi.org/… Murguía DI, Bringezu S (2016) …»
    r"|(?<=\S)\s+(?=[A-ZÀ-Þ][\w'’\-]+\s[A-Z]{1,3}(?:,\s[A-ZÀ-Þ][\w'’\-]+\s[A-Z]{1,3}){0,8}\s\((?:1[89]|20)\d\d[a-z]?\))")

YEAR = re.compile(r"(?<![\d./\-–—])(1[89]\d\d|20[0-2]\d|2030)(?!\d)([a-zа-я])?(?![\d])")
YEAR_PAREN = re.compile(r"\((1[89]\d\d|20[0-2]\d|2030)[a-zа-я]?(?:,\s*[^)]{0,20})?\)")
DOI = re.compile(r"(?:doi\.org/|doi[:\s]\s*|DOI[:\s]\s*)?(10\.\d{4,9}/[^\s\"<>«»]+)", re.IGNORECASE)
ISBN = re.compile(r"ISBN(?:[-‐\s]?1[03])?[:\s]*((?:[0-9Xx][\s\-‐–]?){9,16}[0-9Xx])", re.IGNORECASE)
URL = re.compile(r"(?:https?://|www\.)[^\s<>«»\"]+", re.IGNORECASE)
PAGES_LOC = re.compile(r"(?:\b[СCcс]\.|\bсс\.|\bpp?\.|\bS\.|\bP\.|\bстр\.)\s?(\d+\s*(?:[-–—]\s*\d+)?)(?![\d])")
PAGES_TAIL = re.compile(r"[:,]\s?(\d+\s*[-–—]\s*\d+)\s*\.?\s*$")
VOLUME = re.compile(r"(?:\b[ТTт]\.|\bтом\b|\bVol\.?|\bvol\.?|\bV\.|\bBd\.)\s?(\d+|[IVXLC]+)\b")
ISSUE = re.compile(r"(?:№|\bNo\.?|\bN\s|\bВып\.|\bвып\.|\bIss\.?|\bissue\b|\bH\.|\bHeft\b|\bNr\.)\s?(\d+(?:\s*[-–/]\s*\d+)?)")
SPRINGER_VI = re.compile(r"\b(\d{1,4})\s?\((\d{1,4}(?:[-–]\d{1,4})?)\)\s?:\s?(\d+(?:\s*[-–—]\s*\d+)?)")
QUOTED_TITLE = re.compile(r"[\"“«]([^\"”»]{8,300})[\"”»]")
PLACE_COLON = re.compile(
    r"(?:^|[\s.,;])(?:М|Л|СПб|Пермь|Екатеринбург|Новосибирск|Свердловск|Киев|Минск|Ленинград|Москва|Томск|Уфа|Казань|"
    r"Апатиты|Иркутск|Кемерово|Тула|Ростов-на-Дону|Алма-Ата|Ташкент|New York|London|Berlin|Moscow|Amsterdam|"
    r"Oxford|Cambridge|Rotterdam|Boston|Heidelberg|Dordrecht|Leiden|Paris|Leipzig|Stuttgart|Hannover|Wien)\.?\s?:")
BIB_MARKER = re.compile(
    r"//|\b[СCс]\.\s?\d|\d\s?[сc]\.(?:\s|$)|\bpp?\.\s?\d|\bVol\.|\b[ТT]\.\s?\d|№\s?\d|\bISBN\b|\bDOI\b|doi\.org|"
    r"https?://|\b(?:М|Л|СПб)\.?\s?:|\bИзд|Press\b|Verlag|\bPubl|\bJ\.\s|Journal|журн|вестник|труды|\bТр\.|"
    r"сборник|\bсб\.|диссерт|автореф|\bдис\.|\bed(?:s)?\.|Proc(?:eedings)?\b", re.IGNORECASE)
IMPRINT = re.compile(r"copyright|©|all rights reserved|printed in|library of congress|\bУДК\b|\bББК\b|"
                     r"подписано в печать|тираж\s+\d|заказ\s+№", re.IGNORECASE)
RESPONSIBILITY = re.compile(r"\s*(?:\[|\(|под\s|сост|ред\.|пер\.|авт\.|отв\.|науч\.|eds?\.|by\s|"
                            r"(?:[А-ЯЁA-Z]\.\s?){1,2}\s?[А-ЯЁA-Z][а-яёa-z])", re.IGNORECASE)
ENTRY_END = re.compile(r"(?:[.)\]»]|\d|\b[сcp]\.?|pp\.)\s*$")
TITLE_STOP = re.compile(r"\s//\s?|\s/\s|\s[-–—]\s|\.\s?[-–—]\s|\s\[(?:Текст|Электронный ресурс|Text|Electronic "
                        r"resource)\]")
_GERMAN = re.compile(r"\b(?:und|der|die|des|für|über|Verlag|Heft|Bd\.|Auflage|Aufl\.|zur|zum)\b")


# ------------------------------------------------------------------------------------------------ helpers
def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _block_lines(block: Any) -> list[str]:
    """Lines of a block: OCR Markdown is cleaned first (tables and emphasis marks), line breaks are kept."""
    text = _get(block, "text") or ""
    if _get(block, "origin") == "OCR":
        from vkm_corpus.ocr.normalize import text_from_markdown

        text = text_from_markdown(text)
    text = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    return [ln.rstrip() for ln in text.split("\n")]


def _page_index(block: Any) -> int:
    pid = _get(block, "page_id")
    if not pid:
        return 0
    try:
        return int(parse_page_id(pid).index)
    except Exception:  # noqa: BLE001 - odd page ids sort last, never crash the commit
        return 10**6


_HOMOGLYPHS = "AaBCcEeHKkMOoPpTXxYy"
_LAT2CYR = str.maketrans(_HOMOGLYPHS, "АаВСсЕеНКкМОоРрТХхУу")
_CYR = re.compile(r"[А-ЯЁа-яё]")
_TOKEN = re.compile(r"[^\W\d_]+")


def _fold(text: str) -> str:
    """Parse view of OCR text: Latin homoglyphs inside Cyrillic words («Габдraxимов») and one- or two-letter Latin
    tokens among Cyrillic ones («Аплонов B.C.») become Cyrillic. Only the parse uses it; the entry text stays as
    extracted."""
    if not _CYR.search(text):
        return text
    tokens = list(_TOKEN.finditer(text))
    out = list(text)
    for i, m in enumerate(tokens):
        w = m.group(0)
        if not re.search(r"[A-Za-z]", w) or not all(ch in _HOMOGLYPHS or _CYR.match(ch) for ch in w):
            continue
        near = any(_CYR.search(tokens[j].group(0)) for j in (i - 1, i + 1) if 0 <= j < len(tokens))
        if _CYR.search(w) or (len(w) <= 2 and near):
            out[m.start():m.end()] = w.translate(_LAT2CYR)
    return "".join(out)


def _split_inline(line: str) -> list[str]:
    """Split a line of an author-led list where a new name follows a finished entry («…974–983. Abramowitz, M.,»)."""
    parts, start = [], 0
    for m in _INLINE_START.finditer(line):
        head = line[start:m.start()]
        # a finished entry, or the tail of one carried over from the previous block («Prospect. 40, 761–783.»)
        if (YEAR.search(head) and len(head) >= 30) or (len(head) >= 12 and re.search(r"\d\s?\.$", head)):
            parts.append(head)
            start = m.end()
    parts.append(line[start:])
    return parts


def is_heading_text(text: str) -> bool:
    t = normalize_text_v1(text)
    return 0 < len(t) <= 90 and bool(BIB_HEADING.match(t))


def is_bibliographic(text: str) -> bool:
    """Enough evidence that a fragment is a reference entry: a year or a bibliographic marker, and no imprint."""
    t = normalize_text_v1(text)
    if len(t) < CONFIG["min_entry_chars"] or IMPRINT.search(t):
        return False
    letters = sum(ch.isalpha() for ch in t)
    if letters < 0.45 * len(t.replace(" ", "")):
        return False
    return bool(YEAR.search(t) or BIB_MARKER.search(t))


def _label(line: str) -> tuple[int, str, int] | None:
    m = LABEL.match(line)
    if not m:
        return None
    raw = m.group(1) or m.group(2)
    printed = f"[{raw}]" if m.group(1) else raw
    return int(raw), printed, m.end()


def _plausible(n: int, last: int | None) -> bool:
    if last is None:
        return True
    return last < n <= last + CONFIG["label_step_max"] or (n == 1 and last >= 1)


# ------------------------------------------------------------------------------------------------ zones
@dataclass
class _Frag:
    block: Any
    lines: list[str]


@dataclass
class _Zone:
    frags: list[_Frag] = field(default_factory=list)
    heading: Any = None
    n_ref: int = 0


def _looks_like_entry_start(line: str) -> bool:
    return bool(_label(line) or AUTHOR_START.match(line.strip()))


def build_zones(blocks: Sequence[Any]) -> list[_Zone]:
    """Reference zones of one source (blocks in page and reading order; primary text layer only)."""
    seq = sorted((b for b in blocks if _get(b, "is_primary_layer", True) and _get(b, "block_type") not in _FURNITURE),
                 key=lambda b: (_page_index(b), _get(b, "reading_order") or 0))
    zones: list[_Zone] = []
    zone: _Zone | None = None
    misses = 0

    def next_is_ref(i: int) -> bool:
        for j in range(i + 1, min(i + 3, len(seq))):
            if _get(seq[j], "block_type") == "REFERENCE_LIST":
                return True
        return False

    for i, b in enumerate(seq):
        bt = _get(b, "block_type")
        text = _get(b, "text") or ""
        if bt not in _NOT_HEADING and is_heading_text(text):
            if zone is not None and (zone.frags or zone.heading is not None):
                zones.append(zone)
            zone, misses = _Zone(heading=b), 0
            continue
        if bt == "REFERENCE_LIST":
            if zone is None:
                zone = _Zone()
            zone.frags.append(_Frag(b, _block_lines(b)))
            zone.n_ref += 1
            misses = 0
            continue
        if zone is not None and bt in _ADMITTED:
            lines = _block_lines(b)
            first = next((ln for ln in lines if ln.strip()), "")
            sandwich = zone.n_ref > 0 and next_is_ref(i) and len(normalize_text_v1(text)) <= 1500
            heading_only = zone.n_ref == 0 and zone.heading is not None and (
                (_looks_like_entry_start(first) and is_bibliographic(text)) or (zone.frags and not _label(first)
                                                                                and len(text) <= 600))
            labelled = zone.n_ref > 0 and _label(first) is not None and is_bibliographic(text)
            if sandwich or heading_only or labelled:
                zone.frags.append(_Frag(b, lines))
                misses = 0
                continue
            misses += 1
            if zone.n_ref == 0 and zone.heading is not None and misses < 2 and not zone.frags:
                continue            # one stray block right after the heading (epigraph, note) is tolerated
        if zone is not None:
            if zone.frags:
                zones.append(zone)
            zone, misses = None, 0
    if zone is not None and zone.frags:
        zones.append(zone)
    return [z for z in zones if z.frags]


# ------------------------------------------------------------------------------------------------ segmentation
@dataclass
class RawEntry:
    label: str | None
    number: int | None
    lines: list[str] = field(default_factory=list)
    blocks: list[Any] = field(default_factory=list)
    zone_index: int = 0
    numbered: bool = False

    def add(self, line: str, block: Any) -> None:
        if line.strip():
            self.lines.append(line)
        if not any(b is block for b in self.blocks):
            self.blocks.append(block)

    @property
    def text(self) -> str:
        return "\n".join(ln.strip() for ln in self.lines if ln.strip())

    @property
    def page_ids(self) -> list[str]:
        out: list[str] = []
        for b in self.blocks:
            pid = _get(b, "page_id")
            if pid and pid not in out:
                out.append(pid)
        return out


def _label_chain(zone: _Zone) -> int:
    """Length of the longest increasing run of printed numbers at line starts (numbered-list evidence)."""
    best = run = 0
    last: int | None = None
    for f in zone.frags:
        for ln in f.lines:
            lab = _label(ln)
            if not lab:
                continue
            n = lab[0]
            if last is not None and last < n <= last + CONFIG["label_step_max"]:
                run += 1
            else:
                run = 1
            last = n
            best = max(best, run)
    return best


def _column_edge(frag: _Frag, zone: _Zone) -> float | None:
    x0 = _get(frag.block, "bbox_x0")
    if x0 is None:
        return None
    pid = _get(frag.block, "page_id")
    w = CONFIG["column_window_pt"]
    xs = [_get(f.block, "bbox_x0") for f in zone.frags if _get(f.block, "page_id") == pid]
    near = [x for x in xs if x is not None and x0 - w <= x <= x0 + CONFIG["edge_tolerance_pt"]]
    return min(near) if near else x0


def _complete(entry: RawEntry | None) -> bool:
    if entry is None or not entry.lines:
        return True
    text = normalize_text_v1(entry.text)
    return bool(ENTRY_END.search(text)) and bool(YEAR.search(text) or BIB_MARKER.search(text))


def segment_zone(zone: _Zone, zone_index: int) -> tuple[list[RawEntry], list[tuple[str, Any]]]:
    """Entries of one zone and its leading lines before the first entry (a tail carried over from the previous
    zone, re-attached by :func:`extract_entries`)."""
    numbered = _label_chain(zone) >= (2 if len(zone.frags) <= 3 else 3)
    # author-led lists (author–year, alphabetical): every entry starts with a name, so a block that does not start
    # with one continues the previous entry even at the column edge (a tail carried over a page or column), and a
    # name right after a finished entry inside a line starts a new one (text layers without line breaks)
    heads = [next((ln.strip() for ln in f.lines if ln.strip()), "") for f in zone.frags]
    inline = sum(len(_split_inline(ln)) - 1 for f in zone.frags for ln in f.lines) if not numbered else 0
    author_led = not numbered and bool(heads) and (
        sum(bool(AUTHOR_START.match(_fold(h))) for h in heads) >= 0.5 * len(heads)
        or inline >= max(3, 0.5 * len(heads)))
    if author_led:
        zone = _Zone([_Frag(f.block, [part for ln in f.lines for part in _split_inline(ln)]) for f in zone.frags],
                     zone.heading, zone.n_ref)
    entries: list[RawEntry] = []
    cur: RawEntry | None = None
    last: int | None = None
    leading: list[tuple[str, Any]] = []
    for fi, frag in enumerate(zone.frags):
        pending_blank = False
        edge = None if numbered else _column_edge(frag, zone)
        x0 = _get(frag.block, "bbox_x0")
        at_edge = edge is not None and x0 is not None and x0 <= edge + CONFIG["edge_tolerance_pt"]
        first_line = True
        prev_line = ""
        for li, line in enumerate(frag.lines):
            if not line.strip():
                pending_blank = cur is not None and bool(cur.lines)
                continue
            if li == 0 and is_heading_text(line) and len(frag.lines) > 1:
                continue            # «Список литературы» on the first line of a reference block
            if numbered:
                lab = _label(line)
                fresh = first_line or pending_blank
                if lab and (last is None or last < lab[0] <= last + CONFIG["label_step_max"]
                            or (lab[0] == 1 and fresh and _complete(cur))):
                    cur = RawEntry(lab[1], lab[0], zone_index=zone_index, numbered=True)
                    entries.append(cur)
                    last = lab[0]
                    cur.add(line, frag.block)
                elif lab and fresh and _complete(cur):
                    # an unexpected number at a block start after a complete entry: an entry with a misread number
                    # (kept, the sequence is not advanced) or a rubric heading of an index (dropped below)
                    cur = RawEntry(lab[1], lab[0], zone_index=zone_index, numbered=True)
                    entries.append(cur)
                    cur.add(line, frag.block)
                elif cur is None:
                    leading.append((line, frag.block))
                else:
                    cur.add(line, frag.block)
            else:
                stripped = line.strip()
                start = False
                if cur is None:
                    if not entries and not leading and _orphan_tail(stripped):
                        leading.append((line, frag.block))
                        first_line = False
                        prev_line = line
                        continue
                    start = not leading or bool(AUTHOR_START.match(_fold(stripped))) or _complete_text(
                        " ".join(ln for ln, _b in leading))
                    if not start:
                        leading.append((line, frag.block))
                        first_line = False
                        prev_line = line
                        continue
                elif pending_blank:
                    start = True
                elif first_line:
                    author = bool(AUTHOR_START.match(_fold(stripped)))
                    if author_led:
                        start = author and (_complete(cur) or (at_edge and bool(ENTRY_END.search(cur.text))))
                    else:
                        start = (at_edge and (author or _complete(cur)) and not stripped[:1].islower()) or \
                                (author and _complete(cur))
                elif AUTHOR_START.match(_fold(stripped)) and ENTRY_END.search(prev_line.strip()) and \
                        (_complete(cur) or (author_led and len(cur.text) >= 40)):
                    start = True
                if start:
                    cur = RawEntry(None, None, zone_index=zone_index)
                    entries.append(cur)
                cur.add(line, frag.block)
            pending_blank = False
            first_line = False
            prev_line = line
    return entries, leading


def _complete_text(text: str) -> bool:
    t = normalize_text_v1(text)
    return bool(t) and bool(ENTRY_END.search(t)) and bool(YEAR.search(t) or BIB_MARKER.search(t))


def _orphan_tail(line: str) -> bool:
    """The first line of a zone that continues an entry of the previous zone: no printed number, no leading name,
    and it starts in lower case or with a venue/locator tail («J. Geophys. Res., 82, 277–296.», «вып. 3. С. 5–9.»)."""
    s = line.strip()
    if not s or _label(s) or AUTHOR_START.match(_fold(s)):
        return False
    return s[:1].islower() or bool(re.match(r"^[^\s]{1,25}(?:\s[^\s]{1,25}){0,6}[,.:]\s?(?:Vol\.?\s?|vol\.?\s?|[ТT]\.\s?|"
                                            r"№\s?|pp?\.\s?|[СC]\.\s?|\d)", s)) or bool(re.match(r"^[\d(]", s))


# ------------------------------------------------------------------------------------------------ parse
@dataclass
class Parsed:
    authors: list[str] = field(default_factory=list)
    title: str | None = None
    year_raw: str | None = None
    year: int | None = None
    venue: str | None = None
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    url: str | None = None
    doi: str | None = None
    isbn: list[str] = field(default_factory=list)
    method: str = RULES_VERSION
    confidence: float = 0.0
    language: str | None = None


def _clean(s: str | None) -> str | None:
    if s is None:
        return None
    s = re.sub(r"\s+", " ", s).strip(" \t,;:.–—-/")
    s = re.sub(r"\s*\[(?:Текст|Электронный ресурс|Text|Electronic resource)\]\s*", " ", s, flags=re.I).strip()
    return s or None


def _language(text: str) -> str | None:
    cyr = lat = 0
    for ch in text:
        if ch.isalpha():
            if "CYRILLIC" in unicodedata.name(ch, ""):
                cyr += 1
            elif "LATIN" in unicodedata.name(ch, ""):
                lat += 1
    if cyr == lat == 0:
        return None
    if cyr >= lat:
        return "ru"
    return "de" if len(_GERMAN.findall(text)) >= 2 else "en"


def _authors(text: str) -> tuple[list[str], str, str | None]:
    """(authors, remainder, pattern) — the longest author list at the start of the text."""
    best: tuple[int, str | None, re.Match[str] | None] = (0, None, None)
    for key, rx in AUTHOR_LIST.items():
        m = rx.match(text)
        if m and m.end() > best[0]:
            best = (m.end(), key, m)
    end, key, m = best
    if m is None or key is None:
        return [], text, None
    span = m.group(0)
    names = [_clean(a.group(0)) for a in AUTHOR_ONE[key].finditer(span)]
    names = [n for n in names if n]
    rest = text[end:].lstrip(" ,;:.")
    return names, rest, key


def _year(text: str, after_authors: str) -> tuple[str | None, int | None]:
    lo, hi = CONFIG["year_range"]
    m = YEAR_PAREN.search(text)
    if m:
        return m.group(0).strip("()").split(",")[0].strip(), int(m.group(1))
    m = re.match(r"^[(\s]*(1[89]\d\d|20[0-2]\d|2030)([a-zа-я])?[).,:\s]", after_authors + " ")
    if m:
        return m.group(1) + (m.group(2) or ""), int(m.group(1))
    stop = TITLE_STOP.search(text)
    region = text[stop.end():] if stop else text
    for rx_text in (region, text):
        for m in YEAR.finditer(rx_text):
            tail = rx_text[m.end():m.end() + 2]
            if tail[:1] in ("-", "–", "—") and tail[1:2].isdigit():
                continue    # a range such as 1941–1945 in a title
            y = int(m.group(1))
            if lo <= y <= hi:
                return m.group(1) + (m.group(2) or ""), y
    return None, None


def _title_venue(rest: str, style: str) -> tuple[str | None, str | None]:
    q = QUOTED_TITLE.search(rest)
    if q and q.start() < 40:
        title = q.group(1)
        after = rest[q.end():].lstrip(" ,.")
        venue = re.split(r",\s*(?:\d|Vol|vol|pp?\.)|\(\d{4}", after)[0]
        return _clean(title), _clean(venue)
    if re.search(r"(?<!:)//", rest):
        left, right = re.split(r"(?<!:)//", rest, maxsplit=1)
        title = re.split(r"\s/\s", left)[0]
        venue = re.split(r"\.\s?(?=(?:1[89]\d\d|20[0-3]\d|№|[ТT]\.|Vol|V\.|Вып|вып|[СC]\.|P\.|pp?\.|No)\b)|"
                         r"\s[-–—]\s|,\s?(?=(?:1[89]\d\d|20[0-3]\d|№|[Тт]\.|[Вв]ып\.)\b)", right.strip())[0]
        return _clean(title), _clean(venue)
    if style == "author_year":
        body = re.sub(r"^[(\s]*(?:1[89]\d\d|20[0-2]\d|2030)[a-zа-я]?[).,:\s]+", "", rest)
        parts = re.split(r"(?:(?<=[^\W\d_]{2})|(?<=[)\]?!]))\.\s+(?=[A-ZА-ЯЁ\d(])", body, maxsplit=1)
        title = parts[0]
        venue = None
        if len(parts) > 1:
            venue = re.split(r"[,.]?\s?(?=\d{1,4}\s?[(:,])|,\s?(?:Vol|vol|pp?\.|\d)", parts[1])[0]
        return _clean(title), _clean(venue)
    stop = TITLE_STOP.search(rest)
    place = PLACE_COLON.search(rest)
    cut = min([m.start() for m in (stop, place) if m] or [len(rest)])
    title = rest[:cut]
    if cut == len(rest):
        title = re.split(r"(?<=[a-zа-яё)\]])\.\s+(?=[A-ZА-ЯЁ])", rest, maxsplit=1)[0]
    venue = None
    if stop is not None and stop.group(0).strip() == "/":
        after = rest[stop.end():]
        # «/ И.О. Фамилия», «/ под ред.», «/ [сост.]» state responsibility; anything else names the collection
        if not RESPONSIBILITY.match(after):
            end = [m.start() for m in (re.search(r"\s[-–—]\s|\.\s?[-–—]\s|[,.:]\s?(?:№|[Тт]\.\s?\d|[Вв]ып\.|"
                                                 r"(?:1[89]\d\d|20[0-3]\d)\b)|//", after),
                                       PLACE_COLON.search(after)) if m]
            venue = after[:min(end)] if end else after
    return _clean(title), _clean(venue)


def parse_entry(text: str) -> Parsed:
    """Rule-based parse of one entry (GOST 7.1/7.0.5, author–year and numbered English styles)."""
    t = _fold(normalize_text_v1(text))
    lab = _label(t)
    body = t[lab[2]:] if lab else t
    p = Parsed(language=_language(body))
    d = DOI.search(body)
    p.doi = normalize_doi(d.group(1)) if d else None
    isbns = []
    for m in ISBN.finditer(body):
        v = normalize_isbn(m.group(1))
        if v and v not in isbns:
            isbns.append(v)
    p.isbn = isbns
    u = URL.search(body)
    p.url = u.group(0).rstrip(".,;)") if u else None
    clean = re.sub(r"\s+", " ", DOI.sub(" ", URL.sub(" ", body))).strip() if (p.url or p.doi) else body
    authors, rest, pattern = _authors(clean)
    responsibility: list[str] = []
    m = re.search(r"\s/\s([^;/–—]{2,300}?)(?:;|\s[-–—]\s|//|\.\s?[-–—]|$)", clean)
    if m and not re.match(r"\s*(?:под\s|сост|ред\.|пер\.)", m.group(1), re.IGNORECASE):
        for key in ("ru_is", "ru_si", "en_is", "en_si", "en_si2"):
            names = [n for n in (_clean(a.group(0)) for a in AUTHOR_ONE[key].finditer(m.group(1))) if n]
            if len(names) > len(responsibility):
                responsibility = names
    # «Фамилия И.О. Название / Фамилия И.О., Фамилия2 И.О.»: the statement of responsibility lists everyone
    p.authors = responsibility if len(responsibility) > len(authors) else authors
    p.year_raw, p.year = _year(clean, rest if authors else "")
    after_year = re.match(r"^[(\s]*(?:1[89]\d\d|20[0-2]\d|2030)[a-zа-я]?[).,:\s]", (rest if authors else "") + " ")
    style = "gost" if ("//" in clean or TITLE_STOP.search(clean) or PLACE_COLON.search(clean)) else "plain"
    if after_year or (pattern == "en_spr" and YEAR_PAREN.search(clean[:80])):
        style = "author_year"
    elif QUOTED_TITLE.search(clean):
        style = "quoted"
    p.title, p.venue = _title_venue(rest if authors else clean, style)
    if p.title and len(p.title) < 3:
        p.title = None
    m = SPRINGER_VI.search(clean)
    if m:
        p.volume, p.issue, p.pages = m.group(1), m.group(2), re.sub(r"\s+", "", m.group(3))
    else:
        v = VOLUME.search(clean)
        i = ISSUE.search(clean)
        p.volume = v.group(1) if v else None
        p.issue = re.sub(r"\s+", "", i.group(1)) if i else None
        region = rest if authors else clean     # initials («GLASBERGEN, P. 1989») are not page locators
        pages = list(PAGES_LOC.finditer(region))
        if pages:
            p.pages = re.sub(r"\s+", "", pages[-1].group(1))
        else:
            tail = PAGES_TAIL.search(region)
            p.pages = re.sub(r"\s+", "", tail.group(1)) if tail else None
    p.method = f"{RULES_VERSION}/{style}"
    score = 0.0
    score += 0.3 if p.authors else 0.0
    score += 0.3 if p.title and len(p.title) >= 8 else 0.0
    score += 0.2 if p.year else 0.0
    score += 0.2 if any((p.venue, p.pages, p.volume, p.issue, p.doi, p.isbn, p.url)) else 0.0
    p.confidence = round(score, 2)
    return p


# ------------------------------------------------------------------------------------------------ entries
@dataclass
class Entry:
    label: str | None
    ordinal: int
    blocks: list[Any]
    text: str
    normalized_text: str
    continues_on_page_id: str | None
    parsed: Parsed
    numbered: bool


@dataclass
class SegmentStats:
    zones: int = 0
    entries: int = 0
    dropped_not_bibliographic: int = 0
    dropped_leading_lines: int = 0
    numbered_zones: int = 0


def extract_entries(blocks: Iterable[Any]) -> tuple[list[Entry], SegmentStats]:
    """All bibliography entries of one source, in document order, with a source-wide running ordinal."""
    stats = SegmentStats()
    out: list[Entry] = []
    zones = build_zones(list(blocks))
    stats.zones = len(zones)
    ordinal = 0
    raw_all: list[RawEntry] = []
    for zi, zone in enumerate(zones):
        raw, leading = segment_zone(zone, zi)
        if leading:
            prev = raw_all[-1] if raw_all else None
            first_page = _page_index(leading[0][1])
            last_page = _page_index(prev.blocks[-1]) if prev and prev.blocks else None
            if prev is not None and last_page is not None and 0 <= first_page - last_page <= 1:
                for line, block in leading:        # a tail carried over a page, column or layout break
                    prev.add(line, block)
            else:
                stats.dropped_leading_lines += len(leading)
        if raw and raw[0].numbered:
            stats.numbered_zones += 1
        raw_all.extend(raw)
    for e in raw_all:
        text = e.text
        if not text or not is_bibliographic(text):
            stats.dropped_not_bibliographic += 1
            continue
        ordinal += 1
        pages = e.page_ids
        out.append(Entry(label=e.label, ordinal=ordinal, blocks=list(e.blocks), text=text,
                         normalized_text=normalize_text_v1(text),
                         continues_on_page_id=pages[1] if len(pages) > 1 else None,
                         parsed=parse_entry(text), numbered=e.numbered))
    stats.entries = len(out)
    return out, stats
