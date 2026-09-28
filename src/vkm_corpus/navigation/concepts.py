"""Concept (term) graph of the navigation layer NAV (``docs/corpus_platform/NAVIGATION_LAYER.md`` §4) — no LLM.

``build(con, *, section_pages=None, seeds=None, **options)`` returns three Arrow tables:

* ``terms`` — noun phrases found by POS patterns (Russian: Adj* + Noun (+ genitive Noun groups), 1–4 words; English
  and German noun runs; abbreviations), keyed by their lemma key (``TRM-`` id from ``navigation.ids``). A candidate is
  kept when it occurs in >= 2 units of >= 2 sources (or is a project seed) and its C-value is positive;
* ``term_mentions`` — term ↔ co-occurrence unit with tf, tf-idf, the pages and the best blocks. The unit is an N1
  section when ``section_pages`` is given, otherwise a run of pages that starts at a heading (``HEADING_GROUP``),
  otherwise a page; long units are cut into windows of ``max_unit_pages`` pages;
* ``term_edges`` — a generic edge list (``src_term_id``, ``dst_term_id`` / ``dst_ref``, ``kind``, ``weight``,
  ``n_units``, ``n_sources``, ``examples``, ``rule_version``): ``CO_OCCURS`` (NPMI over units, top-k neighbours per
  term), ``DEFINED_AS`` (definition patterns; ``dst_ref`` is the block), ``SAME_AS`` (a translation or an
  abbreviation printed in parentheses) and ``CONTAINS`` (a longer term lexically contains a shorter one). N2 adds
  ``SYMBOL_OF`` with the same schema.

An edge is a navigation hint — «discussed together in N units of M sources» — never a physical claim (DERIVED,
AUTO_EXTRACTED_UNREVIEWED). Morphology degrades gracefully: pymorphy3 (POS patterns and lemmas) → Snowball stems →
crude suffix stripping. Lemma keys, and so term ids, depend on it: the backend is written into every ``terms`` row
(``morphology``). Pair counting (the heavy part) runs on the GPU with cuDF when it is importable and in DuckDB
otherwise; both return the same rows (NPMI rounded half-up to 6 decimals, deterministic tie-breaks). Leiden
communities of the co-occurrence graph need cuGraph (``community`` is NULL without it).
"""
from __future__ import annotations

import json
import logging
import math
import multiprocessing as mp
import os
import re
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from typing import Any, Iterable, NamedTuple, Sequence

import numpy as np
import pyarrow as pa

from vkm_corpus.navigation import ids as nav_ids

log = logging.getLogger(__name__)

RULE_VERSION = nav_ids.RULE_VERSIONS["concepts"]
TEXT_BLOCK_TYPES: tuple[str, ...] = ("TEXT", "HEADING", "TITLE", "CAPTION", "LIST_ITEM", "ABSTRACT")
HEADING_BLOCK_TYPES: tuple[str, ...] = ("HEADING", "TITLE")
MAX_WORDS = 4
UNIT_KINDS = ("SECTION", "HEADING_GROUP", "PAGE")
EDGE_KINDS = ("CO_OCCURS", "DEFINED_AS", "SAME_AS", "CONTAINS")
METADATA_KEY = b"vkm_nav_concepts"

DEFAULTS: dict[str, Any] = {
    "max_unit_pages": 5,        # a unit longer than this is cut into windows (co-occurrence stays local)
    "min_df_units": 2,          # a candidate needs >= 2 units ...
    "min_df_sources": 2,        # ... of >= 2 sources (seeds are exempt)
    "top_k": 30,                # neighbours kept per term
    "min_npmi": 0.2,
    "min_shared_units": 3,
    "min_edge_sources": 2,      # a CO_OCCURS pair must be seen together in >= 2 sources
    "max_nested_share": 0.9,    # drop a candidate seen >= 90 % of the time inside one longer candidate
    "max_terms": None,          # optional cap by C-value (seeds are always kept)
    "workers": None,            # lemmatisation processes (None: min(cpu, 16); 1: in-process)
    "backend": "auto",          # pair counting: "auto" | "cudf" | "duckdb"
    "communities": "auto",      # Leiden via cuGraph: "auto" | True | False
    "morphology": "auto",       # "auto" | "pymorphy3" | "snowball" | "crude"
}

# ---------------------------------------------------------------------------------------------------------- vocabularies


def _ws(text: str) -> frozenset[str]:
    return frozenset(text.split())


# single-word candidates that are never terms
STOP_NOUNS_RU = _ws("""
работа данные результат случай вид раз часть год рисунок рис таблица табл глава раздел пример вопрос ряд число
количество образ место сторона конец начало мера помощь основа цель задача тип характер связь момент отношение условие
способ вариант группа использование применение наличие отсутствие необходимость возможность особенность значение
формула уравнение выражение автор статья книга издание страница стр том номер итог вывод сумма причина время период
этап стадия целое ход рамка счет учет зависимость сравнение соответствие точка зрение мнение внимание интерес роль
факт фактор аспект подход целое прочее др пр см табл рисунок приложение введение заключение литература список
""")
# head nouns that make a multiword candidate generic («ряд авторов», «результаты расчета»)
HEAD_STOP_RU = _ws("""
ряд количество число множество большинство часть помощь случай основа результат наличие отсутствие использование
применение необходимость возможность пример рисунок рис таблица табл глава раздел вид тип значение итог вывод целое ход
рамка счет учет зависимость сравнение соответствие автор статья вопрос задача цель роль сторона образ особенность
""")
# adjectives (key form) that break a noun phrase: pronominal, ordinal and evaluative words
STOP_ADJ_RU = _ws("""
данный настоящий следующий различный другой каждый весь такой некоторый определенный большой малый новый последний
известный существующий возможный необходимый соответствующий указанный рассматриваемый приведенный полученный
представленный имеющийся аналогичный подобный значительный небольшой важный отдельный конкретный целый самый
иной сам свой наш ваш этот тот который какой любой многий несколько один описанный изложенный вышеуказанный
нижеследующий дальнейший последующий предыдущий вышеприведенный нижеприведенный названный отмеченный упомянутый
используемый применяемый предложенный разработанный принятый заданный искомый характерный типичный особый некий
всякий прочий остальной хороший плохой подобный лучший
""")
PREPOSITIONS_RU = _ws("в во на с со по при за под о об от до из к ко для без над через между про")
# printed abbreviations and function words (the fallback without POS tags relies on this list)
STOP_WORDS_RU = _ws("""
греч лат англ нем франц фр итал рис табл стр гл см др пр им тыс млн млрд руб обл ок ср напр вып изд ред сб тр кн
и не что он на я как а то все она так его но да ты у же вы бы только ее мне было вот меня еще нет ему теперь когда
даже ну вдруг ли если уже или ни быть был него вас нибудь опять уж вам ведь там потом себя ничего ей может они тут
где есть надо ней мы тебя их чем была сам чтоб будто чего раз тоже себе будет тогда кто этот того потому этого
какой совсем ним здесь этом один почти мой тем чтобы нее сейчас были куда зачем всех никогда можно наконец два
хоть после больше тот эти нас всего них какая много разве три эту моя впрочем хорошо свою этой перед иногда лучше
чуть том нельзя такой им более всегда конечно всю это также который которая которое которые которых котором которой
которым которого является являются являлся могут следует должен должна должно должны однако поэтому причем зависит
зависят равен равна равно равны при этом весьма очень наиболее менее
""")
# «в случае», «с помощью», «в пределах» …: the noun after the preposition is part of a complex preposition
COMPLEX_PREP_NOUNS_RU = _ws("""
случае случаях помощью помощи виде качестве основе основании результате течение процессе ходе пределах учетом
зависимости мере связи целях счет условиях области отношении сравнению соответствии направлении рамках составе
сторону протяжении пользу силу свете целом частности отличие итоге
""")
DEFINE_UNDERSTOOD_RU = _ws("понимается понимаются понимают понимаем подразумевается подразумеваются подразумевают")
DEFINE_CALLED_RU = _ws("называется называются называют называем назывался называлась называлось назывались "
                       "именуется именуются")
UNITS_ABBR = _ws("""
мпа кпа гпа па кн мн гц кгц мгц ггц квт мвт гвт мдж кдж дж дб кв мв мм см км кг мг сут тыс млн млрд руб
mpa kpa gpa pa kn mn hz khz mhz ghz kw mw gw mj kj db kv mv mm cm km kg mg ppm ppb
""")
ROMAN_RE = re.compile(r"^[IVXLCDM]+$")

STOP_EN = _ws("""
a about above after again against all almost along already also although always am among an and another any are around
as at be because been before being below between both but by can cannot could did do does doing done down during each
either else enough etc even ever every few for from further had has have having he her here hers herself him himself
his how however i if in into is it its itself just least less let like may me might more most much must my myself
neither no nor not now of off often on once one only or other others otherwise our ours ourselves out over own per
rather same several shall she should since so some such than that the their theirs them themselves then there
therefore these they this those though through thus to too toward towards under until up upon us very via was we well
were what when where whether which while who whom whose why will with within without would yet you your yours hence ie
eg fig figs figure figures table tables eq eqs section sections chapter chapters et al see shown show shows showed given
obtained used using use uses based following follows followed described presented proposed considered assumed defined
called known termed respectively first second third fourth new different various many large small good better best
main major minor certain particular specific important possible necessary usually generally typically mainly mostly
relatively approximately nearly increase increases increased decrease decreases decreased depend depends depending
lead leads result results resulted occur occurs occurred become becomes became provide provides provided allow allows
allowed require requires required include includes included consist consists contain contains obtain make makes made
take takes taken give gives find finds found seem seems appear appears indicate indicates suggest suggests represent
represents reach reaches remain remains produce produces cause causes caused note noted thereby whereas whose due
""")
STOP_NOUNS_EN = _ws("""
result data case paper study work value number way part time year example problem approach type kind order term set end
side amount degree fact reason purpose aim author reference page equation expression formula step appendix
introduction conclusion summary chapter section figure table note lot variety respect addition terms basis
""")
HEAD_STOP_EN = _ws("number amount lot variety kind type example case result figure table section chapter equation page")
EN_LY_NOUNS = _ws("anomaly assembly family supply monopoly italy reply")
EN_ED_NOUNS = _ws("bed seed speed feed need shed reed weed creed embed hundred sled bred")
STOP_DE = _ws("""
der die das den dem des ein eine einer eines einem einen und oder aber nicht ist sind war waren wird werden wurde wurden
sein hat haben hatte mit von zu zur zum im in an am auf aus bei bis durch für gegen nach über unter vor zwischen als wie
so auch nur noch schon sehr mehr weniger kann können muss müssen soll sollen sich es er sie wir ihr man dieser diese
dieses jede jeder jedes alle allem allen aller sowie bzw usw dass daß wenn weil ob da hier dort dann denn doch sondern
also etwa kein keine keinen abb tab
""")
_EN_MARKERS = _ws("the and of is are to in with for that by this which from")
_DE_MARKERS = _ws("der die das und ist sind nicht mit von zu den dem des ein eine für auf")

_CYR_RE = re.compile(r"[Ѐ-ӿ]")
_LAT_RE = re.compile(r"[A-Za-zÀ-ɏ]")
_UKR = str.maketrans({"і": "и", "ї": "и", "є": "е", "ґ": "г", "І": "И", "Ї": "И", "Є": "Е", "Ґ": "Г"})
_LAT2CYR = {"a": "а", "e": "е", "o": "о", "p": "р", "c": "с", "x": "х", "y": "у", "k": "к", "m": "м", "i": "и",
            "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "K": "К", "M": "М", "O": "О", "P": "Р", "T": "Т",
            "X": "Х", "Y": "У"}
_CYR2LAT = {"а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y", "к": "k", "м": "m",
            "А": "A", "В": "B", "С": "C", "Е": "E", "Н": "H", "К": "K", "М": "M", "О": "O", "Р": "P", "Т": "T",
            "Х": "X"}
_MATH_RE = re.compile(r"\$\$.+?\$\$|\$[^$]{1,400}?\$", re.S)
_TOKEN_RE = re.compile(r"[^\W\d_]+(?:[-‐‑][^\W\d_]+)*(?:-\d{1,3}(?!\d))?|\d+(?:[.,]\d+)*|[^\w\s]")
_DASHES = frozenset("-–—‒")
_SENT_END = frozenset(".!?;")
_CASE_MAP = {"gen2": "gent", "loc2": "loct", "acc2": "accs", "voct": "nomn"}
_RU_ENDINGS = sorted("""ями ами ого его ому ему ыми ими ых их ой ей ый ий ая яя ое ее ые ие ую юю ом ем ам ям ах ях ов ев
ью ия ий ья ье ы и а я о е у ю ь""".split(), key=len, reverse=True)
_RU_VERBISH = ("ться", "ется", "ются", "ует", "уют", "ать", "ять", "ить", "еть", "ывает", "ивает", "ал", "ила", "ило",
               "или", "ался", "лась", "лось", "лись")


# -------------------------------------------------------------------------------------------------------------- morphology


class _RuWord(NamedTuple):
    nominal: bool                   # can take part in a noun phrase
    noun: tuple | None              # (lemma, gender, frozenset[(case, number)])
    adj: tuple | None               # (key form, frozenset[(case, number, gender)])
    top_adj: bool                   # the most probable parse is an adjective or participle


_BREAK = _RuWord(False, None, None, False)


class Morphology:
    """Word analysis with graceful degradation: pymorphy3 → Snowball stems → crude suffix stripping."""

    def __init__(self, mode: str = "auto") -> None:
        self._morph = None
        self._ru_stem = self._de_stem = None
        if mode in ("auto", "pymorphy3"):
            try:
                import pymorphy3  # noqa: PLC0415 — optional dependency (extra `navigation`)

                self._morph = pymorphy3.MorphAnalyzer()
            except Exception:  # noqa: BLE001 — ImportError or a missing dictionary package
                if mode == "pymorphy3":
                    raise
        if mode != "crude":
            try:
                import snowballstemmer  # noqa: PLC0415

                self._ru_stem = snowballstemmer.stemmer("russian")
                self._de_stem = snowballstemmer.stemmer("german")
            except Exception:  # noqa: BLE001
                if mode == "snowball":
                    raise
        self.name = "pymorphy3" if self._morph else ("snowball" if self._ru_stem else "crude")
        self._ru_cache: dict[str, _RuWord] = {}
        self._stem_cache: dict[str, str] = {}

    # -- Russian with pymorphy3
    def ru_word(self, w: str) -> _RuWord:
        hit = self._ru_cache.get(w)
        if hit is not None:
            return hit
        res = self._analyze(w)
        if len(self._ru_cache) < 2_000_000:
            self._ru_cache[w] = res
        return res

    def _analyze(self, w: str) -> _RuWord:
        if len(w) < 2 or w in PREPOSITIONS_RU or w in STOP_WORDS_RU or w in UNITS_ABBR:
            return _BREAK
        parses = self._morph.parse(w)
        top = parses[0]
        pos = top.tag.POS
        if pos not in ("NOUN", "ADJF", "PRTF"):
            return _BREAK
        floor = top.score * 0.1
        noun_lemma = gender = adj_key = None
        noun_forms: set[tuple[str, str]] = set()
        adj_forms: set[tuple[str, str, str | None]] = set()
        for p in parses:
            if p.score < floor:
                continue
            tag = p.tag
            # pymorphy grammemes validate comparisons: work with plain strings
            case = str(tag.case) if tag.case else None
            case = _CASE_MAP.get(case, case)
            number = str(tag.number) if tag.number else None
            gram_gender = str(tag.gender) if tag.gender else None
            if tag.POS == "NOUN":
                if noun_lemma is None:
                    noun_lemma, gender = p.normal_form, gram_gender
                if p.normal_form == noun_lemma and case and number:
                    noun_forms.add((case, number))
            elif tag.POS in ("ADJF", "PRTF"):
                if "Apro" in tag or "Anum" in tag:
                    continue
                key = p.normal_form if tag.POS == "ADJF" else self._prtf_key(p)
                if adj_key is None:
                    adj_key = key
                if key == adj_key and case and number:
                    adj_forms.add((case, number, gram_gender))
        if pos in ("ADJF", "PRTF") and ("Apro" in top.tag or "Anum" in top.tag or adj_key in STOP_ADJ_RU):
            return _BREAK
        if adj_key in STOP_ADJ_RU:
            adj_forms = set()
        noun = (noun_lemma, gender, frozenset(noun_forms)) if noun_forms else None
        adj = (adj_key, frozenset(adj_forms)) if adj_forms else None
        if noun is None and adj is None:
            return _BREAK
        return _RuWord(True, noun, adj, pos != "NOUN")

    @staticmethod
    def _prtf_key(p: Any) -> str:
        try:
            q = p.inflect({"masc", "sing", "nomn"})
            return q.word if q is not None else p.word
        except Exception:  # noqa: BLE001
            return p.word

    def nominative(self, surface: str) -> str:
        """Dictionary form of a Russian phrase: the head group in the nominative, singular unless the printed form
        can only be plural («горные работы»); genitive dependents stay as printed («оседание земной поверхности»)."""
        if self._morph is None:
            return surface
        words = surface.split()
        run = [self.ru_word(w) for w in words]
        chunks = _ru_chunks(run)
        if not chunks or not chunks[0]:
            return surface
        adjs, noun, cases = chunks[0][0]
        start = adjs[0] if adjs else noun
        if start != 0 or not cases:
            return surface
        number = "plur" if {n for _, n in cases} == {"plur"} else "sing"
        if ("nomn", number) in cases:
            return surface

        def consistent(p: Any) -> bool:
            c = str(p.tag.case) if p.tag.case else None
            return (_CASE_MAP.get(c, c), str(p.tag.number) if p.tag.number else None) in cases

        lemma = run[noun].noun[0]
        noun_parse = next((p for p in self._morph.parse(words[noun])
                           if p.tag.POS == "NOUN" and p.normal_form == lemma and consistent(p)), None)
        if noun_parse is None:
            return surface
        inf = noun_parse.inflect({"nomn", number})
        if inf is None and number == "sing":                    # plural-only nouns
            number = "plur"
            inf = noun_parse.inflect({"nomn", number})
        if inf is None:
            return surface
        out = list(words)
        out[noun] = inf.word
        gender = str(inf.tag.gender) if inf.tag.gender else None
        for a in adjs:
            ap = next((p for p in self._morph.parse(words[a]) if p.tag.POS in ("ADJF", "PRTF") and consistent(p)),
                      None)
            want = {"nomn", number} | ({gender} if number == "sing" and gender else set())
            ai = ap.inflect(want) if ap is not None else None
            if ai is None:
                return surface
            out[a] = ai.word
        return " ".join(out)

    # -- stems (fallback and German)
    def ru_stem(self, w: str) -> str:
        hit = self._stem_cache.get(w)
        if hit is None:
            hit = self._ru_stem.stemWord(w) if self._ru_stem else _crude_ru_stem(w)
            self._stem_cache[w] = hit
        return hit

    def de_stem(self, w: str) -> str:
        return self._de_stem.stemWord(w) if self._de_stem else w


def _crude_ru_stem(w: str) -> str:
    for e in _RU_ENDINGS:
        if w.endswith(e) and len(w) - len(e) >= 3:
            return w[: -len(e)]
    return w


def _singular_en(w: str) -> str:
    irregular = {"data": "data", "criteria": "criterion", "strata": "stratum", "phenomena": "phenomenon",
                 "media": "medium", "matrices": "matrix", "indices": "index", "vertices": "vertex",
                 "formulae": "formula", "analyses": "analysis", "hypotheses": "hypothesis", "theses": "thesis",
                 "syntheses": "synthesis", "bases": "basis", "axes": "axis", "series": "series", "species": "species"}
    if w in irregular:
        return irregular[w]
    if len(w) <= 3 or w.endswith(("ss", "us", "is", "ics", "ous", "sis")):
        return w
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith(("sses", "xes", "ches", "shes", "zes")):
        return w[:-2]
    if w.endswith("s"):
        return w[:-1]
    return w


# ------------------------------------------------------------------------------------------------------ tokens and chunks


def _fix_script(tok: str) -> tuple[str, str]:
    """(token, script) with script 'cyr' | 'lat' | 'mix'; repairs OCR homoglyph mixes («kерна», «сiльвинит»)."""
    if tok.isascii():
        return tok, "lat"
    nc = len(_CYR_RE.findall(tok))
    nl = len(_LAT_RE.findall(tok))
    if nc and not nl:
        return tok.translate(_UKR), "cyr"
    if nl and not nc:
        return tok, "lat"
    if nc >= nl:
        fixed = "".join(_LAT2CYR.get(ch, ch) for ch in tok)
        if not _LAT_RE.search(fixed):
            return fixed.translate(_UKR), "cyr"
    else:
        fixed = "".join(_CYR2LAT.get(ch, ch) for ch in tok)
        if not _CYR_RE.search(fixed):
            return fixed, "lat"
    return tok, "mix"


def _agree(adj_forms: frozenset, noun_forms: frozenset, gender: str | None) -> frozenset:
    out = set()
    for c, n in noun_forms:
        for ac, an, ag in adj_forms:
            if ac == c and an == n and (n == "plur" or ag is None or gender is None or gender == "ms-f" or ag == gender):
                out.add((c, n))
                break
    return frozenset(out)


def _ru_chunks(run: Sequence[_RuWord]) -> list[list[tuple[list[int], int, frozenset]]]:
    """Split a run of words into noun-phrase chunks: a head group (Adj* Noun) followed by genitive groups."""
    chunks: list[list[tuple[list[int], int, frozenset]]] = []
    cur: list[tuple[list[int], int, frozenset]] = []
    n = len(run)
    i = 0
    while i < n:
        w = run[i]
        if not w.nominal:
            if cur:
                chunks.append(cur)
                cur = []
            i += 1
            continue
        j = i
        adjs: list[int] = []
        while j + 1 < n and run[j].adj is not None and run[j + 1].nominal:
            nxt = run[j + 1]
            as_adj = run[j].noun is None or run[j].top_adj or (
                nxt.noun is not None and bool(_agree(run[j].adj[1], nxt.noun[2], nxt.noun[1])))
            if not as_adj:
                break
            adjs.append(j)
            j += 1
        if run[j].noun is None:                          # adjectives without a noun
            if cur:
                chunks.append(cur)
                cur = []
            i = j + 1
            continue
        noun = j
        lemma, gender, cases = run[noun].noun
        agreed: list[int] = []
        for a in reversed(adjs):
            c2 = _agree(run[a].adj[1], cases, gender)
            if not c2:
                break
            cases = c2
            agreed.append(a)
        agreed.reverse()
        if len(agreed) < len(adjs) and cur:              # a non-agreeing adjective breaks the chunk
            chunks.append(cur)
            cur = []
        if cur:
            gen = frozenset(x for x in cases if x[0] == "gent")
            if gen:
                cur.append((agreed, noun, gen))
            else:
                chunks.append(cur)
                cur = [(agreed, noun, cases)]
        else:
            cur = [(agreed, noun, cases)]
        i = noun + 1
    if cur:
        chunks.append(cur)
    return chunks


class Cand(NamedTuple):
    t0: int                 # first token index in the block
    t1: int                 # last token index (inclusive)
    key: str                # lemma key (``navigation.ids.norm_text`` form)
    surface: str
    lang: str
    n_words: int
    kind: str               # NP | ABBR
    head_cases: frozenset   # cases of the head noun group (empty when unknown)


class BlockAnalysis(NamedTuple):
    cands: list[Cand]
    definitions: list[tuple[str, str]]                       # (key, rule)
    same_as: list[tuple[str, str, str, str, str, str]]      # (key_a, lang_a, key_b, lang_b, surface_b, rule)


def _is_abbr(tok: str) -> bool:
    letters = [ch for ch in tok if ch.isalpha()]
    if not 2 <= len(letters) <= 8:
        return False
    upper = sum(1 for ch in letters if ch.isupper())
    if upper < 2 or upper * 2 < len(letters) or ROMAN_RE.match(tok):
        return False
    return tok.lower() not in UNITS_ABBR


def _tokenize(text: str) -> tuple[list[str], list[str]]:
    text = _MATH_RE.sub(" ¶ ", text)
    toks: list[str] = []
    scripts: list[str] = []
    for m in _TOKEN_RE.finditer(text):
        t = m.group()
        if t[0].isalpha():
            t, sc = _fix_script(t)
            toks.append(t)
            scripts.append(sc)
        else:
            toks.append(t)
            scripts.append("num" if t[0].isdigit() else "punct")
    return toks, scripts


def _block_language(toks: list[str], scripts: list[str], hint: str | None) -> str:
    cyr = sum(len(t) for t, s in zip(toks, scripts) if s == "cyr")
    lat = sum(len(t) for t, s in zip(toks, scripts) if s == "lat")
    if cyr + lat < 12 and hint in ("ru", "en", "de"):
        return hint
    if cyr >= lat:
        return "ru"
    low = [t.lower() for t, s in zip(toks, scripts) if s == "lat"]
    de = sum(1 for t in low if t in _DE_MARKERS)
    en = sum(1 for t in low if t in _EN_MARKERS)
    if de > en and de >= 2:
        return "de"
    if hint == "de" and de >= en:
        return "de"
    return "en"


def analyze_text(text: str, morph: Morphology, *, lang_hint: str | None = None,
                 patterns: bool = True) -> BlockAnalysis:
    """Candidates of one block (or a query/seed phrase), with definition and parenthesis patterns."""
    toks, scripts = _tokenize(text or "")
    n = len(toks)
    if n == 0:
        return BlockAnalysis([], [], [])
    lang = _block_language(toks, scripts, lang_hint)
    letters = [ch for t, s in zip(toks, scripts) if s in ("cyr", "lat") for ch in t]
    allcaps = len(letters) >= 8 and sum(1 for ch in letters if ch.isupper()) > 0.7 * len(letters)
    cands: list[Cand] = []
    run_idx: list[int] = []
    run_script = None

    def flush() -> None:
        if run_idx:
            if run_script == "cyr":
                cands.extend(_ru_candidates(toks, run_idx, morph))
            else:
                cands.extend(_latin_candidates(toks, run_idx, "de" if lang == "de" else "en", morph))
            run_idx.clear()

    for i, (t, sc) in enumerate(zip(toks, scripts)):
        if sc in ("cyr", "lat"):
            if not allcaps and _is_abbr(t):
                flush()
                run_script = None
                key = nav_ids.norm_text(t)
                if key:
                    cands.append(Cand(i, i, key, t, "ru" if sc == "cyr" else "en", 1, "ABBR", frozenset()))
                continue
            if sc == "cyr" and i > 0 and scripts[i - 1] == "cyr" and toks[i - 1].lower() in PREPOSITIONS_RU \
                    and t.lower() in COMPLEX_PREP_NOUNS_RU:
                flush()
                run_script = None
                continue
            if run_script is not None and sc != run_script:
                flush()
            run_script = sc
            run_idx.append(i)
        else:
            flush()
            run_script = None
    flush()
    if not patterns:
        return BlockAnalysis(cands, [], [])
    return BlockAnalysis(cands, _definitions(toks, scripts, cands), _same_as(toks, scripts, cands))


def _ru_candidates(toks: list[str], idx: list[int], morph: Morphology) -> list[Cand]:
    words = [toks[i].lower().replace("ё", "е") for i in idx]
    out: list[Cand] = []
    if morph.name == "pymorphy3":
        run = [morph.ru_word(w) for w in words]
        for chunk in _ru_chunks(run):
            for gi, (adjs, head, cases) in enumerate(chunk):
                head_lemma = run[head].noun[0]
                for s in adjs + [head]:
                    for gj in range(gi, len(chunk)):
                        end = chunk[gj][1]
                        nw = end - s + 1
                        if nw > MAX_WORDS:
                            break
                        if nw == 1 and (head_lemma in STOP_NOUNS_RU or len(head_lemma) < 3):
                            continue
                        if nw > 1 and head_lemma in HEAD_STOP_RU:
                            continue
                        lemmas = []
                        for p in range(s, end + 1):
                            wa = run[p]
                            is_noun = any(p == g[1] for g in chunk[gi:gj + 1])
                            lemmas.append(wa.noun[0] if is_noun else wa.adj[0])
                        key = nav_ids.norm_text(" ".join(lemmas))
                        if key:
                            out.append(Cand(idx[s], idx[end], key, " ".join(words[s:end + 1]), "ru", nw, "NP",
                                            cases if gi == 0 else frozenset()))
        return out
    # fallback without POS: runs of content words, sub-sequences of 1..3 words
    ok = [len(w) >= 3 and w not in PREPOSITIONS_RU and w not in STOP_WORDS_RU and w not in STOP_ADJ_RU
          and w not in UNITS_ABBR and not w.endswith(_RU_VERBISH) for w in words]
    stems = [morph.ru_stem(w) for w in words]
    for s in range(len(words)):
        if not ok[s]:
            continue
        for e in range(s, min(len(words), s + 3)):
            if not ok[e] or idx[e] - idx[s] != e - s:
                break
            if e == s and (words[s] in STOP_NOUNS_RU or stems[s] in STOP_NOUNS_RU):
                continue
            key = nav_ids.norm_text(" ".join(stems[s:e + 1]))
            if key:
                out.append(Cand(idx[s], idx[e], key, " ".join(words[s:e + 1]), "ru", e - s + 1, "NP", frozenset()))
    return out


def _latin_candidates(toks: list[str], idx: list[int], lang: str, morph: Morphology) -> list[Cand]:
    out: list[Cand] = []
    orig = [toks[i] for i in idx]
    low = [t.lower() for t in orig]
    if lang == "de":
        ok = [len(w) >= 3 and w not in STOP_DE and w not in UNITS_ABBR for w in low]
    else:
        ok = [len(w) >= 2 and w not in STOP_EN and w not in UNITS_ABBR and not ROMAN_RE.match(o)
              and not (w.endswith("ly") and len(w) > 4 and w not in EN_LY_NOUNS) for w, o in zip(low, orig)]
    n = len(low)
    for s in range(n):
        if not ok[s]:
            continue
        for e in range(s, min(n, s + MAX_WORDS)):
            if not ok[e] or idx[e] - idx[s] != e - s:
                break
            last = low[e]
            nw = e - s + 1
            if lang == "de":
                if not orig[e][:1].isupper() or nw > 3:
                    continue
                if nw == 1 and len(last) < 4:
                    continue
                key = nav_ids.norm_text(" ".join(morph.de_stem(w) for w in low[s:e + 1]))
            else:
                if len(last) < 3 or (last.endswith("ed") and last not in EN_ED_NOUNS):
                    continue
                head = _singular_en(last)
                if nw == 1 and (len(last) < 4 or head in STOP_NOUNS_EN or last in STOP_NOUNS_EN):
                    continue
                if nw > 1 and head in HEAD_STOP_EN:
                    continue
                key = nav_ids.norm_text(" ".join(low[s:e] + [head]))
            if key:
                out.append(Cand(idx[s], idx[e], key, " ".join(low[s:e + 1]), lang, nw, "NP", frozenset()))
    return out


def _sentence_first_word(toks: list[str], scripts: list[str], i: int) -> int:
    j = i
    while j > 0 and toks[j - 1] not in _SENT_END:
        j -= 1
    while j <= i and scripts[j] not in ("cyr", "lat"):
        j += 1
    return j


def _pick(cands: Iterable[Cand], *, case: str | None = None) -> Cand | None:
    best = None
    for c in cands:
        if case is not None and c.head_cases and not any(x[0] == case for x in c.head_cases):
            continue
        if best is None or c.n_words > best.n_words:
            best = c
    return best


def _definitions(toks: list[str], scripts: list[str], cands: list[Cand]) -> list[tuple[str, str]]:
    if not cands:
        return []
    by_end: dict[int, list[Cand]] = defaultdict(list)
    by_start: dict[int, list[Cand]] = defaultdict(list)
    for c in cands:
        if c.kind == "NP":
            by_end[c.t1].append(c)
            by_start[c.t0].append(c)
    out: list[tuple[str, str]] = []
    n = len(toks)
    low = [t.lower() for t in toks]

    def spanning(end: int, case: str | None) -> Cand | None:
        if end < 0:
            return None
        first = _sentence_first_word(toks, scripts, end)
        return _pick((c for c in by_end.get(end, ()) if c.t0 == first), case=case)

    for i, w in enumerate(low):
        if w == "это" and i >= 2 and toks[i - 1] in _DASHES:
            c = spanning(i - 2, "nomn")
            if c:
                out.append((c.key, "def_eto"))
        elif w in DEFINE_UNDERSTOOD_RU:
            for j in range(max(0, i - 7), i - 1):
                if low[j] == "под":
                    c = _pick((c for c in by_start.get(j + 1, ()) if c.t1 == i - 1), case="ablt")
                    if c:
                        out.append((c.key, "def_ponimaetsya"))
                    break
        elif w in DEFINE_CALLED_RU:
            c = _pick((c for c in by_start.get(i + 1, ())), case="ablt")
            if c is not None and c.head_cases:
                out.append((c.key, "def_nazyvaetsya"))
            else:
                c = spanning(i - 1, "ablt")
                if c is not None and c.head_cases:
                    out.append((c.key, "def_nazyvaetsya"))
        elif w == "(" and i + 1 < n and low[i + 1] == "от":
            close = next((k for k in range(i + 2, min(n, i + 25)) if toks[k] == ")"), None)
            if close is not None and close + 1 < n and toks[close + 1] in _DASHES:
                c = spanning(i - 1, "nomn")
                if c:
                    out.append((c.key, "def_etym"))
        elif w in ("is", "are") and i + 2 < n:
            if low[i + 1] in ("defined", "termed") and low[i + 2] == "as":
                c = spanning(i - 1, None)
                if c:
                    out.append((c.key, "def_en_defined_as"))
            elif low[i + 1] in ("called", "termed") or (low[i + 1] == "known" and low[i + 2] == "as"):
                k = i + 2 if low[i + 1] in ("called", "termed") else i + 3
                while k < n and low[k] in ("the", "a", "an"):
                    k += 1
                c = _pick(by_start.get(k, ()))
                if c:
                    out.append((c.key, "def_en_called"))
    return out


def _abbr_matches(abbr: str, long_surface: str) -> bool:
    a = [ch for ch in abbr.lower() if ch.isalpha()]
    words = [w for w in re.split(r"[\s\-‐‑]+", long_surface.lower()) if w]
    if not a or not words or len(words) > len(a) or a[0] != words[0][0]:
        return False
    k = 0
    for ch in "".join(words):
        if k < len(a) and ch == a[k]:
            k += 1
    return k == len(a)


def _same_as(toks: list[str], scripts: list[str], cands: list[Cand]) -> list[tuple[str, str, str, str, str, str]]:
    by_end: dict[int, list[Cand]] = defaultdict(list)
    by_span: dict[tuple[int, int], list[Cand]] = defaultdict(list)
    for c in cands:
        by_end[c.t1].append(c)
        by_span[(c.t0, c.t1)].append(c)
    out: list[tuple[str, str, str, str, str, str]] = []
    n = len(toks)
    for p, t in enumerate(toks):
        if t != "(" or p == 0:
            continue
        q = next((k for k in range(p + 1, min(n, p + 12)) if toks[k] in "()"), None)
        if q is None or toks[q] != ")":
            continue
        a = p + 1
        while a < q and (toks[a].lower().rstrip(".") in ("англ", "eng", "engl", "english", "нем", "лат", "англ.")
                         or toks[a] in (".", ":")):
            a += 1
        if a >= q or toks[a].lower() == "от" or any(scripts[k] not in ("cyr", "lat") for k in range(a, q)):
            continue
        inner = by_span.get((a, q - 1), [])
        left = by_end.get(p - 1, [])
        if not inner or not left:
            continue
        inner_c = max(inner, key=lambda c: (c.kind == "ABBR", c.n_words))
        inner_script = scripts[a]
        if inner_c.kind == "ABBR":
            same = [c for c in left if c.kind == "NP" and scripts[c.t0] == inner_script
                    and _abbr_matches(inner_c.surface, c.surface)]
            cross = [c for c in left if c.kind == "NP" and scripts[c.t0] != inner_script]
            pick = max(same, key=lambda c: c.n_words) if same else (
                max(cross, key=lambda c: c.n_words) if cross else None)
            if pick is not None:
                out.append((pick.key, pick.lang, inner_c.key, inner_c.lang, inner_c.surface, "paren_abbreviation"))
            continue
        left_abbr = [c for c in left if c.kind == "ABBR" and c.t0 == p - 1]
        if left_abbr:
            ab = left_abbr[0]
            if scripts[ab.t0] != inner_script or _abbr_matches(ab.surface, inner_c.surface):
                out.append((inner_c.key, inner_c.lang, ab.key, ab.lang, ab.surface, "paren_abbreviation"))
            continue
        other = [c for c in left if c.kind == "NP" and scripts[c.t0] != inner_script]
        if not other:
            continue
        exact = [c for c in other if c.n_words == inner_c.n_words]
        pick = exact[0] if exact else max(other, key=lambda c: c.n_words)
        out.append((pick.key, pick.lang, inner_c.key, inner_c.lang, inner_c.surface, "paren_translation"))
    return out


def phrase_keys(text: str, morph: Morphology | None = None) -> list[str]:
    """Lemma keys of a phrase (a query or a seed): the key spanning all words first, then the others by length."""
    morph = morph or Morphology()
    toks, scripts = _tokenize(text or "")
    words = [i for i, s in enumerate(scripts) if s in ("cyr", "lat")]
    if not words:
        return []
    res = analyze_text(text, morph, patterns=False)
    full = [c for c in res.cands if c.t0 == words[0] and c.t1 == words[-1]]
    rest = sorted({c.key for c in res.cands} - {c.key for c in full}, key=lambda k: (-len(k.split()), k))
    keys = [max(full, key=lambda c: c.n_words).key] if full else []
    if not keys:                                                   # no noun-phrase parse: lemmas/stems of all words
        lemmas = []
        for i in words:
            w = toks[i].lower().replace("ё", "е")
            if scripts[i] == "cyr":
                rw = morph.ru_word(w) if morph.name == "pymorphy3" else None
                lemmas.append(rw.noun[0] if rw is not None and rw.noun else (
                    rw.adj[0] if rw is not None and rw.adj else morph.ru_stem(w)))
            else:
                lemmas.append(_singular_en(w))
        k = nav_ids.norm_text(" ".join(lemmas))
        if k:
            keys.append(k)
    return keys + [k for k in rest if k not in keys]


# ---------------------------------------------------------------------------------------------------- worker processes

_WORKER_MORPH: Morphology | None = None


def _init_worker(mode: str) -> None:
    global _WORKER_MORPH  # noqa: PLW0603 — one analyser per worker process
    _WORKER_MORPH = Morphology(mode)


def _process_batch(payload: tuple[int, list[str], list[str | None], str]) -> dict[str, Any]:
    start, texts, langs, mode = payload
    global _WORKER_MORPH  # noqa: PLW0603
    if _WORKER_MORPH is None or (mode != "auto" and _WORKER_MORPH.name != mode):
        _WORKER_MORPH = Morphology(mode)
    morph = _WORKER_MORPH
    keys: dict[str, int] = {}
    meta: list[tuple[str, int, str]] = []
    occ_k: list[int] = []
    occ_b: list[int] = []
    occ_c: list[int] = []
    surf: Counter = Counter()
    defs: list[tuple[str, int, str]] = []
    same: list[tuple[str, str, str, str, str, int, str]] = []
    for off, text in enumerate(texts):
        bidx = start + off
        res = analyze_text(text, morph, lang_hint=langs[off])
        cnt: Counter = Counter()
        for c in res.cands:
            k = keys.get(c.key)
            if k is None:
                k = keys[c.key] = len(meta)
                meta.append((c.lang, c.n_words, c.kind))
            cnt[k] += 1
            surf[(k, c.surface)] += 1
        for k, v in cnt.items():
            occ_k.append(k)
            occ_b.append(bidx)
            occ_c.append(v)
        defs.extend((key, bidx, rule) for key, rule in res.definitions)
        same.extend((*row[:5], bidx, row[5]) for row in res.same_as)
    return {"keys": list(keys), "meta": meta, "occ": (np.asarray(occ_k, np.int32), np.asarray(occ_b, np.int32),
                                                       np.asarray(occ_c, np.int32)),
            "surf": [(k, s, v) for (k, s), v in surf.items()], "defs": defs, "same": same,
            "morphology": morph.name}


# --------------------------------------------------------------------------------------------------------------- build


def _table_exists(con: Any, schema: str, name: str) -> bool:
    try:
        return bool(con.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema = ? "
                                "AND table_name = ?", [schema, name]).fetchone()[0])
    except Exception:  # noqa: BLE001
        return False


def _canon(con: Any, name: str) -> str:
    return f"canonical.{name}" if _table_exists(con, "canonical", name) else name


def _load_blocks(con: Any) -> pa.Table:
    blocks, pages = _canon(con, "blocks"), _canon(con, "pages")
    types = ", ".join(f"'{t}'" for t in TEXT_BLOCK_TYPES)
    sql = f"""
        SELECT b.object_id AS block_id, b.page_id, b.source_id, coalesce(p.page_index, 0)::INTEGER AS page_index,
               coalesce(b.reading_order, 0)::INTEGER AS reading_order, b.block_type, b.normalized_text AS text
        FROM {blocks} b LEFT JOIN {pages} p ON p.page_id = b.page_id
        WHERE b.is_primary_layer AND b.block_type IN ({types})
          AND b.normalized_text IS NOT NULL AND length(trim(b.normalized_text)) > 0
        ORDER BY b.source_id, page_index, b.page_id, reading_order, b.object_id"""
    tbl = con.execute(sql).to_arrow_table()
    return tbl.append_column("block_idx", pa.array(np.arange(tbl.num_rows, dtype=np.int32)))


def _source_languages(con: Any) -> dict[str, str]:
    links, works = _canon(con, "source_work_links"), _canon(con, "works")
    try:
        rows = con.execute(f"SELECT l.source_id, w.languages FROM {links} l JOIN {works} w ON w.work_id = l.work_id "
                           "WHERE l.is_primary").fetchall()
    except Exception:  # noqa: BLE001 — a canon without works (tests, partial snapshots)
        return {}
    return {sid: langs[0] for sid, langs in rows if langs}


def _section_pages_table(section_pages: Any, con: Any) -> pa.Table | None:
    if section_pages is None:
        return None
    if isinstance(section_pages, str):
        return con.execute(f"SELECT section_id, page_id FROM {section_pages}").to_arrow_table()
    if isinstance(section_pages, pa.Table):
        return section_pages.select(["section_id", "page_id"])
    if hasattr(section_pages, "to_arrow_table"):                     # a DuckDB relation
        return section_pages.to_arrow_table().select(["section_id", "page_id"])
    if hasattr(section_pages, "to_arrow"):
        return section_pages.to_arrow().select(["section_id", "page_id"])
    rows = list(section_pages)
    return pa.table({"section_id": [r[0] for r in rows], "page_id": [r[1] for r in rows]})


def _run_extraction(blocks: pa.Table, src_lang: dict[str, str], *, workers: int, mode: str) -> list[dict[str, Any]]:
    texts = blocks.column("text").to_pylist()
    sources = blocks.column("source_id").to_pylist()
    langs = [src_lang.get(s) for s in sources]
    step = 2000
    payloads = [(i, texts[i:i + step], langs[i:i + step], mode) for i in range(0, len(texts), step)]
    if workers <= 1 or len(payloads) <= 1:
        return [_process_batch(p) for p in payloads]
    method = "fork" if "fork" in mp.get_all_start_methods() else "spawn"
    with ProcessPoolExecutor(max_workers=workers, mp_context=mp.get_context(method),
                             initializer=_init_worker, initargs=(mode,)) as ex:
        return list(ex.map(_process_batch, payloads, chunksize=1))


def _results_to_tables(results: list[dict[str, Any]]) -> dict[str, pa.Table]:
    occ, meta, surf, defs, same = [], [], [], [], []
    for r in results:
        keys = pa.array(r["keys"], pa.string())
        k, b, c = r["occ"]
        occ.append(pa.table({"key": keys.take(pa.array(k)), "block_idx": pa.array(b), "cnt": pa.array(c)}))
        meta.append(pa.table({"key": keys, "lang": [m[0] for m in r["meta"]],
                              "n_words": pa.array([m[1] for m in r["meta"]], pa.int16()),
                              "kind": [m[2] for m in r["meta"]]}))
        if r["surf"]:
            sk = pa.array([x[0] for x in r["surf"]], pa.int32())
            surf.append(pa.table({"key": keys.take(sk), "surface": [x[1] for x in r["surf"]],
                                  "cnt": pa.array([x[2] for x in r["surf"]], pa.int64())}))
        defs.extend(r["defs"])
        same.extend(r["same"])
    empty_occ = pa.table({"key": pa.array([], pa.string()), "block_idx": pa.array([], pa.int32()),
                          "cnt": pa.array([], pa.int32())})
    return {
        "occ": pa.concat_tables(occ) if occ else empty_occ,
        "meta": pa.concat_tables(meta) if meta else pa.table({"key": pa.array([], pa.string()),
                                                             "lang": pa.array([], pa.string()),
                                                             "n_words": pa.array([], pa.int16()),
                                                             "kind": pa.array([], pa.string())}),
        "surf": pa.concat_tables(surf) if surf else pa.table({"key": pa.array([], pa.string()),
                                                             "surface": pa.array([], pa.string()),
                                                             "cnt": pa.array([], pa.int64())}),
        "defs": pa.table({"key": pa.array([d[0] for d in defs], pa.string()),
                          "block_idx": pa.array([d[1] for d in defs], pa.int32()),
                          "rule": pa.array([d[2] for d in defs], pa.string())}),
        "same": pa.table({"key_a": pa.array([s[0] for s in same], pa.string()),
                          "lang_a": pa.array([s[1] for s in same], pa.string()),
                          "key_b": pa.array([s[2] for s in same], pa.string()),
                          "lang_b": pa.array([s[3] for s in same], pa.string()),
                          "surface_b": pa.array([s[4] for s in same], pa.string()),
                          "block_idx": pa.array([s[5] for s in same], pa.int32()),
                          "rule": pa.array([s[6] for s in same], pa.string())}),
    }


def _cvalues(keys: Sequence[str], tf: Sequence[int]) -> tuple[np.ndarray, list[tuple[int, int]], np.ndarray]:
    """C-value (log2(words + 1) variant, so that single words count), the nested (longer, shorter) pairs and, per
    key, the largest frequency of a longer key that contains it (``interferometric synthetic`` ⊂ ``… radar``)."""
    pos = {k: i for i, k in enumerate(keys)}
    parents: dict[int, list[int]] = defaultdict(list)
    nested: list[tuple[int, int]] = []
    for bi, b in enumerate(keys):
        toks = b.split()
        n = len(toks)
        if n < 2:
            continue
        seen: set[int] = set()
        for i in range(n):
            for j in range(i + 1, n + 1):
                if j - i == n:
                    continue
                ai = pos.get(" ".join(toks[i:j]))
                if ai is not None and ai not in seen:
                    seen.add(ai)
                    parents[ai].append(bi)
                    nested.append((bi, ai))
    cv = np.zeros(len(keys), dtype=np.float64)
    maxsup = np.zeros(len(keys), dtype=np.float64)
    for ai, a in enumerate(keys):
        w = math.log2(len(a.split()) + 1)
        ps = parents.get(ai)
        cv[ai] = w * (tf[ai] - (sum(tf[p] for p in ps) / len(ps) if ps else 0.0))
        maxsup[ai] = max((tf[p] for p in ps or ()), default=0)
    return cv, nested, maxsup


def _units_sql(max_pages: int | None, with_sections: bool) -> str:
    win = f"(row_number() OVER (PARTITION BY {{part}} ORDER BY page_index, page_id) - 1) // {int(max_pages)}" \
        if max_pages else "0"
    sections = f"""
        sec AS (SELECT sp.section_id, pg.source_id, pg.page_id, pg.page_index FROM sp JOIN pg USING (page_id)),
        secw AS (SELECT *, {win.format(part='section_id')} AS win FROM sec),""" if with_sections else ""
    rest_filter = "WHERE page_id NOT IN (SELECT page_id FROM sec)" if with_sections else ""
    sec_union = """
        SELECT 'SECTION' AS unit_kind, section_id::VARCHAR AS section_id, source_id, page_id, page_index,
               section_id || '#' || CAST(win AS VARCHAR) AS ukey FROM secw UNION ALL""" if with_sections else ""
    return f"""
    CREATE TABLE unit_pages AS
    WITH {sections}
    rest AS (SELECT * FROM pg {rest_filter}),
    src AS (SELECT source_id, bool_or(has_heading) AS any_heading FROM rest GROUP BY source_id),
    grp AS (
        SELECT r.*, s.any_heading,
               CASE WHEN s.any_heading
                    THEN sum(CASE WHEN r.has_heading THEN 1 ELSE 0 END) OVER (
                        PARTITION BY r.source_id ORDER BY r.page_index, r.page_id ROWS UNBOUNDED PRECEDING)
                    ELSE row_number() OVER (PARTITION BY r.source_id ORDER BY r.page_index, r.page_id) END AS g
        FROM rest r JOIN src s USING (source_id)),
    grpw AS (SELECT *, {win.format(part='source_id, g')} AS win FROM grp)
    {sec_union}
    SELECT CASE WHEN any_heading THEN 'HEADING_GROUP' ELSE 'PAGE' END AS unit_kind, NULL::VARCHAR AS section_id,
           source_id, page_id, page_index,
           source_id || '#' || CAST(g AS VARCHAR) || '#' || CAST(win AS VARCHAR) AS ukey FROM grpw"""


def _ensure_cuda_path() -> None:
    """cuDF ufuncs JIT-compile through CuPy, which needs the CUDA headers: in a conda RAPIDS env they live under
    ``<env>/targets/x86_64-linux``; point ``CUDA_PATH`` there when it is unset."""
    if os.environ.get("CUDA_PATH"):
        return
    for base in {sys.prefix, sys.base_prefix}:
        cand = os.path.join(base, "targets", "x86_64-linux")
        if os.path.exists(os.path.join(cand, "include", "cuda_runtime.h")):
            os.environ["CUDA_PATH"] = cand
            return


def _gpu_available() -> bool:
    _ensure_cuda_path()                         # before CuPy is imported: it resolves the CUDA path at import
    try:
        import cudf  # noqa: F401, PLC0415

        return True
    except Exception:  # noqa: BLE001
        return False


def _chunks_by_workload(t: np.ndarray, u: np.ndarray, n_terms: int, budget: int) -> list[tuple[int, int]]:
    per_unit = np.bincount(u)
    work = np.bincount(t, weights=per_unit[u], minlength=n_terms)
    out, lo, acc = [], 0, 0.0
    for i in range(n_terms):
        acc += work[i]
        if acc > budget and i > lo:
            out.append((lo, i))
            lo, acc = i, work[i]
    out.append((lo, n_terms))
    return out


_PAIRS_SQL = """
    WITH L AS (SELECT t, u, s FROM m WHERE t >= $lo AND t < $hi),
    P AS (
        SELECT L.t AS t1, R.t AS t2, count(*) AS c, count(DISTINCT L.s) AS ns
        FROM L JOIN m R ON R.u = L.u AND R.t <> L.t
        GROUP BY 1, 2
        HAVING count(*) >= $min_shared AND count(DISTINCT L.s) >= $min_sources),
    S AS (
        SELECT P.*, CASE WHEN P.c >= $n THEN 1.0 ELSE
            floor(ln((P.c::DOUBLE * $n) / (d1.df::DOUBLE * d2.df::DOUBLE)) / (-ln(P.c::DOUBLE / $n)) * 1e6 + 0.5) / 1e6
            END AS npmi
        FROM P JOIN d d1 ON d1.t = P.t1 JOIN d d2 ON d2.t = P.t2),
    R AS (SELECT *, floor(npmi * c / (c + 2.0) * 1e6 + 0.5) / 1e6 AS score FROM S
          WHERE npmi >= $min_npmi AND NOT EXISTS (SELECT 1 FROM nest WHERE nest.t1 = S.t1 AND nest.t2 = S.t2))
    SELECT t1, t2, c, ns, npmi FROM R
    QUALIFY row_number() OVER (PARTITION BY t1 ORDER BY score DESC, c DESC, t2) <= $k"""


def _pairs_duckdb(t, u, s, df, nested, n_units, p) -> pa.Table:
    import duckdb  # noqa: PLC0415

    db = duckdb.connect()
    try:
        db.register("m_in", pa.table({"t": t, "u": u, "s": s}))
        db.execute("CREATE TABLE m AS SELECT * FROM m_in")
        db.register("d_in", pa.table({"t": np.arange(len(df), dtype=np.int32), "df": df.astype(np.int64)}))
        db.execute("CREATE TABLE d AS SELECT * FROM d_in")
        db.register("n_in", pa.table({"t1": nested[:, 0].astype(np.int32), "t2": nested[:, 1].astype(np.int32)}))
        db.execute("CREATE TABLE nest AS SELECT * FROM n_in")
        parts = []
        for lo, hi in _chunks_by_workload(t, u, len(df), 60_000_000):
            parts.append(db.execute(_PAIRS_SQL, {
                "lo": lo, "hi": hi, "min_shared": p["min_shared_units"], "min_sources": p["min_edge_sources"],
                "n": float(n_units), "min_npmi": p["min_npmi"], "k": p["top_k"]}).to_arrow_table())
        return pa.concat_tables(parts) if parts else None
    finally:
        db.close()


def _pairs_cudf(t, u, s, df, nested, n_units, p) -> pa.Table:
    _ensure_cuda_path()
    import cudf  # noqa: PLC0415

    m = cudf.DataFrame({"t": t, "u": u, "s": s})
    right = m[["t", "u"]]
    d1 = cudf.DataFrame({"t1": np.arange(len(df), dtype=np.int32), "df1": df.astype(np.float64)})
    d2 = d1.rename(columns={"t1": "t2", "df1": "df2"})
    nest = cudf.DataFrame({"t1": nested[:, 0].astype(np.int32), "t2": nested[:, 1].astype(np.int32)})
    n = float(n_units)
    parts = []
    for lo, hi in _chunks_by_workload(t, u, len(df), 150_000_000):
        left = m[(m.t >= lo) & (m.t < hi)]
        j = left.merge(right, on="u", how="inner", suffixes=("1", "2"))
        j = j[j.t1 != j.t2]
        g = j.groupby(["t1", "t2"]).agg({"u": "count", "s": "nunique"}).reset_index()
        del j
        g = g.rename(columns={"u": "c", "s": "ns"})
        g = g[(g.c >= p["min_shared_units"]) & (g.ns >= p["min_edge_sources"])]
        if len(g) == 0:
            continue
        g = g.merge(d1, on="t1").merge(d2, on="t2")
        c = g.c.astype("float64")
        raw = np.log((c * n) / (g.df1 * g.df2)) / (-np.log(c / n))    # numpy ufuncs dispatch to cuDF
        npmi = np.floor(raw * 1e6 + 0.5) / 1e6
        g["npmi"] = npmi.where(g.c < n, 1.0)
        g = g[g.npmi >= p["min_npmi"]]
        g = g.merge(nest, on=["t1", "t2"], how="leftanti")
        cf = g.c.astype("float64")
        g["score"] = np.floor(g.npmi * cf / (cf + 2.0) * 1e6 + 0.5) / 1e6
        g = g.sort_values(["t1", "score", "c", "t2"], ascending=[True, False, False, True])
        g["rk"] = g.groupby("t1", sort=False).cumcount()
        g = g[g.rk < p["top_k"]]
        parts.append(g[["t1", "t2", "c", "ns", "npmi"]].to_arrow())
    return pa.concat_tables(parts) if parts else None


def _leiden(a: np.ndarray, b: np.ndarray, w: np.ndarray) -> dict[int, int] | None:
    try:
        import cudf  # noqa: PLC0415
        import cugraph  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    if len(a) == 0:
        return {}
    g = cugraph.Graph(directed=False)
    g.from_cudf_edgelist(cudf.DataFrame({"src": a, "dst": b, "w": w}), source="src", destination="dst",
                         edge_attr="w")
    parts, _ = cugraph.leiden(g, resolution=1.0, random_state=42)
    parts = parts.to_pandas()
    sizes = parts.groupby("partition").size().sort_values(ascending=False, kind="stable")
    renum = {int(c): i for i, c in enumerate(sizes.index)}
    return {int(v): renum[int(c)] for v, c in zip(parts["vertex"], parts["partition"])}


def _edge_id(kind: str, src: str, dst: str) -> str:
    import hashlib  # noqa: PLC0415

    return "TED-" + hashlib.sha256(f"vkm-nav-term-edge-v1|{kind}|{src}|{dst}".encode()).hexdigest()[:16]


TERMS_SCHEMA = pa.schema([
    ("term_id", pa.string()), ("lemma", pa.string()), ("lemma_key", pa.string()),
    ("surface_forms", pa.list_(pa.string())), ("language", pa.string()), ("n_words", pa.int16()),
    ("kind", pa.string()), ("df_units", pa.int32()), ("df_sources", pa.int32()), ("tf", pa.int64()),
    ("cvalue", pa.float64()), ("idf", pa.float64()), ("seed", pa.bool_()), ("community", pa.int32()),
    ("morphology", pa.string()), ("rule_version", pa.string()),
])
MENTIONS_SCHEMA = pa.schema([
    ("term_id", pa.string()), ("unit_id", pa.string()), ("unit_kind", pa.string()), ("section_id", pa.string()),
    ("source_id", pa.string()), ("tf", pa.int32()), ("tfidf", pa.float64()), ("page_ids", pa.list_(pa.string())),
    ("best_block_ids", pa.list_(pa.string())), ("rule_version", pa.string()),
])
EDGES_SCHEMA = pa.schema([
    ("edge_id", pa.string()), ("kind", pa.string()), ("src_term_id", pa.string()), ("dst_term_id", pa.string()),
    ("dst_ref", pa.string()), ("weight", pa.float64()), ("n_units", pa.int32()), ("n_sources", pa.int32()),
    ("examples", pa.list_(pa.string())), ("example_block_ids", pa.list_(pa.string())), ("rule", pa.string()),
    ("rule_version", pa.string()),
])


def build(con: Any, *, section_pages: Any = None, seeds: Iterable[str] | None = None,
          **options: Any) -> dict[str, pa.Table]:
    """Build ``terms``, ``term_mentions`` and ``term_edges`` from the canonical snapshot behind ``con`` (DuckDB).

    ``section_pages`` — N1 sections as (``section_id``, ``page_id``) rows (Arrow table, relation, table name or
    tuples); pages outside them fall back to heading groups. ``seeds`` — project phrases (default
    ``concepts_seeds.CORE_SEEDS``; ``()`` for none). Options and defaults: ``DEFAULTS``; unknown keyword arguments
    (the common arguments of ``vkm-corpus nav build``) are ignored. Build statistics are in the ``terms`` schema
    metadata under ``vkm_nav_concepts`` (JSON).
    """
    import duckdb  # noqa: PLC0415

    unknown = sorted(set(options) - set(DEFAULTS))
    if unknown:
        log.debug("concepts.build ignores %s", unknown)
    p = {k: options.get(k, v) for k, v in DEFAULTS.items()}
    timings: dict[str, float] = {}
    t_start = time.time()
    if seeds is None:
        from vkm_corpus.navigation.concepts_seeds import CORE_SEEDS  # noqa: PLC0415

        seeds = CORE_SEEDS
    morph = Morphology(p["morphology"])
    workers = p["workers"] if p["workers"] is not None else min(os.cpu_count() or 1, 16)
    backend = p["backend"]
    if backend == "auto":
        backend = "cudf" if _gpu_available() else "duckdb"

    blocks = _load_blocks(con)
    src_lang = _source_languages(con)
    sp = _section_pages_table(section_pages, con)
    timings["load"] = time.time() - t_start

    t0 = time.time()
    results = _run_extraction(blocks, src_lang, workers=workers, mode=morph.name)
    raw = _results_to_tables(results)
    morph_names = {r["morphology"] for r in results} or {morph.name}
    if len(morph_names) > 1:
        raise RuntimeError(f"workers used different morphology backends: {sorted(morph_names)}")
    timings["extract"] = time.time() - t0

    t0 = time.time()
    seed_keys: dict[str, str] = {}
    for phrase in seeds:
        ks = phrase_keys(phrase, morph)
        if ks:
            seed_keys.setdefault(ks[0], phrase)

    w = duckdb.connect()
    try:
        w.register("blocks_in", blocks.drop_columns(["text"]))
        w.execute("CREATE TABLE b AS SELECT * FROM blocks_in")
        w.execute("""CREATE TABLE pg AS SELECT source_id, page_id, min(page_index) AS page_index,
                     bool_or(block_type IN ('HEADING', 'TITLE')) AS has_heading FROM b GROUP BY source_id, page_id""")
        if sp is not None:
            w.register("sp_in", sp)
            w.execute("CREATE TABLE sp AS SELECT DISTINCT section_id, page_id FROM sp_in")
        w.execute(_units_sql(p["max_unit_pages"], sp is not None))
        w.execute("""
            CREATE TABLE units AS
            WITH u AS (SELECT ukey, any_value(unit_kind) AS unit_kind, any_value(section_id) AS section_id,
                              any_value(source_id) AS source_id, arg_min(page_id, page_index) AS first_page_id,
                              count(*) AS n_pages
                       FROM unit_pages GROUP BY ukey),
                 u2 AS (SELECT *, 'NCU-' || left(sha256('vkm-nav-concept-unit-v1|' || unit_kind || '|'
                                                        || coalesce(section_id, '') || '|' || source_id || '|'
                                                        || first_page_id), 16) AS unit_id FROM u)
            SELECT *, (row_number() OVER (ORDER BY unit_id) - 1)::INTEGER AS unit_idx,
                   (dense_rank() OVER (ORDER BY source_id) - 1)::INTEGER AS src_idx
            FROM u2""")
        w.execute("""CREATE TABLE bu AS SELECT DISTINCT b.block_idx, u.unit_idx, u.src_idx
                     FROM b JOIN unit_pages up ON up.page_id = b.page_id JOIN units u ON u.ukey = up.ukey""")
        n_units = w.execute("SELECT count(*) FROM units").fetchone()[0]

        w.register("occ_in", raw["occ"])
        w.execute("CREATE TABLE occ AS SELECT * FROM occ_in")
        w.register("meta_in", raw["meta"])
        w.execute("""CREATE TABLE km AS
                     WITH l AS (SELECT key, lang, count(*) AS c FROM meta_in GROUP BY key, lang),
                          lw AS (SELECT key, lang FROM l
                                 QUALIFY row_number() OVER (PARTITION BY key ORDER BY c DESC, lang) = 1)
                     SELECT m.key, any_value(lw.lang) AS lang, max(m.n_words) AS n_words, max(m.kind) AS kind
                     FROM meta_in m JOIN lw USING (key) GROUP BY m.key""")
        w.register("seed_in", pa.table({"key": pa.array(list(seed_keys), pa.string()),
                                        "phrase": pa.array(list(seed_keys.values()), pa.string())}))
        w.execute("CREATE TABLE seeds AS SELECT * FROM seed_in")
        w.execute("""
            CREATE TABLE ks AS
            WITH a AS (SELECT o.key, sum(o.cnt) AS tf, count(DISTINCT b.source_id) AS df_sources
                       FROM occ o JOIN b USING (block_idx) GROUP BY o.key),
                 u AS (SELECT o.key, count(DISTINCT bu.unit_idx) AS df_units
                       FROM occ o JOIN bu USING (block_idx) GROUP BY o.key)
            SELECT a.key, a.tf, coalesce(u.df_units, 0) AS df_units, a.df_sources,
                   (a.key IN (SELECT key FROM seeds)) AS seed
            FROM a LEFT JOIN u USING (key)""")
        n_candidates = w.execute("SELECT count(*) FROM ks").fetchone()[0]
        pre = w.execute("""SELECT key, tf, seed FROM ks WHERE (df_units >= $u AND df_sources >= $s) OR seed
                           ORDER BY key""", {"u": p["min_df_units"], "s": p["min_df_sources"]}).fetchall()
        keys = [r[0] for r in pre]
        cv, _, maxsup = _cvalues(keys, [r[1] for r in pre])
        share = float(p["max_nested_share"])
        keep = [i for i, r in enumerate(pre) if r[2] or (cv[i] > 0 and maxsup[i] < share * r[1])]
        if p["max_terms"]:
            order = sorted(keep, key=lambda i: (not pre[i][2], -cv[i], keys[i]))
            keep = sorted(order[: int(p["max_terms"])])
        kept_keys = [keys[i] for i in keep]
        kept_tf = [pre[i][1] for i in keep]
        _, nested_pairs, _ = _cvalues(kept_keys, kept_tf)   # nesting inside the kept vocabulary
        cvalue = {k: float(cv[i]) for k, i in zip(kept_keys, keep)}
        term_ids = [nav_ids.term_id(k) for k in kept_keys]
        order = sorted(range(len(kept_keys)), key=lambda i: term_ids[i])
        idx_of = {kept_keys[i]: r for r, i in enumerate(order)}
        w.register("kept_in", pa.table({
            "key": pa.array([kept_keys[i] for i in order], pa.string()),
            "term_id": pa.array([term_ids[i] for i in order], pa.string()),
            "term_idx": pa.array(np.arange(len(order), dtype=np.int32)),
            "cvalue": pa.array([cvalue[kept_keys[i]] for i in order], pa.float64())}))
        w.execute("CREATE TABLE kept AS SELECT * FROM kept_in")
        timings["filter"] = time.time() - t0

        # ---- mentions
        t0 = time.time()
        w.execute("""
            CREATE TABLE mo AS
            SELECT k.term_idx, bu.unit_idx, o.block_idx, o.cnt, b.block_id, b.page_id, b.page_index, b.reading_order
            FROM occ o JOIN kept k USING (key) JOIN b USING (block_idx) JOIN bu USING (block_idx)""")
        w.execute("""
            CREATE TABLE mentions AS
            WITH pages AS (SELECT term_idx, unit_idx, page_id, min(page_index) AS pi, sum(cnt) AS c
                           FROM mo GROUP BY term_idx, unit_idx, page_id),
                 bb AS (SELECT term_idx, unit_idx, block_id, row_number() OVER (
                            PARTITION BY term_idx, unit_idx ORDER BY cnt DESC, page_index, reading_order, block_id) AS rk
                        FROM mo QUALIFY rk <= 3),
                 agg AS (SELECT term_idx, unit_idx, sum(c)::INTEGER AS tf,
                                list(page_id ORDER BY pi, page_id)[1:10] AS page_ids
                         FROM pages GROUP BY term_idx, unit_idx),
                 best AS (SELECT term_idx, unit_idx, list(block_id ORDER BY rk) AS best_block_ids
                          FROM bb GROUP BY term_idx, unit_idx)
            SELECT agg.*, best.best_block_ids FROM agg JOIN best USING (term_idx, unit_idx)""")
        w.execute("""
            CREATE TABLE tstat AS
            SELECT k.term_idx, k.term_id, k.key, k.cvalue, ks.tf, ks.df_units, ks.df_sources, ks.seed,
                   ln($n / ks.df_units::DOUBLE) AS idf
            FROM kept k JOIN ks USING (key)""", {"n": float(max(n_units, 1))})
        timings["mentions"] = time.time() - t0

        # ---- CO_OCCURS
        t0 = time.time()
        m = w.execute("""SELECT mm.term_idx AS t, mm.unit_idx AS u, bu_src.src_idx AS s
                         FROM mentions mm JOIN (SELECT DISTINCT unit_idx, src_idx FROM bu) bu_src USING (unit_idx)
                         JOIN tstat ts USING (term_idx) WHERE ts.df_units >= $k
                         ORDER BY t, u""", {"k": p["min_shared_units"]}).to_arrow_table()
        df_arr = np.zeros(len(order), dtype=np.int64)
        for ti, dfu in w.execute("SELECT term_idx, df_units FROM tstat").fetchall():
            df_arr[ti] = dfu
        nested = np.array([(idx_of[kept_keys[a]], idx_of[kept_keys[b]]) for a, b in nested_pairs]
                          + [(idx_of[kept_keys[b]], idx_of[kept_keys[a]]) for a, b in nested_pairs],
                          dtype=np.int32).reshape(-1, 2)
        tt = m.column("t").to_numpy().astype(np.int32)
        uu = m.column("u").to_numpy().astype(np.int32)
        ss = m.column("s").to_numpy().astype(np.int32)
        pairs = None
        if len(tt):
            if backend == "cudf":
                try:
                    pairs = _pairs_cudf(tt, uu, ss, df_arr, nested, n_units, p)
                except Exception as exc:  # noqa: BLE001 — GPU out of memory, driver trouble: CPU path
                    log.warning("cuDF pair counting failed (%s); falling back to DuckDB", exc)
                    backend = "duckdb"
            if backend == "duckdb":
                pairs = _pairs_duckdb(tt, uu, ss, df_arr, nested, n_units, p)
        if pairs is None:
            pairs = pa.table({"t1": pa.array([], pa.int32()), "t2": pa.array([], pa.int32()),
                              "c": pa.array([], pa.int64()), "ns": pa.array([], pa.int64()),
                              "npmi": pa.array([], pa.float64())})
        w.register("pairs_in", pairs)
        w.execute("""CREATE TABLE co AS
                     SELECT least(t1, t2)::INTEGER AS a, greatest(t1, t2)::INTEGER AS b, max(npmi) AS npmi,
                            max(c) AS c FROM pairs_in GROUP BY 1, 2""")
        w.execute("""
            CREATE TABLE co_ex AS
            WITH j AS (
                SELECT co.a, co.b, ma.unit_idx, least(ma.tf, mb.tf) AS score, bu_src.src_idx,
                       coalesce(list_intersect(ma.page_ids, mb.page_ids)[1], ma.page_ids[1]) AS page_id
                FROM co JOIN mentions ma ON ma.term_idx = co.a
                JOIN mentions mb ON mb.term_idx = co.b AND mb.unit_idx = ma.unit_idx
                JOIN (SELECT DISTINCT unit_idx, src_idx FROM bu) bu_src ON bu_src.unit_idx = ma.unit_idx),
            r AS (SELECT *, row_number() OVER (PARTITION BY a, b ORDER BY score DESC, unit_idx) AS rk FROM j)
            SELECT a, b, count(*)::INTEGER AS n_units, count(DISTINCT src_idx)::INTEGER AS n_sources,
                   list(page_id ORDER BY rk) FILTER (WHERE rk <= 3) AS examples
            FROM r GROUP BY a, b""")
        timings["cooccurrence"] = time.time() - t0

        # ---- communities (GPU only)
        t0 = time.time()
        community: dict[int, int] | None = None
        if p["communities"] is True or (p["communities"] == "auto" and backend == "cudf"):
            co_rows = w.execute("SELECT a, b, npmi FROM co ORDER BY a, b").to_arrow_table()
            try:
                community = _leiden(co_rows.column("a").to_numpy().astype(np.int32),
                                    co_rows.column("b").to_numpy().astype(np.int32),
                                    co_rows.column("npmi").to_numpy().astype(np.float64))
            except Exception as exc:  # noqa: BLE001
                log.warning("Leiden communities failed (%s)", exc)
                community = None
        timings["communities"] = time.time() - t0

        # ---- display forms
        t0 = time.time()
        surf = raw["surf"]
        w.register("surf_in", surf)
        top = w.execute("""
            WITH s AS (SELECT key, surface, sum(cnt) AS c FROM surf_in WHERE key IN (SELECT key FROM kept)
                       GROUP BY key, surface),
                 r AS (SELECT *, row_number() OVER (PARTITION BY key ORDER BY c DESC, surface) AS rk FROM s)
            SELECT key, list(surface ORDER BY rk) FILTER (WHERE rk <= 5) AS forms FROM r GROUP BY key""").fetchall()
        forms = {k: f for k, f in top}
        meta = {k: (lang, nw, kind) for k, lang, nw, kind in w.execute("SELECT key, lang, n_words, kind FROM km "
                                                                       "WHERE key IN (SELECT key FROM kept)").fetchall()}
        stats = {r[0]: r for r in w.execute("SELECT term_idx, term_id, key, cvalue, tf, df_units, df_sources, seed, idf "
                                            "FROM tstat ORDER BY term_idx").fetchall()}
        rows_terms: dict[str, list] = {f.name: [] for f in TERMS_SCHEMA}
        for ti in range(len(order)):
            _, tid, key, cval, tf, dfu, dfs, seed, idf = stats[ti]
            lang, nw, kind = meta.get(key, ("ru", len(key.split()), "NP"))
            fl = forms.get(key) or [key]
            lemma = fl[0]
            if lang == "ru" and kind == "NP":
                lemma = morph.nominative(lemma)
            rows_terms["term_id"].append(tid)
            rows_terms["lemma"].append(lemma)
            rows_terms["lemma_key"].append(key)
            rows_terms["surface_forms"].append(list(fl))
            rows_terms["language"].append(lang)
            rows_terms["n_words"].append(int(nw))
            rows_terms["kind"].append(kind)
            rows_terms["df_units"].append(int(dfu))
            rows_terms["df_sources"].append(int(dfs))
            rows_terms["tf"].append(int(tf))
            rows_terms["cvalue"].append(round(float(cval), 6))
            rows_terms["idf"].append(round(float(idf), 6))
            rows_terms["seed"].append(bool(seed))
            rows_terms["community"].append(community.get(ti) if community is not None else None)
            rows_terms["morphology"].append(morph.name)
            rows_terms["rule_version"].append(RULE_VERSION)
        timings["display"] = time.time() - t0

        # ---- tables
        t0 = time.time()
        mentions = w.execute("""
            SELECT ts.term_id, u.unit_id, u.unit_kind, u.section_id, u.source_id, mm.tf,
                   round(mm.tf * ts.idf, 6) AS tfidf, mm.page_ids, mm.best_block_ids, $rv AS rule_version
            FROM mentions mm JOIN tstat ts USING (term_idx) JOIN units u USING (unit_idx)
            ORDER BY ts.term_id, u.unit_id""", {"rv": RULE_VERSION}).to_arrow_table().cast(MENTIONS_SCHEMA)

        w.register("defs_in", raw["defs"])
        w.register("same_in", raw["same"])
        w.register("nested_in", pa.table({"sup": pa.array([idx_of[kept_keys[a]] for a, _ in nested_pairs], pa.int32()),
                                          "sub": pa.array([idx_of[kept_keys[b]] for _, b in nested_pairs], pa.int32())}))
        edges = w.execute("""
            WITH co_e AS (
                SELECT 'CO_OCCURS' AS kind, ta.term_id AS src, tb.term_id AS dst, NULL::VARCHAR AS dst_ref,
                       co.npmi AS weight, x.n_units, x.n_sources, x.examples, []::VARCHAR[] AS example_block_ids,
                       'npmi_units' AS rule
                FROM co JOIN co_ex x USING (a, b) JOIN tstat ta ON ta.term_idx = co.a JOIN tstat tb ON tb.term_idx = co.b),
            def_e AS (
                SELECT 'DEFINED_AS' AS kind, k.term_id AS src, NULL::VARCHAR AS dst, b.block_id AS dst_ref,
                       1.0::DOUBLE AS weight, 1 AS n_units, 1 AS n_sources, [b.page_id] AS examples,
                       [b.block_id] AS example_block_ids,
                       min(d.rule) AS rule
                FROM defs_in d JOIN kept k USING (key) JOIN b USING (block_idx)
                GROUP BY k.term_id, b.block_id, b.page_id),
            same_raw AS (
                SELECT CASE WHEN ka.term_id IS NULL OR (kb.term_id IS NOT NULL AND kb.term_id < ka.term_id)
                            THEN kb.term_id ELSE ka.term_id END AS src,
                       CASE WHEN ka.term_id IS NULL OR (kb.term_id IS NOT NULL AND kb.term_id < ka.term_id)
                            THEN ka.term_id ELSE kb.term_id END AS dst,
                       CASE WHEN ka.term_id IS NULL THEN s.lang_a || ':' || s.key_a
                            WHEN kb.term_id IS NULL THEN s.lang_b || ':' || s.key_b END AS dst_ref,
                       s.rule, s.block_idx, b.block_id, b.page_id, bu.unit_idx, bu.src_idx
                FROM same_in s LEFT JOIN kept ka ON ka.key = s.key_a LEFT JOIN kept kb ON kb.key = s.key_b
                JOIN b USING (block_idx) JOIN (SELECT block_idx, min(unit_idx) AS unit_idx, min(src_idx) AS src_idx
                                               FROM bu GROUP BY block_idx) bu USING (block_idx)
                WHERE (ka.term_id IS NOT NULL OR kb.term_id IS NOT NULL) AND s.key_a <> s.key_b),
            same_e AS (
                SELECT 'SAME_AS' AS kind, src, dst, dst_ref, count(*)::DOUBLE AS weight,
                       count(DISTINCT unit_idx) AS n_units, count(DISTINCT src_idx) AS n_sources,
                       list(DISTINCT page_id ORDER BY page_id)[1:3] AS examples,
                       list(DISTINCT block_id ORDER BY block_id)[1:3] AS example_block_ids, min(rule) AS rule
                FROM same_raw GROUP BY src, dst, dst_ref),
            cont_e AS (
                SELECT 'CONTAINS' AS kind, ta.term_id AS src, tb.term_id AS dst, NULL::VARCHAR AS dst_ref,
                       round(ta.tf::DOUBLE / tb.tf, 6) AS weight, ta.df_units AS n_units, ta.df_sources AS n_sources,
                       []::VARCHAR[] AS examples, []::VARCHAR[] AS example_block_ids, 'lexical_containment' AS rule
                FROM nested_in n JOIN tstat ta ON ta.term_idx = n.sup JOIN tstat tb ON tb.term_idx = n.sub)
            SELECT * FROM co_e UNION ALL SELECT * FROM def_e UNION ALL SELECT * FROM same_e
            UNION ALL SELECT * FROM cont_e""").to_arrow_table()
        edge_ids = [_edge_id(k, s, d if d is not None else r) for k, s, d, r in zip(
            edges.column("kind").to_pylist(), edges.column("src").to_pylist(), edges.column("dst").to_pylist(),
            edges.column("dst_ref").to_pylist())]
        edges = pa.table({
            "edge_id": edge_ids, "kind": edges.column("kind"), "src_term_id": edges.column("src"),
            "dst_term_id": edges.column("dst"), "dst_ref": edges.column("dst_ref"),
            "weight": edges.column("weight"), "n_units": edges.column("n_units"),
            "n_sources": edges.column("n_sources"), "examples": edges.column("examples"),
            "example_block_ids": edges.column("example_block_ids"), "rule": edges.column("rule"),
            "rule_version": pa.array([RULE_VERSION] * edges.num_rows, pa.string())}).cast(EDGES_SCHEMA)
        edges = edges.sort_by([("kind", "ascending"), ("src_term_id", "ascending"), ("dst_term_id", "ascending"),
                               ("dst_ref", "ascending")])
        timings["tables"] = time.time() - t0
        unit_counts = dict(w.execute("SELECT unit_kind, count(*) FROM units GROUP BY 1").fetchall())
    finally:
        w.close()

    terms = pa.table(rows_terms, schema=TERMS_SCHEMA)
    kinds = Counter(edges.column("kind").to_pylist())
    info = {
        "rule_version": RULE_VERSION, "morphology": morph.name, "backend": backend,
        "communities": community is not None, "params": {k: p[k] for k in DEFAULTS if k != "workers"},
        "counts": {"blocks": blocks.num_rows, "units": n_units, "unit_kinds": unit_counts,
                   "candidates": n_candidates, "terms": terms.num_rows,
                   "terms_by_language": dict(Counter(terms.column("language").to_pylist())),
                   "seed_terms": int(sum(terms.column("seed").to_pylist())), "mentions": mentions.num_rows,
                   "edges": dict(kinds)},
        "timings_s": {k: round(v, 2) for k, v in timings.items()},
    }
    terms = terms.replace_schema_metadata({METADATA_KEY: json.dumps(info, ensure_ascii=False).encode("utf-8")})
    return {"terms": terms, "term_mentions": mentions, "term_edges": edges}


def build_info(terms: pa.Table) -> dict[str, Any]:
    """Build statistics stored by ``build`` in the ``terms`` schema metadata."""
    md = terms.schema.metadata or {}
    return json.loads(md.get(METADATA_KEY, b"{}"))
