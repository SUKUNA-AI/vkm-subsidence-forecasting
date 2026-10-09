#!/usr/bin/env python3
"""World passport extraction, step 2c: verify the producer's answers against the page texts of their packets and
compare two runs over the same pages.

``--run R``: every record of ``sol/R/<packet>/answer.json`` (or ``double/<packet>.json`` for ``--run double``) is
checked by :func:`vkm_world.extraction.verify.check_record`; passing records go to ``accept/R/accepted.jsonl``,
failing ones to ``accept/R/rejected.jsonl`` with their reasons (nothing is deleted); ``accept/R/summary.json``
counts both. Every record stays AUTO_EXTRACTED_UNREVIEWED.

``--compare A B``: agreement of the accepted records of B against A on the packets both runs have, by number and by
attribution (``compare/A__B/``: summary, disagreements, records only in A / only in B).

Usage:  python scripts/extract_accept.py --work <dir> --run trial_high
        python scripts/extract_accept.py --work <dir> --compare trial_high trial_medium
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vkm_world.extraction import compare as C                 # noqa: E402
from vkm_world.extraction import verify as V                  # noqa: E402

RULE_VERSION = "extract_accept_v1"


def answer_path(work: Path, run: str, pid: str) -> Path:
    return work / "double" / f"{pid}.json" if run == "double" else work / "sol" / run / pid / "answer.json"


def packets_of(work: Path, run: str) -> list[str]:
    mans = [json.loads(x)["packet_id"] for x in open(work / "packets.jsonl", encoding="utf-8")]
    return [p for p in mans if answer_path(work, run, p).is_file()
            and (run == "double" or (work / "sol" / run / p / "DONE").is_file())]


def verify_run(work: Path, run: str) -> dict:
    out = work / "accept" / run
    out.mkdir(parents=True, exist_ok=True)
    acc, rej = [], []
    reasons, kinds = Counter(), Counter()
    for pid in packets_of(work, run):
        pk = json.loads((work / "packets" / f"{pid}.json").read_text(encoding="utf-8"))
        pages = {p: V.PageText(p, t) for p, t in pk["page_texts"].items()}
        try:
            ans = json.loads(answer_path(work, run, pid).read_text(encoding="utf-8"))
        except ValueError as e:
            rej.append({"record_id": f"{run}:{pid}:ANSWER", "packet_id": pid, "reasons": [f"ANSWER_NOT_JSON:{e}"]})
            continue
        meta_p = work / "sol" / run / pid / "meta.json"
        producer = json.loads(meta_p.read_text(encoding="utf-8"))["producer"] if meta_p.is_file() else \
            {"producer": "claude-subagent double entry"}
        for i, r in enumerate(ans.get("records", []), 1):
            rr = dict(r)
            rr.update(record_id=f"{run}:{pid}:{i:03d}", run=run, packet_id=pid,
                      producer=producer, review_status="AUTO_EXTRACTED_UNREVIEWED")
            why = V.check_record(r, pages)
            kinds[r.get("kind")] += 1
            if why:
                rr["reasons"] = why
                rej.append(rr)
                for w in why:
                    reasons[w.split(":")[0]] += 1
            else:
                rr["warnings"] = V.warnings_for(r)
                acc.append(rr)
    for name, rows in (("accepted.jsonl", acc), ("rejected.jsonl", rej)):
        with open(out / name, "w", encoding="utf-8", newline="\n") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    summ = {"rule_version": RULE_VERSION, "run": run, "packets": len(packets_of(work, run)),
            "records": len(acc) + len(rej), "accepted": len(acc), "rejected": len(rej),
            "reject_reasons": dict(reasons.most_common()), "kinds": dict(kinds.most_common()),
            "accepted_by_kind": dict(Counter(r.get("kind") for r in acc).most_common()),
            "accepted_by_scale": dict(Counter(r.get("scale") for r in acc).most_common()),
            "accepted_by_site": dict(Counter(r.get("site_norm") for r in acc).most_common())}
    (out / "summary.json").write_text(json.dumps(summ, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    return summ


def check_file(work: Path, pid: str, path: Path) -> dict:
    """Self-check of one answer file (for the double entry): reasons per failing record."""
    pk = json.loads((work / "packets" / f"{pid}.json").read_text(encoding="utf-8"))
    pages = {p: V.PageText(p, t) for p, t in pk["page_texts"].items()}
    ans = json.loads(path.read_text(encoding="utf-8"))
    bad = []
    for i, r in enumerate(ans.get("records", []), 1):
        why = V.check_record(r, pages)
        if why:
            bad.append({"record_no": i, "page_id": r.get("page_id"), "value_as_printed": r.get("value_as_printed"),
                        "unit_as_printed": r.get("unit_as_printed"), "reasons": why})
    return {"packet_id": pid, "records": len(ans.get("records", [])), "failing": len(bad), "details": bad}


def _load(path: Path) -> list[dict]:
    return [json.loads(x) for x in open(path, encoding="utf-8")] if path.is_file() else []


def compare_runs(work: Path, a: str, b: str) -> dict:
    pages = {}
    for line in open(work / "packets.jsonl", encoding="utf-8"):
        m = json.loads(line)
        pages[m["packet_id"]] = m["page_ids"]
    for d in (work / "packets").glob("*.json"):            # packets of earlier builds kept on disk (pinned)
        if d.stem not in pages:
            pages[d.stem] = json.loads(d.read_text(encoding="utf-8"))["manifest"]["page_ids"]

    def pages_done(run):
        return {pg for pid in packets_of(work, run) for pg in pages.get(pid, ())}
    common_pages = pages_done(a) & pages_done(b)            # pages, so a packet split for one run still compares
    common = sorted({pid for pid in set(packets_of(work, a)) | set(packets_of(work, b))
                     if set(pages.get(pid, ())) & common_pages})
    ra = [r for r in _load(work / "accept" / a / "accepted.jsonl") if r["page_id"] in common_pages]
    rb = [r for r in _load(work / "accept" / b / "accepted.jsonl") if r["page_id"] in common_pages]
    out = work / "compare" / f"{a}__{b}"
    out.mkdir(parents=True, exist_ok=True)
    pairs, only_a, only_b = C.match(ra, rb)
    summ = C.agreement(ra, rb)
    summ.update(rule_version=RULE_VERSION, run_a=a, run_b=b, packets=common, n_common_pages=len(common_pages))
    cols = ["page_id", "kind", "parameter_code", "value_as_printed", "unit_as_printed", "material_as_printed",
            "scale", "site_norm", "conditions", "record_id"]
    with open(out / "disagreements.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["fields"] + [f"a_{c}" for c in cols] + [f"b_{c}" for c in cols])
        for r, s in pairs:
            d = C.attribution_diff(r, s)
            if d:
                w.writerow([";".join(d)] + [r.get(c) for c in cols] + [s.get(c) for c in cols])
    for name, rows in (("only_a.csv", only_a), ("only_b.csv", only_b)):
        with open(out / name, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(cols + ["quote"])
            for r in rows:
                w.writerow([r.get(c) for c in cols] + [r.get("quote")])
    (out / "summary.json").write_text(json.dumps(summ, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    return summ


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--work", required=True, type=Path)
    ap.add_argument("--run")
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"))
    ap.add_argument("--check", nargs=2, metavar=("PACKET_ID", "ANSWER_JSON"))
    a = ap.parse_args()
    if a.check:
        print(json.dumps(check_file(a.work, a.check[0], Path(a.check[1])), ensure_ascii=False, indent=1))
    if a.run:
        print(json.dumps(verify_run(a.work, a.run), ensure_ascii=False, indent=1))
    if a.compare:
        print(json.dumps(compare_runs(a.work, *a.compare), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
