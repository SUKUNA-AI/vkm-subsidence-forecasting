#!/usr/bin/env python3
"""Review file (``WORLD_EXTRACTION/adjudication/*.jsonl``) from the result of the economical page-image review
workflow (11.10.2026): one verdict per record from the first reviewer; a second, independent check only for the
verdicts that change a value / unit / header multiplier / mine / rock class, reject a number or are LOW; an arbiter
only where the second check disagrees. The arbiter's decision, when there is one, replaces the first verdict.

Workflow result: ``{"first": [{"group", "source", "first": [verdict…]}], "rechecks": [{"records": [{"record_id",
"group", "recheck_reason"}], "check": [{"record_id", "agrees", "field", "proposed", "confidence", "basis"}] | null,
"judged": [{"record_id", "final_verdict", "corrected_json", …, "keeps_first_verdict"}], "lost"}], "lost_groups"}``;
the record pages and flags come from the group files (``extract_review_groups.py``).

Output line (the format of ``adjudication_20261010_plausibility.jsonl``): ``record_id``, ``page_id``, ``item_kind``
(``PLAUSIBILITY:<why>``), ``verdict``, ``corrected`` (numbers as numbers), ``material_class``, ``value_scale_factor``,
``checked_on``, ``basis``, ``confidence``, ``branch_hint``, ``round2`` (``recheck_reason``, ``second_agrees``,
``judge``: NOT_NEEDED / CHANGED / KEPT / LOST), ``second_checked``. ``--items-out`` writes the records in the batch
format of ``extract_review_check.py`` for the self-check. ``--overrides`` (``{record_id: {"set": {field: value},
"drop": [field], "why": "…"}}``) applies documented manual fixes after the self-check (e.g. «21,25 ± 2,00» kept as the
printed mean, not as a computed interval); each fix is recorded in ``round2.manual_override``.

Usage:  python scripts/extract_adjudication_from_workflow.py --result <workflow_result.json> --groups <dir>
        --out <adjudication_YYYYMMDD_<name>.jsonl> [--items-out <items.jsonl>] [--overrides <fixes.json>]
        [--receipt <receipt.json>]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

NUMERIC = ("value_min", "value_max")
SUP = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻", "0123456789-")


def number(x) -> float | None:
    """«2,26» / «−0.5» / «1·10⁻³» / «1e3» / 1000 → float; None when it is not a number."""
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        return float(x)
    s = str(x or "").strip().replace("−", "-").replace(" ", "").replace(" ", "").replace(",", ".")
    if not s:
        return None
    m = re.fullmatch(r"([-+]?\d*\.?\d+)?(?:[x×*·]?10\^?\(?([-+]?\d+)\)?)?", s.translate(SUP))
    if m and (m.group(1) or m.group(2)):
        base = float(m.group(1)) if m.group(1) else 1.0
        return base * (10.0 ** int(m.group(2)) if m.group(2) else 1.0)
    try:
        return float(s)
    except ValueError:
        return None


def parse_corrected(raw: str, problems: list[str], rid: str) -> dict:
    try:
        c = json.loads(raw or "{}")
    except ValueError:
        problems.append(f"{rid}: corrected_json is not JSON")
        return {}
    if not isinstance(c, dict):
        problems.append(f"{rid}: corrected_json is not an object")
        return {}
    for k in NUMERIC:
        if k in c and c[k] not in (None, ""):
            v = number(c[k])
            if v is None:
                problems.append(f"{rid}: {k} {c[k]!r} is not a number")
            else:
                c[k] = v
    return c


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--result", required=True, type=Path)
    ap.add_argument("--groups", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--items-out", type=Path)
    ap.add_argument("--overrides", type=Path)
    ap.add_argument("--receipt", type=Path)
    a = ap.parse_args()
    res = json.loads(a.result.read_text(encoding="utf-8"))
    overrides = json.loads(a.overrides.read_text(encoding="utf-8")) if a.overrides else {}
    recs = {}
    for g in sorted(a.groups.glob("group_*.json")):
        d = json.loads(g.read_text(encoding="utf-8"))
        for p in d["pages"]:
            for it in p["items"]:
                recs[it["record_id"]] = dict(page_id=p["page_id"], packet_id=p["packet_id"], why=it.get("why") or [],
                                             record=it["record"])
    second, judged = {}, {}
    for ch in res.get("rechecks", []):
        checks = {c["record_id"]: c for c in (ch.get("check") or [])}
        for r in ch["records"]:
            second[r["record_id"]] = dict(reason=r["recheck_reason"], check=checks.get(r["record_id"]),
                                          lost=bool(ch.get("lost")))
        for j in ch.get("judged") or []:
            judged[j["record_id"]] = j
    problems, out, stats = [], [], {"first": 0, "second_checked": 0, "second_disagreed": 0, "judge_changed": 0,
                                    "judge_kept": 0, "judge_lost": 0, "second_lost": 0}
    for grp in res.get("first", []):
        for v in grp.get("first") or []:
            rid = v["record_id"]
            if rid not in recs:
                problems.append(f"{rid}: not in the groups")
                continue
            stats["first"] += 1
            base = recs[rid]
            s = second.get(rid)
            j = judged.get(rid)
            round2 = {"recheck_reason": "", "second_agrees": None, "judge": "NOT_NEEDED"}
            final = dict(verdict=v["verdict"], corrected=parse_corrected(v.get("corrected_json"), problems, rid),
                         material_class=v.get("material_class") or None,
                         value_scale_factor=v.get("value_scale_factor") or None,
                         branch_hint=v.get("branch_hint") or "", confidence=v["confidence"], basis=v["basis"])
            if s:
                round2["recheck_reason"] = s["reason"]
                c = s["check"]
                if c is None:
                    stats["second_lost"] += 1
                    round2["judge"] = "SECOND_CHECK_LOST"
                else:
                    stats["second_checked"] += 1
                    round2["second_agrees"] = bool(c["agrees"])
                    if not c["agrees"]:
                        stats["second_disagreed"] += 1
                        round2["second_field"] = c.get("field") or ""
                        if j is None:
                            stats["judge_lost"] += 1
                            round2["judge"] = "LOST"
                            final["confidence"] = "LOW"
                        elif j.get("keeps_first_verdict"):
                            stats["judge_kept"] += 1
                            round2["judge"] = "KEPT"
                        else:
                            stats["judge_changed"] += 1
                            round2["judge"] = "CHANGED"
                        if j is not None:
                            final.update(verdict=j["final_verdict"],
                                         corrected=parse_corrected(j.get("corrected_json"), problems, rid),
                                         material_class=j.get("material_class") or final["material_class"],
                                         value_scale_factor=j.get("value_scale_factor") or None,
                                         branch_hint=j.get("branch_hint") or final["branch_hint"],
                                         confidence=j["confidence"], basis=f"[арбитр] {j['basis']}")
            f = final["value_scale_factor"]
            if f is not None:
                fv = number(f)
                if fv is None:
                    problems.append(f"{rid}: value_scale_factor {f!r} is not a number")
                final["value_scale_factor"] = None if fv in (None, 1.0) else f"{fv:g}"
            o = overrides.get(rid)
            if o:
                for k in o.get("drop", []):
                    final["corrected"].pop(k, None)
                final["corrected"].update(o.get("set", {}))
                round2["manual_override"] = o["why"]
            out.append(dict(record_id=rid, page_id=base["page_id"],
                            item_kind="PLAUSIBILITY:" + ";".join(base["why"]), verdict=final["verdict"],
                            corrected=final["corrected"], material_class=final["material_class"],
                            value_scale_factor=final["value_scale_factor"], checked_on=v.get("checked_on") or "",
                            basis=final["basis"], confidence=final["confidence"], branch_hint=final["branch_hint"],
                            round2=round2, second_checked=bool(s and s["check"] is not None)))
    unused = sorted(set(overrides) - {x["record_id"] for x in out})
    if unused:
        problems.append(f"overrides for unknown records: {unused[:5]}")
    missing = sorted(set(recs) - {x["record_id"] for x in out})
    if missing:
        problems.append(f"{len(missing)} records without a verdict, e.g. {missing[:5]}")
    a.out.parent.mkdir(parents=True, exist_ok=True)
    with open(a.out, "w", encoding="utf-8", newline="\n") as fh:
        for x in out:
            fh.write(json.dumps(x, ensure_ascii=False) + "\n")
    if a.items_out:
        pages = {}
        for rid, b in recs.items():
            row = pages.setdefault(b["page_id"], {"page_id": b["page_id"], "packet_id": b["packet_id"], "items": []})
            row["items"].append({"kind": "PLAUSIBILITY", "records": [dict(b["record"], record_id=rid)]})
        with open(a.items_out, "w", encoding="utf-8", newline="\n") as fh:
            for row in pages.values():
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    verdicts = {}
    for x in out:
        verdicts[x["verdict"]] = verdicts.get(x["verdict"], 0) + 1
    summary = {"rule_version": "extract_adjudication_from_workflow_v1", "records": len(out), "verdicts": verdicts,
               "stats": stats, "lost_groups": res.get("lost_groups", []), "missing": len(missing),
               "problems": problems[:80],
               "manual_overrides": len(overrides),
               "inputs": {"result_sha256": hashlib.sha256(a.result.read_bytes()).hexdigest(),
                          "overrides_sha256": hashlib.sha256(a.overrides.read_bytes()).hexdigest() if a.overrides
                          else "",
                          "groups": len(list(a.groups.glob("group_*.json")))},
               "output_sha256": hashlib.sha256(a.out.read_bytes()).hexdigest()}
    if a.receipt:
        a.receipt.write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
