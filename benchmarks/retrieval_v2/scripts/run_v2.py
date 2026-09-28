"""V2 rankings (PREREGISTRATION §3): for every encoded text model — dense only (B:m) and the served scheme E with m as
the dense leg (E:m); for every visual model — the page-image channel (VIS:v), its fusion with E (E+VIS:v) and the
three-leg first stage (E3:v); the baseline E (V1's ``E|rrf-prod>late@100``) recomputed from V1's stored inputs.

V1's own functions are imported unchanged from ``benchmarks/retrieval_v1/scripts/run_v1.py`` (page ranking, RRF of
pages, late re-scoring, page late scores, duplicate groups), so E:m differs from E only by the dense vectors.

Environment: ``J_V1`` (read only: ``units_*.jsonl``, ``cache/{S_late,dense}.npy``, ``cache/units_ids.json``,
``out/api.json``, ``q/q_rx580.jsonl``, ``core/duckdb``), ``V2_WORK`` (``vec/<key>/``, ``vis/<key>/``). Writes
``$V2_WORK/out/rankings_v2.json`` (top-100 pages per system and query; the unit that brought each page for B systems).
No metric is computed here.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
WORK = Path(os.environ["V2_WORK"])
V1 = Path(os.environ["J_V1"])
OUT = WORK / "out"
DEPTH = 100
RRF_K = 60


def load_v1():
    spec = importlib.util.spec_from_file_location("run_v1", REPO / "benchmarks/retrieval_v1/scripts/run_v1.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.DEPTH == DEPTH and mod.RRF_K == RRF_K
    return mod


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def dedup_pages(ranked: list[tuple[str, float]], dup: dict[str, str], depth: int = DEPTH) -> list[tuple[str, float]]:
    """Collapse duplicate groups in a page ranking (first page of a group stays), as the served dense leg does."""
    out, seen = [], set()
    for p, s in ranked:
        g = dup.get(p, p)
        if g in seen:
            continue
        seen.add(g)
        out.append((p, s))
        if len(out) >= depth:
            break
    return out


def page_vector_ranking(scores: np.ndarray, page_ids: np.ndarray, depth: int = 400) -> list[tuple[str, float]]:
    """Pages (one vector each) by score, ties by page id; the head is long enough for duplicate collapsing."""
    n = min(len(scores), depth)
    top = np.argpartition(-scores, n - 1)[:n]
    order = top[np.lexsort((page_ids[top], -scores[top]))]
    return [(str(page_ids[i]), float(scores[i])) for i in order]


def scores(D: np.ndarray, Q: np.ndarray) -> np.ndarray:
    """(documents × queries) cosine scores, one matrix-vector product per query: this reproduces V1's stored dense
    leg bit for bit (a batched matrix product differs in the last float bits, which can flip the representative of a
    duplicate-page group; check 0 found one such query)."""
    return np.stack([D @ Q[j] for j in range(Q.shape[0])], axis=1)


def main() -> None:
    r1 = load_v1()
    from vkm_corpus.retrieval_lab import bench as B
    from vkm_corpus.retrieval_lab import fusion

    bench = B.load_benchmark(REPO / "benchmarks/retrieval_v0")
    qids = [q.query_id for q in bench.queries]
    t0 = time.time()
    units = r1.Units()
    v1_ids = json.load(open(V1 / "cache" / "units_ids.json", encoding="utf-8"))
    if v1_ids != units.ids:
        raise SystemExit("unit order differs from V1's S_late rows")
    S_late = np.load(V1 / "cache" / "S_late.npy", mmap_mode="r")
    if S_late.shape != (len(units.ids), len(qids)):
        raise SystemExit(f"S_late shape {S_late.shape}")
    didx = units.coll["dense"]
    fin = units.coll["final"]
    dense_ids = [units.ids[i] for i in didx]
    api = json.load(open(V1 / "out" / "api.json", encoding="utf-8"))["systems"]["bm25"]
    bm25 = {q: [(h["id"], float(h.get("bm25_score") or 0.0)) for h in api[q]["hits"]][:DEPTH] for q in qids}
    dups = r1.dup_groups()
    log("units", len(units.ids), "dense", len(didx), "final", len(fin), "loaded in", round(time.time() - t0, 1), "s")

    # --- dense score matrices: (dense units × queries) per text model; V1's stored vectors = the served baseline
    dense_models: dict[str, np.ndarray] = {}
    dq, _lq, _meta = r1.load_queries_vectors()
    base = np.load(V1 / "cache" / "dense.npy")
    dense_models["nano-rx580"] = scores(base, np.stack([dq[q] for q in qids]))
    del base
    for d in sorted((WORK / "vec").iterdir()) if (WORK / "vec").is_dir() else []:
        if not (d / "meta.json").is_file():
            continue
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        if meta.get("probe") or meta.get("units") != "dense":
            continue
        ids = json.loads((d / "docs_ids.json").read_text(encoding="utf-8"))
        if ids != dense_ids:
            raise SystemExit(f"{d.name}: unit order differs from the dense collection")
        qi = json.loads((d / "queries_ids.json").read_text(encoding="utf-8"))
        if qi != qids:
            raise SystemExit(f"{d.name}: query order differs")
        D = np.load(d / "docs.f32.npy")
        Q = np.load(d / "queries.f32.npy")
        dense_models[d.name] = scores(D, Q)
        if (d / "queries_q8.f32.npy").is_file():            # serving parity (§7): Q8_0 queries on GPU documents
            dense_models[d.name + "~q8"] = scores(D, np.load(d / "queries_q8.f32.npy"))
        del D
        log("dense scores", d.name, dense_models[d.name].shape)
    visual: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for d in sorted((WORK / "vis").iterdir()) if (WORK / "vis").is_dir() else []:
        if not (d / "meta.json").is_file() or json.loads((d / "meta.json").read_text(encoding="utf-8")).get("probe"):
            continue
        pids = np.array(json.loads((d / "docs_ids.json").read_text(encoding="utf-8")), dtype=object)
        if json.loads((d / "queries_ids.json").read_text(encoding="utf-8")) != qids:
            raise SystemExit(f"{d.name}: query order differs")
        P = np.load(d / "docs.f32.npy")
        visual[d.name] = (pids, scores(P, np.load(d / "queries.f32.npy")))
        if (d / "queries_q8.f32.npy").is_file():            # serving parity (§7): Q8_0 text-tower queries
            visual[d.name + "~q8"] = (pids, scores(P, np.load(d / "queries_q8.f32.npy")))
        if (d / "queries_f16_diag.f32.npy").is_file():      # diagnostic, not in the rule: F16 GGUF text-tower queries
            visual[d.name + "~f16"] = (pids, scores(P, np.load(d / "queries_f16_diag.f32.npy")))
        del P
        log("visual scores", d.name, visual[d.name][1].shape)

    rankings: dict[str, dict[str, list]] = {}
    best: dict[str, dict[str, dict[str, str]]] = {}
    full = np.full(len(units.ids), -np.inf, dtype=np.float32)
    t0 = time.time()
    for j, qid in enumerate(qids):
        sl = np.asarray(S_late[:, j])
        ps = r1.page_scores(sl, fin[np.isfinite(sl[fin])], units)[0]
        rankings.setdefault("A|bm25-os", {})[qid] = [p for p, _ in bm25[qid]]
        dense_dup: dict[str, list[tuple[str, float]]] = {}
        for key, S in dense_models.items():
            full[:] = -np.inf
            full[didx] = S[:, j]
            pr, bu = r1.page_ranking(full, didx, units)
            rankings.setdefault(f"B:{key}", {})[qid] = [p for p, _ in pr]
            best.setdefault(f"B:{key}", {})[qid] = bu
            dd = r1.page_ranking(full, didx, units, dup=dups)[0]
            dense_dup[key] = dd
            fused = r1.rrf_pages({"bm25": bm25[qid], "dense": dd[:DEPTH]})
            rankings.setdefault(f"E:{key}", {})[qid] = [p for p, _ in r1.late_rescore(fused, ps, DEPTH)[:DEPTH]]
        e_base = [(p, 0.0) for p in rankings["E:nano-rx580"][qid]]
        for key, (pids, S) in visual.items():
            vr = page_vector_ranking(S[:, j], pids)
            rankings.setdefault(f"VIS:{key}", {})[qid] = [p for p, _ in vr[:DEPTH]]
            vdup = dedup_pages(vr, dups)
            rankings.setdefault(f"E+VIS:{key}", {})[qid] = [p for p, _ in fusion.rrf(
                {"e": e_base[:DEPTH], "vis": vdup[:DEPTH]}, k=RRF_K)[:DEPTH]]
            fused3 = r1.rrf_pages({"bm25": bm25[qid], "dense": dense_dup["nano-rx580"][:DEPTH], "vis": vdup[:DEPTH]})
            rankings.setdefault(f"E3:{key}", {})[qid] = [p for p, _ in r1.late_rescore(fused3, ps, DEPTH)[:DEPTH]]
        if (j + 1) % 20 == 0:
            log("queries", j + 1, "/", len(qids), round(time.time() - t0, 1), "s")
    OUT.mkdir(parents=True, exist_ok=True)
    meta = {"dense_models": sorted(dense_models), "visual_models": sorted(visual), "queries": len(qids),
            "depth": DEPTH, "rrf_k": RRF_K, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    tmp = OUT / "rankings_v2.json.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "rankings": rankings, "best": best}, f)
    os.replace(tmp, OUT / "rankings_v2.json")
    log("written", len(rankings), "systems", json.dumps(meta))


if __name__ == "__main__":
    main()
