"""Object keys for the layer of objects of the world passport: mines, blocks, panels, profile lines, benchmarks,
boreholes, shafts, faults, seams. A name as printed («блок 201», «скв. 128», «пр 3», «Rp64», «1 СЗП», «ствол 2-бис»,
«разлом № 98», «СКПРУ-1») becomes ``(kind, mine, ident)``; names with one key are one object.

The mine comes from the name itself when it is printed there (``блок 201 СКРУ-1``), otherwise from the attribution of
the record (``site_norm``); it is never guessed. A key with an unknown mine stays apart from keys with a mine.
"""
from __future__ import annotations

import re
import unicodedata

_MINE_RX = [
    (re.compile(r"(?:СКПРУ|СКРУ|СКРУ-|Соликамск\w*\s+калийн\w*\s+рудоуправлени\w*)\s*[-–№]?\s*([123])(?!\d)", re.I),
     "SKRU"),
    (re.compile(r"(?:БКПРУ|БКРУ|БПКРУ|Березниковск\w*\s+калийн\w*\s+(?:производствен\w*\s+)?рудоуправлени\w*)\s*[-–№]?"
                r"\s*([1-4])(?!\d)", re.I), "BKPRU"),
    (re.compile(r"(?:БКЗ|БК3)\s*[-–]?\s*([1-4])(?!\d)", re.I), "BKPRU"),
]
_ORDINAL_MINE = [(re.compile(r"перв\w*\s+(?:соликамск|калийн)", re.I), "SKRU1"),
                 (re.compile(r"втор\w*\s+соликамск", re.I), "SKRU2"),
                 (re.compile(r"трет\w*\s+соликамск", re.I), "SKRU3"),
                 (re.compile(r"усть-?\s?яйв", re.I), "UST_YAYVA")]
_UNATTRIBUTED = {"", "UNKNOWN", "VKM_UNSPECIFIED", "NON_VKM", "OTHER_POTASH_SITE"}

_PATTERNS = [   # (kind, regex, group of the identifier) — first match wins
    ("BOREHOLE", re.compile(r"(?:скв\w*\.?|скважин\w*)\s*№?\s*(\d+[а-я]?(?:[-/]\d+)?)", re.I), 1),
    ("BENCHMARK", re.compile(r"(?:\bRp|Рп|репер\w*|rp)\s*№?\s*(\d+)", re.I), 1),
    ("LINE", re.compile(r"(?:профильн\w*\s+лини\w*|наблюдательн\w*\s+лини\w*|лини\w*|\bпр\.?|\bПЛ)\s*№?\s*(\d+)",
                        re.I), 1),
    ("BLOCK", re.compile(r"блок\w*\s*№?\s*(\d+[а-яА-Я]?)(?![\dА-Яа-я])", re.I), 1),
    ("PANEL", re.compile(r"(\d+)\s*-?\s*(СЗП|СВП|ЮЗП|ЮВП|СП|ЮП|ВП|ЗП|ОП)(?![А-Яа-я])"), 0),
    ("PANEL", re.compile(r"панел\w*\s*№?\s*(\d+[А-Яа-я]{0,3})(?![\dА-Яа-я])", re.I), 1),
    ("SHAFT", re.compile(r"ствол\w*\s*№?\s*(\d+\s?-?\s?(?:бис)?)", re.I), 1),
    ("FAULT", re.compile(r"разлом\w*\s*№?\s*(\d+)", re.I), 1),
    ("STATION", re.compile(r"(?:замерн\w*|наблюдательн\w*|комплексн\w*)\s+станци\w*\s*№?\s*(\d*)", re.I), 1),
]
_TYPE_KIND = {"MINE": "MINE", "BLOCK": "BLOCK", "PANEL": "PANEL", "PROFILE_LINE": "LINE", "BENCHMARK": "BENCHMARK",
              "BOREHOLE": "BOREHOLE", "SHAFT": "SHAFT", "FAULT": "FAULT", "STATION": "STATION", "PILLAR": "PILLAR",
              "SEAM": "SEAM"}
_LEVEL_KIND = {"mine": "MINE", "neighbour_mine": "MINE", "mine_field": "MINE", "block": "BLOCK", "panel": "PANEL",
               "survey_line": "LINE", "benchmark": "BENCHMARK", "borehole": "BOREHOLE", "pillar": "PILLAR",
               "intermine_pillar": "PILLAR"}      # hierarchy «seam» rows are particular features, not the unit


def _norm(s: str | None) -> str:
    t = unicodedata.normalize("NFKC", (s or "").replace("№", " ")).replace("ё", "е").replace("Ё", "Е")   # NFKC: № → No
    return re.sub(r"[‐-―−]", "-", t)


def mine_in(text: str | None) -> str:
    """Mine code printed in the text: SKRU1…3, BKPRU1…4, UST_YAYVA; empty if none or more than one."""
    t = _norm(text)
    found = set()
    for rx, prefix in _MINE_RX:
        for m in rx.finditer(t):
            found.add(f"{prefix}{m.group(1)}")
    for rx, code in _ORDINAL_MINE:
        if rx.search(t):
            found.add(code)
    return found.pop() if len(found) == 1 else ""


def _ident(s: str) -> str:
    return re.sub(r"\s+", "", s).upper().replace("-", "")


def object_key(name: str | None, entity_type: str | None = None, site_norm: str | None = None,
               seam_of=None) -> tuple[str, str, str]:
    """``(kind, mine, ident)``; kind OTHER with a normalised name when no pattern applies. ``seam_of`` maps a name to
    a stratigraphic unit id (``Units.unit_of`` of the passport)."""
    t = _norm(name).strip()
    mine = mine_in(t)
    site = (site_norm or "").upper()
    if not mine and site not in _UNATTRIBUTED and not site.startswith(("SKRU1_OR", "BEREZNIKI", "SOLIKAMSK")):
        mine = site
    if not mine and site.startswith("SKRU1_OR"):
        mine = "SKRU1_OR_SKRU2"
    etype = (entity_type or "").upper()
    if etype == "MINE" or (mine and re.fullmatch(r"(?:рудник\w*\s+)?[«\"]?(?:СК\w*|БК\w*)\s*[-–№]?\s*\d[»\"]?", t, re.I)):
        code = mine_in(t)
        if code:
            return "MINE", code, ""
    for kind, rx, g in _PATTERNS:
        m = rx.search(t)
        if m:
            ident = _ident(m.group(0) if g == 0 else m.group(g))
            if kind == "PANEL" and g == 0:
                ident = _ident(m.group(1) + m.group(2))
            if kind in ("BOREHOLE", "FAULT"):
                return kind, "", ident            # numbering is deposit-wide; the mine is an attribute
            return kind, mine or "UNKNOWN", ident
    if etype == "SEAM" and seam_of is not None:
        u = seam_of(t)
        if u:
            return "SEAM", "", u
    kind = _TYPE_KIND.get(etype, "OTHER")
    words = re.sub(r"[^\w]+", " ", t.casefold()).strip()
    return kind, mine or "UNKNOWN", words[:80]


def hierarchy_key(row: dict, seam_of=None) -> tuple[str, str, str] | None:
    """Key of a Phase-1 spatial hierarchy row (by its level and name), or None for levels without one."""
    kind = _LEVEL_KIND.get(row.get("level", ""))
    if kind is None:
        return None
    attr = row.get("mine_attribution") or ""
    mine = mine_in(attr) or mine_in(row.get("name")) or (row.get("site_scope") or "").upper()
    if kind == "MINE":
        code = mine_in(row.get("name")) or mine_in(attr) or mine
        return ("MINE", code, "") if code else None
    k = object_key(row.get("name"), kind if kind != "LINE" else "PROFILE_LINE", mine, seam_of)
    return k if k[0] == kind else None
