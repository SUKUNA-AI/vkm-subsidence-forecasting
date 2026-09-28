"""Curated Work register (CP-09): ``WORK_REGISTER.csv`` + ``WORK_LINKS.csv`` → works, links, authors, venues.

Files live in PRIVATE ``00_registry/work_registry/`` (source-related metadata, receipt alongside); tests point the
loader to another directory with ``VKM_WORK_REGISTRY_DIR``. Groups come only from curated links: no fuzzy or ML
grouping; the canonical build never invents a ``work_id``.

WORK_REGISTER.csv — one row per work id (tombstones included). List cells are separated by ``;``:
``authors`` entries are ``Name`` or ``Name|ROLE``; ``external_ids`` entries are ``SCHEME:value:RELATION``.

WORK_LINKS.csv — every curated edge with a fixed type signature per ``link_kind``:
SOURCE_WORK (source → work; ``to_id`` empty only for FOREIGN_CONTENT of an unidentified work), WORK_WORK
(work ↔ work), SOURCE_SOURCE (source ↔ source, page ranges for SHARES_PAGES_WITH).
"""
from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from vkm_corpus import ids
from vkm_corpus.contracts.builders import build_row
from vkm_corpus.contracts.vocab import (
    SYMMETRIC_WORK_RELATIONS,
    AuthorRole,
    SourceRelationType,
    SourceWorkLinkType,
    VenueType,
    WorkLinkKind,
    WorkRelationType,
    WorkType,
)
from vkm_corpus.ids import grammar
from vkm_corpus.versions import PIPELINE_VERSION

WORK_REGISTRY_REL = "00_registry/work_registry"
WORK_REGISTER_COLUMNS = (
    "work_id", "status", "merged_into", "anchor_source_id", "work_type", "title", "title_en", "title_variants",
    "authors", "year_raw", "year", "venue", "venue_type", "volume", "issue", "pages", "edition", "publisher", "city",
    "doi", "isbn", "language", "external_ids", "basis_title", "basis_authors", "basis_year", "basis_venue",
    "basis_identifiers", "identity_status", "available_from", "available_from_precision", "available_from_basis",
    "curation_status", "curated_by", "curated_at", "curation_receipt", "notes",
)
WORK_LINKS_COLUMNS = (
    "link_kind", "from_id", "relation", "to_id", "from_page_start", "from_page_end", "to_page_start", "to_page_end",
    "part_label", "printed_range", "is_primary", "basis", "curation_status", "notes",
)
DEFAULT_VENUE_TYPE = {WorkType.JOURNAL_ARTICLE: VenueType.JOURNAL, WorkType.CONFERENCE_PAPER:
                      VenueType.PROCEEDINGS_SERIES, WorkType.JOURNAL_ISSUE: VenueType.JOURNAL,
                      WorkType.PROCEEDINGS_VOLUME: VenueType.PROCEEDINGS_SERIES}


class WorkRegistryError(ValueError):
    """The curated work files violate their grammar or type signatures."""


def _read_csv(path: Path, columns: tuple[str, ...]) -> tuple[list[dict[str, str]], str]:
    data = path.read_bytes()
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig"), newline=""))
    if tuple(reader.fieldnames or ()) != columns:
        raise WorkRegistryError(f"{path.name}: columns {reader.fieldnames} != {list(columns)}")
    return [dict(r) for r in reader], hashlib.sha256(data).hexdigest()


def _list(cell: str) -> list[str]:
    return [x.strip() for x in cell.split(";") if x.strip()]


def _int(cell: str) -> int | None:
    cell = cell.strip()
    return int(cell) if cell else None


def _none(cell: str) -> str | None:
    cell = cell.strip()
    return cell or None


def parse_authors(cell: str) -> list[tuple[str, str]]:
    out = []
    for entry in _list(cell):
        name, _, role = entry.partition("|")
        role = role.strip() or AuthorRole.AUTHOR.value
        AuthorRole(role)
        out.append((name.strip(), role))
    return out


def parse_external_ids(cell: str) -> list[dict[str, str]]:
    out = []
    for entry in _list(cell):
        scheme, _, rest = entry.partition(":")
        value, _, relation = rest.rpartition(":")
        if not (scheme and value and relation):
            raise WorkRegistryError(f"external id must be SCHEME:value:RELATION: {entry!r}")
        out.append({"scheme": scheme, "value": value, "relation": relation})
    return out


@dataclass
class WorkRegistry:
    """Rows of the seven registry datasets built from the curated work files (dicts ready for ``build_row``)."""

    works: list[Any] = field(default_factory=list)
    source_work_links: list[Any] = field(default_factory=list)
    work_relations: list[Any] = field(default_factory=list)
    source_relations: list[Any] = field(default_factory=list)
    authors: list[Any] = field(default_factory=list)
    work_authors: list[Any] = field(default_factory=list)
    venues: list[Any] = field(default_factory=list)
    inputs: dict[str, str] = field(default_factory=dict)


def load_work_registry(directory: Path, *, run_id: str, created_at: datetime, config_hash: str,
                       known_sources: set[str] | None = None) -> WorkRegistry:
    reg_path, links_path = directory / "WORK_REGISTER.csv", directory / "WORK_LINKS.csv"
    work_rows, work_sha = _read_csv(reg_path, WORK_REGISTER_COLUMNS)
    link_rows, links_sha = _read_csv(links_path, WORK_LINKS_COLUMNS)
    reg_ref = f"PRIVATE:{WORK_REGISTRY_REL}/WORK_REGISTER.csv"
    links_ref = f"PRIVATE:{WORK_REGISTRY_REL}/WORK_LINKS.csv"
    out = WorkRegistry(inputs={"work_register_sha256": work_sha, "work_links_sha256": links_sha})

    def env(kind: str, oid: str, *, origin: str = "REGISTRY", ref: str, sha: str, row: int | None) -> dict:
        return {"object_id": oid, "object_kind": kind, "origin": origin, "pipeline_version": PIPELINE_VERSION,
                "processing_run_id": run_id, "extractor_id": "registry-import", "extractor_version": PIPELINE_VERSION,
                "config_hash": config_hash, "created_at": created_at, "review_status": "NOT_APPLICABLE",
                "quality_flags": [], "input_ref": ref, "input_sha256": sha, "input_row": row}

    work_ids: set[str] = set()
    authors: dict[str, dict[str, Any]] = {}
    venues: dict[str, dict[str, Any]] = {}
    for i, r in enumerate(work_rows, 1):
        wid = r["work_id"]
        if not grammar.matches("work", wid) or wid in work_ids:
            raise WorkRegistryError(f"WORK_REGISTER row {i}: bad or duplicate work_id {wid!r}")
        work_ids.add(wid)
        venue_id = None
        if r["venue"].strip():
            vtype = r["venue_type"].strip() or DEFAULT_VENUE_TYPE.get(r["work_type"], VenueType.UNKNOWN).value
            if vtype != VenueType.UNKNOWN or r["work_type"] in DEFAULT_VENUE_TYPE:
                venue_id = ids.venue_id(r["venue"])
                v = venues.setdefault(venue_id, {"title": r["venue"].strip(), "type": vtype, "works": [], "row": i})
                v["works"].append(wid)
        year = _int(r["year"])
        doi = ids.normalize_doi(r["doi"]) if r["doi"].strip() else None
        if r["doi"].strip() and doi is None:
            raise WorkRegistryError(f"WORK_REGISTER row {i}: not a DOI {r['doi']!r}")
        isbns = []
        for raw in _list(r["isbn"]):
            norm = ids.normalize_isbn(raw)
            if norm is None:
                raise WorkRegistryError(f"WORK_REGISTER row {i}: invalid ISBN {raw!r}")
            isbns.append(norm)
        avail = _none(r["available_from"])
        out.works.append(build_row(
            "works", env("WORK", wid, ref=reg_ref, sha=work_sha, row=i), work_id=wid, status=r["status"],
            merged_into_work_id=_none(r["merged_into"]), anchor_source_id=r["anchor_source_id"],
            work_type=r["work_type"], title=_none(r["title"]), title_en=_none(r["title_en"]),
            title_variants=_list(r["title_variants"]),
            authors_display="; ".join(n for n, _ in parse_authors(r["authors"])) or None,
            publication_year=year, publication_year_raw=_none(r["year_raw"]),
            publication_date=date(year, 1, 1) if year else None,
            publication_date_precision="year" if year else "unknown", venue_id=venue_id,
            venue_display=_none(r["venue"]), volume=_none(r["volume"]), issue=_none(r["issue"]),
            pages_range=_none(r["pages"]), edition=_none(r["edition"]), publisher=_none(r["publisher"]),
            publisher_city=_none(r["city"]), doi=doi, isbn=sorted(set(isbns)), languages=_list(r["language"]),
            external_ids=parse_external_ids(r["external_ids"]),
            metadata_basis={"title": r["basis_title"] or "UNKNOWN", "authors": r["basis_authors"] or "UNKNOWN",
                            "year": r["basis_year"] or "UNKNOWN", "venue": r["basis_venue"] or "UNKNOWN",
                            "identifiers": r["basis_identifiers"] or "UNKNOWN"},
            identity_status=r["identity_status"],
            available_from=date.fromisoformat(avail) if avail else None,
            available_from_precision=_none(r["available_from_precision"]),
            available_from_basis=r["available_from_basis"] or "UNKNOWN", curation_status=r["curation_status"],
            curated_by=_none(r["curated_by"]),
            curated_at=date.fromisoformat(r["curated_at"]) if r["curated_at"].strip() else None,
            curation_receipt=_none(r["curation_receipt"]), notes=_none(r["notes"])))
        for ordinal, (name, role) in enumerate(parse_authors(r["authors"]), 1):
            key = ids.name_key(name)
            aid = ids.author_id(name)
            a = authors.setdefault(aid, {"name": name, "key": str(key), "script": key.script, "works": [], "row": i})
            a["works"].append(wid)
            out.work_authors.append(build_row(
                "work_authors", {**env("WORK_AUTHOR", ids.wau_id(wid, ordinal), ref=reg_ref, sha=work_sha, row=i),
                                 "curation_status": r["curation_status"], "basis": "CURATED_MANUAL", "notes": None},
                work_id=wid, author_id=aid, ordinal=ordinal, role=role, name_as_listed=name))
    for aid, a in sorted(authors.items()):
        out.authors.append(build_row(
            "authors", env("AUTHOR", aid, origin="DERIVED", ref=reg_ref, sha=work_sha, row=a["row"]),
            author_id=aid, name_display=a["name"], name_key=a["key"], script=a["script"],
            derived_from_work_ids=sorted(set(a["works"]))))
    for vid, v in sorted(venues.items()):
        key = ids.venue_key(v["title"])
        out.venues.append(build_row(
            "venues", env("VENUE", vid, origin="DERIVED", ref=reg_ref, sha=work_sha, row=v["row"]),
            venue_id=vid, venue_type=v["type"], title_display=v["title"], title_key=key,
            script=ids.script_of(v["title"]), derived_from_work_ids=sorted(set(v["works"]))))

    for i, r in enumerate(link_rows, 1):
        kind = r["link_kind"]
        base = {"curation_status": r["curation_status"], "basis": r["basis"], "notes": _none(r["notes"])}
        if kind == WorkLinkKind.SOURCE_WORK:
            SourceWorkLinkType(r["relation"])
            sid, wid = r["from_id"], _none(r["to_id"])
            if not grammar.matches("source", sid) or (wid is not None and wid not in work_ids):
                raise WorkRegistryError(f"WORK_LINKS row {i}: SOURCE_WORK needs a source and a known work")
            if known_sources is not None and sid not in known_sources:
                raise WorkRegistryError(f"WORK_LINKS row {i}: source {sid} is not in the register")
            ps, pe = _int(r["from_page_start"]), _int(r["from_page_end"])
            oid = ids.swl_id(sid, wid, r["relation"], ps, pe)
            out.source_work_links.append(build_row(
                "source_work_links", {**env("SOURCE_WORK_LINK", oid, ref=links_ref, sha=links_sha, row=i), **base},
                source_id=sid, work_id=wid, link_type=r["relation"],
                is_primary=r["is_primary"].strip().lower() == "true", page_start=ps, page_end=pe,
                part_label=_none(r["part_label"]), printed_range=_none(r["printed_range"])))
        elif kind == WorkLinkKind.WORK_WORK:
            rel = WorkRelationType(r["relation"])
            a, b = r["from_id"], r["to_id"]
            if a not in work_ids or b not in work_ids:
                raise WorkRegistryError(f"WORK_LINKS row {i}: WORK_WORK needs two known works")
            if rel in SYMMETRIC_WORK_RELATIONS and b < a:
                a, b = b, a
            oid = ids.wrl_id(a, rel.value, b)
            out.work_relations.append(build_row(
                "work_relations", {**env("WORK_RELATION", oid, ref=links_ref, sha=links_sha, row=i), **base},
                from_work_id=a, relation=rel.value, to_work_id=b, is_symmetric=rel in SYMMETRIC_WORK_RELATIONS))
        elif kind == WorkLinkKind.SOURCE_SOURCE:
            rel = SourceRelationType(r["relation"])
            a, b = r["from_id"], r["to_id"]
            if not (grammar.matches("source", a) and grammar.matches("source", b)):
                raise WorkRegistryError(f"WORK_LINKS row {i}: SOURCE_SOURCE needs two sources")
            fps, tps = _int(r["from_page_start"]), _int(r["to_page_start"])
            oid = ids.srl_id(a, rel.value, b, fps, tps)
            out.source_relations.append(build_row(
                "source_relations", {**env("SOURCE_RELATION", oid, ref=links_ref, sha=links_sha, row=i), **base},
                from_source_id=a, relation=rel.value, to_source_id=b, from_page_start=fps,
                from_page_end=_int(r["from_page_end"]), to_page_start=tps, to_page_end=_int(r["to_page_end"])))
        else:
            raise WorkRegistryError(f"WORK_LINKS row {i}: unknown link_kind {kind!r}")
    return out


def write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, Any]]) -> str:
    """Deterministic CSV (UTF-8, LF, all columns quoted minimally); returns the sha256 of the bytes."""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(columns), lineterminator="\n", extrasaction="raise")
    w.writeheader()
    for r in rows:
        w.writerow({c: "" if r.get(c) is None else r.get(c) for c in columns})
    data = buf.getvalue().encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()
