"""V1 step 1: the canon of the CURRENT snapshot (DuckDB copy) → units ``vkm-units-v1/A`` (exactly as the production
export ``vkm_corpus.search.vectors.export_units_at``) and the stage-2 units export of the dense index → unit tables of
the run, their difference, page table and qrels coverage.

Environment: ``J_V1`` — work dir with ``core/duckdb/vkm_corpus.duckdb`` (copy of CORE's DuckDB of CURRENT) and
``core/units/<snapshot>/vkm-units-v1-A/`` (production units exports). Writes (all under ``$J_V1``, git-ignored):

* ``units_final.jsonl`` — units of the CURRENT snapshot (incl. ``BIB_ENTRY``) with text;
* ``units_dense.jsonl`` — units of the stage-2 export the production dense index was built from (with text);
* ``pages.json`` — page table (source, index, chars, printed label) and source titles (for judging only);
* ``prepare.json`` — snapshot ids, counts by kind, the unit difference (same / changed / new / gone), coverage.
"""
import hashlib
import json
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from vkm_corpus.retrieval_lab import bench as B
from vkm_corpus.retrieval_lab.canon import CanonReader
from vkm_corpus.retrieval_lab.units import UNIT_RULE, UnitConfig, build_units, render

V1 = Path(os.environ["J_V1"])
BENCH_DIR = Path("benchmarks/retrieval_v0")
DENSE_UNITS_SNAPSHOT = os.environ.get("J_DENSE_UNITS_SNAPSHOT", "snap-20260928T113713Z-fa0aa127")


def jline(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def main() -> None:
    t0 = time.time()
    reader = CanonReader.from_duckdb(V1 / "core" / "duckdb" / "vkm_corpus.duckdb")
    snap = reader.snapshot()
    meta = reader.source_meta()
    labels = reader.page_labels()
    cfg = UnitConfig()
    units_final = []
    pages = {}
    n_obj = Counter()
    sids = reader.source_ids()
    for i in range(0, len(sids), 20):                     # same batching as export_units_at
        data = reader.load_all(sids[i:i + 20])
        for k, v in data.items():
            n_obj[k] += len(v)
        for p in data["pages"]:
            pages[p["page_id"]] = {"source_id": p["source_id"], "page_index": p["page_index"],
                                   "chars": p["char_count"], "label": labels.get(p["page_id"])}
        units = build_units(data["pages"], data["blocks"], data["figures"], data["tables"], data["formulas"],
                            data["bibliography"], cfg)
        for u in units:
            text = render(u, "A", meta.get(u.source_id), labels.get(u.page_id or ""))
            units_final.append({"unit_id": u.unit_id, "kind": u.kind, "source_id": u.source_id, "page_id": u.page_id,
                                "object_ids": list(u.object_ids), "part": u.part, "flags": list(u.flags),
                                "text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(), "text": text})
    reader.close()
    ids = [u["unit_id"] for u in units_final]
    if len(set(ids)) != len(ids):
        sys.exit("duplicate unit ids")
    with open(V1 / "units_final.jsonl", "w", encoding="utf-8", newline="\n") as f:
        for u in units_final:
            f.write(jline(u))
    # stage-2 export (dense index units) with text from its docs.jsonl
    ud = V1 / "core" / "units" / DENSE_UNITS_SNAPSHOT / f"{UNIT_RULE}-A"
    man = json.loads((ud / "units.json").read_text(encoding="utf-8"))
    text_of = {}
    with open(ud / "docs.jsonl", encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            text_of[r["object_id"]] = r["text"]
    units_dense = []
    with open(ud / "units.jsonl", encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            units_dense.append({"unit_id": r["unit_id"], "kind": r["kind"], "source_id": r["source_id"],
                                "page_id": r["page_id"], "object_ids": r["object_ids"], "part": r["part"], "flags": [],
                                "text_hash": r["text_hash"], "text": text_of[r["unit_id"]]})
    with open(V1 / "units_dense.jsonl", "w", encoding="utf-8", newline="\n") as f:
        for u in units_dense:
            f.write(jline(u))
    # difference final vs dense-index units
    dense_hash = {u["unit_id"]: u["text_hash"] for u in units_dense}
    final_hash = {u["unit_id"]: u["text_hash"] for u in units_final}
    diff = defaultdict(Counter)
    changed_pages = set()
    for u in units_final:
        h = dense_hash.get(u["unit_id"])
        state = "same" if h == u["text_hash"] else ("changed_text" if h else "new")
        diff[state][u["kind"]] += 1
        if state != "same" and u["kind"] != "BIB_ENTRY":
            changed_pages.add(u["page_id"])
    for u in units_dense:
        if u["unit_id"] not in final_hash:
            diff["gone"][u["kind"]] += 1
            changed_pages.add(u["page_id"])
    # the production export of the same snapshot (agent L's stage 3), if present, must be the same unit set
    prod_check = None
    pd = V1 / "core" / "units" / snap.get("snapshot_id", "?") / f"{UNIT_RULE}-A"
    if (pd / "units.jsonl").is_file():
        prod = {}
        with open(pd / "units.jsonl", encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                prod[r["unit_id"]] = r["text_hash"]
        prod_check = {"production_units": len(prod), "lab_units": len(final_hash),
                      "same_ids": len(set(prod) & set(final_hash)),
                      "text_hash_mismatch": sum(1 for k, h in final_hash.items() if k in prod and prod[k] != h),
                      "only_lab": len(set(final_hash) - set(prod)), "only_production": len(set(prod) - set(final_hash)),
                      "identical": prod == final_hash}
    # qrels coverage on the canon
    bench = B.load_benchmark(BENCH_DIR)
    judged = bench.judgments(level="PAGE")
    cand = bench.judgments(level="PAGE", statuses=("CANDIDATE", "NEEDS_REVIEW"))
    cov = {"text": Counter(), "visual": Counter()}
    covered, not_covered = [], {}
    for q in bench.queries:
        j = judged.get(q.query_id, {})
        inside = {p: g for p, g in j.items() if p in pages}
        if any(g >= 2 for g in inside.values()):
            cov[q.track]["covered"] += 1
            covered.append(q.query_id)
        else:
            cov[q.track]["not_covered"] += 1
            outside = sorted(p for p, g in j.items() if g >= 2 and p not in pages)
            not_covered[q.query_id] = {"verified_rel_outside_canon": outside,
                                       "candidate_rel": sorted(p for p, g in cand.get(q.query_id, {}).items() if g >= 2)}
    missing = sorted({p for j in judged.values() for p in j if p not in pages})
    titles = {sid: {"title": m.title, "authors": m.authors, "year": m.year, "work_id": m.work_id}
              for sid, m in meta.items()}
    (V1 / "pages.json").write_text(json.dumps({"pages": pages, "sources": titles}, ensure_ascii=False),
                                   encoding="utf-8")
    out = {"snapshot": snap, "dense_units_snapshot": man["snapshot_id"], "dense_units_sha256": man["units_sha256"],
           "unit_rule": UNIT_RULE, "unit_config": cfg.as_dict(), "objects": dict(n_obj), "pages": len(pages),
           "sources": len(sids),
           "units_final": dict(Counter(u["kind"] for u in units_final)), "units_final_total": len(units_final),
           "units_dense": dict(Counter(u["kind"] for u in units_dense)), "units_dense_total": len(units_dense),
           "diff_final_vs_dense": {k: dict(v) for k, v in diff.items()}, "pages_with_changed_units": len(changed_pages),
           "production_export_check": prod_check,
           "coverage": {k: dict(v) for k, v in cov.items()}, "covered_queries": covered,
           "not_covered": not_covered, "qrel_pages_missing_in_canon": missing, "seconds": round(time.time() - t0, 1)}
    (V1 / "prepare.json").write_text(json.dumps(out, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k not in ("covered_queries",)}, indent=1, ensure_ascii=False,
                     default=str)[:8000])


if __name__ == "__main__":
    main()
