#!/usr/bin/env python3
"""Draft of the world prior sheet (10.10.2026): for every parameter the worlds of SKRU-1 need (modules C1–C4), the
range the passport can offer per branch / hypothesis / site group, the evidence behind it and whether the owner has to
choose. The sheet is a PROPOSAL: the owner approves it; nothing here becomes a world input by itself.

Inputs (PRIVATE ``11_evidence_vnext/canonical/``): ``WORLD_PASSPORT/world_passport.csv``, ``passport_evidence.csv``,
``transfer_branches.csv``; ``WORLD_PARAMETERS/curated/c3_hypotheses.csv``.

Rules: only evidence rows that are not excluded by the review and not copies; ORIGINAL rows first (CITED only when no
original exists, marked); the proposal is the 10–90 % range of the values when there are ≥ 5 of them from ≥ 2 sources,
otherwise min–max; LAB (branch A) and calibrated / massif (branch B) are never merged; a parameter without evidence
for the class stays UNKNOWN; norms (N) are listed, not proposed as physics.

Output: ``WORLD_PASSPORT/world_priors_proposal.csv``, ``world_priors_proposal_receipt.json``.

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
from collections import defaultdict
from pathlib import Path

csv.field_size_limit(sys.maxsize)
RULE_VERSION = "world_priors_v0"
CLASSES = ("ROCKSALT", "SYLVINITE", "CARNALLITE", "CLAY_CONTACT", "TRANSITION_OVERBURDEN", "COVER", "BACKFILL")
C2_PARAMS = ("RHO", "E", "EDEF", "NU", "UCS", "UTS", "COH", "PHI", "LTS_RATIO")
C4_PARAMS = ("CHAMBER_WIDTH", "PILLAR_WIDTH", "CHAMBER_HEIGHT", "EXTRACTION_RATIO", "LOADING_DEGREE",
             "BACKFILL_RATIO", "BACKFILL_DELAY", "BACKFILL_STIFFNESS", "BACKFILL_COMPACTION")
SITE_ORDER = ("SKRU1", "OTHER_VKM_SITE", "VKM_REGIONAL", "GENERAL_OR_UNSTATED", "NON_VKM")
EXCLUDED = {"ADJUDICATED_REJECT", "ADJUDICATED_NOT_WORLD"}


def _rows(p: Path) -> list[dict]:
    return list(csv.DictReader(open(p, encoding="utf-8"))) if p.is_file() else []


def _q(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    lo = int(k)
    return xs[lo] + (xs[min(lo + 1, len(xs) - 1)] - xs[lo]) * (k - lo)


def propose(rows: list[dict]) -> dict:
    """Proposed range from evidence rows of one group."""
    orig = [x for x in rows if x["origin"] != "CITED"]
    use = orig or rows
    vals = [float(v) for x in use for v in {x["si_min"], x["si_max"]} if v not in ("", None)]
    srcs = {x["source_id"] for x in use}
    out = {"n_values": len(vals), "n_sources": len(srcs), "n_original": len(orig),
           "only_cited": "Y" if not orig else "", "adjudicated": sum(1 for x in use if x["verification"].startswith(
               "ADJUDICATED")), "sources": ";".join(sorted(srcs))[:400],
           "evidence_ids": ";".join(x["ev_id"] for x in use)[:1200]}
    if not vals:
        return dict(out, proposed_min="", proposed_max="", rule="NO_NUMBERS")
    if len(vals) >= 5 and len(srcs) >= 2:
        return dict(out, proposed_min=f"{_q(vals, .1):.4g}", proposed_max=f"{_q(vals, .9):.4g}", rule="P10-P90",
                    full_min=f"{min(vals):.4g}", full_max=f"{max(vals):.4g}")
    return dict(out, proposed_min=f"{min(vals):.4g}", proposed_max=f"{max(vals):.4g}", rule="MIN-MAX",
                full_min=f"{min(vals):.4g}", full_max=f"{max(vals):.4g}")


def build(canon: Path) -> dict[str, bytes]:
    wp = canon / "WORLD_PASSPORT"
    ev = [x for x in _rows(wp / "passport_evidence.csv")
          if x["verification"] not in EXCLUDED and x.get("copy_role") not in ("COPY_CITED", "SAME_NUMBER_OTHER_PRODUCER")]
    trans = _rows(wp / "transfer_branches.csv")
    hyps = _rows(canon / "WORLD_PARAMETERS" / "curated" / "c3_hypotheses.csv")
    by = defaultdict(list)
    for x in ev:
        by[(x["module"], x["parameter"], x["target"], x["site_group"], x["branch"])].append(x)
    out = []

    def add(module, param, target, branch, site, unit, status, note, rows, choice=""):
        rec = {"prior_id": "", "module": module, "parameter": param, "target": target, "branch_or_hypothesis": branch,
               "site_group": site, "si_unit": unit, "world_status": status}
        rec.update(propose(rows) if rows else {"proposed_min": "", "proposed_max": "", "rule": "UNKNOWN",
                                               "n_values": 0, "n_sources": 0, "n_original": 0, "only_cited": "",
                                               "adjudicated": 0, "sources": "", "evidence_ids": ""})
        rec.update(owner_choice_needed=choice, note=note)
        out.append(rec)

    for cls in CLASSES:
        for p in C2_PARAMS:
            unit = {"RHO": "kg/m3", "NU": "1", "PHI": "deg", "LTS_RATIO": "1"}.get(p, "Pa")
            found = False
            for br, label in (("A", "A: лаборатория → массив через множитель"), ("B", "B: эффективное (массив/модель)"),
                              ("N", "N: норматив (не физика)")):
                vkm = [x for site in SITE_ORDER[:4] for x in by.get(("C2", p, cls, site, br), []) if x["si_min"]]
                ana = [x for x in by.get(("C2", p, cls, "NON_VKM", br), []) if x["si_min"]]
                rows, site = (vkm, "VKM") if vkm else (ana, "NON_VKM")
                if not rows:
                    continue
                found = True
                n_sk = sum(1 for x in rows if x["site_group"] == "SKRU1")
                tf = [t["transfer_id"] for t in trans if t.get("parameter", "").split(" ")[0] in (p, "EDEF" if p == "E" else p)
                      and cls in (t.get("material_class") or "")]
                note = label + (f"; из них СКРУ-1: {n_sk}" if site == "VKM" else "; только аналоги вне ВКМ (Transfer)")
                if br == "A":
                    note += f"; множители переноса: {';'.join(tf)[:200]}" if tf else "; множитель переноса UNKNOWN"
                add("C2", p, cls, br, site, unit,
                    ("SCENARIO_RANGE" if site == "VKM" else "ANALOGUE_VIA_TRANSFER") if br in ("A", "B")
                    else "NORMATIVE_LISTED", note, rows, "Y" if br in ("A", "B") else "")
            if not found:
                add("C2", p, cls, "-", "SKRU1", unit, "UNKNOWN", "нет evidence для класса", [])
        creep = [x for x in ev if x["module"] == "C2" and x["parameter"].startswith("CREEP") and x["target"] == cls]
        laws = sorted({x["parameter"] for x in creep})
        for law in laws:
            rows = [x for x in creep if x["parameter"] == law]
            add("C2", law, cls, ";".join(sorted({x["branch"] for x in rows})), "ANY", rows[0]["si_unit"],
                "SCENARIO_RANGE", "параметр закона ползучести (семейство — выбор мира)", rows, "Y")
        if not laws:
            add("C2", "CREEP_LAW_PARAM", cls, "-", "SKRU1", "", "UNKNOWN", "нет закона ползучести для класса", [])
    for h in hyps:
        add("C3", "LAMBDA", h["hypothesis_id"], h["hypothesis_id"], "ANY", "1",
            "SCENARIO_RANGE" if h.get("lambda_min") not in ("", "UNKNOWN") else "UNKNOWN",
            f"{h.get('name_ru', '')[:120]}; λ {h.get('lambda_min')}–{h.get('lambda_max')}; "
            f"применимость к СКРУ-1: {h.get('applicability_SKRU1', '')}", [], "Y")
        out[-1].update(proposed_min=h.get("lambda_min", ""), proposed_max=h.get("lambda_max", ""), rule="HYPOTHESIS")
    for p in ("TEMPERATURE", "PORE_PRESSURE"):
        rows = [x for x in ev if x["module"] == "C3" and x["parameter"] == p and x["si_min"]]
        add("C3", p, "UNSPECIFIED", "-", "ANY", rows[0]["si_unit"] if rows else "", "SCENARIO_RANGE" if rows else "UNKNOWN",
            "СКРУ-1: нет собственных значений" if rows else "нет evidence", rows, "Y" if rows else "")
    c1 = [x for x in ev if x["module"] == "C1" and x["parameter"] == "THICKNESS" and x["si_min"]]
    for unit_id in sorted({x["target"] for x in c1 if x["target"]}):
        rows_sk = [x for x in c1 if x["target"] == unit_id and x["site_group"] == "SKRU1"]
        rows_vk = [x for x in c1 if x["target"] == unit_id and x["site_group"] != "SKRU1"]
        if rows_sk:
            add("C1", "THICKNESS", unit_id, "-", "SKRU1", "m", "FACT", "мощность по СКРУ-1 (скважины/разрезы)",
                rows_sk)
        elif rows_vk:
            add("C1", "THICKNESS", unit_id, "-", "VKM", "m", "ANALOGUE_VIA_TRANSFER", "только региональные данные",
                rows_vk, "Y")
    for p in C4_PARAMS:
        rows = [x for x in ev if x["module"] == "C4" and x["parameter"] == p and x["site_group"] == "SKRU1"]
        rows_vk = [x for x in ev if x["module"] == "C4" and x["parameter"] == p and x["site_group"] != "SKRU1"]
        if rows:
            add("C4", p, "SKRU1 (по зонам/пластам — см. evidence)", "-", "SKRU1", rows[0]["si_unit"] or "",
                "SCENARIO_RANGE", "проект/факт СКРУ-1; распределение по зонам — из календаря C4", rows)
        elif rows_vk:
            add("C4", p, "VKM", "-", "VKM", rows_vk[0]["si_unit"] or "", "ANALOGUE_VIA_TRANSFER",
                "нет значений СКРУ-1", rows_vk, "Y")
        else:
            add("C4", p, "-", "-", "SKRU1", "", "UNKNOWN", "нет evidence", [])
    for i, r in enumerate(out, 1):
        r["prior_id"] = f"WPR-{i:04d}"
    cols = ["prior_id", "module", "parameter", "target", "branch_or_hypothesis", "site_group", "si_unit",
            "world_status", "proposed_min", "proposed_max", "rule", "full_min", "full_max", "n_values", "n_sources",
            "n_original", "only_cited", "adjudicated", "owner_choice_needed", "note", "sources", "evidence_ids"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, lineterminator="\n", extrasaction="ignore")
    w.writeheader()
    for r in out:
        w.writerow({c: r.get(c, "") for c in cols})
    data = buf.getvalue().encode("utf-8")
    counts = defaultdict(int)
    for r in out:
        counts[f"{r['module']}:{r['world_status']}"] += 1
    receipt = {"rule_version": RULE_VERSION,
               "inputs": {k: hashlib.sha256((wp / k).read_bytes()).hexdigest() for k in
                          ("world_passport.csv", "passport_evidence.csv", "transfer_branches.csv") if (wp / k).is_file()},
               "output_sha256": hashlib.sha256(data).hexdigest(), "rows": len(out), "by_status": dict(sorted(counts.items())),
               "owner_choice_needed": sum(1 for r in out if r["owner_choice_needed"] == "Y")}
    return {"world_priors_proposal.csv": data,
            "world_priors_proposal_receipt.json": (json.dumps(receipt, ensure_ascii=False, indent=1) + "\n").encode()}


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
