"""Query intent cues for retrieval routing: bibliographic queries (agent L) and visual queries (agent VIS).

**Bibliographic intent** routes a hybrid query additionally to the BIB_ENTRY units (the reference-list entries),
because under CP-42 those units never rank pages; V1 (``benchmarks/retrieval_v1``) showed that without them the served
scheme loses on bibliographic questions (0.597 vs 0.798 nDCG@10 on 7 queries). The detector is rules only,
deterministic and explainable (every decision lists its cues); it never looks at the corpus.

* **Strong cues** (one is enough): a DOI or an ISBN; «список литературы / публикаций / трудов», «библиография»,
  «литература о …», «references to», «bibliography»; a citation phrase («ссылки на», «цитирует», «cited by»);
  «et al.», «и др.», «и соавт.»; a surname with initials («Барях А.А.», «А.А. Барях», «Baryakh A.»); works of an
  author («работы Баряха», «труды Кудряшова», «articles by Baryakh»); «Journal of …», «Proceedings of …»; a quoted
  title of three or more words («Указания по защите рудников от затопления»).
* **Weak cues** (two are needed): a surname with a year («Барях, 2010»), a periodical word («журнал», «вестник»,
  «известия», «записки»), a normative reference (ГОСТ, СНиП, СП, ISO, ASTM).

Eponyms are not authors: «закон Бингама», «ядро Абеля», «критерий Кулона–Мора» carry no cue; «горные работы» is not
«работы <автора>»; place and organisation adjectives («Верхнекамского», «Соликамской») never count as surnames.

**Visual intent** (agent VIS) routes a hybrid query additionally to the page-image channel (Qwen3-VL-Embedding-2B page
vectors): V2 (``benchmarks/retrieval_v2``) measured +0.109 nDCG@10 on the 42 visual queries for RRF(E, VIS), but −0.043
on text queries — so the channel is a route, not a default. One cue is enough; a cue is a word that names a kind of
picture («рисунок», «рис.», «схема», «карта», «план», «разрез», «график», «профиль», «радарограмма», «фото», «снимок»,
«таблица», «диаграмма», «изолинии», «эпюра», «чертёж», «выкопировка», «планшет», «интерферограмма», геологическая
«колонка»; EN figure, map, plan, cross-section, graph, chart, plot, diagram, scheme, profile, radargram, photo, image,
table, layout, drawing, contour, interferogram). False friends carry no cue: «технологическая карта», «план
мероприятий / действий / развития», «фотограмметрия», «картина», «картирование», «профилактика», «планирование»,
«площадь сечения». The word list was fixed before the detector was evaluated on the V1 queries (MODEL_CHOICE).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_CAP_RU = r"[А-ЯЁ][а-яё]{2,}(?:-[А-ЯЁ][а-яё]{2,})?"
_CAP_EN = r"[A-Z][a-z]{2,}(?:-[A-Z][a-z]{2,})?"
# adjectives of places and organisations look like capitalised surnames in the genitive
_ADJECTIVE_END = re.compile(r"(?:ск|цк)(?:ий|ая|ое|ие|ого|ой|ом|их|ому|ими|ую|им)$"
                            r"|(?:ный|ная|ное|ные|ного|ной|ном|ных|ному|ным)$")

STRONG_PATTERNS: dict[str, re.Pattern[str]] = {
    "doi": re.compile(r"\b10\.\d{4,9}/[^\s\"«»<>]+", re.I),
    "isbn": re.compile(r"\bISBN(?:-1[03])?[\s:]*(?:97[89][\s-]?)?\d[\d\s-]{7,15}[\dXx]\b"
                       r"|\b97[89]-\d{1,5}-\d{1,7}-\d{1,7}-\d\b", re.I),
    "bibliography_phrase": re.compile(
        r"\bспис(?:ок|к\w*)\s+(?:(?:использованн|цитируем|рекомендуем)\w*\s+)?(?:литератур|источник|публикац|трудов|работ)\w*"
        r"|\bбиблиограф\w*|\bперечень\s+(?:публикац|трудов|работ)\w*"
        r"|^\s*литератур\w*\b|\bлитератур(?:а|у|ы)\s+(?:о|об|по|про|для)\b"
        r"|\breferences?\s+(?:to|list|on)\b|\breference\s+list\b|\bbibliograph\w*|\bliterature\s+(?:on|about)\b",
        re.I),
    "citation_phrase": re.compile(r"\bссылк\w*\s+на\b|\bцитир\w+|\bcited\s+by\b|\bcitations?\s+(?:of|to)\b", re.I),
    "et_al": re.compile(r"\bet\s+al\b\.?|(?<!\w)и\s+др\.|(?<!\w)и\s+соавт\w*", re.I),
    "journal_of": re.compile(r"\bjournal\s+of\b|\bproceedings\s+of\b|\btransactions\s+(?:of|on)\b", re.I),
}
# «Барях А.А.» / «А.А. Барях» / «Baryakh, A.» / «A. Baryakh» — the capitalised word is group 1 or 2
_INITIALS = (
    re.compile(rf"(?<!\w)({_CAP_RU}),?\s+[А-ЯЁ]\.\s?(?:[А-ЯЁ]\.)?(?!\w)|(?<!\w)[А-ЯЁ]\.\s?(?:[А-ЯЁ]\.\s?)?({_CAP_RU})\b"),
    re.compile(rf"\b({_CAP_EN}),?\s+[A-Z]\.(?:\s?[A-Z]\.)?(?!\w)|\b[A-Z]\.\s?(?:[A-Z]\.\s?)?({_CAP_EN})\b"),
)
_WORKS_RU = re.compile(r"(?<![а-яёА-ЯЁ])((?i:работ[ыа]?|труд(?:ы|ов)|стат(?:ья|ьи|ей)|публикаци(?:я|и|й)"
                       r"|монографи(?:я|и|й)|книг[аиу]?|учебник(?:и|ов)?|диссертаци(?:я|и|й)|пособи(?:е|я)))\s+"
                       rf"(?:[А-ЯЁ]\.\s?){{0,2}}({_CAP_RU})")
_WORKS_EN = re.compile(r"\b(?i:articles?|papers?|works?|publications?|books?|monographs?|studies)\s+(?i:by|of)\s+"
                       rf"({_CAP_EN})")
_QUOTED = re.compile(r"[«\"„“]([^«»\"„“”]{12,})[»\"”“]")
_MINING_WORKS = re.compile(r"(?:горн|очистн|подготовительн|закладочн|буровзрывн|проходческ|строительн|ремонтн|"
                           r"маркшейдерск|полев|камеральн|опытн|экспериментальн)\w*\s+$", re.I)

WEAK_PATTERNS: dict[str, re.Pattern[str]] = {
    "author_year": re.compile(rf"(?<!\w)({_CAP_RU}|{_CAP_EN})\s*[(,]?\s*(?:18|19|20)\d{{2}}\)?(?!\d)"),
    "periodical": re.compile(r"(?<!\w)(?:журнал\w*|вестник\w*|известия|записки|сборник\w*\s+трудов"
                             r"|трудах?\s+конференц\w*|journal|proceedings|bulletin)(?!\w)", re.I),
    "standard": re.compile(r"(?<!\w)(?:ГОСТ|СНиП|СП\s+\d|ISO\s+\d|ASTM\s+[A-Z]?\d|ОСТ\s+\d|РД\s+\d)"),
}


@dataclass(frozen=True)
class Intent:
    bibliographic: bool
    cues: tuple[str, ...] = field(default_factory=tuple)
    weak: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, object]:
        return {"bibliographic": self.bibliographic, "cues": list(self.cues), "weak_cues": list(self.weak)}


def surname_like(word: str) -> bool:
    """A capitalised word that is not a place/organisation adjective («Верхнекамского», «Соликамской»)."""
    return bool(word) and not _ADJECTIVE_END.search(word.lower().split("-")[-1])


def bibliographic_intent(query: str) -> Intent:
    """Rule-based bibliographic intent of a query: fires on one strong cue or on two weak cues."""
    text = query or ""
    strong = {name for name, rx in STRONG_PATTERNS.items() if rx.search(text)}
    for rx in _INITIALS:
        if any(surname_like(m.group(1) or m.group(2) or "") for m in rx.finditer(text)):
            strong.add("author_initials")
    for m in _WORKS_RU.finditer(text):
        if surname_like(m.group(2)) and not _MINING_WORKS.search(text[:m.start(1)]):
            strong.add("works_of_author")
            break
    if any(surname_like(m.group(1)) for m in _WORKS_EN.finditer(text)):
        strong.add("works_of_author")
    if any(len(m.group(1).split()) >= 3 for m in _QUOTED.finditer(text)):
        strong.add("quoted_title")
    weak: list[str] = []
    for name, rx in WEAK_PATTERNS.items():
        matches = list(rx.finditer(text))
        if name == "author_year":
            matches = [m for m in matches if surname_like(m.group(1))]
        if matches:
            weak.append(name)
    return Intent(bool(strong) or len(weak) >= 2, tuple(sorted(strong)), tuple(weak))


# ------------------------------------------------------------------------------------------------ visual intent
_RU_END = r"(?![а-яё])"
_W = r"(?<![а-яёА-ЯЁ\w])"
VISUAL_PATTERNS: dict[str, re.Pattern[str]] = {
    "figure": re.compile(_W + r"(?:рисун(?:ок|ка|ку|ке|ком|ки|ков|кам|ками|ках)" + _RU_END + r"|рис\.|иллюстрац\w*"
                         r"|изображени\w*)"
                         r"|\bfig(?:ure)?s?\b\.?|\billustrations?\b|\bimages?\b|\bimagery\b", re.I),
    "scheme": re.compile(_W + r"(?:схем\w*|картосхем\w*|чертеж\w*|чертёж\w*|эскиз\w*)"
                         r"|\bschem(?:e|es|atic|atics)\b|\bdiagrams?\b|\bdrawings?\b|\bsketch(?:es)?\b|\blayouts?\b",
                         re.I),
    "map": re.compile(_W + r"(?:карт(?:а|ы|е|у|ой|ою|ам|ами|ах)?" + _RU_END + r"|картограм\w*|планшет\w*"
                      r"|выкопировк\w*)|\bmaps?\b|\bcontour\s+(?:lines?|plots?)\b", re.I),
    "plan": re.compile(_W + r"план(?:а|у|е|ом|ы|ов|ам|ами|ах)?" + _RU_END + r"|\bplans?\b|\bplan\s+view\b", re.I),
    "section": re.compile(_W + r"разрез(?:а|у|е|ом|ы|ов|ам|ами|ах)?" + _RU_END
                          + r"|\bcross[- ]?sections?\b|\bgeolog(?:ic|ical)\s+sections?\b", re.I),
    "graph": re.compile(_W + r"(?:график\w*|диаграмм\w*|гистограмм\w*|номограмм\w*|эпюр\w*)"
                        r"|\bgraphs?\b|\bcharts?\b|\bplots?\b|\bhistograms?\b", re.I),
    "profile": re.compile(_W + r"(?:профил(?:ь|я|ю|ем|е|и|ей|ям|ями|ях)" + _RU_END + r"|профильн\w*)"
                          r"|\bprofiles?\b", re.I),
    "radargram": re.compile(_W + r"радарограм\w*|\bradargrams?\b|\bgpr\s+(?:profiles?|sections?|images?)\b", re.I),
    "photo": re.compile(_W + r"(?:фото(?:графи\w*|снимк\w*|снимок|план\w*|схем\w*)?" + _RU_END
                        + r"|(?:аэро|космо)?сним(?:ок|ка|ку|ке|ком|ки|ков|кам|ками|ках)" + _RU_END + r")"
                        r"|\bphotos?\b|\bphotographs?\b|\bsnapshots?\b", re.I),
    "table": re.compile(_W + r"(?:таблиц\w*|табл\.)|\btables?\b", re.I),
    "isolines": re.compile(_W + r"(?:изолини\w*|изогипс\w*|изобар\w*)|\bisolines?\b|\bisopachs?\b", re.I),
    "interferogram": re.compile(_W + r"интерферограм\w*|\binterferograms?\b", re.I),
    "column": re.compile(_W + r"(?:(?:стратиграфическ|литологическ|геологическ)\w*\s+колонк\w*"
                         r"|колонк\w*\s+скважин\w*)|\b(?:stratigraphic|lithologic|borehole)\s+columns?\b", re.I),
}
# false friends: a match inside one of these phrases is not a picture («технологическая карта» is a document)
VISUAL_EXCLUDE: tuple[re.Pattern[str], ...] = (
    re.compile(r"технологическ\w*\s+карт\w*", re.I),
    re.compile(_W + r"план\w*\s+(?:мероприяти|действи|развити|финансировани|исследовани|эксперимент|работы\b)\w*",
               re.I),
    re.compile(r"площад\w*\s+(?:поперечного\s+)?(?:сечени|разрез)\w*", re.I),
    re.compile(r"\bbusiness\s+plans?\b|\bplans?\s+to\b|\btables?\s+of\s+contents\b", re.I),
)


@dataclass(frozen=True)
class VisualIntent:
    visual: bool
    cues: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, object]:
        return {"visual": self.visual, "cues": list(self.cues)}


def visual_intent(query: str) -> VisualIntent:
    """Rule-based visual intent of a query: one picture word is enough (the cue names are reported)."""
    text = query or ""
    excluded = [m.span() for rx in VISUAL_EXCLUDE for m in rx.finditer(text)]
    cues = set()
    for name, rx in VISUAL_PATTERNS.items():
        for m in rx.finditer(text):
            if any(a <= m.start() < b for a, b in excluded):
                continue
            cues.add(name)
            break
    return VisualIntent(bool(cues), tuple(sorted(cues)))
