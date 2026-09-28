"""RETRIEVAL_BENCHMARK_V1 (full corpus, final snapshot): production BM25 (OpenSearch, analyzer ``vkm_text``) and the
lab BM25, dense jina-v5-nano (stored stage-2 vectors, exact), page-level RRF (the production scheme), late mLateOn
MaxSim re-scoring (scheme E) and full MaxSim, the EDGE text reranker v3.5 through the VKM API; page rankings.

Environment: ``J_V1`` (work dir: ``prepare_v1.py`` outputs, ``core/`` copies of CORE's derived artifacts, ``q/`` query
encodings of the RX580 service), ``VKM_API_TOKEN_FILE`` (read-only API token), ``VKM_API_URL`` (else ``ssh -G core``
+ :8000). Everything written goes to ``$J_V1`` (git-ignored); the public report keeps IDs and numbers only.

Stages (re-runnable, each writes under ``$J_V1/out`` or ``$J_V1/cache``):
    vectors  — dense matrix of the dense-index units; late token matrix (float16) of all units (union of the dense-index
               units and the units of the snapshot) with offsets; coverage of the late artifacts
    api      — production search through the VKM API for every query, top-100 pages and timings; names: ``bm25``
               (``/v1/search``), ``hybrid`` (``/v1/search/hybrid`` with ``late: false``), ``hybrid_late`` (the served
               default with the late stage), ``bm25_recheck`` (BM25 again after an index rebuild)
    first    — lab BM25 per collection, exact dense, full MaxSim (all units × all queries), page-level fusion (RRF of
               the production scheme), late re-scoring at depths, ablations (BIB_ENTRY, pre-bibliography units)
    rerank   — EDGE text reranker v3.5 over the top-N pages of chosen systems (sequential; passages = best units;
               resumes where it stopped; ``J_RR_RECHECK=n`` first re-runs n queries done earlier, e.g. after a backend
               restart, and records whether the order is the same; ~1 min of 5xx stops the stage)
Metrics, significance and latency are computed by ``eval_v1.py``; pooled judging is ``pool_v1.py``.

Collections (units of rule ``vkm-units-v1/A``): ``final`` — units of the snapshot without BIB_ENTRY (the PAGE track
under CP-42); ``final_bib`` — with BIB_ENTRY (ablation); ``dense`` — the units the production dense index was built
from (stage 2; reference lists still inside BLOCK_GROUPs on 1.7k pages).
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

from vkm_corpus.retrieval_lab import bench as B
from vkm_corpus.retrieval_lab import fusion
from vkm_corpus.retrieval_lab.bm25 import LocalBM25

V1 = Path(os.environ["J_V1"])
OUT = V1 / "out"
CACHE = V1 / "cache"
OUT.mkdir(parents=True, exist_ok=True)
CACHE.mkdir(parents=True, exist_ok=True)
BENCH_DIR = Path("benchmarks/retrieval_v0")
V1_DIR = Path("benchmarks/retrieval_v1")
DEPTH = 100                    # pages per first-stage list (production: candidates = 100 per leg)
UNIT_SCAN = 6000               # units scanned to collect DEPTH distinct pages
RRF_K = 60
LATE_DEPTHS = (30, 50, 100, 200)
DIM_LATE = 128


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def dump_atomic(obj, path: Path, **kw) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, **kw)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------------------------------- units
def read_units(name: str) -> list[dict]:
    with open(V1 / name, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


class Units:
    """Union of the dense-index units and the snapshot units (one row per unit id) + collections as index arrays."""

    def __init__(self, with_text: bool = False):
        final = read_units("units_final.jsonl")
        dense = read_units("units_dense.jsonl")
        self.rows: list[dict] = []
        self.pos: dict[str, int] = {}
        for u in dense + final:
            if u["unit_id"] in self.pos:
                if self.rows[self.pos[u["unit_id"]]]["text_hash"] != u["text_hash"]:
                    raise RuntimeError(f"unit {u['unit_id']}: text differs between exports")
                continue
            if not with_text:
                u = {k: v for k, v in u.items() if k != "text"}
            self.pos[u["unit_id"]] = len(self.rows)
            self.rows.append(u)
        self.ids = [u["unit_id"] for u in self.rows]
        self.page = np.array([u["page_id"] or "" for u in self.rows], dtype=object)
        self.kind = np.array([u["kind"] for u in self.rows], dtype=object)
        self.coll = {"final": np.array([self.pos[u["unit_id"]] for u in final if u["kind"] != "BIB_ENTRY"]),
                     "final_bib": np.array([self.pos[u["unit_id"]] for u in final]),
                     "dense": np.array([self.pos[u["unit_id"]] for u in dense])}
        self.text = {u["unit_id"]: u["text"] for u in (dense + final)} if with_text else None

    def page_of(self, i: int) -> str:
        return self.page[i]


def page_ranking(scores: np.ndarray, idx: np.ndarray, units: Units, depth: int = DEPTH,
                 dup: dict[str, str] | None = None) -> tuple[list[tuple[str, float]], dict[str, str]]:
    """Pages of a unit collection ranked by their best unit (= first occurrence of the unit ranking); the page's
    best unit is kept for passages. ``dup`` (page → duplicate group) collapses copies like the production path."""
    s = scores[idx]
    n = min(len(idx), UNIT_SCAN)
    while True:
        top = np.argpartition(-s, n - 1)[:n] if n < len(idx) else np.arange(len(idx))
        order = top[np.lexsort((np.asarray(units.ids, dtype=object)[idx[top]], -s[top]))]
        out, best, seen = [], {}, set()
        for j in order:
            u = idx[j]
            p = units.page[u]
            g = dup.get(p, p) if dup else p
            if not p or g in seen:
                continue
            seen.add(g)
            out.append((p, float(s[j])))
            best[p] = units.ids[u]
            if len(out) >= depth:
                break
        if len(out) >= depth or n >= len(idx):
            return out, best
        n = min(len(idx), n * 4)


def page_scores(scores: np.ndarray, idx: np.ndarray, units: Units) -> tuple[dict[str, float], dict[str, str]]:
    """Max unit score per page over a collection (and the argmax unit) — the late page score of the production
    scheme (agent L: a PAGE scores the max MaxSim over the units of the page)."""
    pages = units.page[idx]
    order = np.lexsort((-scores[idx], pages))
    p_sorted = pages[order]
    first = np.ones(len(order), dtype=bool)
    first[1:] = p_sorted[1:] != p_sorted[:-1]
    sel = order[first]
    return ({units.page[idx[j]]: float(scores[idx[j]]) for j in sel},
            {units.page[idx[j]]: units.ids[idx[j]] for j in sel})


# ---------------------------------------------------------------------------------------------------- stage: vectors
def parts_of(kind_dir: Path) -> list[Path]:
    out = []
    for m in sorted(glob.glob(str(kind_dir / "**" / "_manifest-*.json"), recursive=True)):
        mp = Path(m)
        out += [mp.parent / p["file"] for p in json.loads(mp.read_text(encoding="utf-8"))["parts"]]
    return out


def stage_vectors():
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    units = Units()
    # dense: the dense-index units, in collection order
    didx = units.coll["dense"]
    want = {units.ids[i]: k for k, i in enumerate(didx)}
    hashes = {units.ids[i]: units.rows[i]["text_hash"] for i in didx}
    mat = None
    filled = np.zeros(len(didx), dtype=bool)
    info = {"dense": {}, "late": {}}
    t0 = time.time()
    sigs = set()
    for part in parts_of(V1 / "core" / "dense"):
        t = pq.read_table(part, columns=["object_id", "text_hash", "dimension", "vector", "config_hash",
                                         "model_id", "quant"])
        dim = int(t.column("dimension")[0].as_py())
        if mat is None:
            mat = np.zeros((len(didx), dim), dtype=np.float32)
        flat = pc.list_flatten(t.column("vector")).to_numpy(zero_copy_only=False).astype(np.float32)
        sigs |= set(t.column("config_hash").unique().to_pylist())
        info["dense"]["model_id"] = t.column("model_id")[0].as_py()
        info["dense"]["quant"] = t.column("quant")[0].as_py()
        for r, (oid, th) in enumerate(zip(t.column("object_id").to_pylist(), t.column("text_hash").to_pylist())):
            k = want.get(oid)
            if k is None or hashes[oid] != th:
                continue
            mat[k] = flat[r * dim:(r + 1) * dim]
            filled[k] = True
    if not filled.all():
        raise RuntimeError(f"{int((~filled).sum())} dense-index units without a vector")
    mat /= np.maximum(np.linalg.norm(mat, axis=1, keepdims=True), 1e-12)
    np.save(CACHE / "dense.npy", mat)
    info["dense"].update({"units": int(len(didx)), "dim": int(mat.shape[1]), "signatures": sorted(sigs),
                          "seconds": round(time.time() - t0, 1)})
    log("dense", mat.shape)
    del mat
    # late: every unit of the union that has a token-vector row with the same text hash
    t0 = time.time()
    hash_of = {u["unit_id"]: u["text_hash"] for u in units.rows}
    counts = np.zeros(len(units.ids), dtype=np.int64)
    parts = parts_of(V1 / "core" / "late")
    sigs = set()
    for part in parts:
        t = pq.read_table(part, columns=["object_id", "text_hash", "token_count", "config_hash"])
        sigs |= set(t.column("config_hash").unique().to_pylist())
        for oid, th, c in zip(t.column("object_id").to_pylist(), t.column("text_hash").to_pylist(),
                              t.column("token_count").to_pylist()):
            if hash_of.get(oid) == th:
                counts[units.pos[oid]] = int(c)
    offsets = np.zeros(len(units.ids) + 1, dtype=np.int64)
    offsets[1:] = np.cumsum(counts)
    total = int(offsets[-1])
    tok = np.lib.format.open_memmap(CACHE / "late_tokens.f16.npy", mode="w+", dtype=np.float16,
                                    shape=(total, DIM_LATE))
    for n, part in enumerate(parts, 1):
        t = pq.read_table(part, columns=["object_id", "text_hash", "token_count", "vectors", "dimension"])
        dim = int(t.column("dimension")[0].as_py())
        if dim != DIM_LATE:
            raise RuntimeError(f"late dim {dim}")
        flat = pc.list_flatten(t.column("vectors")).to_numpy(zero_copy_only=False).astype(np.float32)
        p = 0
        for oid, th, c in zip(t.column("object_id").to_pylist(), t.column("text_hash").to_pylist(),
                              t.column("token_count").to_pylist()):
            n_el = int(c) * dim
            if hash_of.get(oid) == th:
                i = units.pos[oid]
                block = flat[p:p + n_el].reshape(int(c), dim)
                block = block / np.maximum(np.linalg.norm(block, axis=1, keepdims=True), 1e-12)
                tok[offsets[i]:offsets[i + 1]] = block.astype(np.float16)
            p += n_el
        if n % 50 == 0:
            log("late parts", n, "/", len(parts))
    tok.flush()
    del tok
    np.save(CACHE / "late_offsets.npy", offsets)
    (CACHE / "units_ids.json").write_text(json.dumps(units.ids), encoding="utf-8")
    missing = defaultdict(lambda: defaultdict(int))
    for name, idx in units.coll.items():
        for i in idx:
            if counts[i] == 0:
                missing[name][units.kind[i]] += 1
    info["late"] = {"tokens_total": total, "units_with_tokens": int((counts > 0).sum()), "units": len(units.ids),
                    "signatures": sorted(sigs), "missing_by_collection": {k: dict(v) for k, v in missing.items()},
                    "tokens_per_unit": {name: round(float(counts[idx].sum()) / max(1, len(idx)), 1)
                                        for name, idx in units.coll.items()},
                    "tokens_by_collection": {name: int(counts[idx].sum()) for name, idx in units.coll.items()},
                    "bytes_fp16_final": int(counts[units.coll["final"]].sum()) * DIM_LATE * 2,
                    "seconds": round(time.time() - t0, 1)}
    (OUT / "vectors.json").write_text(json.dumps(info, indent=1), encoding="utf-8")
    log("late", total, "tokens;", json.dumps(info["late"]["missing_by_collection"]))


# ---------------------------------------------------------------------------------------------------- queries
def load_queries_vectors() -> tuple[dict[str, np.ndarray], dict[str, np.ndarray], dict[str, dict]]:
    dense, late, meta = {}, {}, {}
    for line in open(V1 / "q" / "q_rx580.jsonl", encoding="utf-8"):
        r = json.loads(line)
        d = np.asarray(r["dense"]["vector"], dtype=np.float32)
        dense[r["query_id"]] = d / max(float(np.linalg.norm(d)), 1e-12)
        m = np.asarray(r["late"]["vectors"], dtype=np.float32)
        late[r["query_id"]] = m / np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-12)
        meta[r["query_id"]] = {"wall_ms": r.get("wall_ms"), "dense_ms": r["dense"].get("encode_ms"),
                               "late_ms": r["late"].get("encode_ms"), "late_tokens": r["late"].get("token_vectors"),
                               "dense_model": r["dense"].get("model"), "late_model": r["late"].get("model"),
                               "dense_signature": r["dense"].get("signature"),
                               "late_signature": r["late"].get("signature")}
    return dense, late, meta


def load_late_matrix(fp32: bool = True):
    tok = np.load(CACHE / "late_tokens.f16.npy", mmap_mode="r")
    offsets = np.load(CACHE / "late_offsets.npy")
    if fp32:
        out = np.empty(tok.shape, dtype=np.float32)
        step = 2_000_000
        for a in range(0, tok.shape[0], step):
            out[a:a + step] = tok[a:a + step]
        return out, offsets
    return tok, offsets


def maxsim_all_queries(tok: np.ndarray, offsets: np.ndarray, qmats: list[np.ndarray],
                       chunk_tokens: int = 150_000) -> np.ndarray:
    """(units × queries) MaxSim sum over query tokens; units without tokens get -inf."""
    q_all = np.concatenate(qmats).astype(np.float32)
    q_off = np.zeros(len(qmats) + 1, dtype=np.int64)
    q_off[1:] = np.cumsum([len(q) for q in qmats])
    n_units = len(offsets) - 1
    out = np.full((n_units, len(qmats)), -np.inf, dtype=np.float32)
    has = np.diff(offsets) > 0
    nz = np.nonzero(has)[0]
    # walk over units with tokens in chunks of contiguous token ranges
    k = 0
    while k < len(nz):
        start_u = nz[k]
        tok_start = offsets[start_u]
        # extend while within budget
        end_k = int(np.searchsorted(offsets[nz + 1], tok_start + chunk_tokens, side="right"))
        end_k = max(end_k, k + 1)
        us = nz[k:end_k]
        tok_end = offsets[us[-1] + 1]
        sims = tok[tok_start:tok_end] @ q_all.T
        starts = offsets[us] - tok_start
        per_unit = np.maximum.reduceat(sims, starts, axis=0)
        out[us] = np.add.reduceat(per_unit, q_off[:-1], axis=1)
        k = end_k
    return out


# ---------------------------------------------------------------------------------------------------- stage: api
def api_base() -> str:
    if os.environ.get("VKM_API_URL"):
        return os.environ["VKM_API_URL"]
    out = subprocess.run(["ssh", "-G", "core"], capture_output=True, text=True, check=True).stdout
    host = next(l.split()[1] for l in out.splitlines() if l.startswith("hostname "))
    return f"http://{host}:8000"


def api_client(timeout: float = 120.0):
    import httpx

    token = Path(os.environ["VKM_API_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
    return httpx.Client(base_url=api_base(), headers={"Authorization": f"Bearer {token}"}, timeout=timeout,
                        trust_env=False)


def post_retry(client, path: str, body: dict, tries: int = 30, wait_s: float = 10.0):
    import httpx

    last = None
    for _ in range(tries):
        try:
            r = client.post(path, json=body)
            if r.status_code in (502, 503, 504):
                last = f"HTTP {r.status_code}"
            else:
                return r
        except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectTimeout) as exc:
            last = type(exc).__name__
        log("retry", path, last)
        time.sleep(wait_s)
    raise RuntimeError(f"{path}: giving up after {tries} tries ({last})")


def _hits(resp: dict) -> list[dict]:
    out = []
    for it in resp.get("items") or []:
        env, rec = it.get("envelope") or {}, it.get("record") or {}
        h = {"id": env.get("object_id"), "rank": rec.get("rank")}
        for k in ("bm25_score", "rrf_score", "trace"):
            if rec.get(k) is not None:
                h[k] = rec[k]
        out.append(h)
    return out


def stage_api(which: list[str]):
    bench = B.load_benchmark(BENCH_DIR)
    path = OUT / "api.json"
    done = json.load(open(path, encoding="utf-8")) if path.is_file() else {"systems": {}, "meta": {}}
    client = api_client()
    status = client.get("/v1/status").json().get("status", {})
    deps = status.get("dependencies") or {}
    now = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "systems": which,
           "canonical": {k: (status.get("canonical") or {}).get(k) for k in
                         ("snapshot_id", "current_snapshot_id", "up_to_date", "built_at")},
           "opensearch_aliases": (deps.get("opensearch") or {}).get("aliases"),
           "late_interaction": deps.get("late_interaction")}
    done["meta"].setdefault("status", now)                 # the status of the first collection is kept
    done["meta"].setdefault("status_history", []).append(now)
    for name in which:
        sysd = done["systems"].setdefault(name, {})
        todo = [q for q in bench.queries if q.query_id not in sysd]
        log("api", name, len(todo), "queries")
        for n, q in enumerate(todo, 1):
            hits, records, walls = [], [], []
            for cursor in (None, "50"):
                if name in ("bm25", "bm25_recheck"):       # recheck: same request against the current build
                    body = {"query": q.text, "kinds": ["PAGE"], "limit": 50}
                    route = "/v1/search"
                elif name == "hybrid":
                    # collected before the API had a late stage; since late is on by default, a re-run must disable it
                    body = {"query": q.text, "kinds": ["PAGE"], "limit": 50, "candidates": DEPTH, "late": False}
                    route = "/v1/search/hybrid"
                elif name == "hybrid_late":
                    body = {"query": q.text, "kinds": ["PAGE"], "limit": 50, "candidates": DEPTH, "late": True,
                            "late_candidates": DEPTH}
                    route = "/v1/search/hybrid"
                else:
                    raise ValueError(name)
                if cursor:
                    body["cursor"] = cursor
                t0 = time.perf_counter()
                r = post_retry(client, route, body)
                walls.append(round((time.perf_counter() - t0) * 1000, 1))
                if r.status_code != 200:
                    records.append({"status": r.status_code, "error": r.text[:400]})
                    break
                resp = r.json()
                hits += _hits(resp)
                rec = ((resp.get("item") or {}).get("record") or {})
                records.append({"status": 200, "elapsed_ms": (resp.get("meta") or {}).get("elapsed_ms"),
                                "timings_ms": rec.get("timings_ms"), "stages": rec.get("stages") if not cursor else None,
                                "totals": rec.get("totals"), "fused_total": rec.get("fused_total"),
                                "snapshot": (resp.get("meta") or {}).get("canonical_snapshot_id"),
                                "warnings": (resp.get("meta") or {}).get("warnings")})
                if len(_hits(resp)) < 50:
                    break
            sysd[q.query_id] = {"hits": hits, "calls": records, "wall_ms": walls}
            if n % 20 == 0:
                dump_atomic(done, path)
                log("api", name, n, "/", len(todo))
        dump_atomic(done, path)
    client.close()


# ---------------------------------------------------------------------------------------------------- stage: first
def dup_groups() -> dict[str, str]:
    import duckdb

    con = duckdb.connect(str(V1 / "core" / "duckdb" / "vkm_corpus.duckdb"), read_only=True)
    rows = con.execute("SELECT page_id, dup_group_id FROM duplicate_page_candidates").fetchall()
    con.close()
    return {p: g for p, g in rows}


def rrf_pages(lists: dict[str, list[tuple[str, float]]], depth: int = DEPTH) -> list[tuple[str, float]]:
    return fusion.rrf(lists, k=RRF_K)[:2 * depth]


def late_rescore(cands: list[tuple[str, float]], pscore: dict[str, float], n: int) -> list[tuple[str, float]]:
    """Production late stage: the top-n candidates re-ordered by the late page score (unscored after the scored
    ones in candidate order), the rest keeps the fused order."""
    head, tail = cands[:n], cands[n:]
    scored = [(p, pscore[p]) for p, _ in head if p in pscore and np.isfinite(pscore[p])]
    unscored = [(p, s) for p, s in head if not (p in pscore and np.isfinite(pscore[p]))]
    order = {p: i for i, (p, _) in enumerate(head)}
    scored.sort(key=lambda x: (-x[1], order[x[0]]))
    return scored + unscored + list(tail)


def stage_first():
    units = Units(with_text=True)
    bench = B.load_benchmark(BENCH_DIR)
    queries = list(bench.queries)
    qids = [q.query_id for q in queries]
    dq, lq, qmeta = load_queries_vectors()
    dups = dup_groups()
    timing: dict[str, float] = {}
    rankings: dict[str, dict[str, list]] = defaultdict(dict)
    best: dict[str, dict[str, dict[str, str]]] = defaultdict(dict)   # system → query → page → best unit
    # --- BM25 (lab, analyzer mirroring vkm_text) per collection
    bm25_units = {}
    for coll in ("final", "final_bib", "dense"):
        idx = units.coll[coll]
        t0 = time.time()
        bm = LocalBM25.build([units.ids[i] for i in idx], [units.text[units.ids[i]] for i in idx])
        timing[f"bm25_lab_build_s|{coll}"] = round(time.time() - t0, 1)
        t0 = time.time()
        scores = np.full(len(units.ids), -np.inf, dtype=np.float32)
        per_q = {}
        for q in queries:
            hits = bm.search(q.text, UNIT_SCAN)
            per_q[q.query_id] = hits
        timing[f"bm25_lab_query_ms|{coll}"] = round((time.time() - t0) * 1000 / len(queries), 1)
        for q in queries:
            scores[:] = -np.inf
            for uid, s in per_q[q.query_id]:
                scores[units.pos[uid]] = s
            valid = idx[np.isfinite(scores[idx])]
            if len(valid) == 0:
                rankings[f"A|bm25-lab@{coll}"][q.query_id] = []
                continue
            pr, bu = page_ranking(scores, valid, units)
            rankings[f"A|bm25-lab@{coll}"][q.query_id] = pr
            best[f"A|bm25-lab@{coll}"][q.query_id] = bu
        bm25_units[coll] = per_q
        del bm
        log("bm25 lab", coll, timing[f"bm25_lab_build_s|{coll}"], timing[f"bm25_lab_query_ms|{coll}"])
    # --- dense exact (the dense-index units)
    dmat = np.load(CACHE / "dense.npy")
    didx = units.coll["dense"]
    Q = np.stack([dq[q] for q in qids])
    t0 = time.time()
    S_dense_c = dmat @ Q.T                                     # (dense units, queries)
    timing["dense_exact_ms_per_query_batched"] = round((time.time() - t0) * 1000 / len(qids), 2)
    t0 = time.time()
    _ = dmat @ Q[0]
    timing["dense_exact_ms_single"] = round((time.time() - t0) * 1000, 2)
    del dmat
    S_dense = np.full((len(units.ids), len(qids)), -np.inf, dtype=np.float32)
    S_dense[didx] = S_dense_c
    del S_dense_c
    for j, qid in enumerate(qids):
        pr, bu = page_ranking(S_dense[:, j], didx, units)
        rankings["B|dense"][qid] = pr
        best["B|dense"][qid] = bu
        rankings["B|dense-dup"][qid] = page_ranking(S_dense[:, j], didx, units, dup=dups)[0]
    log("dense done")
    # --- late: full MaxSim for every unit with tokens (union), all queries at once
    t0 = time.time()
    tok, offsets = load_late_matrix(fp32=True)
    timing["late_load_s"] = round(time.time() - t0, 1)
    qmats = [lq[q] for q in qids]
    t0 = time.time()
    S_late = maxsim_all_queries(tok, offsets, qmats)
    timing["late_full_maxsim_batched_s_total"] = round(time.time() - t0, 1)
    np.save(CACHE / "S_late.npy", S_late)
    # single-query cost of the full MaxSim over the final collection (threads as set by the environment)
    fin = units.coll["final"]
    fin_has = fin[np.diff(offsets)[fin] > 0]
    lat = []
    for qid in qids[:12]:
        t1 = time.perf_counter()
        _ = maxsim_all_queries(tok, offsets, [lq[qid]])
        lat.append((time.perf_counter() - t1) * 1000)
    timing["late_full_maxsim_ms_single_p50"] = round(float(np.median(lat)), 1)
    timing["late_tokens_final"] = int(np.diff(offsets)[fin].sum())
    timing["late_units_final_with_tokens"] = int(len(fin_has))
    # re-score cost for 100 candidate pages (their units) — numpy, one query
    del tok
    log("late full done", timing["late_full_maxsim_batched_s_total"], "s")
    tok, offsets = load_late_matrix(fp32=False)
    # --- per-query systems
    api = json.load(open(OUT / "api.json", encoding="utf-8"))["systems"] if (OUT / "api.json").is_file() else {}
    page_units_final = defaultdict(list)
    for i in fin:
        page_units_final[units.page[i]].append(i)
    rescore_ms = []
    for j, qid in enumerate(qids):
        sl = S_late[:, j]
        for coll in ("final", "final_bib", "dense"):
            idx = units.coll[coll]
            valid = idx[np.isfinite(sl[idx])]
            pr, bu = page_ranking(sl, valid, units)
            rankings[f"L|late-full@{coll}"][qid] = pr
            best[f"L|late-full@{coll}"][qid] = bu
        ps = {c: page_scores(sl, units.coll[c][np.isfinite(sl[units.coll[c]])], units)
              for c in ("final", "final_bib", "dense")}
        # production BM25 (OpenSearch) and production hybrid as served (API)
        legs = {}
        if "bm25" in api and qid in api["bm25"]:
            rankings["A|bm25-os"][qid] = [(h["id"], float(h.get("bm25_score") or 0.0)) for h in api["bm25"][qid]["hits"]]
            legs["bm25"] = rankings["A|bm25-os"][qid][:DEPTH]
        if "hybrid" in api and qid in api["hybrid"]:
            rankings["C|hybrid-api"][qid] = [(h["id"], float(h.get("rrf_score") or 0.0))
                                             for h in api["hybrid"][qid]["hits"]]
        if "hybrid_late" in api and qid in api["hybrid_late"]:
            rankings["E|hybrid-late-api"][qid] = [(h["id"], 0.0) for h in api["hybrid_late"][qid]["hits"]]
        dense_pages = rankings["B|dense-dup"][qid]
        if "bm25" in legs:
            fused = rrf_pages({"bm25": legs["bm25"], "dense": dense_pages[:DEPTH]})
            rankings["C|rrf-prod"][qid] = fused[:DEPTH]
            for n in LATE_DEPTHS:
                for coll in ("final", "final_bib", "dense"):
                    if n != 100 and coll != "final":
                        continue
                    name = f"E|rrf-prod>late@{n}" + ("" if coll == "final" else f"@{coll}")
                    t1 = time.perf_counter()
                    rankings[name][qid] = late_rescore(fused, ps[coll][0], n)[:DEPTH]
                    if n == 100 and coll == "final":
                        rescore_ms.append((time.perf_counter() - t1) * 1000)
                    best[name][qid] = ps[coll][1]
            # candidates without the dense leg: BM25 top-100 → late
            rankings["E|bm25-os>late@100"][qid] = late_rescore(legs["bm25"], ps["final"][0], 100)[:DEPTH]
            best["E|bm25-os>late@100"][qid] = ps["final"][1]
            # three-way RRF with the full late list
            rankings["F3|rrf(bm25-os,dense,late-full)"][qid] = fusion.rrf(
                {"bm25": legs["bm25"], "dense": dense_pages[:DEPTH],
                 "late": rankings["L|late-full@final"][qid][:DEPTH]}, k=RRF_K)[:DEPTH]
            best["F3|rrf(bm25-os,dense,late-full)"][qid] = ps["final"][1]
            best["C|rrf-prod"][qid] = ps["final"][1]
        # lab line (V0 scheme, unit level) on the dense-index units: RRF(BM25 lab, dense) top-100 units → late
        bmu = [(u, s) for u, s in bm25_units["dense"][qid][:DEPTH]]
        didx_sorted = didx[np.argsort(-S_dense[didx, j], kind="stable")[:DEPTH]]
        du = [(units.ids[i], float(S_dense[i, j])) for i in didx_sorted]
        fu = fusion.rrf({"bm25": bmu, "dense": du}, k=RRF_K)[:DEPTH]
        rankings["C|rrf-unit-lab@dense"][qid] = to_page_list([u for u, _ in fu], units)
        resc = sorted(((u, float(sl[units.pos[u]])) for u, _ in fu), key=lambda x: (-x[1], x[0]))
        rankings["E|rrf-unit-lab>late@dense"][qid] = to_page_list([u for u, _ in resc], units)
        # page-level RRF of the lab BM25 (final collection) with dense → late (sensitivity to the BM25 engine)
        lab_legs = {"bm25": rankings["A|bm25-lab@final"][qid][:DEPTH], "dense": rankings["B|dense"][qid][:DEPTH]}
        fl = rrf_pages(lab_legs)
        rankings["C|rrf-lab"][qid] = fl[:DEPTH]
        rankings["E|rrf-lab>late@100"][qid] = late_rescore(fl, ps["final"][0], 100)[:DEPTH]
        best["E|rrf-lab>late@100"][qid] = ps["final"][1]
    timing["late_rescore_top100_pages_ms_p50_python"] = round(float(np.median(rescore_ms)), 3) if rescore_ms else None
    # late re-score cost from the token matrix (100 pages → their units, MaxSim) — the service-side work
    lat = []
    for j, qid in enumerate(qids[:40]):
        pages_ = [p for p, _ in rankings["C|rrf-prod"].get(qid, [])][:100]
        uidx = [i for p in pages_ for i in page_units_final.get(p, []) if offsets[i + 1] > offsets[i]]
        t1 = time.perf_counter()
        qm = lq[qid]
        for i in uidx:
            m = np.asarray(tok[offsets[i]:offsets[i + 1]], dtype=np.float32)
            (m @ qm.T).max(axis=0).sum()
        lat.append((time.perf_counter() - t1) * 1000)
    timing["late_rescore_top100_pages_ms_p50_numpy"] = round(float(np.median(lat)), 1) if lat else None
    timing["late_rescore_units_per_query_p50"] = None
    dump_atomic({"rankings": rankings, "best": best, "timing": timing, "qmeta": qmeta}, OUT / "rankings.json")
    log("first stage written", len(rankings), "systems", json.dumps(timing))


def to_page_list(unit_ids: list[str], units: Units) -> list[tuple[str, float]]:
    seen, out = set(), []
    for u in unit_ids:
        p = units.page[units.pos[u]]
        if p and p not in seen:
            seen.add(p)
            out.append((p, 0.0))
    return out


# ---------------------------------------------------------------------------------------------------- stage: rerank
RERANK_FALLBACK = 7            # guaranteed to fit the 4096-token listwise prompt (J's V0 finding 8)
PASSAGE_CHARS = {8: 1200, 10: 1000, 12: 800}


def stage_rerank(systems: list[str], depths: list[int], only_track: str = "text"):
    units = Units(with_text=True)
    data = json.load(open(OUT / "rankings.json", encoding="utf-8"))
    rankings, bestd = data["rankings"], data["best"]
    bench = B.load_benchmark(BENCH_DIR)
    prep = json.load(open(V1 / "prepare.json", encoding="utf-8"))
    covered = set(prep["covered_queries"])
    qtext = {q.query_id: q.text for q in bench.queries}
    qs = [q.query_id for q in bench.queries if q.track == only_track and q.query_id in covered]
    # first BLOCK_GROUP unit of each page (final collection) for figure/table/formula context
    first_group: dict[str, str] = {}
    for i in units.coll["final"]:
        if units.kind[i] == "BLOCK_GROUP":
            first_group.setdefault(units.page[i], units.ids[i])
    path = OUT / "rerank.json"
    done = json.load(open(path, encoding="utf-8")) if path.is_file() else {"rankings": {}, "meta": {}}
    recheck = int(os.environ.get("J_RR_RECHECK", "0"))   # re-run N queries done earlier (after a backend restart)
    client = api_client(timeout=400)
    try:
        for sysname in systems:
            for depth in depths:
                out_name = f"R|{sysname}>rerank@{depth}"
                done["rankings"].setdefault(out_name, {})
                done["meta"].setdefault(out_name, {})
                earlier = [q for q in qs if q in done["rankings"][out_name]]
                todo = [q for q in qs if q not in done["rankings"][out_name] and q in rankings.get(sysname, {})]
                if os.environ.get("J_RR_LIMIT"):
                    todo = todo[:int(os.environ["J_RR_LIMIT"])]
                for qid in earlier[:recheck]:
                    ranked = [p for p, _ in rankings[sysname][qid]]
                    new, meta = rerank_pages(client, qtext[qid], ranked, bestd.get(sysname, {}).get(qid, {}), units,
                                             first_group, depth)
                    old = [p for p, _ in done["rankings"][out_name][qid]][:len(new)]
                    same = {"identical": new == old, "top1_same": bool(new and old and new[0] == old[0]),
                            "overlap_top3": len(set(new[:3]) & set(old[:3])), "n": len(new), "meta": meta}
                    done.setdefault("recheck", {}).setdefault(out_name, {})[qid] = same
                    dump_atomic(done, path)
                    log("recheck", out_name, qid, json.dumps({k: v for k, v in same.items() if k != "meta"}))
                log("rerank", out_name, len(todo), "queries")
                for n, qid in enumerate(todo, 1):
                    ranked = [p for p, _ in rankings[sysname][qid]]
                    bu = bestd.get(sysname, {}).get(qid, {})
                    new, meta = rerank_pages(client, qtext[qid], ranked, bu, units, first_group, depth)
                    if not new:
                        meta["fail_open"] = True
                        new = []
                    rest = [p for p in ranked if p not in set(new)]
                    done["rankings"][out_name][qid] = [[p, 0.0] for p in new + rest]
                    done["meta"][out_name][qid] = meta
                    dump_atomic(done, path)                   # every query: an outage loses nothing
                    if n % 10 == 0:
                        log(out_name, n, "/", len(todo), json.dumps(meta)[:200])
                dump_atomic(done, path)
    except RuntimeError as exc:                              # backend unavailable: stop, never work around it
        dump_atomic(done, path)
        log("rerank stopped:", exc)
    finally:
        client.close()


def build_passage(page: str, unit_id: str | None, units: Units, first_group: dict[str, str], cap: int) -> list[str]:
    """Objects of the page's best unit (a figure/table/formula unit is followed by the page's first text group for
    context), cut to ~``cap`` characters of unit text so that n candidates rarely saturate the per-candidate budget."""
    objs: list[str] = []
    chars = 0
    chain = []
    if unit_id and unit_id in units.pos:
        chain.append(unit_id)
        if units.rows[units.pos[unit_id]]["kind"] != "BLOCK_GROUP" and first_group.get(page):
            chain.append(first_group[page])
    for uid in chain:
        row = units.rows[units.pos[uid]]
        per_obj = max(1, len(units.text[uid]) // max(1, len(row["object_ids"])))
        for o in row["object_ids"]:
            if objs and chars + per_obj > cap:
                break
            if o not in objs:
                objs.append(o)
                chars += per_obj
        if chars >= cap:
            break
    return objs[:20]


def rerank_pages(client, query: str, pages: list[str], best_unit: dict[str, str], units: Units,
                 first_group: dict[str, str], depth: int) -> tuple[list[str], dict]:
    cands = pages[:depth]
    passages = []
    for p in cands:
        objs = build_passage(p, best_unit.get(p), units, first_group, PASSAGE_CHARS.get(depth, 1000))
        if objs:
            passages.append({"candidate_id": p, "object_ids": objs})
    body = {"query": query, "candidate_ids": cands, "top_n": len(cands), "passages": passages}
    t0 = time.time()
    at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    r = post_retry(client, "/v1/rerank/text", body, tries=6)   # ~1 min of 5xx → the stage stops (no long retry loops)
    dt = time.time() - t0
    if r.status_code == 413 and depth > RERANK_FALLBACK:
        ranked, meta = rerank_pages(client, query, pages, best_unit, units, first_group, RERANK_FALLBACK)
        meta["fallback_from"] = depth
        meta["latency_s"] = round(meta["latency_s"] + dt, 2)
        return ranked, meta
    if r.status_code != 200:
        return [], {"status": r.status_code, "error": r.text[:300], "latency_s": round(dt, 2), "depth": depth, "at": at}
    data = r.json()
    items = sorted(data.get("items", []), key=lambda i: i["record"]["rank"])
    ranked = [i["envelope"]["object_id"] for i in items]
    service = (data.get("item") or {}).get("record", {})
    meta = {"status": 200, "at": at, "latency_s": round(dt, 2), "n_sent": len(cands), "n_ranked": len(ranked),
            "depth": depth,
            "rejected": len(service.get("rejected", []) or []),
            "rejected_codes": sorted({x.get("code", "?") for x in service.get("rejected", []) or []}),
            "truncated": sum(1 for i in items if i["record"].get("truncated")),
            "n_tokens": [i["record"].get("n_tokens") for i in items]}
    return ranked, meta


# ---------------------------------------------------------------------------------------------------- main
if __name__ == "__main__":
    stage = sys.argv[1]
    if stage == "vectors":
        stage_vectors()
    elif stage == "api":
        stage_api(sys.argv[2].split(","))
    elif stage == "first":
        stage_first()
    elif stage == "rerank":
        # aliases keep shell metacharacters out of command lines
        alias = {"E100": "E|rrf-prod>late@100", "C": "C|rrf-prod", "L": "L|late-full@final", "BM25": "A|bm25-os"}
        stage_rerank([alias.get(s, s) for s in sys.argv[2].split(",")], [int(x) for x in sys.argv[3].split(",")],
                     sys.argv[4] if len(sys.argv) > 4 else "text")
    else:
        raise SystemExit(f"unknown stage {stage}")
