"""Constructors and parsers of VKM document-layer IDs (CP-16 p. 3; H-14, H-15, H-34, H-50).

Principles:

1. An ID never depends on database internals, row numbers or file order.
2. It is deterministic where the object has a natural anchor; runs and snapshots are events with unique IDs.
3. An ID never silently changes meaning: a new meaning gets a new ID plus lineage (``object_lineage``) or a tombstone.
4. An ID is never a file name (``:`` is invalid on Windows).

Page objects: ``<page_id>:<b|f|t|m|c><12 hex>``, hash of (scope, kind, origin, region_origin, anchor, producer_key).
The anchor is the bbox in PAGE_PT_TL quantised to 0.1 pt; objects without geometry use an XML element path
(EPUB/DOCX) or the document-order ordinal plus a text hash. DOCX objects extracted from the XML are *document-scoped*
(``<source_id>:doc:<kind><hash>``, anchor = ``docx_paragraph_path``) so that a change of the pinned render or of the
render alignment does not change their IDs (H-50); their ``page_id`` column carries the render page (RENDER_DEPENDENT).

``producer_key = extractor_id | extraction_generation | models | raw_config_hash`` (H-14): software versions are *not*
part of it — they live in the provenance envelope. ``extraction_generation`` is a semantic version bumped consciously
when the regions or the raw content of an extractor change (new IDs + ``object_lineage`` rows).
"""
from __future__ import annotations

import hashlib
import re
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from vkm_world.core.io import sha256_file, sha256_json

from vkm_corpus.contracts.vocab import OBJECT_KIND_CODE, ObjectKind, PageUnit, RegionOrigin, Script
from vkm_corpus.ids import grammar
from vkm_corpus.ids.grammar import COMPILED, matches

__all__ = [
    "grammar", "matches", "IdError",
    "source_id", "source_number", "document_id", "page_id", "parse_page_id", "PageRef",
    "bbox_anchor", "xml_anchor", "ordinal_anchor", "producer_key", "object_id", "parse_object_id", "ObjectRef",
    "ObjectIdAllocator", "work_id", "work_number", "name_key", "NameKey", "author_id", "venue_key", "venue_id",
    "script_of", "artifact_id", "artifact_id_of_file", "artifact_hex", "new_run_id", "run_started_at", "step_id",
    "error_id", "commit_id", "snapshot_id", "link_id", "swl_id", "wrl_id", "srl_id", "wau_id", "bml_id", "oln_id",
    "normalize_doi", "normalize_isbn", "normalize_issn",
]

KIND_BY_CODE: dict[str, str] = {code: kind for kind, code in OBJECT_KIND_CODE.items()}


class IdError(ValueError):
    """An ID or an ID component violates the grammar."""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _require(kind: str, value: str) -> str:
    if not matches(kind, value):
        raise IdError(f"not a valid {kind} id: {value!r}")
    return value


# ---------------------------------------------------------------- sources, documents, pages
def source_id(number: int) -> str:
    if not 1 <= int(number) <= 999:
        raise IdError(f"source number out of range 1..999: {number}")
    return f"VKM-SRC-{int(number):03d}"


def source_number(sid: str) -> int:
    return int(_require("source", sid)[8:])


def document_id(sid: str) -> str:
    return f"{_require('source', sid)}:doc"


@dataclass(frozen=True)
class PageRef:
    source_id: str
    unit: str       # PageUnit value: p / r / s
    index: int      # physical/render/spine index from 1

    @property
    def page_id(self) -> str:
        return page_id(self.source_id, self.unit, self.index)


def page_id(sid: str, unit: str | PageUnit, index: int) -> str:
    """``VKM-SRC-NNN:<unit><index:04d>``; index is 1-based (PDF/DjVu physical index = ``pdf_page`` of evidence)."""
    _require("source", sid)
    unit = PageUnit(unit).value
    if not 1 <= int(index) <= 9999:
        raise IdError(f"page index out of range 1..9999: {index}")
    return f"{sid}:{unit}{int(index):04d}"


def parse_page_id(pid: str) -> PageRef:
    _require("page", pid)
    sid, rest = pid.split(":")
    return PageRef(sid, rest[0], int(rest[1:]))


# ---------------------------------------------------------------- page objects
def _q(value: float) -> str:
    v = round(float(value), 1)
    if v == 0:
        v = 0.0
    return f"{v:.1f}"


def bbox_anchor(x0: float, y0: float, x1: float, y1: float) -> str:
    """Region anchor in PAGE_PT_TL, quantised to 0.1 pt (float noise below 0.05 pt does not change the ID)."""
    if not (x1 > x0 and y1 > y0):
        raise IdError(f"degenerate bbox: {(x0, y0, x1, y1)}")
    return "bbox:" + ",".join(_q(v) for v in (x0, y0, x1, y1))


def xml_anchor(element_path: str) -> str:
    """Anchor of an object taken from markup (DOCX ``docx_paragraph_path``, EPUB element path)."""
    if not element_path or "|" in element_path or element_path != element_path.strip():
        raise IdError(f"bad element path for an anchor: {element_path!r}")
    return "xml:" + element_path


def _key_text(text: str) -> str:
    t = unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")
    return re.sub(r"\s+", " ", t).strip()


def ordinal_anchor(ordinal: int, text: str) -> str:
    """Anchor for objects without geometry and markup path: document-order ordinal + hash of the text."""
    if int(ordinal) < 0:
        raise IdError("ordinal must be >= 0")
    return f"ord:{int(ordinal):05d}|txt:{_sha(_key_text(text))[:16]}"


def producer_key(extractor_id: str, extraction_generation: int, raw_config_hash: str,
                 models: Iterable[Mapping[str, Any] | Any] = ()) -> str:
    """Key of the producer of an object's region/raw content (H-14).

    ``models``: only models that produced the region or the content (layout/recognition), as mappings or objects
    with ``role``, ``model_id``, ``model_revision``. Software versions are deliberately absent."""
    if not extractor_id or "|" in extractor_id:
        raise IdError(f"bad extractor_id {extractor_id!r}")
    if not matches("sha256", raw_config_hash):
        raise IdError("raw_config_hash must be 64 lowercase hex")
    items = []
    for m in models:
        get = m.get if isinstance(m, Mapping) else (lambda k, _m=m: getattr(_m, k))
        items.append(f"{get('role')}={get('model_id')}@{get('model_revision')}")
    parts = ["vkm-producer-v2", extractor_id, str(int(extraction_generation)), ",".join(sorted(items)) or "-",
             raw_config_hash]
    return _sha("|".join(parts))


def object_id(scope_id: str, object_kind: str | ObjectKind, origin: str, region_origin: str | None, anchor: str,
              producer: str, dup: int = 0) -> str:
    """Stable ID of a page (or DOCX document) object.

    ``scope_id`` is the ``page_id``; for objects taken from DOCX XML (``region_origin = DOCX_ELEMENT``) it is the
    ``document_id``. ``dup`` > 0 disambiguates identical anchors of the same producer (flag
    DUPLICATE_DETECTION_DISAMBIGUATED)."""
    kind = ObjectKind(object_kind)
    code = OBJECT_KIND_CODE.get(kind)
    if code is None:
        raise IdError(f"{kind} is not a page object kind")
    docx = region_origin == RegionOrigin.DOCX_ELEMENT
    if docx:
        _require("document", scope_id)
    else:
        _require("page", scope_id)
    if not (anchor.startswith(("bbox:", "xml:", "ord:"))):
        raise IdError(f"bad anchor {anchor!r}")
    if not matches("sha256", producer):
        raise IdError("producer key must be 64 lowercase hex")
    anc = anchor + (f"|dup:{int(dup)}" if dup else "")
    payload = "|".join(["vkm-docobj-v2", scope_id, kind.value, str(origin), str(region_origin or "-"), anc, producer])
    return f"{scope_id}:{code}{_sha(payload)[:12]}"


@dataclass(frozen=True)
class ObjectRef:
    source_id: str
    scope_id: str            # page_id or document_id
    page_id: str | None      # None for document-scoped objects
    object_kind: str
    hash12: str


def parse_object_id(oid: str) -> ObjectRef:
    _require("object", oid)
    sid, scope, tail = oid.split(":")
    scope_id = f"{sid}:{scope}"
    return ObjectRef(sid, scope_id, None if scope == "doc" else scope_id, KIND_BY_CODE[tail[0]], tail[1:])


class ObjectIdAllocator:
    """Allocates object IDs for one scope and detects identical anchors (→ ``dup:n`` and a flag).

    Feed objects in a deterministic order (e.g. sorted by bbox, then by raw content hash): the n-th repetition of an
    identical (kind, origin, region_origin, anchor, producer) gets ``dup:n``. A collision of two *different* anchors
    on the same ID raises ``IdError`` (ID_COLLISION) — never a silent deduplication."""

    def __init__(self) -> None:
        self._seen: dict[tuple[str, ...], int] = {}
        self._ids: dict[str, tuple[str, ...]] = {}

    def allocate(self, scope_id: str, object_kind: str, origin: str, region_origin: str | None, anchor: str,
                 producer: str) -> tuple[str, bool]:
        key = (scope_id, str(object_kind), str(origin), str(region_origin or "-"), anchor, producer)
        dup = self._seen.get(key, 0)
        self._seen[key] = dup + 1
        oid = object_id(scope_id, object_kind, origin, region_origin, anchor, producer, dup)
        full = key + (str(dup),)
        if oid in self._ids and self._ids[oid] != full:
            raise IdError(f"ID_COLLISION: {oid}")
        self._ids[oid] = full
        return oid, dup > 0


# ---------------------------------------------------------------- works, authors, venues
def work_id(anchor_source_id: str) -> str:
    """``VKM-WRK-NNN`` from the anchor source (the smallest VKM-SRC of an explicitly confirmed group, CP-09)."""
    return f"VKM-WRK-{source_number(anchor_source_id):03d}"


def work_number(wid: str) -> int:
    return int(_require("work", wid)[8:])


def script_of(text: str) -> str:
    cyr = lat = 0
    for ch in text:
        if not ch.isalpha():
            continue
        name = unicodedata.name(ch, "")
        if "CYRILLIC" in name:
            cyr += 1
        elif "LATIN" in name:
            lat += 1
    if cyr and lat:
        return Script.MIXED.value
    if cyr:
        return Script.CYRL.value
    if lat:
        return Script.LATN.value
    return Script.OTHER.value


@dataclass(frozen=True)
class NameKey:
    surname: str
    initials: str
    full_names: str
    script: str

    def __str__(self) -> str:
        return f"{self.surname}|{self.initials}|{self.full_names}|{self.script}"


_BRACKETS = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_NAME_TOKEN = re.compile(r"[^\W\d_]+(?:-[^\W\d_]+)*")


def name_key(name: str) -> NameKey:
    """Name key of a person string (H-15): NFKC, casefold, ё→е, bracketed remarks dropped; the first multi-letter
    token is the surname, one-letter tokens are initials, other tokens are full names; plus the script.

    ``Иванов И. И.`` ≡ ``Иванов И.И.`` ≡ ``И.И. Иванов``; ``Ivanov I.I.`` ≠ ``Иванов И.И.`` (different script);
    ``Иванов Иван Иванович`` ≠ ``Иванов И.И.`` (initials never merge with full names)."""
    stripped = _BRACKETS.sub(" ", name)
    text = unicodedata.normalize("NFKC", stripped).casefold().replace("ё", "е").replace(".", " ").replace(",", " ")
    tokens = _NAME_TOKEN.findall(text)
    if not tokens:
        raise IdError(f"no name tokens in {name!r}")
    long_tokens = [t for t in tokens if len(t) > 1]
    surname = long_tokens[0] if long_tokens else tokens[0]
    rest = list(tokens)
    rest.remove(surname)
    initials = "".join(t for t in rest if len(t) == 1)
    fulls = " ".join(t for t in rest if len(t) > 1)
    return NameKey(surname, initials, fulls, script_of(stripped))


def author_id(name: str) -> str:
    """``AUT-<12 hex>``: a *name-key cluster*, never a verified person (identity_status NAME_KEY_ONLY, H-15)."""
    return "AUT-" + _sha("vkm-author-v1|" + str(name_key(name)))[:12]


def normalize_issn(value: str) -> str:
    digits = re.sub(r"[^0-9Xx]", "", value).upper()
    if not re.fullmatch(r"[0-9]{7}[0-9X]", digits):
        raise IdError(f"not an ISSN: {value!r}")
    return f"{digits[:4]}-{digits[4:]}"


def venue_key(title: str | None = None, issn_l: str | None = None) -> str:
    if issn_l:
        return "issn:" + normalize_issn(issn_l)
    if not title or not title.strip():
        raise IdError("venue key needs a title or an ISSN-L")
    t = unicodedata.normalize("NFKC", title).casefold().replace("ё", "е")
    t = re.sub(r"[^\w]+", " ", t).strip()
    return f"title:{t}|{script_of(title)}"


def venue_id(title: str | None = None, issn_l: str | None = None) -> str:
    """``VEN-<12 hex>`` from a curated ISSN-L, else from the normalised title and its script (a translated journal
    and its original are different venues)."""
    return "VEN-" + _sha("vkm-venue-v1|" + venue_key(title, issn_l))[:12]


def normalize_doi(value: str | None) -> str | None:
    """Lower-case DOI without URL/``doi:`` prefix; ``None`` if the value does not look like a DOI."""
    if not value:
        return None
    v = value.strip().lower()
    v = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", v)
    v = v.rstrip(".,;)")
    return v if re.fullmatch(r"10\.[0-9]{4,9}/\S+", v) else None


def normalize_isbn(value: str | None) -> str | None:
    """ISBN-13 digits without hyphens (ISBN-10 is converted); ``None`` if the check digit fails."""
    if not value:
        return None
    digits = re.sub(r"[^0-9Xx]", "", value).upper()
    if len(digits) == 10:
        if not re.fullmatch(r"[0-9]{9}[0-9X]", digits):
            return None
        total = sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(digits))
        if total % 11:
            return None
        core = "978" + digits[:9]
    elif len(digits) == 13 and digits.isdigit():
        core = digits[:12]
        check = (10 - sum((1 if i % 2 == 0 else 3) * int(c) for i, c in enumerate(core)) % 10) % 10
        return digits if check == int(digits[12]) else None
    else:
        return None
    check = (10 - sum((1 if i % 2 == 0 else 3) * int(c) for i, c in enumerate(core)) % 10) % 10
    return core + str(check)


# ---------------------------------------------------------------- artifacts
def artifact_id(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def artifact_id_of_file(path: str | Path) -> str:
    return "sha256:" + sha256_file(path)


def artifact_hex(aid: str) -> str:
    return _require("artifact", aid)[7:]


# ---------------------------------------------------------------- runs, steps, errors, commits, snapshots
def new_run_id(now: datetime | None = None, token: str | None = None) -> str:
    """``RUN-<UTC start>-<8 random hex>``: unique event id (not deterministic by design)."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    token = token or secrets.token_hex(4)
    rid = f"RUN-{now:%Y%m%dT%H%M%SZ}-{token}"
    return _require("run", rid)


def run_started_at(rid: str) -> datetime:
    _require("run", rid)
    return datetime.strptime(rid[4:20], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


def step_id(run: str, source: str | None, page: str | None, stage: str, attempt: int, detail: str = "") -> str:
    _require("run", run)
    payload = "|".join(["vkm-step-v1", run, source or "-", page or "-", str(stage), str(int(attempt)), detail])
    return "STP-" + _sha(payload)[:16]


def error_id(run: str, step: str | None, code: str, seq: int) -> str:
    _require("run", run)
    return "ERR-" + _sha("|".join(["vkm-error-v1", run, step or "-", str(code), str(int(seq))]))[:16]


_VOLATILE_MARKER_KEYS = ("commit_id", "committed_at")


def commit_id(marker_body: Mapping[str, Any]) -> str:
    """``CMT-<16 hex>``: hash of the canonical marker body without ``commit_id`` and ``committed_at``.

    The body contains the run id, the parent commit and the partition paths, so every commit has its own id;
    a no-op (unchanged content) is detected by comparing ``content_fingerprint`` with the parent, not by the id
    (H-34)."""
    body = {k: v for k, v in marker_body.items() if k not in _VOLATILE_MARKER_KEYS}
    return "CMT-" + sha256_json(body)[:16]


def snapshot_id(created_at: datetime, manifest_body: Mapping[str, Any]) -> str:
    body = {k: v for k, v in manifest_body.items() if k not in ("snapshot_id", "created_at")}
    ts = created_at.astimezone(timezone.utc)
    return f"snap-{ts:%Y%m%dT%H%M%SZ}-{sha256_json(body)[:8]}"


# ---------------------------------------------------------------- link rows
def link_id(prefix: str, *parts: object) -> str:
    if prefix not in ("SWL", "WRL", "SRL", "WAU", "BML", "OLN"):
        raise IdError(f"unknown link prefix {prefix!r}")
    payload = "|".join([f"{prefix}-v1", *("-" if p is None else str(p) for p in parts)])
    return f"{prefix}-{_sha(payload)[:16]}"


def swl_id(source: str, work: str | None, link_type: str, page_start: int | None, page_end: int | None) -> str:
    return link_id("SWL", source, work, link_type, page_start, page_end)


def wrl_id(from_work: str, relation: str, to_work: str) -> str:
    return link_id("WRL", from_work, relation, to_work)


def srl_id(from_source: str, relation: str, to_source: str, from_page_start: int | None,
           to_page_start: int | None) -> str:
    return link_id("SRL", from_source, relation, to_source, from_page_start, to_page_start)


def wau_id(work: str, ordinal: int) -> str:
    return link_id("WAU", work, int(ordinal))


def bml_id(entry: str, cited_work: str, match_method: str) -> str:
    return link_id("BML", entry, cited_work, match_method)


def oln_id(old_object: str, new_object: str) -> str:
    return link_id("OLN", old_object, new_object)


def all_patterns() -> dict[str, str]:
    """Grammar table for documentation and export."""
    return dict(grammar.PATTERNS)


def _self_check(values: Sequence[tuple[str, str]]) -> list[str]:  # pragma: no cover - debugging helper
    return [f"{k}: {v}" for k, v in values if not COMPILED[k].fullmatch(v)]
