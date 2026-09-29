"""Structured tables of the navigation layer (NAV §10) — grids with provenance from ``canonical.tables``, no LLM.

``build(con, *, section_pages=None, sections=None, formula_symbols=None, stats=None, source_ids=None, **options)``
returns three Arrow tables (rule ``tables_v1``):

* ``table_structure`` — one row per canonical table: number «3.2» and caption, grid size, header rows and how they
  were found, bands and blocks, merged cells, orientation, parse method, confidence, quality flags, the properties
  and materials named by its headers, row labels and caption, and whether the grid may stand for the text layer of
  its region (``covers_region``: the parameter part then reads the region from the grid, not from the text lines);
* ``table_cells`` — one row per cell of the canonical grid, spans kept (id ``TBL-…:r<row>c<col>``): the text as
  printed and cleaned (math rendered, an unbalanced ``$`` closed, glued header words re-spaced), the row role, the
  header path of its column (multi-level headers, top first), the row label, the value as printed and parsed
  (decimal comma, ranges, «±», bounds, powers — «2,8·10-3» with the superscript lost is read as a power and flagged),
  the unit and where it came from (cell, units row, column header, units column, row label, group row, caption), the
  cell and column type and flags;
* ``table_columns`` — one row per column of a block: header path, symbol, unit, power multiplier, statistic, role
  (label, index, units, symbol, value, dispersion, count, text), type, and the property of the parameters vocabulary
  its header names.

Rules (``tables_v1``). OCR tables are recognised in horizontal bands (``PIPELINE.md`` §5); the bands are re-read from
the raw output and must reproduce the canonical cells. A band that brings a header of its own starts a new block; a
band that repeats the header continues the block (the repeated rows are marked); a band of another width whose
columns do not look like the block's is a block without a header (``BAND_COLUMNS_MISMATCH``) — its columns never take
the header of another band. Header rows are the rows above the first data row (numbers in at least half of the
filled cells — the parameter part's rule), or the ``<th>`` rows of an EPUB or DOCX table. Row roles mark statistics
(mean, min, max, median; dispersion — standard deviation spelled «Стандарт», «СКО», «σ» or «S» next to a mean,
coefficient of variation; counts), repeated headers, column numbers, groups and notes. A caption gives a unit only to
value columns without one: per column from a list «[name, symbol, unit; …]», or the one unit that closes the caption —
never a time or a year, nor a legend's unit, nor to an argument column or one naming another property. Nothing is
corrected: a value keeps its printed digits; a lost decimal point («218» among «2,18») is flagged
``DECIMAL_POINT_SUSPECT``, several numbers in one cell ``MULTI_VALUE``, and an unparseable cell stays text. Everything
is DERIVED navigation (``AUTO_EXTRACTED_UNREVIEWED``), never evidence.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from functools import lru_cache
from html.parser import HTMLParser
from typing import Any, Callable, Iterable

from vkm_corpus.navigation import ids as nav_ids
from vkm_corpus.navigation import parameters as P
from vkm_corpus.navigation import parameters_vocab as V
from vkm_corpus.navigation.formulas import latex_to_plain

RULE_VERSION = nav_ids.RULE_VERSIONS["tables"]
REVIEW_STATUS = "AUTO_EXTRACTED_UNREVIEWED"
DEFAULTS: dict[str, Any] = {
    "respace": "auto",        # glued header words: "auto" (pymorphy3 dictionary when importable) | "off"
    "max_header_rows": 5,     # rows above the first data row read as a header at most
    "max_grid": 100_000,      # grid positions analysed per table (larger: cells only, flag TOO_LARGE)
}
NUMERIC = frozenset({"NUMBER", "RANGE", "PM", "BOUND"})
HEADER_ZONE = frozenset({"HEADER", "TITLE", "UNITS", "NUMBERING_HEAD", "AXIS_TITLE", "AXIS"})
VALUE_ROWS = frozenset({"DATA", "STAT_MEAN", "STAT_MIN", "STAT_MAX", "STAT_MEDIAN"})
ROW_ROLES = ("TITLE", "HEADER", "UNITS", "NUMBERING_HEAD", "AXIS_TITLE", "AXIS", "DATA", "STAT_MEAN", "STAT_MIN",
             "STAT_MAX", "STAT_MEDIAN", "STAT_DISPERSION", "STAT_COUNT", "GROUP", "TEXT", "NUMBERING",
             "REPEATED_HEADER", "NOTE", "EMPTY", "UNKNOWN")
_BENIGN = frozenset({"MATH_RENDERED", "UNBALANCED_MATH_CLOSED", "HEADER_RESPACED", "DEHYPHENATED"})
_CANON_FLAGS = ("TRUNCATED", "REPETITION", "EMPTY_ON_INK", "NATIVE_OCR_DISAGREE", "TABLE_STRUCTURE_UNCERTAIN")
_BASE_CONFIDENCE = {"EPUB_XHTML": 0.9, "DOCX_XML": 0.9, "NATIVE_FIND_TABLES": 0.85, "BOTH_AGREE": 0.85,
                    "OCR_GLM": 0.75, "BOTH_DISAGREE": 0.65}
_PENALTIES = {"TRUNCATED": 0.2, "REPETITION": 0.3, "EMPTY_ON_INK": 0.2, "FIGURE_SUSPECT": 0.2,
              "BAND_COLUMNS_MISMATCH": 0.15, "BANDS_UNRESOLVED": 0.1, "NO_HEADER": 0.1, "HEADER_SHORT": 0.1,
              "SPARSE": 0.1, "RAGGED_ROWS": 0.05, "OVERLAPPING_CELLS": 0.1, "TOO_LARGE": 0.2,
              "TABLE_STRUCTURE_UNCERTAIN": 0.2}
NOTE = ("DERIVED navigation layer (AUTO_EXTRACTED_UNREVIEWED): table grids parsed by rules from the canonical OCR or "
        "native tables; values as printed, never corrected (flags mark suspects); not evidence — check the page "
        "image before use.")


# ================================================================================================= bands (raw HTML)
class _BandParser(HTMLParser):
    """Rows of the first ``<table>`` of one band, read as ``ocr.normalize.parse_html_table`` reads them (stdlib)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[list[Any]]] = []
        self.depth = 0
        self.row: list[list[Any]] | None = None
        self.cell: list[Any] | None = None
        self.in_head = 0
        self.done = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.done:
            return
        if tag == "table":
            self.depth += 1
        elif self.depth != 1:
            return
        elif tag == "thead":
            self.in_head += 1
        elif tag == "tr":
            self.row, self.cell = [], None
            self.rows.append(self.row)
        elif tag in ("td", "th"):
            if self.row is None:
                self.row = []
                self.rows.append(self.row)
            self.cell = [tag, dict(attrs), [], self.in_head > 0]
            self.row.append(self.cell)

    def handle_endtag(self, tag: str) -> None:
        if self.done:
            return
        if tag == "table":
            self.depth -= 1
            self.done = self.depth <= 0
        elif self.depth != 1:
            return
        elif tag == "thead":
            self.in_head = max(0, self.in_head - 1)
        elif tag in ("td", "th"):
            self.cell = None
        elif tag == "tr":
            self.row = self.cell = None

    def handle_data(self, data: str) -> None:
        if self.cell is not None and not self.done:
            self.cell[2].append(data)


def _span(attrs: dict[str, Any], key: str) -> int:
    try:
        return max(1, int(attrs.get(key) or 1))
    except ValueError:
        return 1


def parse_band(html: str) -> dict[str, Any] | None:
    """One band's grid (cells with ``th``, rows, columns): the occupancy rule and the dropped trailing empty rows of
    ``ocr.normalize.parse_html_table``; a cut-off band keeps its complete rows."""
    m = re.search(r"<table\b.*?</table>", html, re.S | re.I)
    if m:
        markup = m.group(0)
    else:
        start = re.search(r"<table\b", html, re.I)
        if not start:
            return None
        body = html[start.start():]
        last = body.lower().rfind("</tr>")
        if last < 0:
            return None
        markup = body[:last + 5] + "</table>"
    p = _BandParser()
    try:
        p.feed(markup)
        p.close()
    except Exception:  # noqa: BLE001 - malformed markup: the bands stay unresolved
        return None
    occupied: set[tuple[int, int]] = set()
    cells: list[dict[str, Any]] = []
    for r, tr in enumerate(p.rows):
        c = 0
        for tag, attrs, parts, head in tr:
            while (r, c) in occupied:
                c += 1
            rs, cs = _span(attrs, "rowspan"), _span(attrs, "colspan")
            for dr in range(rs):
                for dc in range(cs):
                    occupied.add((r + dr, c + dc))
            cells.append({"row": r, "col": c, "row_span": rs, "col_span": cs, "th": tag == "th" or head,
                          "text": re.sub(r"\s+", " ", "".join(parts)).strip()})
            c += cs
    filled = {x["row"] + dr for x in cells if x["text"] for dr in range(x["row_span"])}
    last = max(filled, default=-1)
    cells = [{**x, "row_span": min(x["row_span"], last - x["row"] + 1)} for x in cells if x["row"] <= last]
    occupied = {(r, c) for r, c in occupied if r <= last}
    return {"cells": cells, "n_rows": max((r for r, _ in occupied), default=-1) + 1,
            "n_cols": max((c for _, c in occupied), default=-1) + 1}


def _key(cell: dict[str, Any], dr: int = 0) -> tuple:
    return (int(cell["row"]) + dr, int(cell["col"]), int(cell.get("row_span") or 1), int(cell.get("col_span") or 1),
            re.sub(r"\s+", "", cell.get("text") or ""))


def resolve_bands(raw_output: str | None, cells: list[dict[str, Any]]) -> dict[str, Any]:
    """Bands of an OCR table: ``band_of`` (band of every canonical row; None when the raw output does not reproduce
    the canonical cells), ``widths`` (columns of every band), ``th_rows`` (rows marked ``<th>`` in any band) and
    ``n_bands``."""
    parts = [x for x in re.split(r"(?i)(?=<table\b)", raw_output or "") if re.match(r"(?i)<table\b", x)]
    out: dict[str, Any] = {"band_of": None, "widths": [], "th_rows": set(), "n_bands": len(parts)}
    if len(parts) <= 1:
        out["th_rows"] = {int(c["row"]) for c in cells if c.get("is_header")}
        return out
    bands = [parse_band(x) for x in parts]
    if any(b is None for b in bands):
        return out
    stacked, th_rows, band_of, off = [], set(), [], 0
    for k, b in enumerate(bands):
        stacked += [_key(c, off) for c in b["cells"]]
        th_rows |= {int(c["row"]) + off for c in b["cells"] if c["th"]}
        band_of += [k] * b["n_rows"]
        off += b["n_rows"]
    if sorted(stacked) == sorted(_key(c) for c in cells):
        out.update({"band_of": band_of, "widths": [b["n_cols"] for b in bands], "th_rows": th_rows})
    return out


# ================================================================================================= cell text
_LATEX_HINT = re.compile(r"\\[A-Za-z]+|[_^]\{")
_PLUS_NUMBER = re.compile(r"^\+\s*(?=\d)")
_LOST_POWER = re.compile(r"[·⋅∙×xхЧ*´]\s*10\s*(?:[-−–]\s?\d{1,2}|\d{1,2})(?![\d.,])")


def _render_tokens(t: str) -> str:
    """LaTeX without delimiters («σ_{сж}», «\\sigma_1»): each whitespace token with balanced braces rendered."""
    out = []
    for tok in t.split(" "):
        if _LATEX_HINT.search(tok) and tok.count("{") == tok.count("}"):
            tok = latex_to_plain(tok) or tok
        out.append(tok)
    return " ".join(out)


def clean_cell_text(text: str | None) -> tuple[str, list[str]]:
    """Readable single-line text of a cell and the repairs made: math spans rendered (``$\\sigma_{1}$`` → ``σ_1``,
    ``MATH_RENDERED``), an unbalanced ``$`` closed at the far end («SiO_{2}$» → ``$SiO_{2}$``, «$1.46» → ``$1.46$``,
    ``UNBALANCED_MATH_CLOSED``), a word broken across lines joined («ру- доуправление», ``DEHYPHENATED``), a leading
    «+» of a number dropped. Letters and digits are not changed."""
    t = re.sub(r"\s+", " ", text or "").strip()
    flags: list[str] = []
    if not t:
        return "", flags
    if "$" in t:
        if t.count("$") % 2:
            t = ("$" + t) if t.endswith("$") and not t.startswith("$") else (t + "$")
            flags.append("UNBALANCED_MATH_CLOSED")
        flags.append("MATH_RENDERED")
    elif _LATEX_HINT.search(t):
        t = _render_tokens(t)
        flags.append("MATH_RENDERED")
    t = _PLUS_NUMBER.sub("", P.cell_plain(t))
    if _CELL_HYPHEN.search(t):
        t = _CELL_HYPHEN.sub("", t)
        flags.append("DEHYPHENATED")
    return t, flags


_CELL_HYPHEN = re.compile(r"(?<=[а-яё])[-‐] (?=[а-яё])")


# ------------------------------------------------------------------------------------------ glued words (optional)
_GLUED = re.compile(r"[А-Яа-яЁё]{12,}")
_SHORT_OK = frozenset("на по до от за из при для без над под".split())
# first parts of compound words the dictionary does not list («внутрисолевых», «электрооборудования»)
_COMPOUND_HEADS = frozenset("внутри электр электро прямо около после сверх между против много одно двух трех "
                            "само полу вне пере лито гидро термо гео".split())


@lru_cache(maxsize=1)
def _dictionary() -> Callable[[str], bool] | None:
    """``word_is_known`` of pymorphy3 (extra ``navigation``), cached; None when the dictionary is not installed."""
    try:
        import pymorphy3  # noqa: PLC0415 - optional dependency

        morph = pymorphy3.MorphAnalyzer()
    except Exception:  # noqa: BLE001
        return None
    return lru_cache(maxsize=500_000)(lambda w: bool(morph.word_is_known(w)))


def segment_glued(word: str, is_word: Callable[[str], bool], max_parts: int = 3) -> list[str] | None:
    """The fewest dictionary words (2..``max_parts``) a glued token splits into («глубинаотработки» → «глубина»,
    «отработки»). Conservative: every piece has five letters or more (a preposition «на», «при» … excepted) and the
    first piece is not the head of a compound («внутри», «электро», «пере» …); None when there is no such split."""
    low = word.lower().replace("ё", "е")
    n = len(low)
    best: list[list[tuple[int, int]] | None] = [None] * (n + 1)
    best[0] = []
    for i in range(1, n + 1):
        for j in range(max(0, i - 24), i):
            prev = best[j]
            if prev is None or len(prev) >= max_parts:
                continue
            piece = low[j:i]
            if (len(piece) < 5 and piece not in _SHORT_OK) or (j == 0 and piece in _COMPOUND_HEADS) \
                    or not is_word(piece):
                continue
            if best[i] is None or len(prev) + 1 < len(best[i]):
                best[i] = prev + [(j, i)]
    got = best[n]
    return [word[a:b] for a, b in got] if got is not None and len(got) >= 2 else None


_GLUED_MIXED = re.compile(r"[A-Za-zА-Яа-яЁё]{12,}")
_LAT2CYR = str.maketrans("acekmopxytdlnuhiABCEHKMOPTX", "асекморхутдлпиниАВСЕНКМОРТХ")   # OCR look-alikes


def _fold_mixed(word: str) -> str:
    """A mostly Cyrillic token with Latin look-alikes («Пределdlительной») in Cyrillic; «козфф» → «коэфф»."""
    cyr = sum(1 for ch in word if "а" <= ch.lower() <= "я" or ch in "ёЁ")
    if cyr == len(word) or cyr < 0.6 * len(word):
        return word
    out = word.translate(_LAT2CYR)
    return re.sub(r"(?i)^коз(?=фф)", lambda m: m.group(0)[:2] + ("Э" if m.group(0)[2].isupper() else "э"), out)


def respace(text: str, is_word: Callable[[str], bool] | None) -> str | None:
    """The text with glued words re-spaced («Глубинаотработки», OCR «Пределdlительной» → «Предел длительной»), or
    None when nothing was split (always None without a dictionary)."""
    if not is_word or not _GLUED_MIXED.search(text):
        return None
    changed = False

    def fix(m: re.Match) -> str:
        nonlocal changed
        w = m.group(0)
        folded = _fold_mixed(w)
        if not _GLUED.fullmatch(folded) or is_word(folded.lower().replace("ё", "е")):
            return w
        parts = segment_glued(folded, is_word)
        if not parts:
            return w
        changed = True
        return " ".join(parts)

    out = _GLUED_MIXED.sub(fix, text)
    return out if changed else None


# ================================================================================================= cell values
# a printed symbol: a Greek letter with an index («σсж», «ε_1»), one letter with an index or digits («E_0», «K2»),
# a Latin capital with a Cyrillic index («Kдл», «Dпр») — not a short word («Блок»)
_CELL_SYMBOL = re.compile(r"(?:[α-ωΑ-Ωϑϕϵ][\wа-яё]{0,4}|[A-Za-zА-ЯЁа-яё](?:_\{?[\w,.]{1,6}\}?|\d{1,2})?|"
                          r"[A-Z][а-яё]{1,3}|[A-Za-z][a-z]?)[′'*]?")


def cell_value(clean: str, lang: str | None = None) -> dict[str, Any]:
    """Type and parsed value of a cleaned cell: one value expression (number, range, «±», bound) with its unit,
    several numbers (``MULTI``), a bare unit, a symbol, text or empty."""
    if not clean or P._EMPTY_CELL.match(clean):
        return {"value_type": "EMPTY"}
    v = P.parse_cell(clean, lang)
    if v is not None:
        q = v.qualifier
        vt = "RANGE" if q == "range" else "PM" if q == "±" else "BOUND" if q in ("≤", "≥", "<", ">", "≈") else "NUMBER"
        flags = [f for f in v.flags if f in ("ALTERNATIVE_IN_PARENS", "SHARED_POWER")]
        if _LOST_POWER.search(v.text) and "^" not in v.text:
            flags.append("POWER_SUPERSCRIPT_LOST")
        return {"value_type": vt, "val": v, "n_numbers": 2 if q in ("range", "±") else 1, "flags": flags}
    am = _ANGLE_MARK.fullmatch(clean)                     # «15"», «30′»: seconds or minutes of arc, as printed
    if am:
        v = P.parse_cell(am.group(1), lang)
        if v is not None:
            return {"value_type": "NUMBER", "val": v, "n_numbers": 1, "flags": ["ANGLE_MARK"]}
    stripped = clean.strip(" ()[]")
    if not re.search(r"\d", stripped):
        u = P.match_unit(stripped, 0)
        if u is not None and u.end >= len(stripped.rstrip(" .,;:")):
            return {"value_type": "UNIT", "unit": u}
        if len(stripped) <= 8 and _CELL_SYMBOL.fullmatch(stripped):
            return {"value_type": "SYMBOL"}
        return {"value_type": "TEXT"}
    if _NUMBER_PAIR.fullmatch(clean):                       # «0,90/0,70»: two values of one cell
        return {"value_type": "MULTI", "n_numbers": 2, "flags": ["MULTI_VALUE"]}
    vals = [x for x in P.scan_values(clean, lang) if x.reject is None]
    if len(vals) >= 2 and len(re.findall(r"[^\W\d_]", clean)) <= 4:
        flags = ["MULTI_VALUE"]
        if re.fullmatch(r"[-−–]?\d{1,3} \d{1,3}", clean):
            flags.append("SPACED_DIGITS_SUSPECT")          # «2 18»: a decimal comma read as a space?
        return {"value_type": "MULTI", "n_numbers": len(vals), "flags": flags}
    return {"value_type": "TEXT", "n_numbers": len(vals) or None}


_ANGLE_MARK = re.compile(r"\s*([-−–]?\d+(?:[.,]\d+)?)\s*(?:″|\"|''|′|')\s*")
# «0,90/0,70», «13.4-25.8/19.2» (from–to / mean): several values of one cell
_NUMBER_PAIR = re.compile(r"[-−–]?\d+(?:[.,]\d+)?(?:\s*[-−–÷]\s*\d+(?:[.,]\d+)?)?\s*/\s*[-−–]?\d+(?:[.,]\d+)?")
NUMISH = NUMERIC | {"MULTI"}


# ================================================================================================= labels
_R = re.compile
_STATS: tuple[tuple[str, re.Pattern], ...] = (
    # OCR spellings included: «Средневадратичное», «Коэффициент варizations», «Stандарт»
    ("DISPERSION", _R(r"коэф\w*\.?\s*(?:вар|изм)\w*|\w*цент\s+вар\w*|вариаци\w*|"
                      r"(?:квадр|вадрат|дратич)\w*\.?\s*(?:откл|ошиб)\w*|"
                      r"ср\.?\s*кв\w*\.?\s*откл\w*|средне\s?к?вадрат\w*|(?<![\w-])[сcs]тандарт\w*|станд\.?\s*откл\w*|"
                      r"дисперси\w*|погрешност\w*|доверительн\w*|(?<![\w-])ско(?![\w-])|std\.?(?![\w-])|"
                      r"standard\s+(?:deviation|error)|coefficient\s+of\s+variation|(?<![\w-])c\.?o\.?v\.?(?![\w-])|"
                      r"variance")),
    # a count of samples, tests or measurements — not «Количество шламов, млн т» (an amount)
    ("COUNT", _R(r"(?<![\w-])(?:кол(?:-во|\.|ичеств\w*)|числ[оа])\s+(?:\w+\s+)?(?:образц|проб|испытан|опыт|"
                 r"определени|замер|измерени|точек|скважин|наблюдени|серий|анализ)\w*|^\s*кол(?:-во|\.|ичество)\s*$|"
                 r"(?<![\w-])(?:number|count)\s+of\s+(?:samples|specimens|tests|measurements)|^\s*n\s*(?:[,=]|$)")),
    ("MEDIAN", _R(r"медиан\w*|median")),
    ("MEAN", _R(r"(?<![\w/-])(?:средн\w*|ср\.\s*знач\w*|mean|average|avg)(?![\w-])")),
    # «мин.», «min» — not the minutes of «м/мин», «об/мин», «Время, мин», «Время (мин.)», «Time, min»; OCR spellings
    # «Minimalное», «Minimalое», «Maximalное» (folded to «мипималное», «махималное»)
    ("MIN", _R(r"(?<![\w/-])(?:minimum|minimal\w*|ми[нп]имал\w*|наименьш\w*)(?![\w-])|"
               r"(?<![\w/(,-])(?<![(,]\s)(?:min|мин\.)(?![\w-])|^\s*мин\s*$")),
    ("MAX", _R(r"(?<![\w/-])(?:max(?:imum)?|maximal\w*|макс\.?|ма(?:кс|х)имал\w*|наибольш\w*)(?![\w-])")),
)
# a bare dispersion symbol («σ», «S», «±σ», «δ, МПа»): a dispersion only next to a mean row or column
_BARE_DISPERSION = _R(r"^\s*(?:±\s*)?(?:σ|s|s_?x|σ_?x|δ)\s*(?:[,(].{0,12})?$", re.I)
# statistics written as symbols in the label column («x̄ | σ | V | n»): read only when two or more of them label rows
_STAT_SYMBOLS = (("MEAN", _R(r"^\s*(?:x̄|x|x_ср|xср|x_?m|m_?x|μ)\s*$", re.I)),
                 ("DISPERSION", _R(r"^\s*(?:σ|s|s_?x|σ_?x|v|v,\s*%|c_?v|cv|v_?x)\s*$", re.I)),
                 ("COUNT", _R(r"^\s*n\s*$", re.I)),
                 ("MIN", _R(r"^\s*x_?min\s*$", re.I)), ("MAX", _R(r"^\s*x_?max\s*$", re.I)))


def _stat_symbol(label: str | None) -> str | None:
    return next((name for name, rx in _STAT_SYMBOLS if rx.match(label or "")), None)
_SYMBOL_HEADER = _R(r"(?<![\w-])(?:обозначени\w*|обозн\.?|символ\w*|symbols?|notation)(?![\w-])", re.I)
# a header naming a quantity at conditions: the axis under it lists the conditions («… при σn = …», «… at T»)
_AXIS_CONDITION = _R(r"(?<![\w-])(?:при|под|at|for|under|в\s+зависимости\s+от|depending\s+on)\s", re.I)
_NOTE_START = _R(r"^\s*(?:примечани\w*|прим\.|notes?\b|\*)", re.I)
_TABLE_NUMBER = _R(r"(?:таблица|табл\.?|table|tab\.?)\s*(?:№\s*)?([А-ЯA-Zа-я]{0,3}\.?\s*[\dIVXLC]+(?:\s*[.\-–]\s*\d+)*"
                   r"[а-яa-z]?)", re.I)
_FIGURE_LABEL = _R(r"^\s*(?:рис(?:\.|унок)|fig(?:\.|ure)|схема|scheme)", re.I)


def stat_of(text: str | None) -> str | None:
    """The statistic a row label or header names: DISPERSION, COUNT, MEDIAN, MEAN, MIN or MAX (first match)."""
    if not text:
        return None
    low = P.lower_same_length(text)
    got = next((name for name, rx in _STATS if rx.search(low)), None)
    if got is None and re.search(r"[а-я]", low) and re.search(r"\(\s*min\s*\)", low):
        got = "MIN"                                        # «Длина комплекта (min)»: in a Russian text the minimum
    return got


def table_number(label: str | None, caption: str | None) -> str | None:
    """The printed number of a table («3.2», «Пр.8», «2.11») from its label, else from the start of the caption."""
    for text in (label, (caption or "")[:40]):
        m = _TABLE_NUMBER.search(text or "")
        if m:
            return re.sub(r"\s+", "", m.group(1)).rstrip(".")
    return None


# ================================================================================================= one table
class _Grid:
    """Positions → covering cell, and the parameter part's notions of filled, numeric (at the origin of a cell),
    numbering, data and group rows."""

    def __init__(self, cells: list[dict[str, Any]], n_rows: int, n_cols: int) -> None:
        self.cells, self.n_rows, self.n_cols = cells, n_rows, n_cols
        self.at = [[-1] * n_cols for _ in range(n_rows)]
        self.overlaps = 0
        for i, c in enumerate(cells):
            for r in range(c["row"], min(n_rows, c["row"] + c["row_span"])):
                for k in range(c["col"], min(n_cols, c["col"] + c["col_span"])):
                    if self.at[r][k] < 0:
                        self.at[r][k] = i
                    else:
                        self.overlaps += 1

    def cell(self, r: int, c: int) -> dict[str, Any] | None:
        i = self.at[r][c]
        return self.cells[i] if i >= 0 else None

    def text(self, r: int, c: int) -> str:
        x = self.cell(r, c)
        return x["clean"] if x is not None else ""

    def filled(self, r: int, c: int) -> bool:
        x = self.cell(r, c)
        return x is not None and x["vtype"] != "EMPTY"

    def origin(self, r: int, c: int) -> bool:
        x = self.cell(r, c)
        return x is not None and x["row"] == r and x["col"] == c

    def num(self, r: int, c: int) -> Any:
        x = self.cell(r, c)
        return x["val"] if x is not None and x["row"] == r and x["col"] == c and x["vtype"] in NUMERIC else None

    def distinct(self, r: int) -> list[dict[str, Any]]:
        seen: list[int] = []
        for c in range(self.n_cols):
            i = self.at[r][c]
            if i >= 0 and i not in seen and self.cells[i]["vtype"] != "EMPTY":
                seen.append(i)
        return [self.cells[i] for i in seen]

    def own_texts(self, r: int) -> list[int]:
        return [c for c in range(self.n_cols) if self.filled(r, c) and self.num(r, c) is None and self.origin(r, c)]

    def numbering_row(self, r: int) -> bool:
        """Column numbers under a header: whole numbers only, rising by one from 1 or 2 — one number may be missing
        (a cell the OCR dropped: «1 2 3 5 6 7 8 9»)."""
        nums = [self.num(r, c) for c in range(self.n_cols) if self.num(r, c) is not None]
        if len(nums) < 3 or any(self.filled(r, c) and self.num(r, c) is None for c in range(self.n_cols)):
            return False
        seq = [x.vmin for x in nums]
        if not all(float(x).is_integer() and x == y for x, y in ((v.vmin, v.vmax) for v in nums)):
            return False
        steps = [b - a for a, b in zip(seq, seq[1:])]
        return all(s == 1 for s in steps) or (seq[0] in (1, 2) and all(s in (1, 2) for s in steps)
                                              and steps.count(2) == 1 and len(seq) >= 5)

    def numeric_cell(self, x: dict[str, Any]) -> bool:
        """A number, or several numbers with at most one letter («13.4-25.8/19.2»; not «1+∫0 t Kdτ»)."""
        return x["vtype"] in NUMERIC or (x["vtype"] == "MULTI" and x.get("letters", 0) <= 1)

    def value_row(self, r: int) -> bool:
        """A row with a number (a numeric or multi-number cell at its origin) that is not a column numbering: the
        header ends above it. (The parameter part asks for numbers in half of the filled cells; a table of text with
        one number per row — a stratigraphic column — would lose its rows into the header.)"""
        return any(self.origin(r, c) and self.numeric_cell(self.cells[self.at[r][c]]) for c in range(self.n_cols)) \
            and not self.numbering_row(r)

    def group_row(self, r: int) -> bool:
        texts = self.own_texts(r)
        if len(texts) != 1 or any(self.num(r, c) for c in range(self.n_cols)):
            return False
        return self.cell(r, texts[0])["col_span"] >= max(2, self.n_cols - 1) or all(
            not self.filled(r, c) for c in range(self.n_cols) if self.at[r][c] != self.at[r][texts[0]])

    def header_like(self, r: int) -> bool:
        cells = self.distinct(r)
        return len(cells) >= 2 and all(not self.numeric_cell(x) and len(x["clean"]) <= 80 for x in cells)

    def row_key(self, r: int) -> tuple[str, ...]:
        return tuple(P.lower_same_length(re.sub(r"\s+", "", x["clean"])) for x in self.distinct(r))

    def units_row(self, r: int) -> bool:
        cells = self.distinct(r)
        units = sum(1 for x in cells if x["vtype"] == "UNIT")
        return units >= 1 and units >= len(cells) - 1 and all(x["vtype"] in ("UNIT", "TEXT", "SYMBOL") for x in cells)

    def col_pattern(self, rows: list[int]) -> list[str]:
        """Per column over ``rows``: N when ≥ 40 % of the rows show a number there, T when ≥ 40 % show text, else
        '' (a merged cell counts for every row it covers; a few rows shifted by the OCR do not decide; a cell of
        several numbers — or a code like «M6-1 M6-2» — counts for neither)."""
        out = []
        n = max(1, len(rows))
        for c in range(self.n_cols):
            types = [self.cells[self.at[r][c]]["vtype"] for r in rows if self.filled(r, c)]
            num = sum(1 for t in types if t in NUMERIC)
            text = sum(1 for t in types if t in ("TEXT", "SYMBOL", "UNIT"))
            out.append("N" if num >= 0.4 * n else "T" if text >= 0.4 * n else "")
        return out


def _header_zone(g: _Grid, rows: list[int], th_rows: set[int], max_header: int, trust_th: bool,
                 more: bool = False) -> tuple[int, str]:
    """Header rows at the top of ``rows`` and how they were found: TH (EPUB/DOCX ``<th>``), DETECTED (above the first
    row with a number), CAPPED (header-like rows when that row is far down), TEXT_TABLE, HEADER_BAND (a first band
    of header rows only, ``more`` bands follow), NONE."""
    if not rows:
        return 0, "NONE"
    if trust_th:
        k = 0
        while k < len(rows) and rows[k] in th_rows:
            k += 1
        if 0 < k < len(rows) and k <= max_header:
            return k, "TH"
    first = next((i for i, r in enumerate(rows) if g.value_row(r)), None)
    if first is None and more and len(rows) <= max_header and g.header_like(rows[0]):
        return len(rows), "HEADER_BAND"
    if first is None:
        return (1, "TEXT_TABLE") if len(rows) >= 2 and g.header_like(rows[0]) else (0, "NONE")
    if first <= max_header:
        return first, "DETECTED"
    return (1 if g.header_like(rows[0]) else 0), "CAPPED"   # rows of text before far-away numbers: body rows


def _fits(g: _Grid, blk: dict[str, Any], rows: list[int], width: int) -> bool:
    """A band continues a block. A band of the block's width does unless its columns contradict the block's (numbers
    in one, text in the other) with no column of numbers in common. A band of another width must, over the columns
    both have, contradict none, share a column of numbers and have ≥ 2 filled columns in common (text columns may
    otherwise differ: hatching symbols, labels shifted by a lost row span). Against a block that is only a header (or
    whose body the OCR mostly dropped): the same width, or every filled column of the band has a header."""
    w = min(width, blk["width"])
    body = [r for r in blk["rows"][blk["n_header"]:] if r not in blk["repeated"]]
    b = g.col_pattern(rows)[:w]
    a = g.col_pattern(body)[:w] if body else []
    if sum(1 for x in a if x) < 2:          # only a header, or a body the OCR mostly dropped: the header decides
        heads = [any(g.filled(r, c) for r in blk["rows"][:blk["n_header"]]) for c in range(w)]
        return width == blk["width"] or (any(b) and all(h for x, h in zip(b, heads) if x))
    both = [(x, y) for x, y in zip(a, b) if x and y]
    conflict = any((x == "N") != (y == "N") for x, y in both)
    shared = any(x == y == "N" for x, y in both)
    if width == blk["width"]:
        return not conflict or shared
    return not conflict and shared and len(both) >= 2


def _blocks(g: _Grid, bands: dict[str, Any], max_header: int, trust_th: bool
            ) -> tuple[list[dict[str, Any]], list[str]]:
    """Blocks of rows under one header (:func:`_fits`). A band with a header of its own starts a new block (a repeat
    of the block's header keeps the block and marks the rows); a band that fits the last block with a header joins
    it (a wider band then has columns without a header); a band that fits neither it nor the block without header
    just before it is a block without a header (``aligned`` false)."""
    flags: list[str] = []
    band_of, widths, th_rows = bands.get("band_of"), bands.get("widths") or [], bands.get("th_rows") or set()
    by_band: dict[int, list[int]] = defaultdict(list)
    for r in range(g.n_rows):
        by_band[band_of[r] if band_of else 0].append(r)
    blocks: list[dict[str, Any]] = []
    for k in sorted(by_band):
        rows = by_band[k]
        width = widths[k] if k < len(widths) else g.n_cols
        if not blocks:                      # a first band of header rows only, when numbers follow in later bands
            later = [r for b in sorted(by_band) if b != k for r in by_band[b]]
            n, how = _header_zone(g, rows, th_rows, max_header, trust_th,
                                  more=any(g.value_row(r) for r in later))
            blocks.append({"rows": list(rows), "bands": [k], "n_header": n, "how": how, "aligned": True,
                           "width": width, "repeated": set()})
            continue
        head_blk = next(b for b in reversed(blocks) if b["aligned"])
        n, _how = _header_zone(g, rows, th_rows, max_header, trust_th)
        own = n if 0 < n < len(rows) and g.header_like(rows[0]) else 0
        if own:                             # a header names the columns of numbers below it (a group label does not)
            value_cols = [c for c, x in enumerate(g.col_pattern(rows[own:])) if x == "N"]
            named = [c for c in value_cols if any(g.filled(r, c) for r in rows[:own])]
            own = own if value_cols and len(named) >= 0.5 * len(value_cols) else 0
        if own:
            head = [g.row_key(r) for r in head_blk["rows"][:head_blk["n_header"]]]
            mine = [g.row_key(r) for r in rows[:own]]
            if head and mine == head[:len(mine)]:
                head_blk["rows"] += rows
                head_blk["bands"].append(k)
                head_blk["repeated"].update(rows[:own])
                flags.append("REPEATED_HEADER_ROWS")
            else:
                blocks.append({"rows": list(rows), "bands": [k], "n_header": own, "how": "BAND_HEADER",
                               "aligned": True, "width": width, "repeated": set()})
                flags.append("BAND_OWN_HEADER")
        elif _fits(g, head_blk, rows, width):
            head_blk["rows"] += rows
            head_blk["bands"].append(k)
        elif not blocks[-1]["aligned"] and _fits(g, blocks[-1], rows, width):
            blocks[-1]["rows"] += rows
            blocks[-1]["bands"].append(k)
        else:
            blocks.append({"rows": list(rows), "bands": [k], "n_header": 0, "how": "NONE", "aligned": False,
                           "width": width, "repeated": set()})
            flags.append("BAND_COLUMNS_MISMATCH")
    for b in blocks:
        b["rows"].sort()
    return blocks, flags


def _label_cols(g: _Grid, rows: list[int], heads: dict[int, str]) -> tuple[list[int], set[int]]:
    """Leading label columns of a block's value rows (the parameter part's rule): text-dominated or an index column."""
    labels: list[int] = []
    index: set[int] = set()
    for c in range(g.n_cols):
        fl = [r for r in rows if g.filled(r, c)]
        nums = [r for r in fl if g.num(r, c) is not None]
        seq = [g.num(r, c).vmin for r in nums]
        head = heads.get(c) or ""
        is_index = (bool(fl) and len(nums) == len(fl) and len(seq) >= 2 and all(float(x).is_integer() for x in seq)
                    and all(b - a == 1 for a, b in zip(seq, seq[1:])) and P.parse_header(head).prop is None) or \
            bool(P._NUMBERING_HEADER.match(head or "x"))
        if is_index or (fl and len(nums) <= 0.3 * len(fl)):
            labels.append(c)
            if is_index:
                index.add(c)
        elif nums:
            break
    return labels, index


def _column_type(types: list[str]) -> str:
    if not types:
        return "EMPTY"
    n = len(types)
    for name, k in (("NUMERIC", sum(1 for t in types if t in NUMERIC)), ("UNIT", types.count("UNIT")),
                    ("SYMBOL", types.count("SYMBOL")), ("TEXT", types.count("TEXT") + types.count("MULTI"))):
        if k >= 0.8 * n:
            return name
    return "MIXED"


def _unit_fields(u: Any) -> dict[str, Any]:
    if u is None:
        return {"unit_raw": None, "unit_canonical": None, "unit_dim": None}
    return {"unit_raw": u.raw.strip(), "unit_canonical": u.canon, "unit_dim": u.dim}


_UNIT_CLOSES = re.compile(r"\s*(?:$|[)\](,;:/]|[·⋅∙×x*]\s*10|(?:min|max|мин|макс|ср|средн)\w*\.?)", re.I)
# units the parameter vocabulary leaves out (it serves the target properties): temperature and the velocity of radar
# waves — read for the unit of a table column only, never by the parameter part; «K» only in brackets («T (K)»; a
# «K» after a comma may be a symbol: «Модуль объемного сжатия, K»)
_EXTRA_UNITS: tuple[tuple[re.Pattern, str, float, str], ...] = (
    (re.compile(r"(?:°\s?[CС]|℃)(?![^\W\d_])"), "temperature", math.nan, "°C"),
    (re.compile(r"(?:см|cm)\s?/\s?(?:нс|ns)(?![^\W\d_])", re.I), "velocity", 1e7, "см/нс"),
    (re.compile(r"(?:м|m)\s?/\s?(?:нс|ns)(?![^\W\d_])", re.I), "velocity", 1e9, "м/нс"),
)
_KELVIN = (re.compile(r"K(?![^\W\d_])"), "temperature", 1.0, "K")
_UNIT_SEP = re.compile(r"(?:,|\(|(?<![\w-])(?:в|in)\s)\s*")


def _extra_unit(text: str) -> Any:
    """A unit of :data:`_EXTRA_UNITS` right after a separator («Температура, °С», «Temperature(℃)», «Скорость, см/нс»),
    or «K» alone in brackets («T(K)»)."""
    for m in _UNIT_SEP.finditer(text):
        pos = m.end()
        for rx, dim, f, canon in _EXTRA_UNITS:
            u = rx.match(text, pos)
            if u:
                return P.Unit(u.group(0), dim, f, canon, u.start(), u.end())
        k = _KELVIN[0].match(text, pos)
        if k and text[m.start()] == "(" and re.match(r"\s*\)", text[k.end():]):
            return P.Unit("K", _KELVIN[1], _KELVIN[2], _KELVIN[3], k.start(), k.end())
    return None


def column_unit(text: str) -> Any:
    """The unit of one header level (``parameters.header_unit``, then temperature and radar-wave velocity) when it
    stands clear: followed by nothing, a closing bracket, a separator or a statistic («km s–1» is not km); square
    brackets read as round ones («Bias [mm/yr]»); a one-letter unit only after a comma or as the closing bracket
    («Длина, м», «Глубина (м)», «T (K)»; not «Height H», «Case (a) …», «h»)."""
    norm = text.replace("[", "(").replace("]", ")")
    u = P.header_unit(norm) or _extra_unit(norm)
    if u is None or (u.dim == V.TIME and u.canon == "год" and (not norm[:u.start].strip(" ,(:")
                                                               or u.raw.strip().lower() == "year")):
        return None                                         # «Год», «Author(s), Year»: a column of calendar years
    rest = norm[u.end:]
    ok = bool(_UNIT_CLOSES.match(rest) or P._CONDITION_TAIL.match(norm, u.end))
    if ok and len(u.raw.strip()) == 1 and u.raw.strip() not in "%‰°":
        before = norm[:u.start].rstrip()
        closing = before.endswith("(") and re.fullmatch(r"\s*\)\s*", rest)
        ok = before.endswith(",") or bool(closing)
    if ok:
        return u
    return column_unit(rest) if rest.strip() else None     # «Высота (H), м»: the unit after a symbol


# ---- units of a caption
_CAPTION_LIST = re.compile(r"\[([^\[\]]*;[^\[\]]*)\]")     # «[Temperature, T, K; Creep Rate, ε̇, min^-1]»
_CAPTION_LEGEND = re.compile(r";|(?<![\w-])(?:где|where)(?![\w-])", re.I)
_CAPTION_NOTE = re.compile(r"\s*\(\s*(?:n\s*=\s*\d+|продолжение|окончание|cont(?:inued|\.)?|end)\s*\)\s*$", re.I)
_CAPTION_SEP = re.compile(r"(?:,|\(|\[|(?<![\w-])(?:в|in)\s)\s*(?:(?:вес|масс?|мол|об|wt|vol|mol|mass)\.?\s*)?", re.I)
# an argument column (time, a coordinate, a number, a year): never takes the unit of the values from the caption
_ARGUMENT_HEADER = re.compile(r"\s*(?:t|τ|x|y|z|№(?:\s*п/п)?|n|h/d|l/h|год\w*|year\w*|дата|date)\s*", re.I)


def _whole_unit(text: str) -> Any:
    """A unit that is the whole of a short text («K», «MPa», «min^-1», «%»), else None."""
    t = text.strip()
    if not t:
        return None
    u = P.match_unit(t, 0)
    if u is not None:
        return u if not t[u.end:].strip() else None
    for rx, dim, f, canon in (*_EXTRA_UNITS, _KELVIN):
        if rx.fullmatch(t):
            return P.Unit(t, dim, f, canon, 0, len(t))
    return None


def _norm_key(text: str | None) -> str:
    return re.sub(r"[\s_{}$\\()\[\]]+", "", P.lower_same_length(text or ""))


def caption_units(caption: str | None) -> dict[str, Any]:
    """Units a table caption gives its value columns: ``clauses`` — (name, symbol, unit) of a list caption «[Confining
    Pressure, P, GPa; Differential Stress, (σ1−σ3) MPa]», each for the column headed by that symbol or name; ``unit``
    — one unit that closes the caption («…, МПа», «… (кг/м³)», «…, вес. %»; a note «(n = 61)» may follow) or stands
    alone in brackets («Mean RMS (mm) for all baselines»), when no other unit follows a separator, the caption is no
    legend («где h – …; v – …») and the unit is no time or year («… (Days)», «2000–2021 гг.»: an axis or a period, not
    the values); a one-letter unit only «%», «‰», «°», «м», «m»."""
    out: dict[str, Any] = {"unit": None, "clauses": []}
    text = caption or ""
    lm = _CAPTION_LIST.search(text)
    if lm:
        for clause in lm.group(1).split(";"):
            parts = [p.strip() for p in clause.split(",") if p.strip()]
            if len(parts) < 2:
                continue
            sym = parts[1] if len(parts) >= 3 else None
            u = _whole_unit(parts[-1])
            if u is None:
                u = column_unit(parts[-1])                 # «(σ1−σ3) MPa»: the symbol and the unit in one part
                if u is None:
                    continue
                sym = parts[-1][:u.start].strip() or sym
            out["clauses"].append((_norm_key(parts[0]), _norm_key(sym) if sym else None, u))
        return out
    if _CAPTION_LEGEND.search(text):
        return out
    text = _CAPTION_NOTE.sub("", text).rstrip(" .:;")
    found = [u for m in _CAPTION_SEP.finditer(text) for u in (P.match_unit(text, m.end()),)
             if u is not None and u.complete]
    if not found or len({u.canon for u in found}) > 1:
        return out
    last = found[-1]
    closes = re.fullmatch(r"\s*[)\]]?", text[last.end:])
    bracketed = text[:last.start].rstrip().endswith(("(", "[")) and re.match(r"\s*[)\]]", text[last.end:])
    raw = last.raw.strip()
    if (closes or bracketed) and last.dim != V.TIME and (len(raw) > 1 or raw in "%‰°мm"):
        out["unit"] = last
    return out


def _column_caption_unit(col: dict[str, Any], cap: dict[str, Any], cap_prop: Any) -> Any:
    """The caption unit of a value column without a unit of its own (:func:`caption_units`): the clause naming its
    symbol or name, else the caption's unit unless the column is an argument or names another property (the values
    under an axis — «Слой / 9», «Слой / 10» — take it)."""
    if col.get("role") != "VALUE":
        return None
    head = col.get("header_text") or ""
    if cap["clauses"]:
        hk = _norm_key(head)
        hits = [u for name, sym, u in cap["clauses"]
                if hk and ((sym and hk == sym) or (len(name) >= 4 and name in hk))]
        return hits[0] if len(hits) == 1 else None
    u = cap["unit"]
    if u is None or _ARGUMENT_HEADER.fullmatch(head):
        return None
    key = col.get("property_key")
    if key is not None and ((cap_prop is not None and key != cap_prop.key)
                            or not P._dims_ok(V.PROPERTY_BY_KEY[key], u)):
        return None
    return u


def _level_unit(levels: list[str]) -> Any:
    """Unit of a column from its header levels, the lowest (most specific) level first (:func:`column_unit`)."""
    for text in reversed(levels):
        u = column_unit(text)
        if u is not None:
            return u
    return None


def decimal_suspects(values: list[tuple[Any, str, float]]) -> set[Any]:
    """Keys of whole numbers that look like a decimal whose point the OCR lost («218» between «1,24» and «2,51»).
    ``values`` — (key, printed text, value) of one column in row order. A whole number is a suspect when ≥ 70 % of
    the column is printed with d ≥ 1 decimals and D digits in all (the most common pair), it has exactly D digits, is
    not a year, its neighbours above and below (one at the column's edge) are such decimals, and it is 10^d times
    their mean within half an order. Flag only: the printed value stays."""
    info = []
    for k, t, v in values:
        s = t.strip()
        m = re.fullmatch(r"[-−–]?(\d+)[.,](\d+)", s)
        if m:
            info.append((k, "D", len(m.group(2)), len(m.group(1)) + len(m.group(2)), v))
        elif re.fullmatch(r"[-−–]?\d+", s):
            info.append((k, "I", 0, len(s.lstrip("-−–")), v))
        else:
            info.append((k, "X", 0, 0, v))
    decs = [x for x in info if x[1] == "D"]
    if len(info) < 3 or len(decs) < 2 or len(decs) < 0.7 * len(info):
        return set()
    d, total = Counter((x[2], x[3]) for x in decs).most_common(1)[0][0]
    out = set()
    for i, (k, kind, _d, digits, v) in enumerate(info):
        if kind != "I" or digits != total or not v or 1800 <= abs(v) <= 2100:
            continue
        nb = [info[j] for j in (i - 1, i + 1) if 0 <= j < len(info) and info[j][1] == "D" and info[j][2] == d]
        if len(nb) < (1 if i in (0, len(info) - 1) else 2):
            continue
        ref = sum(abs(x[4]) for x in nb) / len(nb)
        if ref and 10 ** (d - 0.5) <= abs(v) / ref <= 10 ** (d + 0.5):
            out.add(k)
    return out


def _empty_structure(struct: dict[str, Any], flags: list[str]) -> dict[str, Any]:
    struct.update({"n_rows": 0, "n_cols": 0, "n_cells": 0, "n_filled_cells": 0, "n_numeric_cells": 0,
                   "n_text_cells": 0, "n_flagged_cells": 0, "n_spanning_cells": 0, "n_header_rows": 0,
                   "header_method": "NONE", "n_bands": 0, "n_blocks": 0, "blocks": [], "orientation": "EMPTY",
                   "confidence": 0.05, "structure_ok": False, "covers_region": False,
                   "quality_flags": sorted(set(flags)), "property_keys": [], "materials": [],
                   "caption_property_key": None})
    return {"structure": struct, "cells": [], "columns": []}


def structure_table(t: dict[str, Any], *, lang: str | None = None, source_symbols: dict[str, str] | None = None,
                    is_word: Callable[[str], bool] | None = None, max_header: int = 5,
                    max_grid: int = 100_000) -> dict[str, Any]:
    """Structure of one canonical table row (pure): ``{"structure": {...}, "cells": [...], "columns": [...]}``."""
    tid = t["object_id"]
    nid = nav_ids.table_id(tid)
    method = t.get("recognition_method") or "UNKNOWN"
    flags: list[str] = [f for f in _CANON_FLAGS if f in (t.get("quality_flags") or [])]
    base = {"table_id": tid, "nav_table_id": nid, "source_id": t.get("source_id"), "page_id": t.get("page_id")}
    label = t.get("table_label")
    caption = re.sub(r"\s+", " ", t.get("caption") or "").strip() or None
    if _FIGURE_LABEL.match(label or "") or (not label and _FIGURE_LABEL.match(caption or "")):
        flags.append("FIGURE_SUSPECT")
    struct: dict[str, Any] = {
        **base, "table_label": label, "table_number": table_number(label, caption), "caption": caption,
        "recognition_method": method, "raw_format": t.get("raw_format"), "origin": t.get("origin"),
        "parse_method": f"{method}:{t.get('raw_format') or '-'}", "bbox_x0": t.get("bbox_x0"),
        "bbox_y0": t.get("bbox_y0"), "bbox_x1": t.get("bbox_x1"), "bbox_y1": t.get("bbox_y1"),
        "bbox_space": t.get("bbox_space"), "review_status": REVIEW_STATUS, "rule_version": RULE_VERSION}
    cells = [dict(c) for c in (t.get("cells") or [])]
    if not cells:
        return _empty_structure(struct, flags + ["NO_CELLS"])
    for c in cells:
        c["row"], c["col"] = int(c.get("row") or 0), int(c.get("col") or 0)
        c["row_span"], c["col_span"] = max(1, int(c.get("row_span") or 1)), max(1, int(c.get("col_span") or 1))
        c["clean"], c["flags"] = clean_cell_text(c.get("text"))
        v = cell_value(c["clean"], lang)
        c.update({"vtype": v["value_type"], "val": v.get("val"), "n_numbers": v.get("n_numbers"),
                  "unit_only": v.get("unit"), "letters": len(re.findall(r"[^\W\d_]", c["clean"]))})
        c["flags"] += v.get("flags", [])
    cells.sort(key=lambda c: (c["row"], c["col"]))
    n_rows = int(t.get("n_rows") or max(c["row"] + c["row_span"] for c in cells))
    n_cols = int(t.get("n_cols") or max(c["col"] + c["col_span"] for c in cells))
    ocr = t.get("raw_format") == "HTML"
    if n_rows * n_cols > max_grid:
        return _flat(struct, cells, n_rows, n_cols, flags + ["TOO_LARGE"])
    bands = resolve_bands(t.get("raw_output"), cells) if ocr else \
        {"band_of": None, "widths": [], "th_rows": {c["row"] for c in cells if c.get("is_header")}, "n_bands": 1}
    if bands["band_of"] is None and bands["n_bands"] > 1:
        flags.append("BANDS_UNRESOLVED")
    g = _Grid(cells, n_rows, n_cols)
    if g.overlaps:
        flags.append("OVERLAPPING_CELLS")
    if any(g.at[r][c] < 0 for r in range(n_rows) for c in range(n_cols)):
        flags.append("RAGGED_ROWS")
    blocks, bflags = _blocks(g, bands, max_header, trust_th=not ocr)
    flags += bflags
    cap_text = P.cell_plain(" ".join(x for x in (label, caption) if x))
    cap = P.parse_header(cap_text, source_symbols) if cap_text else P.HeaderInfo("")
    role: dict[int, str] = {}
    block_of: dict[int, int] = {}
    row_info: dict[int, dict[str, Any]] = {}
    col_info: dict[tuple[int, int], dict[str, Any]] = {}
    columns: list[dict[str, Any]] = []
    for b_i, blk in enumerate(blocks):
        _structure_block(g, blk, b_i, role, block_of, row_info, col_info, columns, flags, source_symbols, is_word,
                         t, nid)
    cap_units = caption_units(None if "FIGURE_SUSPECT" in flags else cap_text)
    cap_by_col: dict[tuple[int, int], Any] = {}
    if cap_units["unit"] is not None or cap_units["clauses"]:
        cap_prop = cap.prop if not cap.ambiguous else None
        for key, ci in col_info.items():
            u = None if ci.get("unit") is not None else _column_caption_unit(ci["rec"], cap_units, cap_prop)
            if u is not None:
                cap_by_col[key] = u
                ci["rec"].update(_unit_fields(u))
                ci["rec"]["unit_source"] = "CAPTION"
    cells_out = _cell_records(g, cells, blocks, bands, role, block_of, row_info, col_info, cap_by_col, base, nid)
    # ---- orientation, decimal suspects, table level
    n_num = sum(1 for c in cells if c["vtype"] in NUMERIC)
    first = blocks[0]
    col_props = {(x["block"], x["col"]) for x in columns if x["property_key"] and x["role"] == "VALUE"}
    row_props = {r for r, i in row_info.items() if i.get("prop") is not None and role.get(r) in VALUE_ROWS}
    if not n_num:
        orientation = "TEXT_ONLY"
    elif any(v == "AXIS" for v in role.values()):
        orientation = "MATRIX"
    elif n_cols == 2 and first.get("labels") == [0] and 0 not in first.get("index", ()):
        orientation = "KEY_VALUE"                   # «name | value» rows (an index column is a normal table)
    elif len(row_props) > len(col_props):
        orientation = "TRANSPOSED"
    elif col_props or first["n_header"]:
        orientation = "NORMAL"
    else:
        orientation = "UNKNOWN"
    if orientation in ("NORMAL", "UNKNOWN"):
        # a row of whole numbers only (a count of tests whose label the OCR lost) is not a row of lost decimal points
        whole: dict[int, list[bool]] = defaultdict(list)
        for c, rec in zip(cells, cells_out):
            if c["val"] is not None and rec["column_role"] == "VALUE":
                whole[rec["row"]].append(bool(re.fullmatch(r"[-−–]?\d+", c["val"].text.strip())))
        count_rows = {r for r, w in whole.items() if len(w) >= 2 and all(w)}
        pools: dict[tuple[int, int], list[tuple[Any, str, float]]] = defaultdict(list)
        for c, rec in zip(cells, cells_out):
            ci = col_info.get((rec["block"], rec["col"]))
            if c["val"] is not None and rec["row_role"] in VALUE_ROWS and ci and ci["rec"]["role"] == "VALUE" \
                    and ci["rec"]["header_path"] and rec["row_property_key"] is None and rec["row"] not in count_rows:
                pools[(rec["block"], rec["col"])].append((id(rec), c["val"].text.strip(), c["val"].vmin))
        hit = set().union(*(decimal_suspects(v) for v in pools.values())) if pools else set()
        for rec in cells_out:
            if id(rec) in hit:
                rec["flags"].append("DECIMAL_POINT_SUSPECT")
    for rec in cells_out:
        rec["flags"] = sorted(set(rec["flags"]))
    for cf, tf in (("DECIMAL_POINT_SUSPECT", "DECIMAL_POINT_SUSPECT"), ("MULTI_VALUE", "MULTI_VALUE_CELLS"),
                   ("HEADER_RESPACED", "HEADER_RESPACED"), ("UNIT_CONFLICT", "UNIT_CONFLICT")):
        if any(cf in r["flags"] for r in cells_out):
            flags.append(tf)
    if not any(role.get(r) in HEADER_ZONE for r in first["rows"]) and n_num and first["how"] != "TEXT_TABLE":
        flags.append("NO_HEADER")
    if first["how"] == "CAPPED":
        flags.append("HEADER_CAPPED")
    area = sum(min(c["row_span"], n_rows - c["row"]) * min(c["col_span"], n_cols - c["col"])
               for c in cells if c["vtype"] != "EMPTY")
    if n_rows * n_cols >= 12 and area < 0.35 * n_rows * n_cols:
        flags.append("SPARSE")
    fl = set(flags)
    conf = _BASE_CONFIDENCE.get(method, 0.6) - sum(_PENALTIES.get(f, 0.0) for f in fl)
    ok = n_rows >= 2 and n_cols >= 2 and not ({"REPETITION", "EMPTY_ON_INK"} & fl)
    covers = ok and n_num > 0 and not ({"TRUNCATED", "FIGURE_SUSPECT", "BANDS_UNRESOLVED"} & fl) \
        and t.get("bbox_space") == "PAGE_PT_TL"
    props = sorted({x["property_key"] for x in columns if x["property_key"]}
                   | {i["prop"].key for i in row_info.values() if i.get("prop") is not None}
                   | ({cap.prop.key} if cap.prop is not None and not cap.ambiguous else set()))
    mats = sorted({m for x in columns for m in x["materials"]}
                  | {m for i in row_info.values() for m in i.get("materials", [])} | {m.label for m in cap.materials})
    struct.update({
        "n_rows": n_rows, "n_cols": n_cols, "n_cells": len(cells),
        "n_filled_cells": sum(1 for c in cells if c["vtype"] != "EMPTY"), "n_numeric_cells": n_num,
        "n_text_cells": sum(1 for c in cells if c["vtype"] == "TEXT"),
        "n_flagged_cells": sum(1 for r in cells_out if set(r["flags"]) - _BENIGN),
        "n_spanning_cells": sum(1 for c in cells if c["row_span"] > 1 or c["col_span"] > 1),
        "n_header_rows": sum(1 for r in first["rows"] if role.get(r) in HEADER_ZONE),
        "header_method": first["how"], "n_bands": max(1, bands["n_bands"]), "n_blocks": len(blocks),
        "blocks": [{"block": i, "row_start": b["rows"][0], "row_end": b["rows"][-1],
                    "n_header_rows": sum(1 for r in b["rows"] if role.get(r) in HEADER_ZONE),
                    "aligned": b["aligned"], "bands": list(b["bands"])} for i, b in enumerate(blocks)],
        "orientation": orientation, "confidence": round(max(0.05, min(0.95, conf)), 2), "structure_ok": ok,
        "covers_region": covers, "quality_flags": sorted(fl), "property_keys": props, "materials": mats,
        "caption_property_key": cap.prop.key if cap.prop is not None and not cap.ambiguous else None})
    return {"structure": struct, "cells": cells_out, "columns": columns}


def _structure_block(g: _Grid, blk: dict[str, Any], b_i: int, role: dict[int, str], block_of: dict[int, int],
                     row_info: dict[int, dict[str, Any]], col_info: dict[tuple[int, int], dict[str, Any]],
                     columns: list[dict[str, Any]], flags: list[str], source_symbols: dict[str, str] | None,
                     is_word: Callable[[str], bool] | None, t: dict[str, Any], nid: str) -> None:
    """Roles of the rows, header paths, label columns, column records and row labels of one block (in place)."""
    rows, n_head, n_cols = blk["rows"], blk["n_header"], g.n_cols
    for r in rows:
        block_of[r] = b_i
    head_rows = rows[:n_head]
    for i, r in enumerate(head_rows):
        texts = g.own_texts(r)
        if g.numbering_row(r):
            role[r] = "NUMBERING_HEAD"
        elif i > 0 and g.units_row(r):
            role[r] = "UNITS"
        elif i > 0 and g.group_row(r) and (g.cell(r, texts[0])["col_span"] >= 2 or i == n_head - 1):
            role[r] = "GROUP"                     # a group label inside the header zone (P's skip_head), or right
                                                  # above the first row of numbers («Ⅰ» over its rows)
        elif i == 0 and n_head > 1 and len(texts) == 1 and \
                g.cell(r, texts[0])["col_span"] >= max(2, (2 * n_cols) // 3):
            role[r] = "TITLE"
        else:
            role[r] = "HEADER"
    body = rows[n_head:]
    axis_cols: set[int] = set()                   # columns under an axis: their values are of another quantity
    if 0 < n_head < len(rows) - 1:                # an axis row under the header (the parameter part's rule)
        r0 = rows[n_head]
        num_cols = [c for c in range(n_cols) if g.num(r0, c) is not None]
        spanning = Counter(g.at[rows[n_head - 1]][c] for c in num_cols if g.filled(rows[n_head - 1], c))
        if spanning and max(spanning.values()) >= 3:
            # the numbers under one spanning header cell rise or fall; any text of the row stands outside it and is
            # short («Пиковая прочность на сдвиг при σn, МПа» over «1,0 | 3,0 | 5,0» next to «C, МПа | tgφ»), and no
            # label of its own stands left of the numbers (a data row has one)
            span = spanning.most_common(1)[0][0]
            under = [c for c in range(n_cols) if g.at[rows[n_head - 1]][c] == span]
            inside = [c for c in num_cols if c in under]
            texts = g.own_texts(r0)
            if len(inside) >= 3 and set(num_cols) <= set(under) and \
                    P._monotonic([g.num(r0, c).vmin for c in inside]) and \
                    all(c not in under and len(g.text(r0, c)) <= 24 for c in texts) and \
                    not any(g.origin(r0, c) and g.filled(r0, c) for c in range(min(under))):
                role[r0] = "AXIS"
                body = rows[n_head + 1:]
                if _AXIS_CONDITION.search(g.cells[span]["clean"]):
                    pass     # «… при различных нормальных напряжениях, МПа»: the body is the quantity named above
                else:        # «Вязкость η, Па·с» over 0,3 0,4 0,5: the axis holds that quantity, the body another
                    role[rows[n_head - 1]], axis_cols = "AXIS_TITLE", set(under)
    axis_title: tuple[int, str] | None = None
    if n_head == 0 and len(rows) >= 3 and blk["aligned"]:
        # an axis on top of a table without a header: a label naming a quantity with its unit (or numbers sharing one
        # unit, such as longitudes), then ≥ 3 rising or falling numbers («confining pressure [MPa] | 0.1 | 0.5 | 1.0»);
        # the label stands left of the numbers or at the right edge
        r0 = rows[0]
        texts = g.own_texts(r0)
        num_cols = [c for c in range(n_cols) if g.num(r0, c) is not None]
        units = {g.num(r0, c).unit.canon if g.num(r0, c).unit is not None else None for c in num_cols}
        side = [c for c in texts if c < num_cols[0]] or [c for c in texts if c > num_cols[-1]] if num_cols else []
        if side and len(num_cols) >= 3 and all(c < num_cols[0] or c > num_cols[-1] for c in texts) and \
                P._monotonic([g.num(r0, c).vmin for c in num_cols]) and \
                (column_unit(g.text(r0, side[0])) or (len(units) == 1 and None not in units)):
            role[r0], axis_title, axis_cols = "AXIS", (side[0], g.text(r0, side[0])), set(num_cols)
            body = rows[1:]
    for r in blk["repeated"]:
        role[r] = "REPEATED_HEADER"
    path_rows = [r for r in rows if role.get(r) in ("HEADER", "AXIS_TITLE", "AXIS")]
    units_rows = [r for r in rows if role.get(r) == "UNITS"]

    def header_levels() -> dict[int, list[str]]:
        out: dict[int, list[str]] = {}
        for c in range(n_cols):
            levels: list[str] = []
            last = -2
            for r in path_rows:
                i = g.at[r][c]
                if i < 0 or i == last or not g.filled(r, c):
                    continue
                last = i
                if axis_title is not None and role.get(r) == "AXIS" and r == rows[0]:
                    if c == axis_title[0]:
                        continue                  # the corner cell names the axis, not its own column
                    levels.append(axis_title[1])
                levels.append(g.cells[i]["clean"])
            out[c] = levels
        return out

    heads = header_levels()
    body_rows = [r for r in body if r not in blk["repeated"]]
    value_rows = [r for r in body_rows if any(g.num(r, c) is not None for c in range(n_cols))]
    labels, index_cols = _label_cols(g, value_rows, {c: " ".join(v) for c, v in heads.items()})
    if axis_title is not None and axis_title[0] not in labels:     # the column under the corner holds the other axis
        labels = sorted(set(labels) | {axis_title[0]})
    if is_word is not None:                       # glued words of header cells and row labels
        touched = {g.at[r][c] for r in path_rows for c in range(n_cols) if g.at[r][c] >= 0}
        touched |= {g.at[r][c] for r in body_rows for c in labels if g.at[r][c] >= 0}
        for i in sorted(touched):
            fixed = respace(g.cells[i]["clean"], is_word)
            if fixed:
                g.cells[i]["clean"] = fixed
                g.cells[i]["flags"].append("HEADER_RESPACED")
        heads = header_levels()
    period = 0                                    # two-block tables: the header repeats with a period
    hp = [tuple(heads[c]) for c in range(n_cols)]
    for p in range(2, n_cols // 2 + 1):
        if all(hp[c] == hp[c + p] for c in range(n_cols - p)) and sum(1 for c in range(p) if hp[c]) >= 2:
            period = p
            break
    if period:
        flags.append("TWO_BLOCK")
    # ---- body rows
    has_mean_row = False
    for r in body_rows:
        lab = " ".join(dict.fromkeys(g.text(r, c) for c in labels if g.filled(r, c)))
        row_info[r] = {"label": lab or None, "stat": stat_of(lab)}
        has_mean_row |= row_info[r]["stat"] == "MEAN"
    symbols = {r: _stat_symbol(row_info[r]["label"]) for r in body_rows}
    if len({s for s in symbols.values() if s}) >= 2:   # «x | σ | V» rows of a statistics table
        for r, s in symbols.items():
            if s and row_info[r]["stat"] is None:
                row_info[r]["stat"] = s
                has_mean_row |= s == "MEAN"
    header_keys = {g.row_key(h) for h in head_rows if role.get(h) == "HEADER"}
    for r in body_rows:
        info = row_info[r]
        has_num = any(g.num(r, c) is not None for c in range(n_cols) if c not in labels)
        if info["stat"] is None and has_mean_row and info["label"] and _BARE_DISPERSION.match(info["label"]):
            info["stat"] = "DISPERSION"
        stat = info["stat"]
        if not g.distinct(r):
            role[r] = "EMPTY"
        elif len(g.row_key(r)) >= 2 and g.row_key(r) in header_keys:
            role[r] = "REPEATED_HEADER"
            flags.append("REPEATED_HEADER_ROWS")
        elif g.numbering_row(r):
            role[r] = "NUMBERING"
        elif has_num:
            role[r] = "STAT_" + stat if stat else "DATA"
        elif g.group_row(r):
            role[r] = "GROUP"
        elif r == body_rows[-1] and len(g.own_texts(r)) == 1 and (
                _NOTE_START.match(g.text(r, g.own_texts(r)[0])) or len(g.text(r, g.own_texts(r)[0])) >= 60):
            role[r] = "NOTE"
        else:
            role[r] = "TEXT"
    # ---- columns
    units_col = next((c for c in range(n_cols) if heads[c] and P._UNITS_HEADER.search(" ".join(heads[c]))
                      and P.parse_header(" ".join(heads[c])).prop is None), None)
    has_mean_col = any(stat_of(" ".join(heads[c])) == "MEAN" for c in range(n_cols))
    typed = [r for r in body_rows if role.get(r) in VALUE_ROWS | {"STAT_DISPERSION", "STAT_COUNT", "TEXT"}]
    for c in range(n_cols):
        joined = " ".join(heads[c])
        info = P.parse_header(joined, source_symbols) if joined else P.HeaderInfo("")
        unit, source = None, None
        for r in units_rows:
            x = g.cell(r, c)
            if x is not None and x["vtype"] == "UNIT":
                unit, source = x["unit_only"], "UNITS_ROW"
                break
        if unit is None:
            unit = _level_unit(heads[c])
            source = "HEADER" if unit is not None else None
        types = [g.cells[g.at[r][c]]["vtype"] for r in typed if g.origin(r, c) and g.filled(r, c)]
        ctype = _column_type(types)
        n_num = sum(1 for x in types if x in NUMERIC)
        stat = stat_of(joined)
        if stat is None and has_mean_col and _BARE_DISPERSION.match(joined or ""):
            stat = "DISPERSION"
        if c == units_col:
            crole = "UNITS"
        elif c in index_cols:
            crole = "INDEX"
        elif c in labels:
            crole = "LABEL"
        elif stat == "DISPERSION" or (joined and P._DISPERSION.search(P.lower_same_length(joined))):
            crole = "DISPERSION"
        elif stat == "COUNT":
            crole = "COUNT"
        elif _SYMBOL_HEADER.search(joined) or ctype == "SYMBOL":
            crole = "SYMBOL"
        elif n_num and n_num >= 0.5 * len(types):
            crole = "VALUE"
        elif types:
            crole = "TEXT"
        else:
            crole = "EMPTY"
        prop = info.prop if not info.ambiguous else None
        cflags: list[str] = ["PROPERTY_AMBIGUOUS"] if info.ambiguous else []
        if prop is not None and unit is not None and not P._dims_ok(prop, unit):
            cflags.append("UNIT_PROPERTY_MISMATCH")
            prop = None
        if crole in ("DISPERSION", "COUNT", "INDEX", "LABEL", "UNITS", "SYMBOL", "TEXT", "EMPTY"):
            prop = None
        if c in axis_cols:
            cflags.append("AXIS_COLUMN")
            prop = None
        basis = None if prop is None else (
            "SOURCE_SYMBOL" if info.symbol_source else "SYMBOL" if info.symbol_only else "PHRASE")
        if crole == "VALUE" and not heads[c] and any(heads[x] for x in range(n_cols)):
            cflags.append("NO_HEADER")
            if "HEADER_SHORT" not in flags:
                flags.append("HEADER_SHORT")
        rec = {
            "table_id": t["object_id"], "nav_table_id": nid, "block": b_i, "col": c, "header_path": list(heads[c]),
            "header_text": " / ".join(heads[c]) or None, "symbol": info.symbol, **_unit_fields(unit),
            "unit_source": source, "multiplier_exp": info.multiplier_exp, "stat": stat, "column_type": ctype,
            "role": crole, "col_group": (c // period) if period else 0, "n_filled": len(types), "n_numeric": n_num,
            "property_key": prop.key if prop else None, "property_label": prop.label_ru if prop else None,
            "property_basis": basis, "property_ambiguous": bool(info.ambiguous),
            "materials": sorted({m.label for m in info.materials}), "flags": sorted(set(cflags)),
            "review_status": REVIEW_STATUS, "rule_version": RULE_VERSION}
        columns.append(rec)
        col_info[(b_i, c)] = {"rec": rec, "unit": unit, "source": source, "prop": prop}
    # ---- row labels: property, unit (units column, the label, then the group row above), multiplier, materials
    group_unit = None
    for r in body_rows:
        info = row_info[r]
        lab = info["label"] or ""
        rh = P.parse_header(lab, source_symbols) if lab else P.HeaderInfo("")
        info.update({"prop": rh.prop if not rh.ambiguous else None, "unit": None, "unit_source": None,
                     "multiplier_exp": rh.multiplier_exp, "materials": [m.label for m in rh.materials]})
        if role.get(r) in ("GROUP", "REPEATED_HEADER"):
            own = g.own_texts(r)
            group_unit = column_unit(g.text(r, own[0])) if role[r] == "GROUP" and own else None
        if units_col is not None:
            x = g.cell(r, units_col)
            u = x["unit_only"] if x is not None and x["vtype"] == "UNIT" else (
                P.match_unit(x["clean"], 0) if x is not None and x["clean"] else None)
            if u is not None:
                info["unit"], info["unit_source"] = u, "UNITS_COLUMN"
        if info["unit"] is None and lab and column_unit(lab) is not None:
            info["unit"], info["unit_source"] = column_unit(lab), "ROW_LABEL"
        # «Основные размеры, мм:» over «длина», «ширина»: the items of a group (lower case, no unit of their own;
        # not «Масса, т» after them)
        if info["unit"] is None and group_unit is not None and role.get(r) in VALUE_ROWS and \
                _GROUP_ITEM.fullmatch(lab):
            info["unit"], info["unit_source"] = group_unit, "GROUP_LABEL"
    blk.update({"labels": labels, "index": index_cols, "period": period})


_GROUP_ITEM = re.compile(r"\s*[-–—•·]?\s*[a-zа-яё][^,()\[\]]*")


def _cell_records(g: _Grid, cells: list[dict[str, Any]], blocks: list[dict[str, Any]], bands: dict[str, Any],
                  role: dict[int, str], block_of: dict[int, int], row_info: dict[int, dict[str, Any]],
                  col_info: dict[tuple[int, int], dict[str, Any]], cap_by_col: dict[tuple[int, int], Any],
                  base: dict[str, Any], nid: str) -> list[dict[str, Any]]:
    out = []
    band_of = bands.get("band_of")
    for c in cells:
        r, k = c["row"], c["col"]
        b_i = block_of.get(r, 0)
        blk = blocks[b_i]
        ci = col_info.get((b_i, k)) or {}
        crec = ci.get("rec") or {}
        rrole = role.get(r, "TEXT")
        info = row_info.get(r, {})
        v = c["val"]
        head = rrole in HEADER_ZONE
        rec = {
            "cell_id": nav_ids.table_cell_id(nid, r, k), **base, "row": r, "col": k, "row_span": c["row_span"],
            "col_span": c["col_span"], "band": band_of[r] if band_of else 0, "block": b_i, "text": c.get("text"),
            "text_clean": c["clean"] or None, "is_header": head, "row_role": rrole,
            "column_role": crec.get("role"), "header_path": [] if head else list(crec.get("header_path") or []),
            "header_text": None if head else crec.get("header_text"),
            "row_label": info.get("label") if k not in blk.get("labels", ()) else None,
            "row_property_key": info["prop"].key if info.get("prop") is not None else None,
            "value_type": c["vtype"], "column_type": crec.get("column_type"),
            "value_text": v.text.strip() if v is not None else None, "value_min": v.vmin if v is not None else None,
            "value_max": v.vmax if v is not None else None, "value_pm": v.pm if v is not None else None,
            "qualifier": v.qualifier if v is not None else None, "n_numbers": c.get("n_numbers"),
            **_unit_fields(None), "unit_source": None, "multiplier_exp": None, "flags": list(c["flags"]),
            "review_status": REVIEW_STATUS, "rule_version": RULE_VERSION}
        if c["vtype"] == "UNIT":
            rec.update(_unit_fields(c["unit_only"]))
            rec["unit_source"] = "CELL"
        if v is not None and not head:
            unit, source, mult = None, None, None
            cu, rw = ci.get("unit"), info.get("unit")
            if v.unit is not None and not v.unit_inherited:
                unit, source = v.unit, "CELL"
            elif cu is not None and rw is not None:
                if rw.canon != cu.canon:              # the column and the row name different units: none is taken
                    rec["flags"].append("UNIT_CONFLICT")
                    source = "CONFLICT"
                else:
                    unit, source = cu, ci.get("source")
                    mult = crec.get("multiplier_exp")
            elif cu is not None:
                unit, source, mult = cu, ci.get("source"), crec.get("multiplier_exp")
            elif rw is not None:
                unit, source, mult = rw, info.get("unit_source"), info.get("multiplier_exp")
            elif (b_i, k) in cap_by_col and rrole in VALUE_ROWS | {"STAT_DISPERSION"}:
                unit, source = cap_by_col[(b_i, k)], "CAPTION"
            if mult is None and source is None:
                mult = crec.get("multiplier_exp") or info.get("multiplier_exp")
            rec.update(_unit_fields(unit))
            rec["unit_source"], rec["multiplier_exp"] = source, mult
        if not blk["aligned"] and not head:
            rec["flags"].append("BAND_COLUMNS_MISMATCH")
        if v is not None and rrole in VALUE_ROWS and "NO_HEADER" in (crec.get("flags") or ()):
            rec["flags"].append("NO_HEADER")
        out.append(rec)
    return out


def _flat(struct: dict[str, Any], cells: list[dict[str, Any]], n_rows: int, n_cols: int,
          flags: list[str]) -> dict[str, Any]:
    """A table too large to analyse: its cells with text and value only, no roles."""
    base = {k: struct[k] for k in ("table_id", "nav_table_id", "source_id", "page_id")}
    out = []
    for c in cells:
        v = c["val"]
        out.append({
            "cell_id": nav_ids.table_cell_id(struct["nav_table_id"], c["row"], c["col"]), **base, "row": c["row"],
            "col": c["col"], "row_span": c["row_span"], "col_span": c["col_span"], "band": 0, "block": 0,
            "text": c.get("text"), "text_clean": c["clean"] or None, "is_header": False, "row_role": "UNKNOWN",
            "column_role": None, "header_path": [], "header_text": None, "row_label": None,
            "row_property_key": None, "value_type": c["vtype"], "column_type": None,
            "value_text": v.text.strip() if v is not None else None, "value_min": v.vmin if v is not None else None,
            "value_max": v.vmax if v is not None else None, "value_pm": v.pm if v is not None else None,
            "qualifier": v.qualifier if v is not None else None, "n_numbers": c.get("n_numbers"),
            **_unit_fields(v.unit if v is not None else None),
            "unit_source": "CELL" if v is not None and v.unit is not None else None, "multiplier_exp": None,
            "flags": sorted(set(c["flags"])), "review_status": REVIEW_STATUS, "rule_version": RULE_VERSION})
    struct.update({
        "n_rows": n_rows, "n_cols": n_cols, "n_cells": len(cells),
        "n_filled_cells": sum(1 for c in cells if c["vtype"] != "EMPTY"),
        "n_numeric_cells": sum(1 for c in cells if c["vtype"] in NUMERIC),
        "n_text_cells": sum(1 for c in cells if c["vtype"] == "TEXT"), "n_flagged_cells": 0,
        "n_spanning_cells": sum(1 for c in cells if c["row_span"] > 1 or c["col_span"] > 1), "n_header_rows": 0,
        "header_method": "NONE", "n_bands": 1, "n_blocks": 1, "blocks": [], "orientation": "UNKNOWN",
        "confidence": 0.3, "structure_ok": False, "covers_region": False, "quality_flags": sorted(set(flags)),
        "property_keys": [], "materials": [], "caption_property_key": None})
    return {"structure": struct, "cells": out, "columns": []}


# ================================================================================================= parameters input
def param_blocks(cells: list[dict[str, Any]], structure: dict[str, Any]
                 ) -> list[tuple[list[dict[str, Any]], int, int, int, list[int], dict[tuple[int, int], list[str]]]]:
    """Sub-grids of one structured table for the parameter part — per block, the rows that hold values or name them
    (header, units, group, text, data, mean / min / max) renumbered from 0 with the cleaned texts. Dispersion and
    count rows, repeated headers, column numbers, notes, empty rows, an axis and the header row naming it (the
    parameter part's own rule: the numbers under an axis are of another quantity) are left out; a block without an
    aligned header gets no header rows. Per block: ``(cells, n_rows, n_cols, header_rows, row_map, flags)`` —
    ``row_map[i]`` is the canonical row of sub-row ``i``, ``flags[(row, col)]`` the flags of a canonical cell."""
    keep = {"HEADER", "TITLE", "UNITS", "GROUP", "TEXT", *VALUE_ROWS}
    by_block: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for c in cells:
        by_block[int(c.get("block") or 0)].append(c)
    aligned = {int(b["block"]): bool(b["aligned"]) for b in structure.get("blocks") or []}
    n_cols = int(structure.get("n_cols") or 0)
    out = []
    for b in sorted(by_block):
        cs = by_block[b]
        roles: dict[int, str] = {}
        for c in cs:
            roles.setdefault(int(c["row"]), c.get("row_role") or "TEXT")
        rows = sorted(r for r, x in roles.items() if x in keep)
        if not aligned.get(b, True):
            rows = [r for r in rows if roles[r] not in HEADER_ZONE]
        if not any(roles[r] in VALUE_ROWS for r in rows):
            continue
        new = {r: i for i, r in enumerate(rows)}
        sub, flags = [], {}
        for c in cs:
            r0, rs = int(c["row"]), int(c.get("row_span") or 1)
            kept = [r for r in range(r0, r0 + rs) if r in new]
            if not kept:
                continue
            sub.append({"row": new[kept[0]], "col": int(c["col"]), "row_span": len(kept),
                        "col_span": int(c.get("col_span") or 1), "is_header": False,
                        "text": c.get("text_clean") or ""})
            flags[(r0, int(c["col"]))] = list(c.get("flags") or [])
        head = 0 if not aligned.get(b, True) else next(
            (i for i, r in enumerate(rows) if roles[r] in VALUE_ROWS), len(rows))
        out.append((sub, len(rows), n_cols, head, rows, flags))
    return out


# ================================================================================================= build
def _source_symbols(con: Any, formula_symbols: Any, only: list[str] | None) -> dict[str, dict[str, str]]:
    """Symbols each source defines in the where-clauses of its formulas (N2), read as the parameter part reads them."""
    fsyms = P._rows(formula_symbols) if formula_symbols is not None else (
        P._from_con(con, "formula_symbols", "formula_id, source_id, symbol, symbol_key, definition, unit") or [])
    defs: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    for s in fsyms:
        if s.get("definition") and s.get("source_id") and (not only or s["source_id"] in only):
            dtext = s["definition"]
            ms = [m for m in P.find_mentions(dtext, P.lower_same_length(dtext)) if m.start <= 12]
            prop_key = ms[0].prop.key if len({m.prop.key for m in ms}) == 1 else P._OTHER_MEANING
            defs[s["source_id"]][P._hint_key(s["symbol"])][prop_key] += 1
    return P.source_symbol_table(defs)


def build(con: Any, *, section_pages: Any = None, sections: Any = None, formula_symbols: Any = None,
          stats: dict[str, Any] | None = None, source_ids: Iterable[str] | None = None,
          **options: Any) -> dict[str, Any] | None:
    """Structured-table datasets of the snapshot behind ``con`` (DuckDB with the ``canonical`` schema)."""
    import pyarrow as pa

    if not P._has(con, "SELECT 1 FROM canonical.tables LIMIT 0"):
        return None
    opts = {**DEFAULTS, **{k: v for k, v in options.items() if k in DEFAULTS}}
    only = sorted(set(source_ids)) if source_ids else None
    counters: Counter = Counter()
    where, params = "", []
    if only:
        where = f"WHERE t.source_id IN ({', '.join('?' for _ in only)})"
        params = list(only)
    cur = con.execute(f"""
        SELECT t.object_id, t.source_id, t.page_id, coalesce(p.page_index, 0) AS page_index, t.table_label, t.caption,
               t.n_rows, t.n_cols, t.header_rows, t.cells, t.raw_output, t.raw_format, t.recognition_method,
               t.quality_flags, t.origin, t.bbox_x0, t.bbox_y0, t.bbox_x1, t.bbox_y1, t.bbox_space
        FROM canonical.tables t LEFT JOIN canonical.pages p ON p.page_id = t.page_id {where}
        ORDER BY t.source_id, page_index, t.object_id""", params)
    names = [d[0] for d in cur.description]
    trows = [dict(zip(names, r)) for r in cur.fetchall()]
    langs = P._source_langs(con)
    symbols = _source_symbols(con, formula_symbols, only)
    is_word = _dictionary() if opts["respace"] == "auto" else None
    sp = P._rows(section_pages) if section_pages is not None else (
        P._from_con(con, "section_pages", "section_id, page_id") or [])
    se = P._rows(sections) if sections is not None else (
        P._from_con(con, "sections", "section_id, heading_block_id, page_start_index") or [])
    heading_pos = {r[0]: (int(r[1]), r[2]) for r in P._by_ids(con, """
        SELECT object_id, coalesce(reading_order, 0), bbox_y0 FROM canonical.blocks WHERE object_id IN ({ph})""",
        [s.get("heading_block_id") for s in se])} if se else {}
    secs = P._SectionIndex(sp, se, heading_pos)
    s_rows: list[dict[str, Any]] = []
    c_rows: list[dict[str, Any]] = []
    k_rows: list[dict[str, Any]] = []
    for t in trows:
        counters["tables"] += 1
        try:
            got = structure_table(t, lang=langs.get(t["source_id"]), source_symbols=symbols.get(t["source_id"]),
                                  is_word=is_word, max_header=int(opts["max_header_rows"]),
                                  max_grid=int(opts["max_grid"]))
        except Exception as exc:  # noqa: BLE001 - one malformed table must not stop the build
            counters["tables_failed"] += 1
            counters[f"failed:{type(exc).__name__}"] += 1
            continue
        st = got["structure"]
        st["page_index"] = int(t["page_index"])
        st["section_id"] = secs.of(t["page_id"], None, t.get("bbox_y0")) if secs.enabled else None
        s_rows.append(st)
        c_rows += got["cells"]
        k_rows += got["columns"]
    for st in s_rows:
        counters["structure_ok"] += bool(st["structure_ok"])
        counters["covers_region"] += bool(st["covers_region"])
        counters[f"orientation:{st['orientation']}"] += 1
        counters[f"header_method:{st['header_method']}"] += 1
        for f in st["quality_flags"]:
            counters[f"flag:{f}"] += 1
    for c in c_rows:
        counters[f"role:{c['row_role']}"] += 1
        counters[f"value_type:{c['value_type']}"] += 1
        for f in c["flags"]:
            counters[f"cell_flag:{f}"] += 1
    counters["cells"], counters["columns"] = len(c_rows), len(k_rows)
    if stats is not None:
        stats.update({"counters": dict(sorted(counters.items())), "rule_version": RULE_VERSION,
                      "options": {k: opts[k] for k in sorted(opts)},
                      "respace": "pymorphy3" if is_word is not None else "off"})
    return {"table_structure": pa.Table.from_pylist([{k: r.get(k) for k in STRUCTURE_COLUMNS} for r in s_rows],
                                                    schema=TABLE_STRUCTURE_SCHEMA()),
            "table_cells": pa.Table.from_pylist([{k: r.get(k) for k in CELL_COLUMNS} for r in c_rows],
                                                schema=TABLE_CELLS_SCHEMA()),
            "table_columns": pa.Table.from_pylist([{k: r.get(k) for k in COLUMN_COLUMNS} for r in k_rows],
                                                  schema=TABLE_COLUMNS_SCHEMA())}


# ================================================================================================= schemas
STRUCTURE_COLUMNS = (
    "table_id", "nav_table_id", "source_id", "page_id", "page_index", "section_id", "table_label", "table_number",
    "caption", "n_rows", "n_cols", "n_cells", "n_filled_cells", "n_numeric_cells", "n_text_cells", "n_flagged_cells",
    "n_spanning_cells", "n_header_rows", "header_method", "n_bands", "n_blocks", "blocks", "orientation",
    "recognition_method", "raw_format", "origin", "parse_method", "confidence", "structure_ok", "covers_region",
    "quality_flags", "property_keys", "materials", "caption_property_key", "bbox_x0", "bbox_y0", "bbox_x1",
    "bbox_y1", "bbox_space", "review_status", "rule_version")
CELL_COLUMNS = (
    "cell_id", "table_id", "nav_table_id", "source_id", "page_id", "row", "col", "row_span", "col_span", "band",
    "block", "text", "text_clean", "is_header", "row_role", "column_role", "header_path", "header_text", "row_label",
    "row_property_key", "value_type", "column_type", "value_text", "value_min", "value_max", "value_pm", "qualifier",
    "n_numbers", "unit_raw", "unit_canonical", "unit_dim", "unit_source", "multiplier_exp", "flags", "review_status",
    "rule_version")
COLUMN_COLUMNS = (
    "table_id", "nav_table_id", "block", "col", "header_path", "header_text", "symbol", "unit_raw", "unit_canonical",
    "unit_dim", "unit_source", "multiplier_exp", "stat", "column_type", "role", "col_group", "n_filled", "n_numeric",
    "property_key", "property_label", "property_basis", "property_ambiguous", "materials", "flags", "review_status",
    "rule_version")


def TABLE_STRUCTURE_SCHEMA():  # noqa: N802 - schema constants built lazily (pyarrow is optional at import)
    import pyarrow as pa

    i32, i16, f, lst, b = pa.int32(), pa.int16(), pa.float64(), pa.list_(pa.string()), pa.bool_()
    block = pa.struct([("block", i16), ("row_start", i32), ("row_end", i32), ("n_header_rows", i16),
                       ("aligned", b), ("bands", pa.list_(i16))])
    types = {"page_index": i32, "n_rows": i32, "n_cols": i32, "n_cells": i32, "n_filled_cells": i32,
             "n_numeric_cells": i32, "n_text_cells": i32, "n_flagged_cells": i32, "n_spanning_cells": i32,
             "n_header_rows": i16, "n_bands": i16, "n_blocks": i16, "blocks": pa.list_(block), "confidence": f,
             "structure_ok": b, "covers_region": b, "quality_flags": lst, "property_keys": lst, "materials": lst,
             "bbox_x0": f, "bbox_y0": f, "bbox_x1": f, "bbox_y1": f}
    return pa.schema([(c, types.get(c, pa.string())) for c in STRUCTURE_COLUMNS])


def TABLE_CELLS_SCHEMA():  # noqa: N802
    import pyarrow as pa

    i32, i16, f, lst = pa.int32(), pa.int16(), pa.float64(), pa.list_(pa.string())
    types = {"row": i32, "col": i32, "row_span": i16, "col_span": i16, "band": i16, "block": i16,
             "is_header": pa.bool_(), "header_path": lst, "value_min": f, "value_max": f, "value_pm": f,
             "n_numbers": i16, "multiplier_exp": i16, "flags": lst}
    return pa.schema([(c, types.get(c, pa.string())) for c in CELL_COLUMNS])


def TABLE_COLUMNS_SCHEMA():  # noqa: N802
    import pyarrow as pa

    i32, i16, lst = pa.int32(), pa.int16(), pa.list_(pa.string())
    types = {"block": i16, "col": i32, "header_path": lst, "multiplier_exp": i16, "col_group": i16,
             "n_filled": i32, "n_numeric": i32, "property_ambiguous": pa.bool_(), "materials": lst, "flags": lst}
    return pa.schema([(c, types.get(c, pa.string())) for c in COLUMN_COLUMNS])


def summarize(tables: dict[str, Any]) -> dict[str, Any]:
    """Counts for the public receipt (ids and numbers only, no corpus text)."""
    st = tables["table_structure"].to_pylist()
    cells = tables["table_cells"]
    cols = tables["table_columns"].to_pylist()

    def by(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
        return dict(sorted(Counter(str(r[key]) for r in rows).items(), key=lambda kv: (-kv[1], kv[0])))

    def ordered(c: Counter) -> dict[str, int]:
        return dict(sorted(c.items(), key=lambda kv: (-kv[1], kv[0])))

    return {
        "tables": len(st), "sources": len({r["source_id"] for r in st}), "cells": cells.num_rows,
        "columns": len(cols), "structure_ok": sum(1 for r in st if r["structure_ok"]),
        "covers_region": sum(1 for r in st if r["covers_region"]),
        "with_header": sum(1 for r in st if r["n_header_rows"]), "multi_block": sum(1 for r in st if r["n_blocks"] > 1),
        "with_number": sum(1 for r in st if r["table_number"]), "by_orientation": by(st, "orientation"),
        "by_header_method": by(st, "header_method"), "by_parse_method": by(st, "parse_method"),
        "table_flags": ordered(Counter(f for r in st for f in r["quality_flags"])),
        "row_roles": ordered(Counter(cells.column("row_role").to_pylist())),
        "value_types": ordered(Counter(cells.column("value_type").to_pylist())),
        "cell_flags": ordered(Counter(f for fl in cells.column("flags").to_pylist() for f in fl)),
        "columns_with_property": sum(1 for c in cols if c["property_key"]),
        "columns_by_role": by(cols, "role"), "columns_by_unit_source": by(cols, "unit_source"),
    }
