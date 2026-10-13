#!/usr/bin/env python3
"""Entity index (agent Г, 13.10.2026): where the catalogued world objects — boreholes, profile lines, benchmarks,
blocks, panels, shafts — are mentioned, one row per (object, source, page).

Inputs — committed PUBLIC catalogues only (no PRIVATE access, no network, deterministic):
- ``evidence/boreholes/borehole_catalog.csv`` (objects, numbers, ``locators`` ``NNN:pNN``) with
  ``borehole_identity_issues.csv`` (the «possibly the same» pairs the catalogue keeps apart → ``identity_note``) and
  ``borehole_picks.csv`` (more pages of the same boreholes);
- ``evidence/world/c4/*_mining_calendar.csv`` (panels, blocks, shafts, profile lines by object id, ``page_id``);
- ``evidence/world/observations/world_observation_catalog.csv`` (profile-line codes ``…PL…``, ``page_id``);
- ``evidence/monitoring/monitoring_observation_catalog.csv`` (survey lines and benchmarks of ``spatial_support``);
- ``evidence/mining/mining_geometry_catalog.csv`` (block and panel ids of ``spatial_entity_ids``);
- ``evidence/sources/record_index.csv`` (``pdf_page`` of an evidence record: the page when a locator has none).

Output: ``catalogues/entities/entity_index.csv`` (columns ``vkm_corpus.search.entities.COLUMNS``) and
``catalogues/entities/build_receipt.json`` (input sha256 and rows, counts per kind and catalogue, rows without a page,
skipped records, the rules). The catalogue pack serves the CSV as the table ``entity_index`` (file stem).

Rules (``entity_index_v1``; MODEL_CHOICE of navigation, not scientific data):
- page basis: a locator page ``pNN``, ``p.NN``, ``pdf p.NN`` or a page id ``VKM-SRC-NNN:pNNNN`` is the physical page
  from 1, as ``pdf_page`` of the evidence records and ``page_index`` of the canon (checked: picks 714/714, monitoring
  504/504, mining geometry 563/563 locator pages equal the records' ``pdf_page``; borehole 75 at SRC-012 p64 =
  ``VKM-SRC-012:p0064``, table 3.1). Sources whose pages are DOCX renders (``rNNNN``) are those the catalogues cite
  with ``r`` page ids; every other source gets ``p``;
- object identity is never inferred: an object is a catalogue record (``borehole_id``, an object id of C4 or of the
  mining geometry, a code of the observation catalogue, a line of the monitoring catalogue within its scope); pages
  of one record merge (borehole catalogue + its picks); the same number elsewhere is another row;
- the site is the catalogue's own (``mine_or_site`` with ``site_attribution_status``, ``site_norm``, ``scope``,
  ``mine_scope``) — never derived from the page or the number;
- numbers: a printed number (digits with an optional letter prefix or suffix, ``/`` or ``-`` part, a prime) → its
  key (``vkm_corpus.search.entities.number_key``); a catalogue record without a number (a group of holes, a pit, a
  station, «без номера») is not indexed; ranges are not expanded; quoted text of a label is replaced by «…»;
  plain numbers of the observation catalogue (line or benchmark? station?) are skipped — only explicit ``PL`` /
  ``PROFILE`` codes count.

Usage:  python scripts/build_entity_index.py [--check]
"""
from __future__ import annotations

import argparse
import csv
import glob
import hashlib
import io
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from vkm_corpus.search.entities import (BENCHMARK, BLOCK, BOREHOLE, COLUMNS, ENTITY_KINDS, IDENTITY_RULE,  # noqa: E402
                                        NOTE, PANEL, PROFILE_LINE, RULE, SHAFT, number_key)

OUT = "catalogues/entities/entity_index.csv"
RECEIPT = "catalogues/entities/build_receipt.json"
BOREHOLES = "evidence/boreholes/borehole_catalog.csv"
ISSUES = "evidence/boreholes/borehole_identity_issues.csv"
PICKS = "evidence/boreholes/borehole_picks.csv"
C4_GLOB = "evidence/world/c4/*_mining_calendar.csv"
OBSERVATIONS = "evidence/world/observations/world_observation_catalog.csv"
MONITORING = "evidence/monitoring/monitoring_observation_catalog.csv"
GEOMETRY = "evidence/mining/mining_geometry_catalog.csv"
RECORD_INDEX = "evidence/sources/record_index.csv"
LABEL_CHARS = 100
NOTE_OTHERS = 4                                     # «possibly the same» ids named in a note (the rest: «+N»)
csv.field_size_limit(sys.maxsize)

_PAGE_ID = re.compile(r"\bVKM-SRC-(\d{3}):([pr])(\d{4})\b")
_LOC_PAGE_ID = re.compile(r"\bVKM-SRC-(\d{3}):([pr])(\d{1,4})(?!\d)")
_SHORT_PAGE = re.compile(r"(?<![\d-])(\d{3}):([pr])(\d{1,4})(?!\d)")
_R_SOURCE = re.compile(r"\bVKM-SRC-(\d{3}):r\d{4}\b|(?<![\d-])(\d{3}):r\d{1,4}(?!\d)")
_LABEL = re.compile(r"(?<![А-Яа-яA-Za-z])(Рис(?:унок)?|рис(?:унок)?|Табл(?:ица)?|табл(?:ица)?|Fig(?:ure)?|Table)\.?\s*"
                    r"(\d+(?:\.\d+)*[а-яa-z]?)(?:\s*[–-]\s*(\d+(?:\.\d+)*))?")
# a printed borehole/shaft number at the start of a label: optional kind words, «№», the number
_NUM = re.compile(r"^(?:(?:солеразведочн|нефтян|подземн|разведочн|опорн|структурн|параметрическ|наблюдательн|шахтн|"
                  r"контрольно[-\s]стволов)\w*\s+)?"
                  r"(?P<head>скв(?:ажин\w*)?\.?|ств(?:ол\w*)?\.?)?\s*(?:№\s*)?"
                  r"(?P<num>(?:[а-яёa-z]{1,3}[-\s]?)?\d{1,5}(?:\s?\d{3})?(?:[а-яa-z]{1,3})?"
                  r"(?:[/-](?:\d{1,4}|[а-яa-z]{1,4}))?'?)"
                  r"(?![\d.,])", re.IGNORECASE)
_QUOTED = re.compile(r"«[^«»]*»|\"[^\"]*\"")
_PAREN = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_PANEL_CODE = re.compile(r"^(?P<num>\d{1,3}(?:-\d{1,3})?)(?P<dir>(?:Yu|[SZVP])*)(?:-(?P<hor>\d{2,3}))?$")
_BLOCK_CODE = re.compile(r"^B(?P<num>\d{1,3})(?P<side>[A-Z]{0,2})$")
_PANEL_BLOCK = re.compile(r"^(?P<pan>\d{1,3}(?:Yu|[SZVP])+)-(?P<num>\d{1,3})(?P<side>[A-Z]?)$")   # BLK:1YuVP-2
_PL_CODE = (re.compile(r"^(?:[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*_)?PL_?(?P<num>\d{1,3}|[IVXL]{1,6})$"),
            re.compile(r"^[A-Z][A-Z0-9]*_PROFILE_(?P<num>\d{1,3}|[IVXL]{1,6})$"))
_LINE_TEXT = re.compile(r"(?:лини[яи]|profile\s+line)\s*№?\s*(\d{1,3}(?:\.\d{1,2})?)(?![\d.])", re.IGNORECASE)
_RP = re.compile(r"\bRp\s?(\d{1,4}(?:\.\d{1,2})?)(?![\d.])")
_REPERS = re.compile(r"репер(?:ы|а|ов)?\s*№?\s*(\d{1,4}(?:\s*,\s*\d{1,4})*)(?![\d–-])", re.IGNORECASE)
_DIR_CYR = {"Yu": "Ю", "S": "С", "Z": "З", "V": "В", "P": "П"}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(root: Path, rel: str) -> list[dict[str, str]]:
    with (root / rel).open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def short(text: str, limit: int = LABEL_CHARS) -> str:
    """A label or site cell cut to ``limit`` characters; quoted text («…») is replaced by «…» (no quotes of the
    sources in the index, only names and numbers)."""
    text = " ".join(_QUOTED.sub("«…»", str(text or "")).split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return (cut or text[:limit]).rstrip(" ,;:(«\"") + "…"


def labels_of(text: str) -> list[str]:
    """Printed figure/table labels named in a locator («Рис. 3.2», «Табл. 3.1»), normalised, in order."""
    out: list[str] = []
    for m in _LABEL.finditer(text or ""):
        head = m.group(1).lower()
        name = "Рис." if head.startswith("рис") else "Табл." if head.startswith("табл") else \
            "Fig." if head.startswith("fig") else "Table"
        label = f"{name} {m.group(2)}" + (f"–{m.group(3)}" if m.group(3) else "")
        if label not in out:
            out.append(label)
    return out


class Pages:
    """Page ids from locators: ``r`` (DOCX render) for the sources the catalogues cite with ``r`` pages."""

    def __init__(self, r_sources: set[str]) -> None:
        self.r_sources = r_sources

    def page_id(self, source_id: str, page: int | str, prefix: str | None = None) -> str:
        prefix = prefix or ("r" if source_id in self.r_sources else "p")
        return f"{source_id}:{prefix}{int(page):04d}"


def r_sources(root: Path, rels: Iterable[str]) -> set[str]:
    out: set[str] = set()
    for rel in rels:
        for m in _R_SOURCE.finditer((root / rel).read_text(encoding="utf-8")):
            out.add(f"VKM-SRC-{m.group(1) or m.group(2)}")
    return out


# ------------------------------------------------------------------------------------------------------- objects
def parse_number(label: str) -> tuple[str | None, str | None]:
    """(kind, printed number) of a borehole-catalogue label: «скв. 75» → (BOREHOLE, «75»), «ствол № 2-бис» →
    (SHAFT, «2-бис»); a label that does not start with a number (pit, station, «без номера») → (None, None)."""
    text = _PAREN.sub(" ", label or "").strip().strip("«»\"")
    m = _NUM.match(text)
    if not m:
        return None, None
    head = (m.group("head") or "").lower()
    return (SHAFT if head.startswith("ств") else BOREHOLE), m.group("num").strip()


def borehole_numbers(row: dict[str, str]) -> tuple[str, list[tuple[str, str]]]:
    """(kind, [(printed number, key)]) of a borehole-catalogue row: the qualified aliases first (``SOL:75``,
    ``DUR:206 | DUR:209``, ``S048:130, 131``, ``S048:1021@BKPRU2``), the printed variants when no alias gives a
    number."""
    kind, found = BOREHOLE, []

    def add(label: str) -> None:
        nonlocal kind
        k, num = parse_number(label)
        if num is None:
            return
        if k == SHAFT:
            kind = SHAFT
        key = number_key(num)
        if key and key not in [x[1] for x in found]:
            found.append((num, key))

    for alias in (row.get("aliases_qualified") or "").split("|"):
        tail = alias.split(":", 1)[1] if ":" in alias else ""
        for part in tail.split(","):
            add(part.split("@", 1)[0])
    if not found:
        for variant in (row.get("alternate_ids") or "").split("|"):
            add(variant)
    return kind, found


def object_id_entities(oid: str) -> list[dict[str, str]]:
    """Entities of one object id of the C4 calendars / the mining geometry (``SKRU1:PAN13:9VP``, ``SKRU3:PAN:1:B2``,
    ``SKRU1:BLK:201``, ``SKRU2:SHAFT:5``, ``SKRU2:BH:130``, ``SL:1``, ``UNATTR:YuVP:5``); ids of mines, fields, areas,
    zones, horizons and workings give none."""
    parts = [p for p in (oid or "").strip().split(":") if p]
    if len(parts) < 2:
        return []
    out: list[dict[str, str]] = []

    def panel(code: str, pid: str) -> dict[str, str] | None:
        m = _PANEL_CODE.match(code)
        if not m:
            return None
        letters = re.findall(r"Yu|[SZVP]", m.group("dir") or "")
        num = m.group("num") + "".join(_DIR_CYR[x] for x in letters)
        printed = num + (f" (гор. −{m.group('hor')})" if m.group("hor") else "")
        return {"kind": PANEL, "entity_id": pid, "number": printed, "key": number_key(num), "parent": ""}

    if parts[0] == "SL" and len(parts) == 2:
        m = re.match(r"^(\d{1,3})([A-Z]*)$", parts[1])
        if m:
            out.append({"kind": PROFILE_LINE, "entity_id": oid, "number": m.group(1) + (
                f" ({m.group(2)})" if m.group(2) else ""), "key": number_key(m.group(1)), "parent": ""})
    elif parts[0] == "UNATTR" and len(parts) == 3 and parts[1] in ("YuVP", "YuZP", "SVP", "SZP", "VP", "ZP"):
        p = panel(parts[2] + parts[1], oid)
        if p:
            out.append(p)
    elif len(parts) >= 3 and parts[1] in ("PAN", "PAN13"):
        p = panel(parts[2], ":".join(parts[:3]))
        if p:
            out.append(p)
            if len(parts) == 4:
                b = _BLOCK_CODE.match(parts[3])
                if b:
                    out.append({"kind": BLOCK, "entity_id": oid, "number": b.group("num") + b.group("side"),
                                "key": number_key(b.group("num")), "parent": f"панель {p['number']}"})
    elif len(parts) == 3 and parts[1] == "BLK" and re.match(r"^\d{1,4}[A-Za-zА-Яа-я]?$", parts[2]):
        out.append({"kind": BLOCK, "entity_id": oid, "number": parts[2], "key": number_key(parts[2]), "parent": ""})
    elif len(parts) == 3 and parts[1] == "BLK" and _PANEL_BLOCK.match(parts[2]):
        m = _PANEL_BLOCK.match(parts[2])
        p = panel(m.group("pan"), f"{parts[0]}:PAN:{m.group('pan')}")
        if p:
            out += [p, {"kind": BLOCK, "entity_id": oid, "number": m.group("num") + m.group("side"),
                        "key": number_key(m.group("num")), "parent": f"панель {p['number']}"}]
    elif len(parts) == 3 and parts[1] == "SHAFT" and re.match(r"^\d{1,3}", parts[2]):
        out.append({"kind": SHAFT, "entity_id": oid, "number": parts[2], "key": number_key(parts[2]), "parent": ""})
    elif len(parts) == 3 and parts[1] == "BH" and re.match(r"^\d{1,5}", parts[2]):
        out.append({"kind": BOREHOLE, "entity_id": oid, "number": parts[2], "key": number_key(parts[2]),
                    "parent": ""})
    return out


# ------------------------------------------------------------------------------------------------------- builder
class Builder:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.rows: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self.skipped: dict[str, int] = {}
        c4 = sorted(Path(p).relative_to(root).as_posix() for p in glob.glob(str(root / C4_GLOB)))
        self.inputs = [BOREHOLES, ISSUES, PICKS, *c4, OBSERVATIONS, MONITORING, GEOMETRY, RECORD_INDEX]
        self.c4 = c4
        self.pages = Pages(r_sources(root, [BOREHOLES, PICKS, *c4, OBSERVATIONS]))
        self.pdf_page = {r["vn_id"]: r for r in read(root, RECORD_INDEX)}

    def skip(self, why: str) -> None:
        self.skipped[why] = self.skipped.get(why, 0) + 1

    def add(self, *, kind: str, object_key: str, entity_id: str, label: str, number: str, keys: list[str],
            parent: str, site: str, site_attribution: str, source_id: str, page_id: str, page_basis: str,
            labels: list[str], catalogue: str, record_id: str, status: str, identity_note: str) -> None:
        key = (kind, object_key, source_id, page_id)
        row = self.rows.get(key)
        if row is None:
            row = self.rows[key] = {
                "entity_kind": kind, "entity_id": entity_id, "entity_label": short(label), "number": number,
                "keys": list(keys), "parent": parent, "site": short(site), "site_attribution": short(site_attribution),
                "source_id": source_id, "page_id": page_id, "page_basis": [], "object_labels": [],
                "catalogue": [], "record_ids": [], "status": [], "identity_note": identity_note}
        for name, value in (("page_basis", page_basis), ("catalogue", catalogue), ("record_ids", record_id),
                            ("status", status)):
            if value and value not in row[name]:
                row[name].append(value)
        for lab in labels:
            if lab not in row["object_labels"]:
                row["object_labels"].append(lab)
        for k in keys:
            if k not in row["keys"]:
                row["keys"].append(k)

    # ----------------------------------------------------------------------------- borehole catalogue and picks
    def boreholes(self) -> None:
        possible: dict[str, list[tuple[str, str]]] = {}
        for r in read(self.root, ISSUES):
            ids = [x.strip() for x in r["borehole_ids"].split(";") if x.strip()]
            if len(ids) < 2 or not (r["decision"].startswith("KEPT_SEPARATE") or
                                    r["issue_type"] == "POSSIBLE_SAME_NOT_MERGED"):
                continue
            for a in ids:
                for b in ids:
                    if a != b:
                        possible.setdefault(a, []).append((b, r["issue_id"]))
        self.bh: dict[str, dict[str, Any]] = {}
        for r in read(self.root, BOREHOLES):
            kind, nums = borehole_numbers(r)
            bid = r["borehole_id"]
            basis = (r.get("identity_basis") or "").split(":", 1)[0].strip()
            note = f"catalogue object {bid}" + (f"; identity basis {basis}" if basis else "")
            others = sorted({b for b, _i in possible.get(bid, [])})
            if others:
                issues = sorted({i for _b, i in possible.get(bid, [])})
                named = ", ".join(others[:NOTE_OTHERS]) + (f" +{len(others) - NOTE_OTHERS}" if len(others) >
                                                           NOTE_OTHERS else "")
                note += f"; possibly the same as {named} ({', '.join(issues[:NOTE_OTHERS])}): kept separate"
            ent = {"kind": kind, "entity_id": bid, "label": r["alternate_ids"] or r["aliases_qualified"] or bid,
                   "number": nums[0][0] if nums else "", "keys": [k for _n, k in nums], "site": r["mine_or_site"],
                   "site_attribution": r["site_attribution_status"] or "AS_IN_CATALOGUE", "status": r["status"],
                   "note": note}
            if not nums:                           # a group, a pit, a station, «без номера»: no numbered object
                self.skip("borehole_catalog: label without a number (not indexed)")
                continue
            self.bh[bid] = ent
            cited = {s.strip() for s in r["sources"].split(";") if s.strip()}
            paged: set[str] = set()
            for piece in r["locators"].split("|"):
                refs = _SHORT_PAGE.findall(piece)
                labels = labels_of(piece) if len(refs) == 1 else []
                for src, prefix, page in refs:
                    sid = f"VKM-SRC-{src}"
                    paged.add(sid)
                    self._bh_row(ent, sid, self.pages.page_id(sid, page, prefix), "LOCATOR_PAGE", labels, BOREHOLES,
                                 bid, r["status"])
            for sid in sorted(cited - paged):
                self._bh_row(ent, sid, "", "NONE", [], BOREHOLES, bid, r["status"])
        for r in read(self.root, PICKS):
            ent = self.bh.get(r["borehole_id"])
            if ent is None:
                self.skip("borehole_picks: borehole not indexed (not in the catalogue or without a number)")
                continue
            sid, page, basis, prefix = r["source_id"], None, "", None
            m = _LOC_PAGE_ID.search(r["locator"])
            if m and f"VKM-SRC-{m.group(1)}" == sid:
                page, prefix, basis = m.group(3), m.group(2), "LOCATOR_PAGE"
            else:
                m2 = re.match(r"\s*p\.?\s*(\d{1,4})\b", r["locator"])
                if m2:
                    page, basis = m2.group(1), "LOCATOR_PAGE"
                elif r["vn_id"] in self.pdf_page and self.pdf_page[r["vn_id"]].get("pdf_page"):
                    page, basis = self.pdf_page[r["vn_id"]]["pdf_page"], "RECORD_PDF_PAGE"
            pid = self.pages.page_id(sid, page, prefix) if page and sid else ""
            self._bh_row(ent, sid, pid, basis or "NONE", labels_of(r["locator"]), PICKS, r["pick_id"], r["status"])

    def _bh_row(self, ent: dict[str, Any], sid: str, pid: str, basis: str, labels: list[str], catalogue: str,
                record_id: str, status: str) -> None:
        self.add(kind=ent["kind"], object_key=ent["entity_id"], entity_id=ent["entity_id"], label=ent["label"],
                 number=ent["number"], keys=ent["keys"], parent="", site=ent["site"],
                 site_attribution=ent["site_attribution"], source_id=sid, page_id=pid, page_basis=basis,
                 labels=labels, catalogue=catalogue, record_id=record_id, status=status, identity_note=ent["note"])

    # ----------------------------------------------------------------------------- object ids (C4, geometry)
    def _object_rows(self, oids: str, *, label: str, site: str, attribution: str, pages: list[tuple[str, str, str]],
                     catalogue: str, record_id: str, status: str, labels: list[str]) -> None:
        any_entity = False
        for oid in (o.strip() for o in (oids or "").split(";") if o.strip()):
            ents = object_id_entities(oid)
            if not ents:
                continue
            any_entity = True
            for e in ents:
                if not e["key"]:
                    self.skip(f"{catalogue}: object id without a number")
                    continue
                note = f"catalogue object id {e['entity_id']}; not matched to other catalogues or sources"
                for sid, pid, basis in pages:
                    self.add(kind=e["kind"], object_key=e["entity_id"], entity_id=e["entity_id"],
                             label=label or e["entity_id"], number=e["number"], keys=[e["key"]], parent=e["parent"],
                             site=site, site_attribution=attribution, source_id=sid, page_id=pid, page_basis=basis,
                             labels=labels, catalogue=catalogue, record_id=record_id, status=status,
                             identity_note=note)
        if not any_entity:
            self.skip(f"{catalogue}: no numbered object id (mine, field, area, zone, working or empty)")

    def calendars(self) -> None:
        for rel in self.c4:
            for r in read(self.root, rel):
                pages = []
                for pid in (p.strip() for p in r["page_id"].split(";") if p.strip()):
                    m = _PAGE_ID.fullmatch(pid)
                    if m:
                        pages.append((f"VKM-SRC-{m.group(1)}", pid, "CATALOGUE_PAGE_ID"))
                if not pages and r["source_id"]:
                    pages = [(r["source_id"], "", "NONE")]
                self._object_rows(r["object_id"], label=r["object_as_printed"], site=r["site_norm"],
                                  attribution="site_norm", pages=pages, catalogue=rel, record_id=r["row_id"],
                                  status=r["status"], labels=labels_of(r["locator"]))

    def geometry(self) -> None:
        for r in read(self.root, GEOMETRY):
            sources = [s.strip() for s in r["source_ids"].split(";") if s.strip()]
            m = re.search(r"pdf p\.?\s*(\d{1,4})", r["locators"])
            if m and len(sources) == 1:
                pages = [(sources[0], self.pages.page_id(sources[0], m.group(1)), "LOCATOR_PAGE")]
            else:
                pages = [(s, "", "NONE") for s in sources]
            self._object_rows(r["spatial_entity_ids"], label="", site=r["mine_scope"], attribution="mine_scope",
                              pages=pages, catalogue=GEOMETRY, record_id=r["row_id"], status=r["status"],
                              labels=labels_of(r["locators"]))

    # ----------------------------------------------------------------------------- observations, monitoring
    def observations(self) -> None:
        for r in read(self.root, OBSERVATIONS):
            code = (r["line_or_benchmark"] or "").strip()
            m = next((rx.match(code) for rx in _PL_CODE if rx.match(code)), None)
            if not m:
                if code:
                    self.skip("world_observation_catalog: line_or_benchmark not a PL/PROFILE code (kind unknown)")
                continue
            pages = [(f"VKM-SRC-{p.group(1)}", p.group(0), "CATALOGUE_PAGE_ID")
                     for p in _PAGE_ID.finditer(r["page_id"] or "")] or ([(r["source_id"], "", "NONE")]
                                                                      if r["source_id"] else [])
            for sid, pid, basis in pages:
                self.add(kind=PROFILE_LINE, object_key=f"{code}@{r['site_norm']}", entity_id=code, label=code,
                         number=m.group("num"), keys=[number_key(m.group("num"))], parent="", site=r["site_norm"],
                         site_attribution="site_norm", source_id=sid, page_id=pid, page_basis=basis,
                         labels=labels_of(r["figure_label"]), catalogue=OBSERVATIONS, record_id=r["obs_id"],
                         status=r["status"], identity_note=f"catalogue code {code} within site {r['site_norm']}; "
                                                           "not matched to other catalogues or sources")

    def monitoring(self) -> None:
        for r in read(self.root, MONITORING):
            parts = [p.strip() for p in (r["spatial_support"] or "").split("|")]
            head = parts[0].lower() if parts else ""
            if head not in ("survey_line", "benchmark"):
                continue
            text = " | ".join(parts[1:])
            found: list[tuple[str, str, str]] = []           # (kind, printed number, label)
            if head == "survey_line" and len(parts) > 1:
                m = _LINE_TEXT.search(parts[1])
                if m:
                    found.append((PROFILE_LINE, m.group(1), parts[1]))
            for m in _RP.finditer(text):
                found.append((BENCHMARK, m.group(1), f"Rp{m.group(1)}"))
            for m in _REPERS.finditer(text):
                for n in re.findall(r"\d{1,4}", m.group(1)):
                    found.append((BENCHMARK, n, f"репер {n}"))
            if not found:
                self.skip("monitoring_observation_catalog: survey line / benchmark without a number")
                continue
            sid, page, basis = r["source_id"], None, "NONE"
            m = re.match(r"\s*p(\d{1,4})\b", r["locator"] or "")
            if m:
                page, basis = m.group(1), "LOCATOR_PAGE"
            else:
                vns = [v for v in re.findall(r"EV-VN-S\d{3}-\d{4}", r["vn_ids"]) if v in self.pdf_page]
                if vns and self.pdf_page[vns[0]].get("pdf_page"):
                    page, basis = self.pdf_page[vns[0]]["pdf_page"], "RECORD_PDF_PAGE"
            pid = self.pages.page_id(sid, page) if page and sid else ""
            seen: set[tuple[str, str]] = set()
            for kind, num, label in found:
                key = number_key(num)
                if (kind, key) in seen:
                    continue
                seen.add((kind, key))
                obj = f"MON:{kind}:{key}@{r['scope']}"
                self.add(kind=kind, object_key=obj, entity_id="", label=label, number=num, keys=[key], parent="",
                         site=r["scope"], site_attribution="scope", source_id=sid, page_id=pid, page_basis=basis,
                         labels=labels_of(r["locator"]), catalogue=MONITORING, record_id=r["cat_id"],
                         status=r["status"], identity_note=f"monitoring {kind.lower()} {num} within scope "
                                                           f"{r['scope']}; not matched to other catalogues or sources")

    # ----------------------------------------------------------------------------- output
    def build(self) -> tuple[str, dict[str, Any]]:
        self.boreholes()
        self.calendars()
        self.geometry()
        self.observations()
        self.monitoring()
        out_rows = []
        for (kind, obj, sid, pid), r in self.rows.items():
            m = _PAGE_ID.fullmatch(pid) if pid else None
            ident = f"{kind}|{obj}|{sid}|{pid}"
            out_rows.append({
                "entity_row_id": "ENT-" + hashlib.sha256(ident.encode("utf-8")).hexdigest()[:12],
                "entity_kind": kind, "entity_id": r["entity_id"], "entity_label": r["entity_label"],
                "number": r["number"], "number_key": ";".join(r["keys"]), "parent": r["parent"], "site": r["site"],
                "site_attribution": r["site_attribution"], "source_id": sid, "page_id": pid,
                "page_index": str(int(m.group(3))) if m else "", "page_basis": ";".join(sorted(r["page_basis"])),
                "object_labels": "; ".join(r["object_labels"]), "catalogue": ";".join(r["catalogue"]),
                "record_ids": ";".join(r["record_ids"]), "status": ";".join(sorted(r["status"])) or "AUTO",
                "identity_note": r["identity_note"], "_obj": obj})

        def natural(key: str) -> tuple:
            return tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in re.findall(r"\d+|\D+", key))

        out_rows.sort(key=lambda r: (ENTITY_KINDS.index(r["entity_kind"]), natural(r["number_key"].split(";")[0]),
                                     r["site"], r["_obj"], r["source_id"], int(r["page_index"] or 10 ** 6),
                                     r["page_id"]))
        ids = [r["entity_row_id"] for r in out_rows]
        if len(set(ids)) != len(ids):
            raise SystemExit("entity_row_id collision")
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=list(COLUMNS), lineterminator="\n", extrasaction="ignore")
        w.writeheader()
        w.writerows(out_rows)
        text = buf.getvalue()
        count = lambda f: dict(sorted(_counter(f(r) for r in out_rows).items()))  # noqa: E731
        receipt = {
            "generator": "scripts/build_entity_index.py", "rule": RULE, "output": OUT,
            "output_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "rows": len(out_rows),
            "inputs": [{"path": rel, "sha256": sha256(self.root / rel)} for rel in self.inputs],
            "counts": {"by_kind": count(lambda r: r["entity_kind"]),
                       "by_catalogue": count(lambda r: r["catalogue"].split(";")[0]),
                       "by_page_basis": count(lambda r: r["page_basis"] or "NONE"),
                       "rows_without_page": sum(1 for r in out_rows if not r["page_id"]),
                       "rows_without_number": sum(1 for r in out_rows if not r["number_key"]),
                       "objects": len({(r["entity_kind"], r["_obj"]) for r in out_rows}),
                       "sources": len({r["source_id"] for r in out_rows if r["source_id"]}),
                       "pages": len({r["page_id"] for r in out_rows if r["page_id"]}),
                       "r_page_sources": sorted(self.pages.r_sources)},
            "skipped": dict(sorted(self.skipped.items())),
            "identity_rule": IDENTITY_RULE, "note": NOTE,
            "page_basis": "physical page from 1 (pdf_page of the evidence records, page_index of the canon); locator "
                          "pages agree with the records' pdf_page: picks 714/714, monitoring 504/504, mining geometry "
                          "563/563; spot check VKM-SRC-012 p64 = VKM-SRC-012:p0064 (page_index 64, printed 63)"}
        return text, receipt


def _counter(values: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out


def build(root: Path = ROOT) -> tuple[str, dict[str, Any]]:
    return Builder(root).build()


def receipt_text(receipt: dict[str, Any]) -> str:
    return json.dumps(receipt, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="exit 1 if the committed files differ from a rebuild")
    args = ap.parse_args(argv)
    text, receipt = build(ROOT)
    files = {ROOT / OUT: text, ROOT / RECEIPT: receipt_text(receipt)}
    if args.check:
        stale = [p.relative_to(ROOT).as_posix() for p, t in files.items()
                 if not p.is_file() or p.read_text(encoding="utf-8") != t]
        print(json.dumps({"stale": stale}, ensure_ascii=False))
        return 1 if stale else 0
    for path, t in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(t.encode("utf-8"))
    print(json.dumps({"rows": receipt["rows"], "counts": receipt["counts"], "skipped": receipt["skipped"]},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
