"""GRAPH_SEARCH_V1 stage A (NAV venv with pymorphy3): G3's other wordings of every query, computed by the served code
(``NavGraphSignals.expansions`` → ``NavStore.run("expand_query")``), with and without the narrower term, and the
NAV-side latency of the stages (PREREGISTRATION §9): the lookup build and ``expand_query`` per query.

Environment: ``GS_WORK`` (writes ``stage_a.json``), ``GS_NAV_ROOT`` (a data root with
``derived/navigation/<snapshot>/nav.duckdb`` + ``CURRENT`` and ``duckdb/vkm_corpus.duckdb``).
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
from dataclasses import replace
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from vkm_corpus.navigation import store as nav_store  # noqa: E402
from vkm_corpus.retrieval_lab import bench as B  # noqa: E402
from vkm_corpus.retrieval_lab import topic_bench as TB  # noqa: E402
from vkm_corpus.search import graph_stages as G  # noqa: E402

WORK = Path(os.environ["GS_WORK"])
ROOT = Path(os.environ["GS_NAV_ROOT"])


def query_texts() -> list[str]:
    splits = json.loads((REPO / "benchmarks/graph_search_v1/splits.json").read_text(encoding="utf-8"))
    bench = B.load_benchmark(REPO / "benchmarks/retrieval_v0")
    texts = [q.text for q in bench.queries if q.query_id in splits["retrieval_v1"]]
    texts += [q.text for t in TB.load_set(REPO / "benchmarks/topic_v1/topic_set_v1.jsonl") for q in t.queries]
    return list(dict.fromkeys(texts))


def main() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    nav = nav_store.NavStore(ROOT, canonical_db=ROOT / "duckdb" / "vkm_corpus.duckdb")
    sig = G.NavGraphSignals(nav)
    t0 = time.perf_counter()
    maps = sig.view()
    view_ms = round((time.perf_counter() - t0) * 1000, 1)
    texts = query_texts()
    base = G.GraphParams()
    out: dict = {"snapshot_id": nav.snapshot_id(), "view_build_ms": view_ms, "parts": maps.parts,
                 "expansions": {"narrower": {}, "plain": {}}, "latency_ms": {}}
    sig.expansions(texts[0], base)                                  # warm the morphology (dictionary load)
    for name, params in (("narrower", replace(base, concepts_narrower=True)),
                         ("plain", replace(base, concepts_narrower=False))):
        lat = []
        for text in texts:
            t1 = time.perf_counter()
            out["expansions"][name][text] = sig.expansions(text, params)
            lat.append((time.perf_counter() - t1) * 1000)
        out["latency_ms"][f"expand_query_{name}"] = {"p50": round(statistics.median(lat), 2),
                                                      "p90": round(statistics.quantiles(lat, n=10)[-1], 2),
                                                      "n": len(lat)}
    out["counts"] = {name: {"queries": len(v), "with_expansion": sum(1 for r in v.values() if r["expansions"]),
                            "kinds": {k: sum(1 for r in v.values() for x in r["expansions"] if x["kind"] == k)
                                      for k in ("equivalents", "narrower")}}
                     for name, v in out["expansions"].items()}
    (WORK / "stage_a.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: out[k] for k in ("snapshot_id", "view_build_ms", "parts", "latency_ms", "counts")},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
