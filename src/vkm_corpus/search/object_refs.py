"""Numbered objects named in a query: «скв. 75», «скважина № 75», «профильная линия 3», «блок 2», «панель 5»,
«репер 12», «рис. 3.1», «табл. 2.4», «формула (3.12)» (agent Б, 13.10).

A reference is a *type word* followed by a *number*; the number alone is never a reference, so «75 м», «УДК О-75»,
«2.1 МПа» or «СКРУ-1» are not object numbers. A number followed by a unit (м, мм, МПа, % …) or by an ordinal
ending («линия 2-го порядка») is not one either. Forms: Russian case forms and abbreviations of the type word
(«скв.», «скважины», «рис.», «рисунке», «табл.», «таблице»), an optional «№», the English «borehole», «profile line»,
«block», «panel», «benchmark», «fig.»/«figure», «table», «equation»/«formula». Every pattern is a MODEL_CHOICE of
retrieval, not scientific data.

How the search uses a reference (``vkm_corpus.search.query`` and ``vkm_corpus.search.hybrid``):

* BM25 phrase variants (:attr:`ObjectRef.phrases`) — «скв 75», «скважина 75», «borehole 75» … The analyzer drops
  «№» and the abbreviation dot and stems the type word, so «скважина 75» also meets «скважины № 75»; the number is a
  token of its own («3.1» one token, «3.10» another);
* the topic (:func:`strip_refs`) — the query without its references — ranks; the reference promotes inside the
  topic's first hits (the Q5 rule of agent L for printed object numbers);
* the query translation keeps the reference (:func:`keep_refs`: «скважина 75» → «borehole» → «borehole 75»).

Only queries with a recognised reference change; every other query takes exactly the old path.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

BOREHOLE, LINE, BLOCK, PANEL, BENCHMARK = "BOREHOLE", "LINE", "BLOCK", "PANEL", "BENCHMARK"
FIGURE, TABLE, FORMULA = "FIGURE", "TABLE", "FORMULA"
KINDS: tuple[str, ...] = (BOREHOLE, LINE, BLOCK, PANEL, BENCHMARK, FIGURE, TABLE, FORMULA)
LABEL_KINDS: frozenset[str] = frozenset({FIGURE, TABLE, FORMULA})       # printed numbers of document objects

# type word → (Russian phrase heads, English phrase heads): the BM25 phrase is «<head> <number>»
PHRASE_HEADS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    BOREHOLE: (("скважина", "скв"), ("borehole",)),
    LINE: (("линия",), ("profile line",)),
    BLOCK: (("блок",), ("block",)),
    PANEL: (("панель",), ("panel",)),
    BENCHMARK: (("репер",), ("benchmark",)),
    FIGURE: (("рис", "рисунок"), ("fig", "figure")),
    TABLE: (("табл", "таблица"), ("table",)),
    FORMULA: (("формула", "уравнение"), ("equation",)),
}
EN_WORD: dict[str, str] = {BOREHOLE: "borehole", LINE: "line", BLOCK: "block", PANEL: "panel",
                           BENCHMARK: "benchmark", FIGURE: "figure", TABLE: "table", FORMULA: "equation"}

_L = r"(?<![0-9A-Za-zА-Яа-яЁё_])"                            # left word boundary (Cyrillic-aware)
_NO = r"\s*(?:№\s*|N\s*[o°º]?\.?\s*|#\s*)?"                   # optional «№», «No.», «#»
_ORDINAL = r"(?:го|й|я|е|х|ой|ая|ое|ые|ых|ом|му|ми|ти|ть)(?![А-Яа-яЁё])"
# 75, 75а, 1/2, 12-3, 1-С (not 2.5, not 2,5, not the ordinal 3-й)
_ID = rf"(\d{{1,5}}(?:[а-яa-z](?![а-яa-zё]))?(?:[/-](?!{_ORDINAL})(?:\d{{1,4}}|[а-яa-z]{{1,3}})(?![\d.,а-яa-zё]))?)"
_DOTTED = r"(\d{1,3}(?:\.\d{1,3}){0,3}(?:[а-яa-z](?![а-яa-zё]))?)"  # 3, 3.1, 3.1.2, 3.1а
_UNITS = (r"м|мм|см|км|м2|м3|м²|м³|m|mm|cm|km|мпа|кпа|гпа|па|mpa|kpa|gpa|pa|кн|мн|kn|mn|кг|тыс|млн|млрд|%|‰|°|"
          r"м/с|км/с|m/s|km/s|град|сек|мин|сут|лет|год|года|г|гг|г/см3|т/м3|kg|кв|kv|квт|kw|вт|гц|hz")
_STOP = (r"(?!\d)"                                             # the whole number (no backtracking into «25»)
         rf"(?!\s*(?:{_UNITS})(?![0-9A-Za-zА-Яа-яЁё]))"        # «75 м», «2.1 МПа», «1975 г.»: a quantity
         rf"(?![-–]{_ORDINAL})"                                 # «2-го порядка»: an ordinal
         r"(?![.,]\d)")                                        # «2,5»: a decimal
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple((kind, re.compile(f"{_L}(?:{head}){_NO}{num}{_STOP}",
                                                                              re.IGNORECASE)) for kind, head, num in (
    (BOREHOLE, r"(?:скв(?:ажин[а-яё]{0,3})?\.?|bore\s?holes?|drill\s?holes?)", _ID),
    (LINE, r"(?:(?:профильн[а-яё]{0,3}|наблюдательн[а-яё]{0,3})\s+)?лини(?:я|и|ю|ей|ях|ям|ями)|profile\s+lines?",
     _ID),
    (BLOCK, r"(?:блок(?:а|у|е|ом|и|ов|ам|ами|ах)?|blocks?)", _ID),
    (PANEL, r"(?:панел(?:ь|и|ью|ей|ям|ях|ями)|panels?)", _ID),
    (BENCHMARK, r"(?:репер(?:а|у|ом|е|ы|ов|ам|ами|ах)?|benchmarks?)", _ID),
    (FIGURE, r"(?:рис(?:ун(?:ок|ка|ке|ку|ком|ки|ков|кам|ках))?\.?|fig(?:ure|s)?\.?)", _DOTTED),
    (TABLE, r"(?:табл(?:иц(?:а|ы|е|у|ей|ах))?\.?|tables?)", _DOTTED),
    (FORMULA, r"(?:формул(?:а|ы|е|у|ой|ам|ах)?|уравнени(?:е|я|ю|ем|и|й)|equations?|eqs?\.?|formulas?)\s*\(?",
     _DOTTED + r"\)?"),
))


@dataclass(frozen=True)
class ObjectRef:
    """One numbered object of a query: ``kind`` (:data:`KINDS`), ``number`` as printed (lower case, «№» dropped)
    and the character span of the whole reference in the query."""

    kind: str
    number: str
    start: int
    end: int
    surface: str

    @property
    def phrases(self) -> tuple[str, ...]:
        """BM25 phrase variants «<type word> <number>» (Russian and English heads)."""
        ru, en = PHRASE_HEADS[self.kind]
        return tuple(f"{head} {self.number}" for head in (*ru, *en))

    @property
    def english(self) -> str:
        return f"{EN_WORD[self.kind]} {self.number}"

    @property
    def is_label(self) -> bool:
        """A printed number of a figure, table or formula (``object_label`` / ``equation_label`` of the objects)."""
        return self.kind in LABEL_KINDS

    def as_record(self) -> dict[str, str]:
        return {"kind": self.kind, "number": self.number, "text": self.surface}


def parse_refs(query: str) -> list[ObjectRef]:
    """The numbered objects of ``query`` in query order (overlaps: the earlier, then the longer match wins)."""
    found: list[ObjectRef] = []
    for kind, pattern in _PATTERNS:
        for m in pattern.finditer(query or ""):
            number = m.group(1).lower().replace("ё", "е")
            found.append(ObjectRef(kind, number, m.start(), m.end(), m.group(0).strip()))
    found.sort(key=lambda r: (r.start, -(r.end - r.start), KINDS.index(r.kind)))
    out: list[ObjectRef] = []
    for r in found:
        if out and r.start < out[-1].end:
            continue
        out.append(r)
    return out


def strip_refs(query: str, refs: list[ObjectRef] | None = None) -> str:
    """The topic: the query without its references («скважина 75 скорость волн» → «скорость волн»)."""
    refs = parse_refs(query) if refs is None else refs
    out, pos = [], 0
    for r in sorted(refs, key=lambda r: r.start):
        out.append(query[pos:r.start])
        pos = r.end
    out.append(query[pos:])
    text = re.sub(r"\s+", " ", " ".join(out))
    return text.strip(" \t,.;:–—-()")


_TOPIC_STOP = frozenset({"для", "при", "над", "под", "про", "как", "что", "это", "или", "the", "and", "for", "with"})


def has_topic(topic: str) -> bool:
    """Whether the rest of a query is a topic: at least one word of three letters or more (prepositions, conjunctions
    and bare numbers left over from «по профильной линии № 8» or «скважины 75 и 76» are not a topic)."""
    return any(len(w) >= 3 and w.lower() not in _TOPIC_STOP for w in re.findall(r"[^\W\d_]+", topic or ""))


def keep_refs(query: str, text: str | None, refs: list[ObjectRef] | None = None, *,
              english: bool = True) -> str | None:
    """``text`` (another wording of ``query``) with every reference of ``query`` kept. ``english`` (a translation): a
    number the wording lost goes right after its English type word when that word is there («borehole» → «borehole
    75»), else the English reference is appended («subsidence trough» → «subsidence trough figure 3.1»); otherwise (a
    wording in the query's language) the reference as written in the query is appended. ``None`` stays ``None``; a
    query without references returns ``text`` unchanged."""
    if text is None:
        return None
    refs = parse_refs(query) if refs is None else refs
    out = text
    for r in refs:
        tokens = re.findall(r"[0-9]+(?:[./-][0-9a-zа-яё]+)*[a-zа-яё]?", out.lower())
        if r.number in tokens:
            continue
        word = re.search(rf"(?<![a-z]){EN_WORD[r.kind]}s?(?![a-z])", out, re.IGNORECASE) if english else None
        out = (f"{out[:word.end()]} {r.number}{out[word.end():]}" if word else
               f"{out} {r.english if english else r.surface}")
    return " ".join(out.split())
