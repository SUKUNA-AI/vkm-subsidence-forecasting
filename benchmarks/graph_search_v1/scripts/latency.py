"""GRAPH_SEARCH_V1 latency (PREREGISTRATION §9): what the graph stages add, measured with the served code on the
WORKSTATION, per query of both sets, for every final system.

* ``stage_ms`` — the served stage functions of one request (``GraphRun``: gate, seed, window, the post fusion with
  G2/G5 legs, G1) over the P-E state, without the searches they trigger;
* ``bm25_ms`` — the lab BM25 page searches of the G3 wordings and of the G4 sources (stand-ins for the OpenSearch
  calls the service makes; the service runs the G3 ones in parallel from the start and the G4 one before late in
  ``window`` mode, during late in ``post`` mode);
* ``late_extra_targets`` — pages a ``window`` leg adds to the late window (CORE: V1 measured the late stage at 65 ms p50
  for 100 targets);
* ``expand_query`` (G3's NAV lookup) and the lookup build come from stage A.

Writes ``$GS_WORK/latency.json``. Environment: as ``run.py``.
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run as R  # noqa: E402
from vkm_corpus.search import graph_stages as G  # noqa: E402


def pct(xs: list[float]) -> dict[str, float]:
    xs = sorted(xs)
    return {"p50": round(statistics.median(xs), 3), "p90": round(xs[int(0.9 * (len(xs) - 1))], 3),
            "max": round(xs[-1], 3), "n": len(xs)}


def main() -> None:
    from engine import Engine

    sets = R.load_sets()
    a = json.loads((R.work() / "stage_a.json").read_text(encoding="utf-8"))
    maps = R.load_maps()
    eng = Engine(encoders=False)
    texts = sets["texts"]
    eng.encode(texts.values())
    finals = R.final_systems()
    systems = {**{n: R.VARIANTS[v] for n, v in finals.items() if v in R.VARIANTS}, **R.gc_system()}
    out: dict = {"systems": {}}
    for name, (stages, overrides) in systems.items():
        params = R.params_of(overrides)
        stage_ms, bm25_ms, extra = [], [], []
        for qid, text in sorted(texts.items()):
            st = eng.run_e(text)
            exp = a["expansions"]["narrower" if params.concepts_narrower else "plain"].get(text) or {}
            calls: list[float] = []

            def bm25(t: str, srcs: list[str] | None, depth: int) -> list[str]:
                t1 = time.perf_counter()
                res = eng.bm25_pages(t, srcs, depth)
                calls.append((time.perf_counter() - t1) * 1000)
                return res

            t0 = time.perf_counter()
            run = G.GraphRun(stages, params, G.StaticSignals(maps, {text: exp}), bm25=bm25)
            run.gate(page_kind=True, late=True)
            if run.on("concepts"):
                texts_x = run.wordings(text)
                run.concepts_leg(texts_x, [run.bm25(x, None, params.concepts_depth) for x in texts_x])
            srcs = run.seed([p for p, _s in st.fused])
            if srcs:
                run.cites_leg(run.bm25(text, srcs, params.cites_depth))
            window = run.window(st.window)
            order = list(st.order)
            if any(run.on(s) for s in G.PAGE_STAGES):
                order = run.after_late(order)
            if run.on("collapse"):
                run.finish(order, {})
            total = (time.perf_counter() - t0) * 1000
            stage_ms.append(total - sum(calls))
            bm25_ms.append(sum(calls))
            extra.append(len(window) - len(st.window))
        out["systems"][name] = {"stages": list(stages), "stage_ms": pct(stage_ms), "bm25_ms": pct(bm25_ms),
                                "late_extra_targets": pct([float(x) for x in extra])}
        R.log("latency", name, out["systems"][name]["stage_ms"])
    out["expand_query_ms"] = a["latency_ms"]
    out["lookup_build_ms"] = a["view_build_ms"]
    (R.work() / "latency.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
