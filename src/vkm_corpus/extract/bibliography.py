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

**Segmentation** (rules v3; the frozen v2 rules stay available as :data:`SEG_V2`, see *Stable ids*).
- Numbered lists («12.», «[12]», «53568.»): an entry starts at each plausible next number (last + 1 … last + 5, or a
  restart at 1). Every other line continues the current entry, across blocks and pages. v3: the next expected number
  inside a line opens an entry too when the text before it ends like an entry («… С. 5–9. 4. Иванов И.И. …»); an
  unexpected number at a block start restarts the sequence when the next numbers continue from it (index numbers
  after a rubric «2. …» or after an OCR-misread number).
- Unnumbered lists (author–year, alphabetical): an entry starts at any of:
  - a blank line (OCR);
  - a block that starts at the left edge of its column with an author-like token (hanging indent);
  - a line with an author pattern right after a line that completed an entry.
- A list is author-led when most blocks open with a name; v3 also counts lines that open with a name after a finished
  entry (layouts whose blocks hold the tail of one entry and the start of the next). In an author-led list a line that
  does not open with a name continues the entry; v3: a corporate author with a year («US Geological Survey. 2008.»,
  «IFG 2020a.») or a normative title («ГОСТ …», «Указания …») after a finished entry opens one.
- v3 names: extended Latin letters, particles and multi-word surnames («Kılınçoğlu», «Santos de Almeida AC»).
- Dropped fragments: no year and no bibliographic marker; imprint or copyright text; bare headings.

**Stable ids.** The ``raw_config_hash`` of an entry is part of its object id together with the first block's region.
:func:`raw_config_hashes` gives each region the hash of the oldest rules whose segmentation yields the same ordered
entry texts there: entries whose text did not change keep their ids, a region with a changed entry gets the current
hash and new ids (so validator B07 never meets one id with two texts, and B04 still recomputes every id).
"""
from __future__ import annotations

import bisect
import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Hashable, Iterable, Sequence

from vkm_corpus.contracts.text_rules import normalize_text_v1
from vkm_corpus.ids import normalize_doi, normalize_isbn, parse_page_id

EXTRACTOR_ID = "bib-segmenter"
EXTRACTOR_VERSION = "0.3.0"
RULES_VERSION = "bib_rules_v3"


def _config_hash(cfg: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(cfg, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


# the rule configuration: its hash is the config_hash of every entry and part of the commit signature (a new version
# recommits every source); the raw_config_hash of an entry (part of the object id, H-14) comes from raw_config_hashes
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
CONFIG_HASH = _config_hash(CONFIG)
# rules v2 (frozen): their configuration hash is the raw_config_hash of the regions that v3 segments as v2 did
CONFIG_V2: dict[str, Any] = {**CONFIG, "rules": "bib_rules_v2"}
CONFIG_HASH_V2 = _config_hash(CONFIG_V2)

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
LABEL = re.compile(r"^\s*(?:\[(\d{1,5})\]|(\d{1,5})\s?[.)](?![\d])|(\d{1,3})\s(?=[А-ЯЁA-Z«\"][а-яёa-z]+[\s,]))\s*")
# list bullets drawn as glyphs of symbol fonts (Private Use Area) or bullet signs before a name or a number
BULLET = re.compile(r"^[ \t •·●▪◦‣⁃∙*-]+")


def _latin_letters(category: str) -> str:
    """Latin letters of one case beyond ASCII (Latin-1, Extended-A/B, Extended Additional), for character classes."""
    return "".join(ch for ch in map(chr, [*range(0xC0, 0x250), *range(0x1E00, 0x1F00)])
                   if unicodedata.category(ch) == category and "LATIN" in unicodedata.name(ch, ""))


# v3 name classes: any Latin letter (ş, ł, č, ő …), combining accents, apostrophes and hyphens
_UP = "A-Z" + _latin_letters("Lu")
_LO = "a-z" + _latin_letters("Ll")
_ANY = _UP + _LO + "'’\\-¨´`" + chr(0x300) + "-" + chr(0x36F)       # + combining accents
_PART_EN = (r"(?:[Vv]an|[Vv]on|[Dd]e|[Dd]el|[Dd]ella|[Dd]er|[Dd]en|[Dd]es|[Dd]u|[Dd]a|[Dd]as|[Dd]os|[Dd]i|[Ll]a|[Ll]e"
            r"|[Tt]en|[Tt]er)")
_PART_IN = r"(?:de|da|del|do|dos|das|y|e|van|von|der|den|la|le|di)"
_EN_SURNAME = rf"(?:{_PART_EN}\s?){{0,2}}[{_UP}][{_ANY}]+(?:\s{_PART_IN}\s[{_UP}][{_ANY}]+)?"
# with a lower-case letter: «Allen RJ», not «PROCEEDINGS OF»
_EN_SURNAME_LC = rf"(?:{_PART_EN}\s?){{0,2}}[{_UP}](?=[{_ANY}]*[{_LO}])[{_ANY}]+(?:\s{_PART_IN}\s[{_UP}][{_ANY}]+)?"
# initials: transliterated digraphs («Yu.», «Zh.», «Kh.»), hyphenated («J.-P.», «S-W.»)
_EN_I1 = rf"(?:Shch|Dzh|Yu|Ya|Ye|Yo|Iu|Ia|Ju|Ja|Zh|Kh|Ch|Sh|Ts|Th|[{_UP}](?:-[{_UP}])?)"
_EN_INIT = rf"(?:{_EN_I1}\s?\.\s?-?){{1,3}}(?:[A-Z](?=[,;]))?"
_SUFFIX = r"(?:,?\s(?:Jr|Sr|II|III|IV)\.?(?=[\s,;]))?"
_EN_CAPS = (r"(?!(?:IEEE|ASCE|AAPG|SPE|SPIE|ISRM|USGS|NASA|ASTM|IAEA|OECD|AGU|GSA|SEG|EAGE|ICE|DOE|EPA|ESA|USA|URL|DOI"
            r"|ISBN|ISSN)\b)(?:(?:VAN|VON|DE|DER|DEN|DU|DA|DI|LE|LA|DEL|SAN)\s)?"
            rf"[{_UP}][{_UP}'’\-]{{2,}}(?:\s(?:DE|DA|DEL|DOS|DAS|VAN|VON|DER|DEN|LA|LE|DI|Y)\s[{_UP}][{_UP}'’\-]{{2,}}"
            rf"|\s[{_UP}][{_UP}'’\-]{{2,}})?")
_RU_PART = r"(?:[Дд]е|[Дд]и|[Дд]у|[Дд]ю|[Лл]е|[Лл]а|[Вв]ан|[Фф]он|[Дд]ер|[Дд]ен|[Дд]ель|[Дд]а|[Оо]['’])"
_RU_SURNAME = rf"(?:{_RU_PART}\s?){{0,2}}[А-ЯЁ][а-яё]+(?:[-‐][А-ЯЁ]?[а-яё]+){{0,3}}"
_RU_INIT = r"(?:(?:Дж|[А-ЯЁ])\s?\.\s?-?){1,2}(?:[А-ЯЁ](?=[,;]))?"
# the second word of a two-word surname («San Martín»), never a venue word («International Symposium, T. J.»)
_SECOND = (r"(?!(?:Symposium|Conference|Congress|Workshop|Proceedings|Journal|Society|Institute|University|Survey"
           r"|Press|Review|Research|Report|Bulletin|Transactions|Letters|Series|Handbook|Studies|Engineering"
           r"|Mechanics|Geology|Sciences?|International|National|Annual|Meeting|Session)\b)")
_RU_CAPS = r"(?!(?:СССР|РСФСР|РАН|ГОСТ|ОСТ|ВНИИГ|ВНИМИ|ВСЕГЕИ|ИГД|МГУ|ЛГИ|УрО|СНиП)\b)[А-ЯЁ]{2,}(?:[-‐][А-ЯЁ]{2,})?"
AUTHOR_PATTERNS: dict[str, str] = {
    # Фамилия И.О. | Фамилия, И. О. | Де Гроот С.
    "ru_si": rf"{_RU_SURNAME},?\s{{0,2}}{_RU_INIT}",
    # И.О. Фамилия
    "ru_is": rf"{_RU_INIT}\s?{_RU_SURNAME}",
    # ФАМИЛИЯ И.О.
    "ru_caps": rf"{_RU_CAPS},?\s{{1,2}}{_RU_INIT}",
    # Фамилия ИО (initials without dots)
    "ru_si_nd": rf"{_RU_SURNAME}\s[А-ЯЁ]{{2}}(?=[.,;](?:\s|$))",
    # SURNAME, I. J. | VAN SURNAME I.J.
    "en_caps": rf"{_EN_CAPS}(?:,\s?|\s)(?:{_EN_INIT}|[A-Z](?=[,;]\s)){_SUFFIX}",
    # Surname, I. J. | Santos de Almeida, A. J. | Lund Jr., R. F.
    "en_si": rf"{_EN_SURNAME}(?:\s{_SECOND}[{_UP}][{_LO}'’\-]+)?(?:,?\s(?:Jr|Sr)\.?)?,\s?{_EN_INIT}{_SUFFIX}",
    # Surname I.J.
    "en_si2": rf"{_EN_SURNAME}\s{_EN_INIT}{_SUFFIX}",
    # Surname IJ (author-year, Springer) | Jan Y-M | van Eijs RMHE
    "en_spr": rf"{_EN_SURNAME_LC}(?:\s{_SECOND}[{_UP}][{_LO}'’\-]+)?\s(?:[A-Z](?:\s[A-Z]){{1,2}}|[A-Z](?:-[A-Z]){{1,2}}"
              rf"|[A-Z]{{1,4}})(?=[,.(\s;:]|$)",
    # I. J. Surname
    "en_is": rf"{_EN_INIT}\s?{_EN_SURNAME}{_SUFFIX}",
}
# a list keeps its script family but may mix forms («Adler, R. J., and J. E. Taylor»)
_FAMILY = {"ru_si": ("ru_si", "ru_is", "ru_si_nd", "ru_caps"), "ru_is": ("ru_is", "ru_si"),
           "ru_caps": ("ru_caps", "ru_si", "ru_is"), "ru_si_nd": ("ru_si_nd", "ru_si"),
           "en_caps": ("en_caps", "en_is"), "en_si": ("en_si", "en_si2", "en_is"),
           "en_si2": ("en_si2", "en_si", "en_is"), "en_spr": ("en_spr",), "en_is": ("en_is", "en_si", "en_si2")}
_AUTHOR_SEP = r"(?:\s*[,;]\s*(?:(?:и|and|&|und|y|et)\s+)?|\s*(?:и|and|&|und|y|et)\s+)"
_ET_AL = r"(?:\s*,?\s*(?:и\s+др\.?|et\.?\s+al\.?|\[и\s+др\.?\]|\(и\s+др\.?\)|u\.\s?a\.))?"
AUTHOR_LIST = {k: re.compile(rf"^(?:{AUTHOR_PATTERNS[k]})(?:{_AUTHOR_SEP}(?:"
                             + "|".join(AUTHOR_PATTERNS[f] for f in fam) + rf"))*{_ET_AL}")
               for k, fam in _FAMILY.items()}
AUTHOR_ONE = {k: re.compile("|".join(AUTHOR_PATTERNS[f] for f in fam)) for k, fam in _FAMILY.items()}
AUTHOR_START = re.compile("^(?:" + "|".join(AUTHOR_PATTERNS[k] for k in ("ru_si", "ru_is", "en_caps", "en_si",
                                                                         "en_si2", "en_spr")) + ")")

# one Springer name, atomic: a list of them is tried at every space of a line, so no name may be re-split on failure
_SPR_NAME = rf"(?>{_EN_SURNAME_LC}(?:\s{_SECOND}[{_UP}][{_LO}'’\-]+)?\s(?:[A-Z](?:-[A-Z]){{1,2}}|[A-Z]{{1,4}}))"
_ORG_WORDS = (r"(?:Agency|Survey|Institute|Department|Ministry|Commission|Council|Association|Society|Service|Office"
              r"|Bureau|Organi[sz]ation|Corporation|Company|Foundation|Laborator(?:y|ies)|Cent(?:re|er)|Administration"
              r"|Authority|Committee|Academ(?:y|ies)|University|Group|Consulting|Board|Division|Directorate"
              r"|Programme|Program|Consortium|Union|Federation|Observatory|Network|Inc\.?|Ltd\.?|LLC|GmbH|AG|B\.V\."
              r"|Bundesamt|Bundesanstalt|Gesellschaft|Kommission|Ministerium|Agentur)")
_CONNECT = r"(?:of|and|for|the|on|in|&|für|und|de|du|des|la|et)"
# an organisation: capitalised words and connectors around an organisation word («US Geological Survey», «Organisation
# for Economic Co-operation and Development»)
_ORG_NAME = (rf"(?:[{_UP}][\w&’'.\-]*\s(?:{_CONNECT}\s){{0,2}}){{0,8}}{_ORG_WORDS}"
             rf"(?:,?\s(?:{_CONNECT}\s){{0,2}}[{_UP}][\w&’'\-]*){{0,6}}")
# upper-case names and acronyms («IFG», «ABC & XYZ», «LANDESAMT FÜR BERGBAU (LAB)»), not «PROCEEDINGS OF …»
_ACRONYM = rf"[{_UP}][{_UP}0-9&/\-]{{1,24}}"
_ACRONYMS = (r"(?!(?:PROCEEDINGS|PROC|JOURNAL|TRANSACTIONS|BULLETIN|REPORT|ANNALS|SYMPOSIUM|CONFERENCE|CONGRESS"
             r"|WORKSHOP|HANDBOOK|VOLUME|VOL|PART|THE|IN)\b)"
             rf"{_ACRONYM}(?:\s(?:&\s)?{_ACRONYM}){{0,5}}(?:\s(?:B\.V\.|N\.V\.|GmbH|AG|Inc\.|Ltd\.))?"
             rf"(?:\s\([^()]{{3,80}}\))?")
_YEAR_SLOT = r"\(?(?:1[89]|20)\d\d[a-z]?(?:/\d{2,4})*\)?(?=[.,:;\s])"
_NORMATIVE = (r"(?:ГОСТ|ОСТ|СНиП|СП\s\d|РД\s\d|ПБ\s\d|СанПиН|Указания|Инструкци[яи]|Правила|Руководство|Методические"
              r"|Временные\s(?:правила|указания|рекомендации|методические)|Единые\s+правила|Федеральные\s+нормы"
              r"|Технологический\s+регламент|Положение\s(?:о|по))\b")
# a corporate author with its year in the author–year slot, or a normative title: opens an entry of an author-led list
ENTITY_START = re.compile(rf"^(?:{_ORG_NAME}\.?,?\s{_YEAR_SLOT}|{_ACRONYMS},?\s{_YEAR_SLOT}|{_NORMATIVE})")

_INLINE_START = re.compile(
    r"(?:(?<=[\d)\]]\.)|(?<=[^\W\d_]{2}\.))\s+(?=(?:" + AUTHOR_PATTERNS["en_si"] + "|" + AUTHOR_PATTERNS["en_caps"]
    + "|" + AUTHOR_PATTERNS["ru_si"] + r")[\s,])"
    # Springer author–year: «… 43:1–9. https://doi.org/… Ortíz DI, Santos de Almeida AC (2016) …»
    r"|(?<=\S)\s+(?=" + _SPR_NAME + r"(?:,\s" + _SPR_NAME + r"){0,8}\s\((?:1[89]|20)\d\d[a-z]?\))"
    # a corporate author after a finished entry: «… 12–17. US Geological Survey. 2008. …»
    r"|(?<=[\d)\]]\.)\s+(?=" + _ORG_NAME + r"\.?,?\s\(?(?:1[89]|20)\d\d[a-z]?\)?[.,:])")

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

# ------------------------------------------------------------------------------------------------ rules v2 (frozen)
# The name patterns and the OCR fold of rules v2, kept verbatim: SEG_V2 reproduces the v2 entry texts, which decides
# the raw_config_hash (hence the ids) of the regions that v3 segments as v2 did. Do not edit.
_V2_RU_SURNAME = r"[А-ЯЁ][а-яё]+(?:[-‐][А-ЯЁ]?[а-яё]+){0,3}"
_V2_LAT_UP = "A-ZÀ-ÖØ-Þ"
_V2_LAT_ANY = r"A-Za-zÀ-ÖØ-öø-ÿ'’\-¨´`"
_V2_EN_SURNAME = rf"(?:(?:van|von|de|der|den|du|da|la|le|di|mc|mac|o')\s?)?[{_V2_LAT_UP}][{_V2_LAT_ANY}]+"
_V2_RU_INIT = r"(?:[А-ЯЁ]\s?\.\s?-?){1,2}"
_V2_EN_INIT = r"(?:[A-Z]\.\s?-?){1,3}(?:[A-Z](?=[,;]))?"
_V2_SUFFIX = r"(?:,?\s(?:Jr|Sr|II|III|IV)\.?(?=[\s,;]))?"
_V2_PATTERNS: dict[str, str] = {
    "ru_si": rf"{_V2_RU_SURNAME},?\s{{0,2}}{_V2_RU_INIT}",
    "ru_is": rf"{_V2_RU_INIT}\s?{_V2_RU_SURNAME}",
    "en_caps": rf"[{_V2_LAT_UP}][{_V2_LAT_UP}'’\-]{{2,}}(?:\s[{_V2_LAT_UP}][{_V2_LAT_UP}'’\-]{{2,}})?,\s?{_V2_EN_INIT}"
               rf"{_V2_SUFFIX}",
    "en_si": rf"{_V2_EN_SURNAME}(?:\s[{_V2_LAT_UP}][a-zà-öø-ÿ'’\-]+)?,\s?{_V2_EN_INIT}{_V2_SUFFIX}",
    "en_si2": rf"{_V2_EN_SURNAME}\s{_V2_EN_INIT}{_V2_SUFFIX}",
    "en_spr": rf"{_V2_EN_SURNAME}(?:\s[{_V2_LAT_UP}][a-zà-öø-ÿ'’\-]+)?\s(?:[A-Z](?:\s[A-Z]){{1,2}}|[A-Z]{{1,3}})"
              rf"(?=[,.(\s;:]|$)",
}
_V2_AUTHOR_START = re.compile("^(?:" + "|".join(_V2_PATTERNS[k] for k in ("ru_si", "ru_is", "en_caps", "en_si",
                                                                          "en_si2", "en_spr")) + ")")
_V2_INLINE_START = re.compile(
    r"(?:(?<=[\d)\]]\.)|(?<=[^\W\d_]{2}\.))\s+(?=(?:" + _V2_PATTERNS["en_si"] + "|" + _V2_PATTERNS["en_caps"]
    + "|" + _V2_PATTERNS["ru_si"] + r")[\s,])"
    r"|(?<=\S)\s+(?=[A-ZÀ-Þ][\w'’\-]+\s[A-Z]{1,3}(?:,\s[A-ZÀ-Þ][\w'’\-]+\s[A-Z]{1,3}){0,8}\s\((?:1[89]|20)\d\d[a-z]?\))")


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
    return [BULLET.sub("", ln).rstrip() for ln in text.split("\n")]


def _page_index(block: Any) -> int:
    pid = _get(block, "page_id")
    if not pid:
        return 0
    try:
        return int(parse_page_id(pid).index)
    except Exception:  # noqa: BLE001 - odd page ids sort last, never crash the commit
        return 10**6


_HOMOGLYPHS = "AaBCcEeHKkMOoPpTXxYy"
_CYR_HOMOGLYPHS = "АаВСсЕеНКкМОоРрТХхУу"
_LAT2CYR = str.maketrans(_HOMOGLYPHS, _CYR_HOMOGLYPHS)
_CYR2LAT = str.maketrans(_CYR_HOMOGLYPHS, _HOMOGLYPHS)
_GREEK2CYR = str.maketrans("ΑΒΓΕΗΚΜΟΡΤΥΧο", "АВГЕНКМОРТУХо")
_GREEK2LAT = str.maketrans("ΑΒΕΗΙΚΜΝΟΡΤΥΧΖο", "ABEHIKMNOPTYXZo")
_CYR = re.compile(r"[А-ЯЁа-яё]")
_LAT = re.compile(r"[A-Za-z]")
_GREEK = re.compile(r"[ΑΒΓΕΗΙΚΜΝΟΡΤΥΧΖο]")
_TOKEN = re.compile(r"[^\W\d_]+")
# spacing accents of TeX text layers: after the letter they mark («Le´ garde»), or before it («Havraˇcek»)
_SPACING = {"´": chr(0x301), "`": chr(0x300), "¨": chr(0x308), "¸": chr(0x327), "˝": chr(0x30B), "ˆ": chr(0x302),
            "˜": chr(0x303), "ˇ": chr(0x30C), "˚": chr(0x30A), "˘": chr(0x306), "˙": chr(0x307)}
_LIGATURES = str.maketrans({"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"})
# a spacing accent with the letters around it: «e´ g» (a gap after it: the accent marks the letter before), «a´u»
_ACCENT = re.compile(r"([A-Za-z]?)([´`¨¸˝ˆ˜ˇ˚˘˙])(\s?)([A-Za-z]?)")
_VOWELS = frozenset("aeiouy")
_OCR_EL = re.compile(r"(?<![A-Za-z\d])(?:J1|JI|Jl)(?=\s?\.)")
_OCR_ZE = re.compile(r"(?<=,\s)3(?=\s?\.\s?[А-ЯЁ]\s?\.)")         # «Гайдамак, 3. И.» → «З. И.»


def _fold_v2(text: str) -> str:
    """The OCR fold of rules v2 (frozen, SEG_V2): Latin homoglyphs inside Cyrillic words and one- or two-letter Latin
    tokens among Cyrillic ones become Cyrillic."""
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


def _composes(letter: str, mark: str) -> bool:
    return bool(letter) and len(unicodedata.normalize("NFC", letter + mark)) == 1


def _repair_accents(text: str) -> str:
    """Spacing accents of TeX text layers joined to their letters («Le´ garde» → «Légarde», «Sj¨ostrand» →
    «Sjöstrand», «Havraˇcek» → «Havraček»). Caron, ring, breve and dot mark the letter after them; the others mark the
    letter before them (always before a gap) unless a consonant precedes and a vowel follows («Mollar´e»)."""
    if not any(ch in _SPACING for ch in text):
        return text

    def join(m: re.Match[str]) -> str:
        before, accent, gap, after = m.groups()
        mark = _SPACING[accent]
        if after.isupper() and not gap and accent not in "ˇ˚˘˙":
            return m.group(0)                   # «O´Dell»: an apostrophe
        if accent in "ˇ˚˘˙" and not gap and _composes(after, mark):
            return before + after + mark        # caron, ring, breve, dot precede their letter («Havraˇcek»)
        if not gap and before.lower() not in _VOWELS and after.lower() in _VOWELS and _composes(after, mark):
            return before + after + mark        # «Mollar´e», «Sj¨ostrand»: a consonant before, a vowel after
        if _composes(before, mark):
            # «Le´ garde», «Mile` z»: the gap inside a word goes; «Poincare´ H.», «a` partir»: it stays
            in_word = m.start() > 0 and m.string[m.start() - 1].isalpha()
            return before + mark + ("" if gap and in_word and after.islower() else gap) + after
        if after and not gap and _composes(after, mark):
            return before + after + mark
        return m.group(0)

    return unicodedata.normalize("NFC", _ACCENT.sub(join, text))


def _script(word: str) -> str | None:
    """'cyr' or 'lat' when the word has a letter of that script that is not a homoglyph of the other one."""
    if any(_CYR.match(ch) and ch not in _CYR_HOMOGLYPHS for ch in word):
        return "cyr"
    if any(_LAT.match(ch) and ch not in _HOMOGLYPHS for ch in word):
        return "lat"
    return None


def _sides(text: str, spans: list[re.Match[str]], i: int) -> set[str]:
    """Scripts of the nearest unambiguous words left and right of token i within its name group: tokens separated
    only by spaces, dots, commas or hyphens (initials and surnames of one name)."""
    seen = set()
    for step in (-1, 1):
        j = i
        while 0 <= j + step < len(spans):
            a, b = (spans[j + step], spans[j]) if step < 0 else (spans[j], spans[j + step])
            gap = text[a.end():b.start()]
            if len(gap) > 3 or not re.fullmatch(r"[\s.,\-]*", gap):
                break
            j += step
            s = _script(spans[j].group(0))
            if s:
                seen.add(s)
                break
    return seen


def _dominant(text: str) -> str | None:
    cyr, lat = len(_CYR.findall(text)), len(_LAT.findall(text))
    return "cyr" if cyr > lat else "lat" if lat > cyr else None


def _fold(text: str) -> str:
    """Parse view of OCR and text-layer noise (rules v3). The entry text itself stays as extracted.
    - TeX spacing accents are joined to their letters;
    - Latin homoglyphs inside Cyrillic words («Габдraxимов») and one- or two-letter Latin tokens among Cyrillic ones
      («Аплонов B.C.») become Cyrillic (as in v2);
    - Cyrillic homoglyphs inside Latin words («Sрerling») and short Cyrillic tokens among Latin words («Gorbin М.
      E.») become Latin; Greek capitals take the script of their neighbours («Зубов Ε. Н.», «Olsen Κ.»);
    - OCR «J1.», «JI.» for «Л.» and «, 3.» for «, З.» in Cyrillic text; typographic ligatures («Delﬁno») spelled
      out."""
    text = _repair_accents(text.translate(_LIGATURES))
    has_cyr = bool(_CYR.search(text))
    if not has_cyr and not _GREEK.search(text):
        return text
    if has_cyr and len(_CYR.findall(text)) > len(_LAT.findall(text)):
        text = _OCR_ZE.sub("З", _OCR_EL.sub("Л", text))
    spans = list(_TOKEN.finditer(text))
    dominant = _dominant(text)
    out = list(text)
    for i, m in enumerate(spans):
        w = new = m.group(0)
        if _GREEK.search(w):
            sides = _sides(text, spans, i)
            ctx = _script(w.translate(_GREEK2CYR)) or (next(iter(sides)) if len(sides) == 1 else dominant)
            new = w.translate(_GREEK2CYR if ctx == "cyr" else _GREEK2LAT) if ctx else w
        elif _LAT.search(w) and _CYR.search(w):
            if all(ch in _HOMOGLYPHS or _CYR.match(ch) for ch in w):
                new = w.translate(_LAT2CYR)
            elif all(ch in _CYR_HOMOGLYPHS or _LAT.match(ch) for ch in w):
                new = w.translate(_CYR2LAT)
        elif _LAT.search(w) and len(w) <= 2 and all(ch in _HOMOGLYPHS for ch in w):
            # Latin look-alikes become Cyrillic next to a Cyrillic name («Аплонов B.C.», «/ A. А. Маловичко»)
            sides = _sides(text, spans, i)
            if "cyr" in sides or (not sides and dominant == "cyr"):
                new = w.translate(_LAT2CYR)
        elif _CYR.search(w) and len(w) <= 2 and all(ch in _CYR_HOMOGLYPHS for ch in w):
            # Cyrillic look-alikes become Latin only among Latin names («Gorbin М. E.», «Borisov А.А., Smirnov»)
            sides = _sides(text, spans, i)
            if sides == {"lat"} or (not sides and dominant == "lat"):
                new = w.translate(_CYR2LAT)
        if new != w:
            out[m.start():m.end()] = new
    return "".join(out)


def _split_inline(line: str, seg: SegRules | None = None) -> list[str]:
    """Split a line of an author-led list where a new name follows a finished entry («…974–983. Abramowitz, M.,»)."""
    seg = seg or SEG_V3
    parts, start = [], 0
    for m in seg.inline_start.finditer(line):
        head = line[start:m.start()]
        # a finished entry, or the tail of one carried over from the previous block («Prospect. 40, 761–783.»);
        # v3: any sentence before a name list with its year («… Chichester: Wiley. Grab, S. W. (1994) …»)
        if (YEAR.search(head) and len(head) >= 30) or (len(head) >= 12 and re.search(r"\d\s?\.$", head)) or (
                seg.year_heads and len(head) >= 12 and head.rstrip().endswith(".")
                and _year_head(line[m.end():], seg)):
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
    raw = m.group(1) or m.group(2) or m.group(3)
    printed = f"[{raw}]" if m.group(1) else raw
    return int(raw), printed, m.end()


def _plausible(n: int, last: int | None) -> bool:
    if last is None:
        return True
    return last < n <= last + CONFIG["label_step_max"] or (n == 1 and last >= 1)


# ------------------------------------------------------------------------------------------------ segmentation rules
@dataclass(frozen=True)
class SegRules:
    """One version of the segmentation rules. ``config_hash`` is the raw_config_hash of the regions whose entries
    these rules produce (the oldest such version wins, :func:`raw_config_hashes`)."""

    name: str
    config_hash: str
    author_start: re.Pattern[str]
    inline_start: re.Pattern[str]
    fold: Callable[[str], str]
    line_author_led: bool = False            # v3: author-led lists recognised from lines, not only from blocks
    entity_start: re.Pattern[str] | None = None   # v3: corporate author + year, normative title open an entry
    numbered_inline: bool = False            # v3: the next expected number inside a line opens an entry
    year_heads: bool = False                 # v3: «Name, I., 1997a.» opens an entry of an author-led list


SEG_V2 = SegRules("bib_rules_v2", CONFIG_HASH_V2, _V2_AUTHOR_START, _V2_INLINE_START, _fold_v2)
SEG_V3 = SegRules(RULES_VERSION, CONFIG_HASH, AUTHOR_START, _INLINE_START, _fold, line_author_led=True,
                  entity_start=ENTITY_START, numbered_inline=True, year_heads=True)
SEGMENTATIONS: tuple[SegRules, ...] = (SEG_V2, SEG_V3)      # oldest first; the last one is current

_INLINE_LABEL = re.compile(r"\s(\d{1,5})\s?\.\s+(?=[А-ЯЁA-Z«\"“\[])")
_ENDS_ENTRY = re.compile(r"(?:\d{4}[a-zа-я]?|\d+\s?[-–—]\s?\d+|(?:[СсCcPpSs]|pp|стр)\.\s?\d+(?:\s?[-–—]\s?\d+)?"
                         r"|\d+\s?[сcp]|[)\]»])\s?\.\s*(?:[-–—]\s*)?$")


def _continues(numbers: list[tuple[tuple[int, int], int]], pos: tuple[int, int], n: int, last: int | None) -> bool:
    """The next printed numbers after ``pos`` (up to three) continue from ``n`` before they continue from ``last``:
    the list goes on from a number the sequence did not expect (an index after an OCR-misread number)."""
    step = CONFIG["label_step_max"]
    i = bisect.bisect_right([p for p, _m in numbers], pos)
    for _p, m in numbers[i:i + 3]:
        if n < m <= n + step:
            return True
        if last is not None and last < m <= last + step:
            return False
    return False


def _split_label(line: str, expected: int, skip: int = 0) -> tuple[str, str] | None:
    """(head, rest) at the expected next number inside a line, after text that ends like an entry
    («… С. 5–9. 4. Иванов И.И. …»); five-digit index numbers only need a punctuation mark before them."""
    for m in _INLINE_LABEL.finditer(line, skip):
        if int(m.group(1)) != expected:
            continue
        head = line[:m.start()]
        if len(head.strip()) >= 20 and (_ENDS_ENTRY.search(head) or (expected >= 1000
                                                                      and re.search(r"[.,;)\]]\s*$", head))):
            return head, line[m.start() + 1:]
    return None


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


def _looks_like_entry_start(line: str, seg: SegRules | None = None) -> bool:
    return bool(_label(line) or (seg or SEG_V3).author_start.match(line.strip()))


def build_zones(blocks: Sequence[Any], seg: SegRules | None = None) -> list[_Zone]:
    """Reference zones of one source (blocks in page and reading order; primary text layer only)."""
    seg = seg or SEG_V3
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
                (_looks_like_entry_start(first, seg) and is_bibliographic(text)) or (zone.frags and not _label(first)
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


def _name_lines(zone: _Zone, seg: SegRules) -> int:
    """Lines that open with a name right after a line that finished an entry (blocks holding a tail and a start)."""
    n, prev = 0, ""
    for f in zone.frags:
        for ln in f.lines:
            s = ln.strip()
            if not s:
                continue
            if prev and ENTRY_END.search(prev) and seg.author_start.match(seg.fold(s)):
                n += 1
            prev = s
    return n


_YEAR_AFTER_NAMES = re.compile(r"[\s,.(:]{0,4}(?:1[89]|20)\d\d[a-z]?(?![\d])")
_OPEN_LIST = re.compile(r"(?:[,;&]|\s(?:and|и|und))\s*$")


def _open_list(entry: RawEntry | None) -> bool:
    """The entry is still in its author list: names only so far, ending with a separator («Evans, D. L., …, Ott,
    H.,»), not a title that ends with a comma («…: Applied Geophysics. Cambridge Univ. Press,»)."""
    if entry is None or not entry.lines:
        return False
    text = _fold(normalize_text_v1(entry.text))
    if not _OPEN_LIST.search(text) or YEAR.search(text):
        return False
    core = re.sub(r"(?:[\s,;&]|\band\b|\bи\b|\bund\b)+$", "", text)
    end = max((m.end() for m in (rx.match(core) for rx in AUTHOR_LIST.values()) if m), default=0)
    return end >= len(core) - 1


def _year_head(line: str, seg: SegRules) -> bool:
    """A name list followed by a year: the head of an author–year entry («Abel, A.B., 1997a.», «Allen RJ (2001)»)."""
    f = seg.fold(line)
    end = max((m.end() for m in (rx.match(f) for rx in AUTHOR_LIST.values()) if m), default=0)
    return end > 0 and bool(_YEAR_AFTER_NAMES.match(f, end))


def _column_edge(frag: _Frag, zone: _Zone, xs_by_page: dict[Any, list[float]] | None = None) -> float | None:
    """Left edge of the fragment's column: the smallest block x0 of the page within the column window."""
    x0 = _get(frag.block, "bbox_x0")
    if x0 is None:
        return None
    pid = _get(frag.block, "page_id")
    w = CONFIG["column_window_pt"]
    if xs_by_page is None:
        xs_by_page = _xs_by_page(zone)
    near = [x for x in xs_by_page.get(pid, ()) if x0 - w <= x <= x0 + CONFIG["edge_tolerance_pt"]]
    return min(near) if near else x0


def _xs_by_page(zone: _Zone) -> dict[Any, list[float]]:
    xs: dict[Any, list[float]] = defaultdict(list)
    for f in zone.frags:
        x = _get(f.block, "bbox_x0")
        if x is not None:
            xs[_get(f.block, "page_id")].append(x)
    return xs


def _complete(entry: RawEntry | None) -> bool:
    if entry is None or not entry.lines:
        return True
    text = normalize_text_v1(entry.text)
    return bool(ENTRY_END.search(text)) and bool(YEAR.search(text) or BIB_MARKER.search(text))


def segment_zone(zone: _Zone, zone_index: int,
                 seg: SegRules | None = None) -> tuple[list[RawEntry], list[tuple[str, Any]]]:
    """Entries of one zone and its leading lines before the first entry (a tail carried over from the previous
    zone, re-attached by :func:`extract_entries`)."""
    seg = seg or SEG_V3
    numbered = _label_chain(zone) >= (2 if len(zone.frags) <= 3 else 3)
    # author-led lists (author–year, alphabetical): every entry starts with a name, so a block that does not start
    # with one continues the previous entry even at the column edge (a tail carried over a page or column), and a
    # name right after a finished entry inside a line starts a new one (text layers without line breaks)
    heads = [next((ln.strip() for ln in f.lines if ln.strip()), "") for f in zone.frags]
    inline = sum(len(_split_inline(ln, seg)) - 1 for f in zone.frags for ln in f.lines) if not numbered else 0
    author_led = not numbered and bool(heads) and (
        sum(bool(seg.author_start.match(seg.fold(h))) for h in heads) >= 0.5 * len(heads)
        or inline >= max(3, 0.5 * len(heads)))
    # v3: lines open with names although most blocks do not (each block holds a tail and the next start); here a
    # block that opens with a name at the column edge still opens an entry, as in lists that are not author-led
    by_lines = False
    if seg.line_author_led and not numbered and heads and not author_led:
        author_led = by_lines = _name_lines(zone, seg) >= max(3, 0.5 * len(heads))
    if author_led:
        zone = _Zone([_Frag(f.block, [part for ln in f.lines for part in _split_inline(ln, seg)]) for f in zone.frags],
                     zone.heading, zone.n_ref)
    entries: list[RawEntry] = []
    cur: RawEntry | None = None
    last: int | None = None
    leading: list[tuple[str, Any]] = []
    step = CONFIG["label_step_max"]
    xs_by_page = _xs_by_page(zone)
    # printed numbers at line starts, in order: (fragment, line) -> number
    numbers = [((fi, li), lab[0]) for fi, f in enumerate(zone.frags) for li, ln in enumerate(f.lines)
               if (lab := _label(ln))] if numbered and seg.numbered_inline else []
    for fi, frag in enumerate(zone.frags):
        pending_blank = False
        edge = None if numbered else _column_edge(frag, zone, xs_by_page)
        x0 = _get(frag.block, "bbox_x0")
        at_edge = edge is not None and x0 is not None and x0 <= edge + CONFIG["edge_tolerance_pt"]
        first_line = True
        prev_line = ""
        queue = list(enumerate(frag.lines))
        while queue:
            li, line = queue.pop(0)
            if not line.strip():
                pending_blank = cur is not None and bool(cur.lines)
                continue
            if li == 0 and is_heading_text(line) and len(frag.lines) > 1:
                continue            # «Список литературы» on the first line of a reference block
            if numbered:
                lab = _label(line)
                fresh = first_line or pending_blank
                # v3: a five-digit index number at a block start after a small rubric number («2. Партийное
                # строительство» → «53576. Абилов, А. …») restarts the sequence instead of being glued to the rubric
                index_jump = seg.numbered_inline and lab is not None and fresh and last is not None and \
                    last < 1000 and lab[0] >= 10000
                if lab and (last is None or last < lab[0] <= last + step or (lab[0] == 1 and fresh and _complete(cur))
                            or index_jump):
                    cur = RawEntry(lab[1], lab[0], zone_index=zone_index, numbered=True)
                    entries.append(cur)
                    last = lab[0]
                elif lab and fresh and _complete(cur):
                    # an unexpected number at a block start after a complete entry: an entry with a misread number
                    # (kept, the sequence is not advanced) or a rubric heading of an index (dropped below); v3: the
                    # sequence restarts from it when the next numbered lines continue from it, not from the old one
                    cur = RawEntry(lab[1], lab[0], zone_index=zone_index, numbered=True)
                    entries.append(cur)
                    if seg.numbered_inline and _continues(numbers, (fi, li), lab[0], last):
                        last = lab[0]
                if seg.numbered_inline and last is not None:
                    cut = _split_label(line, last + 1, lab[2] if lab else 0)
                    if cut is not None:
                        line = cut[0]
                        queue.insert(0, (-1, cut[1]))
                if cur is None:
                    leading.append((line, frag.block))
                else:
                    cur.add(line, frag.block)
            else:
                stripped = line.strip()
                name = bool(seg.author_start.match(seg.fold(stripped)))
                entity = bool(seg.entity_start is not None and seg.entity_start.match(stripped))
                # «Name, I., 1997a.» after anything but an unfinished author list («…, Ott, H.,»)
                year_head = seg.year_heads and author_led and name and cur is not None and \
                    not _open_list(cur) and _year_head(stripped, seg)
                start = False
                if cur is None:
                    if not entries and not leading and _orphan_tail(stripped, seg):
                        leading.append((line, frag.block))
                        first_line = False
                        prev_line = line
                        continue
                    start = not leading or name or entity or _complete_text(" ".join(ln for ln, _b in leading))
                    if not start:
                        leading.append((line, frag.block))
                        first_line = False
                        prev_line = line
                        continue
                elif pending_blank:
                    start = True
                elif first_line:
                    if by_lines:
                        start = (name or entity) and (at_edge or _complete(cur)) and not _open_list(cur)
                    elif author_led:
                        start = (name and (_complete(cur) or (at_edge and bool(ENTRY_END.search(cur.text))))) or \
                                (entity and _complete(cur)) or year_head
                    else:
                        author = name or entity
                        start = (at_edge and (author or _complete(cur)) and not stripped[:1].islower()) or \
                                (author and _complete(cur))
                elif ENTRY_END.search(prev_line.strip()) and (
                        (name and (_complete(cur) or (author_led and len(cur.text) >= 40)))
                        or (entity and _complete(cur))):
                    start = True
                elif year_head:
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


def _orphan_tail(line: str, seg: SegRules | None = None) -> bool:
    """The first line of a zone that continues an entry of the previous zone: no printed number, no leading name,
    and it starts in lower case or with a venue/locator tail («J. Geophys. Res., 82, 277–296.», «вып. 3. С. 5–9.»)."""
    seg = seg or SEG_V3
    s = line.strip()
    if not s or _label(s) or seg.author_start.match(seg.fold(s)):
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


# «Для цитирования:», «См.», «See also» before the names
_PREFIX = re.compile(r"^(?:Для\s+цитирования|For\s+citation|Cite\s+this\s+article(?:\s+as)?|См\.(?:\s+также)?|"
                     r"See(?:\s+also)?\b|Cf\.)\s*:?\s*", re.IGNORECASE)
# a letter-spaced or broken surname at the start of an entry («М о р о з о в , Н . А .», «Ра дуг ин Ю. Н.»)
_SPACED_HEAD = re.compile(r"^([А-ЯЁ](?:\s?[а-яё]){2,30}|[A-Z](?:\s?[a-z]){2,30})\s?(,?)\s?(?=(?:Дж|[А-ЯЁA-Z])\s?\.)")
# a letter-spaced name before its initials anywhere in the entry («…, Ми л е й к о С. Т., …»)
_SPACED_NAME = re.compile(r"(?<![^\s,;(])([А-ЯЁA-Z][а-яёa-z]?(?:\s[а-яёa-z]){2,30})\s?(,?)\s?(?=(?:Дж|[А-ЯЁA-Z])\s?\.)")
_ONE_LETTER_WORDS = frozenset("ВОКСУИАЯ")
_SHORT_WORDS = frozenset("и в во о об обо на по к ко с со у из за от до не ни как для при а но или же ли бы and "
                         "of the on in to for a an by at".split())
# a corporate author in the author–year slot: agency, company, acronym, or up to three words before «(year)»
_CORPORATE = re.compile(
    rf"^(?P<org>{_ORG_NAME}(?:\s\([^()]{{2,60}}\))?|{_ACRONYMS}"
    r"|(?!(?:Proceedings|Proc|Journal|Transactions|Trans|Bulletin|Bull|Report|Annals|Abstracts|Handbook|Encyclopedia"
    r"|Dictionary|In|The|A|An|On|Vol|Volume|Part)\b)"
    rf"[{_UP}][{_LO}]{{2,}}(?:\s[{_UP}][{_LO}]{{2,}}){{0,2}}(?=\s\())"
    rf"\.?,?\s(?={_YEAR_SLOT})")
_FULLNAME = rf"[{_UP}][{_LO}]+(?:-[{_LO}]+)?\s[{_UP}][{_LO}]+(?:-[{_UP}]?[{_LO}]+)?"
_FULLNAME_LIST = re.compile(rf"^({_FULLNAME}(?:,\s(?:and\s)?{_FULLNAME}){{1,11}})(?P<etal>,?\s+et\s+al\.?)?\.\s"
                            rf"(?=[{_UP}\d«\"“])")
_FULLNAME_ONLY = re.compile(rf"^\s*{_FULLNAME}(?:\s?(?:,|and|&)\s?(?:and\s)?{_FULLNAME}){{0,11}}"
                            r"(?:,?\s+et\s+al\.?)?\s*\.?\s*$")
# words of titles and bodies that never are given names or surnames of a full-name author list
_TITLE_WORDS = frozenset(
    "Rock Rocks Salt Mechanics Mining Mine Mines Geology Geological Engineering Science Sciences Research Journal "
    "International National Conference Proceedings Analysis Model Models Modeling Modelling Method Methods Study "
    "Studies Theory Introduction Handbook Physics Water Earth Ground Surface Subsidence Deformation Stress Strain "
    "Creep Numerical Finite Element Elements Data Remote Sensing Radar Survey Institute University Society Press "
    "Symposium Workshop Report Technical Applied Advances New Fundamentals Principles Structural Soil Soils Potash "
    "Coal Oil Gas Energy Storage Underground Tunnel Seismic Geophysics Geophysical Hydrogeology Groundwater "
    "Environmental Monitoring Safety Risk Design Construction Materials Properties Behavior Behaviour Failure "
    "Fracture Damage Strength Test Tests Testing Laboratory Field Case History Review Management Control System "
    "Systems Process Processes Standard Guide Guidelines Manual Series Volume Edition Chapter Part Annual Federal "
    "State Department Agency Office Service Company Group Center Centre Academy Global Regional Basin Deposit".split())
# «Фамилия, Имя.» of bibliographic indexes («Иванов, Владимир. Title»), not «Город, Издательство.»
_RU_SURNAME_GIVEN = re.compile(r"^([А-ЯЁ][а-яё]{2,}(?:-[А-ЯЁ][а-яё]+)?),\s([А-ЯЁ][а-яё]{2,})\.\s(?=[А-ЯЁ«\[])")
_NOT_PERSON = frozenset("Москва Ленинград Пермь Свердловск Екатеринбург Киев Минск Новосибирск Томск Казань Уфа "
                        "Недра Наука Мир Энергия Химия Стройиздат Госгортехиздат Металлургия Техника Изд".split())
# «— Авт.: И. О. Фамилия, …», «(авт. Фамилия И.О., …)», «— авторы И.О. Фамилия» — authors listed after the description
_AVT = re.compile(r"(?:\bА\s?вт\.|\(авт\.|\bавторы\b)\s?:?\s?([^\[\]()—–]{3,300}?)"
                  r"(?=\s?[\[(]\s?и\s+др|\s?и\s+др\.|\s[-–—]\s|\.\s?[-–—]|\s—|\)|\.\s*$|$)")
_PROJECT = re.compile(r"^[A-Z][A-Z0-9\-]{2,20}(?:\s[IVX]{1,4})?\.\s+(?=[A-Z])")
# Springer lists of text layers without spaces («CheruvierE,SuauJ(1986)», «AndersonB,…,Davydycheva S (1999)»)
_NOSPACE = re.compile(r"^((?:[A-Z][a-z]+\s?[A-Z]{1,3},\s?){0,11}[A-Z][a-z]+\s?[A-Z]{1,3})\s?(?=\((?:1[89]|20)\d\d)")


def _repair_head(text: str) -> str:
    """Parse view: a letter-spaced or broken surname at the start joined before its initials («М о р о з о в ,
    Н . А .», «Ш ибаева, Е. Л.», «Ра дуг ин Ю. Н.»). Letter-spaced names (most pieces one letter) always; other
    breaks only in Cyrillic, without short words among the pieces («Mori and L. W.», «К вопросу об А.» stay), and
    after a one-letter word («С аблин», «О роли») only before a comma (index style)."""
    m = _SPACED_HEAD.match(text)
    if not m or " " not in m.group(1):
        return text
    pieces = m.group(1).split()
    spaced = sum(len(p) == 1 for p in pieces) >= 0.7 * len(pieces)
    if not spaced:
        if not _CYR.match(pieces[0]) or any(p.lower() in _SHORT_WORDS for p in pieces[1:]):
            return text
        if pieces[0] in _ONE_LETTER_WORDS and not m.group(2):
            return text
    word = "".join(pieces)
    return word + (", " if m.group(2) else " ") + text[m.end():]


def _repair_spaced(text: str) -> str:
    """Parse view: letter-spaced names before initials joined anywhere in the entry («Ми л е й к о С. Т.»)."""
    return _SPACED_NAME.sub(lambda m: m.group(1).replace(" ", "") + (", " if m.group(2) else " "), text)


def _names(span: str, key: str, end: int | None = None) -> list[str]:
    """Names of a matched author list (``span[:end]``; the characters after it stay visible to the lookaheads of
    the patterns); a space before a dot of an initial goes («А . И.» → «А. И.»)."""
    end = len(span) if end is None else end
    found = (a.group(0) for a in AUTHOR_ONE[key].finditer(span[:end + 3]) if a.end() <= end)
    return [re.sub(r"\s+\.", ".", n) for n in map(_clean, found) if n]


def _authors(text: str) -> tuple[list[str], str, str | None]:
    """(authors, remainder, pattern) — the longest author list at the start of the text; then, if none, a corporate
    author in the author–year slot, a list of full names, or «Фамилия, Имя.» of an index."""
    best: tuple[int, str | None, re.Match[str] | None] = (0, None, None)
    for key, rx in AUTHOR_LIST.items():
        m = rx.match(text)
        if m and m.end() > best[0]:
            best = (m.end(), key, m)
    end, key, m = best
    if m is not None and key is not None:
        names = _names(text, key, end)
        rest = text[end:].lstrip(" ,;:.")
        return names, rest, key
    c = _CORPORATE.match(text)
    if c:
        return [_clean(c.group("org")) or c.group("org")], text[c.end():], "corporate"
    f = _FULLNAME_LIST.match(text)
    if f:
        names = [n.strip() for n in re.split(r",\s(?:and\s)?", f.group(1))]
        tokens = {t for n in names for t in n.split()}
        if not tokens & _TITLE_WORDS and (len(names) >= 3 or f.group("etal") or len(names) == 2):
            return names, text[f.end():], "full_names"
    r = _RU_SURNAME_GIVEN.match(text)
    if r and r.group(1) not in _NOT_PERSON and r.group(2) not in _NOT_PERSON:
        return [f"{r.group(1)}, {r.group(2)}"], text[r.end():], "ru_full"
    s = _NOSPACE.match(text)
    if s:
        names = [re.sub(r"(?<=[a-z])\s?(?=[A-Z]{1,3}$)", " ", n.strip()) for n in s.group(1).split(",")]
        return names, text[s.end():], "en_spr"
    p = _PROJECT.match(text)
    if p:           # «SALTPROJ II. KRAUSE, K.-P., …»: a project acronym before the names
        names, rest, key = _authors(text[p.end():])
        if names and key in AUTHOR_LIST:
            return names, rest, key
    return [], text, None


def _responsibility(clean: str) -> list[str]:
    """Persons of the statement of responsibility («Название / И.О. Фамилия, И.О. Фамилия // …»); editors,
    compilers and translators are not authors."""
    m = re.search(r"\s/\s([^;/–—]{2,300}?)(?:;|\s[-–—]\s|//|\.\s?[-–—]|$)", clean)
    if not m or re.match(r"\s*(?:под\s|сост|ред\.|пер\.|отв\.|науч\.|eds?\.|edited|by\s)", m.group(1), re.IGNORECASE):
        return []
    # one form per list («И.О. Фамилия» first, as GOST prints it): a mixed alternation would pair a surname with the
    # next person's initials when one name does not match («Ş. Yıldız, M. Sofu» → «Yıldız, M»)
    best: list[str] = []
    for key in ("ru_is", "en_is", "ru_si", "en_si", "en_si2"):
        names = [n for n in (_clean(a.group(0)) for a in re.finditer(AUTHOR_PATTERNS[key], m.group(1))) if n]
        if len(names) > len(best):
            best = [re.sub(r"\s+\.", ".", n) for n in names]
    region = m.group(1).strip()
    if not best and _LAT.search(region) and _FULLNAME_ONLY.match(region) and not re.search(_ORG_WORDS, region):
        names = [n.strip() for n in re.split(r"\s?(?:,|&|\band\b)\s?(?:and\s)?", re.sub(r",?\s+et\s+al\.?", "",
                                                                                           region.rstrip(". ")))]
        if not {t for n in names for t in n.split()} & _TITLE_WORDS:
            best = [n for n in names if n]
    return best


def _listed_after(clean: str) -> list[str]:
    """«— Авт.: И. О. Фамилия, И. О. Фамилия [и др.]» (bibliographic indexes)."""
    m = _AVT.search(clean)
    if not m:
        return []
    region = re.sub(r"\b([А-ЯЁ])\s(?=[а-яё]{2})", r"\1", m.group(1))       # «И. Б. Ш утов» → «И. Б. Шутов»
    region = re.sub(r"\s+\.", ".", region)
    # one form per list («И.О. Фамилия» or «Фамилия И.О.»): mixing them pairs a surname with the next initials
    lists = [[_clean(a.group(0)) for a in re.finditer(AUTHOR_PATTERNS[k], region)] for k in ("ru_is", "ru_si")]
    return [n for n in max(lists, key=len) if n]


def _year(text: str, after_authors: str) -> tuple[str | None, int | None]:
    lo, hi = CONFIG["year_range"]
    m = YEAR_PAREN.search(text)
    if m:
        return m.group(0).strip("()").split(",")[0].strip(), int(m.group(1))
    m = re.match(r"^[(\s]*(1[89]\d\d|20[0-2]\d|2030)([a-zа-я])?(?:/\d{2,4})*[).,:\s]", after_authors + " ")
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
        body = re.sub(r"^[(\s]*(?:1[89]\d\d|20[0-2]\d|2030)[a-zа-я]?(?:/\d{2,4})*[).,:\s]+", "", rest)
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
    t = BULLET.sub("", _fold(normalize_text_v1(text)))
    lab = _label(t)
    body = BULLET.sub("", t[lab[2]:]) if lab else t
    body = _PREFIX.sub("", body.lstrip("–— "), count=1)         # «[2] – Белкин П.А.», «Для цитирования: …»
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
    clean = _repair_spaced(_repair_head(clean))
    authors, rest, pattern = _authors(clean)
    # «Фамилия И.О. Название / Фамилия И.О., Фамилия2 И.О.»: the statement of responsibility lists everyone
    responsibility = _responsibility(clean)
    p.authors = responsibility if len(responsibility) > len(authors) else authors
    if not p.authors:
        p.authors = _listed_after(clean)
    p.year_raw, p.year = _year(clean, rest if authors else "")
    after_year = re.match(r"^[(\s]*(?:1[89]\d\d|20[0-2]\d|2030)[a-zа-я]?(?:/\d{2,4})*[).,:\s]",
                          (rest if authors else "") + " ")
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


def _segment(blocks: Sequence[Any], seg: SegRules, stats: SegmentStats) -> list[RawEntry]:
    """Kept raw entries of one source in document order (zones, segmentation, re-attached tails, dropped noise)."""
    zones = build_zones(blocks, seg)
    stats.zones = len(zones)
    raw_all: list[RawEntry] = []
    for zi, zone in enumerate(zones):
        raw, leading = segment_zone(zone, zi, seg)
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
    kept = []
    for e in raw_all:
        if not e.text or not is_bibliographic(e.text):
            stats.dropped_not_bibliographic += 1
            continue
        kept.append(e)
    return kept


def extract_entries(blocks: Iterable[Any], seg: SegRules | None = None) -> tuple[list[Entry], SegmentStats]:
    """All bibliography entries of one source, in document order, with a source-wide running ordinal."""
    stats = SegmentStats()
    out: list[Entry] = []
    for ordinal, e in enumerate(_segment(list(blocks), seg or SEG_V3, stats), 1):
        text = e.text
        pages = e.page_ids
        out.append(Entry(label=e.label, ordinal=ordinal, blocks=list(e.blocks), text=text,
                         normalized_text=normalize_text_v1(text),
                         continues_on_page_id=pages[1] if len(pages) > 1 else None,
                         parsed=parse_entry(text), numbered=e.numbered))
    stats.entries = len(out)
    return out, stats


def raw_config_hashes(entries: Sequence[Entry], blocks: Iterable[Any],
                      group_key: Callable[[Any, int, str], Hashable]) -> list[str]:
    """The raw_config_hash of each entry (of the current rules): the hash of the oldest rules whose segmentation gives
    the same ordered entry texts for the entry's id group. ``group_key(first_block, ordinal, text)`` is the id group
    of an entry: everything its object id hashes except the producer key (scope, anchor, origin, region, models).

    Unchanged regions keep their ids; a region whose entries changed takes the current hash, so its ids are new."""
    blocks = list(blocks)
    keys = [group_key(e.blocks[0], e.ordinal, e.text) for e in entries]
    current: dict[Hashable, list[str]] = defaultdict(list)
    for k, e in zip(keys, entries):
        current[k].append(e.text)
    chosen: dict[Hashable, str] = {}
    for seg in SEGMENTATIONS[:-1]:
        older: dict[Hashable, list[str]] = defaultdict(list)
        for ordinal, e in enumerate(_segment(blocks, seg, SegmentStats()), 1):
            older[group_key(e.blocks[0], ordinal, e.text)].append(e.text)
        for k, texts in current.items():
            if k not in chosen and older.get(k) == texts:
                chosen[k] = seg.config_hash
    return [chosen.get(k, SEGMENTATIONS[-1].config_hash) for k in keys]
