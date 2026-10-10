#!/usr/bin/env python3
"""Borehole picks of the page review (10.10.2026) → curated table read by the world passport (C1).

Input: the result of the borehole-picks workflow (JSON: a list of groups, or ``{"result": [...]}``; each group holds
``picks`` after extraction, an independent image check and, on disputes, a judge). One output row per pick with a
stable id; total depth and collar elevation are moved to their own columns when a reader put them into the depth
columns. Picks are AUTO_EXTRACTED_UNREVIEWED: verified by agents on the page image, not by a person.

Output: ``WORLD_PARAMETERS/curated/borehole_picks_<date>.csv`` and ``borehole_picks_<date>_receipt.json``.

Usage:  VKM_RESOURCES_ROOT=<PRIVATE> python scripts/build_borehole_picks_review.py --result <workflow.json>
        --date 20261010
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import sys
from collections import Counter
from pathlib import Path

COLS = ["pick_id", "pick_key", "group", "page_id", "source_id", "borehole_as_printed", "borehole_id_catalogue",
        "site_as_printed", "site_norm", "unit_as_printed", "unit_canonical", "pick_kind", "top_depth_m",
        "bottom_depth_m", "top_abs_m", "bottom_abs_m", "thickness_m", "collar_elevation_m", "total_depth_m", "x", "y",
        "crs_as_printed", "value_as_printed", "read_from", "status", "scale", "confidence", "verification", "locator",
        "quote", "notes"]


def _num(v) -> str:
    return "" if v is None or v == "" else f"{float(v):.6g}"


def rows_of(groups: list[dict]) -> list[dict]:
    out, seen = [], set()
    for g in sorted((g for g in groups if g), key=lambda g: g.get("group", 0)):
        verification = ("ADJUDICATED_JUDGE" if g.get("judged") else "VERIFIED_IMAGE_CHECK") if not g.get("lost") \
            else "EXTRACTED_UNVERIFIED"
        for p in g.get("picks") or []:
            key = (p.get("page_id"), p.get("borehole_as_printed"), p.get("unit_as_printed"), p.get("pick_kind"),
                   p.get("top_depth_m"), p.get("bottom_depth_m"), p.get("top_abs_m"), p.get("thickness_m"))
            if key in seen:
                continue
            seen.add(key)
            r = {c: "" for c in COLS}
            r.update({k: ("" if p.get(k) is None else str(p.get(k))) for k in COLS if k in p})
            for c in ("top_depth_m", "bottom_depth_m", "top_abs_m", "bottom_abs_m", "thickness_m", "collar_elevation_m",
                      "x", "y"):
                r[c] = _num(p.get(c))
            if p.get("pick_kind") == "TOTAL_DEPTH" and not p.get("total_depth_m"):
                r["total_depth_m"], r["bottom_depth_m"] = r["bottom_depth_m"] or r["top_depth_m"], ""
                r["top_depth_m"] = "" if r["total_depth_m"] == r["top_depth_m"] else r["top_depth_m"]
            if p.get("pick_kind") == "COLLAR_ELEVATION" and not r["collar_elevation_m"]:
                r["collar_elevation_m"], r["top_abs_m"] = r["top_abs_m"], ""
            r.update(group=str(g.get("group", "")), source_id=(p.get("page_id") or "").rpartition(":")[0],
                     scale="FIELD" if p.get("status") != "INTERPOLATION" else "MODEL", verification=verification)
            out.append(r)
    for i, r in enumerate(out, 1):
        r["pick_id"] = f"BPR-{i:05d}"
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--result", required=True, type=Path)
    ap.add_argument("--date", required=True)
    a = ap.parse_args()
    res = os.environ.get("VKM_RESOURCES_ROOT")
    if not res:
        print("set VKM_RESOURCES_ROOT", file=sys.stderr)
        return 2
    raw = json.loads(a.result.read_text(encoding="utf-8"))
    groups = raw.get("result", raw) if isinstance(raw, dict) else raw
    rows = rows_of(groups)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLS, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    data = buf.getvalue().encode("utf-8")
    out = Path(res) / "11_evidence_vnext" / "canonical" / "WORLD_PARAMETERS" / "curated"
    (out / f"borehole_picks_{a.date}.csv").write_bytes(data)
    receipt = {"input_sha256": hashlib.sha256(a.result.read_bytes()).hexdigest(),
               "output_sha256": hashlib.sha256(data).hexdigest(), "groups": len(groups), "picks": len(rows),
               "by_site": dict(Counter(r["site_norm"] or "-" for r in rows).most_common()),
               "by_kind": dict(Counter(r["pick_kind"] for r in rows).most_common()),
               "by_verification": dict(Counter(r["verification"] for r in rows).most_common()),
               "linked_to_catalogue": sum(1 for r in rows if r["borehole_id_catalogue"]),
               "pages_without_boreholes": sum(len(g.get("pages_without_boreholes") or []) for g in groups if g)}
    (out / f"borehole_picks_{a.date}_receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=1) + "\n",
                                                               encoding="utf-8", newline="\n")
    print(json.dumps(receipt, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
