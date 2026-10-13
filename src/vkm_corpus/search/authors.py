"""Authors of the work registry in a query or a lookup (agent Г, 13.10): «работы Баряха», «Барях А.А.», «Baryakh».

The registry (canon tables ``authors``, ``work_authors``, ``works``, ``work_sources``) knows each author as a
*name-key cluster* (``name_key`` = surname | initials | full names | script, H-15) — never an identified person. This
module matches a name written in a query against those keys without guessing:

* ``EXACT_KEY`` — same script, same surname (case, ё/е, initials and word order do not matter);
* ``INFLECTED`` — a Russian case form of the surname («Баряха», «Кудряшову», «Лебедевой» → «Барях», «Кудряшов»,
  «Лебедева»): the query word's nominative candidates (:func:`nominatives`) meet a registry surname;
* ``TRANSLIT`` — the other script («Baryakh» ↔ «Барях») or another Latin spelling of one transliteration («Barjakh»):
  both surnames reduce to one Latin skeleton (:func:`skeleton`: a loose transliteration — kh/h, ya/ia/ja,
  shch/sch/sh … collapse), compared in full;
* ``FUZZY`` — one edit between skeletons (surnames of ≥ 5 letters), offered only when nothing matched above.

Initials of the query («Барях А.А.», «А. Барях») must agree with the cluster's (prefix, across scripts); two clusters
of one surname (homonyms, e.g. «Лебедева О.О.» and «Лебедева А.М.») are both returned and marked — never merged or
chosen. Every rule is a MODEL_CHOICE of retrieval, not scientific data.

**Author mentions in a query** (:func:`mentions`, the hybrid search's author route): a cue names the word that may be
an author — «работы / труды / статьи / публикации … X», «X и др.», «X et al.», «X А.А.» / «А.А. X», «по X»
(capitalised), EN «papers / works by X»; or a capitalised surname-like word alone, not right after an eponym head
(«закон Бингама», «критерий Кулона», «модель …»). A mention counts only when the registry confirms it (EXACT_KEY,
INFLECTED or TRANSLIT; FUZZY never routes). The *topic* is the query without the name, its initials and the cue words.
"""
from __future__ import annotations

import re
import threading
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

EXACT_KEY, INFLECTED, TRANSLIT, FUZZY = "EXACT_KEY", "INFLECTED", "TRANSLIT", "FUZZY"
METHOD_ORDER = {EXACT_KEY: 0, INFLECTED: 1, TRANSLIT: 2, FUZZY: 3}
ROUTE_METHODS = frozenset({EXACT_KEY, INFLECTED, TRANSLIT})
IDENTITY_NOTE = "a cluster of one name key (NAME_KEY_ONLY), not an identified person"
HOMONYM_NOTE = ("several name-key clusters share this surname: listed side by side, not merged and not chosen "
                "(H-15)")

_CYR2LAT = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh", "з": "z", "и": "i", "й": "y",
            "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
            "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e",
            "ю": "yu", "я": "ya", "і": "i", "ї": "i", "є": "e", "ў": "u"}
_DIGRAPHS = (("shch", "s"), ("sch", "s"), ("tch", "c"), ("ch", "c"), ("sh", "s"), ("zh", "z"), ("kh", "h"),
             ("ts", "c"), ("tz", "c"), ("ph", "f"), ("x", "ks"), ("w", "v"), ("q", "k"))
_TOKEN = re.compile(r"[^\W\d_]+(?:-[^\W\d_]+)*")
# oblique case endings of Russian surnames → the nominative (suffix, replacement), longest first
_ENDINGS: tuple[tuple[str, str], ...] = (
    ("ского", "ский"), ("скому", "ский"), ("ским", "ский"), ("ском", "ский"), ("ской", "ская"), ("скую", "ская"),
    ("цкого", "цкий"), ("цкому", "цкий"), ("цким", "цкий"), ("цкой", "цкая"),
    ("ого", ""), ("ому", ""), ("ему", ""), ("ым", ""), ("ом", ""), ("ем", ""),
    ("ой", "а"), ("ую", "а"),
    ("а", ""), ("у", ""), ("е", ""), ("у", "а"), ("е", "а"), ("ы", "а"), ("и", "а"))


def _fold(text: Any) -> str:
    return unicodedata.normalize("NFKC", str(text or "")).casefold().replace("ё", "е")


def script_of(text: str) -> str:
    cyr = lat = 0
    for ch in text or "":
        if ch.isalpha():
            name = unicodedata.name(ch, "")
            cyr += "CYRILLIC" in name
            lat += "LATIN" in name
    return "MIXED" if cyr and lat else "CYRL" if cyr else "LATN" if lat else "OTHER"


def skeleton(word: str) -> str:
    """A loose Latin skeleton of a name for comparison across scripts: Cyrillic transliterated, then kh → h,
    sh/shch/sch → s, zh → z, ch/ts → c, y/j → i, a soft vowel's i dropped (ya, ia, ja → a), doubled letters single.
    «Барях» and «Baryakh», «Ермашов» and «Yermashov», «Кудряшов» and «Kudryashov» meet."""
    s = "".join(_CYR2LAT.get(ch, ch) for ch in _fold(word))
    s = re.sub(r"[^a-z]", "", s)
    for a, b in _DIGRAPHS:
        s = s.replace(a, b)
    s = re.sub(r"(?:iy|yi|ij|ii|yj)$", "i", s)
    s = s.replace("y", "i").replace("j", "i")
    s = re.sub(r"i(?=[aeou])", "", s)
    return re.sub(r"(.)\1+", r"\1", s)


def nominatives(word: str) -> list[str]:
    """The word and its nominative candidates if it is a Russian surname in an oblique case («Баряха» → «барях»,
    «Лебедевой» → «лебедева», «Горского» → «горский»; a stem keeps ≥ 3 letters). Only candidates: the registry
    decides."""
    w = _fold(word)
    out = [w]
    if script_of(w) != "CYRL":
        return out
    for suffix, repl in _ENDINGS:
        if w.endswith(suffix) and len(w) - len(suffix) >= 3:
            cand = w[:-len(suffix)] + repl
            if cand not in out:
                out.append(cand)
    return out


def _edits_le1(a: str, b: str) -> bool:
    """Damerau–Levenshtein distance ≤ 1."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diff = [i for i in range(len(a)) if a[i] != b[i]]
        return len(diff) == 1 or (len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]]
                                  and a[diff[1]] == b[diff[0]])
    if len(a) > len(b):
        a, b = b, a
    return any(a == b[:i] + b[i + 1:] for i in range(len(b)))


@dataclass(frozen=True)
class PersonName:
    """A name as written: surname (folded), initials (folded letters), full names, script."""

    surname: str
    initials: str
    full_names: tuple[str, ...]
    script: str
    text: str

    @classmethod
    def parse(cls, text: str) -> "PersonName | None":
        stripped = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", text or "")
        tokens = _TOKEN.findall(_fold(stripped).replace(".", " ").replace(",", " "))
        if not tokens:
            return None
        long_tokens = [t for t in tokens if len(t) > 1]
        surname = long_tokens[0] if long_tokens else tokens[0]
        rest = list(tokens)
        rest.remove(surname)
        return cls(surname, "".join(t for t in rest if len(t) == 1), tuple(t for t in rest if len(t) > 1),
                   script_of(stripped), " ".join(str(text or "").split()))


def _initial_keys(initials: str, full_names: Iterable[str] = ()) -> str:
    letters = initials or "".join(n[0] for n in full_names if n)
    return "".join((skeleton(ch) or ch)[:1] for ch in letters)


def initials_agree(query: PersonName, initials: str, full_names: Iterable[str]) -> bool:
    """No initials in the query, or the two initial sequences agree as prefixes (across scripts: «А.» ≡ «A.»)."""
    q = _initial_keys(query.initials, query.full_names)
    a = _initial_keys(initials, full_names)
    return not q or not a or a.startswith(q) or q.startswith(a)


# ------------------------------------------------------------------------------------------------ registry index
@dataclass
class AuthorRecord:
    author_id: str
    name_display: str
    surname: str
    initials: str
    full_names: tuple[str, ...]
    script: str
    skeleton: str
    identity_status: str | None = None
    works: list[str] = field(default_factory=list)


@dataclass
class AuthorMatch:
    author_id: str
    name_display: str
    method: str
    query_surname: str
    homonyms: list[str] = field(default_factory=list)     # other clusters of the same surname that also matched

    def as_record(self) -> dict[str, Any]:
        return {"author_id": self.author_id, "name_display": self.name_display, "method": self.method,
                "homonym_clusters": list(self.homonyms), "identity_note": IDENTITY_NOTE}


def _name_key_parts(row: Mapping[str, Any]) -> tuple[str, str, tuple[str, ...], str]:
    key = str(row.get("name_key") or "")
    parts = key.split("|")
    if len(parts) == 4 and parts[0]:
        return parts[0], parts[1], tuple(x for x in parts[2].split() if x), parts[3]
    name = PersonName.parse(str(row.get("name_display") or ""))
    if name is None:
        return "", "", (), "OTHER"
    return name.surname, name.initials, name.full_names, name.script


@dataclass
class AuthorIndex:
    """Authors (name-key clusters) with their works and the works' registered sources."""

    authors: dict[str, AuthorRecord]
    works: dict[str, dict[str, Any]]
    work_authors: dict[str, list[dict[str, Any]]]
    sources: dict[str, list[dict[str, Any]]]
    snapshot_id: str | None = None
    _by_surname: dict[str, list[str]] = field(default_factory=dict, repr=False)
    _by_skeleton: dict[str, list[str]] = field(default_factory=dict, repr=False)

    @classmethod
    def from_rows(cls, authors: Iterable[Mapping[str, Any]], work_authors: Iterable[Mapping[str, Any]],
                  works: Iterable[Mapping[str, Any]], work_sources: Iterable[Mapping[str, Any]],
                  snapshot_id: str | None = None) -> "AuthorIndex":
        recs: dict[str, AuthorRecord] = {}
        for a in authors:
            surname, initials, fulls, script = _name_key_parts(a)
            if not surname or not a.get("author_id"):
                continue
            recs[a["author_id"]] = AuthorRecord(a["author_id"], str(a.get("name_display") or ""), surname, initials,
                                                fulls, script, skeleton(surname), a.get("identity_status"))
        wa: dict[str, list[dict[str, Any]]] = {}
        for r in sorted(work_authors, key=lambda r: (r.get("work_id") or "", r.get("ordinal") or 0)):
            wa.setdefault(r["work_id"], []).append({"ordinal": r.get("ordinal"), "role": r.get("role"),
                                                    "name_as_listed": r.get("name_as_listed"),
                                                    "author_id": r.get("author_id")})
            rec = recs.get(r.get("author_id") or "")
            if rec is not None and r["work_id"] not in rec.works:
                rec.works.append(r["work_id"])
        src: dict[str, list[dict[str, Any]]] = {}
        for r in sorted(work_sources, key=lambda r: (r.get("work_id") or "", r.get("source_id") or "")):
            have = src.setdefault(r.get("work_id") or "", [])
            if r.get("work_id") and r.get("source_id") and r["source_id"] not in [x["source_id"] for x in have]:
                have.append({"source_id": r["source_id"], "link_type": r.get("link_type"),
                             "is_primary": r.get("is_primary")})
        out = cls(recs, {w["work_id"]: dict(w) for w in works if w.get("work_id")}, wa, src, snapshot_id)
        for aid, rec in sorted(recs.items()):
            out._by_surname.setdefault(rec.surname, []).append(aid)
            out._by_skeleton.setdefault(rec.skeleton, []).append(aid)
        return out

    # ------------------------------------------------------------------ matching
    def match(self, text: str, *, fuzzy: bool = True) -> list[AuthorMatch]:
        """Clusters whose key matches the name ``text`` (best method per cluster; FUZZY only when nothing else
        matched and ``fuzzy``), best method first. Initials of the query must agree."""
        name = PersonName.parse(text)
        if name is None or len(name.surname) < 2:
            return []
        found: dict[str, tuple[str, str]] = {}

        def put(aid: str, method: str, surname: str) -> None:
            rec = self.authors[aid]
            if not initials_agree(name, rec.initials, rec.full_names):
                return
            old = found.get(aid)
            if old is None or METHOD_ORDER[method] < METHOD_ORDER[old[0]]:
                found[aid] = (method, surname)

        for aid in self._by_surname.get(name.surname, []):
            if self.authors[aid].script == name.script or name.script == "MIXED":
                put(aid, EXACT_KEY, name.surname)
        cands = nominatives(name.surname)
        for cand in cands[1:]:
            for aid in self._by_surname.get(cand, []):
                if self.authors[aid].script == "CYRL":
                    put(aid, INFLECTED, cand)
        for cand in cands:
            for aid in self._by_skeleton.get(skeleton(cand), []):
                # the other script, or another Latin spelling of one transliteration («Barjakh» / «Baryakh»)
                if self.authors[aid].script != name.script or name.script == "LATN":
                    put(aid, TRANSLIT, cand)
        if not found and fuzzy:                    # the word or one of its nominatives, one edit away
            for cand in cands:
                q = skeleton(cand)
                if len(q) < 5:
                    continue
                for sk, aids in self._by_skeleton.items():
                    if len(sk) >= 5 and _edits_le1(q, sk):
                        for aid in aids:
                            put(aid, FUZZY, cand)
        out = [AuthorMatch(aid, self.authors[aid].name_display, m, s) for aid, (m, s) in found.items()]
        by_surname: dict[str, list[str]] = {}
        for m in out:
            by_surname.setdefault(skeleton(self.authors[m.author_id].surname), []).append(m.author_id)
        for m in out:
            m.homonyms = sorted(a for a in by_surname[skeleton(self.authors[m.author_id].surname)]
                                if a != m.author_id)
        out.sort(key=lambda m: (METHOD_ORDER[m.method], self.authors[m.author_id].surname, m.name_display,
                                m.author_id))
        return out

    def works_of(self, author_ids: Iterable[str], *, active_only: bool = True) -> list[dict[str, Any]]:
        """Works of the clusters (merged works left out with ``active_only``), each with its authors as listed,
        the matched clusters (ordinal, role) and the registered sources; order: works the cluster wrote (role
        AUTHOR / CORPORATE_AUTHOR / unknown) before supervised, edited or translated ones, then lead author first,
        then newer year, then id."""
        want = set(author_ids)
        rows = []
        for wid in sorted({w for a in want for w in self.authors[a].works if a in self.authors}):
            work = self.works.get(wid) or {"work_id": wid}
            if active_only and work.get("status") == "MERGED_INTO":
                continue
            authors = self.work_authors.get(wid, [])
            matched = [a for a in authors if a.get("author_id") in want]
            rows.append({"work": work, "authors": authors, "matched": matched,
                         "sources": self.sources.get(wid, [])})
        own = {"AUTHOR", "CORPORATE_AUTHOR", "UNKNOWN", None}       # works the cluster wrote before supervised ones
        rows.sort(key=lambda r: (not any(a.get("role") in own for a in r["matched"]),
                                 min((a.get("ordinal") or 99) for a in r["matched"]) if r["matched"] else 99,
                                 -(r["work"].get("publication_year") or 0), r["work"].get("work_id") or ""))
        return rows


class CanonAuthors:
    """:class:`AuthorIndex` of a canonical snapshot (``CanonStore``: ``query`` + ``snapshot_id``), rebuilt when the
    snapshot changes. ``index()`` raises what the canon raises (an API failure without a snapshot)."""

    def __init__(self, canon: Any) -> None:
        self.canon = canon
        self._lock = threading.Lock()
        self._cached: AuthorIndex | None = None

    def index(self) -> AuthorIndex:
        snapshot = self.canon.snapshot_id()
        with self._lock:
            if self._cached is not None and self._cached.snapshot_id == snapshot:
                return self._cached
        q = self.canon.query
        idx = AuthorIndex.from_rows(
            q("SELECT author_id, name_display, name_key, script, identity_status FROM authors"),
            q("SELECT work_id, author_id, ordinal, role, name_as_listed FROM work_authors"),
            q("SELECT work_id, title, publication_year, status, merged_into_work_id, work_type FROM works"),
            q("SELECT work_id, source_id, link_type, is_primary FROM work_sources"), snapshot)
        with self._lock:
            self._cached = idx
        return idx


# ------------------------------------------------------------------------------------------------ query mentions
_NAME = r"[А-ЯЁA-Z][а-яёa-z]{2,}(?:-[А-ЯЁA-Z][а-яёa-z]{2,})?"
_ANY_NAME = r"[А-ЯЁA-Zа-яёa-z][а-яёa-z]{2,}(?:-[А-ЯЁA-Zа-яёa-z][а-яёa-z]{2,})?"
_INIT = r"(?:[А-ЯЁA-Z]\.\s?){1,2}"
_WORK_WORDS = (r"работ[ыау]?|труд(?:ы|ов|ах|ам)|стать(?:я|и|ей|ях|ям)|публикаци(?:я|и|й|ях|ям)|монографи(?:я|и|й|ях)"
               r"|книг(?:а|и|у|ах)?|учебник(?:и|а|ов|ах)?|диссертаци(?:я|и|й|ях)|пособи(?:е|я|ях)"
               r"|отч[её]т(?:ы|а|ов|ах)?"
               r"|доклад(?:ы|а|ов|ах)?|исследовани(?:я|й|ях)")
CUE_PATTERNS: dict[str, re.Pattern[str]] = {
    "works_of": re.compile(rf"(?<![а-яёА-ЯЁ\w])(?P<cue>(?i:{_WORK_WORDS}))\s+(?P<ini>{_INIT})?(?P<name>{_ANY_NAME})"),
    "works_by": re.compile(rf"\b(?P<cue>(?i:articles?|papers?|works?|publications?|books?|monographs?|studies|reports?)"
                           rf"\s+(?i:by|of))\s+(?P<ini>{_INIT})?"
                           rf"(?P<name>[A-Za-z][a-z]{{2,}}(?:-[A-Za-z][a-z]{{2,}})?)"),
    "et_al": re.compile(rf"(?<![\w-])(?P<name>{_NAME})(?:,?\s*(?P<ini>{_INIT}))?\s*(?P<cue>и\s+др\.?|и\s+соавт\w*\.?"
                        rf"|et\s+al\.?)", re.IGNORECASE),
    "initials_after": re.compile(rf"(?<![\w-])(?P<name>{_NAME}),?\s+(?P<ini>{_INIT})(?![А-ЯЁA-Za-zа-яё])"),
    "initials_before": re.compile(rf"(?<![\w.])(?P<ini>{_INIT})\s?(?P<name>{_NAME})\b"),
    "po": re.compile(rf"(?<![\w-])(?P<cue>по)\s+(?P<name>{_NAME})(?![\w-])"),
    "surname": re.compile(rf"(?<![\w-])(?P<name>{_NAME})(?![\w-])"),
}
STRONG_CUES = ("works_of", "works_by", "et_al", "initials_after", "initials_before", "po")
# «горные / очистные … работы» are not «работы <автора>»; eponym heads: «закон Бингама» names a law, not a work
_MINING_WORKS = re.compile(r"(?:горн|очистн|подготовительн|закладочн|буровзрывн|проходческ|строительн|ремонтн|"
                           r"маркшейдерск|полев|камеральн|опытн|экспериментальн|научн|исследовательск)\w*\s+$", re.I)
_EPONYM_HEAD = re.compile(r"(?:закон\w*|ядр\w*|критери\w*|модел\w*|метод\w*|формул\w*|уравнени\w*|теори\w*|гипотез\w*|"
                          r"услови\w*|числ\w*|коэффициент\w*|решени\w*|задач\w*|принцип\w*|правил\w*|эффект\w*|схем\w*|"
                          r"тел\w*|сред\w*|элемент\w*|поправк\w*|прибор\w*|способ\w*|подход\w*)\s+$", re.I)
_ADJECTIVE_END = re.compile(r"(?:ск|цк)(?:ий|ая|ое|ие|ого|ой|ом|их|ому|ими|ую|им)$"
                            r"|(?:ный|ная|ное|ные|ного|ной|ном|ных|ному|ным|ную|ными)$")
# words that ask for citations or reference lists name no topic («ссылки на», «цитирует», «библиография», «cited by»)
_CITATION_WORDS = re.compile(r"(?<![а-яёА-ЯЁ\w])(?:ссылк\w*(?:\s+на)?|цитир\w*|библиограф\w*|references?(?:\s+to)?"
                             r"|cited(?:\s+by)?|citations?(?:\s+of)?)(?![а-яёА-ЯЁ\w])", re.IGNORECASE)
_TOPIC_STOP = re.compile(rf"(?<![а-яёА-ЯЁ\w])(?:{_WORK_WORDS}|автор\w*|список\w*|литератур\w*|все|всех|его|её|ее|их|"
                         r"papers?|works?|articles?|by|of|all)(?![а-яёА-ЯЁ\w])", re.IGNORECASE)


@dataclass(frozen=True)
class Mention:
    """A word of a query that may name an author: the cue that found it, the name as written (with initials when
    adjacent) and the span to remove from the topic."""

    cue: str
    name: str
    start: int
    end: int

    def as_record(self) -> dict[str, Any]:
        return {"cue": self.cue, "text": self.name}


def mentions(query: str) -> list[Mention]:
    """Candidate author mentions of ``query``, strong cues first, one per name span (no registry lookup here)."""
    text = query or ""
    out: list[Mention] = []
    taken: list[tuple[int, int]] = []
    for cue, rx in CUE_PATTERNS.items():
        for m in rx.finditer(text):
            name = m.group("name")
            ns, ne = m.span("name")
            if any(a <= ns < b for a, b in taken):
                continue
            if _ADJECTIVE_END.search(name.lower().split("-")[-1]):
                continue
            if cue == "works_of" and _MINING_WORKS.search(text[:m.start("cue")]):
                continue
            if cue == "surname" and _EPONYM_HEAD.search(text[:ns]):
                continue
            ini = m.groupdict().get("ini") or ""
            written = f"{name} {ini.strip()}".strip() if ini else name
            start = min(m.start(), ns) if cue != "surname" else ns
            out.append(Mention(cue, written, start, m.end()))
            taken.append((ns, ne))
    out.sort(key=lambda x: (STRONG_CUES.index(x.cue) if x.cue in STRONG_CUES else len(STRONG_CUES), x.start))
    return out


def topic_without(query: str, spans: Iterable[tuple[int, int]]) -> str:
    """The query without the given spans and the cue words («работы Баряха по сдвижению» → «по сдвижению»)."""
    text = query or ""
    keep, pos = [], 0
    for a, b in sorted(spans):
        keep.append(text[pos:a])
        pos = max(pos, b)
    keep.append(text[pos:])
    rest = _TOPIC_STOP.sub(" ", _CITATION_WORDS.sub(" ", " ".join(keep)))
    rest = re.sub(r"\b(?:и\s+др|и\s+соавт\w*|et\s+al)\b\.?", " ", rest, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", rest).strip(" \t,.;:–—-()«»\"")


@dataclass
class AuthorRoute:
    """What the author route found in a query: the registry-confirmed mentions, their clusters, the topic."""

    status: str
    mentions: list[dict[str, Any]] = field(default_factory=list)
    authors: list[AuthorMatch] = field(default_factory=list)
    topic: str = ""
    ignored: list[dict[str, Any]] = field(default_factory=list)


def resolve_route(query: str, index: AuthorIndex | None) -> AuthorRoute:
    """NOT_DETECTED (no candidate mention) | NO_REGISTRY_MATCH (candidates, none confirmed) | UNAVAILABLE (candidates
    but no registry) | MATCHED. Only EXACT_KEY / INFLECTED / TRANSLIT confirm; FUZZY hits are listed as ignored."""
    found = mentions(query)
    if not found:
        return AuthorRoute("NOT_DETECTED")
    if index is None:                         # without the registry only a cue's mention is worth reporting
        strong = [m for m in found if m.cue != "surname"]
        return AuthorRoute("UNAVAILABLE" if strong else "NOT_DETECTED", [m.as_record() for m in strong])
    confirmed: list[tuple[Mention, list[str]]] = []
    authors: dict[str, AuthorMatch] = {}
    ignored: list[dict[str, Any]] = []
    for m in found:
        hits = index.match(m.name)
        good = [h for h in hits if h.method in ROUTE_METHODS]
        if not good:
            if hits:
                ignored.append({**m.as_record(), "fuzzy": [h.author_id for h in hits][:5]})
            continue
        confirmed.append((m, [h.author_id for h in good]))
        for h in good:
            old = authors.get(h.author_id)
            if old is None or METHOD_ORDER[h.method] < METHOD_ORDER[old.method]:
                authors[h.author_id] = h
    if not confirmed:                         # a capitalised word alone is no cue: only a cue's miss is reported
        strong = [m for m in found if m.cue != "surname"]
        return AuthorRoute("NO_REGISTRY_MATCH" if strong else "NOT_DETECTED", [m.as_record() for m in strong],
                           ignored=ignored)
    topic = topic_without(query, [(m.start, m.end) for m, _ids in confirmed])
    recs = [{**m.as_record(), "author_ids": ids} for m, ids in confirmed]
    ordered = sorted(authors.values(), key=lambda a: (METHOD_ORDER[a.method], a.name_display, a.author_id))
    return AuthorRoute("MATCHED", recs, ordered, topic, ignored)
