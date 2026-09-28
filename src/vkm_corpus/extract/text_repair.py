"""Text-layer diagnostics: glyph statistics, function-word plausibility, deterministic CP1251 mojibake repair.

The repair re-reads Latin-1-range glyphs (U+00C0..U+00FF, U+00A8, U+00B8) as CP1251 bytes – the typical defect of
single-byte Cyrillic fonts without a proper ``/ToUnicode``. It is a DERIVATION: the raw layer is kept, the repaired
text is accepted only if it passes the plausibility check, otherwise the page goes to OCR.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

FUNC_WORDS = frozenset(
    """и в во не на с со что по из к ко а о об от для при до как это или то же за но так их его ее её был была были
    быть есть также может между более после где которые который которая которое если только уже под над без через
    этого этом этой этих он она они мы все всех т е см рис табл чем того том
    the of and to in a is for on that by with as are be this from at an or which was were it its can not have has
    been these their also between we our than into such
    der die das und von zu mit im den dem des ist wird werden ein eine einer eines für auf als sich nicht auch durch
    nach aus wie oder bei sind wurde wurden
    le la les et des du un une est dans pour par sur que qui au aux ce ces avec""".split())
WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё]+")
_GOOD_PUNCT = set(".,;:!?-–—()[]{}\"'«»„“”‘’/\\%+=*<>№§°±×·…_|~^&#@$")
_WS = {0x20, 0x09, 0x0A, 0x0D, 0x0C, 0xA0}


def char_stats(codes: Iterable[int]) -> dict[str, int]:
    """Counts over non-whitespace code points."""
    n = cyr = lat = latext = greek = digits = ctrl = pua = repl = punct = other = 0
    for o in codes:
        if o in _WS:
            continue
        n += 1
        if 0x0400 <= o <= 0x052F:
            cyr += 1
        elif 0x41 <= o <= 0x5A or 0x61 <= o <= 0x7A:
            lat += 1
        elif 0x30 <= o <= 0x39:
            digits += 1
        elif 0x00C0 <= o <= 0x024F:
            latext += 1
        elif 0x0370 <= o <= 0x03FF:
            greek += 1
        elif o < 0x20 or 0x7F <= o <= 0x9F:
            ctrl += 1
        elif 0xE000 <= o <= 0xF8FF:
            pua += 1
        elif o == 0xFFFD:
            repl += 1
        elif (o < 0x110000 and chr(o) in _GOOD_PUNCT) or 0x2010 <= o <= 0x2044 or 0x2200 <= o <= 0x22FF:
            punct += 1
        else:
            other += 1
    return {"chars": n, "cyr": cyr, "lat": lat, "latext": latext, "greek": greek, "digits": digits, "ctrl": ctrl,
            "pua": pua, "repl": repl, "punct": punct, "other": other}


def text_char_stats(text: str) -> dict[str, int]:
    return char_stats(ord(c) for c in text)


def remap_cp1251(text: str) -> str:
    out = []
    for ch in text:
        o = ord(ch)
        if 0xC0 <= o <= 0xFF or o in (0xA8, 0xB8):
            out.append(bytes([o]).decode("cp1251"))
        else:
            out.append(ch)
    return "".join(out)


def word_stats(text: str) -> tuple[int, int]:
    """(word tokens, function-word hits)."""
    words = WORD_RE.findall(text)
    return len(words), sum(1 for w in words if w.lower() in FUNC_WORDS)


def repair_plausible(text: str, *, min_cyr_share: float = 0.5, min_func_rate: float = 0.05,
                     min_words_for_func: int = 40) -> tuple[bool, dict[str, float]]:
    """Plausibility of the CP1251-repaired text: Cyrillic share and function-word rate."""
    rep = remap_cp1251(text)
    n = sum(1 for c in rep if not c.isspace()) or 1
    cyr = sum(1 for c in rep if "Ѐ" <= c <= "ӿ")
    words, func = word_stats(rep)
    rate = func / words if words else 0.0
    ok = cyr / n >= min_cyr_share and (words < min_words_for_func or rate >= min_func_rate)
    return ok, {"cyr_share": round(cyr / n, 4), "words": words, "func_rate": round(rate, 4)}


def letter_share(text: str) -> float:
    """Share of letters (Cyrillic, Latin, Greek) among non-whitespace characters (scenario-B layer quality)."""
    st = text_char_stats(text)
    return (st["cyr"] + st["lat"] + st["greek"] + st["latext"]) / st["chars"] if st["chars"] else 0.0
