#!/usr/bin/env python3
"""One-time seed of the id ledgers of the world passport (12.10.2026): rule v1 numbered WPV / WPP / WPR by position,
so adding a row renumbered every later id. Rule v2 keeps issued ids in ledgers (``evidence_id_ledger.csv`` — WPV,
``passport_id_ledger.csv`` — WPP, ``priors_id_ledger.csv`` — WPR). This script binds the ids already issued by v1 to the natural keys of v2,
so no existing reference changes.

WPV: the v1 positional order is reproduced on the same inputs (rows of the current code, sorted by the v1 key with
the v1 record id of the creep-law rows) and checked row by row against the v1 ``passport_evidence.csv`` (producer,
record id, parameter, page, value as printed, SI min / max). Any mismatch stops the seed: nothing is written.
WPP / WPR: the v1 tables give id → key in the v1 key format; the v2 build inherits an id when its group maps 1:1 and
retires it with the named successors when it is split.

Usage:  VKM_RESOURCES_ROOT=<PRIVATE> python scripts/seed_world_id_ledger.py --runs high_full wave2_high double
        [--dry-run]
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import io
import json
import os
import sys
from pathlib import Path

csv.field_size_limit(sys.maxsize)
ROOT = Path(__file__).resolve().parents[1]
V1 = "world_passport_v1"
V1_PRIORS = "world_priors_v1"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rows(p: Path) -> list[dict]:
    return list(csv.DictReader(open(p, encoding="utf-8")))


def _csv(rows: list[dict], cols: list[str]) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, lineterminator="\n", extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({c: r.get(c, "") for c in cols})
    return buf.getvalue().encode("utf-8")


def seed(canon: Path, runs: list[str]) -> tuple[dict[str, bytes], dict]:
    wpp = _load("build_world_passport")
    PR = wpp.PR
    out_dir = canon / "WORLD_PASSPORT"
    old_ev = _rows(out_dir / "passport_evidence.csv")
    old_reg = _rows(out_dir / "world_passport.csv")
    old_pri = _rows(out_dir / "world_priors_proposal.csv")
    wpp._READ.clear()
    wpp._CANON[:] = [canon]
    units = wpp.Units(wpp._rows(canon / "GEOLOGY_COORDS" / "stratigraphic_units.csv"))
    ev = (wpp.phase1_rows(canon) + wpp.curated_rows(canon) + wpp.skru1_rows(canon, units) + wpp.calendar_rows(canon)
          + wpp.borehole_rows(canon, units) + wpp.extraction_rows(canon, runs, units))
    prod_order = {"PHASE1_CATALOGUE": 0, "CURATED_SKRU1_20261008": 1, "CURATED_C3_20261009": 2,
                  "CURATED_LAWS_20261009": 3, "CURATED_C4_CALENDAR_20261010": 4, "PHASE1_BOREHOLE_PICKS": 5}
    v1rec = lambda x: x.get("_legacy_record_id") or x["producer_record_id"]                 # noqa: E731
    ev.sort(key=lambda x: (x["module"], x["parameter"], x["target"], prod_order.get(x["producer"], 5), x["producer"],
                           x["source_id"], x["page_id"], v1rec(x)))
    if len(ev) != len(old_ev):
        raise SystemExit(f"row count differs: v2 rows {len(ev)}, v1 table {len(old_ev)} — not seeded")
    fields = ("producer", "parameter", "page_id", "value_as_printed", "si_min", "si_max")
    bad = []
    for i, (x, o) in enumerate(zip(ev, old_ev), 1):
        if o["ev_id"] != f"WPV-{i:06d}" or v1rec(x) != o["producer_record_id"] or any(
                (x.get(f) or "") != (o.get(f) or "") for f in fields):
            bad.append((i, o["ev_id"]))
    if bad:
        raise SystemExit(f"v1 order not reproduced for {len(bad)} rows, first {bad[:5]} — not seeded")
    keys = [wpp.evidence_key(x) for x in ev]
    if len(set(keys)) != len(keys):
        raise SystemExit("v2 evidence keys are not unique — not seeded")
    ledger = [{"id": o["ev_id"], "kind": "WPV", "natural_key": k, "state": "ACTIVE", "since_rule": V1}
              for o, k in zip(old_ev, keys)]
    ledger += [{"id": r["passport_id"], "kind": "WPP", "state": "ACTIVE", "since_rule": V1,
                "natural_key": "|".join((r["module"], r["parameter"], r["target"], r["site_group"], r["branch"]))}
               for r in old_reg]
    pri = [{"id": r["prior_id"], "kind": "WPR", "state": "ACTIVE", "since_rule": V1_PRIORS,
            "natural_key": "|".join((r["module"], r["parameter"], r["target"], r["branch_or_hypothesis"],
                                     r["site_group"]))} for r in old_pri]
    for kind, rows in (("WPP", ledger), ("WPR", pri)):
        ks = [r["natural_key"] for r in rows if r["kind"] == kind]
        if len(set(ks)) != len(ks):
            raise SystemExit(f"{kind}: v1 keys are not unique — not seeded")
    led = PR.IdLedger(ledger).table()
    pled = PR.IdLedger(pri).table()
    files = {"evidence_id_ledger.csv": _csv([r for r in led if r["kind"] == "WPV"], PR.LEDGER_COLS),
             "passport_id_ledger.csv": _csv([r for r in led if r["kind"] == "WPP"], PR.LEDGER_COLS),
             "priors_id_ledger.csv": _csv(pled, PR.LEDGER_COLS)}
    sha = lambda p: hashlib.sha256((out_dir / p).read_bytes()).hexdigest()               # noqa: E731
    receipt = {"rule": "seed_world_id_ledger_v1", "runs": runs,
               "v1_tables": {p: sha(p) for p in ("passport_evidence.csv", "world_passport.csv",
                                                 "world_priors_proposal.csv")},
               "inputs": dict(sorted(wpp._READ.items())),
               "check": {"wpv_rows_reproduced": len(ev), "wpv_mismatches": 0},
               "counts": {"WPV": len(old_ev), "WPP": len(old_reg), "WPR": len(old_pri)},
               "outputs": {k: hashlib.sha256(v).hexdigest() for k, v in files.items()}}
    files["id_ledger_seed_receipt.json"] = (json.dumps(receipt, ensure_ascii=False, indent=1) + "\n").encode("utf-8")
    return files, receipt


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--runs", nargs="+", default=["high_full", "double"])
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    res = os.environ.get("VKM_RESOURCES_ROOT")
    if not res:
        print("set VKM_RESOURCES_ROOT", file=sys.stderr)
        return 2
    canon = Path(res) / "11_evidence_vnext" / "canonical"
    out = canon / "WORLD_PASSPORT"
    if any((out / k).exists() for k in ("evidence_id_ledger.csv", "passport_id_ledger.csv", "priors_id_ledger.csv")):
        print("ledgers exist: the seed runs once", file=sys.stderr)
        return 1
    files, receipt = seed(canon, a.runs)
    if not a.dry_run:
        for k, v in files.items():
            (out / k).write_bytes(v)
    print(json.dumps({k: v for k, v in receipt.items() if k != "inputs"}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
