"""Bootstrap of the curated Work register (CP-09, design D §4.5, §13.2): proposes ``WORK_REGISTER.csv`` and
``WORK_LINKS.csv`` for review. Nothing is written to PRIVATE: the proposal goes to an output directory with a receipt
and an ambiguity report; a human/coordinator curates it and commits it to PRIVATE ``00_registry/work_registry/``.

Groups come only from the explicit list of audit A §2.8 (``CURATED_GROUPS`` below, basis REPOSITORY_AUDIT) and from
explicit Phase-1 evidence (pages carrying another article's bibliography). Everything else is one Work per file
(BOOTSTRAP_SINGLETON). Foreign pages inferred from the audit A page equivalences were confirmed by the coordinator
(2026-09-28) and are CURATED. Metadata order per field (DN-D-02):
Phase-1 coverage master (001–041) → intake manifest identity → literature-hunt table (only via
``acquired_2026_09_27`` or the FIRST ``ALREADY_LOCAL:`` token, DN-D-18) → register notes → unknown. Conflicts are not
averaged: the chosen value keeps its basis and the conflict goes to the ambiguity report.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vkm_corpus import ids
from vkm_corpus.registry.sources import REGISTER_REL, load_register
from vkm_corpus.registry.works import WORK_LINKS_COLUMNS, WORK_REGISTER_COLUMNS, write_csv
from vkm_corpus.versions import PIPELINE_VERSION

HUNT_REL = "00_registry/literature_hunt_2026-09-27/PHYSICAL_WORLD_SOURCE_PRIORITY.csv"
INTAKE_REL = "00_registry/intake"
SEED_VERSION = "1"

GENRE_TO_WORK_TYPE = {
    "journal_article": "JOURNAL_ARTICLE", "monograph": "MONOGRAPH", "textbook": "TEXTBOOK",
    "dissertation": "DISSERTATION", "conference_paper": "CONFERENCE_PAPER", "technical_report": "TECHNICAL_REPORT",
    "training_manual": "TRAINING_MANUAL", "teaching_manual": "TEACHING_MANUAL",
    "normative_document": "NORMATIVE_DOCUMENT", "institutional_report": "INSTITUTIONAL_REPORT",
    "dissertation_abstract": "DISSERTATION_ABSTRACT", "presentation": "PRESENTATION",
    "practice_manual": "PRACTICE_MANUAL", "thesis_secondary": "THESIS", "thesis": "THESIS",
    "methodical_guidance": "METHODICAL_GUIDANCE", "dataset": "DATASET", "book_chapter": "BOOK_CHAPTER",
    "patent": "PATENT", "bibliographic_index": "BIBLIOGRAPHIC_INDEX", "reference_tables": "REFERENCE_TABLES",
    "proceedings_volume": "PROCEEDINGS_VOLUME", "journal_issue": "JOURNAL_ISSUE",
    "retired_legacy_archive": "PROJECT_DATA_PACKAGE",
}
# the 8 DjVu files whose title pages were not verified (CP-10)
# DjVu files whose identity rested on file name and page count only. All eight were checked on their title pages by
# agent C on 2026-09-28 (CP-10; receipt PRIVATE 00_registry/djvu_title_check_2026-09-28/RECEIPT.json), so none is left.
DJVU_UNVERIFIED: set[str] = set()
TITLE_PAGE_VERIFIED = {"VKM-SRC-053", "VKM-SRC-054", "VKM-SRC-221", "VKM-SRC-229", "VKM-SRC-230", "VKM-SRC-240",
                       "VKM-SRC-245", "VKM-SRC-248"}
CONTAINERS = {"VKM-SRC-089", "VKM-SRC-193", "VKM-SRC-203", "VKM-SRC-204", "VKM-SRC-205", "VKM-SRC-206",
              "VKM-SRC-207"}
VOLUME_TYPES = {"proceedings_volume", "journal_issue", "collection", "book"}
# hunt rows that describe a container volume itself (all other hunt rows of a container are its components)
CONTAINER_VOLUME_PWL = {"PWL-0381"}


def S(n: int) -> str:
    return ids.source_id(n)


@dataclass
class Group:
    """An explicitly confirmed group of audit A §2.8: sources → one work, with the link type of each source."""

    members: dict[str, dict[str, Any]]
    work_type: str
    note: str


# ---------------------------------------------------------------- audit A §2.8 (curated by A + coordinator, CP-09)
CURATED_GROUPS = [
    Group({S(13): {"link": "FULL_COPY"}, S(25): {"link": "FULL_COPY"}, S(202): {"link": "FULL_COPY"}},
          "TEACHING_MANUAL", "013 original page ZIP (absent by register); 025 PDF assembled from 013; 202 another "
          "digital copy; pagination of 025 and 202 is not aligned automatically"),
    Group({S(147): {"link": "FRONT_MATTER_ONLY"}, S(232): {"link": "FULL_COPY"}}, "MONOGRAPH",
          "147 is the free front-matter preview only; 232 the full scan"),
    Group({S(208): {"link": "PART", "part_label": "1", "printed_range": "1-100"},
           S(209): {"link": "PART", "part_label": "2", "printed_range": "101-199"}}, "TEXTBOOK",
          "two parts of one book with continuous printed pagination"),
]
WORK_RELATIONS = [  # (from source, relation, to source, note)
    (S(229), "VOLUME_SET_SIBLING", S(230), "volumes 1 and 2 of one set; two works"),
    (S(20), "SERIES_SIBLING", S(28), "parts 1 (1998) and 2 (1999) of one training series"),
    (S(1), "ABSTRACT_OF", S(196), "author abstract of the dissertation"),
    (S(2), "COMPANION_OF", S(201), "companion materials of the same author and series"),
    (S(14), "NOT_SAME", S(37), "look-alike titles; different works"),
]
SOURCE_RELATIONS = [  # (from, relation, to, from_pages, to_pages, basis, curation, note)
    (S(25), "DERIVED_FROM", S(13), None, None, "REPOSITORY_AUDIT", "CURATED", "025 assembled from the pages of 013"),
    (S(22), "CONTAINS_COPY_OF", S(23), None, None, "SHA256_IDENTITY", "CURATED",
     "the retired archive 022 contains a byte copy of 023; 022 is never unpacked"),
    (S(31), "SHARES_PAGES_WITH", S(5), (5, 5), (1, 1), "REPOSITORY_AUDIT", "CURATED", "identical page"),
    (S(32), "SHARES_PAGES_WITH", S(5), (1, 1), (6, 6), "REPOSITORY_AUDIT", "CURATED", "identical page"),
    (S(34), "SHARES_PAGES_WITH", S(199), (5, 5), (1, 1), "REPOSITORY_AUDIT", "CURATED",
     "the end of 034 and the start of 199 share a printed page"),
]
# page ranges that carry another work (bibliography attribution is then NULL, never the host)
FOREIGN_CONTENT = [  # (source, first page, last page, foreign work source or None, basis, curation, note)
    (S(21), 1, 1, None, "PHASE1_EVIDENCE", "CURATED", "page 1 carries the reference list of a preceding article"),
    (S(30), 1, 1, None, "PHASE1_EVIDENCE", "CURATED", "top of page 1 is the end of a preceding article"),
    (S(39), 1, 1, None, "PHASE1_EVIDENCE", "CURATED", "top of page 1 is the reference list of a preceding article"),
    # the next three follow from the audit A page equivalences above; confirmed by the coordinator on 2026-09-28
    (S(5), 1, 1, S(31), "REPOSITORY_AUDIT", "CURATED",
     "page 1 = page 5 of 031: carries the end (with references) of 031"),
    (S(32), 1, 1, S(5), "REPOSITORY_AUDIT", "CURATED",
     "page 1 = page 6 of 005: carries the end (with references) of 005"),
    (S(199), 1, 1, S(34), "REPOSITORY_AUDIT", "CURATED",
     "page 1 shares a printed page with the end (with references) of 034"),
]
# notes that must never become links (audit A §2.8, DN-D-19): container 205 "contains" is contradictory
NEVER_LINK = {S(205): "contradictory 'contains' of the hunt rows (issue numbers disagree); not linked in v0"}
# works whose bibliographic data must be set by hand (no usable catalogue row); curated from register notes
MANUAL_METADATA = {
    # title pages of the eight DjVu files (agent C, 2026-09-28, CP-10)
    S(230): {"title": "Механика сплошной среды. Т. 2", "authors": "Седов Л.И.", "year": "2004", "language": "ru",
             "publisher_city": "Санкт-Петербург: Лань", "basis": "TITLE_PAGE_VERIFIED"},
    S(229): {"title": "Механика сплошной среды. Т. 1", "authors": "Седов Л.И.", "year": "1994", "language": "ru",
             "publisher_city": "Москва: Наука", "isbn": "5-02-007052-1", "basis": "TITLE_PAGE_VERIFIED"},
    S(221): {"title": "Таблицы координат Гаусса–Крюгера для широт от 32° до 80° через 5′ и для долгот от 0° до 3½° "
                      "через 7½′ и таблицы размеров рамок и площадей трапеций топографических съёмок. Эллипсоид "
                      "Красовского",
             "authors": "Вировец А.М.|COMPILER; Мауэрер В.Г.|COMPILER; Троицкий Б.В.|COMPILER; Иванов В.Ф.|COMPILER; "
                        "Петрова Е.Ф.|COMPILER; Барвенко Е.И.|COMPILER; Шишкин В.Н.|COMPILER",
             "year": "1948", "language": "ru",
             "publisher_city": "Москва: Издательство геодезической и картографической литературы ГУГК",
             "basis": "TITLE_PAGE_VERIFIED"},
    S(240): {"title": "Математические методы в гидрогеологии и инженерной геологии", "authors": "Антонов В.В.",
             "year": "1987", "language": "ru",
             "publisher_city": "Ленинград: Ленинградский горный институт им. Г.В. Плеханова",
             "basis": "TITLE_PAGE_VERIFIED"},
    S(53): {"publisher_city": "Москва: Наука", "basis": "TITLE_PAGE_VERIFIED"},
    S(245): {"publisher_city": "Москва: Советское радио", "basis": "TITLE_PAGE_VERIFIED"},
    S(248): {"publisher_city": "Москва: Мир", "basis": "TITLE_PAGE_VERIFIED"},
    S(203): {"title": "Стратегия и процессы освоения георесурсов: материалы научной сессии Горного института УрО "
                      "РАН, 19–23 апреля 2004 г.", "year": "2004", "language": "ru", "basis": "REGISTER_NOTES"},
    S(204): {"title": "Стратегия и процессы освоения георесурсов: сборник научных трудов. Вып. 12", "year": "2014",
             "language": "ru", "basis": "REGISTER_NOTES"},
    S(205): {"title": "Горное эхо. 2020. № 3 (80)", "year": "2020", "venue": "Горное эхо", "language": "ru",
             "basis": "REGISTER_NOTES"},
    S(206): {"title": "Горное эхо. 2018. № 3 (72)", "year": "2018", "venue": "Горное эхо", "language": "ru",
             "basis": "REGISTER_NOTES"},
    S(207): {"title": "Горное эхо. 2010. № 1–2 (39–40)", "year": "2010", "venue": "Горное эхо", "language": "ru",
             "basis": "REGISTER_NOTES"},
    S(89): {"title": "Стратегия и процессы освоения георесурсов: сборник научных трудов ГИ УрО РАН. Вып. 16",
            "year": "2018", "language": "ru", "basis": "REGISTER_NOTES"},
}

# further facts of audit A §2.8 recorded on a work (EDITION_OF 011; NOT_SAME against works outside the corpus)
EXTRA_FACTS = {
    S(11): {"edition": "2-е изд., перераб.",
            "notes": "second edition; the first edition (2001) is outside the corpus and cited there as another "
                     "edition (EDITION_OF without an identifier)"},
    S(50): {"external_ids": "ISBN:9785247034131:NOT_SAME",
            "notes": "not the Borzakovsky-Papulov handbook (register notes: its ISBN is recorded as NOT_SAME)"},
    S(34): {"notes": "not S01 (Gornyi Zhurnal 2023 no. 11) per audit A §2.8"},
    S(14): {"notes": "NOT_SAME as VKM-WRK-037 (look-alike title)"},
    S(229): {"edition": "5-е изд., испр.",
             "notes": "volume 1; volume 2 (VKM-WRK-230) is another edition and publisher"},
    S(230): {"edition": "6-е изд., стер.",
             "notes": "volume 2; series 'Классический университетский учебник'; volume 1 (VKM-WRK-229) is another "
                      "edition and publisher"},
    S(240): {"notes": "учебное пособие"},
    S(54): {"notes": "physical page 270 is a placeholder page of the source electronic library, not a book page"},
}
_EDITORIAL = ("ред.", "ред ", "отв.", "сост.", "пер.", "под ред", "редакционн", "зав. ред")
_DOI = re.compile(r"\b(10\.[0-9]{4,9}/[^\s;,]+)", re.I)
_ISBN = re.compile(r"ISBN(?:-1[03])?[:\s]*([0-9Xx][0-9Xx\- ]{8,16}[0-9Xx])")
_YEAR = re.compile(r"^(1[89][0-9]{2}|20[0-9]{2})(?:[–-](?:1[89][0-9]{2}|20[0-9]{2}))?$")
_PERSON = re.compile(r"[A-ZА-ЯЁ][\w'\-]+\s+(?:[A-ZА-ЯЁ]\.\s?)+|(?:[A-ZА-ЯЁ]\.\s?)+\s*[A-ZА-ЯЁ][\w'\-]+")
_AUTHOR_PREFIX = re.compile(r"^(?:[A-ZА-ЯЁ][\w\-]+\s+(?:[A-ZА-ЯЁ]\.\s?){1,2}[,;]?\s*)+")
_NOTE_END = re.compile(r"\s(?:Identity:|Open-access copy|Relations:|Acquired by|Not read in|Closes hunt|Hunt row)")


def _read_csv(path: Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return [dict(r) for r in csv.DictReader(f)]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def split_people(text: str) -> list[tuple[str, str]]:
    """Authors of a free-text author field: ``;``/``,``-separated, bracketed remarks dropped; a supervisor named in a
    remark ("науч. рук. X") becomes a SUPERVISOR entry; "(корпоративный автор)" → CORPORATE_AUTHOR."""
    out: list[tuple[str, str]] = []
    depth, cur, parts = 0, [], []
    for ch in text:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        if ch in ",;" and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    for raw in parts:
        remarks = re.findall(r"\(([^)]*)\)", raw)
        name = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", raw)
        name = re.sub(r"\s+", " ", name).strip(" ;,")
        if (not name or len(name) < 3 or len(name) > 80 or ":" in name or re.search(r"\d{4}", name)
                or name.lower().startswith(("et al", "и др") + _EDITORIAL)):
            continue
        role = "CORPORATE_AUTHOR" if any("корпоратив" in r for r in remarks) else "AUTHOR"
        if "|" in name:                        # explicit role of curated metadata: "Name|COMPILER"
            name, _, explicit = name.rpartition("|")
            name, role = name.strip(), explicit.strip() or role
        out.append((name, role))
        for r in remarks:
            m = re.search(r"рук\.?\s*(?:[\wа-яё.,\-]+\s+){0,4}?((?:[А-ЯЁ]\.\s?){1,2}[А-ЯЁ][а-яё\-]+|[А-ЯЁ][а-яё\-]+"
                          r"\s+(?:[А-ЯЁ]\.\s?){1,2})", r)
            if m:
                out.append((re.sub(r"\s+", " ", m.group(1)).strip(), "SUPERVISOR"))
    seen, uniq = set(), []
    for n, r in out:
        if (n, r) not in seen:
            seen.add((n, r))
            uniq.append((n, r))
    return uniq


def parse_notes_bibliography(notes: str) -> dict[str, str]:
    """``Author; Author; YEAR; Title; [EN]; venue...`` prefix written by the intake tools (REGISTER_NOTES)."""
    head = _NOTE_END.split(notes, maxsplit=1)[0]
    tokens = [t.strip() for t in head.split("; ")]
    for i, t in enumerate(tokens):
        if _YEAR.match(t.strip(" .")):
            if i + 1 >= len(tokens):
                return {}
            authors = []
            for a in reversed(tokens[:i]):
                if not _PERSON.search(a) or ":" in a or len(a) > 80:
                    break
                authors.insert(0, a)
            out = {"authors": "; ".join(authors), "year": t.strip(" ."), "title": tokens[i + 1].strip(" .")}
            rest = tokens[i + 2:]
            if rest and rest[0].startswith("["):
                out["title_en"] = rest[0].strip("[]. ")
                rest = rest[1:]
            if rest:
                out["venue_raw"] = rest[0].split(", no.")[0].split(", vol.")[0].split(", т.")[0].strip(" .")
            return out
    return {}


def _year(text: str) -> str:
    """First year of a raw year field; empty for a range of several years (multi-volume sets)."""
    if re.search(r"(1[89][0-9]{2}|20[0-9]{2})\s*[–-]\s*(1[89][0-9]{2}|20[0-9]{2})", text or ""):
        return ""
    m = re.search(r"(1[89][0-9]{2}|20[0-9]{2})", text or "")
    return m.group(1) if m else ""


def _lang(title: str, given: str) -> str:
    code = given.strip().split(";")[0].split(",")[0].split("/")[0].strip()[:2].lower()
    if re.fullmatch(r"[a-z]{2}", code):
        return code
    cyr = sum(1 for ch in title or "" if "а" <= ch.lower() <= "я" or ch.lower() == "ё")
    lat = sum(1 for ch in title or "" if "a" <= ch.lower() <= "z")
    return "ru" if cyr > lat else ("en" if lat else "")


def _strip_author_prefix(title: str) -> str:
    rest = _AUTHOR_PREFIX.sub("", title or "").strip()
    return rest if rest and rest != title else title


def _isbns(*texts: str) -> list[str]:
    out = []
    for t in texts:
        for raw in re.split(r"[;,]", t or ""):
            norm = ids.normalize_isbn(raw)
            if norm:
                out.append(norm)
        for m in _ISBN.finditer(t or ""):
            norm = ids.normalize_isbn(m.group(1))
            if norm:
                out.append(norm)
    return sorted(set(out))


def _doi(*texts: str) -> str:
    for t in texts:
        for m in _DOI.finditer(t or ""):
            d = ids.normalize_doi(m.group(1))
            if d:
                return d
    return ""


@dataclass
class SeedResult:
    works: list[dict[str, Any]] = field(default_factory=list)
    links: list[dict[str, Any]] = field(default_factory=list)
    ambiguities: list[dict[str, Any]] = field(default_factory=list)
    inputs: dict[str, dict[str, Any]] = field(default_factory=dict)


def hunt_rows_by_source(hunt: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    """PWL rows of a source: ``acquired_2026_09_27`` = source id, or the FIRST ``ALREADY_LOCAL:`` token (DN-D-18)."""
    by: dict[str, list[dict[str, str]]] = defaultdict(list)
    for h in hunt:
        got = set()
        a = h.get("acquired_2026_09_27", "").strip()
        if ids.grammar.matches("source", a):
            got.add(a)
        m = re.match(r"ALREADY_LOCAL:(VKM-SRC-[0-9]{3})", h.get("local_status", "").strip())
        if m:
            got.add(m.group(1))
        for sid in got:
            by[sid].append(h)
    return by


def propose_work_registry(resources_root: Path, coverage_path: Path, *, curated_by: str = "coordinator-review",
                          now: datetime | None = None) -> SeedResult:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    res = SeedResult()
    reg_rows, reg_sha = load_register(resources_root / REGISTER_REL)
    reg = {r["resource_id"]: r for r in reg_rows}
    coverage = {r["source_id"]: r for r in _read_csv(coverage_path)}
    hunt_path = resources_root / HUNT_REL
    hunt = _read_csv(hunt_path)
    by_hunt = hunt_rows_by_source(hunt)
    res.inputs = {"PRIVATE:" + REGISTER_REL: {"sha256": reg_sha, "rows": len(reg_rows)},
                  "PUBLIC:evidence/sources/SOURCE_COVERAGE_MASTER.csv": {"sha256": _sha(coverage_path),
                                                                          "rows": len(coverage)},
                  "PRIVATE:" + HUNT_REL: {"sha256": _sha(hunt_path), "rows": len(hunt)}}
    intake: dict[str, dict[str, str]] = {}
    for mf in sorted((resources_root / INTAKE_REL).glob("*/IMPORT_MANIFEST.csv")):
        rows = _read_csv(mf)
        res.inputs["PRIVATE:" + mf.relative_to(resources_root).as_posix()] = {"sha256": _sha(mf), "rows": len(rows)}
        for r in rows:
            sid = (r.get("resource_id") or r.get("source_id") or "").strip()
            if ids.grammar.matches("source", sid):
                intake.setdefault(sid, r)

    group_of: dict[str, Group] = {}
    for g in CURATED_GROUPS:
        for sid in g.members:
            group_of[sid] = g

    def meta_for(sid: str) -> tuple[dict[str, str], dict[str, str]]:
        """(fields, basis) of the bibliographic metadata of one source."""
        r = reg[sid]
        f: dict[str, str] = {}
        basis = {"title": "UNKNOWN", "authors": "UNKNOWN", "year": "UNKNOWN", "venue": "UNKNOWN",
                 "identifiers": "UNKNOWN"}
        hrows = by_hunt.get(sid, [])
        volume_rows = [h for h in hrows if h["global_id"] in CONTAINER_VOLUME_PWL]
        hrow = (volume_rows[0] if volume_rows else None) if sid in CONTAINERS else (hrows[0] if len(hrows) == 1 else
                                                                                    None)
        notes_bib = parse_notes_bibliography(r["notes"])
        cov = coverage.get(sid)
        manual = MANUAL_METADATA.get(sid)
        if manual:
            f.update({k: v for k, v in manual.items() if k != "basis"})
            for k in ("title", "authors", "year"):
                if manual.get(k) is not None:
                    basis[k] = manual["basis"]
            if manual.get("venue"):
                basis["venue"] = manual["basis"]
        if cov and cov.get("title"):
            for k, src in (("title", "title"), ("authors", "authors"), ("year", "year")):
                if not f.get(k) and cov.get(src):
                    f[k] = cov[src]
                    basis[k] = "PHASE1_COVERAGE_MASTER"
        exact = intake.get(sid, {}).get("exact_identity", "")
        if exact:
            parsed = parse_notes_bibliography(exact.replace("; ", "; "))
            ib = intake[sid].get("identity_basis", "")
            b = "CATALOGUE_DATA_UNVERIFIED" if "UNVERIFIED" in ib else "INTAKE_MANIFEST"
            for k in ("title", "authors", "year"):
                if not f.get(k) and parsed.get(k):
                    f[k] = parsed[k]
                    basis[k] = b
            if not f.get("title_en") and parsed.get("title_en"):
                f["title_en"] = parsed["title_en"]
        if hrow:
            for k, src in (("title", "title_ru"), ("authors", "authors_ru"), ("year", "year")):
                val = hrow.get(src, "").strip()
                if not val and k == "title":
                    val = hrow.get("title_en", "").strip()
                if not val and k == "authors":
                    val = hrow.get("authors_translit", "").strip()
                if not f.get(k) and val:
                    f[k] = val
                    basis[k] = "HUNT_TABLE"
            for k, src in (("title_en", "title_en"), ("title_variants", "title_variants"), ("volume", "volume"),
                           ("issue", "issue"), ("pages", "pages"), ("language", "language"),
                           ("publisher_city", "publisher_city")):
                if not f.get(k) and hrow.get(src, "").strip():
                    f[k] = hrow[src].strip()
            if hrow.get("venue", "").strip() and not f.get("venue"):
                f["venue"] = hrow["venue"].strip()
                basis["venue"] = "HUNT_TABLE"
        if sid in CONTAINERS:
            notes_bib = {k: v for k, v in notes_bib.items() if k not in ("authors", "title", "title_en")}
            f.setdefault("authors", "")
        for k in ("title", "authors", "year", "title_en"):
            if sid in CONTAINERS and k == "authors":
                continue
            if not f.get(k) and notes_bib.get(k):
                f[k] = notes_bib[k]
                if k in basis:
                    basis[k] = "REGISTER_NOTES"
        if not f.get("venue") and notes_bib.get("venue_raw"):
            f["venue"] = notes_bib["venue_raw"]
            basis["venue"] = "REGISTER_NOTES"
        doi = ids.normalize_doi(hrow.get("doi", "")) if hrow else None
        if doi:
            basis["identifiers"] = "HUNT_TABLE"
        elif sid not in CONTAINERS:
            doi = _doi(r["notes"])
            if doi:
                basis["identifiers"] = "REGISTER_NOTES"
        f["doi"] = doi or f.get("doi", "")
        isbns = _isbns(hrow.get("isbn", "")) if hrow else []
        if isbns:
            basis["identifiers"] = "HUNT_TABLE" if basis["identifiers"] == "UNKNOWN" else basis["identifiers"]
        else:
            first = _ISBN.search(r["notes"])
            isbns = _isbns("ISBN " + first.group(1)) if first else []
            if isbns and basis["identifiers"] == "UNKNOWN":
                basis["identifiers"] = "REGISTER_NOTES"
        if not isbns and manual and manual.get("isbn"):
            isbns = _isbns("ISBN " + manual["isbn"])
            basis["identifiers"] = manual["basis"]
        f["isbn"] = "; ".join(isbns)
        if sid in TITLE_PAGE_VERIFIED:          # the title page agrees with these values (CP-10 receipt)
            for k in ("title", "authors", "year"):
                if f.get(k):
                    basis[k] = "TITLE_PAGE_VERIFIED"
            if f.get("venue") or f.get("publisher_city"):
                basis["venue"] = "TITLE_PAGE_VERIFIED"
        if f.get("title"):
            f["title"] = _strip_author_prefix(f["title"])
        return f, basis

    def identity_for(sid: str) -> str:
        if sid in DJVU_UNVERIFIED:
            return "FILENAME_PAGECOUNT_UNVERIFIED"
        if sid in TITLE_PAGE_VERIFIED:
            return "VERIFIED_IN_FILE"
        if sid in coverage and coverage[sid].get("coverage_level") in ("FULLY_REVIEWED", "RELEVANT_SECTIONS_REVIEWED"):
            return "VERIFIED_IN_FILE"
        ib = " ".join(intake.get(sid, {}).get(k, "") for k in ("identity_basis", "identity_check"))
        if re.search(r"TITLE_PAGE|DOI|AUTHOR_TITLE|AUTHOR_HEADER|first author and title|author, title", ib):
            return "VERIFIED_IN_FILE"
        return "CATALOGUE_UNVERIFIED"

    work_of: dict[str, str] = {}
    done_groups: set[int] = set()
    for sid in sorted(reg):
        g = group_of.get(sid)
        if g is not None:
            if id(g) in done_groups:
                continue
            done_groups.add(id(g))
            anchor = min(g.members)
            members = sorted(g.members)
        else:
            anchor, members = sid, [sid]
        wid = ids.work_id(anchor)
        for m in members:
            work_of[m] = wid
        # metadata: best member (coverage first, then the member with the richest metadata)
        best = None
        def rank(x: str) -> tuple:
            link = g.members[x]["link"] if g else "FULL_COPY"
            return (link != "FULL_COPY", x not in coverage, x not in by_hunt, x)

        for m in sorted(members, key=rank):
            f, b = meta_for(m)
            if f.get("title"):
                best = (m, f, b)
                break
        if best is None:
            f, b = meta_for(anchor)
            best = (anchor, f, b)
        _, f, b = best
        wtype = g.work_type if g else GENRE_TO_WORK_TYPE.get(reg[sid]["source_class"], "UNKNOWN")
        if g is None and reg[sid]["source_class"] in ("book_archive", "derived_convenience_pdf", "front_matter"):
            wtype = "UNKNOWN"
        people = split_people(f.get("authors", ""))
        ext = []
        for m in members:
            hrows = by_hunt.get(m, [])
            for h in hrows:
                rel = "COMPONENT" if ((m in CONTAINERS and h["global_id"] not in CONTAINER_VOLUME_PWL)
                                      or h.get("source_type", "") == "book_chapter") else "SAME_WORK"
                ext.append(f"PWL:{h['global_id']}:{rel}")
        pc = f.get("publisher_city", "")
        city, publisher = (pc.split(":", 1) + [""])[:2] if ":" in pc else (pc, "")
        identity = identity_for(anchor) if g is None else min(
            (identity_for(m) for m in members), key=["VERIFIED_IN_FILE", "CATALOGUE_UNVERIFIED",
                                                     "FILENAME_PAGECOUNT_UNVERIFIED"].index)
        year = _year(f.get("year", ""))
        res.works.append({
            "work_id": wid, "status": "ACTIVE", "merged_into": "", "anchor_source_id": anchor, "work_type": wtype,
            "title": f.get("title", ""), "title_en": f.get("title_en", ""), "title_variants": f.get(
                "title_variants", ""), "authors": "; ".join(n if r == "AUTHOR" else f"{n}|{r}" for n, r in people),
            "year_raw": f.get("year", ""), "year": year, "venue": f.get("venue", ""), "venue_type": "",
            "volume": f.get("volume", ""), "issue": f.get("issue", ""), "pages": f.get("pages", ""), "edition": "",
            "publisher": publisher.strip(), "city": city.strip(), "doi": f.get("doi", ""), "isbn": f.get("isbn", ""),
            "language": _lang(f.get("title", ""), f.get("language", "")), "external_ids": "; ".join(sorted(set(ext))),
            "basis_title": b["title"], "basis_authors": b["authors"] if people else "UNKNOWN",
            "basis_year": b["year"] if year else "UNKNOWN", "basis_venue": b["venue"],
            "basis_identifiers": b["identifiers"], "identity_status": identity, "available_from": "",
            "available_from_precision": "", "available_from_basis": "UNKNOWN",
            "curation_status": "CURATED" if g else "AUTO_PROPOSED", "curated_by": curated_by if g else "",
            "curated_at": now.date().isoformat() if g else "", "curation_receipt": "",
            "notes": "; ".join(x for x in (g.note if g else "", NEVER_LINK.get(anchor, ""),
                                           EXTRA_FACTS.get(anchor, {}).get("notes", "")) if x)})
        extra = EXTRA_FACTS.get(anchor, {})
        if extra.get("edition"):
            res.works[-1]["edition"] = extra["edition"]
        if extra.get("external_ids"):
            ids_now = [x for x in res.works[-1]["external_ids"].split("; ") if x]
            res.works[-1]["external_ids"] = "; ".join(sorted(set(ids_now + [extra["external_ids"]])))
        if not f.get("title"):
            res.ambiguities.append({"kind": "NO_TITLE", "work_id": wid, "sources": members})
        if len(by_hunt.get(anchor, [])) > 1 and anchor not in CONTAINERS:
            res.ambiguities.append({"kind": "SEVERAL_HUNT_ROWS", "work_id": wid,
                                    "pwl": [h["global_id"] for h in by_hunt[anchor]]})

    # ---------------------------------------------------------------- links
    for sid in sorted(reg):
        g = group_of.get(sid)
        spec = g.members[sid] if g else {"link": "FULL_COPY"}
        res.links.append({"link_kind": "SOURCE_WORK", "from_id": sid, "relation": spec["link"],
                          "to_id": work_of[sid], "is_primary": "true", "part_label": spec.get("part_label", ""),
                          "printed_range": spec.get("printed_range", ""),
                          "basis": "REPOSITORY_AUDIT" if g else "BOOTSTRAP_SINGLETON",
                          "curation_status": "CURATED" if g else "AUTO_PROPOSED",
                          "notes": "" if g else "one file = its own work (bootstrap)"})
    for sid, first, last, foreign, basis, cur, note in FOREIGN_CONTENT:
        res.links.append({"link_kind": "SOURCE_WORK", "from_id": sid, "relation": "FOREIGN_CONTENT",
                          "to_id": work_of[foreign] if foreign else "", "from_page_start": first,
                          "from_page_end": last, "is_primary": "false", "basis": basis, "curation_status": cur,
                          "notes": note})
        if cur != "CURATED":
            res.ambiguities.append({"kind": "INFERRED_FOREIGN_CONTENT", "source": sid, "pages": [first, last],
                                    "foreign_work": work_of[foreign] if foreign else None, "note": note})
    for a, rel, b, note in WORK_RELATIONS:
        res.links.append({"link_kind": "WORK_WORK", "from_id": work_of[a], "relation": rel, "to_id": work_of[b],
                          "basis": "REPOSITORY_AUDIT", "curation_status": "CURATED", "notes": note})
    for a, rel, b, fp, tp, basis, cur, note in SOURCE_RELATIONS:
        res.links.append({"link_kind": "SOURCE_SOURCE", "from_id": a, "relation": rel, "to_id": b,
                          "from_page_start": fp[0] if fp else "", "from_page_end": fp[1] if fp else "",
                          "to_page_start": tp[0] if tp else "", "to_page_end": tp[1] if tp else "",
                          "basis": basis, "curation_status": cur, "notes": note})
    res.ambiguities += [
        {"kind": "DN-D-18", "pwl": "PWL-0007", "note": "local_status mentions 066 and a 'related' 036: linked to 066 "
         "only (acquired_2026_09_27 / first ALREADY_LOCAL token)"},
        {"kind": "DN-D-18", "pwl": "PWL-0127", "note": "local_status mentions 103 and 016: linked by the first token "
         "only"},
        {"kind": "CONTAINER", "sources": sorted(CONTAINERS), "note": "one work per container file; component PWL "
         "rows are external_ids COMPONENT; child works are not created in v0 (DN-D-19)"},
        {"kind": "NOT_LINKED", "source": S(205), "note": NEVER_LINK[S(205)]},
        {"kind": "EDITION", "source": S(11), "note": "2nd edition 2013; the 1st edition (2001) is cited in the corpus "
         "as another edition — no identifier to record; EDITION_OF needs a curated external id"},
        {"kind": "NOT_SAME_EXTERNAL", "source": S(50), "note": "not the Borzakovsky–Papulov handbook (register notes); "
         "no external id recorded"},
        {"kind": "NOT_SAME_EXTERNAL", "source": S(34), "note": "not S01 (Gornyi Zhurnal 2023 no. 11) per audit A; no "
         "external id recorded"},
    ]
    return res


def write_seed(res: SeedResult, out_dir: Path, *, command: str) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    w_sha = write_csv(out_dir / "WORK_REGISTER.csv", WORK_REGISTER_COLUMNS, res.works)
    l_sha = write_csv(out_dir / "WORK_LINKS.csv", WORK_LINKS_COLUMNS, res.links)
    amb = json.dumps(res.ambiguities, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    (out_dir / "AMBIGUITIES.json").write_text(amb, encoding="utf-8", newline="\n")
    counts: dict[str, int] = defaultdict(int)
    for link in res.links:
        counts[f"{link['link_kind']}:{link['relation']}"] += 1
    receipt = {
        "receipt_kind": "WORK_REGISTRY_SEED", "seed_version": SEED_VERSION, "pipeline_version": PIPELINE_VERSION,
        "command": command, "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "inputs": res.inputs,
        "outputs": {"WORK_REGISTER.csv": {"sha256": w_sha, "rows": len(res.works)},
                    "WORK_LINKS.csv": {"sha256": l_sha, "rows": len(res.links)},
                    "AMBIGUITIES.json": {"sha256": hashlib.sha256(amb.encode("utf-8")).hexdigest(),
                                         "rows": len(res.ambiguities)}},
        "counts": {"works": len(res.works),
                   "works_curated": sum(1 for w in res.works if w["curation_status"] == "CURATED"),
                   "works_without_title": sum(1 for w in res.works if not w["title"]),
                   "links_by_kind": dict(sorted(counts.items()))},
        "rules": "CP-09; audit A §2.8 groups CURATED (REPOSITORY_AUDIT); singletons AUTO_PROPOSED; "
                 "foreign pages inferred from audit A page equivalences CURATED (coordinator 2026-09-28); "
                 "DN-D-18 first-token rule",
    }
    (out_dir / "RECEIPT.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                                          encoding="utf-8", newline="\n")
    return receipt
