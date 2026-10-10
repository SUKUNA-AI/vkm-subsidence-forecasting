"""Parameter-value candidates of the navigation layer (NAV §8) — material and mining parameters with provenance, no LLM.

``build(con, *, section_pages=None, sections=None, formula_parameters=None, formula_symbols=None, **options)``
returns two Arrow tables:

* ``parameter_candidates`` — one row per printed value of a property of the vocabulary
  (:mod:`vkm_corpus.navigation.parameters_vocab`): property, symbol, material, the value as printed and parsed
  (ranges «10–15», «от … до …», «±», powers «2·10⁻⁵», decimal comma), the unit as printed and in SI, a qualifier,
  a scale hint (LAB / MASSIF / NORMATIVE / MODEL / UNKNOWN from context words), a site hint (ВКМ, СКРУ-1…, БКПРУ-…,
  other deposits as ``ANALOGUE:…``, UNKNOWN), the method (``TABLE`` — a cell of ``canonical.tables``, read through
  the structured grid of the part ``tables`` when the build has it (rule ``parameters_v2``); ``TEXT`` — a text block
  outside such a grid; ``NEAR_FORMULA`` — N2's value next to a formula whose where-clause defines the symbol) and
  the locator
  (source, page, N1 section, block or table cell, character span of the value in
  ``canonical.blocks.normalized_text``);
* ``parameter_summary`` — per (property, material, scale hint, SI unit): counts of candidates, sources and pages and
  the SI range — a navigation aid, never a recommended value.

Rules. A value belongs to the nearest preceding property phrase of its sentence whose unit dimension it matches; a
dimensional property needs a printed unit (or a unit printed with the phrase, «Сцепление, МПа»). A value right after
another quantity («при давлении 5 МПа»), a relative change («снижается на 20 %», «в 2 раза») or a reference
(«рис. 3», «[12]») is not a candidate. Material, unit, scale and site that cannot be read from the context stay
``UNKNOWN`` (``NULL`` for the unit) — nothing is guessed; a power printed in a table header («E·10⁻³, МПа»,
«Ey·10⁴, МПа» — authors use both directions) keeps the printed number and leaves the SI value empty. Everything is
DERIVED navigation (``AUTO_EXTRACTED_UNREVIEWED``): promotion to evidence happens later under the evidence rules.
"""
from __future__ import annotations

import bisect
import math
import multiprocessing as mp
import os
import re
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Iterable

from vkm_corpus.navigation import ids as nav_ids
from vkm_corpus.navigation import parameters_vocab as V
from vkm_corpus.navigation.formulas import latex_to_plain, plain_symbol, symbol_key

RULE_VERSION = nav_ids.RULE_VERSIONS["parameters"]
REVIEW_STATUS = "AUTO_EXTRACTED_UNREVIEWED"
METHODS = ("TABLE", "TEXT", "NEAR_FORMULA")
TEXT_BLOCK_TYPES = ("TEXT", "LIST_ITEM", "ABSTRACT", "CAPTION", "FOOTNOTE")
SUMMARY_NOTE = "navigation aid: counts and SI range of auto-extracted candidates, never a recommended value"
DEFAULTS: dict[str, Any] = {
    "workers": None,          # processes of the text pass (None: min(cpu, 16); 1: in-process)
    "max_gap": 150,           # characters between a property phrase and its value (same sentence)
    "batch": 3000,            # blocks per worker task
}

# ================================================================================================= text with offsets
_MATH_SPAN = re.compile(r"\$\$(.+?)\$\$|\$(.+?)\$|\\\((.+?)\\\)", re.S)
_SUPER = str.maketrans({"⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4", "⁵": "5", "⁶": "6", "⁷": "7", "⁸": "8",
                        "⁹": "9", "⁻": "-", "⁺": "+"})
_SUPER_CHARS = frozenset("⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺")


def _fold_char(ch: str) -> str:
    """Mathematical alphanumerics (𝐸, 𝜈) and compatibility Greek to plain letters; one char in, one char out."""
    o = ord(ch)
    if 0x1D400 <= o <= 0x1D7FF or ch in "µϵϑϰϕϱϖ":
        n = unicodedata.normalize("NFKC", ch)
        return n if len(n) == 1 else ch
    return ch


def plain_with_map(text: str | None) -> tuple[str, list[int]]:
    """Readable plain text of a block (LaTeX spans rendered, superscripts as ``^``) and, for every output character,
    the index of the input character it comes from (locators refer to the input text)."""
    s = text or ""
    out: list[str] = []
    pos: list[int] = []
    i = 0
    for m in _MATH_SPAN.finditer(s):
        _copy_plain(s, i, m.start(), out, pos)
        body = m.group(1) or m.group(2) or m.group(3) or ""
        rendered = " " + latex_to_plain(body) + " "
        for k, ch in enumerate(rendered):
            out.append(ch)
            pos.append(m.start() if k < len(rendered) - 1 else m.end() - 1)
        i = m.end()
    _copy_plain(s, i, len(s), out, pos)
    return "".join(out), pos


def _copy_plain(s: str, a: int, b: int, out: list[str], pos: list[int]) -> None:
    for j in range(a, b):
        ch = s[j]
        if ch in _SUPER_CHARS:
            if not out or (out[-1] != "^" and not (pos and s[pos[-1]] in _SUPER_CHARS)):
                out.append("^")
                pos.append(j)
            out.append(ch.translate(_SUPER))
        elif "ﬀ" <= ch <= "ﬆ":               # a typographic ligature («sandﬁll»): its letters, one source char
            for x in unicodedata.normalize("NFKC", ch):
                out.append(x)
                pos.append(j)
            continue
        else:
            out.append(_fold_char(ch))
        pos.append(j)


_LAT2CYR_LOWER = str.maketrans("acekmopxytdlnuhi", "асекморхутдлпини")
_MIXED_WORD = re.compile(r"[a-zа-яё]*(?:[a-z][а-яё]|[а-яё][a-z])[a-zа-яё]*")


def lower_same_length(s: str) -> str:
    """Lower case with «ё» → «е», keeping the length (offsets of the plain text stay valid). OCR repairs of the same
    length: Latin letters inside a Cyrillic word («прочнocти», «Пределdlительной») become Cyrillic, «козфф» → «коэфф»,
    Ukrainian «і» of the OCR («Сільвинит») is «и»."""
    low = "".join(c.lower() if len(c.lower()) == 1 else c for c in s).replace("ё", "е").replace("і", "и")
    low = low.replace("ї", "и")
    if re.search(r"[a-z]", low) and re.search(r"[а-я]", low):
        low = _MIXED_WORD.sub(lambda m: m.group(0).translate(_LAT2CYR_LOWER)
                              if len(re.findall(r"[а-я]", m.group(0))) >= 2 else m.group(0), low)
    return low.replace("козфф", "коэфф")


# ================================================================================================= sentences
_SENT_BREAK = re.compile(r"[.!?](?=\s+[«\"(]?[А-ЯЁA-Z])")
_INITIAL_BEFORE = re.compile(r"(?<![\w])[А-ЯЁA-Z]$")                  # «А. Б. Иванов»
_ABBR_BEFORE = re.compile(r"(?<![\w])(?:рис|табл|стр|гл|разд|др|пр|им|проф|акад|гг?|ок|пп?|ср|напр|вып|т|е|д|см|"
                          r"fig|figs|eq|eqs|al|vol|no|pp?|ed|eds|cf|approx|ca|resp)$", re.I)


def sentence_starts(plain: str) -> list[int]:
    """Start offsets of the sentences (a break is «.!?» + space + capital, not after an abbreviation or initial)."""
    starts = [0]
    for m in _SENT_BREAK.finditer(plain):
        before = plain[max(0, m.start() - 8):m.start()]
        if _INITIAL_BEFORE.search(before) or (_ABBR_BEFORE.search(before) and
                                              not re.search(r"\d\s*(?:см|гг?|т)$", before)):
            continue                            # «5 см.», «1985 г.» end a sentence; «см. рис.», «г. Березники» do not
        starts.append(m.end())
    return starts


def sentence_of(starts: list[int], pos: int) -> int:
    return bisect.bisect_right(starts, pos) - 1


# ================================================================================================= units
@dataclass
class Unit:
    raw: str
    dim: str
    factor: float
    canon: str
    start: int
    end: int
    complete: bool = True     # nothing continues it («МПа» of «МПа⁻ⁿ·с⁻¹» is not complete)


_UNIT_RX: list[tuple[re.Pattern, str, float, str]] = [(re.compile(p, re.I), d, f, c) for p, d, f, c in V.UNITS]
_NEG3 = r"\s?(?:-3|−3|–3|⁻³|\^-3)"
_NEG1 = r"\s?(?:-1|−1|–1|⁻¹|\^-1)"
_EXTRA_UNITS: list[tuple[re.Pattern, str, float, str]] = [
    # spellings with a negative power: «kg m-3», «g cm−3», «kN m-3», «mm yr-1»
    (re.compile(r"(?:кг|kg)\s?(?:м|m)" + _NEG3, re.I), V.DENSITY, 1.0, "кг/м³"),
    (re.compile(r"(?:г|g)\s?(?:см|cm)" + _NEG3, re.I), V.DENSITY, 1000.0, "г/см³"),
    (re.compile(r"(?:кн|kn)\s?(?:м|m)" + _NEG3, re.I), V.WEIGHT, 1e3, "кН/м³"),
    (re.compile(r"(?:мм|mm)\s?(?:год|yr|a)" + _NEG1, re.I), V.VELOCITY, 1e-3 / (365.25 * 86400), "мм/год"),
    (re.compile(r"(?:см|cm)\s?(?:год|yr|a)" + _NEG1, re.I), V.VELOCITY, 1e-2 / (365.25 * 86400), "см/год"),
]
_ALL_UNITS = _EXTRA_UNITS + _UNIT_RX
_BARE_TIME_OK = frozenset({"сут", "год", "мес", "ч", "мин"})      # a bare «с»/«s»/«a»/«d» is never a time unit
_UNIT_CONT = re.compile(r"\s?(?:[⁻⁺^·⋅∙*×/]|[-−–]\s?[\dn])")


def match_unit(plain: str, pos: int) -> Unit | None:
    """The longest known unit starting at ``pos`` (after optional spaces); the next char must not be a letter."""
    p = pos
    while p < len(plain) and plain[p] in "   ":
        p += 1
    if p >= len(plain):
        return None
    best: Unit | None = None
    for rx, dim, f, canon in _ALL_UNITS:
        m = rx.match(plain, p)
        if not m or m.end() == p:
            continue
        end = m.end()
        if end < len(plain) and plain[end].isalpha() and plain[end - 1] != ".":
            continue
        if dim == V.TIME and canon not in _BARE_TIME_OK:
            continue
        if best is None or end - p > best.end - best.start:
            best = Unit(plain[p:end], dim, f, canon, p, end)
    if best is not None and _UNIT_CONT.match(plain, best.end):
        best.complete = False
    return best


# ================================================================================================= numbers
_NUM_CORE = r"(?:\d{1,3}(?:[   ]\d{3})+(?!\d)|\d+)(?:[.,]\d+)?"
_EXP = (r"(?:\s*[·⋅∙×xхЧ*´]\s*10\s*(?:\^\s*\(?\s*(?P<e1>[-−–]?\s*\d{1,3})(?:\s*\))?|(?P<e2>[-−–]\s?\d{1,2})(?![\d.,])"
        r"|(?P<e3>\d{1,2})(?![\d.,]))|(?<=\d)[eEеЕ](?P<e4>[-+−]?\d{1,3})(?!\d))")
_NUMBER = re.compile(r"(?P<sign>[-−–](?=\d))?(?P<core>" + _NUM_CORE + r")(?:" + _EXP + r")?")
_POWER_ONLY = re.compile(r"10\s*\^\s*\(?\s*(?P<e>[-−–]?\d{1,3})(?:\s*\))?")
_NUM_START = re.compile(r"(?<![\w.,/^_\\])(?<![^\W\d_][-–−])(?:[-−–](?=\d)|\d)")   # not «СКРУ-1», «α−1»
_RANGE_SEP = re.compile(r"\s*(?:–|—|-|−|÷|\.{2,3}|…)\s*|~\s?(?=\d)")          # «126~960 m»: «~» right after a number
# «от 10 до 15», «с глубины 15,3 м до 121,0 м» (a noun of the position between «с» and the number)
_FROM = re.compile(r"(?:(?<![\w-])(?:от|from|between|с)(?:\s+(?:глубин\w*|отметк\w*|высот\w*|уровн\w*))?)\s+$", re.I)
_TO = re.compile(r"\s+(?:до|to|and|по)\s+", re.I)
_EN_TO = re.compile(r"\s+to\s+", re.I)
_PM = re.compile(r"\s*(?:±|\+/-|\+-)\s*")
_LIST_GAP = re.compile(r"\s*(?:,|;|и|and|or|или)\s*", re.I)
_TUPLE_AFTER = re.compile(r"\s*[×xх*]\s*\d")
_ANGLE_MIN = re.compile(r"\s*(\d{1,2}(?:[.,]\d+)?)\s*[′'’¢`]")
_ANGLE_SEC = re.compile(r"\s*(\d{1,2}(?:[.,]\d+)?)\s*(?:″|\"|''|′′)")
_YEAR_AFTER = re.compile(r"\s*(?:гг?\.|год\w*|г\b)", re.I)
_SHARED_POWER = re.compile(r"\s*[·⋅∙×xхЧ*]\s*10\s*\^\s*\(?\s*([-−–]?\d{1,3})(?:\s*\))?")


def _to_int(s: str) -> int:
    return int(re.sub(r"\s", "", s).replace("−", "-").replace("–", "-"))


@dataclass
class Num:
    value: float
    end: int
    exp: int | None


def number_at(plain: str, pos: int, lang: str | None = None) -> Num | None:
    """The number printed at ``pos``: decimal comma, thousands spaces, ``2·10^-5`` / ``2·10-5`` / ``1,46Ч104`` /
    ``1.5e-3`` / ``10^-5``."""
    m2 = _POWER_ONLY.match(plain, pos)
    if m2:
        try:
            e = _to_int(m2.group("e"))
            return Num(10.0 ** e, m2.end(), e)
        except (OverflowError, ValueError):
            return None
    m = _NUMBER.match(plain, pos)
    if not m:
        return None
    core = m.group("core")
    if lang == "en" and re.fullmatch(r"\d{1,3}(?:,\d{3})+", core):
        core = core.replace(",", "")
    core = re.sub(r"[   ]", "", core).replace(",", ".")
    try:
        v = float(core)
    except ValueError:
        return None
    exp = None
    for g in ("e1", "e2", "e3", "e4"):
        if m.group(g):
            exp = _to_int(m.group(g))
            break
    if m.group("e3") and exp == 0:
        return None
    if exp is not None:
        try:
            v *= 10.0 ** exp
        except OverflowError:
            return None
    if m.group("sign"):
        v = -v
    return Num(v, m.end(), exp) if math.isfinite(v) else None


@dataclass
class Val:
    start: int                # span of the number(s) in the plain text
    end: int
    text: str
    vmin: float
    vmax: float
    qualifier: str            # '=' | 'range' | '±'
    pm: float | None = None
    unit: Unit | None = None
    unit_inherited: bool = False
    list_id: int = -1
    reject: str | None = None
    flags: list[str] = field(default_factory=list)

    @property
    def span_end(self) -> int:
        return self.unit.end if self.unit is not None and not self.unit_inherited else self.end


def _one_value(plain: str, s: int, lang: str | None) -> Val | None:
    n1 = number_at(plain, s, lang)
    if n1 is None:
        return None
    val = Val(s, n1.end, plain[s:n1.end], n1.value, n1.value, "=")
    e1 = n1.end
    pre = plain[max(0, s - 12):s]
    # «10 МПа – 15 МПа», «от 151,5 м до 398,2 м»: the unit printed after both numbers
    sep_at = e1
    u_mid = match_unit(plain, e1)
    if u_mid is not None and u_mid.dim != V.ANGLE:
        rm2 = _RANGE_SEP.match(plain, u_mid.end) or (_TO.match(plain, u_mid.end) if _FROM.search(pre) else None)
        if rm2:
            n2 = number_at(plain, rm2.end(), lang)
            u2 = match_unit(plain, n2.end) if n2 is not None else None
            if u2 is not None and u2.canon == u_mid.canon:
                sep_at = u_mid.end
    tm = _TO.match(plain, sep_at) if _FROM.search(pre) else None
    if tm is None:                  # «25 to 30 MPa», «2500 m to 3000 m»: an English range without «from»
        t2 = _EN_TO.match(plain, e1 if u_mid is None else u_mid.end)
        n2 = number_at(plain, t2.end(), lang) if t2 else None
        u2 = match_unit(plain, n2.end) if n2 is not None else None
        if u2 is not None and (u_mid is None or u2.canon == u_mid.canon):
            tm = t2
    rm = _RANGE_SEP.match(plain, sep_at) if tm is None else None
    pm = _PM.match(plain, e1)
    if tm is not None or rm is not None:
        n2 = number_at(plain, (tm or rm).end(), lang)
        if n2 is not None and not _TUPLE_AFTER.match(plain, n2.end):
            lo = n1.value
            if n2.exp is not None and n1.exp is None and abs(n2.exp) >= 2:      # «1–5·10⁻⁵» = (1–5)·10⁻⁵
                lo = n1.value * 10.0 ** n2.exp
                val.flags.append("SHARED_POWER")
            val = Val(s, n2.end, plain[s:n2.end], min(lo, n2.value), max(lo, n2.value), "range", flags=val.flags)
            if tm is not None and pre.rstrip().lower().endswith(("с", "from")):
                val.flags.append("FROM_TO")
    elif pm is not None:
        n2 = number_at(plain, pm.end(), lang)
        if n2 is not None:
            val = Val(s, n2.end, plain[s:n2.end], n1.value, n1.value, "±", pm=abs(n2.value))
    # «(1–5)·10⁻⁵»
    if val.qualifier == "range" and s > 0 and plain[s - 1] == "(":
        close = plain.find(")", val.end)
        if close >= 0 and plain[val.end:close].strip() == "":
            pw = _SHARED_POWER.match(plain, close + 1)
            if pw:
                f = 10.0 ** _to_int(pw.group(1))
                val.vmin, val.vmax = val.vmin * f, val.vmax * f
                val.start, val.end = s - 1, pw.end()
                val.text = plain[val.start:val.end]
                val.flags.append("SHARED_POWER")
    return val


def scan_values(plain: str, lang: str | None = None) -> list[Val]:
    """All numeric value expressions of a text with their units; members of a list («20, 15 и 10 ГПа») take the unit
    printed after the last member. Rejected parses (tuples, dates, identifiers, years, «в 2 раза») carry ``reject``."""
    vals: list[Val] = []
    i, n = 0, len(plain)
    while i < n:
        m = _NUM_START.search(plain, i)
        if not m:
            break
        s = m.start()
        if plain[s] in "-−–" and s > 0 and (plain[s - 1].isdigit() or plain[s - 1].isalpha()):
            i = s + 1
            continue
        val = _one_value(plain, s, lang)
        if val is None:
            i = s + 1
            continue
        if _TUPLE_AFTER.match(plain, val.end) or (s > 0 and plain[s - 1] in "×xх*" and val.qualifier == "="):
            val.reject = "tuple"
        elif re.match(r"[.,:]\d", plain[val.end:val.end + 2]):
            val.reject = "date_or_version"
        unit = match_unit(plain, val.end)
        if unit is None and re.match(r"\s*/\s*[^\W\d_]", plain[val.end:val.end + 4]):
            val.reject = val.reject or "fraction"            # «1/φ», «1/сут» standing alone: a formula or a unit
        if re.search(r"[√∛∜]\s*$", plain[max(0, val.start - 3):val.start]):
            val.reject = val.reject or "formula"             # «K = √3»
        if unit is None and val.end < n and plain[val.end].isalpha():
            val.reject = val.reject or "identifier"
        if unit is not None and unit.dim == V.ANGLE and val.qualifier == "=":
            mm = _ANGLE_MIN.match(plain, unit.end)
            if mm:
                add, end = float(mm.group(1).replace(",", ".")) / 60.0, mm.end()
                ss = _ANGLE_SEC.match(plain, end)
                if ss:
                    add, end = add + float(ss.group(1).replace(",", ".")) / 3600.0, ss.end()
                val.vmin = val.vmax = val.vmin + add
                unit.end, unit.raw = end, plain[unit.start:end]
        val.unit = unit
        if unit is None and val.start > 0 and plain[val.start - 1] == "(" and plain[val.end:val.end + 1] == ")":
            val.reject = val.reject or "enumerator"          # «(1)», «(3.2)»: list items and equation numbers
        whole = [x for x in (val.vmin, val.vmax)]
        if all(float(x).is_integer() and 1800 <= x <= 2100 for x in whole) and (
                (unit is None and _YEAR_AFTER.match(plain, val.end)) or (unit is not None and unit.canon == "год")):
            val.reject = "year"
        if V.REJECT_AFTER.match(plain, val.span_end):
            val.reject = val.reject or "times"
        vals.append(val)
        i = max(val.span_end, s + 1)
    # lists: consecutive values separated only by «,», «;», «и», «or»
    lid, k = 0, 0
    while k < len(vals):
        j = k
        while j + 1 < len(vals) and vals[j].unit is None and vals[j].reject is None and \
                _LIST_GAP.fullmatch(plain[vals[j].end:vals[j + 1].start]):
            j += 1
        if j > k:
            last = vals[j]
            for x in vals[k:j + 1]:
                x.list_id = lid
            if last.unit is not None and last.reject is None:
                for x in vals[k:j]:
                    u = last.unit
                    x.unit = Unit(u.raw, u.dim, u.factor, u.canon, u.start, u.end, u.complete)
                    x.unit_inherited = True
            lid += 1
        k = j + 1
    return vals


def value_si(vmin: float, vmax: float, pm: float | None, unit: Unit | None
             ) -> tuple[float | None, float | None, float | None]:
    if unit is None:
        return None, None, None
    lo, hi = vmin * unit.factor, vmax * unit.factor
    return min(lo, hi), max(lo, hi), (pm * unit.factor if pm is not None else None)


# ================================================================================================= mentions
@dataclass
class Mention:
    prop: V.PropertyDef
    start: int
    end: int
    unit: Unit | None = None          # «Сцепление, МПа»: the unit printed with the phrase
    symbol: str | None = None


_PROP_RX: dict[str, re.Pattern] = {
    p.key: re.compile(r"(?<![\w-])(?:" + "|".join(p.patterns) + r")", re.I) for p in V.PROPERTIES}
_EXCLUDE_BEFORE: dict[str, re.Pattern] = {
    "cohesion": re.compile(r"(?:муфт\w*|сил\w*|надежн\w*|полн\w*|жестк\w*|услови\w*)\s+$", re.I),
    "permeability": re.compile(r"(?:диэлектрическ\w*|магнитн\w*|относительн\w*|dielectric|magnetic|relative)\s+$",
                               re.I),
    "density": re.compile(r"(?:probability|current|energy|spectral|charge|flux|power|point|scatterer|pixel|crack|"
                          r"fracture|excess|вероятност\w*|тока|энерги\w*|потока|отражател\w*|сети|трещин\w*|"
                          r"разност\w*|перепад\w*|контраст\w*|избыточн\w*)\s+$", re.I),
    "limit_strain": re.compile(r"(?:упруг|пластическ|остаточн|elastic|plastic)\w*\s+$", re.I),  # a part of it
    # «deceleration of subsidence by 3–5 cm»: a change of the movement, not a subsidence
    "subsidence": re.compile(r"(?:decelerat|accelerat|замедлени|ускорени)\w*\s+(?:of\s+)?$", re.I),
}
# «модуль деформации (секущий) на пределе прочности (Dпр, ГПа)»: a point of the curve, not the strength itself
_AT_STRENGTH_LIMIT = re.compile(r"(?<![\w-])(?:на|при|до)\s+$")
# «… на контуре до 0,2 % на глубине 1 м», «в кровле на глубине 2 м»: a distance into the rock, not a depth of mining
_INTO_ROCK_BEFORE = re.compile(r"(?:контур|стенк|обнажени|кровл|почв|целик|забо)\w*", re.I)
_EXCLUDE_AFTER: dict[str, re.Pattern] = {
    "friction_coefficient": re.compile(r"^\s*(?:гидравлическ|сопротивлени)", re.I),
    "density": re.compile(r"^\s*(?:вероятност|распределени|тока|потока|энерги|сети|отражател|точек|трещин|"
                          r"постоянн\w* отражател|of (?:probability|states|scatterers|points)|contrasts?\b|"
                          r"differences?\b|anomal|excess)", re.I),
    "depth": re.compile(r"^\s*(?:скважин|шпур|промерзани|проникновени|резкост|модуляци)", re.I),
    "depth_unspecified": re.compile(r"^\s*(?:скважин|шпур|промерзани|проникновени|резкост|модуляци|грунтов|"
                                    r"of (?:field|focus|penetration|investigation)|резания|рыхлени|трещин)", re.I),
    "thickness_unspecified": re.compile(r"^\s*(?:дозы|излучени|сигнал|двигател|привод|электр|насос|of the signal)",
                                        re.I),
    "subsidence": re.compile(r"^\s*(?:рельс|фундамент|сооружени|здани|опор)", re.I),
    # «степень нагружения образцов» is the load level of a laboratory test, not of a pillar
    "loading_degree": re.compile(r"^\s*(?:\([^)]{0,12}\)\s*)?(?:образц|проб|specimen|sample)", re.I),
}
_PHRASE_UNIT = re.compile(r"\s*(?:,|\(|в\s+(?=\S))\s*", re.I)
_PAREN_SYMBOL = re.compile(r"\s*\(\s*([^\s(),;]{1,8})\s*\)")
_PAREN_SYMBOL_UNIT = re.compile(r"\s*\(\s*([^\s(),;]{1,8})\s*,\s*([^()]{1,14}?)\s*\)")
_COMMA_SYMBOL = re.compile(r"\s*,?\s*([A-Za-zΑ-Ωα-ωА-ЯЁ][\w′^]{0,5})\s*,")
_NOT_SYMBOL = frozenset("рис табл стр др пр гл разд пп и на по мпа гпа кпа па мм см км".split())


def symbolish(tok: str | None, *, assigned: bool = False) -> str | None:
    """A printed symbol («E», «σсж», «Kдл») normalised, or None for an ordinary word or a unit. ``assigned``: the
    token stands before «=», so a unit spelling («m», «с») is a symbol there."""
    if not tok:
        return None
    t = tok.strip(" .,;:")
    if not t or len(t) > 8 or t.lower() in _NOT_SYMBOL or not t[0].isalpha():
        return None
    letters = [c for c in t if c.isalpha()]
    if len(letters) >= 4 and all("Ѐ" <= c <= "ӿ" for c in letters) and t[0].islower():
        return None
    if not assigned and match_unit(t, 0) is not None and match_unit(t, 0).end == len(t):
        return None
    return plain_symbol(t)


def find_mentions(plain: str, low: str) -> list[Mention]:
    """Property phrases of a text (the longest phrase wins where two overlap), with a unit printed right after the
    phrase («Модуль деформации, ГПа») and a symbol («(E)», «Е, ГПа»)."""
    found: list[Mention] = []
    for p in V.PROPERTIES:
        if not any(f in low for f in p.prefilter):
            continue
        for m in _PROP_RX[p.key].finditer(low):
            ex = _EXCLUDE_BEFORE.get(p.key)
            if ex is not None and ex.search(low[max(0, m.start() - 24):m.start()]):
                continue
            if p.key == "depth" and low.startswith("на глубин", m.start()) and _INTO_ROCK_BEFORE.search(
                    low[max(0, m.start() - 30):m.start()]):
                continue
            if p.key == "strength_unspecified" and low.startswith("предел", m.start()) and \
                    _AT_STRENGTH_LIMIT.search(low[max(0, m.start() - 6):m.start()]):
                continue
            ea = _EXCLUDE_AFTER.get(p.key)
            if ea is not None and ea.search(low[m.end():m.end() + 30]):
                continue
            found.append(Mention(p, m.start(), m.end()))
    kept: list[Mention] = []
    for x in sorted(found, key=lambda x: (-(x.end - x.start), x.start)):
        if all(x.end <= k.start or x.start >= k.end for k in kept):
            kept.append(x)
    kept.sort(key=lambda x: x.start)
    for x in kept:
        pos = x.end
        ps = _PAREN_SYMBOL.match(plain, pos)
        psu = _PAREN_SYMBOL_UNIT.match(plain, pos) if not ps else None
        if ps and symbolish(ps.group(1)):
            x.symbol = symbolish(ps.group(1))
            pos = ps.end()
        elif psu and symbolish(psu.group(1), assigned=True):      # «модуль спада (М, ГПа)»
            x.symbol = symbolish(psu.group(1), assigned=True)
            u = match_unit(plain, psu.start(2))
            if u is not None and u.end >= psu.end(2):
                x.unit = u
            continue
        cs = _COMMA_SYMBOL.match(plain, pos)
        if cs and symbolish(cs.group(1)):
            x.symbol = x.symbol or symbolish(cs.group(1))
            pos += cs.end() - 1
        pu = _PHRASE_UNIT.match(plain, pos)
        if pu:
            u = match_unit(plain, pu.end())
            if u is not None and re.match(r"\s*[),:;]|\s+[-−]?\d|\s*$|\s*[–—-]", plain[u.end:u.end + 4]):
                x.unit = u
    return kept


# ================================================================================================= context tags
@dataclass
class Tagged:
    label: str
    group: str
    start: int
    end: int
    raw: str


_MAT_RX: list[tuple[V.MaterialDef, re.Pattern, re.Pattern | None]] = [
    (md, re.compile(r"(?<![\w-])(?:" + "|".join(md.patterns) + r")", re.I),
     re.compile(r"(?<![\w-])(?:" + "|".join(md.abbr) + r")(?![\w-])") if md.abbr else None)
    for md in V.MATERIALS]
_MAT_PREFILTER = ("сол", "сильвин", "карнал", "галит", "ангидр", "доломит", "мерг", "глин", "аргил", "алевр", "песч",
                  "извест", "гипс", "толщ", "пачк", "отложен", "заклад", "отход", "шлам", "рассол", "уг", "руд",
                  "тверд", "salt",
                  "halit", "sylvin", "carnal", "anhydr", "dolom", "marl", "clay", "mudst", "siltst", "sandst",
                  "limest", "gyps", "fill", "brine", "coal", "ore", "overburden", "potash")
_ABBR_UPPER = re.compile(r"[А-ЯЁA-Z]{2,}")
_INCLUSION_BEFORE = re.compile(r"(?<![\w-])(?:с|со|with)\s+(?:(?:тонк|редк|част|многочисленн|отдельн|thin)\w*\s+)?"
                               r"(?P<w>(?:прослоя\w*|прослойк\w*|прослоями|включени\w*|примес\w*|ячейк\w*|гнезд\w*|"
                               r"вкраплени\w*|линз\w*|interlayers?|inclusions?|layers of)\s+)?$", re.I)
_INCLUSION_WORD = re.compile(r"прослой|прослоя|прослое|включени|примес|interlayer|inclusion", re.I)
_NEGATED_BEFORE = re.compile(r"(?<![\w-])(?:без|without|no)\s+$", re.I)          # «Без закладки»
_LAT2CYR_UPPER = str.maketrans("ABCEHKMOPTX", "АВСЕНКМОРТХ")      # OCR «CMT», «B3T» for «СМТ», «ВЗТ»


def _dedupe_tags(tags: list[Tagged]) -> list[Tagged]:
    tags.sort(key=lambda t: (t.start, -(t.end - t.start)))
    kept: list[Tagged] = []
    for t in tags:
        if kept and t.start < kept[-1].end:
            continue
        kept.append(t)
    return kept


def find_materials(plain: str, low: str) -> list[Tagged]:
    has_abbr = bool(_ABBR_UPPER.search(plain))
    if not has_abbr and not any(f in low for f in _MAT_PREFILTER):
        return []
    out: list[Tagged] = []
    cyr = plain.translate(_LAT2CYR_UPPER).replace("3", "З") if has_abbr else plain
    for md, rx, ab in _MAT_RX:
        for m in rx.finditer(low):
            if md.label == "сильвинит" and V.COMPANY_BEFORE.search(plain[max(0, m.start() - 14):m.start()]):
                continue
            inc = _INCLUSION_BEFORE.search(low[max(0, m.start() - 40):m.start()])
            if inc and (inc.group("w") or _INCLUSION_WORD.search(low[m.start():m.end()])):
                continue            # «сильвинит с глинистыми прослойками», «с прослоями глины»: not the rock itself
            if _NEGATED_BEFORE.search(low[max(0, m.start() - 10):m.start()]):
                continue
            out.append(Tagged(md.label, md.group, m.start(), m.end(), plain[m.start():m.end()]))
        if ab is not None and has_abbr:
            for m in ab.finditer(cyr):
                out.append(Tagged(md.label, md.group, m.start(), m.end(), plain[m.start():m.end()]))
    return _dedupe_tags(out)


_SITE_RX = [(label, re.compile(p, re.I)) for label, p in V.SITES]
_SCALE_RX = [(cat, key, re.compile(r"(?<![\w-])(?:" + p + r")", re.I)) for cat, key, p in V.SCALE_CUES]
_NOT_MASSIF_BEFORE = re.compile(r"(?:закладочн|рифогенн|лесн|жил|дачн|садов|соляно-купольн)\w*\s+$", re.I)
_NOT_MASSIF_AFTER = re.compile(r"^\s*(?:данных|точек|значений|измерений|информации|памяти)", re.I)


def find_sites(low: str) -> list[Tagged]:
    out: list[Tagged] = []
    for label, rx in _SITE_RX:
        for m in rx.finditer(low):
            lab = label + m.group(1) if label.endswith("-") and m.groups() and m.group(1) else label.rstrip("-")
            out.append(Tagged(lab, "ANALOGUE" if lab.startswith("ANALOGUE:") else "VKM", m.start(), m.end(),
                              low[m.start():m.end()]))
    return _dedupe_tags(out)


def find_scale_cues(low: str) -> list[Tagged]:
    out: list[Tagged] = []
    for cat, key, rx in _SCALE_RX:
        for m in rx.finditer(low):
            if key == "в массиве" and (_NOT_MASSIF_BEFORE.search(low[max(0, m.start() - 20):m.start()])
                                       or _NOT_MASSIF_AFTER.search(low[m.end():m.end() + 16])):
                continue
            out.append(Tagged(cat, key, m.start(), m.end(), key))
    out.sort(key=lambda t: t.start)
    return out


_GENERIC_SITES = ("ВКМ", "СКРУ", "БКПРУ")


def specific_site(tags: list[Tagged]) -> str | None:
    """One site label of a group of site tags: the most specific when they are consistent («ВКМ» + «СКРУ-1» →
    «СКРУ-1», «СКРУ» + «СКРУ-2» → «СКРУ-2»); None when they disagree."""
    labels = {t.label for t in tags}
    if not labels:
        return None
    if len(labels) == 1:
        return next(iter(labels))
    if any(lab.startswith("ANALOGUE:") for lab in labels):
        return None
    specific = [lab for lab in labels if lab not in _GENERIC_SITES]
    if len(specific) == 1:
        s = specific[0]
        fam = "СКРУ" if s.startswith("СКРУ") else ("БКПРУ" if s.startswith("БКПРУ") else None)
        others = labels - {s, "ВКМ"}
        return s if not others or others == {fam} else None
    if not specific:
        rest = labels - {"ВКМ"}
        return next(iter(rest)) if len(rest) == 1 else None
    return None


def nearest_label(tags: list[Tagged], pos: float) -> str | None:
    """Label of the tag nearest to ``pos``; None when different labels are equally near."""
    if not tags:
        return None
    dist = [abs((t.start + t.end) / 2.0 - pos) for t in tags]
    best = min(dist)
    near = {t.label for t, d in zip(tags, dist) if d - best < 1e-9}
    return near.pop() if len(near) == 1 else None


# ================================================================================================= text candidates
_FOREIGN_RX = re.compile(r"(?<![\w-])(?:" + "|".join(V.FOREIGN_NOUNS) + r")", re.I)
_LINK = re.compile(r"(?:составля\w*|составил\w*|равн\w*|равен|достига\w*|достиг\w*|изменя\w*|колебл\w*|находит\w*|"
                   r"было|был[аио]?|—|–|=|:|\bis\b|\bwas\b|\bwere\b|\bof\b|equals?)")
_CHANGE_TAIL = re.compile(r"(?:(?:снижа|сниже|уменьш|увелич|возраст|повыш|пониж|пада|раст[её]т|рост|вырос|отлича|"
                          r"превыша|increas|decreas|reduc|higher|lower|greater|smaller|differ)\w*[^.;\d]{0,25}"
                          r"(?:\bна|\bв|\bby|\bfrom)|(?<![\w-])кратн\w*|(?<![\w-])multiples? of|(?<![\w-])шаг\w*)\s*$",
                          re.I)
_QUALIFIERS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"(?:не\s+более|не\s+выше|не\s+превыша\w*|не\s+больше|(?<![\w-])до|≤|<=|up\s+to|not\s+more\s+than|"
                r"at\s+most|no\s+more\s+than)\s*$", re.I), "≤"),
    (re.compile(r"(?:не\s+менее|не\s+ниже|≥|>=|at\s+least|not\s+less\s+than)\s*$", re.I), "≥"),
    (re.compile(r"(?:(?<![\w-])менее|(?<![\w-])меньше|less\s+than|<)\s*$", re.I), "<"),
    (re.compile(r"(?:(?<![\w-])более|(?<![\w-])больше|(?<![\w-])свыше|(?<![\w-])выше|more\s+than|greater\s+than|"
                r"(?<![\w-])over|(?<![\w-])above|>)\s*$", re.I), ">"),
    (re.compile(r"(?:(?<![\w-])около|(?<![\w-])порядка|(?<![\w-])примерно|(?<![\w-])приблизительно|"
                r"(?<![\w-])приближенно|(?<![\w-])ориентировочно|≈|~|≅|approximately|(?<![\w-])about|"
                r"(?<![\w-])around|(?<![\w-])roughly|of\s+the\s+order\s+of)\s*$", re.I), "≈"),
)
_MEAN = re.compile(r"(?:в\s+среднем|средн\w*|\baverage|\bmean\b|on\s+average)", re.I)
_RESPECTIVELY = re.compile(r"соответственно|respectively", re.I)
_REF_ALWAYS = re.compile(r"(?:(?<![\w-])(?:рис|табл|стр|с|гл|разд|п|пп|fig|figs|eq|eqs|sect|ch|p|pp|no|ref|скв|"
                         r"ф-л[аеы])\.|(?<![\w-])(?:рисун\w*|таблиц\w*|формул\w*|уравнени\w*|выражени\w*|глав\w*|"
                         r"раздел\w*|приложени\w*|figure|table|equation|section|chapter|page|гост|ост|ту|снип|iso|"
                         r"astm|din)|№|\[)\s*$", re.I)
_REF_NUMBERING = re.compile(r"(?<![\w-])(?:пласт\w*|блок\w*|панел\w*|лав\w*|вариант\w*|лини[июя]\w*|профил\w*|"
                            r"станци\w*|участ\w*|сери[июя]\w*|тип\w*|образц\w*|образец|опыт\w*|точк\w*|репер\w*|"
                            r"пункт\w*|скважин\w*|шахт\w*|рудник\w*|ствол\w*|групп\w*|этап\w*|стади\w*|случа\w*|"
                            r"схем\w*|модел\w*|слой|сло[яеи]|зон\w*|крив(?:ая|ой|ую|ые|ых|ым|ыми)|type|case|sample|"
                            r"specimen|model|zone|layer|stage|scheme|borehole|well|line|station|point|block|panel|"
                            r"series|group|variant|step|curves?)\s*$", re.I)
_ASSIGN_TAIL = re.compile(r"(?<![\w])(?P<sym>[A-Za-zА-Яа-яЁёΑ-Ωα-ωϑϕ][\w′'*^]{0,7}(?:_\{?[\w.,]{1,8}\}?)?)\s*"
                          r"(?P<op>=|≈|≅|~|≤|≥|<|>)\s*$")
_SOFT_WORDS = frozenset("""соответственно составляет составляют составил составила составило составили равен равна
    равно равны равным равной достигает достигают достигал достигала около порядка примерно не более менее до от в
    среднем при этом для у в а и или его ее их она они оно был была было были также того этого значение значения
    значений величина величины величиной среднее средняя средний also is are was were of the respectively about
    approximately and with value values""".split())


def _dims_ok(prop: V.PropertyDef, unit: Unit | None) -> bool:
    if V.ANY in prop.dims:
        return True
    if unit is None:
        return V.NONE in prop.dims
    return unit.dim in prop.dims


def _in_range(prop: V.PropertyDef, lo: float | None, hi: float | None, dim: str | None) -> bool:
    vr = prop.vrange
    if vr is None or lo is None or hi is None:
        return True
    if isinstance(vr, dict):
        vr = vr.get(dim or V.NONE)
        if vr is None:
            return True
    return vr[0] <= lo and hi <= vr[1]


def _si_or_bare(val: Val, unit: Unit | None) -> tuple[float | None, float | None]:
    if unit is not None:
        lo, hi, _ = value_si(val.vmin, val.vmax, val.pm, unit)
        return lo, hi
    return val.vmin, val.vmax                  # a bare number of a dimensionless property is its SI value


def _mask(low: str, a: int, b: int, spans: list[tuple[int, int]]) -> str:
    """``low[a:b]`` with the given spans blanked (other property phrases do not block a value)."""
    chars = list(low[a:b])
    for s, e in spans:
        for k in range(max(s, a), min(e, b)):
            chars[k - a] = " "
    return "".join(chars)


def _gap_blocked(gap_low: str, prop: V.PropertyDef) -> bool:
    last = None
    for m in _FOREIGN_RX.finditer(gap_low):
        if any(m.group(0).startswith(o) for o in prop.own_nouns):
            continue
        last = m
    if last is None:
        return False
    tail = gap_low[last.end():]
    num = re.search(r"\d", tail)
    return not (num and _LINK.search(tail[num.end():]))    # the other quantity took its own value earlier


_DIM_AFTER = re.compile(r"\s*,?\s*(шириной|ширины|высотой|длиной|мощностью|глубиной|толщиной|диаметром|радиусом|"
                        r"wide|long|high|thick|deep|in diameter|across|in length|in width|in height)\b", re.I)
# after the value: a position («78 м от кровли», «в 70 м ниже кровли») or a comparison («на 200 м глубже»)
_RELATION_AFTER = re.compile(r"\s*,?\s*(?:(?:ниже|выше|от|до|вглубь)\s+(?:кровл|почв|подошв|поверхност|устья|забо|"
                             r"контур|границ|оси|центр|края|кромк)\w*|(?:глубже|выше|ниже|больше|меньше|шире|уже|"
                             r"длиннее|короче|мощнее|сильнее|deeper|higher|lower|shallower|wider|narrower|longer|"
                             r"shorter|thicker|thinner|more than|less than)\b|(?:below|above|from) the (?:top|base|"
                             r"surface|roof|floor))", re.I)
_OTHER_LABEL_TAIL = re.compile(r"[a-zа-яё]{3,}[^\d:=;.]{0,24}[:=]\s*$")
# a «dynamic» modulus or Poisson's ratio comes from wave velocities, not from a static test: flagged
_DYNAMIC_PROPS = frozenset({"youngs_modulus", "deformation_modulus", "shear_modulus", "bulk_modulus",
                            "poisson_ratio"})
# a room width, a loading degree, a subsidence or a backfill ratio has no material (a thickness or the depth of a
# stratum keeps the stratum named with it)
_NO_MATERIAL_PROPS = frozenset(
    p.key for p in V.PROPERTIES
    if p.group in ("subsidence", "backfill") or (p.group == "geometry" and "thickness" not in p.key
                                                 and "depth" not in p.key))
_LABEL_UNIT_TAIL = re.compile(r"(?:^|[\s;|])(?P<lab>[A-Za-zΑ-Ωα-ω][\w]{0,3}|[А-ЯЁ][\wа-яё]{0,3})\s*,\s*"
                              r"(?P<unit>[^\s,;]{1,14})\s*$")


def _other_label_before(gap: str, m: Mention) -> bool:
    """«… B1, sec-1 7.1·10⁶»: a label with its unit right before the value, other than the phrase's own symbol."""
    lu = _LABEL_UNIT_TAIL.search(gap)
    if not lu or match_unit(lu.group("unit"), 0) is None:
        return False
    lab = symbolish(lu.group("lab"))
    return not (lab and m.symbol and symbol_key(lab, loose=True) == symbol_key(m.symbol, loose=True))
_CONTENT_WORD = re.compile(r"[a-zа-яё]{3,}")


_ERROR_BEFORE = re.compile(r"(?:погрешност|ошибк|расхождени|отклонени|невязк|точност|разброс|error|uncertaint|"
                           r"deviation|accuracy|misfit|residual)\w*(?:\s+[^\s,.;]+){0,3}\s*$", re.I)
_SUBCLAUSE_START = re.compile(r",\s*(?:котор\w*|[а-яё]+(?:ющ|ущ|ащ|ящ|вш|ем|им)"
                              r"(?:ий|ая|ее|ие|его|ей|их|ую|ым|ыми|ой|ом)|which|that)\b[^,]*$", re.I)


def _in_subclause(plain: str, m: Mention, val: Val) -> bool:
    """The phrase sits inside a participial or relative clause set off by commas, and the value comes after that
    clause («уровень извлечения, обеспечивающий … степень нагружения целиков, составляет 25–35 %»)."""
    before = plain[max(0, m.start - 80):m.start]
    return bool(_SUBCLAUSE_START.search(before)) and "," in plain[m.end:val.start]


def _clause_break(gap_low: str, limit: int = 4) -> bool:
    """After the last number of the gap, more than ``limit`` content words: the value starts a new clause
    («… ВЗТ порядка 15 м дает возможность разрабатывать глубоко залегающие (до 2000 м)»)."""
    digits = [m.end() for m in re.finditer(r"\d", gap_low)]
    if not digits:
        return False
    words = [w for w in _CONTENT_WORD.findall(gap_low[digits[-1]:]) if w not in _SOFT_WORDS]
    return len(words) > limit


def _inherit_ok(plain: str, low: str, m: Mention, val: Val, mats: list["Tagged"]) -> bool:
    """A bare number takes the unit printed with the phrase when only numbers, materials, symbols and soft words
    stand between them («Сцепление, МПа: каменная соль 10,5; сильвинит 5,7»)."""
    a = m.unit.end
    if a > val.start:
        return False
    txt = list(low[a:val.start])
    for t in mats:
        for k in range(max(t.start, a), min(t.end, val.start)):
            txt[k - a] = " "
    words = re.findall(r"[a-zа-я]{3,}", "".join(txt))
    return all(w in _SOFT_WORDS for w in words)


@dataclass
class Cand:
    prop: V.PropertyDef
    val: Val
    unit: Unit | None
    mention: Mention | None
    symbol: str | None = None
    qualifier: str = "="
    material: str | None = None
    material_raw: str | None = None
    material_group: str | None = None
    scale: str = V.UNKNOWN
    scale_basis: str = "NONE"
    scale_cues: list[str] = field(default_factory=list)
    site: str = V.UNKNOWN
    site_basis: str = "NONE"
    flags: list[str] = field(default_factory=list)
    confidence: float = 0.0
    unit_inherited: bool = False
    multiplier: bool = False


def _qualifier(gap: str, val: Val) -> str:
    if val.qualifier in ("range", "±"):
        return val.qualifier
    tail = gap[-32:]
    for rx, q in _QUALIFIERS:
        if rx.search(tail):
            return q
    return "="


_DEF_BEFORE = re.compile(r"(?:^|[\s,;:(])(?P<sym>[A-Za-zА-ЯЁа-яёΑ-Ωα-ωϑϕ][\w′'*^]{0,6}(?:_\{?[\w.,]{1,8}\}?)?)\s*"
                         r"(?:[—–−]|-\s)\s*(?:это\s+)?$")


def symbol_definitions(plain: str, low: str, mentions: list[Mention]) -> list[tuple[str, str]]:
    """(symbol, property) pairs a text defines: «модуль спада (М, ГПа)», «Dпр, ГПа — …» (the phrase's own symbol),
    «σпр — предел прочности», «где E – модуль деформации» (a symbol and a dash right before the phrase)."""
    out = []
    for m in mentions:
        if m.symbol:
            out.append((m.symbol, m.prop.key))
        d = _DEF_BEFORE.search(plain[max(0, m.start - 24):m.start])
        if d:
            sym = symbolish(d.group("sym"), assigned=True)
            if sym:
                out.append((sym, m.prop.key))
    return out


def text_candidates(text: str | None, lang: str | None = None, *, max_gap: int = 150,
                    definitions: list[tuple[str, str]] | None = None) -> tuple[list[Cand], str, list[int]]:
    """Candidates of one text block (pure). Returns the candidates, the plain text and its offset map; ``definitions``
    (when given) receives the (symbol, property) pairs the text defines."""
    plain, pmap = plain_with_map(text)
    low = lower_same_length(plain)
    if definitions is None and not re.search(r"\d", plain):
        return [], plain, pmap
    mentions = find_mentions(plain, low)
    if definitions is not None and mentions:
        definitions.extend(symbol_definitions(plain, low, mentions))
    if not mentions or not re.search(r"\d", plain):
        return [], plain, pmap
    vals = [v for v in scan_values(plain, lang) if v.reject is None]
    if not vals:
        return [], plain, pmap
    starts = sentence_starts(plain)
    all_mats = find_materials(plain, low)
    # a material named inside a phrase («мощность ПКС», «модуль деформации закладки» is outside) is a fallback only
    inner = [t for t in all_mats if any(m.start <= t.start and t.end <= m.end for m in mentions)]
    mats = [t for t in all_mats if t not in inner]
    sites = find_sites(low)
    cues = find_scale_cues(low)
    spans = [(m.start, m.end) for m in mentions]
    mat_spans = [(t.start, t.end) for t in mats]
    by_sent: dict[int, list[Mention]] = defaultdict(list)
    for m in mentions:
        by_sent[sentence_of(starts, m.start)].append(m)
    first_sym: dict[int, str] = {}              # mention start → the symbol its first value was assigned to
    out: list[Cand] = []
    for val in vals:
        si = sentence_of(starts, val.start)
        pref = low[max(0, val.start - 16):val.start]
        if _REF_ALWAYS.search(pref):
            continue
        near = sorted((m for m in by_sent.get(si, []) if m.end <= val.start and val.start - m.end <= max_gap),
                      key=lambda x: -x.end)
        if _RELATION_AFTER.match(low, val.span_end):
            continue                                  # «78 м от кровли», «на 200 м глубже»: a position or a change
        if re.search(r"±\s*$", plain[max(0, val.start - 3):val.start]):
            continue                                  # «± 0,36 м» alone: an uncertainty, not a value
        if val.unit is None:
            nxt = re.match(r"\s*([a-zа-яё]{3,})", low[val.end:])
            if nxt and nxt.group(1) not in _SOFT_WORDS and not any(t.start == val.end + nxt.start(1)
                                                                    for t in mats):
                continue                              # «1 porosity unit», «3 образца»: the number counts a noun
        after_word = _DIM_AFTER.match(low, val.span_end)
        chosen = None
        for rank, m in enumerate(near):
            if _ERROR_BEFORE.search(low[max(0, m.start - 48):m.start]) or _in_subclause(plain, m, val):
                continue                    # «расхождение значений глубин», «…, обеспечивающий … степень нагружения,»
            unit, inherited = val.unit, val.unit_inherited
            if unit is None and m.unit is not None and _inherit_ok(plain, low, m, val, mats):
                unit, inherited = m.unit, True
            if unit is None and rank > 0:
                break                       # a bare number belongs to the nearest phrase or to none
            if unit is None and V.NONE not in m.prop.dims and V.ANY not in m.prop.dims:
                continue
            if not _dims_ok(m.prop, unit):
                continue
            if unit is None and _REF_NUMBERING.search(pref):
                continue
            if after_word and not any(after_word.group(1).startswith(o) for o in m.prop.own_nouns):
                break                       # «… 8 m wide», «целик 12 м шириной»: the value names another quantity
            gap = plain[m.end:val.start]
            if _CHANGE_TAIL.search(gap[-40:]):
                break                       # a relative change: not a value of any earlier phrase either
            gap_low = _mask(low, m.end, val.start, spans)
            gap_clean = _mask(gap_low, 0, len(gap_low), [(s - m.end, e - m.end) for s, e in mat_spans])
            digits = [x.end() for x in re.finditer(r"\d", gap_clean)]
            if _gap_blocked(gap_low, m.prop) or _clause_break(gap_clean) or (
                    digits and _OTHER_LABEL_TAIL.search(gap_clean[digits[-1]:])):
                break                       # «… D0 = 260 м; продвижение лавы: 900 м»: a label of another quantity
            am = _ASSIGN_TAIL.search(gap)
            sym = symbolish(am.group("sym"), assigned=True) if am else None
            own = m.symbol or first_sym.get(m.start)
            if sym and own and symbol_key(sym, loose=True) != symbol_key(own, loose=True):
                break                       # «E = 20 ГПа, ν = 0,3», «глубина H = 480 м; … b = 3,7 м»: another symbol
            if not sym and _other_label_before(gap, m):
                break                       # «… B1, sec-1 7.1·10⁶»: a flattened table row of another label
            lo, hi = _si_or_bare(val, unit)
            if not _in_range(m.prop, lo, hi, unit.dim if unit is not None else None):
                continue
            c = Cand(m.prop, val, unit, m, unit_inherited=inherited)
            c.qualifier = _qualifier(gap, val)
            if c.qualifier == "=" and (_MEAN.search(gap) or _MEAN.search(low[max(0, m.start - 20):m.start])):
                c.qualifier = "mean"
            if sym:
                c.symbol = sym
                first_sym.setdefault(m.start, sym)
                op = am.group("op")
                if op in ("≈", "≅", "~"):
                    c.qualifier = "≈"
                elif op in ("≤", "<", "≥", ">"):
                    c.qualifier = op
                if definitions is not None and am.start() <= 3:
                    definitions.append((sym, m.prop.key))      # «предел прочности σсж = 19,8 МПа»
            c.symbol = c.symbol or m.symbol
            if inherited and not val.unit_inherited:
                c.flags.append("UNIT_FROM_PHRASE")
                if re.fullmatch(r"[\s\d.,;:–—\-−±()]*", plain[m.unit.end:val.start]):
                    c.flags.append("SERIES")
            if val.unit_inherited:
                c.flags.append("UNIT_FROM_LIST")
            c.flags.extend(f for f in val.flags if f in ("FROM_TO", "SHARED_POWER"))
            if "динамическ" in low[m.start:m.end] and m.prop.key in _DYNAMIC_PROPS:
                c.flags.append("DYNAMIC")
            chosen = c
            break
        if chosen is None:
            continue
        if chosen.val.vmin == chosen.val.vmax == 0 and (chosen.unit is None or chosen.unit_inherited):
            continue                                  # a bare «0»: an axis origin or a list item, not a value
        _assign_material(chosen, plain, low, mats, starts, si, inner)
        out.append(chosen)
    # a series under a phrase unit that is an arithmetic progression («Porosity, % 45 30 15 0») is an axis scale
    series: dict[int, list[Cand]] = defaultdict(list)
    for c in out:
        if "SERIES" in c.flags and c.mention is not None:
            series[c.mention.start].append(c)
    ticks = {id(c) for cs in series.values() if _arithmetic([c.val.vmin for c in cs]) for c in cs}
    out = [c for c in out if id(c) not in ticks]
    cspans = [(c.mention.start if c.mention is not None else c.val.start, c.val.span_end) for c in out]
    for k, c in enumerate(out):
        _assign_scale_site(c, low, cues, sites, starts, sentence_of(starts, c.val.start), vals=vals,
                           cand_spans=cspans[:k] + cspans[k + 1:], mentions=mentions)
    _respectively(out, low, mats)
    for c in out:
        c.confidence = _confidence(c, "TEXT")
    return out, plain, pmap


def _arithmetic(seq: list[float]) -> bool:
    """>= 3 values with one constant non-zero step (0, 15, 30, 45 — the ticks of an axis)."""
    if len(seq) < 3:
        return False
    d = [b - a for a, b in zip(seq, seq[1:])]
    return d[0] != 0 and all(abs(x - d[0]) <= 1e-9 * max(1.0, abs(d[0])) for x in d)


def _set_mat(c: Cand, t: Tagged) -> None:
    c.material, c.material_group, c.material_raw = t.label, t.group, t.raw


_NO_MATERIAL_BEFORE = frozenset({"geometry", "subsidence"})


def _assign_material(c: Cand, plain: str, low: str, mats: list[Tagged], starts: list[int], si: int,
                     inner: list[Tagged] | None = None) -> None:
    if c.prop.key in _NO_MATERIAL_PROPS:
        return                              # a depth, a room width, a subsidence or a backfill ratio has no material
    val, m = c.val, c.mention
    end = val.span_end
    own = [t for t in inner or [] if m.start <= t.start and t.end <= m.end]
    # 1. «20 ГПа для каменной соли», «… of rock salt»
    for t in mats:
        if end <= t.start <= end + 28 and re.fullmatch(r"\s*[,(]?\s*(?:для|у|в|of|for|in)\s+", plain[end:t.start],
                                                       re.I):
            _set_mat(c, t)
            return
    # 2. between the phrase and the value, after the previous number («… соли 20 ГПа, сильвинита — 15 ГПа»)
    gap_m = [t for t in mats if m.end <= t.start and t.end <= val.start]
    if gap_m:
        digits = [x.end() for x in re.finditer(r"\d", plain[m.end:val.start])]
        cut = m.end + digits[-1] if digits else m.end
        tail = [t for t in gap_m if t.start >= cut] or ([] if digits else gap_m)
        if not tail and digits:
            tail = gap_m                        # «… каменной соли 20, 15 и 10 ГПа»: stated before the list
        labels = {t.label for t in tail}
        if len(labels) == 1:
            _set_mat(c, tail[-1])
        else:
            c.flags.append("MATERIAL_AMBIGUOUS")
            c.material_raw = " | ".join(dict.fromkeys(t.raw for t in tail))
        return
    # 3. named inside the phrase («мощность ПКС»; not «коэффициент закладки», where it is the property itself)
    if len({t.label for t in own}) == 1 and c.prop.group != "backfill":
        _set_mat(c, own[-1])
        return
    # 4. before the phrase in the same sentence (not for mining geometry and surface movements, except a body named
    # right before its thickness or depth: «пачки сильвинита мощностью 0,5 м»)
    s0 = starts[si]
    if c.prop.group in _NO_MATERIAL_BEFORE:
        close = [t for t in mats if s0 <= t.start and t.end <= m.start and m.start - t.end <= 12
                 and not re.search(r"[,;:.()]", plain[t.end:m.start])]
        if close:
            _set_mat(c, close[-1])
        return
    before = [t for t in mats if s0 <= t.start and t.end <= m.start]
    if before:
        if len({t.label for t in before}) == 1:
            _set_mat(c, before[-1])
            return
        last, prev = before[-1], before[-2]
        if not re.search(r"\d", plain[last.end:m.start]) and not re.search(r"[,;]|\bи\b|\band\b",
                                                                         plain[prev.end:last.start]):
            _set_mat(c, last)
            return
        c.flags.append("MATERIAL_AMBIGUOUS")
        c.material_raw = " | ".join(dict.fromkeys(t.raw for t in before))


_AFTER_CUE_MAX = 40
_WORDS_ONLY = re.compile(r"[^\W\d_\s]*\s*[^\W\d_\s]*\s*")          # «нормативного| значения |степени нагружения»


def _local_cues(c: Cand, sent: list[Tagged], vals: list[Val], cand_spans: list[tuple[int, int]],
                s1: int, low: str = "", mentions: list[Mention] | None = None) -> list[Tagged]:
    """The cues of the sentence that qualify this value rather than a neighbour:
    - between its phrase and the value, with no other value in between («модуль образцов 20 ГПа, в массиве — 5 ГПа»);
    - before its phrase, outside the phrase…value span of another candidate, not right before the phrase of another
      property («нормативного значения степени нагружения … целики шириной 14,6 м»), and with no value outside the
      vocabulary in between (references «[20]» do not count): the head of an enumeration («при моделировании приняты:
      E = 20 ГПа, ν = 0,3»), not the cue of another quantity («плотность в массиве 2,7 т/м³, … предел прочности»);
    - right after the value (≤ 40 chars) when no other value follows in the sentence."""
    v0, v1 = c.val.start, c.val.span_end
    m0 = c.mention.start if c.mention is not None else v0
    lid = c.val.list_id
    others = [v for v in vals if not (v0 <= v.start < v1 or v.start <= v0 < v.end)
              and not (lid >= 0 and v.list_id == lid)
              and not (low and _REF_ALWAYS.search(low[max(0, v.start - 16):v.start]))]
    cand_vals = {v.start for v in others if any(a <= v.start < b for a, b in cand_spans)}
    foreign = [(m.start, m.end) for m in mentions or [] if m.prop.key != c.prop.key]
    out = []
    for t in sent:
        if m0 <= t.start and t.end <= v0:
            if not any(t.end <= v.start < v0 for v in others):
                out.append(t)
        elif t.end <= m0:
            if any(a <= t.start and t.end <= b for a, b in cand_spans):
                continue
            if any((t.end <= a <= t.end + 12 and a < m0 and _WORDS_ONLY.fullmatch(low[t.end:a])) or a <= t.start < b
                   for a, b in foreign):
                continue
            if any(t.end <= v.start < m0 and v.start not in cand_vals for v in others):
                continue
            out.append(t)
        elif t.start >= v1:
            if t.start - v1 <= _AFTER_CUE_MAX and not any(v1 <= v.start < s1 for v in others):
                out.append(t)
    return out


def _assign_scale_site(c: Cand, low: str, cues: list[Tagged], sites: list[Tagged], starts: list[int],
                       si: int, *, vals: list[Val] | None = None, cand_spans: list[tuple[int, int]] | None = None,
                       mentions: list[Mention] | None = None) -> None:
    s0 = starts[si]
    s1 = starts[si + 1] if si + 1 < len(starts) else len(low)
    pos = (c.val.start + c.val.end) / 2.0
    sent = [t for t in cues if s0 <= t.start < s1]
    local = _local_cues(c, sent, vals or [], cand_spans or [], s1, low, mentions) if sent else []
    if local:
        c.scale_cues = sorted({t.group for t in local})
        cats = {t.label for t in local}
        if len(cats) == 1:
            c.scale, c.scale_basis = cats.pop(), "LOCAL"
        else:
            lab = nearest_label(local, pos)
            if lab is not None:
                c.scale, c.scale_basis = lab, "LOCAL_NEAREST"
            else:
                c.flags.append("SCALE_CONFLICT")
    elif sent or cues:
        # a cue of a neighbouring value or of another sentence of the block is only reported, never taken as the scale
        c.scale_cues = sorted({t.group for t in (sent or cues)})
        c.scale_basis = "NONE_SENTENCE_CUES" if sent else "NONE_BLOCK_CUES"
    sent_sites = [t for t in sites if s0 <= t.start < s1]
    if sent_sites:
        lab = specific_site(sent_sites) or nearest_label(sent_sites, pos)
        if lab is not None:
            c.site, c.site_basis = lab, "SENTENCE"
        else:
            c.flags.append("SITE_CONFLICT")
    elif sites:
        lab = specific_site(sites)
        if lab is not None:
            c.site, c.site_basis = lab, "BLOCK"
        else:
            c.flags.append("SITE_CONFLICT")


def _respectively(cands: list[Cand], low: str, mats: list[Tagged]) -> None:
    """«модуль упругости каменной соли и сильвинита соответственно 20 и 15 ГПа»: materials in the printed order."""
    groups: dict[tuple[int, int], list[Cand]] = defaultdict(list)
    for c in cands:
        if c.val.list_id >= 0 and c.mention is not None:
            groups[(c.mention.start, c.val.list_id)].append(c)
    for cs in groups.values():
        cs.sort(key=lambda c: c.val.start)
        m = cs[0].mention
        seq = [t for t in mats if m.end <= t.start and t.end <= cs[0].val.start]
        if len(seq) >= 2 and len(seq) == len(cs) and len({t.label for t in seq}) == len(seq):
            for c, t in zip(cs, seq):
                _set_mat(c, t)
                c.flags = [f for f in c.flags if f != "MATERIAL_AMBIGUOUS"] + ["RESPECTIVE_ORDER"]
        elif len(seq) >= 2:
            for c in cs:
                c.material = c.material_group = None
                c.material_raw = " | ".join(dict.fromkeys(t.raw for t in seq))
                if "MATERIAL_AMBIGUOUS" not in c.flags:
                    c.flags.append("MATERIAL_AMBIGUOUS")


_PENALTY_FLAGS = frozenset({"MATERIAL_AMBIGUOUS", "SERIES", "HEADER_MULTIPLIER", "SYMBOL_ONLY_HEADER",
                            "SCALE_CONFLICT", "SITE_CONFLICT", "ALTERNATIVE_IN_PARENS", "SYMBOL_DEFINED_IN_SOURCE",
                            "CROSS_BLOCK", "PROPERTY_FROM_CAPTION", "DECIMAL_POINT_SUSPECT"})


def _confidence(c: Cand, method: str) -> float:
    """Heuristic 0–1: method base, printed unit, symbol, material, phrase–value distance, penalties for flags."""
    s = {"TABLE": 0.6, "TEXT": 0.6, "NEAR_FORMULA": 0.5}[method]
    if c.unit is not None:
        s += 0.1 if c.unit_inherited else 0.15
    elif V.NONE in c.prop.dims and c.prop.vrange is not None:
        s += 0.05
    if c.symbol:
        s += 0.05
    if c.material:
        s += 0.05
    if c.mention is not None and method == "TEXT":
        gap = c.val.start - c.mention.end
        s += 0.1 if gap <= 25 else (0.0 if gap <= 70 else -0.1)
    s -= 0.05 * sum(1 for f in set(c.flags) if f in _PENALTY_FLAGS)
    return round(max(0.05, min(0.95, s)), 2)


# ================================================================================================= tables
# «м/мин», «об/мин»: minutes of a unit, not a minimum («/» in the look-behind)
_STAT = (("min", re.compile(r"(?<![\w/-])(?:min|мин\.?|минимальн\w*|наименьш\w*)(?![\w-])", re.I)),
         ("max", re.compile(r"(?<![\w/-])(?:max|макс\.?|максимальн\w*|наибольш\w*)(?![\w-])", re.I)),
         ("mean", re.compile(r"(?<![\w/-])(?:mean|average|avg|ср\.|средн\w*|сред\.)(?![\w-])", re.I)))
_UNITS_HEADER = re.compile(r"(?:ед(?:\.|иниц\w*)\s*(?:изм(?:\.|ерени\w*)?)?|размерност\w*|\bunits?\b)", re.I)
# a column of values of the row's quantity («Значение», «Range», «Default»): not a symbol of another quantity
_GENERIC_VALUE_HEADER = re.compile(r"^\s*(?:значени\w*|величин\w*|показател\w*|value|values|параметр\w*|range|"
                                   r"default|typical|reference|base\s+case|диапазон\w*|пределы\s+изменени\w*)?\s*$",
                                   re.I)
_METHOD_HEADER = re.compile(r"(?<![\w-])(?:метод\w*|method\w*|способ\w*|средн\w*|mean|average|min|max|мин\.?|"
                            r"макс\.?|минимальн\w*|максимальн\w*)(?![\w-])", re.I)
_LITHO_HEADER = re.compile(r"тип\w* пород|(?<![\w-])пород\w*|литолог\w*|состав\w*|lithology|rock type|\brock\b", re.I)
_LOST_POWER_UNIT = re.compile(r"[(,]\s*10([1-9]\d?)\s*(?:,\s*|\s+)(?=[^\W\d_])")   # «(1021 Pa s)», «,104,МПа»
_CAPTION_CONDITION = re.compile(r"(?<![\w-])(?:в интервале|в диапазоне|при|в зависимости|зависимост\w*|для|"
                                r"at|for|in the interval|as a function|versus|vs)(?![\w-])", re.I)
_NUMBERING_HEADER = re.compile(r"^\s*(?:№|n|no\.?|#)\s*(?:п/п|пп|n/n)?\s*$", re.I)
# a symbol proper («A», «σ», «K_s»), not a sample or specimen label («Б8/9», «96/1»)
_PURE_SYMBOL = re.compile(r"[^\W\d_]{1,3}(?:_[^\W\d_]{1,4})?")
_COUNT_HEADER = re.compile(r"кол(?:-во|ичеств)\w*|числ(?:о|а)\b|№|номер\w*|\bnumber\b|\bcount\b|\bn\s*=|год\w*|дат\w*|"
                           r"\byear\b|\bdate\b|глубин\w*|интервал\w*|размер\w*|h/d|\bsize\b", re.I)
# a dispersion of the values («Коэф. вар., %», «Квадр. откл.», «СКО», «Std. dev.»): not a value of the property
_DISPERSION = re.compile(r"коэф\w*\.?\s*вар\w*|вариаци\w*|квадр\w*\.?\s*(?:откл|ошиб)\w*|ср\.?\s*кв\.?\s*откл\w*|"
                         r"стандартн\w*\s*(?:откл|ошиб)\w*|станд\.?\s*откл\w*|дисперси\w*|погрешност\w*|"
                         r"доверительн\w*|(?<![\w-])ско(?![\w-])|std\.?(?![\w-])|standard\s+(?:deviation|error)|"
                         r"coefficient\s+of\s+variation|(?<![\w-])c\.?o\.?v\.?(?![\w-])|variance", re.I)
_MULT_IN_HEADER = re.compile(r"(?:[·⋅∙×xхЧ*´]\s*10\s*(?:\^\s*\(?\s*([-−–]?\s*\d{1,2})(?:\s*\))?|"
                             r"([-−–]\s?\d{1,2})(?!\d)|(\d{1,2})(?!\d))|"
                             r"(?<![\d.,])10\s*\^\s*\(?\s*([-−–]?\s*\d{1,2})(?:\s*\))?)")


def _hint_key(sym: str) -> str:
    return symbol_key(sym, loose=True)


# symbols that name one property in rock-mechanics tables when the unit dimension agrees («σсж, МПа», «γ, кН/м³»,
# «ν»); ambiguous ones («E» — elastic or deformation modulus, «φ», «C») are never read alone
_SYMBOL_HINTS: dict[str, tuple[str, tuple[str, ...]]] = {
    _hint_key(s): v for s, v in (
        ("σ_сж", ("ucs", (V.PRESSURE,))), ("σ_р", ("tensile_strength", (V.PRESSURE,))),
        ("σ_p", ("tensile_strength", (V.PRESSURE,))), ("ν", ("poisson_ratio", (V.NONE,))),
        ("v", ("poisson_ratio", (V.NONE,))), ("μ", ("poisson_ratio", (V.NONE,))),
        ("γ", ("unit_weight", (V.WEIGHT, V.DENSITY))), ("ρ", ("density", (V.DENSITY, V.WEIGHT))),
        ("K_дл", ("long_term_strength_ratio", (V.NONE,))), ("σ_∞", ("long_term_strength", (V.PRESSURE,))),
        ("σ_дл", ("long_term_strength", (V.PRESSURE,))))}
_REFINES = {"strength_unspecified": {"ucs", "tensile_strength", "flexural_strength", "long_term_strength"}}
_HEADER_TOKENS = re.compile(r"[^\s,;:()]+")
_POWER_IN_HEADER = re.compile(r"[·⋅∙×xхЧ*´]\s*10\s*\S*|(?<![\d.,])10\s*\^\s*\S+")


@dataclass
class HeaderInfo:
    text: str
    prop: V.PropertyDef | None = None
    unit: Unit | None = None
    multiplier_exp: int | None = None
    stat: str | None = None
    symbol: str | None = None
    materials: list[Tagged] = field(default_factory=list)
    symbol_only: bool = False
    symbol_source: bool = False
    ambiguous: bool = False


_FULLWIDTH = str.maketrans({"，": ",", "：": ":", "；": ";", "（": "(", "）": ")", "．": "."})


def cell_plain(text: str | None) -> str:
    return re.sub(r"\s+", " ", plain_with_map(text or "")[0].translate(_FULLWIDTH)).strip()


_HEADER_SEP = re.compile(r"(?P<sep>,|\(|(?<![\w-])в\s|;|:)?\s*")
_STAT_WORD = re.compile(r"^\s*(?:min|мин\.?|max|макс\.?)\s*$", re.I)
# «при 20 °C», «At 293 K», «σ3 = 5 МПа»: a unit right after a standalone number is a condition, not the unit of the
# column (an index «σ1 МПа», a power «10^3 МПа», «10-3 1/сут» is no standalone number)
_AFTER_NUMBER = re.compile(r"(?:^|[\s=(≈~<>≤≥])[-−–]?\d+(?:[.,]\d+)?\s*$")
_CONDITION_TAIL = re.compile(r"\s*[,;(/]?\s*(?:при|at|for|t\s*=|т\s*=)\s*[-−–]?\d[\d.,]*\s*[^\s\d]{0,6}\s*\)?\s*$",
                             re.I)


def header_unit(text: str) -> Unit | None:
    """The unit of a header: the first one after an explicit separator («Модуль деформации, ГПа», «σ (МПа) min»),
    else one that closes the text («Плотность г/см³») or is followed only by a condition («…, GPa at 293 K»);
    statistics («min», «мин») and conditions («при 20 °C») are not units."""
    tail_end = len(text.rstrip(" )*.:;"))
    fallback, skip_to = None, 0
    for um in _HEADER_SEP.finditer(text):
        p = um.end()
        if p < skip_to or (p > 0 and text[p - 1].isalnum() and um.group("sep") is None):
            continue
        u = match_unit(text, p)
        if u is None or _STAT_WORD.match(u.raw):
            continue
        skip_to = u.end
        if um.group("sep") is None and _AFTER_NUMBER.search(text[:p]) and \
                not re.search(r"[(,]\s*10[1-9]\d?\s*$", text[:p]):      # «(1021 Pa s)»: 10²¹ with the superscript lost
            continue
        if um.group("sep"):
            return u
        if u.end >= tail_end or re.match(r"\s*[),;*]", text[u.end:u.end + 2]) or _CONDITION_TAIL.match(text, u.end):
            fallback = u
    return fallback


def parse_header(text: str, source_symbols: dict[str, str] | None = None) -> HeaderInfo:
    """Property, unit, power multiplier, statistic, symbol and materials named in a header, row label or caption.
    ``source_symbols`` — symbols the same source defines by a phrase («σпр — предел прочности»), loose key →
    property."""
    h = HeaderInfo(text)
    if not text:
        return h
    low = lower_same_length(text)
    ms = find_mentions(text, low)
    if len({m.prop.key for m in ms}) == 1:
        h.prop, h.symbol = ms[0].prop, ms[0].symbol
    elif ms:
        h.ambiguous = True
    for key, rx in _STAT:
        if rx.search(text):
            h.stat = key
            break
    mm = _MULT_IN_HEADER.search(text)
    if mm:
        g = next((x for x in mm.groups() if x), None)
        if g and _to_int(g) != 0:
            h.multiplier_exp = _to_int(g)
    lost = _LOST_POWER_UNIT.search(text)          # «Viscosity (1021 Pa s)»: 10²¹ with the superscript lost
    if lost and h.multiplier_exp is None and match_unit(text, lost.end()) is not None:
        h.multiplier_exp = int(lost.group(1))
    h.unit = header_unit(text)
    h.materials = find_materials(text, low)
    dim = h.unit.dim if h.unit is not None else V.NONE
    # standalone symbols: the global unambiguous ones and those the source itself defines by a phrase
    head = text[:h.unit.start] if h.unit is not None else text
    hints: dict[str, tuple[str, bool]] = {}              # property → (symbol, defined by the source)
    counts = bool(_COUNT_HEADER.search(head))             # «Кол-во образцов», «h/d»: never a property by a symbol
    for tok in ([] if counts else _HEADER_TOKENS.findall(_POWER_IN_HEADER.sub(" ", head))):
        sym = symbolish(tok) if len(tok) <= 8 else None
        if not sym:
            continue
        hint = _SYMBOL_HINTS.get(_hint_key(sym))
        if hint and dim in hint[1]:
            hints.setdefault(hint[0], (sym, False))
        elif hint is None and source_symbols and source_symbols.get(_hint_key(sym)):
            key = source_symbols[_hint_key(sym)]
            if _dims_ok(V.PROPERTY_BY_KEY[key], h.unit):
                hints.setdefault(key, (sym, True))
    if h.prop is not None and hints:
        # a phrase refined by a symbol («Предел прочности … σсж, МПа» → ucs); contradicted by one (an OCR-shifted
        # header «Относительная предельная деформация | v») it is ambiguous
        if len(hints) == 1 and next(iter(hints)) in _REFINES.get(h.prop.key, ()):
            key, (sym, own) = next(iter(hints.items()))
            h.prop, h.symbol, h.symbol_source = V.PROPERTY_BY_KEY[key], sym, own
        elif any(k != h.prop.key for k in hints):
            h.prop, h.ambiguous = None, True
    elif h.prop is None and not h.ambiguous:
        if len(hints) == 1:                                   # «При t→0 σcж, МПа», «Dпр, ГПа»: one symbol
            key, (sym, own) = next(iter(hints.items()))
            h.prop, h.symbol, h.symbol_only, h.symbol_source = V.PROPERTY_BY_KEY[key], sym, not own, own
        else:
            core = _POWER_IN_HEADER.sub("", head).strip(" ,;:()*")
            h.symbol = symbolish(core) if core and len(core) <= 10 else None
    return h


_EMPTY_CELL = re.compile(r"^[\s\-–—−.…*/]*$")
_CELL_QUAL = re.compile(r"^(≤|≥|<|>|≈|~|до|не более|не менее|около|up to|about)\s*", re.I)
_CELL_ALT = re.compile(r"^(.*?\d)\s*\(\s*[\d.,\s]+\s*\)\s*$")


def parse_cell(text: str, lang: str | None = None) -> Val | None:
    """A cell holding one value expression (number, range, «±», optional qualifier, unit, footnote mark)."""
    t = (text or "").strip()
    if not t or _EMPTY_CELL.match(t):
        return None
    q = None
    qm = _CELL_QUAL.match(t)
    if qm:
        q = {"до": "≤", "не более": "≤", "up to": "≤", "не менее": "≥", "около": "≈", "about": "≈",
             "~": "≈"}.get(qm.group(1).lower(), qm.group(1))
        t = t[qm.end():]
    flags = []
    am = _CELL_ALT.match(t)
    if am:
        t, flags = am.group(1), ["ALTERNATIVE_IN_PARENS"]
    vals = scan_values(t, lang)
    if len(vals) != 1:
        return None
    v = vals[0]
    if v.reject or v.start != 0:
        return None
    rest = t[v.span_end:].strip()
    if rest and not re.fullmatch(r"\*+|[a-zа-я]\)|\^?\d\)|\)", rest, re.I):
        return None
    if q and v.qualifier == "=":
        v.qualifier = q
    v.flags.extend(flags)
    return v


def _monotonic(seq: list[float]) -> bool:
    return len(seq) >= 3 and (all(b > a for a, b in zip(seq, seq[1:])) or all(b < a for a, b in zip(seq, seq[1:])))


def table_candidates(cells: list[dict[str, Any]], n_rows: int | None, n_cols: int | None, caption: str | None = None,
                     label: str | None = None, lang: str | None = None, header_rows: int | None = None,
                     source_symbols: dict[str, str] | None = None) -> list[tuple[Cand, int, int]]:
    """Candidates of one table (pure): (candidate, row, col) for every numeric cell under a property header (or in a
    row whose label names a property). ``source_symbols``: symbols the source defines by a phrase (header
    «Dпр, ГПа»)."""
    if not cells:
        return []
    n_rows = int(n_rows or max(int(c.get("row") or 0) + int(c.get("row_span") or 1) for c in cells))
    n_cols = int(n_cols or max(int(c.get("col") or 0) + int(c.get("col_span") or 1) for c in cells))
    if n_rows <= 0 or n_cols <= 0 or n_rows * n_cols > 40000:
        return []
    grid: list[list[str]] = [["" for _ in range(n_cols)] for _ in range(n_rows)]
    origin: list[list[tuple[int, int]]] = [[(r, c) for c in range(n_cols)] for r in range(n_rows)]
    width: dict[tuple[int, int], int] = {}
    for cell in cells:
        r0, c0 = int(cell.get("row") or 0), int(cell.get("col") or 0)
        rs, cs = max(1, int(cell.get("row_span") or 1)), max(1, int(cell.get("col_span") or 1))
        t = cell_plain(cell.get("text"))
        width[(r0, c0)] = cs
        for r in range(r0, min(n_rows, r0 + rs)):
            for c in range(c0, min(n_cols, c0 + cs)):
                grid[r][c], origin[r][c] = t, (r0, c0)
    parsed = [[parse_cell(grid[r][c], lang) if origin[r][c] == (r, c) else None for c in range(n_cols)]
              for r in range(n_rows)]

    def filled(r: int, c: int) -> bool:
        return bool(grid[r][c]) and not _EMPTY_CELL.match(grid[r][c])

    def own_texts(r: int) -> list[int]:
        return [c for c in range(n_cols) if filled(r, c) and parsed[r][c] is None and origin[r][c] == (r, c)]

    def numbering_row(r: int) -> bool:
        nums = [parsed[r][c] for c in range(n_cols) if parsed[r][c] is not None]
        if len(nums) < 3 or any(filled(r, c) and parsed[r][c] is None for c in range(n_cols)):
            return False
        seq = [x.vmin for x in nums]
        return all(float(x).is_integer() for x in seq) and all(b - a == 1 for a, b in zip(seq, seq[1:]))

    def group_row(r: int) -> bool:
        """A row of one label and no numbers («Монолит-1» across the table, «Полосчатый сильвинит» in column 0)."""
        texts = own_texts(r)
        return len(texts) == 1 and not any(parsed[r][c] for c in range(n_cols)) and (
            width.get((r, texts[0]), 1) >= max(2, n_cols - 1) or
            all(not filled(r, c) for c in range(n_cols) if origin[r][c][1] != texts[0]))

    def data_row(r: int) -> bool:
        ne = [c for c in range(n_cols) if filled(r, c)]
        nums = [c for c in ne if parsed[r][c] is not None]
        return bool(nums) and len(nums) >= 0.5 * max(1, len(ne) - 1) and not numbering_row(r)

    if header_rows is not None and 0 <= int(header_rows) < n_rows:    # 0: a block of a structured table without
        n_head = int(header_rows)                                     # a header of its own (canon gives None or ≥ 1)
    else:
        n_head = 0
        while n_head < min(4, n_rows) and not data_row(n_head):
            n_head += 1
    skip_head = {r for r in range(n_head) if r > 0 and group_row(r) and width.get((r, own_texts(r)[0]), 1) >= 2}
    first_group = next((grid[r][own_texts(r)[0]] for r in sorted(skip_head)), None)
    # an axis row: right under the header, numeric and monotonic across >= 3 columns with no label of its own, under a
    # spanning header cell — the values of the quantity named just above it («Вязкость η, Па·с» over 0,3 0,4 0,5 …);
    # the body is another quantity, so neither the axis row nor the header row naming the axis describes the body
    if 0 < n_head < n_rows - 1:
        r0 = n_head
        num_cols = [c for c in range(n_cols) if parsed[r0][c] is not None]
        nums = [parsed[r0][c].vmin for c in num_cols]
        spanning = Counter(origin[r0 - 1][c] for c in num_cols if filled(r0 - 1, c))
        if len(nums) >= 3 and not own_texts(r0) and _monotonic(nums) and spanning and max(spanning.values()) >= 3:
            skip_head |= {r0, r0 - 1}
            n_head += 1
    body = [r for r in range(n_head, n_rows) if not numbering_row(r)]
    rows = [r for r in body if any(parsed[r][c] for c in range(n_cols))]
    if not rows:
        return []
    headers = {c: parse_header(" ".join(dict.fromkeys(grid[r][c] for r in range(n_head)
                                                      if grid[r][c] and r not in skip_head)), source_symbols)
               for c in range(n_cols)}
    label_cols: list[int] = []
    for c in range(n_cols):
        fl = [r for r in rows if filled(r, c)]
        nums = [r for r in fl if parsed[r][c] is not None]
        seq = [parsed[r][c].vmin for r in nums]
        index_col = (bool(fl) and len(nums) == len(fl) and len(seq) >= 2 and
                     all(float(x).is_integer() for x in seq) and all(b - a == 1 for a, b in zip(seq, seq[1:]))
                     and headers[c].prop is None) or bool(_NUMBERING_HEADER.match(headers[c].text or "x"))
        if index_col or (fl and len(nums) <= 0.3 * len(fl)):
            label_cols.append(c)
        elif nums:
            break
    units_col = next((c for c in range(n_cols) if headers[c].text and headers[c].prop is None
                      and _UNITS_HEADER.search(headers[c].text)), None)
    cap_text = cell_plain(" ".join(x for x in (label, caption) if x))
    cap = parse_header(cap_text, source_symbols) if cap_text else HeaderInfo("")
    # a table of one quantity: the caption names it as its subject (not after «в интервале», «при», «в зависимости
    # от») and no header or row label names any; its value columns are headed by methods, statistics, materials or
    # nothing («Коэффициент длительной прочности … прямым и ускоренным методом» over «Прямой метод | Ускоренный метод»)
    cap_mentions = find_mentions(cap_text, lower_same_length(cap_text)) if cap.prop is not None else []
    one_quantity = cap.prop is not None and bool(cap_mentions) and cap_mentions[0].start <= 80 and \
        not _CAPTION_CONDITION.search(lower_same_length(cap_text[:cap_mentions[0].start])) and \
        not any(h.prop or h.ambiguous for h in headers.values()) and not any(
            parse_header(" ".join(grid[r][c] for c in label_cols if grid[r][c]), source_symbols).prop for r in rows
            if label_cols)
    ctx_low = lower_same_length(" ".join([cap_text] + [h.text for h in headers.values() if h.text]))
    t_cues, t_sites = find_scale_cues(ctx_low), find_sites(ctx_low)
    # a caption cue right before the phrase of a property («допустимой степени нагружения») is that property's only
    cap_all = find_mentions(cap_text, lower_same_length(cap_text)) if cap_text else []
    cue_owner = {t.start: m.prop.key for t in t_cues if t.start < len(cap_text)
                 for m in cap_all if 0 <= m.start - t.end <= 3 or m.start <= t.start < m.end}
    group = parse_header(first_group) if first_group else HeaderInfo("")
    # labels that name materials: a value under a label of its own that names none («Mudstone | Interlayer») is of
    # another body, so the material of the caption is not given to it
    mat_cols = {c for c in range(n_cols) if headers[c].materials}
    row_texts = {r: " ".join(dict.fromkeys(grid[r][c] for c in label_cols if grid[r][c])) for r in rows}
    mat_rows = {r for r, t in row_texts.items() if t and find_materials(t, lower_same_length(t))}
    out: list[tuple[Cand, int, int]] = []
    keys: list[tuple[str, int]] = []
    for r in body:
        if group_row(r):
            group = parse_header(grid[r][own_texts(r)[0]], source_symbols)
            continue
        if r not in rows:
            continue
        row_text = row_texts[r]
        rh = parse_header(row_text, source_symbols) if row_text else HeaderInfo("")
        row_unit = match_unit(grid[r][units_col], 0) if units_col is not None else None
        row_low = lower_same_length(" ".join(x for x in (group.text, row_text) if x))
        row_dispersion = bool(_DISPERSION.search(lower_same_length(row_text))) if row_text else False
        for c in range(n_cols):
            v = parsed[r][c]
            if v is None or c in label_cols or c == units_col:
                continue
            ch = headers[c]
            if ch.prop is not None and rh.prop is not None and ch.prop.key != rh.prop.key:
                continue
            if row_dispersion or (ch.text and _DISPERSION.search(lower_same_length(ch.text))):
                continue                    # a coefficient of variation or a standard deviation, not a value
            if ch.prop is not None:
                prop, src, hdr = ch.prop, "COL", ch
            elif rh.prop is not None:
                # «Permeability (m2) | Varies | Slr | 0.2»: a label of its own between the row label and the value
                last_label = max((x for x in label_cols if x < c), default=-1)
                if any(filled(r, x) and parsed[r][x] is None and origin[r][x] == (r, x)
                       for x in range(last_label + 1, c)):
                    continue
                # a column named by another symbol («… | A | B» of regression coefficients) or a count («Кол-во
                # испытаний») holds that quantity, not the property of the row
                if (ch.symbol and _PURE_SYMBOL.fullmatch(ch.symbol) and not ch.materials and not ch.stat
                        and not _GENERIC_VALUE_HEADER.match(ch.text)) or _COUNT_HEADER.search(ch.text or ""):
                    continue
                prop, src, hdr = rh.prop, "ROW", rh
            elif one_quantity and not rh.ambiguous and ch.unit is None and not _COUNT_HEADER.search(ch.text or "") \
                    and (_GENERIC_VALUE_HEADER.match(ch.text or "") or _METHOD_HEADER.search(ch.text or "")
                         or (ch.materials and not ch.symbol)):
                prop, src, hdr = cap.prop, "CAPTION", cap
            else:
                continue
            flags: list[str] = []
            unit, inherited = v.unit, False
            if unit is None:
                other = rh if src == "COL" else ch
                for u in (hdr.unit, row_unit, other.unit if other.prop is None else None):
                    if u is not None:
                        unit, inherited = Unit(u.raw, u.dim, u.factor, u.canon, u.start, u.end, u.complete), True
                        flags.append("UNIT_FROM_HEADER")
                        break
            if not _dims_ok(prop, unit):
                continue
            mult = hdr.multiplier_exp if inherited or unit is None else None
            if mult is None:
                lo, hi = _si_or_bare(v, unit)
                if not _in_range(prop, lo, hi, unit.dim if unit is not None else None):
                    continue
            cand = Cand(prop, v, unit, None, unit_inherited=inherited, multiplier=mult is not None)
            cand.flags.extend(flags)
            if mult is not None:
                cand.flags.append("HEADER_MULTIPLIER")
            if hdr.symbol_only:
                cand.flags.append("SYMBOL_ONLY_HEADER")
            if hdr.symbol_source:
                cand.flags.append("SYMBOL_DEFINED_IN_SOURCE")
            if src == "CAPTION":
                cand.flags.append("PROPERTY_FROM_CAPTION")
            cand.flags.extend(f for f in v.flags if f in ("ALTERNATIVE_IN_PARENS", "SHARED_POWER"))
            if prop.key in _DYNAMIC_PROPS and ("динамическ" in lower_same_length(hdr.text)
                                               or "dynamic" in hdr.text.lower()):
                cand.flags.append("DYNAMIC")
            cand.symbol = hdr.symbol
            stat = (ch.stat or rh.stat) if src in ("COL", "CAPTION") else ch.stat   # «Максимальное значение» rows
            cand.qualifier = v.qualifier if v.qualifier != "=" else {"min": "min", "max": "max", "mean": "mean"}.get(
                stat or "", "=")
            # material: the row label (rightmost one), the group label above, the column header (transposed
            # tables), a single one named in the caption
            row_m, col_m = (rh.materials, ch.materials) if src != "ROW" else (ch.materials, rh.materials)
            if src != "ROW":
                # the rock of the row: a lithology column («Тип пород», «Состав») first, the nearest one to the left
                # of the value (two-block tables), then the label; then any text cell, nearest first
                texts = [x for x in range(n_cols) if x != c and parsed[r][x] is None and filled(r, x)]
                litho = [x for x in texts if _LITHO_HEADER.search(headers[x].text or "")]
                order = sorted(litho, key=lambda x: (x > c, abs(x - c))) if litho else (
                    [] if row_m else sorted(texts, key=lambda x: (x > c, abs(x - c))))
                for x in order:
                    found_m = parse_header(grid[r][x]).materials
                    if found_m:
                        row_m = found_m[-1:]
                        break
            if prop.key in _NO_MATERIAL_PROPS:
                row_m = col_m = []
                group_mats = []
            else:
                group_mats = group.materials
            if row_m:
                _set_mat(cand, row_m[-1])
                if len({t.label for t in row_m}) > 1:
                    cand.material_raw = " | ".join(dict.fromkeys(t.raw for t in row_m))
                    cand.flags.append("MATERIAL_LAST_OF_LABEL")
            elif len({t.label for t in group_mats}) == 1:
                _set_mat(cand, group_mats[-1])
                cand.flags.append("MATERIAL_FROM_GROUP_ROW")
            elif col_m and len({t.label for t in col_m}) == 1:
                _set_mat(cand, col_m[-1])
            elif prop.key not in _NO_MATERIAL_PROPS and len({t.label for t in cap.materials}) == 1 and not (
                    (src == "ROW" and ch.text and not _GENERIC_VALUE_HEADER.match(ch.text) and mat_cols - {c}) or
                    (src != "ROW" and row_text and mat_rows - {r})):
                _set_mat(cand, cap.materials[-1])
                cand.flags.append("MATERIAL_FROM_CAPTION")
            # scale and site: the property's own header or label, then the caption and all headers, then the row
            cues = find_scale_cues(lower_same_length(hdr.text)) or [
                t for t in t_cues if cue_owner.get(t.start, prop.key) == prop.key] or find_scale_cues(row_low)
            if cues:
                cand.scale_cues = sorted({t.group for t in cues})
                cats = {t.label for t in cues}
                if len(cats) == 1:
                    cand.scale, cand.scale_basis = cats.pop(), "TABLE"
                else:
                    cand.flags.append("SCALE_CONFLICT")
            sites = find_sites(row_low) or t_sites
            if sites:
                lab = specific_site(sites)
                if lab is not None:
                    cand.site, cand.site_basis = lab, "TABLE"
                else:
                    cand.flags.append("SITE_CONFLICT")
            cand.confidence = _confidence(cand, "TABLE")
            out.append((cand, r, c))
            keys.append(("COL", c) if src != "ROW" else ("ROW", r))
    # a lone dimensionless symbol («v» over a frequency column) does not make a table a table of properties
    if out and len(set(keys)) == 1 and all("SYMBOL_ONLY_HEADER" in x.flags and x.unit is None for x, _, _ in out):
        return []
    return out


# ================================================================================================= formula candidates
def formula_candidates(params: list[dict[str, Any]], definitions: dict[tuple[str, str], dict[str, Any]],
                       block_text: dict[str, str]) -> list[tuple[Cand, dict[str, Any]]]:
    """N2's values next to formulas (``formula_parameters``) whose where-clause defines the symbol as a property of
    the vocabulary: «где E — модуль деформации, МПа» + «E = 5000 МПа»."""
    out: list[tuple[Cand, dict[str, Any]]] = []
    for p in params:
        d = definitions.get((p["formula_id"], symbol_key(p["symbol"])))
        if d is None or not d.get("definition"):
            continue
        dtext = d["definition"]
        dlow = lower_same_length(dtext)
        ms = [m for m in find_mentions(dtext, dlow) if m.start <= 12]
        if len({m.prop.key for m in ms}) != 1:
            continue
        prop = ms[0].prop
        vals = [v for v in scan_values(p["value_text"] or "") if v.reject is None]
        if len(vals) != 1:
            continue
        val = vals[0]
        unit_text = p.get("unit") or d.get("unit")
        unit = match_unit(unit_text, 0) if unit_text else val.unit
        if unit_text and unit is None:
            continue
        if not _dims_ok(prop, unit):
            continue
        lo, hi = _si_or_bare(val, unit)
        if not _in_range(prop, lo, hi, unit.dim if unit is not None else None):
            continue
        c = Cand(prop, val, unit, None, symbol=p["symbol"])
        if not p.get("unit") and d.get("unit"):
            c.flags.append("UNIT_FROM_DEFINITION")
        mats = find_materials(dtext, dlow)
        if len({t.label for t in mats}) == 1:
            _set_mat(c, mats[-1])
        ctx: dict[str, Any] = {"formula_id": p["formula_id"], "block_id": p.get("block_id")}
        bt = block_text.get(p.get("block_id") or "")
        if bt is not None:
            plain, pmap = plain_with_map(bt)
            k = plain.find(p["value_text"])
            if k >= 0 and pmap:
                last = len(pmap) - 1
                ctx["char_start"] = pmap[min(k, last)]
                ctx["char_end"] = pmap[min(k + len(p["value_text"]) - 1, last)] + 1
                low = lower_same_length(plain)
                starts = sentence_starts(plain)
                probe = Cand(prop, Val(k, k + len(p["value_text"]), p["value_text"], val.vmin, val.vmax, "="), unit,
                             None)
                _assign_scale_site(probe, low, find_scale_cues(low), find_sites(low), starts,
                                   sentence_of(starts, k),
                                   vals=[v for v in scan_values(plain) if v.reject is None])
                c.scale, c.scale_basis, c.scale_cues = probe.scale, probe.scale_basis, probe.scale_cues
                c.site, c.site_basis = probe.site, probe.site_basis
                c.flags.extend(f for f in probe.flags if f.endswith("_CONFLICT"))
        c.qualifier = val.qualifier
        c.confidence = _confidence(c, "NEAR_FORMULA")
        out.append((c, ctx))
    return out


# ================================================================================================= records
def _cand_record(c: Cand, method: str, *, block_id: str | None = None, pmap: list[int] | None = None,
                 table_id: str | None = None, row: int | None = None, col: int | None = None) -> dict[str, Any]:
    v = c.val
    if V.ANY in c.prop.dims and c.unit is not None and not c.unit.complete:
        # a rheological constant has a compound unit («МПа⁻ⁿ·с⁻¹»): a matched fragment would mislead, so none is kept
        c.unit = None
        c.flags.append("UNIT_NOT_PARSED")
        c.confidence = _confidence(c, method)
    if c.unit is not None:
        lo, hi, pm_si = value_si(v.vmin, v.vmax, v.pm, c.unit)
        unit_si = V.SI_UNIT.get(c.unit.dim)
    elif V.NONE in c.prop.dims and V.ANY not in c.prop.dims:
        lo, hi, pm_si, unit_si = v.vmin, v.vmax, v.pm, "1"
    else:
        lo = hi = pm_si = unit_si = None
    if c.multiplier:
        lo = hi = pm_si = None            # a power printed in the header: direction ambiguous, SI left empty
    rec: dict[str, Any] = {
        "property_key": c.prop.key, "property_label": c.prop.label_ru, "property_group": c.prop.group,
        "symbol": c.symbol, "material": c.material or V.UNKNOWN, "material_raw": c.material_raw,
        "material_group": c.material_group, "value_text": v.text.strip(), "value_min": v.vmin, "value_max": v.vmax,
        "value_pm": v.pm, "qualifier": c.qualifier, "unit_raw": c.unit.raw.strip() if c.unit is not None else None,
        "unit_canonical": c.unit.canon if c.unit is not None else None, "unit_si": unit_si, "value_si_min": lo,
        "value_si_max": hi, "value_si_pm": pm_si, "scale_hint": c.scale, "scale_basis": c.scale_basis,
        "scale_cues": list(c.scale_cues), "site_hint": c.site, "site_basis": c.site_basis, "method": method,
        "block_id": block_id, "table_id": table_id, "table_row": row, "table_col": col, "formula_id": None,
        "char_start": None, "char_end": None, "mention_start": None, "mention_end": None,
        "confidence": c.confidence, "flags": sorted(set(c.flags)),
    }
    if pmap:
        last = len(pmap) - 1
        rec["char_start"] = pmap[min(v.start, last)]
        rec["char_end"] = pmap[min(max(v.span_end, v.start + 1) - 1, last)] + 1
        if c.mention is not None:
            rec["mention_start"] = pmap[min(c.mention.start, last)]
            rec["mention_end"] = pmap[min(c.mention.end - 1, last)] + 1
    return rec


_LINE_END = re.compile(r"[.!?;:]\s*$")
_LINE_START = re.compile(r"^\s*[a-zа-яё0-9(«\"–—\-±≈~<>≤≥]")


def join_run(texts: list[str]) -> tuple[str, list[tuple[int, int]]]:
    """One text of consecutive line-blocks and its segments (combined start of every block); a word hyphenated across
    two lines is joined («исследова-» + «тельский»)."""
    parts: list[str] = []
    segs: list[tuple[int, int]] = []
    pos = 0
    for i, t in enumerate(texts):
        t = t or ""
        if i:
            prev = parts[-1]
            if prev.endswith("-") and len(prev) > 1 and prev[-2].isalpha() and t[:1].islower():
                parts[-1], pos = prev[:-1], pos - 1
            else:
                parts.append(" ")
                pos += 1
        segs.append((i, pos))
        parts.append(t)
        pos += len(t)
    return "".join(parts), segs


def _locate(segs: list[tuple[int, int]], lengths: list[int], ci: int) -> tuple[int, int]:
    """(block index, offset in the block) of a position of the joined text."""
    k = bisect.bisect_right([s for _, s in segs], ci) - 1
    k = max(0, k)
    return segs[k][0], max(0, min(lengths[segs[k][0]], ci - segs[k][1]))


def _text_batch(payload: tuple[list[list[tuple[str, str, str | None]]], int]
                ) -> tuple[list[dict[str, Any]], list[tuple[str, str, str]]]:
    """Worker: candidates and symbol definitions of runs of line-blocks (one block or several joined lines)."""
    runs, max_gap = payload
    out: list[dict[str, Any]] = []
    defs: list[tuple[str, str, str]] = []
    for run in runs:
        ids = [b[0] for b in run]
        texts = [b[1] or "" for b in run]
        text, segs = join_run(texts) if len(run) > 1 else (texts[0], [(0, 0)])
        found: list[tuple[str, str]] = []
        cands, _plain, pmap = text_candidates(text, run[0][2], max_gap=max_gap, definitions=found)
        defs.extend((ids[0], s, p) for s, p in found)
        if not cands:
            continue
        lengths = [len(t) for t in texts]
        last = len(pmap) - 1
        for c in cands:
            rec = _cand_record(c, "TEXT")
            v = c.val
            bi, a = _locate(segs, lengths, pmap[min(v.start, last)])
            bj, b = _locate(segs, lengths, pmap[min(max(v.span_end, v.start + 1) - 1, last)] + 1)
            rec["block_id"], rec["char_start"] = ids[bi], a
            rec["char_end"] = b if bj == bi else lengths[bi]
            if c.mention is not None:
                mi, ma = _locate(segs, lengths, pmap[min(c.mention.start, last)])
                mj, mb = _locate(segs, lengths, pmap[min(c.mention.end - 1, last)] + 1)
                if mi == bi:
                    rec["mention_start"], rec["mention_end"] = ma, (mb if mj == mi else lengths[mi])
                else:
                    rec["flags"] = sorted(set(rec["flags"]) | {"CROSS_BLOCK"})
                    rec["confidence"] = round(max(0.05, rec["confidence"] - 0.05), 2)
            if len(run) > 1:
                rec["flags"] = sorted(set(rec["flags"]) | {"JOINED_LINES"})
            out.append(rec)
    return out, defs


# ================================================================================================= build helpers
def _rows(obj: Any) -> list[dict[str, Any]]:
    if obj is None:
        return []
    if hasattr(obj, "to_pylist"):
        return obj.to_pylist()
    if hasattr(obj, "to_arrow_table"):
        return obj.to_arrow_table().to_pylist()
    if isinstance(obj, str):
        import pyarrow.parquet as pq

        return pq.read_table(obj).to_pylist()
    return [dict(r) for r in obj]


def _from_con(con: Any, name: str, cols: str) -> list[dict[str, Any]] | None:
    """An earlier NAV dataset registered in the connection as ``nav_<name>`` (or ``<name>``), when not passed."""
    for view in (f"nav_{name}", name):
        try:
            cur = con.execute(f'SELECT {cols} FROM "{view}"')
        except Exception:  # noqa: BLE001 - not registered
            continue
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]
    return None


def _has(con: Any, sql: str) -> bool:
    try:
        con.execute(sql)
        return True
    except Exception:  # noqa: BLE001
        return False


_PREFILTER_SQL = "|".join(sorted({f for p in V.PROPERTIES for f in p.prefilter if re.fullmatch(r"[a-zа-я -]+", f)}))


def _load_blocks(con: Any, source_ids: list[str] | None) -> list[tuple]:
    """Primary text blocks of the pages where a property word occurs (all of them: a value may sit on the next line
    of a layout that keeps one block per line)."""
    types = ", ".join(f"'{t}'" for t in TEXT_BLOCK_TYPES)
    where, params = "", []
    if source_ids:
        where = f"AND source_id IN ({', '.join('?' for _ in source_ids)})"
        params = list(source_ids)
    sql = f"""
        WITH hit AS (
            SELECT DISTINCT page_id FROM canonical.blocks
            WHERE is_primary_layer AND block_type IN ({types}) AND normalized_text IS NOT NULL
              AND regexp_matches(replace(lower(normalized_text), 'ё', 'е'), ?) {where})
        SELECT b.object_id, b.source_id, b.page_id, coalesce(p.page_index, 0), coalesce(b.reading_order, 0),
               b.bbox_y0, b.language, b.normalized_text, b.block_type, b.bbox_x0, b.bbox_x1, b.bbox_y1, b.bbox_space
        FROM canonical.blocks b JOIN hit ON hit.page_id = b.page_id
             LEFT JOIN canonical.pages p ON p.page_id = b.page_id
        WHERE b.is_primary_layer AND b.block_type IN ({types}) AND b.normalized_text IS NOT NULL
        ORDER BY b.source_id, 4, b.page_id, 5, b.object_id"""
    rows = con.execute(sql, [_PREFILTER_SQL, *params]).fetchall()
    figs: dict[str, list[tuple[float, float, float, float]]] = defaultdict(list)
    if _has(con, "SELECT 1 FROM canonical.figures LIMIT 0"):
        for pid, x0, y0, x1, y1 in con.execute(
                "SELECT page_id, bbox_x0, bbox_y0, bbox_x1, bbox_y1 FROM canonical.figures "
                "WHERE bbox_space = 'PAGE_PT_TL' AND bbox_x0 IS NOT NULL").fetchall():
            figs[pid].append((x0, y0, x1, y1))
    return [r for r in rows if not _inside_figure(r, figs.get(r[2], ()))]


def _inside_figure(b: tuple, figs: Iterable[tuple[float, float, float, float]], share: float = 0.8) -> bool:
    """A text block lying inside a figure (axis ticks, legends): its numbers are not parameter statements."""
    x0, y0, x1, y1, space = b[9], b[5], b[10], b[11], b[12]
    if space != "PAGE_PT_TL" or None in (x0, y0, x1, y1):
        return False
    area = max(1e-6, (x1 - x0) * (y1 - y0))
    for fx0, fy0, fx1, fy1 in figs:
        ov = max(0.0, min(x1, fx1) - max(x0, fx0)) * max(0.0, min(y1, fy1) - max(y0, fy0))
        if ov / area >= share:
            return True
    return False


def make_runs(blocks: list[tuple], *, max_blocks: int = 12, max_chars: int = 3000) -> list[list[tuple]]:
    """Consecutive TEXT/LIST_ITEM blocks of a page that continue one another (the previous one does not end a
    sentence, the next starts in lower case or with a number) form one run; every other block is a run of its own.
    ``blocks`` rows: (block_id, source_id, page_id, page_index, reading_order, y0, language, text, block_type)."""
    runs: list[list[tuple]] = []
    cur: list[tuple] = []
    for b in blocks:
        if cur:
            p = cur[-1]
            joinable = (b[2] == p[2] and int(b[4]) == int(p[4]) + 1 and b[8] in ("TEXT", "LIST_ITEM")
                        and p[8] in ("TEXT", "LIST_ITEM") and not _LINE_END.search(p[7] or "")
                        and _LINE_START.match(b[7] or "") and len(cur) < max_blocks
                        and sum(len(x[7] or "") for x in cur) + len(b[7] or "") <= max_chars)
            if joinable:
                cur.append(b)
                continue
            runs.append(cur)
        cur = [b]
    if cur:
        runs.append(cur)
    return runs


def _source_meta(con: Any) -> dict[str, dict[str, Any]]:
    try:
        rows = con.execute("SELECT source_id, site_scope, source_class_raw FROM canonical.sources").fetchall()
    except Exception:  # noqa: BLE001 - a partial canon (tests)
        return {}
    return {r[0]: {"site_scope": list(r[1] or []), "source_class": r[2]} for r in rows}


def _source_langs(con: Any) -> dict[str, str]:
    try:
        rows = con.execute("SELECT l.source_id, w.languages FROM canonical.source_work_links l JOIN canonical.works w "
                           "ON w.work_id = l.work_id WHERE l.is_primary").fetchall()
    except Exception:  # noqa: BLE001
        return {}
    return {sid: langs[0] for sid, langs in rows if langs}


def _by_ids(con: Any, sql: str, ids: list[str]) -> list[tuple]:
    out: list[tuple] = []
    ids = sorted({i for i in ids if i})
    for i in range(0, len(ids), 5000):
        chunk = ids[i:i + 5000]
        out.extend(con.execute(sql.format(ph=", ".join("?" for _ in chunk)), chunk).fetchall())
    return out


class _SectionIndex:
    """N1 section of a position: among the leaf sections of a page, the one whose heading precedes the position."""

    def __init__(self, section_pages: list[dict[str, Any]], sections: list[dict[str, Any]],
                 heading_pos: dict[str, tuple[int, float | None]]):
        by_page: dict[str, set[str]] = defaultdict(set)
        for r in section_pages:
            if r.get("section_id") and r.get("page_id"):
                by_page[r["page_id"]].add(r["section_id"])
        info = {r["section_id"]: r for r in sections if r.get("section_id")}
        self.by_page: dict[str, list[tuple[tuple, str]]] = {}
        for pid, sids in by_page.items():
            keyed = []
            for sid in sids:
                s = info.get(sid, {})
                hb = s.get("heading_block_id")
                hp = heading_pos.get(hb) if hb else None
                if hp is not None and hb.startswith(pid + ":"):
                    keyed.append(((1, hp[0], hp[1] if hp[1] is not None else 0.0), sid))
                else:
                    keyed.append(((0, int(s.get("page_start_index") or 0), 0.0), sid))
            keyed.sort()
            self.by_page[pid] = keyed
        self.enabled = bool(self.by_page)

    def of(self, page_id: str | None, ro: int | None = None, y0: float | None = None) -> str | None:
        lst = self.by_page.get(page_id or "")
        if not lst:
            return None
        best = lst[0][1]
        for (flag, a, b), sid in lst:
            if flag == 0 or (ro is not None and ro >= a) or (ro is None and y0 is not None and y0 >= b):
                best = sid
        return best


_NORMATIVE_CLASSES = frozenset({"normative_document", "methodical_guidance"})
_DESCRIBES_DEPOSIT = frozenset({"depth", "depth_unspecified", "seam_thickness", "layer_thickness",
                                "thickness_unspecified"})
_OTHER_MEANING = "__other__"                          # a symbol definition naming no property of the vocabulary


def source_symbol_table(defs: dict[str, dict[str, Counter]]) -> dict[str, dict[str, str]]:
    """Per source: symbol (loose key) → property, kept only when every definition of the symbol in that source names
    the same property and the symbol has two characters or more (a symbol is never shared across sources)."""
    out: dict[str, dict[str, str]] = {}
    for sid, table in defs.items():
        keep = {}
        for k, c in table.items():
            if not k or len(c) != 1 or _OTHER_MEANING in c:
                continue
            if len(k) < 2:
                continue                        # «a», «P», «E»: a one-letter symbol means too many things in a book
            keep[k] = next(iter(c))
        if keep:
            out[sid] = keep
    return out


_STRUCTURE_COLS = ("table_id", "page_id", "n_cols", "blocks", "structure_ok", "covers_region", "bbox_x0", "bbox_y0",
                   "bbox_x1", "bbox_y1", "bbox_space")
_CELL_COLS = ("table_id", "row", "col", "row_span", "col_span", "block", "row_role", "text_clean", "flags",
              "value_type", "is_header")
_CELL_TEXT_SKIP_ROLES = frozenset({"STAT_DISPERSION", "STAT_COUNT", "REPEATED_HEADER", "NUMBERING"})
# flags of a structured cell carried to its candidate
_CELL_FLAGS_KEPT = frozenset({"DECIMAL_POINT_SUSPECT", "POWER_SUPERSCRIPT_LOST"})


def _select(obj: Any, con: Any, name: str, cols: tuple[str, ...]) -> list[dict[str, Any]] | None:
    """Columns of a dataset passed to the builder (Arrow table, rows) or registered in the connection."""
    if obj is None:
        return _from_con(con, name, ", ".join(cols))
    if hasattr(obj, "select") and hasattr(obj, "schema"):
        return obj.select([c for c in cols if c in obj.schema.names]).to_pylist()
    return [{c: r.get(c) for c in cols} for r in _rows(obj)]


def structured_tables(con: Any, table_structure: Any = None, table_cells: Any = None
                      ) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]] | None:
    """The part ``tables`` of the same build (passed or registered as ``nav_table_structure`` /
    ``nav_table_cells``): structure rows by table id and their cells; None when it is absent."""
    st = _select(table_structure, con, "table_structure", _STRUCTURE_COLS)
    if not st:
        return None
    cells = _select(table_cells, con, "table_cells", _CELL_COLS) or []
    by_table: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in cells:
        by_table[c["table_id"]].append(c)
    return {r["table_id"]: r for r in st}, by_table


def _covered_regions(structure: dict[str, dict[str, Any]]) -> dict[str, list[tuple[float, float, float, float]]]:
    """Page → boxes of the tables whose grid stands for their region (``covers_region``)."""
    out: dict[str, list[tuple[float, float, float, float]]] = defaultdict(list)
    for r in structure.values():
        if r.get("covers_region") and r.get("bbox_space") == "PAGE_PT_TL" and None not in (
                r.get("bbox_x0"), r.get("bbox_y0"), r.get("bbox_x1"), r.get("bbox_y1")):
            out[r["page_id"]].append((r["bbox_x0"], r["bbox_y0"], r["bbox_x1"], r["bbox_y1"]))
    return out


def structured_table_candidates(cells: list[dict[str, Any]], structure: dict[str, Any], caption: str | None,
                                label: str | None, lang: str | None, source_symbols: dict[str, str] | None
                                ) -> list[tuple[Cand, int, int]]:
    """Candidates of one structured table: :func:`table_candidates` over each block of its grid (cleaned texts,
    detected header rows; dispersion and count rows, repeated headers and notes left out), located in the canonical
    grid; a cell's suspect flags (lost decimal point, lost superscript) go to its candidate."""
    from vkm_corpus.navigation.tables import param_blocks  # noqa: PLC0415 - the tables part imports this module

    out: list[tuple[Cand, int, int]] = []
    for sub, n_rows, n_cols, head, row_map, flags in param_blocks(cells, structure):
        for c, r, col in table_candidates(sub, n_rows, n_cols, caption, label, lang, head, source_symbols):
            row = row_map[r]
            kept = [f for f in flags.get((row, col), ()) if f in _CELL_FLAGS_KEPT]
            if kept:
                c.flags.extend(kept)
                c.confidence = _confidence(c, "TABLE")
            out.append((c, row, col))
    return out


def cell_text_candidates(cells: list[dict[str, Any]], lang: str | None, max_gap: int = 150
                         ) -> list[tuple[Cand, dict[str, Any], list[int]]]:
    """Values written inside the text of a body cell of a structured table («… на глубине более 300 м»): the text
    rules over the cell alone, since the text lines of the region are not read — the cell is the whole context, so
    no neighbouring column leaks in. Returns (candidate, cell, map of the plain cell text to ``text_clean``)."""
    out: list[tuple[Cand, dict[str, Any], list[int]]] = []
    for c in cells:
        t = c.get("text_clean") or ""
        if c.get("value_type") != "TEXT" or c.get("is_header") or c.get("row_role") in _CELL_TEXT_SKIP_ROLES \
                or len(t) < 8 or not re.search(r"\d", t) or not re.search(r"[^\W\d_]{3}", t):
            continue
        cands, _plain, pmap = text_candidates(t, lang, max_gap=max_gap)
        out.extend((cand, c, pmap) for cand in cands)
    return out


def _norm_value_text(t: str | None) -> str:
    return re.sub(r"\s+", "", t or "").replace(",", ".").replace("−", "-").replace("–", "-")


def _dedupe(recs: list[dict[str, Any]], counters: Counter) -> list[dict[str, Any]]:
    """One candidate per (page, property, value, unit): TABLE before TEXT before NEAR_FORMULA; a candidate of another
    method with a different known material is kept; the kept one records the other methods in ``flags``."""
    rank = {"TABLE": 0, "TEXT": 1, "NEAR_FORMULA": 2}
    recs.sort(key=lambda r: (r["source_id"] or "", r["page_index"], rank[r["method"]], r["block_id"] or "",
                             r["table_id"] or "", r["table_row"] or 0, r["table_col"] or 0, r["char_start"] or 0))
    first: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    kept: list[dict[str, Any]] = []
    for r in recs:
        key = (r["page_id"], r["property_key"], _norm_value_text(r["value_text"]), r["unit_canonical"])
        dup = next((p for p in first[key] if p["method"] != r["method"] and
                    (p["material"] == r["material"] or V.UNKNOWN in (p["material"], r["material"]))), None)
        if dup is not None:
            dup["flags"] = sorted(set(dup["flags"]) | {f"ALSO_{r['method']}"})
            counters["dedup_cross_method"] += 1
            continue
        first[key].append(r)
        kept.append(r)
    return kept


# ================================================================================================= build
def build(con: Any, *, section_pages: Any = None, sections: Any = None, formula_parameters: Any = None,
          formula_symbols: Any = None, table_structure: Any = None, table_cells: Any = None,
          stats: dict[str, Any] | None = None, source_ids: Iterable[str] | None = None,
          **options: Any) -> dict[str, Any]:
    """Parameter datasets of the snapshot behind ``con`` (DuckDB with the ``canonical`` schema). With the part
    ``tables`` of the same build (``table_structure`` + ``table_cells``) a table is read from its structured grid,
    and the text lines inside a table whose grid stands for its region are not read as text (a table flattened into
    one line per row takes the unit of a header phrase for every number)."""
    import pyarrow as pa

    opts = {**DEFAULTS, **{k: v for k, v in options.items() if k in DEFAULTS}}
    only = sorted(set(source_ids)) if source_ids else None
    counters: Counter = Counter()
    meta = _source_meta(con)
    langs = _source_langs(con)
    structured = structured_tables(con, table_structure, table_cells)
    # ---- text blocks (parallel): runs of line-blocks, candidates and symbol definitions
    blocks = _load_blocks(con, only)
    if structured is not None:
        regions = _covered_regions(structured[0])
        kept = [b for b in blocks if b[8] not in ("TEXT", "LIST_ITEM")
                or not _inside_figure(b, regions.get(b[2], ()), share=0.5)]
        counters["text_blocks_in_tables_skipped"] = len(blocks) - len(kept)
        blocks = kept
    counters["text_blocks_scanned"] = len(blocks)
    binfo = {b[0]: b for b in blocks}
    runs = [[(b[0], b[7], b[6] or langs.get(b[1])) for b in run] for run in make_runs(blocks)]
    counters["text_runs"] = len(runs)
    counters["text_runs_joined"] = sum(1 for r in runs if len(r) > 1)
    step = max(1, int(opts["batch"]))
    payloads = [(runs[i:i + step], int(opts["max_gap"])) for i in range(0, len(runs), step)]
    workers = int(opts["workers"] or min(os.cpu_count() or 1, 16))
    if workers <= 1 or len(payloads) <= 1:
        results = [_text_batch(p) for p in payloads]
    else:
        ctx = mp.get_context("fork" if "fork" in mp.get_all_start_methods() else "spawn")
        with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as ex:
            results = list(ex.map(_text_batch, payloads, chunksize=1))
    recs: list[dict[str, Any]] = []
    defs: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    for batch, found in results:
        for r in batch:
            b = binfo[r["block_id"]]
            r.update({"source_id": b[1], "page_id": b[2], "page_index": int(b[3]), "_ro": int(b[4]), "_y0": None,
                      "language": b[6] or langs.get(b[1])})
            recs.append(r)
        for block_id, sym, prop in found:
            defs[binfo[block_id][1]][_hint_key(sym)][prop] += 1
    counters["text_candidates"] = len(recs)
    fparams = _rows(formula_parameters) if formula_parameters is not None else (
        _from_con(con, "formula_parameters", "*") or [])
    if only:
        fparams = [p for p in fparams if p.get("source_id") in only]
    fsyms = _rows(formula_symbols) if formula_symbols is not None else (
        _from_con(con, "formula_symbols", "formula_id, source_id, symbol, symbol_key, definition, unit") or [])
    for s in fsyms:                          # where-clauses of formulas define symbols of their source too
        if s.get("definition") and s.get("source_id") and (not only or s["source_id"] in only):
            dtext = s["definition"]
            ms = [m for m in find_mentions(dtext, lower_same_length(dtext)) if m.start <= 12]
            # a definition naming no property of the vocabulary («половина высоты выработки») still counts: the
            # symbol then means two things in that source and is not used
            prop_key = ms[0].prop.key if len({m.prop.key for m in ms}) == 1 else _OTHER_MEANING
            defs[s["source_id"]][_hint_key(s["symbol"])][prop_key] += 1
    source_symbols = source_symbol_table(defs)
    counters["source_symbols"] = sum(len(v) for v in source_symbols.values())
    # ---- tables
    trows = []
    if _has(con, "SELECT 1 FROM canonical.tables LIMIT 0"):
        from vkm_corpus.contracts.primary_layer import primary_clause

        trows = con.execute(f"""
            SELECT t.object_id, t.source_id, t.page_id, coalesce(p.page_index, 0), t.table_label, t.caption, t.n_rows,
                   t.n_cols, t.header_rows, t.cells, t.bbox_y0
            FROM canonical.tables t LEFT JOIN canonical.pages p ON p.page_id = t.page_id
            WHERE {primary_clause(con, 'tables', 't')}
            ORDER BY t.source_id, t.page_id, t.object_id""").fetchall()
    for tid, sid, pid, pidx, label, caption, nr, nc, hr, cells, y0 in trows:
        if only and sid not in only:
            continue
        counters["tables_scanned"] += 1
        st = structured[0].get(tid) if structured is not None else None
        try:
            if st is not None and st.get("structure_ok"):
                got = structured_table_candidates(structured[1].get(tid, []), st, caption, label, langs.get(sid),
                                                  source_symbols.get(sid))
                counters["tables_structured"] += 1
            else:
                got = table_candidates(list(cells or []), nr, nc, caption, label, langs.get(sid), hr,
                                       source_symbols.get(sid))
        except Exception:  # noqa: BLE001 - one malformed table must not stop the build
            counters["tables_failed"] += 1
            continue
        counters["tables_with_candidates"] += bool(got)
        for c, r, col in got:
            rec = _cand_record(c, "TABLE", table_id=tid, row=r, col=col)
            rec.update({"source_id": sid, "page_id": pid, "page_index": int(pidx), "_ro": None, "_y0": y0,
                        "language": langs.get(sid)})
            recs.append(rec)
        if st is not None and st.get("structure_ok") and st.get("covers_region"):
            # the text lines of this region are not read: values inside the text of its cells, cell by cell
            for c, cell, pmap in cell_text_candidates(structured[1].get(tid, []), langs.get(sid), int(opts["max_gap"])):
                c.flags.append("CELL_TEXT")
                rec = _cand_record(c, "TABLE", table_id=tid, row=int(cell["row"]), col=int(cell["col"]), pmap=pmap)
                rec.update({"source_id": sid, "page_id": pid, "page_index": int(pidx), "_ro": None, "_y0": y0,
                            "language": langs.get(sid)})
                recs.append(rec)
                counters["cell_text_candidates"] += 1
    counters["table_candidates"] = sum(1 for r in recs if r["method"] == "TABLE")
    # ---- values next to formulas (N2's formula_parameters + formula_symbols)
    if fparams and fsyms:
        want = {p["formula_id"] for p in fparams}
        fdefs: dict[tuple[str, str], dict[str, Any]] = {}
        for s in fsyms:
            if s.get("definition") and s["formula_id"] in want:
                fdefs.setdefault((s["formula_id"], s.get("symbol_key") or symbol_key(s["symbol"])), s)
        btext = {r[0]: r for r in _by_ids(con, """
            SELECT b.object_id, b.source_id, b.page_id, coalesce(p.page_index, 0), coalesce(b.reading_order, 0),
                   b.normalized_text
            FROM canonical.blocks b LEFT JOIN canonical.pages p ON p.page_id = b.page_id
            WHERE b.object_id IN ({ph})""", [p.get("block_id") for p in fparams])}
        fpage = {r[0]: r for r in _by_ids(con, """
            SELECT f.object_id, f.source_id, f.page_id, coalesce(p.page_index, 0)
            FROM canonical.formulas f LEFT JOIN canonical.pages p ON p.page_id = f.page_id
            WHERE f.object_id IN ({ph})""", sorted(want))}
        for c, ctx in formula_candidates(fparams, fdefs, {k: v[5] for k, v in btext.items()}):
            rec = _cand_record(c, "NEAR_FORMULA", block_id=ctx.get("block_id"))
            rec.update({"formula_id": ctx["formula_id"], "char_start": ctx.get("char_start"),
                        "char_end": ctx.get("char_end")})
            b = btext.get(ctx.get("block_id") or "")
            f = fpage.get(ctx["formula_id"])
            src = (b or f or (None, None))[1]
            rec.update({"source_id": src, "page_id": (b or f)[2] if (b or f) else None,
                        "page_index": int((b or f)[3]) if (b or f) else 0, "_ro": int(b[4]) if b else None,
                        "_y0": None, "language": langs.get(src or "")})
            recs.append(rec)
    counters["formula_candidates"] = sum(1 for r in recs if r["method"] == "NEAR_FORMULA")
    recs = _dedupe(recs, counters)
    # ---- sections, source metadata, ids
    sp = _rows(section_pages) if section_pages is not None else (_from_con(con, "section_pages",
                                                                             "section_id, page_id") or [])
    se = _rows(sections) if sections is not None else (
        _from_con(con, "sections", "section_id, heading_block_id, page_start_index") or [])
    heading_pos = {r[0]: (int(r[1]), r[2]) for r in _by_ids(con, """
        SELECT object_id, coalesce(reading_order, 0), bbox_y0 FROM canonical.blocks WHERE object_id IN ({ph})""",
        [s.get("heading_block_id") for s in se])} if se else {}
    secs = _SectionIndex(sp, se, heading_pos)
    for r in recs:
        ro, y0 = r.pop("_ro", None), r.pop("_y0", None)
        r["section_id"] = secs.of(r["page_id"], ro, y0) if secs.enabled else None
        m = meta.get(r["source_id"] or "", {})
        r["source_site_scope"] = m.get("site_scope", [])
        r["source_class"] = m.get("source_class")
        # a value of a normative document is normative, but not a depth or a thickness it describes the deposit with
        if r["scale_hint"] == V.UNKNOWN and m.get("source_class") in _NORMATIVE_CLASSES \
                and "SCALE_CONFLICT" not in r["flags"] and r["property_key"] not in _DESCRIBES_DEPOSIT:
            r["scale_hint"], r["scale_basis"] = V.NORMATIVE, "SOURCE_CLASS"
        anchor = r["block_id"] or r["table_id"] or r["formula_id"] or ""
        loc = f"{r['table_row']}:{r['table_col']}" if r["table_id"] else str(r["char_start"])
        if r["table_id"] and r["char_start"] is not None:          # a value inside the text of a cell
            loc += f":{r['char_start']}"
        r["candidate_id"] = nav_ids.parameter_candidate_id(anchor, loc, r["property_key"], r["value_text"])
        r["review_status"], r["rule_version"] = REVIEW_STATUS, RULE_VERSION
    uniq: dict[str, dict[str, Any]] = {}
    for r in recs:
        uniq.setdefault(r["candidate_id"], r)
    recs = sorted(uniq.values(), key=lambda r: (r["source_id"] or "", r["page_index"], r["candidate_id"]))
    counters["candidates"] = len(recs)
    if stats is not None:
        stats.update({"counters": dict(sorted(counters.items())), "rule_version": RULE_VERSION,
                      "table_input": "table_cells" if structured is not None else "canonical.tables"})
    return {
        "parameter_candidates": pa.Table.from_pylist([{k: r.get(k) for k in CANDIDATE_COLUMNS} for r in recs],
                                                     schema=PARAMETER_CANDIDATES_SCHEMA()),
        "parameter_summary": pa.Table.from_pylist(summary_rows(recs), schema=PARAMETER_SUMMARY_SCHEMA()),
    }


# ================================================================================================= summary
def summary_rows(recs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for r in recs:
        groups[(r["property_key"], r["material"], r["scale_hint"], r["unit_si"])].append(r)
    out = []
    for (pk, mat, scale, usi), rs in sorted(groups.items(), key=lambda kv: tuple(str(x) for x in kv[0])):
        lows = [r["value_si_min"] for r in rs if r["value_si_min"] is not None]
        highs = [r["value_si_max"] for r in rs if r["value_si_max"] is not None]
        out.append({
            "property_key": pk, "property_label": V.PROPERTY_BY_KEY[pk].label_ru, "material": mat,
            "scale_hint": scale, "unit_si": usi, "n_candidates": len(rs),
            "n_sources": len({r["source_id"] for r in rs}), "n_pages": len({r["page_id"] for r in rs}),
            "n_without_si": sum(1 for r in rs if r["value_si_min"] is None),
            "value_si_min": min(lows) if lows else None, "value_si_max": max(highs) if highs else None,
            "site_hints": sorted({r["site_hint"] for r in rs}), "methods": sorted({r["method"] for r in rs}),
            "source_ids": sorted({r["source_id"] for r in rs if r["source_id"]}), "note": SUMMARY_NOTE,
            "review_status": REVIEW_STATUS, "rule_version": RULE_VERSION,
        })
    return out


# ================================================================================================= schemas
CANDIDATE_COLUMNS = (
    "candidate_id", "property_key", "property_label", "property_group", "symbol", "material", "material_raw",
    "material_group", "value_text", "value_min", "value_max", "value_pm", "qualifier", "unit_raw", "unit_canonical",
    "unit_si", "value_si_min", "value_si_max", "value_si_pm", "scale_hint", "scale_basis", "scale_cues", "site_hint",
    "site_basis", "source_site_scope", "source_class", "method", "source_id", "page_id", "page_index", "section_id",
    "block_id", "table_id", "table_row", "table_col", "formula_id", "char_start", "char_end", "mention_start",
    "mention_end", "language", "confidence", "flags", "review_status", "rule_version",
)
SUMMARY_COLUMNS = ("property_key", "property_label", "material", "scale_hint", "unit_si", "n_candidates", "n_sources",
                   "n_pages", "n_without_si", "value_si_min", "value_si_max", "site_hints", "methods", "source_ids",
                   "note", "review_status", "rule_version")


def PARAMETER_CANDIDATES_SCHEMA():  # noqa: N802 - a schema constant built lazily (pyarrow is optional at import)
    import pyarrow as pa

    f, i, lst = pa.float64(), pa.int32(), pa.list_(pa.string())
    types = {"value_min": f, "value_max": f, "value_pm": f, "value_si_min": f, "value_si_max": f, "value_si_pm": f,
             "confidence": f, "page_index": i, "table_row": i, "table_col": i, "char_start": i, "char_end": i,
             "mention_start": i, "mention_end": i, "scale_cues": lst, "source_site_scope": lst, "flags": lst}
    return pa.schema([(c, types.get(c, pa.string())) for c in CANDIDATE_COLUMNS])


def PARAMETER_SUMMARY_SCHEMA():  # noqa: N802
    import pyarrow as pa

    f, i, lst = pa.float64(), pa.int32(), pa.list_(pa.string())
    types = {"n_candidates": i, "n_sources": i, "n_pages": i, "n_without_si": i, "value_si_min": f,
             "value_si_max": f, "site_hints": lst, "methods": lst, "source_ids": lst}
    return pa.schema([(c, types.get(c, pa.string())) for c in SUMMARY_COLUMNS])


def summarize(tables: dict[str, Any]) -> dict[str, Any]:
    """Counts for the public receipt (ids and numbers only, no corpus text)."""
    rows = tables["parameter_candidates"].to_pylist()

    def by(key: str) -> dict[str, int]:
        return dict(sorted(Counter(str(r[key]) for r in rows).items(), key=lambda kv: (-kv[1], kv[0])))

    pm = Counter((r["property_key"], r["material"]) for r in rows)
    return {
        "candidates": len(rows), "sources": len({r["source_id"] for r in rows}),
        "pages": len({r["page_id"] for r in rows}), "by_method": by("method"), "by_property": by("property_key"),
        "by_material": by("material"), "by_scale_hint": by("scale_hint"), "by_site_hint": by("site_hint"),
        "by_qualifier": by("qualifier"), "with_unit": sum(1 for r in rows if r["unit_raw"]),
        "with_si": sum(1 for r in rows if r["value_si_min"] is not None),
        "with_section": sum(1 for r in rows if r["section_id"]), "with_symbol": sum(1 for r in rows if r["symbol"]),
        "property_x_material": {f"{p} | {m}": n for (p, m), n in sorted(pm.items(), key=lambda kv: (-kv[1], kv[0]))},
        "summary_rows": tables["parameter_summary"].num_rows,
    }
