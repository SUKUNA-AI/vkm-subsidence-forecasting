#!/usr/bin/env python3
"""Borehole catalogue of Phase 1 updated with the picks of the page review (10.10.2026).

PRIVATE ``11_evidence_vnext/canonical/BOREHOLES/``: the Phase 1 build (26.09.2026, cloud stream, not re-runnable here) is
kept byte for byte in ``base_phase1/`` (created on the first run from the current tables, which must not contain review
rows yet); every run starts from that base, so the update is idempotent. The review picks
(``WORLD_PARAMETERS/curated/borehole_picks_<date>.csv``, ``scripts/build_borehole_picks_review.py``) are added:

* ``borehole_picks.csv`` — one row per review pick, ``pick_id`` ``PKR-<date>-<n>``, ``vn_id`` = the curated pick id, the
  review vocabulary of ``pick_kind`` (TOP / BOTTOM / INTERVAL / THICKNESS / TOTAL_DEPTH / COLLAR_ELEVATION / WATER_LEVEL /
  COORDINATES / OTHER) kept as printed; total depth goes to ``bottom_depth_m``, collar elevation to ``top_abs_m``.
* ``borehole_catalog.csv`` — a review pick linked by the reviewers to a catalogue borehole adds to that row: sources,
  locators, pick count and references, extraction method, a note; an EMPTY collar elevation / total depth / x, y is
  filled when the review gives exactly one value (basis ``PAGE_REVIEW_<date>``); a Phase 1 value is never overwritten —
  a different review value becomes a ``VALUE_CONFLICT`` issue; ``usable_for_3d`` NO → PARTIAL when numeric picks arrive.
  A pick without a catalogue borehole gives a NEW row ``BH-PR-S<src>-<label>``: one per source, label and site (the
  Phase 1 rule — identity only within one source and context, never across sources by the number alone); unnamed holes,
  groups («во всех скважинах») and pits are kept as such (``type``), never as numbered boreholes.
* ``borehole_identity_issues.csv`` — ``POSSIBLE_SAME_NOT_MERGED`` when a new row has the label of a catalogue borehole or of
  a new row of another source at a compatible site (decision KEPT_SEPARATE), ``VALUE_CONFLICT`` for disagreeing values.

Checks (exit 1 on failure): ids unique, scopes in the D-17 vocabulary, epistemic statuses, scale stated, every pick
references a catalogue row, cited issue ids exist, no legacy ``SKRU1_SKRU2``; and the ``vkm_world`` objects
(BoreholeRecord / PickObservation) build for the new and changed rows. Receipt ``build/page_review_update_receipt.json``.

Usage:  VKM_RESOURCES_ROOT=<PRIVATE> python scripts/update_borehole_catalogue.py [--check]
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path

csv.field_size_limit(sys.maxsize)
ROOT = Path(__file__).resolve().parents[1]
RULE_VERSION = "borehole_catalogue_page_review_v1"
FILES = ("borehole_catalog.csv", "borehole_picks.csv", "borehole_identity_issues.csv")
ALLOWED_SCOPES = {"SKRU1", "SKRU1_SKRU2_PILLAR", "SKRU1_OR_SKRU2_UNATTRIBUTED", "SOLIKAMSK_GROUP", "SKRU2", "SKRU3",
                  "BKPRU1", "BKPRU2", "BKPRU3", "BKPRU4", "USOLSKY", "OTHER_VKM_SITE", "VKM_REGIONAL",
                  "OTHER_POTASH_SITE", "NON_VKM", "GENERAL_METHOD", "PROJECT", "UNSTATED"}
ALLOWED_STATUS = {"FACT", "DERIVATION", "INTERPOLATION", "MODEL_CHOICE", "ENGINEERING_ASSUMPTION", "ANALOGUE", "UNKNOWN"}
SCOPE_OF_SITE = {"SKRU1": "SKRU1", "SKRU2": "SKRU2", "SKRU3": "SKRU3", "BKPRU1": "BKPRU1", "BKPRU2": "BKPRU2",
                 "BKPRU3": "BKPRU3", "BKPRU4": "BKPRU4", "SKRU1_OR_SKRU2_UNATTRIBUTED": "SKRU1_OR_SKRU2_UNATTRIBUTED",
                 "VKM_UNSPECIFIED": "VKM_REGIONAL", "UST_YAYVA": "OTHER_VKM_SITE", "BEREZNIKI_CITY": "OTHER_VKM_SITE",
                 "SOLIKAMSK_CITY": "OTHER_VKM_SITE", "OTHER_POTASH_SITE": "OTHER_POTASH_SITE", "NON_VKM": "NON_VKM",
                 "UNKNOWN": "UNSTATED", "": "UNSTATED"}
UNSPECIFIC = {"VKM_REGIONAL", "OTHER_VKM_SITE", "UNSTATED", "SOLIKAMSK_GROUP"}
OFF_VKM = {"NON_VKM", "OTHER_POTASH_SITE", "GENERAL_METHOD"}
CONF_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
DEPTH_COLS = ("top_depth_m", "bottom_depth_m", "top_abs_m", "bottom_abs_m", "thickness_m")
TR = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh", "з": "z", "и": "i", "к": "k", "л": "l",
      "й": "y", "ь": "", "ъ": "", "ё": "e", "щ": "sch", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s",
      "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "ы": "y", "э": "e", "ю": "yu", "я": "ya"}


# ------------------------------------------------------------------------------------------------ io
def read(p: Path) -> tuple[list[str], list[dict]]:
    with open(p, encoding="utf-8", newline="") as f:
        rd = csv.DictReader(f)
        return list(rd.fieldnames or []), list(rd)


def write(p: Path, cols: list[str], rows: list[dict]) -> bytes:
    """The Phase 1 dialect (CRLF, minimal quoting): unchanged rows stay byte-identical."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(cols)
    for r in rows:
        w.writerow([r.get(c, "") for c in cols])
    data = buf.getvalue().encode("utf-8")
    p.write_bytes(data)
    return data


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# ------------------------------------------------------------------------------------------------ labels
def norm_label(raw: str) -> str:
    """Printed borehole label → normalised label (as Phase 1 ``norm_id``): qualifiers in brackets, «скв.», «№» dropped."""
    s = re.sub(r"\([^)]*\)|«|»", " ", raw or "")
    s = re.sub(r"^\s*(подземная\s+|нефтяная\s+|солеразведочная\s+)?(скважина|скв\.?)\s*", "", s.strip(), flags=re.I)
    s = s.replace("№", " ")
    s = re.sub(r"(?<=\d)\s+(?=\d)", "", s)
    s = re.sub(r"\s+", " ", s).strip().rstrip(".,;").strip()
    return s


def label_kind(raw: str, norm: str) -> str:
    low = (raw or "").lower()
    if re.search(r"скважинах|всех скважин|групп", low):
        return "GROUP"
    if "шурф" in low:
        return "PIT"
    if not re.search(r"\d", norm) or re.search(r"безымянн|номер не напечатан|номер не указан", low):
        return "UNNAMED"
    return "NUMBERED"


def ascii_id(s: str) -> str:
    out = []
    for ch in s:
        lo = ch.lower()
        if lo in TR:
            out.append(TR[lo].upper() if ch != lo else TR[lo])
        elif ch.isascii() and (ch.isalnum() or ch == "-"):
            out.append(ch)
        elif ch in "/ ,":
            out.append("_")
    return re.sub(r"_+", "_", "".join(out)).strip("_")[:40] or "X"


def src3(source_id: str) -> str:
    m = re.search(r"(\d+)$", source_id or "")
    return m.group(1) if m else "X"


def num(v) -> float | None:
    try:
        return None if v in (None, "") else float(v)
    except ValueError:
        return None


def fmt(v: float) -> str:
    return f"{v:.6g}"


def page_of(page_id: str) -> str:
    src, _, p = (page_id or "").rpartition(":")
    return f"{src3(src)}:{p}" if src else page_id


# ------------------------------------------------------------------------------------------------ build
def base_tables(bh: Path, create: bool = True) -> dict[str, tuple[list[str], list[dict]]]:
    base = bh / "base_phase1"
    if not base.is_dir():
        cur = {f: read(bh / f) for f in FILES}
        if any(r["pick_id"].startswith("PKR-") for r in cur["borehole_picks.csv"][1]) or \
                any(r["borehole_id"].startswith("BH-PR-") for r in cur["borehole_catalog.csv"][1]):
            raise SystemExit("BOREHOLES has review rows but no base_phase1/ — restore the Phase 1 tables first")
        if not create:
            return cur
        base.mkdir()
        for f in FILES:
            shutil.copyfile(bh / f, base / f)
    return {f: read(base / f) for f in FILES}


def review_picks(curated: Path) -> list[tuple[str, dict]]:
    out = []
    for p in sorted(curated.glob("borehole_picks_*.csv")):
        m = re.fullmatch(r"borehole_picks_(\d{8})\.csv", p.name)
        if m:
            out += [(m.group(1), r) for r in read(p)[1]]
    return out


def pick_row(date: str, r: dict, bh_id: str, cols: list[str]) -> dict:
    kind = r.get("pick_kind") or "OTHER"
    v = {c: r.get(c, "") for c in DEPTH_COLS}
    if kind == "TOTAL_DEPTH" and r.get("total_depth_m") and not v["bottom_depth_m"]:
        v["bottom_depth_m"] = r["total_depth_m"]
    if kind == "COLLAR_ELEVATION" and r.get("collar_elevation_m") and not v["top_abs_m"]:
        v["top_abs_m"] = r["collar_elevation_m"]
    if v["top_depth_m"] or v["bottom_depth_m"]:
        ref = "depth_below_collar"
    elif v["top_abs_m"] or v["bottom_abs_m"]:
        ref = "absolute_elevation"
    elif v["thickness_m"]:
        ref = "thickness_only"
    else:
        ref = "unknown"
    unit = r.get("unit_canonical") or {"TOTAL_DEPTH": "BOREHOLE_TOTAL_DEPTH", "COLLAR_ELEVATION": "COLLAR",
                                       "WATER_LEVEL": "WATER_LEVEL", "COORDINATES": "COORDINATES"}.get(kind, "")
    if not unit and r.get("unit_as_printed"):
        unit = "UNRESOLVED:" + r["unit_as_printed"][:120]
    n = re.sub(r"\D", "", r.get("pick_id") or "") or "0"
    status, notes = r.get("status") if r.get("status") in ALLOWED_STATUS else "UNKNOWN", r.get("notes") or ""
    if status == "UNKNOWN" and any(v.values()):
        # an UNKNOWN quantity carries no value: the printed/scaled numbers are a reading (DERIVATION); what is unknown
        # (e.g. the rock of the interval) stays in the unit and the note
        status, notes = "DERIVATION", f"статус разбора UNKNOWN относится к содержанию интервала; {notes}"
    row = {c: "" for c in cols}
    row.update(v, pick_id=f"PKR-{date}-{int(n):05d}", borehole_id=bh_id,
               borehole_id_as_printed=r.get("borehole_as_printed") or "",
               unit_as_printed=r.get("unit_as_printed") or {"TOTAL_DEPTH": "забой скважины",
                                                            "COLLAR_ELEVATION": "устье скважины"}.get(kind, ""),
               unit_canonical_guess=unit, pick_kind=kind, reference=ref, reference_detail=r.get("crs_as_printed") or "",
               value_as_printed=r.get("value_as_printed") or "", units="m", source_id=r.get("source_id") or "",
               locator=(f"{r.get('page_id')}; {r.get('locator') or ''}").strip("; "), vn_id=r.get("pick_id") or "",
               extraction_method=f"PAGE_REVIEW_{date}:{r.get('read_from') or 'UNKNOWN'}",
               visual_check=f"{r.get('verification') or ''} (агенты по изображению страницы, {date})",
               pick_quality="NUMERIC" if any(v.values()) else "NON_NUMERIC_AS_PRINTED",
               status=status,
               scope=SCOPE_OF_SITE.get(r.get("site_norm") or "", "UNSTATED"), scale=r.get("scale") or "FIELD",
               confidence=r.get("confidence") or "", notes=notes[:500], quote=r.get("quote") or "")
    return row


def single(values: list[tuple[float, str, str]]) -> tuple[tuple[float, str, str] | None, bool]:
    """(value, as printed, pick id) when the review gives one value (±5 mm), and whether it disagrees with itself."""
    if not values:
        return None, False
    vs = sorted(values)
    return (vs[0], False) if vs[-1][0] - vs[0][0] <= 0.005 else (None, True)


def update(canon: Path, check_only: bool = False) -> dict:
    bh = canon / "BOREHOLES"
    tabs = base_tables(bh, create=not check_only)
    (ccols, cat), (pcols, picks), (icols, issues) = (tabs[f] for f in FILES)
    cat_ids = {r["borehole_id"] for r in cat}
    rev = review_picks(canon / "WORLD_PARAMETERS" / "curated")
    dates = sorted({d for d, _ in rev})

    # identity of the picks without a catalogue borehole: one row per source, label and site
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for d, r in rev:
        if not r.get("borehole_id_catalogue"):
            raw = r.get("borehole_as_printed") or ""
            norm = norm_label(raw)
            kind = label_kind(raw, norm)
            key = (r.get("source_id") or "", norm if kind in ("NUMBERED", "PIT") else raw.strip(), r.get("site_norm") or "",
                   kind)
            groups[key].append(r)
    new_id: dict[tuple, str] = {}
    sites_of = defaultdict(set)
    for (src, lab, site, kind) in groups:
        sites_of[(src, lab)].add(site)
    ordinal = Counter()
    used = set(cat_ids)
    for key in sorted(groups):
        src, lab, site, kind = key
        if kind in ("NUMBERED", "PIT"):
            bid = f"BH-PR-S{src3(src)}-{ascii_id(lab)}"
            if len(sites_of[(src, lab)]) > 1:
                bid += "-" + (site or "UNKNOWN")
        else:
            ordinal[(src, kind)] += 1
            bid = f"BH-PR-S{src3(src)}-{kind}{ordinal[(src, kind)]}"
        base_id, k = bid, 2
        while bid in used:
            bid, k = f"{base_id}-{k}", k + 1
        used.add(bid)
        new_id[key] = bid

    def bh_of(r: dict) -> str:
        if r.get("borehole_id_catalogue"):
            return r["borehole_id_catalogue"]
        raw = r.get("borehole_as_printed") or ""
        norm = norm_label(raw)
        kind = label_kind(raw, norm)
        return new_id[(r.get("source_id") or "", norm if kind in ("NUMBERED", "PIT") else raw.strip(),
                       r.get("site_norm") or "", kind)]

    new_picks = [pick_row(d, r, bh_of(r), pcols) for d, r in rev]
    by_bh: dict[str, list[tuple[dict, dict]]] = defaultdict(list)
    for (d, r), p in zip(rev, new_picks):
        by_bh[p["borehole_id"]].append((r, p))
    all_picks = picks + new_picks
    n_picks = Counter(p["borehole_id"] for p in all_picks)

    new_issues: list[dict] = []

    def issue(kind: str, ids: list[str], printed: list[str], rs: list[dict], descr: str, decision: str, conf: str,
              resolve: str) -> str:
        iid = f"ISS-PR-{len(new_issues) + 1:04d}"
        row = {c: "" for c in icols}
        row.update(issue_id=iid, issue_type=kind, borehole_ids=";".join(ids), ids_as_printed=" | ".join(printed),
                   sources=";".join(sorted({r.get("source_id") or "" for r in rs})),
                   locators="; ".join(sorted({r.get("page_id") or "" for r in rs}))[:600],
                   vn_ids=";".join(r.get("pick_id") or "" for r in rs)[:600], description=descr[:600],
                   decision=decision, identity_confidence=conf, what_would_resolve=resolve, issue_status="OPEN",
                   quote=(rs[0].get("quote") or "") if rs else "")
        new_issues.append(row)
        return iid

    issues_of = defaultdict(list)

    def fill_values(row: dict, items: list[tuple[dict, dict]], bid: str) -> list[str]:
        """Collar elevation, total depth and x, y from the review into EMPTY cells; disagreements → VALUE_CONFLICT."""
        notes = []
        coll = [(num(r.get("collar_elevation_m")) if r.get("collar_elevation_m") else num(p["top_abs_m"]),
                 r.get("value_as_printed") or "", p["pick_id"]) for r, p in items if p["pick_kind"] == "COLLAR_ELEVATION"]
        coll = [c for c in coll if c[0] is not None]
        tds = [(num(r.get("total_depth_m")) if r.get("total_depth_m") else num(p["bottom_depth_m"]),
                r.get("value_as_printed") or "", p["pick_id"]) for r, p in items if p["pick_kind"] == "TOTAL_DEPTH"]
        tds = [t for t in tds if t[0] is not None]
        xys = [((num(r.get("x")), num(r.get("y"))), r, p["pick_id"]) for r, p in items
               if r.get("x") not in (None, "") and r.get("y") not in (None, "")]
        for name, vals, col in (("отметка устья", coll, "collar_elevation"), ("глубина скважины", tds, "total_depth")):
            one, inner = single(vals)
            old = num(row.get(col))
            rs = [r for r, p in items if p["pick_id"] in {v[2] for v in vals}]
            if inner:
                iid = issue("VALUE_CONFLICT", [bid], [row.get("alternate_ids", "")[:80]], rs,
                            f"{name}: разбор страниц дал разные значения " + "; ".join(f"{fmt(v)} ({i})" for v, _, i in vals),
                            "KEPT_AS_PRINTED (ячейка каталога не заполнена)", "n/a", "сверить источники и системы высот")
                issues_of[bid].append(iid)
            elif one and old is None and not row.get(col):
                row[col] = fmt(one[0])
                if col == "collar_elevation":
                    row.update(collar_elevation_as_printed=one[1][:200],
                               collar_elevation_status=next((r.get("status") for r in rs if r.get("status") in
                                                             ALLOWED_STATUS), "UNKNOWN"),
                               collar_elevation_basis=f"PAGE_REVIEW: {one[2]}; система высот не указана")
                else:
                    row["total_depth_note"] = "; ".join(x for x in (row.get("total_depth_note"),
                                                                     f"PAGE_REVIEW: {one[2]} — {one[1][:120]}") if x)
                notes.append(f"{name} {fmt(one[0])} м из разбора")
            elif one and old is not None and abs(old - one[0]) > 0.005:
                iid = issue("VALUE_CONFLICT", [bid], [row.get("alternate_ids", "")[:80]], rs,
                            f"{name}: каталог Phase 1 {row.get(col)} м, разбор страниц {fmt(one[0])} м ({one[2]})",
                            "KEPT_PHASE1_VALUE", "n/a", "сверить источники и системы высот")
                issues_of[bid].append(iid)
        if xys:
            pts = {xy for xy, _, _ in xys}
            r0 = xys[0][1]
            if len(pts) == 1 and not row.get("x"):
                (x, y), = pts
                row.update(x=fmt(x), y=fmt(y), crs_as_printed=(r0.get("crs_as_printed") or "")[:300],
                           xy_locator=r0.get("page_id") or "", xy_status=r0.get("status") if r0.get("status") in
                           ALLOWED_STATUS else "DERIVATION", xy_method=f"PAGE_REVIEW: {xys[0][2]}; "
                           f"{(r0.get('value_as_printed') or '')[:160]}", xy_uncertainty="UNKNOWN")
                notes.append("координаты из разбора (своя сетка рисунка)")
            elif len(pts) > 1 or row.get("x"):
                iid = issue("VALUE_CONFLICT", [bid], [row.get("alternate_ids", "")[:80]], [r for _, r, _ in xys],
                            "координаты: " + "; ".join(f"({fmt(a)}, {fmt(b)})" for a, b in sorted(pts))
                            + (f"; в каталоге ({row['x']}, {row['y']})" if row.get("x") else ""),
                            "KEPT_PHASE1_VALUE" if row.get("x") else "KEPT_AS_PRINTED (ячейка каталога не заполнена)",
                            "n/a", "система координат каждого рисунка")
                issues_of[bid].append(iid)
        return notes

    # existing catalogue rows
    for row in cat:
        bid = row["borehole_id"]
        items = by_bh.get(bid)
        if not items:
            continue
        rs = [r for r, _ in items]
        ps = [p for _, p in items]
        notes = fill_values(row, items, bid)
        srcs = [s for s in row["sources"].split(";") if s]
        row["sources"] = ";".join(srcs + sorted({r.get("source_id") for r in rs} - set(srcs) - {""}))
        pages = ", ".join(sorted({page_of(r.get("page_id") or "") for r in rs}))
        row["locators"] = " | ".join(x for x in (row["locators"], f"разбор страниц: {pages}") if x)
        row["n_pick_rows"] = str(n_picks[bid])
        refs = [x for x in row["pick_references"].split(";") if x]
        row["pick_references"] = ";".join(refs + sorted({p["reference"] for p in ps} - set(refs)))
        meth = [x for x in row["extraction_methods"].split(";") if x]
        row["extraction_methods"] = ";".join(meth + [m for m in sorted({p["extraction_method"].split(":")[0]
                                                                        for p in ps}) if m not in meth])
        numeric = sum(1 for p in ps if p["pick_quality"] == "NUMERIC")
        if row["usable_for_3d"] == "NO" and numeric:
            row["usable_for_3d"] = "PARTIAL"
            row["usable_reason"] = "; ".join(x for x in (row["usable_reason"],
                                                         f"разбор страниц: +{numeric} численных отбивок") if x)
        row["notes"] = "; ".join(x for x in (row["notes"], f"Разбор страниц {', '.join(dates)}: +{len(ps)} отбивок "
                                             f"({ps[0]['pick_id']}…{ps[-1]['pick_id']})"
                                             + (f"; {'; '.join(notes)}" if notes else "")) if x)

    # new catalogue rows
    cat_by_norm = defaultdict(list)
    for row in cat:
        for a in re.split(r"\|", row["alternate_ids"]):
            if norm_label(a):
                cat_by_norm[norm_label(a).lower()].append(row)
    new_rows = []
    for key in sorted(groups):
        src, lab, site, kind = key
        bid = new_id[key]
        items = by_bh[bid]
        rs = [r for r, _ in items]
        ps = [p for _, p in items]
        scope = SCOPE_OF_SITE.get(site, "UNSTATED")
        printed = sorted({r.get("borehole_as_printed") or "" for r in rs})
        area = Counter(r.get("site_as_printed") for r in rs if r.get("site_as_printed")).most_common(1)
        numeric = sum(1 for p in ps if p["pick_quality"] == "NUMERIC")
        conf = min((r.get("confidence") for r in rs if r.get("confidence") in CONF_ORDER), key=CONF_ORDER.get,
                   default="MEDIUM")
        row = {c: "" for c in ccols}
        row.update(borehole_id=bid, alternate_ids=" | ".join(printed)[:300],
                   aliases_qualified=(f"S{src3(src)}:{lab}" + (f"@{site or 'UNKNOWN'}" if len(sites_of[(src, lab)]) > 1
                                                                else ""))[:200],
                   type={"GROUP": "GROUP (безымянная группа скважин, не одна скважина)", "PIT": "unknown (шурф)",
                         "UNNAMED": "unknown (номер не напечатан)"}.get(kind) or (
                       "underground" if any("подземн" in (p or "").lower() for p in printed) else "unknown"),
                   sources=src, locators="разбор страниц: " + ", ".join(sorted({page_of(r.get("page_id") or "")
                                                                               for r in rs})),
                   mine_or_site=(area[0][0] if area else site)[:300],
                   site_attribution_basis="разбор страниц: рудник по тексту/подписи страницы (агенты)",
                   site_attribution_status="UNKNOWN" if scope == "UNSTATED" else "FACT",
                   site_attribution_method="page text / caption (page review)", n_pick_rows=str(n_picks[bid]),
                   pick_references=";".join(sorted({p["reference"] for p in ps})),
                   stratigraphic_picks_summary="; ".join(sorted({f"{p['unit_as_printed'][:40]}: "
                                                                 f"{p['value_as_printed'][:40]}" for p in ps
                                                                 if p["pick_kind"] in ("TOP", "BOTTOM", "INTERVAL",
                                                                                       "THICKNESS")}))[:600],
                   uncertainty="тождество — только в пределах источника, подписи и участка; между источниками не "
                               "объединялось",
                   provenance_vn_ids=";".join(r.get("pick_id") or "" for r in rs)[:600],
                   extraction_methods=";".join(sorted({p["extraction_method"].split(":")[0] for p in ps})),
                   visual_check="; ".join(sorted({p["visual_check"] for p in ps}))[:300],
                   identity_basis="одна подпись, один источник и участок (разбор страниц)", identity_confidence="n/a",
                   area_context=(area[0][0] if area else "")[:300],
                   n_mentions=str(len({r.get("page_id") for r in rs})),
                   status="UNKNOWN" if all(r.get("status") == "UNKNOWN" for r in rs) else "FACT", scope=scope,
                   scale="FIELD", spatial_level="borehole", confidence=conf,
                   usable_for_3d="NO" if kind == "GROUP" or not numeric else "PARTIAL",
                   usable_reason=("группа скважин, не одна скважина" if kind == "GROUP" else
                                  f"разбор страниц: {numeric} численных отбивок; плановое положение "
                                  + ("есть" if any(r.get("x") for r in rs) else "не напечатано")),
                   notes=f"Новая строка разбора страниц {', '.join(dates)}: {len(ps)} отбивок", quote=rs[0].get("quote")
                   or "")
        notes = fill_values(row, items, bid)
        if notes:
            row["notes"] += "; " + "; ".join(notes)
        new_rows.append(row)
        if kind != "NUMBERED":
            continue
        cands = [c for c in cat_by_norm.get(lab.lower(), []) if compatible(scope, c["scope"])]
        if cands:
            iid = issue("POSSIBLE_SAME_NOT_MERGED", [bid] + [c["borehole_id"] for c in cands],
                        printed + [c["alternate_ids"][:60] for c in cands], rs,
                        f"подпись «{lab}» есть в каталоге ({', '.join(c['borehole_id'] for c in cands)}); разбор "
                        f"страниц связь не установил (участок {scope} / {', '.join(c['scope'] for c in cands)})",
                        "KEPT_SEPARATE", "UNDETERMINED", "плановое положение или прямое указание источника")
            issues_of[bid].append(iid)
    by_norm_new = defaultdict(list)
    for (src, lab, site, kind), bid in new_id.items():
        if kind == "NUMBERED":
            by_norm_new[lab.lower()].append((src, SCOPE_OF_SITE.get(site, "UNSTATED"), bid))
    for lab, xs in sorted(by_norm_new.items()):
        if len({s for s, _, _ in xs}) < 2:
            continue
        for i, (s1, sc1, b1) in enumerate(xs):
            for s2, sc2, b2 in xs[i + 1:]:
                if s1 != s2 and compatible(sc1, sc2):
                    rs = [r for r, _ in by_bh[b1][:1] + by_bh[b2][:1]]
                    iid = issue("POSSIBLE_SAME_NOT_MERGED", [b1, b2], [lab, lab], rs,
                                f"подпись «{lab}» в двух источниках ({s1}, {s2}) на совместимых участках ({sc1} / {sc2})",
                                "KEPT_SEPARATE", "UNDETERMINED", "плановое положение или прямое указание источника")
                    issues_of[b1].append(iid)
                    issues_of[b2].append(iid)
    for row in cat + new_rows:
        if issues_of.get(row["borehole_id"]):
            q = [x for x in row["unresolved_questions"].split(";") if x]
            row["unresolved_questions"] = ";".join(q + [i for i in issues_of[row["borehole_id"]] if i not in q])

    out_cat, out_issues = cat + new_rows, issues + new_issues
    problems = checks(out_cat, all_picks, out_issues)
    problems += schema_check([r for r in out_cat if r["borehole_id"] in by_bh], new_picks)
    summary = {"rule_version": RULE_VERSION, "review_files": dates, "review_picks": len(rev),
               "linked_to_phase1_rows": sum(1 for _, r in rev if r.get("borehole_id_catalogue")),
               "phase1_rows_updated": sum(1 for r in cat if r["borehole_id"] in by_bh),
               "new_rows": len(new_rows), "new_rows_by_type": dict(Counter(r["type"].split(" ")[0] for r in new_rows)),
               "new_rows_by_scope": dict(Counter(r["scope"] for r in new_rows)),
               "catalogue_rows": len(out_cat), "pick_rows": len(all_picks), "issue_rows": len(out_issues),
               "new_issues": dict(Counter(i["issue_type"] for i in new_issues)),
               "filled": {"collar_elevation": sum(1 for r in out_cat if "PAGE_REVIEW" in r["collar_elevation_basis"]),
                          "total_depth": sum(1 for r in out_cat if "PAGE_REVIEW" in r["total_depth_note"]),
                          "xy": sum(1 for r in out_cat if r["xy_method"].startswith("PAGE_REVIEW"))},
               "usable_for_3d": dict(Counter(r["usable_for_3d"] for r in out_cat)), "problems": problems[:40]}
    if check_only:
        return summary
    outs = {"borehole_catalog.csv": write(bh / "borehole_catalog.csv", ccols, out_cat),
            "borehole_picks.csv": write(bh / "borehole_picks.csv", pcols, all_picks),
            "borehole_identity_issues.csv": write(bh / "borehole_identity_issues.csv", icols, out_issues)}
    summary["inputs"] = {f"base_phase1/{f}": sha((bh / "base_phase1" / f).read_bytes()) for f in FILES}
    summary["inputs"].update({f"WORLD_PARAMETERS/curated/borehole_picks_{d}.csv": sha(
        (canon / "WORLD_PARAMETERS" / "curated" / f"borehole_picks_{d}.csv").read_bytes()) for d in dates})
    summary["outputs"] = {f: sha(b) for f, b in outs.items()}
    (bh / "build").mkdir(exist_ok=True)
    (bh / "build" / "page_review_update_receipt.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    return summary


def compatible(a: str, b: str) -> bool:
    if a in OFF_VKM or b in OFF_VKM:
        return a == b
    return a == b or a in UNSPECIFIC or b in UNSPECIFIC


def checks(cat: list[dict], picks: list[dict], issues: list[dict]) -> list[str]:
    out = []
    for name, rows, col in (("catalog", cat, "borehole_id"), ("picks", picks, "pick_id"), ("issues", issues, "issue_id")):
        dups = [k for k, n in Counter(r[col] for r in rows).items() if n > 1 or not k]
        if dups:
            out.append(f"{name}: duplicate/empty ids {dups[:5]}")
        if any(v == "SKRU1_SKRU2" or re.search(r"SKRU1_SKRU2(?![_A-Z])", v or "") for r in rows for v in r.values()):
            out.append(f"{name}: legacy SKRU1_SKRU2")
    for name, rows in (("catalog", cat), ("picks", picks)):
        bad = Counter(r["scope"] for r in rows if r["scope"] not in ALLOWED_SCOPES)
        if bad:
            out.append(f"{name}: scopes outside the vocabulary {dict(bad)}")
        if any(not r["scale"] for r in rows):
            out.append(f"{name}: empty scale")
    for col in ("status", "site_attribution_status", "collar_elevation_status", "xy_status"):
        bad = Counter(r[col] for r in cat if r[col] not in ALLOWED_STATUS and not (col != "status" and r[col] == ""))
        if bad:
            out.append(f"catalog: {col} outside the statuses {dict(bad)}")
    bad = Counter(r["status"] for r in picks if r["status"] not in ALLOWED_STATUS)
    if bad:
        out.append(f"picks: status outside the statuses {dict(bad)}")
    ids = {r["borehole_id"] for r in cat}
    orphan = sorted({p["borehole_id"] for p in picks} - ids)
    if orphan:
        out.append(f"picks without a catalogue row {orphan[:5]}")
    iss = {r["issue_id"] for r in issues}
    cited = {q for r in cat for q in r["unresolved_questions"].split(";") if q}
    if cited - iss:
        out.append(f"cited issue ids missing {sorted(cited - iss)[:5]}")
    return out


def schema_check(rows: list[dict], picks: list[dict]) -> list[str]:
    """``vkm_world`` objects of the new and changed rows build (as BOREHOLES/build/validate_schema.py)."""
    sys.path.insert(0, str(ROOT / "src"))
    from vkm_world.core.provenance import (EpistemicStatus, Provenance, Quantity, Scale, Scope, SourceRef,
                                           SpatialLevel, SpatialSupport)
    from vkm_world.geology.boreholes import BoreholeRecord, DepthReference, PickObservation, Usability3D
    scopes = {s.value for s in Scope}

    def prov(status, srcs, scope):
        st = EpistemicStatus(status if status in {e.value for e in EpistemicStatus} else "UNKNOWN")
        kw = {}
        if st is EpistemicStatus.DERIVATION:
            kw["method"] = "page review reading"
        if st is EpistemicStatus.INTERPOLATION:
            kw.update(method="interpolation by source author", inputs=("source-author-interpolation",))
        return Provenance(status=st, sources=tuple(srcs), scope=Scope(scope if scope in scopes else "UNSTATED"),
                          scale=Scale.FIELD, spatial=SpatialSupport(level=SpatialLevel.BOREHOLE), **kw)

    errs = Counter()
    for r in rows:
        try:
            BoreholeRecord(id=r["borehole_id"], provenance=prov(r["status"], [SourceRef(source_id=s) for s in
                                                                             r["sources"].split(";") if s], r["scope"]),
                           usable_for_3d=Usability3D(r["usable_for_3d"]), usability_reason=r["usable_reason"][:300])
        except Exception as e:  # noqa: BLE001
            errs[f"hole {r['borehole_id']}: {type(e).__name__}"] += 1
    for p in picks:
        abs_ref = p["reference"] == "absolute_elevation"
        top, bot = (p["top_abs_m"], p["bottom_abs_m"]) if abs_ref else (p["top_depth_m"], p["bottom_depth_m"])
        if not any([top, bot, p["thickness_m"]]):
            continue
        pv = prov(p["status"], [SourceRef(source_id=p["source_id"], locator=p["locator"][:200],
                                          evidence_ids=(p["vn_id"],))], p["scope"])
        q = (lambda n, v: None if v in ("", None) else Quantity(name=n, unit="m", value=float(v), provenance=pv,
                                                                original=p["value_as_printed"][:200]))
        try:
            u = p["unit_canonical_guess"] or "UNRESOLVED:?"
            PickObservation(id=p["pick_id"], provenance=pv, borehole_id=p["borehole_id"],
                            unit_id=u if not u.startswith("UNRESOLVED:") else u[:80].replace(" ", "_"),
                            unit_as_printed=p["unit_as_printed"] or "?", reference=DepthReference(p["reference"]),
                            top=q("top", top), bottom=q("bottom", bot), thickness=q("thickness", p["thickness_m"]))
        except Exception as e:  # noqa: BLE001
            errs[f"pick {p['pick_id']}: {str(e)[:120]}"] += 1
    return [f"schema: {k}" for k in sorted(errs)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="build and check in memory, write nothing")
    a = ap.parse_args()
    res = os.environ.get("VKM_RESOURCES_ROOT")
    if not res:
        print("set VKM_RESOURCES_ROOT", file=sys.stderr)
        return 2
    s = update(Path(res) / "11_evidence_vnext" / "canonical", check_only=a.check)
    print(json.dumps({k: v for k, v in s.items() if k not in ("inputs", "outputs")}, ensure_ascii=False, indent=1))
    return 1 if s["problems"] else 0


if __name__ == "__main__":
    sys.exit(main())
