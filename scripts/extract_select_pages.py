#!/usr/bin/env python3
"""World passport extraction, step 1: select corpus pages for page-level extraction (09.10.2026).

Sources of the selection:
- pages cited by the Phase-1 PRIVATE catalogues (MECH_RHEO mechanics / rheology / conflicts, MINING geometry /
  backfill, GEOLOGY_COORDS thickness, MONITORING_LIFECYCLE observations);
- pages cited by the topic dossiers ``initial_stress.json`` and ``backfill_mechanics.json``;
- pages of the observation figure candidates (``figure_candidates.json`` of 08.10);
- a deterministic term search over the NAV page text (and the tables of the page) by the needs of modules C1–C4
  and of the observation set; a page qualifies by terms of a need AND numbers with a unit of that need.

Tiers: A — SKRU-1 (named on the page or the source is a SKRU-1 source) or pages with values that define the ranges of
the worlds (massif / calibrated mechanics, creep, λ and initial stress, backfill); B — other VKM pages; C — analogues.
Exact text duplicates (NAV ``duplicate_page_candidates``) and copies of one work (NAV ``work_sources``) are merged
to one page. VKM-SRC-252 (internal enterprise document) is excluded.

Usage:  python scripts/extract_select_pages.py --nav <nav_pages.duckdb> --resources <PRIVATE> --out <pages.csv>
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

csv.field_size_limit(sys.maxsize)
ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_SOURCES = {"VKM-SRC-252"}            # internal enterprise document: only with an explicit owner «yes»
MAX_RANGE = 10                                # a cited page range longer than this is not expanded
RULE_VERSION = "extract_select_pages_v1"

_SKRU1 = re.compile(r"СКРУ[\s\-–—№]*1(?![\d,.])|СКРУ1(?!\d)|Соликамск\w*\s+калийн\w*\s+рудоуправлени\w*\s*(?:№\s*)?1(?!\d)",
                    re.I)
_VKM = re.compile(r"Верхнекам|ВКМКС|ВКМ(?![А-Яа-я])|БКПРУ|СКРУ|Березник|Соликамск|Усть-Яйв|Уралкали", re.I)

# needs: (name, module, terms, value pattern) — a page must match both on its own text
_NUM_UNIT = {
    "mech": r"\d\s*(?:МПа|ГПа|кПа|кгс\s*/\s*см|г\s*/\s*см|т\s*/\s*м|кН\s*/\s*м|МН\s*/\s*м|MPa|GPa|g/cm)",
    "creep": r"\d\s*(?:сут|час|ч\s*-\s*1|с\s*-\s*1|1\s*/\s*с|1\s*/\s*сут|МПа|%)|\d\s*[·×]\s*10",
    "ratio": r"\d[.,]\d",
    "length": r"\d\s*(?:м|мм|см|km|км|m)(?![А-Яа-яa-z])",
    "subs": r"\d\s*(?:мм|см|м)(?![А-Яа-яa-z])|(?:19|20)\d\d\s*(?:г|год)",
    "years": r"(?:19[3-9]\d|20[0-2]\d)",
}
NEEDS = (
    ("C2_MECH", "C2", r"модул\w*\s+(?:упругост|деформац|Юнга|спада)|коэффициент\w*\s+Пуассон|предел\w*\s+прочност|"
                      r"прочност\w*\s+на\s+(?:сжатие|растяжение|одноосн)|сцеплени|угл\w*\s+внутренн\w+\s+трени|"
                      r"плотност\w*\s+(?:пород|соли|сильвинит|карналлит|галит)|объ[её]мн\w*\s+(?:вес|масс)|"
                      r"удельн\w*\s+вес|длительн\w*\s+прочност", "mech"),
    ("C2_CREEP", "C2", r"ползуч|реологи|наследственн|ядр\w*\s+(?:Абеля|ползучести|наследственн)|вязкост|"
                       r"скорост\w*\s+(?:установившейся\s+)?ползучест|закон\w*\s+Нортон|степенн\w*\s+закон", "creep"),
    ("C3_STRESS", "C3", r"боков\w*\s+распор|коэффициент\w*\s+бокового|начальн\w*\s+напряж|природн\w*\s+напряж|"
                        r"гидроразрыв|тектоническ\w*\s+напряж|геотерм|температур\w*\s+(?:пород|массива|соли)|"
                        r"горн\w*\s+давлени\w*\s+γ", "ratio"),
    ("C1_GEOLOGY", "C1", r"мощност\w*|глубин\w*\s+залегани|отметк\w*\s+(?:кровли|почвы)|водозащитн\w*\s+толщ|"
                         r"междупласть|соляно-мергельн|покровн\w*\s+каменн", "length"),
    ("C4_MINING", "C4", r"ширин\w*\s+(?:камер|целик|междукамерн)|высот\w*\s+камер|междукамерн\w*\s+целик|"
                        r"степен\w*\s+нагружени|коэффициент\w*\s+извлечени|панел\w*|блок\w*\s+№?\s*\d|"
                        r"камерн\w*\s+систем", "length"),
    ("C4_BACKFILL", "C4", r"закладк|закладочн|галитов\w*\s+отход|гидрозакладк|сухой\s+закладк", "years"),
    ("C4_HISTORY", "C4", r"отработ\w*\s+(?:пласт|панел|блок|участ)|выемк\w*\s+(?:пласт|руды)|"
                         r"начал\w*\s+(?:очистн|эксплуатац|отработ)|затоплени|провал", "years"),
    ("OBS", "OBS", r"оседани|сдвижени\w*\s+земн|нивелир|репер|профильн\w*\s+лини|конвергенц|смещени\w*\s+контур|"
                   r"InSAR|интерферометр|радарн\w*\s+съ[её]мк|мульд\w*\s+сдвижени|GNSS|GPS", "subs"),
)
_NEED_RX = [(n, m, re.compile(t, re.I), re.compile(_NUM_UNIT[u], re.I)) for n, m, t, u in NEEDS]
RANGE_NEEDS = {"C2_MECH", "C2_CREEP", "C3_STRESS", "C4_BACKFILL"}   # tier A when massif/calibrated or SKRU-1


def parse_page_refs(text: str, default_source: str | None = None) -> list[tuple[str, int, int]]:
    """(source_id, first, last) from locators such as ``pdf p.89``, ``p.13``, ``pp.121-125``, ``p13 [...]``,
    ``VKM-SRC-025 p.157-163; VKM-SRC-037 p.12-13``. Printed page numbers (``printed 24-25``, ``печ.``) are skipped."""
    out = []
    t = re.sub(r"\(\s*(?:printed|печ\.?)[^)]*\)", " ", text or "")
    t = re.sub(r"(?:printed|печ\.)\s*[\d\-–, ]+", " ", t)
    pat = re.compile(r"(VKM-SRC-\d{3})?[^A-Za-zА-Яа-я0-9]{0,3}(?:pdf\s+)?(?:pp?\.?\s?|страниц\w*\s|с\.\s?)(\d{1,4})"
                     r"(?:\s?[-–]\s?(\d{1,4}))?")
    cur = default_source
    for m in re.finditer(r"VKM-SRC-\d{3}|(?<![A-Za-zА-Яа-яЁё])(?:pdf\s+)?(?:pp?\.?\s?|с\.\s?)\d{1,4}"
                         r"(?:\s?[-–]\s?\d{1,4})?", t):
        tok = m.group(0)
        if tok.startswith("VKM-SRC"):
            cur = tok
            continue
        mm = pat.match(tok)
        if not mm or cur is None:
            continue
        a = int(mm.group(2))
        b = int(mm.group(3)) if mm.group(3) else a
        if b < a:
            b = a
        out.append((cur, a, b))
    return out


def _rows(path: Path) -> list[dict]:
    return list(csv.DictReader(open(path, encoding="utf-8"))) if path.is_file() else []


def catalogue_refs(canon: Path) -> list[dict]:
    """One reference per (catalogue row, page): source, page range, catalogue, reason, range flag, site scope."""
    refs = []

    def add(cat, row_id, source, a, b, why, scope="", range_value=False):
        refs.append({"catalogue": cat, "row_id": row_id, "source_id": source, "first": a, "last": b, "why": why,
                     "scope": scope, "range_value": range_value})

    for r in _rows(canon / "MECH_RHEO" / "mechanics_evidence_catalog.csv"):
        rv = r["scale"] in ("MASSIF", "CALIBRATED_EFFECTIVE_MODEL", "FIELD") or r["variable"] in (
            "lateral_stress_ratio", "K0_lambda", "long_term_strength", "vertical_stress", "horizontal_stress_magnitude")
        if r["pdf_page"].strip().isdigit():
            p = int(r["pdf_page"])
            add("MECH_RHEO/mechanics", r["row_id"], r["source_id"], p, p, f"mech:{r['variable']}", r["site_scope"], rv)
        else:
            for s, a, b in parse_page_refs(r["locator"], r["source_id"]):
                add("MECH_RHEO/mechanics", r["row_id"], s, a, b, f"mech:{r['variable']}", r["site_scope"], rv)
    for r in _rows(canon / "MECH_RHEO" / "rheology_evidence_catalog.csv"):
        for s, a, b in parse_page_refs(r["locator"], r["source_id"]):
            add("MECH_RHEO/rheology", r["rheo_id"], s, a, b, f"rheo:{r['law_family']}", r["site_scope"], True)
    for r in _rows(canon / "MECH_RHEO" / "mech_rheo_conflicts.csv"):
        for s, a, b in parse_page_refs(r["sources_locators"]):
            add("MECH_RHEO/conflicts", r["conflict_id"], s, a, b, "conflict", "", True)
    for name, cat in (("mining_geometry_catalog.csv", "MINING/geometry"), ("backfill_event_catalog.csv", "MINING/backfill")):
        for r in _rows(canon / "MINING" / name):
            srcs = re.findall(r"VKM-SRC-\d{3}", r.get("source_ids", ""))
            for s, a, b in parse_page_refs(r.get("locators", ""), srcs[0] if srcs else None):
                add(cat, r["row_id"], s, a, b, cat.split("/")[1], r.get("scope", ""), cat.endswith("backfill"))
    for r in _rows(canon / "GEOLOGY_COORDS" / "stratigraphic_thickness_observations.csv"):
        if r["pdf_page"].strip().isdigit():
            p = int(r["pdf_page"])
            add("GEOLOGY_COORDS/thickness", r["obs_id"], r["source_id"], p, p, "thickness", r["scope"])
    for r in _rows(canon / "MONITORING_LIFECYCLE" / "monitoring_observation_catalog.csv"):
        m = re.match(r"\s*p(\d{1,4})(?:\s?[-–]\s?(\d{1,4}))?", r["locator"])
        if m:
            a = int(m.group(1))
            add("MONITORING/observations", r["cat_id"], r["source_id"], a, int(m.group(2) or a), "monitoring",
                r["scope"])
    return refs


def dossier_refs(dossier_dir: Path) -> list[dict]:
    refs = []
    for name, rv in (("initial_stress.json", True), ("backfill_mechanics.json", True)):
        p = dossier_dir / name
        if not p.is_file():
            continue
        blob = json.dumps(json.load(open(p, encoding="utf-8")), ensure_ascii=False)
        for s, a, b in parse_page_refs(blob):
            refs.append({"catalogue": f"dossier/{name[:-5]}", "row_id": "", "source_id": s, "first": a, "last": b,
                         "why": f"dossier:{name[:-5]}", "scope": "", "range_value": rv})
    return refs


def figure_refs(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    refs = []
    for f in json.load(open(path, encoding="utf-8")):
        m = re.match(r"(VKM-SRC-\d{3}):p(\d{4})", f["page_id"])
        if m:
            p = int(m.group(2))
            refs.append({"catalogue": "figures/candidates_20261008", "row_id": f["figure_id"], "source_id": m.group(1),
                         "first": p, "last": p, "why": "figure_candidate", "scope": f.get("site_scope", ""),
                         "range_value": False})
    return refs


def load_nav(nav_path: Path) -> dict:
    import duckdb
    c = duckdb.connect(str(nav_path), read_only=True)
    pages = {}
    for pid, sid, idx, txt, cc, pcl in c.execute(
            "select page_id, source_id, page_index, normalized_text, char_count, page_class from pages").fetchall():
        pages[pid] = {"page_id": pid, "source_id": sid, "page_index": idx, "text": txt or "", "chars": cc or 0,
                      "page_class": pcl}
    tabs = defaultdict(list)
    for pid, lab, txt in c.execute("select page_id, table_label, normalized_text from tables where is_primary_layer "
                                   "order by page_id, object_id").fetchall():
        tabs[pid].append(txt or "")
    src = {sid: {"scope": scope or "", "class": cls or ""} for sid, scope, cls in c.execute(
        "select source_id, site_scope, source_class_raw from sources").fetchall()}
    dups = defaultdict(list)
    for g, pid in c.execute("select dup_group_id, page_id from duplicate_page_candidates order by dup_group_id, "
                            "source_id, page_index").fetchall():
        dups[g].append(pid)
    copies = defaultdict(list)
    for sid, wid, lt, life in c.execute("select source_id, work_id, link_type, lifecycle_status from work_sources "
                                        "order by work_id, source_id").fetchall():
        if lt in ("FULL_COPY",) and life == "ACTIVE":
            copies[wid].append(sid)
    snap = c.execute("select snapshot_id, manifest_sha256 from snapshot").fetchone()
    return {"pages": pages, "tables": tabs, "sources": src, "dups": dups, "copies": copies, "snapshot": snap}


def build(nav: dict, refs: list[dict], budget: int) -> tuple[list[dict], dict]:
    pages, tabs, srcs = nav["pages"], nav["tables"], nav["sources"]
    by_src_idx = {(p["source_id"], p["page_index"]): pid for pid, p in pages.items()}
    # duplicates → one representative (first by source, page); copies of one work → the source cited most
    rep = {}
    for g, pids in nav["dups"].items():
        for pid in pids[1:]:
            rep[pid] = pids[0]
    cited = defaultdict(int)
    for r in refs:
        cited[r["source_id"]] += 1
    copy_of = {}
    for wid, sids in nav["copies"].items():
        if len(sids) > 1:
            keep = max(sids, key=lambda s: (cited.get(s, 0), -int(s[-3:])))
            for s in sids:
                if s != keep:
                    copy_of[s] = keep

    cand: dict[str, dict] = {}
    stats = defaultdict(int)

    def touch(pid, why, catalogue, row_id, range_value):
        if pid in rep:
            stats["merged_text_duplicate"] += 1
            pid = rep[pid]
        x = cand.setdefault(pid, {"reasons": set(), "catalogues": set(), "row_ids": [], "range_value": False,
                                  "needs": set()})
        x["reasons"].add(why)
        x["catalogues"].add(catalogue)
        if row_id:
            x["row_ids"].append(row_id)
        x["range_value"] |= range_value

    for r in refs:
        s = r["source_id"]
        if s in EXCLUDED_SOURCES:
            stats["excluded_source_refs"] += 1
            continue
        if s in copy_of:
            stats["copy_of_work_redirected"] += 1
            s = copy_of[s]
        n = r["last"] - r["first"] + 1
        if n > MAX_RANGE:
            stats["long_range_not_expanded"] += 1
            continue
        for p in range(r["first"], r["last"] + 1):
            pid = by_src_idx.get((s, p))
            if pid is None:
                stats["ref_page_not_in_nav"] += 1
                continue
            touch(pid, r["why"], r["catalogue"], r["row_id"], r["range_value"])

    # term search: need terms and numbers of that need on the page text with its tables
    for pid, p in pages.items():
        if p["source_id"] in EXCLUDED_SOURCES or p["source_id"] in copy_of or pid in rep:
            continue
        text = p["text"] + "\n" + "\n".join(tabs.get(pid, ()))
        hits = []
        for n, _m, trx, nrx in _NEED_RX:
            nt = len(trx.findall(text))
            if nt and nrx.search(text):
                hits.append((n, nt, len(nrx.findall(text))))
        if hits:
            p["_hits"] = hits

    rows = []
    for pid, p in pages.items():
        if p["source_id"] in EXCLUDED_SOURCES:
            continue
        x = cand.get(pid)
        hits = p.get("_hits", [])
        if x is None and not hits:
            continue
        text = p["text"] + "\n" + "\n".join(tabs.get(pid, ()))
        if len(text.strip()) < 150:
            stats["too_little_text"] += 1
            continue
        scope = srcs.get(p["source_id"], {}).get("scope", "")
        skru1_page = bool(_SKRU1.search(text))
        skru1_src = "SKRU1" in scope
        vkm = bool(_VKM.search(text)) or any(k in scope for k in ("VKM", "SKRU", "BKPRU"))
        need_names = {h[0] for h in hits}
        range_value = bool(x and x["range_value"]) or bool(need_names & RANGE_NEEDS and (skru1_page or skru1_src))
        if skru1_page or skru1_src or (range_value and vkm):
            tier = "A"
        elif vkm:
            tier = "B"
        else:
            tier = "C"
        score = (100 if x else 0) + sum(min(nt, 5) + min(nn, 10) for _n, nt, nn in hits) + len(tabs.get(pid, ())) * 5
        reasons = sorted(x["reasons"]) if x else []
        reasons += [f"search:{n}" for n in sorted(need_names)]
        rows.append({"page_id": pid, "source_id": p["source_id"], "page_index": p["page_index"], "tier": tier,
                     "score": score, "from_catalogue": "Y" if x else "N",
                     "skru1": "PAGE" if skru1_page else ("SOURCE" if skru1_src else ""),
                     "source_scope": scope, "n_tables": len(tabs.get(pid, ())), "chars": len(text),
                     "needs": ";".join(sorted(need_names)), "reasons": ";".join(reasons),
                     "catalogues": ";".join(sorted(x["catalogues"])) if x else "",
                     "catalogue_row_ids": ";".join(sorted(set(x["row_ids"])))[:2000] if x else ""})
    # budget order: tier A; catalogue pages of tiers B and C (known to carry values); search pages of B, then C
    def order(r):
        if r["tier"] == "A":
            band = 0
        elif r["from_catalogue"] == "Y":
            band = 1
        else:
            band = 2 + "BC".index(r["tier"])
        return band, "ABC".index(r["tier"]), -r["score"], r["page_id"]
    rows.sort(key=order)
    selected = rows[:budget]
    stats["candidates"] = len(rows)
    stats["selected"] = len(selected)
    stats["dropped_by_budget"] = len(rows) - len(selected)
    selected.sort(key=lambda r: (r["source_id"], r["page_index"]))
    return selected, dict(stats)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--nav", required=True, type=Path)
    ap.add_argument("--resources", required=True, type=Path)
    ap.add_argument("--dossiers", type=Path, default=ROOT / "docs" / "science" / "topic_dossiers")
    ap.add_argument("--figures", type=Path, default=ROOT / "work" / "world_obs_20261008" / "figure_candidates.json")
    ap.add_argument("--budget", type=int, default=2000)
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args()
    canon = a.resources / "11_evidence_vnext" / "canonical"
    refs = catalogue_refs(canon) + dossier_refs(a.dossiers) + figure_refs(a.figures)
    nav = load_nav(a.nav)
    rows, stats = build(nav, refs, a.budget)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    with open(a.out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    by_tier = defaultdict(int)
    for r in rows:
        by_tier[r["tier"]] += 1
    receipt = {"rule_version": RULE_VERSION, "nav_snapshot": list(nav["snapshot"]), "budget": a.budget,
               "n_refs": len(refs), "stats": stats, "by_tier": dict(sorted(by_tier.items())),
               "n_sources": len({r["source_id"] for r in rows}),
               "excluded_sources": sorted(EXCLUDED_SOURCES),
               "output_sha256": hashlib.sha256(a.out.read_bytes()).hexdigest()}
    (a.out.parent / "pages_receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=1) + "\n",
                                                     encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
