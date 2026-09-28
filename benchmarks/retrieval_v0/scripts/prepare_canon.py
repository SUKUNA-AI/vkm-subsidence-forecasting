"""V0 step 1: canary canon → units (vkm-units-v1) → docs.jsonl for RX580 encoding; qrels coverage on the canon."""
import json
import os
from collections import Counter
from pathlib import Path

from vkm_corpus.retrieval_lab import bench as B
from vkm_corpus.retrieval_lab.canon import CanonReader
from vkm_corpus.retrieval_lab.units import UnitConfig, build_units, units_summary

V0 = Path(os.environ["J_V0"])
reader = CanonReader.from_duckdb(V0 / "vkm_corpus_canary.duckdb")
snap = reader.snapshot()
rows = reader.load_all()
units = build_units(rows["pages"], rows["blocks"], rows["figures"], rows["tables"], rows["formulas"],
                    rows["bibliography"], UnitConfig(with_pages=True))
main = [u for u in units if u.kind != "PAGE"]
pages_units = [u for u in units if u.kind == "PAGE"]
summary = units_summary(main)
with open(V0 / "units.jsonl", "w", encoding="utf-8") as f:
    for u in units:
        f.write(json.dumps({"unit_id": u.unit_id, "kind": u.kind, "source_id": u.source_id, "page_id": u.page_id,
                            "object_ids": list(u.object_ids), "text": u.text, "section": u.section_title,
                            "prev": u.prev_text, "next": u.next_text, "image": u.image_artifact_id,
                            "flags": list(u.flags)}, ensure_ascii=False) + "\n")
with open(V0 / "docs.jsonl", "w", encoding="utf-8") as f:
    for u in main:
        f.write(json.dumps({"object_id": u.unit_id, "text": u.text, "source_id": u.source_id, "page_id": u.page_id,
                            "object_type": u.kind}, ensure_ascii=False) + "\n")
canon_pages = {r["page_id"] for r in rows["pages"]}
page_chars = {r["page_id"]: len(r["normalized_text"] or "") for r in rows["pages"]}
bench = B.load_benchmark("benchmarks/retrieval_v0")
judged = bench.judgments(level="PAGE")
cov = {"text": Counter(), "visual": Counter()}
covered = []
for q in bench.queries:
    j = judged.get(q.query_id, {})
    inside = {p: g for p, g in j.items() if p in canon_pages}
    has = any(g >= 2 for g in inside.values())
    cov[q.track]["with_rel_in_canon" if has else "no_rel_in_canon"] += 1
    if has:
        covered.append(q.query_id)
missing_pages = sorted({p for j in judged.values() for p in j if p.split(":")[0] in {r["source_id"] for r in rows["pages"]}
                        and p not in canon_pages})
empty_rel_pages = sorted({p for j in judged.values() for p, g in j.items() if g >= 2 and p in canon_pages
                          and page_chars.get(p, 0) < 40})
out = {"snapshot": snap, "sources": sorted({r["source_id"] for r in rows["pages"]}), "pages": len(canon_pages),
       "objects": {k: len(v) for k, v in rows.items()}, "units_main": summary, "page_units": len(pages_units),
       "coverage": {k: dict(v) for k, v in cov.items()}, "covered_queries": covered,
       "qrel_pages_missing_in_canon_sources": missing_pages[:50], "n_missing": len(missing_pages),
       "relevant_pages_with_empty_text": empty_rel_pages[:50], "n_empty_rel": len(empty_rel_pages)}
(V0 / "prepare.json").write_text(json.dumps(out, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
print(json.dumps({k: v for k, v in out.items() if k not in ("covered_queries",)}, indent=1, ensure_ascii=False,
                 default=str)[:6000])
