#!/usr/bin/env python3
"""Draft of the world prior sheet (10.10.2026, rule v2 12.10.2026): for every parameter the worlds need (modules
C1–C4), the range the passport can offer per branch / hypothesis / site, the evidence behind it and whether the owner
has to choose. The worlds cover the whole VKM; the target sites are SKRU-1, SKRU-2 and SKRU-3, BKPRU-4 data are used
(owner, 10.10.2026): C1 and C4 are proposed per target site, C2 per class over the VKM with the count of each target
site. The sheet is a PROPOSAL (``approval`` = PROPOSAL_NOT_APPROVED): the owner approves it; no range here becomes a
world input by itself.

Inputs (PRIVATE ``11_evidence_vnext/canonical/``): ``WORLD_PASSPORT/passport_evidence.csv``,
``transfer_branches.csv``, ``priors_id_ledger.csv``; ``WORLD_PARAMETERS/curated/c3_hypotheses.csv``. The receipt lists
the files actually read with their SHA-256 and the SHA-256 of the code.

Rules: only evidence rows that are not excluded by the review and not confirmed duplicates (equal numbers alone are
match candidates and stay); retellings (CITED) only when no other row exists, marked. The proposal is the 10–90 % range
of the values when there are ≥ 5 of them from ≥ 2 sources, otherwise min–max. Never merged into one range: branches
A / B / N, measurements / parameters of published models / norms and plans (evidence kind), SI units, different
definitions of a creep law, site groups (a row without a named site — GENERAL_OR_UNSTATED — is not VKM context). A
parameter without evidence for the class stays UNKNOWN; norms (N) are listed, not proposed as physics. FACT for a
target site only when every row passes :func:`vkm_world.extraction.passport_rules.fact_failures` (own site, ORIGINAL,
natural scale, status FACT, value checked on the page).

Output: ``WORLD_PASSPORT/world_priors_proposal.csv``, ``world_priors_links.csv``, ``priors_id_ledger.csv``,
``world_priors_proposal_receipt.json``.

Usage:  VKM_RESOURCES_ROOT=<PRIVATE> python scripts/build_world_priors.py [--check]
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

csv.field_size_limit(sys.maxsize)
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from vkm_world.extraction import passport_rules as PR  # noqa: E402

RULE_VERSION = "world_priors_v2"
CODE_FILES = ("scripts/build_world_priors.py", "src/vkm_world/extraction/passport_rules.py")
LEDGER = "priors_id_ledger.csv"
APPROVAL = "PROPOSAL_NOT_APPROVED"
CLASSES = ("ROCKSALT", "SYLVINITE", "CARNALLITE", "CLAY_CONTACT", "TRANSITION_OVERBURDEN", "COVER", "BACKFILL")
C2_PARAMS = ("RHO", "E", "EDEF", "NU", "UCS", "UTS", "COH", "PHI", "LTS_RATIO")
C4_PARAMS = ("CHAMBER_WIDTH", "PILLAR_WIDTH", "CHAMBER_HEIGHT", "EXTRACTION_RATIO", "LOADING_DEGREE",
             "BACKFILL_RATIO", "BACKFILL_DELAY", "BACKFILL_STIFFNESS", "BACKFILL_COMPACTION")
VKM_SITE_GROUPS = ("SKRU1", "OTHER_VKM_SITE", "VKM_REGIONAL")          # a named VKM site or the VKM as a whole
SITE_CLASS = {**{g: "VKM" for g in VKM_SITE_GROUPS}, "GENERAL_OR_UNSTATED": "SITE_UNSTATED", "NON_VKM": "NON_VKM"}
TARGETS = {"SKRU1": "СКРУ-1", "SKRU2": "СКРУ-2", "SKRU3": "СКРУ-3", "BKPRU4": "БКПРУ-4"}
C1_PARAMS = ("THICKNESS", "DEPTH_TOP", "ELEV_TOP")
NON_TARGET_VKM = {"SKRU1_OR_SKRU2_UNATTRIBUTED", "SKRU2_OR_SKRU3_UNATTRIBUTED", "SKRU1_SKRU2_SKRU3_POOLED",
                  "SOLIKAMSK_MINES_UNATTRIBUTED", "SKRU3_ATTRIBUTION_NOT_ESTABLISHED", "BKPRU1", "BKPRU2", "BKPRU3",
                  "UST_YAYVA", "BEREZNIKI_CITY", "SOLIKAMSK_CITY", "VKM_UNSPECIFIED"}
EXCLUDED = {"ADJUDICATED_REJECT", "ADJUDICATED_NOT_WORLD"}
KIND_LABEL = {"MEASUREMENT": "измерение", "PUBLISHED_MODEL": "параметр опубликованной модели",
              "NORMATIVE": "норматив / проект / план", "UNKNOWN_KIND": "вид evidence не установлен"}
_READ: dict[str, str] = {}


def per_site(rows: list[dict]) -> str:
    """«СКРУ-1: 3, СКРУ-2: 0, …» for the target sites, plus rows of SKRU-1 or SKRU-2 without split."""
    n = defaultdict(int)
    for x in rows:
        n[x["site_norm"]] += 1
    txt = ", ".join(f"{v}: {n[k]}" for k, v in TARGETS.items())
    return (txt + (f"; СКРУ-1 или СКРУ-2 без разделения: {n['SKRU1_OR_SKRU2_UNATTRIBUTED']}"
                   if n["SKRU1_OR_SKRU2_UNATTRIBUTED"] else "")
            + (f"; СКРУ-2 или СКРУ-3 без разделения: {n['SKRU2_OR_SKRU3_UNATTRIBUTED']}"
               if n["SKRU2_OR_SKRU3_UNATTRIBUTED"] else ""))


def _rows(p: Path, root: Path) -> list[dict]:
    rel = p.relative_to(root).as_posix()
    if not p.is_file():
        _READ.setdefault(rel, "ABSENT")
        return []
    data = p.read_bytes()
    _READ[rel] = hashlib.sha256(data).hexdigest()
    return list(csv.DictReader(io.StringIO(io.StringIO(data.decode("utf-8"), newline=None).read())))


def _q(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    lo = int(k)
    return xs[lo] + (xs[min(lo + 1, len(xs) - 1)] - xs[lo]) * (k - lo)


def propose(rows: list[dict]) -> dict:
    """Proposed range from evidence rows of one group: retellings (CITED) only when nothing else exists."""
    own = [x for x in rows if PR.origin_class(x["origin"]) != PR.CITED]
    use = own or rows
    vals = [float(v) for x in use for v in {x["si_min"], x["si_max"]} if v not in ("", None)]
    srcs = {x["source_id"] for x in use}
    out = {"n_values": len(vals), "n_sources": len(srcs),
           "n_original": sum(1 for x in use if PR.origin_class(x["origin"]) == PR.ORIGINAL),
           "n_origin_unknown": sum(1 for x in use if PR.origin_class(x["origin"]) == PR.UNKNOWN),
           "only_cited": "Y" if rows and not own else "", "adjudicated": sum(1 for x in use if x["verification"]
                                                                             .startswith("ADJUDICATED")),
           "n_match_candidates": sum(1 for x in use if x.get("copy_role") in PR.CANDIDATE_ROLES),
           "sources": ";".join(sorted(srcs))[:400], "evidence_ids": ";".join(x["ev_id"] for x in use)[:1200],
           "_use": use, **{k: v for k, v in PR.checks(use).items() if k != "site_check"}}
    if not vals:
        return dict(out, proposed_min="", proposed_max="", rule="NO_NUMBERS")
    if len(vals) >= 5 and len(srcs) >= 2:
        return dict(out, proposed_min=f"{_q(vals, .1):.4g}", proposed_max=f"{_q(vals, .9):.4g}", rule="P10-P90",
                    full_min=f"{min(vals):.4g}", full_max=f"{max(vals):.4g}")
    return dict(out, proposed_min=f"{min(vals):.4g}", proposed_max=f"{max(vals):.4g}", rule="MIN-MAX",
                full_min=f"{min(vals):.4g}", full_max=f"{max(vals):.4g}")


def split(rows: list[dict], creep: bool = False) -> list[tuple[tuple[str, ...], list[dict]]]:
    """Rows of one proposal cell split by evidence kind, SI unit and (creep laws) definition; sorted. A row without an
    SI value carries no unit: it joins the single unit of its kind / definition."""
    cell = lambda x: (PR.evidence_kind(x["scale"]),) + ((PR.definition_key(x.get("equation_as_printed")),)  # noqa: E731
                                                         if creep else ())
    units = defaultdict(set)
    for x in rows:
        if x["si_unit"]:
            units[cell(x)].add(x["si_unit"])
    g = defaultdict(list)
    for x in rows:
        u = units.get(cell(x), set())
        c = cell(x)
        g[(c[0], x["si_unit"] or (next(iter(u)) if len(u) == 1 else "-")) + c[1:]].append(x)
    return sorted(g.items())


def build(canon: Path) -> dict[str, bytes]:
    _READ.clear()
    wp = canon / "WORLD_PASSPORT"
    ev = [x for x in _rows(wp / "passport_evidence.csv", canon)
          if x["verification"] not in EXCLUDED and x.get("copy_role") not in PR.CONFIRMED_DUPLICATE_ROLES]
    trans = _rows(wp / "transfer_branches.csv", canon)
    hyps = _rows(canon / "WORLD_PARAMETERS" / "curated" / "c3_hypotheses.csv", canon)
    ledger = PR.IdLedger(_rows(wp / LEDGER, canon))
    by = defaultdict(list)
    for x in ev:
        by[(x["module"], x["parameter"], x["target"], x["site_group"], x["branch"])].append(x)
    out = []

    def add(module, param, target, branch, site, unit, status, note, rows, choice="", base=None, parts=()):
        key = "|".join((module, param, target, branch, site))
        rec = {"prior_id": "", "natural_key": "|".join((key, *parts)) if parts else key, "_base": base or key,
               "module": module, "parameter": param, "target": target, "branch_or_hypothesis": branch,
               "evidence_kind": parts[0] if parts else "", "site_group": site, "si_unit": unit,
               "world_status": status, "approval": APPROVAL}
        rec.update(propose(rows) if rows else {"proposed_min": "", "proposed_max": "", "rule": "UNKNOWN",
                                               "n_values": 0, "n_sources": 0, "n_original": 0,
                                               "n_origin_unknown": 0, "only_cited": "", "adjudicated": 0,
                                               "n_match_candidates": 0, "sources": "", "evidence_ids": "",
                                               "_use": [], "scientific_use": PR.SCIENTIFIC_USE})
        rec.update(owner_choice_needed=choice, note=note)
        out.append(rec)

    for cls in CLASSES:
        for p in C2_PARAMS:
            unit0 = {"RHO": "kg/m3", "NU": "1", "PHI": "deg", "LTS_RATIO": "1"}.get(p, "Pa")
            found = False
            for br, label in (("A", "A: лаборатория → массив через множитель"), ("B", "B: эффективное (массив/модель)"),
                              ("N", "N: норматив (не физика)")):
                cells = defaultdict(list)
                for site_group, sclass in SITE_CLASS.items():
                    cells[sclass] += [x for x in by.get(("C2", p, cls, site_group, br), []) if x["si_min"]]
                site_rows = [(s, cells[s]) for s in ("VKM", "SITE_UNSTATED") if cells[s]]
                if not cells["VKM"] and cells["NON_VKM"]:
                    site_rows.append(("NON_VKM", cells["NON_VKM"]))
                old_site = "VKM" if [x for s in ("VKM", "SITE_UNSTATED") for x in cells[s]] else "NON_VKM"
                tf = [t["transfer_id"] for t in trans if t.get("parameter", "").split(" ")[0] in (p, "EDEF" if p == "E" else p)
                      and cls in (t.get("material_class") or "")]
                for site, rows_all in site_rows:
                    for parts, rows in split(rows_all):
                        found = True
                        note = (label + f"; {KIND_LABEL[parts[0]]}"
                                + (f"; из них {per_site(rows)}" if site == "VKM" else
                                   "; площадка не указана — не контекст ВКМ" if site == "SITE_UNSTATED" else
                                   "; только аналоги вне ВКМ (Transfer)"))
                        if br == "A":
                            note += f"; множители переноса: {';'.join(tf)[:200]}" if tf else "; множитель переноса UNKNOWN"
                        status = ("NORMATIVE_LISTED" if br == "N" else "SCENARIO_RANGE" if site in ("VKM", "SITE_UNSTATED")
                                  else "ANALOGUE_VIA_TRANSFER")
                        add("C2", p, cls, br, site, parts[1] if parts[1] != "-" else unit0, status, note, rows,
                            "Y" if br in ("A", "B") else "", base="|".join(("C2", p, cls, br, old_site)), parts=parts)
            if not found:
                add("C2", p, cls, "-", "VKM", unit0, "UNKNOWN", "нет evidence для класса", [])
        creep = [x for x in ev if x["module"] == "C2" and x["parameter"].startswith("CREEP") and x["target"] == cls]
        laws = sorted({x["parameter"] for x in creep})
        for law in laws:
            rows_law = [x for x in creep if x["parameter"] == law]
            old_key = "|".join(("C2", law, cls, ";".join(sorted({x["branch"] for x in rows_law})), "ANY"))
            cells = defaultdict(list)
            for x in rows_law:
                cells[(x["branch"], x["site_group"])].append(x)
            for (br, site), rows_c in sorted(cells.items()):
                for parts, rows in split(rows_c, creep=True):
                    defn = next((x.get("equation_as_printed") for x in rows if x.get("equation_as_printed")), "")
                    add("C2", law, cls, br, site, parts[1] if parts[1] != "-" else "",
                        "ANALOGUE_VIA_TRANSFER" if site == "NON_VKM" else "SCENARIO_RANGE",
                        f"параметр закона ползучести (семейство — выбор мира); {KIND_LABEL[parts[0]]}; определение: "
                        f"{defn[:160] or 'не указано'}" + ("; площадка не указана — не контекст ВКМ"
                                                           if site == "GENERAL_OR_UNSTATED" else ""),
                        rows, "Y", base=old_key, parts=parts)
        if not laws:
            add("C2", "CREEP_LAW_PARAM", cls, "-", "VKM", "", "UNKNOWN", "нет закона ползучести для класса", [])
    for h in hyps:
        add("C3", "LAMBDA", h["hypothesis_id"], h["hypothesis_id"], "ANY", "1",
            "SCENARIO_RANGE" if h.get("lambda_min") not in ("", "UNKNOWN") else "UNKNOWN",
            f"{h.get('name_ru', '')[:120]}; λ {h.get('lambda_min')}–{h.get('lambda_max')}; "
            f"применимость к СКРУ-1: {h.get('applicability_SKRU1', '')}", [], "Y")
        out[-1].update(proposed_min=h.get("lambda_min", ""), proposed_max=h.get("lambda_max", ""), rule="HYPOTHESIS")
    for p in ("TEMPERATURE", "PORE_PRESSURE"):
        rows_p = [x for x in ev if x["module"] == "C3" and x["parameter"] == p and x["si_min"]]
        old_key = "|".join(("C3", p, "UNSPECIFIED", "-", "ANY"))
        if not rows_p:
            add("C3", p, "UNSPECIFIED", "-", "ANY", "", "UNKNOWN", "нет evidence", [])
            continue
        cells = defaultdict(list)
        for x in rows_p:
            cells[SITE_CLASS.get(x["site_group"], "SITE_UNSTATED")].append(x)
        for site, rows_c in sorted(cells.items()):
            for parts, rows in split(rows_c):
                add("C3", p, "UNSPECIFIED", "-", site, parts[1] if parts[1] != "-" else "",
                    "ANALOGUE_VIA_TRANSFER" if site == "NON_VKM" else "SCENARIO_RANGE",
                    f"{KIND_LABEL[parts[0]]}; по участкам: {per_site(rows)}", rows, "Y", base=old_key, parts=parts)
    for p in C1_PARAMS:                    # per target site; regional data only where a site has none
        c1 = [x for x in ev if x["module"] == "C1" and x["parameter"] == p and x["si_min"]]
        for unit_id in sorted({x["target"] for x in c1 if x["target"] and not x["target"].startswith("UNRESOLVED")}):
            rows = [x for x in c1 if x["target"] == unit_id]
            missing = []
            for site, name in TARGETS.items():
                own = [x for x in rows if x["site_norm"] == site]
                if own:
                    why = PR.fact_failures(own)
                    add("C1", p, unit_id, "-", site, "m", "SCENARIO_RANGE" if why else "FACT",
                        f"{name}: скважины/разрезы участка" + (f"; не FACT: {'; '.join(why)}" if why else ""), own)
                else:
                    missing.append(name)
            rest = [x for x in rows if x["site_norm"] in NON_TARGET_VKM]
            if missing and rest:
                add("C1", p, unit_id, "-", "VKM", "m", "ANALOGUE_VIA_TRANSFER",
                    f"нет своих значений у: {', '.join(missing)}; данные других участков ВКМ", rest, "Y")
    for p in C4_PARAMS:
        rows_p = [x for x in ev if x["module"] == "C4" and x["parameter"] == p]
        missing = []
        for site, name in TARGETS.items():
            rows_s = [x for x in rows_p if x["site_norm"] == site]
            if rows_s:
                tgt = f"{site} (по зонам/пластам — см. evidence)"
                for parts, rows in split(rows_s):
                    add("C4", p, tgt, "-", site, parts[1] if parts[1] != "-" else "", "SCENARIO_RANGE",
                        f"{KIND_LABEL[parts[0]]} {name}; распределение по зонам — из календаря C4", rows,
                        base="|".join(("C4", p, tgt, "-", site)), parts=parts)
            else:
                missing.append(name)
        rest = [x for x in rows_p if x["site_norm"] in NON_TARGET_VKM]
        if missing and rest:
            for parts, rows in split(rest):
                add("C4", p, "VKM", "-", "VKM", parts[1] if parts[1] != "-" else "", "ANALOGUE_VIA_TRANSFER",
                    f"{KIND_LABEL[parts[0]]}; нет своих значений у: {', '.join(missing)}", rows, "Y",
                    base="|".join(("C4", p, "VKM", "-", "VKM")), parts=parts)
        elif missing:
            add("C4", p, "-", "-", "VKM", "", "UNKNOWN", f"нет evidence у: {', '.join(missing)}", [])
    keys = [(r["natural_key"], r["_base"]) for r in out]
    ids = ledger.assign("WPR", keys, RULE_VERSION, 4)
    links = []
    for r in out:
        r["prior_id"] = ids[r["natural_key"]]
        links += [{"prior_id": r["prior_id"], "ev_id": x["ev_id"]} for x in r.pop("_use")]
    links.sort(key=lambda r: (r["prior_id"], r["ev_id"]))
    cols = ["prior_id", "natural_key", "module", "parameter", "target", "branch_or_hypothesis", "evidence_kind",
            "site_group", "si_unit", "world_status", "approval", "proposed_min", "proposed_max", "rule", "full_min",
            "full_max", "n_values", "n_sources", "n_original", "n_origin_unknown", "only_cited", "adjudicated",
            "n_match_candidates", "origin_check", "value_check", "scientific_use", "owner_choice_needed", "note",
            "sources", "evidence_ids"]

    def table(rows, cs):
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=cs, lineterminator="\n", extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cs})
        return buf.getvalue().encode("utf-8")

    files = {"world_priors_proposal.csv": table(out, cols), "world_priors_links.csv": table(links, ["prior_id", "ev_id"]),
             LEDGER: table(ledger.table(), PR.LEDGER_COLS)}
    counts = Counter(f"{r['module']}:{r['world_status']}" for r in out)
    receipt = {"rule_version": RULE_VERSION,
               "code_sha256": {f: hashlib.sha256((ROOT / f).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
                               for f in CODE_FILES},
               "inputs": dict(sorted(_READ.items())),
               "outputs": {k: hashlib.sha256(v).hexdigest() for k, v in files.items()},
               "output_sha256": hashlib.sha256(files["world_priors_proposal.csv"]).hexdigest(), "rows": len(out),
               "by_status": dict(sorted(counts.items())),
               "ledger": dict(sorted(Counter(f"{r['kind']}:{r['state']}" for r in ledger.rows).items())),
               "owner_choice_needed": sum(1 for r in out if r["owner_choice_needed"] == "Y"),
               "approval": APPROVAL}
    files["world_priors_proposal_receipt.json"] = (json.dumps(receipt, ensure_ascii=False, indent=1) + "\n").encode()
    return files


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    res = os.environ.get("VKM_RESOURCES_ROOT")
    if not res:
        print("set VKM_RESOURCES_ROOT", file=sys.stderr)
        return 2
    canon = Path(res) / "11_evidence_vnext" / "canonical"
    files = build(canon)
    out = canon / "WORLD_PASSPORT"
    if a.check:
        bad = [k for k, v in files.items() if not (out / k).is_file() or (out / k).read_bytes() != v]
        print(json.dumps({"check": "FAIL" if bad else "PASS", "differs": bad}))
        return 1 if bad else 0
    for k, v in files.items():
        (out / k).write_bytes(v)
    print(files["world_priors_proposal_receipt.json"].decode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
