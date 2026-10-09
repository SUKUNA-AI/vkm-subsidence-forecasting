"""Offline verifier of extracted records against the page text the producer was given.

A record passes when
- its ``page_id`` is one of the packet pages (context pages do not count);
- its quote is a substring of the page text after normalisation of spaces, line-break hyphenation, dashes, quotes,
  «ё» and case (an ellipsis splits the quote into parts that must all occur, in order);
- every number of ``value_as_printed`` occurs on the page in its printed form (decimal comma or point, thousands
  spaces, powers of ten printed as separate numbers);
- ``value_min`` / ``value_max`` are numbers printed in ``value_as_printed`` (a printed power of ten applied);
- ``unit_as_printed`` occurs on the page.

Failures are reasons, not deletions: the caller keeps rejected records with their reasons.
"""
from __future__ import annotations

import math
import re
import unicodedata

_DASHES = "‐‑‒–—―−⁃﹣－˗"
_DQUOTES = "«»“”„‟″〝〞＂"
_SQUOTES = "‘’‚‛′＇`"
_TRANS = {ord(c): "-" for c in _DASHES}
_TRANS.update({ord(c): '"' for c in _DQUOTES})
_TRANS.update({ord(c): "'" for c in _SQUOTES})
_TRANS.update({ord("ё"): "е", ord("Ё"): "Е", 0x00AD: None, 0x200B: None, 0xFEFF: None})
_WS = re.compile(r"\s+")
_HYPHEN_BREAK = re.compile(r"-[ \t]*\n\s*")
# a printed number: digits, optional thousands groups separated by a (thin) space, optional decimal part
_NUM = re.compile(r"(?<![\d.,])\d+(?:[    ]\d{3})*(?:[.,]\d+)?(?![\d])")
_DIGITS = re.compile(r"\d+(?:[.,]\d+)?")
_POW = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:[·×x*∙⋅]|\\cdot|\\times)\s*10\s*(?:\^|\*\*)?\s*\{?\s*(-?)\s*(\d+)\s*\}?")
_EXP = re.compile(r"(\d+(?:[.,]\d+)?)\s*[eE]\s*([-+]?)(\d+)")
# «5.1 10-2»: a power of ten printed after a space, without a multiplication sign (signed exponent only)
_POW_SPACE = re.compile(r"(\d+(?:[.,]\d+)?)\s+10\s*\^?\s*\{?\s*(-)\s*(\d+)")
_NEG = re.compile(r"(?<![\d.,])-\s?(\d+(?:[.,]\d+)?)")         # a minus sign not preceded by a number
_DIMENSIONLESS = {"", "-", "—", "д.ед.", "д. ед.", "доли ед.", "безразм.", "1", "отн. ед.", "n/a", "none"}


def base_norm(text: str) -> str:
    """NFKC, one dash, one kind of quotes, «ё» → «е», no soft hyphens. Keeps whitespace and case."""
    return unicodedata.normalize("NFKC", text or "").translate(_TRANS)


def _squash(text: str, join_hyphens: bool) -> str:
    t = base_norm(text)
    t = _HYPHEN_BREAK.sub("" if join_hyphens else "-", t)
    return _WS.sub("", t).casefold()


class PageText:
    """Normalised forms of one page as given to the producer (page text plus its tables)."""

    def __init__(self, page_id: str, text: str):
        self.page_id = page_id
        self.raw = text or ""
        self.joined = _squash(self.raw, True)
        self.kept = _squash(self.raw, False)
        spaced = _WS.sub(" ", base_norm(self.raw))
        self.numbers = {_num_key(m.group(0)) for m in _NUM.finditer(spaced)}
        self.numbers |= {_num_key(m.group(0)) for m in _DIGITS.finditer(spaced)}

    def has_fragment(self, fragment: str) -> bool:
        if not fragment.strip():
            return True
        q1, q2 = _squash(fragment, True), _squash(fragment, False)
        return q1 in self.joined or q2 in self.kept or q1 in self.kept or q2 in self.joined


def _num_key(token: str) -> str:
    t = re.sub(r"[    ]", "", token).replace(",", ".")
    return t


def printed_numbers(value_as_printed: str) -> list[str]:
    """Number tokens of a printed value, in their printed form (thousands spaces kept as in print)."""
    spaced = _WS.sub(" ", base_norm(value_as_printed or "")).replace("...", " ... ")     # «8,5…9,0»
    return [m.group(0) for m in _NUM.finditer(spaced)]


def candidate_values(value_as_printed: str) -> list[float]:
    """Every number the printed value may stand for: each printed number, and a·10^b where a power of ten is
    printed (``5·10-4``, ``5×10⁻⁴``, ``5\\cdot 10^{-4}``, ``5e-4``, ``5.1 10-2``); a number after a minus sign that
    does not follow another number (``гор. –143``, not ``100–143``) also stands for its negative."""
    s = _WS.sub(" ", base_norm(value_as_printed or ""))
    out = [float(_num_key(t)) for t in printed_numbers(s)]

    def negative(at: int) -> bool:
        return s[:at].rstrip().endswith("-") and not re.search(r"\d\s*-\s*$", s[:at])

    out += [-float(_num_key(m.group(1))) for m in _NEG.finditer(s) if not re.search(r"\d\s*$", s[:m.start()])]
    for rx in (_POW, _EXP, _POW_SPACE):
        for m in rx.finditer(s):
            mant = float(m.group(1).replace(",", "."))
            exp = int(m.group(3)) * (-1 if m.group(2) == "-" else 1)
            out.append(mant * 10.0 ** exp)
            if rx is _POW:
                out.append(10.0 ** exp)            # «10-4» alone may be printed with an implicit mantissa 1
            if negative(m.start()):
                out.append(-mant * 10.0 ** exp)
    return out


def _close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-15)


def check_record(rec: dict, pages: dict[str, PageText]) -> list[str]:
    """Reasons why ``rec`` fails; an empty list means it passes."""
    reasons: list[str] = []
    page = pages.get(rec.get("page_id") or "")
    if page is None:
        return ["PAGE_NOT_IN_PACKET"]
    quote = (rec.get("quote") or "").strip()
    if not quote:
        reasons.append("QUOTE_EMPTY")
    else:
        parts = [p for p in re.split(r"\.\.\.|…|\[\.\.\.\]", quote) if p.strip()]
        if not all(page.has_fragment(p) for p in parts):
            reasons.append("QUOTE_NOT_ON_PAGE")
    vap = rec.get("value_as_printed")
    if vap not in (None, ""):
        toks = printed_numbers(vap)
        missing = [t for t in toks if _num_key(t) not in page.numbers]
        if missing:
            reasons.append("NUMBER_NOT_ON_PAGE:" + "|".join(missing))
        cands = candidate_values(vap)
        for key in ("value_min", "value_max"):
            v = rec.get(key)
            if v is not None and not any(_close(float(v), c) for c in cands):
                reasons.append(f"{key.upper()}_NOT_PRINTED")
    else:
        for key in ("value_min", "value_max"):
            if rec.get(key) is not None:
                reasons.append(f"{key.upper()}_WITHOUT_PRINTED_VALUE")
    unit = (rec.get("unit_as_printed") or "").strip()
    if unit.casefold() not in _DIMENSIONLESS and not page.has_fragment(unit):
        reasons.append("UNIT_NOT_ON_PAGE")
    return reasons


def warnings_for(rec: dict) -> list[str]:
    """Soft flags: kept on accepted records for the reviewer."""
    w = []
    vap, quote = rec.get("value_as_printed") or "", rec.get("quote") or ""
    if vap and quote:
        qn = {_num_key(t) for t in printed_numbers(quote)}
        if not all(_num_key(t) in qn for t in printed_numbers(vap)):
            w.append("VALUE_NOT_IN_QUOTE")
    if rec.get("multiplier_as_printed"):
        w.append("HEADER_MULTIPLIER")
    return w
