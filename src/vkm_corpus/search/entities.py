"""World objects of the PUBLIC catalogues and the pages that mention them: the entity index (agent Г, 13.10).

``catalogues/entities/entity_index.csv`` (built by ``scripts/build_entity_index.py`` from the committed PUBLIC
catalogues — boreholes, borehole picks, the C4 mining calendars, the observation and monitoring catalogues, the mining
geometry) has one row per (object, source, page): the kind (:data:`ENTITY_KINDS`), the object id of its catalogue,
the label as printed, the number and its key (:func:`number_key`), the site **as the catalogue states it** with the
catalogue's attribution, the source and the page (``page_id`` / ``page_index``: the physical page from 1, the base of
``pdf_page`` of the evidence records and of the canon's ``page_index``), the catalogue, its record ids and status. The
index is navigation, not evidence, and it never infers identity: borehole 75 of two sources or two mines are two
objects unless the catalogue itself merged them (the borehole catalogue keeps its «possibly the same» pairs apart, the
index repeats that in ``identity_note``). The pack of the catalogues serves it as the table :data:`TABLE`.

This module holds what the builder, the API (``GET /v1/entities``, MCP ``find_entities``) and the hybrid search (the
entity route of ``vkm_corpus.search.registry_routes``) share: the number and site normalisers and :class:`EntityIndex`,
the lookup over the table's rows. Every rule here is a MODEL_CHOICE of navigation, not scientific data.
"""
from __future__ import annotations

import re
import threading
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

TABLE = "entity_index"
RULE = "entity_index_v1"
BOREHOLE, PROFILE_LINE, BENCHMARK, BLOCK, PANEL, SHAFT = ("BOREHOLE", "PROFILE_LINE", "BENCHMARK", "BLOCK", "PANEL",
                                                          "SHAFT")
ENTITY_KINDS: tuple[str, ...] = (BOREHOLE, PROFILE_LINE, BENCHMARK, BLOCK, PANEL, SHAFT)
# reference kinds of ``search.object_refs`` → entity kinds (the numbered objects a query can name)
REF_KIND: dict[str, str] = {"BOREHOLE": BOREHOLE, "LINE": PROFILE_LINE, "BLOCK": BLOCK, "PANEL": PANEL,
                            "BENCHMARK": BENCHMARK}
KIND_ALIASES: dict[str, str] = {**{k: k for k in ENTITY_KINDS}, "LINE": PROFILE_LINE, "PROFILE": PROFILE_LINE,
                                "WELL": BOREHOLE, "REPER": BENCHMARK}
COLUMNS: tuple[str, ...] = (
    "entity_row_id", "entity_kind", "entity_id", "entity_label", "number", "number_key", "parent", "site",
    "site_attribution", "source_id", "page_id", "page_index", "page_basis", "object_labels", "catalogue",
    "record_ids", "status", "identity_note")
IDENTITY_RULE = ("one row per (object, source, page); an object is a catalogue record — the same number in another "
                 "source, mine or catalogue is not asserted to be the same object")
NOTE = ("navigation index built from the PUBLIC catalogues (AUTO, no quotes): where a catalogued object is mentioned; "
        "the site is the catalogue's attribution, not established by the index; not evidence")

_DASHES = re.compile(r"[‐‑‒–—−]")
_LAT2CYR = str.maketrans("aceopxkmhbty", "асеорхкмнвту")      # look-alike letters: «120c» ≡ «120с»
_CYR2LAT = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh", "з": "z", "и": "i", "й": "i",
            "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
            "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e",
            "ю": "yu", "я": "ya"}


def number_key(text: Any) -> str:
    """The comparable form of an object number: NFKC, lower case, ё → е, «№»/«No.» dropped, digit groups joined
    («1 036» → «1036»), spaces dropped («9 ВП» → «9вп»), dashes unified, Latin look-alike letters → Cyrillic
    («120c» → «120с»), outer punctuation stripped (a prime «559'» stays). Query numbers and catalogue numbers go
    through the same function."""
    s = unicodedata.normalize("NFKC", str(text or "")).lower().replace("ё", "е").replace("’", "'")
    s = _DASHES.sub("-", s)
    s = re.sub(r"№|#|\bno\.?(?=\s*\d)", " ", s)
    s = re.sub(r"(?<=\d)\s+(?=\d)", "", s)
    s = re.sub(r"\s+", "", s)
    s = s.strip(".,;:()[]{}\"«»*")
    return s.translate(_LAT2CYR)


def number_keys(cell: Any) -> list[str]:
    """The keys of an index cell (``;``-separated: a row whose catalogue gives reading variants has several)."""
    return [k for k in (x.strip() for x in str(cell or "").split(";")) if k]


def site_key(text: Any) -> str:
    """Comparable site text: lower case, Cyrillic → Latin, letters and digits only («СКРУ-1» ≡ «SKRU1»)."""
    s = unicodedata.normalize("NFKC", str(text or "")).lower().replace("ё", "е")
    s = "".join(_CYR2LAT.get(ch, ch) for ch in s)
    return re.sub(r"[^0-9a-z]+", "", s)


def entity_kind(value: str | None) -> str | None:
    """A request's kind (``BOREHOLE``, ``LINE`` …) → an entity kind; ``ValueError`` for an unknown one."""
    if value is None or not str(value).strip():
        return None
    kind = KIND_ALIASES.get(str(value).strip().upper())
    if kind is None:
        raise ValueError(f"kind is one of {', '.join(ENTITY_KINDS)} (LINE = PROFILE_LINE)")
    return kind


def _natural(key: str) -> tuple:
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in re.findall(r"\d+|\D+", key or ""))


@dataclass
class EntityIndex:
    """The rows of the table :data:`TABLE` with lookups by (kind, number key), source and page."""

    rows: list[dict[str, Any]]
    pack_id: str | None = None
    _by_key: dict[tuple[str, str], list[int]] = field(default_factory=dict, repr=False)
    _by_source: dict[str, list[int]] = field(default_factory=dict, repr=False)
    _by_page: dict[str, list[int]] = field(default_factory=dict, repr=False)

    @classmethod
    def from_rows(cls, rows: Iterable[Mapping[str, Any]], pack_id: str | None = None) -> "EntityIndex":
        out = cls([{c: (r.get(c) if r.get(c) is not None else "") for c in COLUMNS} for r in rows], pack_id)
        for i, r in enumerate(out.rows):
            for key in number_keys(r["number_key"]):
                out._by_key.setdefault((r["entity_kind"], key), []).append(i)
            if r["source_id"]:
                out._by_source.setdefault(r["source_id"], []).append(i)
            if r["page_id"]:
                out._by_page.setdefault(r["page_id"], []).append(i)
        return out

    def lookup(self, kind: str | None = None, number: str | None = None, site: str | None = None,
               source_id: str | None = None, page_id: str | None = None) -> list[dict[str, Any]]:
        """Rows matching every given filter, in index order. ``number`` is compared by :func:`number_key` (exact key;
        a row with reading variants matches any of them); ``site`` is a substring of the row's site by
        :func:`site_key`; no site → the rows of every site, each with its own."""
        kind = entity_kind(kind)
        idx: Iterable[int]
        if number is not None and str(number).strip():
            key = number_key(number)
            kinds = [kind] if kind else list(ENTITY_KINDS)
            idx = sorted({i for k in kinds for i in self._by_key.get((k, key), [])})
        elif page_id:
            idx = self._by_page.get(page_id, [])
        elif source_id:
            idx = self._by_source.get(source_id, [])
        else:
            idx = range(len(self.rows))
        want_site = site_key(site) if site else ""
        out = []
        for i in idx:
            r = self.rows[i]
            if kind and r["entity_kind"] != kind:
                continue
            if source_id and r["source_id"] != source_id:
                continue
            if page_id and r["page_id"] != page_id:
                continue
            if want_site and want_site not in site_key(r["site"]):
                continue
            out.append(r)
        return out


def objects(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The distinct objects of some index rows (kind, catalogue object id or number, site), each with its pages and
    sources — two sites (or two catalogue ids) of one number stay two entries."""
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for r in rows:
        key = (r["entity_kind"], r["entity_id"] or f"#{r['number_key']}", r["site"])
        g = groups.setdefault(key, {"entity_kind": r["entity_kind"], "entity_id": r["entity_id"] or None,
                                    "number": r["number"], "site": r["site"] or None,
                                    "site_attribution": r["site_attribution"] or None, "pages": [], "sources": []})
        if r["page_id"] and r["page_id"] not in g["pages"]:
            g["pages"].append(r["page_id"])
        if r["source_id"] and r["source_id"] not in g["sources"]:
            g["sources"].append(r["source_id"])
    out = list(groups.values())
    for g in out:
        g["n_pages"], g["n_sources"] = len(g["pages"]), len(g["sources"])
    out.sort(key=lambda g: (ENTITY_KINDS.index(g["entity_kind"]) if g["entity_kind"] in ENTITY_KINDS else 99,
                            _natural(number_key(g["number"])), g["site"] or "", g["entity_id"] or ""))
    return out


class CatalogueEntities:
    """:class:`EntityIndex` over a ``CatalogueStore`` (the published pack), rebuilt when the pack changes. ``index()``
    is None when no pack is published or the pack has no :data:`TABLE` (an older pack)."""

    def __init__(self, catalogues: Any) -> None:
        self.catalogues = catalogues
        self._lock = threading.Lock()
        self._cached: tuple[str | None, EntityIndex | None] | None = None

    def index(self) -> EntityIndex | None:
        from vkm_corpus.catalogues.store import CataloguesUnavailable

        if self.catalogues is None:
            return None
        try:
            pack_id = self.catalogues.pack_id()
        except CataloguesUnavailable:
            return None
        with self._lock:
            if self._cached is not None and self._cached[0] == pack_id:
                return self._cached[1]
        try:
            rows = self.catalogues.rows(TABLE, list(COLUMNS))
        except CataloguesUnavailable:
            return None
        index = EntityIndex.from_rows(rows, pack_id) if rows else None
        with self._lock:
            self._cached = (pack_id, index)
        return index
