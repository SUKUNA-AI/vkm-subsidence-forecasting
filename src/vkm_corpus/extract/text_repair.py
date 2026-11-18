"""Text-layer diagnostics: glyph statistics, function-word plausibility, deterministic CP1251 mojibake repair.

Only word-shaped stretches consistent with a misdecoded Cyrillic word are eligible. ASCII-bearing words and
standalone mathematical operators are outside the repair. Keep raw text and locate each change or ambiguity.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

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
_GOOD_PUNCT = set(".,;:!?-–—()[]{}\"'«»„“”‘’/\\%+=*<>№§°±×÷·…_|~^&#@$")
_WS = {0x20, 0x09, 0x0A, 0x0D, 0x0C, 0xA0}
CP1251_REPAIR_RULE = "cp1251_contextual_word_v2"
_TOKEN = re.compile(r"[\w\u00a8\u00b8×÷]+")
_LATIN1_BYTES = frozenset(chr(i) for i in range(0xC0, 0x100)) | {"¨", "¸"}
_RUSSIAN_WORD = re.compile(r"[А-Яа-яЁё]+\Z")
_RUSSIAN_VOWELS = frozenset("аеёиоуыэюя")
# Byte glyphs with no ordinary use inside Latin words. They only count within a plausible mapped word.
_WORD_SIGNAL = frozenset("×÷¨¸ÐðÞþß")
_VARIABLE_CHAIN = re.compile(r"[^×÷](?:[×÷][^×÷])+")
_RELATION_BEFORE = re.compile(r"[=<>≤≥≠≈][^;:\r\n]*\Z")
_RELATION_AFTER = re.compile(r"[^\S\r\n]*[=<>≤≥≠≈]")


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
        elif o in (0xD7, 0xF7):
            punct += 1
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


@dataclass(frozen=True)
class RepairSpan:
    start: int
    end: int
    raw: str
    replacement: str | None
    reason: str


@dataclass(frozen=True)
class TextRepair:
    text: str
    spans: tuple[RepairSpan, ...]


def contextual_cp1251(text: str, *, mapped_font: bool = False,
                      line_context: tuple[str, int] | None = None) -> TextRepair:
    """Return a candidate transcription and offsets into the unchanged input layer."""
    line, offset = line_context if line_context is not None else (text, 0)
    tokens = []
    for match in _TOKEN.finditer(text):
        raw = match.group()
        if raw in ("×", "÷") or any(char not in _LATIN1_BYTES for char in raw):
            continue
        repaired = raw.encode("latin-1").decode("cp1251")
        if not _RUSSIAN_WORD.fullmatch(repaired):
            continue
        letters = set(repaired.lower())
        word_shape = bool(letters & _RUSSIAN_VOWELS and letters - _RUSSIAN_VOWELS)
        # Infix × is ambiguous even without an equation marker. Leading × can still be the initial Ч of a word.
        has_operator = "×" in raw or "÷" in raw
        arithmetic = has_operator and bool(
            "×" in raw[1:] or _VARIABLE_CHAIN.fullmatch(raw)
            or _RELATION_BEFORE.search(line, 0, offset + match.start())
            or _RELATION_AFTER.match(line, offset + match.end()))
        strong = (not arithmetic and len(raw) >= 3 and word_shape and len(letters) >= 3
                  and (any(char in _WORD_SIGNAL for char in raw)
                       or len(raw) >= 4 and repaired[1:].islower()))
        tokens.append((match, repaired, strong, arithmetic))
    contextual = any(strong for _, _, strong, _ in tokens)
    spans = []
    parts = []
    start = 0
    for match, repaired, strong, arithmetic in tokens:
        raw = match.group()
        # Short prepositions often occur in a broken run. Require supporting word evidence in the same run.
        accepted = not mapped_font and not arithmetic and (strong or contextual and repaired.lower() in FUNC_WORDS)
        replacement = repaired if accepted else None
        if accepted:
            reason = "CP1251_WORD_SHAPE" if strong else "CP1251_CONTEXT_FUNCTION_WORD"
        elif arithmetic:
            reason = "AMBIGUOUS_ARITHMETIC"
        else:
            reason = "DECLARED_UNICODE_FONT" if mapped_font else "AMBIGUOUS_CP1251_WORD"
        parts.extend((text[start:match.start()], replacement or raw))
        spans.append(RepairSpan(match.start(), match.end(), raw, replacement, reason))
        start = match.end()
    parts.append(text[start:])
    return TextRepair("".join(parts), tuple(spans))


def remap_cp1251(text: str) -> str:
    return contextual_cp1251(text).text


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
