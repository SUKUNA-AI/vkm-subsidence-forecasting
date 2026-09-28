"""RETRIEVAL_BENCHMARK_V0 (run on the canary snapshot 2026-09-28): BM25 (local), RX580 dense + late (K's artifacts / service),
RRF fusion, late re-scoring, EDGE text rerank through the VKM API, page-level metrics and significance vs BM25.

Environment: J_V0 (work dir: units.jsonl, prepare.json, canary DuckDB, rx/ with derived embeddings of agent K's
harness and query vectors), VKM_API_TOKEN_FILE (read-only API token), VKM_API_URL (or `ssh -G core` + :8000),
J_BGEM3_ROOT / J_BGEM3_QNPZ (BGE-M3 fp32 CPU artifacts and query encodings of agent K).

Stages (each writes JSON under $J_V0/out, re-runnable):
    first   — BM25, dense, late (full MaxSim), fusion, late re-scoring → rankings.json
    bgem3   — BGE-M3 dense / sparse / multi-vector / unified and their fusions → rankings.json
    rerank  — top-24 of chosen systems through POST /v1/rerank/text (≤ 2 in flight) → rerank.json
    visual  — (optional) figure/page candidates → POST /v1/rerank/visual → visual.json
    eval    — metrics, slices, significance → results.json
"""
import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from vkm_corpus.retrieval_lab import bench as B
from vkm_corpus.retrieval_lab import fusion, metrics
from vkm_corpus.retrieval_lab.bm25 import LocalBM25
from vkm_corpus.retrieval_lab.vectors import l2_normalize, maxsim

V0 = Path(os.environ["J_V0"])
OUT = V0 / "out"
OUT.mkdir(parents=True, exist_ok=True)
BENCH_DIR = Path("benchmarks/retrieval_v0")
DEPTH = 100
RERANK_DEPTH = 24

DENSE = {"granite311": V0 / "rx" / "data" / "derived" / "embeddings" / "dense",
         "jnano": V0 / "rx" / "data2" / "derived" / "embeddings" / "dense",
         "qwen3": V0 / "rx" / "data3" / "derived" / "embeddings" / "dense"}
if os.environ.get("J_DENSE") is not None:
    DENSE = {k: v for k, v in DENSE.items() if k in os.environ["J_DENSE"].split(",")}
LATE = {"mlateon": V0 / "rx" / "data" / "derived" / "embeddings" / "multivector",
        "jcolbert": V0 / "rx" / "data2" / "derived" / "embeddings" / "multivector"}
LATE_ALL = dict(LATE)
if os.environ.get("J_LATE") is not None:
    LATE = {k: v for k, v in LATE.items() if k in os.environ["J_LATE"].split(",")}
QVEC = {"granite311":("q_8790.jsonl", "dense"), "mlateon": ("q_8790.jsonl", "late"),
        "jnano": ("q_8791.jsonl", "dense"), "jcolbert": ("q_8791.jsonl", "late"), "qwen3": ("q_8792.jsonl", "dense")}
MODEL_ID = {"granite311": "ibm-granite/granite-embedding-311m-multilingual-r2 Q8_0",
            "jnano": "jinaai/jina-embeddings-v5-text-nano-retrieval Q8_0",
            "mlateon": "lightonai/mLateOn Q8_0", "jcolbert": "jinaai/jina-colbert-v2 Q8_0 (128)",
            "qwen3": "Qwen/Qwen3-Embedding-0.6B Q8_0"}


def dump_atomic(obj, path: Path, **kw) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, **kw)
    os.replace(tmp, path)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def load_units():
    units = [json.loads(l) for l in open(V0 / "units.jsonl", encoding="utf-8")]
    return [u for u in units if u["kind"] != "PAGE"], [u for u in units if u["kind"] == "PAGE"]


def config_dirs(root: Path):
    return sorted({p.parent for p in root.rglob("_manifest-*.json")})


def read_parts(directory: Path):
    import pyarrow.parquet as pq

    parts = []
    for m in sorted(directory.glob("_manifest-*.json")):
        parts += [directory / p["file"] for p in json.loads(m.read_text(encoding="utf-8"))["parts"]]
    return [pq.read_table(p) for p in parts]


def load_dense(root: Path, ids: list[str]) -> np.ndarray:
    import pyarrow.compute as pc

    (d,) = config_dirs(root)
    vec = {}
    for t in read_parts(d):
        oids = t.column("object_id").to_pylist()
        dim = int(t.column("dimension")[0].as_py())
        flat = pc.list_flatten(t.column("vector")).to_numpy(zero_copy_only=False).astype(np.float32)
        for i, oid in enumerate(oids):
            vec[oid] = flat[i * dim:(i + 1) * dim]
    missing = [i for i in ids if i not in vec]
    if missing:
        raise RuntimeError(f"{root}: {len(missing)} units without a dense vector")
    return l2_normalize(np.stack([vec[i] for i in ids]))


def load_late(root: Path, ids: list[str]):
    import pyarrow.compute as pc

    (d,) = config_dirs(root)
    mats = {}
    for t in read_parts(d):
        oids = t.column("object_id").to_pylist()
        counts = t.column("token_count").to_numpy()
        dim = int(t.column("dimension")[0].as_py())
        flat = pc.list_flatten(t.column("vectors")).to_numpy(zero_copy_only=False)
        pos = 0
        for oid, c in zip(oids, counts):
            n = int(c) * dim
            mats[oid] = flat[pos:pos + n].reshape(int(c), dim)
            pos += n
    missing = [i for i in ids if i not in mats]
    if missing:
        raise RuntimeError(f"{root}: {len(missing)} units without token vectors")
    offsets = np.zeros(len(ids) + 1, dtype=np.int64)
    for k, i in enumerate(ids):
        offsets[k + 1] = offsets[k] + len(mats[i])
    big = np.concatenate([mats[i] for i in ids]).astype(np.float32)
    big /= np.maximum(np.linalg.norm(big, axis=1, keepdims=True), 1e-12)
    return big, offsets


def load_qvecs(name: str) -> dict[str, np.ndarray]:
    fname, role = QVEC[name]
    out = {}
    for line in open(V0 / "rx" / "q" / fname, encoding="utf-8"):
        r = json.loads(line)
        if role == "dense":
            out[r["query_id"]] = l2_normalize(np.asarray(r["dense"]["vector"], dtype=np.float32))
        else:
            out[r["query_id"]] = l2_normalize(np.asarray(r["late"]["vectors"], dtype=np.float32))
    return out


def dense_rank(doc: np.ndarray, q: np.ndarray, ids: list[str], k: int = DEPTH):
    s = doc @ q
    top = np.argpartition(-s, k)[:k]
    top = top[np.lexsort((np.array(ids)[top], -s[top]))]
    return [(ids[i], float(s[i])) for i in top]


def maxsim_all(big: np.ndarray, offsets: np.ndarray, q: np.ndarray) -> np.ndarray:
    sims = big @ q.T                                   # (tokens, q_tokens)
    per = np.maximum.reduceat(sims, offsets[:-1], axis=0)   # (units, q_tokens)
    return per.sum(axis=1)


def rank_scores(scores: np.ndarray, ids: list[str], k: int = DEPTH, subset: list[int] | None = None):
    idx = np.arange(len(ids)) if subset is None else np.asarray(subset)
    s = scores[idx]
    k = min(k, len(idx))
    top = np.argpartition(-s, k - 1)[:k]
    order = top[np.lexsort((np.array(ids)[idx[top]], -s[top]))]
    return [(ids[idx[i]], float(s[i])) for i in order]


# ------------------------------------------------------------------ stage: first
def stage_first():
    units, _ = load_units()
    ids = [u["unit_id"] for u in units]
    pos = {u: i for i, u in enumerate(ids)}
    bench = B.load_benchmark(BENCH_DIR)
    queries = list(bench.queries)
    rankings: dict[str, dict[str, list]] = defaultdict(dict)
    timing = {}
    t0 = time.time()
    bm25 = LocalBM25.build(ids, [u["text"] for u in units])
    timing["bm25_build_s"] = round(time.time() - t0, 1)
    t0 = time.time()
    for q in queries:
        rankings["A|bm25"][q.query_id] = bm25.search(q.text, DEPTH)
    timing["bm25_query_ms"] = round((time.time() - t0) * 1000 / len(queries), 1)
    log("bm25 done", timing)
    dense_lists = {}
    for name, root in DENSE.items():
        doc = load_dense(root, ids)
        qv = load_qvecs(name)
        t0 = time.time()
        for q in queries:
            rankings[f"B|dense={name}"][q.query_id] = dense_rank(doc, qv[q.query_id], ids)
        timing[f"{name}_search_ms"] = round((time.time() - t0) * 1000 / len(queries), 2)
        dense_lists[name] = rankings[f"B|dense={name}"]
        log("dense", name, doc.shape)
        del doc
    late_scores = {}
    for name, root in LATE.items():
        big, offsets = load_late(root, ids)
        qv = load_qvecs(name)
        t0 = time.time()
        full = {}
        for q in queries:
            full[q.query_id] = maxsim_all(big, offsets, qv[q.query_id])
            rankings[f"L|late={name}"][q.query_id] = rank_scores(full[q.query_id], ids)
        timing[f"{name}_full_maxsim_ms"] = round((time.time() - t0) * 1000 / len(queries), 1)
        timing[f"{name}_tokens_total"] = int(offsets[-1])
        timing[f"{name}_tokens_per_unit"] = round(float(offsets[-1]) / len(ids), 1)
        late_scores[name] = full
        log("late", name, big.shape)
        del big
    # fusion and late re-scoring
    for dname in DENSE:
        for q in queries:
            lists = {"bm25": rankings["A|bm25"][q.query_id], "dense": rankings[f"B|dense={dname}"][q.query_id]}
            rankings[f"C|bm25+{dname}"][q.query_id] = fusion.rrf(lists, k=60)[:DEPTH]
        for lname in LATE:
            for q in queries:
                fused = rankings[f"C|bm25+{dname}"][q.query_id]
                cand = [pos[u] for u, _ in fused]
                rescored = rank_scores(late_scores[lname][q.query_id], ids, k=len(cand), subset=cand)
                rankings[f"E|bm25+{dname}>late={lname}"][q.query_id] = rescored
                three = {"bm25": rankings["A|bm25"][q.query_id], "dense": rankings[f"B|dense={dname}"][q.query_id],
                         "late": rankings[f"L|late={lname}"][q.query_id]}
                rankings[f"F3|rrf(bm25,{dname},{lname})"][q.query_id] = fusion.rrf(three, k=60)[:DEPTH]
    for lname in LATE:
        for q in queries:
            lists = {"bm25": rankings["A|bm25"][q.query_id], "late": rankings[f"L|late={lname}"][q.query_id]}
            rankings[f"C|bm25+{lname}"][q.query_id] = fusion.rrf(lists, k=60)[:DEPTH]
            cand = [pos[u] for u, _ in rankings["A|bm25"][q.query_id]]
            rankings[f"E|bm25>late={lname}"][q.query_id] = rank_scores(late_scores[lname][q.query_id], ids,
                                                                         k=len(cand), subset=cand)
    dump_atomic({"rankings": rankings, "timing": timing}, OUT / "rankings.json")
    log("first stage written", len(rankings), "systems")


# ------------------------------------------------------------------ stage: bgem3 (K's fp32 CPU FlagEmbedding artifacts)
M3_WEIGHTS = {"dense": 0.4, "sparse": 0.2, "colbert": 0.4}      # model card: weights_for_different_modes


def _one_config_dir(root: Path) -> Path:
    dirs = config_dirs(root)
    if len(dirs) != 1:
        raise RuntimeError(f"{root}: expected one config dir, found {len(dirs)}")
    return dirs[0]


def _parts(directory: Path) -> list[Path]:
    out = []
    for m in sorted(directory.glob("_manifest-*.json")):
        out += [directory / p["file"] for p in json.loads(m.read_text(encoding="utf-8"))["parts"]]
    return out


def load_sparse(root: Path, ids: list[str]):
    import pyarrow.parquet as pq
    from scipy import sparse

    pos = {u: i for i, u in enumerate(ids)}
    rows, cols, vals, seen = [], [], [], set()
    for part in _parts(_one_config_dir(root)):
        t = pq.read_table(part, columns=["object_id", "token_ids", "weights"])
        for oid, tok, w in zip(t.column("object_id").to_pylist(), t.column("token_ids").to_pylist(),
                               t.column("weights").to_pylist()):
            if oid not in pos or oid in seen:
                continue
            seen.add(oid)
            rows += [pos[oid]] * len(tok)
            cols += tok
            vals += w
    missing = len(ids) - len(seen)
    if missing:
        raise RuntimeError(f"{root}: {missing} units without sparse weights")
    return sparse.csr_matrix((np.asarray(vals, dtype=np.float32), (np.asarray(rows), np.asarray(cols))),
                             shape=(len(ids), max(cols) + 1 if cols else 1))


def load_late_stream(root: Path, ids: list[str]):
    """Token vectors of all units into one preallocated float32 array (streamed part by part)."""
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    pos = {u: i for i, u in enumerate(ids)}
    parts = _parts(_one_config_dir(root))
    count = np.zeros(len(ids), dtype=np.int64)
    dim = None
    for part in parts:
        t = pq.read_table(part, columns=["object_id", "token_count", "dimension"])
        dim = int(t.column("dimension")[0].as_py())
        for oid, c in zip(t.column("object_id").to_pylist(), t.column("token_count").to_pylist()):
            if oid in pos:
                count[pos[oid]] = int(c)
    if (count == 0).any():
        raise RuntimeError(f"{root}: {int((count == 0).sum())} units without token vectors")
    offsets = np.zeros(len(ids) + 1, dtype=np.int64)
    offsets[1:] = np.cumsum(count)
    big = np.empty((int(offsets[-1]), dim), dtype=np.float32)
    for part in parts:
        t = pq.read_table(part, columns=["object_id", "token_count", "vectors"])
        flat = pc.list_flatten(t.column("vectors")).to_numpy(zero_copy_only=False)
        p = 0
        for oid, c in zip(t.column("object_id").to_pylist(), t.column("token_count").to_pylist()):
            n = int(c) * dim
            if oid in pos:
                k = pos[oid]
                big[offsets[k]:offsets[k + 1]] = flat[p:p + n].reshape(int(c), dim)
            p += n
    big /= np.maximum(np.linalg.norm(big, axis=1, keepdims=True), 1e-12)
    return big, offsets


def maxsim_batch(big: np.ndarray, offsets: np.ndarray, qmats: list[np.ndarray], chunk_tokens: int = 60000,
                 average: bool = False) -> np.ndarray:
    """(units x queries) MaxSim for many queries at once; unit-aligned token chunks keep memory bounded."""
    q_all = np.concatenate(qmats).astype(np.float32)
    q_off = np.zeros(len(qmats) + 1, dtype=np.int64)
    q_off[1:] = np.cumsum([len(q) for q in qmats])
    n_units = len(offsets) - 1
    out = np.zeros((n_units, len(qmats)), dtype=np.float32)
    u = 0
    while u < n_units:
        v = int(np.searchsorted(offsets, offsets[u] + chunk_tokens, side="right")) - 1
        v = min(max(v, u + 1), n_units)
        sims = big[offsets[u]:offsets[v]] @ q_all.T
        per_unit = np.maximum.reduceat(sims, offsets[u:v] - offsets[u], axis=0)       # (units, q tokens)
        out[u:v] = np.add.reduceat(per_unit, q_off[:-1], axis=1)
        u = v
    if average:
        out /= np.diff(q_off)[None, :].astype(np.float32)
    return out


def load_bgem3_queries() -> dict[str, dict]:
    """K's FlagEmbedding query encodings (npz, J_BGEM3_QNPZ) if given, else the lab encoder's (identical on V0)."""
    if os.environ.get("J_BGEM3_QNPZ"):
        z = np.load(os.environ["J_BGEM3_QNPZ"], allow_pickle=False)
        out = {}
        for i, qid in enumerate(z["query_ids"]):
            a, b = z["sparse_offsets"][i], z["sparse_offsets"][i + 1]
            out[str(qid)] = {"dense": z["dense"][i], "colbert": z["mv"][z["mv_offsets"][i]:z["mv_offsets"][i + 1]],
                             "sparse": {str(t): float(w) for t, w in zip(z["sparse_ids"][a:b], z["sparse_weights"][a:b])}}
        return out
    out = {}
    for line in open(V0 / "rx" / "q" / "q_bgem3.jsonl", encoding="utf-8"):
        r = json.loads(line)
        out[r["query_id"]] = r
    return out


def stage_bgem3():
    root = Path(os.environ["J_BGEM3_ROOT"]) / "derived" / "embeddings"
    units, _ = load_units()
    ids = [u["unit_id"] for u in units]
    pos = {u: i for i, u in enumerate(ids)}
    bench = B.load_benchmark(BENCH_DIR)
    queries = list(bench.queries)
    data = json.load(open(OUT / "rankings.json", encoding="utf-8"))
    rankings, timing = data["rankings"], data["timing"]
    qv = load_bgem3_queries()
    t0 = time.time()
    dense = load_dense(root / "dense", ids)
    log("bgem3 dense", dense.shape, round(time.time() - t0, 1))
    qd = np.stack([l2_normalize(np.asarray(qv[q.query_id]["dense"], dtype=np.float32)) for q in queries])
    s_dense = dense @ qd.T                                            # (units, queries)
    del dense
    t0 = time.time()
    sp = load_sparse(root / "sparse", ids)
    log("bgem3 sparse", sp.shape, sp.nnz, round(time.time() - t0, 1))
    s_sparse = np.zeros_like(s_dense)
    for j, q in enumerate(queries):
        vec = np.zeros(sp.shape[1], dtype=np.float32)
        for tok, w in qv[q.query_id]["sparse"].items():
            if int(tok) < sp.shape[1]:
                vec[int(tok)] = w
        s_sparse[:, j] = sp @ vec
    del sp
    t0 = time.time()
    big, offsets = load_late_stream(root / "multivector", ids)
    timing["bgem3mv_tokens_total"] = int(offsets[-1])
    timing["bgem3mv_tokens_per_unit"] = round(float(offsets[-1]) / len(ids), 1)
    log("bgem3 multivector", big.shape, round(time.time() - t0, 1))
    qm = [l2_normalize(np.asarray(qv[q.query_id]["colbert"], dtype=np.float32)) for q in queries]
    t0 = time.time()
    s_colb = maxsim_batch(big, offsets, qm, average=True)             # FlagEmbedding colbert_score: mean over q tokens
    timing["bgem3mv_full_maxsim_ms"] = round((time.time() - t0) * 1000 / len(queries), 1)
    log("bgem3 maxsim", round(time.time() - t0, 1))
    del big
    s_unified = M3_WEIGHTS["dense"] * s_dense + M3_WEIGHTS["sparse"] * s_sparse + M3_WEIGHTS["colbert"] * s_colb
    new = ("B|dense=bgem3", "S|sparse=bgem3", "L|late=bgem3mv", "U|m3unified", "C|bm25+bgem3",
           "E|bm25+bgem3>late=mlateon", "E|bm25+bgem3>late=bgem3mv", "F3|rrf(bm25,bgem3,mlateon)",
           "E|bm25+granite311>late=bgem3mv", "E|bm25+qwen3>late=bgem3mv", "E|bm25+jnano>late=bgem3mv")
    for name in new:
        rankings[name] = {}
    ml_big, ml_off = load_late(LATE_ALL["mlateon"], ids)
    ml_q = load_qvecs("mlateon")
    for j, q in enumerate(queries):
        qid = q.query_id
        rankings["B|dense=bgem3"][qid] = rank_scores(s_dense[:, j], ids)
        rankings["S|sparse=bgem3"][qid] = rank_scores(s_sparse[:, j], ids)
        rankings["L|late=bgem3mv"][qid] = rank_scores(s_colb[:, j], ids)
        rankings["U|m3unified"][qid] = rank_scores(s_unified[:, j], ids)
        c = fusion.rrf({"bm25": rankings["A|bm25"][qid], "dense": rankings["B|dense=bgem3"][qid]}, k=60)[:DEPTH]
        rankings["C|bm25+bgem3"][qid] = c
        cand = [pos[u] for u, _ in c]
        ml_scores = np.zeros(len(ids), dtype=np.float32)
        ml_scores[cand] = [float(maxsim(ml_q[qid], ml_big[ml_off[k]:ml_off[k + 1]])) for k in cand]
        rankings["E|bm25+bgem3>late=mlateon"][qid] = rank_scores(ml_scores, ids, k=len(cand), subset=cand)
        rankings["E|bm25+bgem3>late=bgem3mv"][qid] = rank_scores(s_colb[:, j], ids, k=len(cand), subset=cand)
        rankings["F3|rrf(bm25,bgem3,mlateon)"][qid] = fusion.rrf(
            {"bm25": rankings["A|bm25"][qid], "dense": rankings["B|dense=bgem3"][qid],
             "late": rankings["L|late=mlateon"][qid]}, k=60)[:DEPTH]
        for d in ("granite311", "qwen3", "jnano"):
            cd = [pos[u] for u, _ in rankings[f"C|bm25+{d}"][qid]]
            rankings[f"E|bm25+{d}>late=bgem3mv"][qid] = rank_scores(s_colb[:, j], ids, k=len(cd), subset=cd)
    dump_atomic({"rankings": rankings, "timing": timing}, OUT / "rankings.json")
    log("bgem3 systems written", len(new))

# ------------------------------------------------------------------ stage: rerank (VKM API → EDGE)
def api_base() -> str:
    if os.environ.get("VKM_API_URL"):
        return os.environ["VKM_API_URL"]
    out = subprocess.run(["ssh", "-G", "core"], capture_output=True, text=True, check=True).stdout
    host = next(l.split()[1] for l in out.splitlines() if l.startswith("hostname "))
    return f"http://{host}:8000"


def api_token() -> str:
    return Path(os.environ["VKM_API_TOKEN_FILE"]).read_text(encoding="utf-8").strip()      # read-only API token


# 413: the API reserves 256 tokens for the v3.5 listwise prompt, but the markup of n passages + the query (twice) takes
# 330-630 tokens, so n saturated candidates overflow 4096; n <= 7 always fits (per-candidate cap 512 tokens)
RERANK_FALLBACK = {24: 12, 12: 7}


def post_retry(client, path: str, body: dict, tries: int = 40, wait_s: float = 15.0):
    """POST with retries on connection errors and 502/503/504 (API redeploys); other statuses are returned."""
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


def rerank_call(client, query: str, units_ranked: list[str], unit_by_id: dict,
                depth: int = RERANK_DEPTH) -> tuple[list[str], dict]:
    cands, passages, cand_unit, order = [], [], {}, []
    for uid in units_ranked:
        u = unit_by_id[uid]
        cid = u["object_ids"][0]
        if cid in cand_unit:
            continue
        cand_unit[cid] = uid
        cands.append(cid)
        if u["kind"] == "BLOCK_GROUP" and len(u["object_ids"]) > 1:
            passages.append({"candidate_id": cid, "object_ids": u["object_ids"][:20]})
        if len(cands) >= depth:
            break
    body = {"query": query, "candidate_ids": cands, "top_n": len(cands), "passages": passages}
    t0 = time.time()
    r = post_retry(client, "/v1/rerank/text", body)
    dt = time.time() - t0
    if r.status_code == 413 and depth in RERANK_FALLBACK:
        ranked, meta = rerank_call(client, query, units_ranked, unit_by_id, RERANK_FALLBACK[depth])
        meta.setdefault("fallback_from", depth)
        meta["latency_s"] = round(meta["latency_s"] + dt, 2)
        return ranked, meta
    if r.status_code != 200:
        return [], {"status": r.status_code, "error": r.text[:300], "latency_s": dt}
    data = r.json()
    ranked = [cand_unit[i["envelope"]["object_id"]] for i in sorted(data.get("items", []),
                                                                     key=lambda i: i["record"]["rank"])]
    service = (data.get("item") or {}).get("record", {})
    meta = {"status": 200, "latency_s": round(dt, 2), "n_sent": len(cands), "n_ranked": len(ranked), "depth": depth,
            "rejected": len(service.get("rejected", []) or []),
            "rejected_codes": sorted({r.get("code", "?") for r in service.get("rejected", []) or []}),
            "rejected_kinds": sorted({unit_by_id[cand_unit[r["id"]]]["kind"] for r in service.get("rejected", []) or []
                                      if r.get("id") in cand_unit}),
            "truncated": sum(1 for i in data.get("items", []) if i["record"].get("truncated"))}
    return ranked, meta


def stage_rerank(systems: list[str], only_covered: bool = True):
    import httpx

    data = json.load(open(OUT / "rankings.json", encoding="utf-8"))
    rankings = data["rankings"]
    units, _ = load_units()
    unit_by_id = {u["unit_id"]: u for u in units}
    covered = set(json.load(open(V0 / "prepare.json", encoding="utf-8"))["covered_queries"])
    bench = B.load_benchmark(BENCH_DIR)
    qtext = {q.query_id: q.text for q in bench.queries}
    text_q = {q.query_id for q in bench.queries if q.track == "text"}
    path = OUT / "rerank.json"
    done = json.load(open(path, encoding="utf-8")) if path.is_file() else {"rankings": {}, "meta": {}}
    client = httpx.Client(base_url=api_base(), headers={"Authorization": f"Bearer {api_token()}"}, timeout=400,
                          trust_env=False)
    for sysname in systems:
        out_name = f"R|{sysname}"
        done["rankings"].setdefault(out_name, {})
        done["meta"].setdefault(out_name, {})
        todo = [q for q in rankings[sysname] if (not only_covered or q in covered) and q in text_q
                and q not in done["rankings"][out_name]]
        if os.environ.get("J_RR_LIMIT"):
            todo = todo[:int(os.environ["J_RR_LIMIT"])]
        log("rerank", sysname, len(todo), "queries")

        def work(qid):
            ranked = [u for u, _ in rankings[sysname][qid]]
            new, meta = rerank_call(client, qtext[qid], ranked, unit_by_id)
            if not new:
                meta["fail_open"] = True          # production behaviour: first-stage order is kept
                return qid, ranked, meta
            rest = [u for u in ranked if u not in set(new)]
            return qid, new + rest, meta

        with ThreadPoolExecutor(max_workers=int(os.environ.get("J_RR_WORKERS", "2"))) as ex:
            for n, (qid, ranked, meta) in enumerate(ex.map(work, todo), 1):
                done["meta"][out_name][qid] = meta
                if ranked is not None:
                    done["rankings"][out_name][qid] = [[u, 0.0] for u in ranked]
                if n % 10 == 0:
                    dump_atomic(done, path)
                    log(out_name, n, "/", len(todo), meta)
        dump_atomic(done, path)
    client.close()


# ------------------------------------------------------------------ stage: visual (m0 via VKM API)
VISUAL_DEPTH = 8


def visual_call(client, query: str, units_ranked: list[str], unit_by_id: dict) -> tuple[list[str], dict]:
    """≤ 8 image candidates: FIGURE units by their figure id, every other unit by its page image."""
    cands, cand_page = [], {}
    for uid in units_ranked:
        u = unit_by_id[uid]
        cid = u["object_ids"][0] if u["kind"] == "FIGURE" else u["page_id"]
        if cid in cand_page:
            continue
        cand_page[cid] = u["page_id"]
        cands.append(cid)
        if len(cands) >= VISUAL_DEPTH:
            break
    t0 = time.time()
    r = post_retry(client, "/v1/rerank/visual", {"query": query, "candidate_ids": cands, "top_n": len(cands)})
    dt = time.time() - t0
    if r.status_code != 200:
        return [], {"status": r.status_code, "error": r.text[:300], "latency_s": dt}
    data = r.json()
    items = sorted(data.get("items", []), key=lambda i: i["record"]["rank"])
    ranked_pages = []
    for i in items:
        p = cand_page.get(i["envelope"]["object_id"])
        if p and p not in ranked_pages:
            ranked_pages.append(p)
    service = (data.get("item") or {}).get("record", {})
    meta = {"status": 200, "latency_s": round(dt, 2), "n_sent": len(cands), "n_ranked": len(items),
            "n_figures": sum(1 for c in cands if c not in cand_page.values()),
            "rejected": len(service.get("rejected", []) or []),
            "rejected_codes": sorted({r.get("code", "?") for r in service.get("rejected", []) or []})}
    return ranked_pages, meta


def stage_visual(systems: list[str]):
    import httpx

    rankings = json.load(open(OUT / "rankings.json", encoding="utf-8"))["rankings"]
    units, _ = load_units()
    unit_by_id = {u["unit_id"]: u for u in units}
    covered = set(json.load(open(V0 / "prepare.json", encoding="utf-8"))["covered_queries"])
    bench = B.load_benchmark(BENCH_DIR)
    vq = [q for q in bench.queries if q.track == "visual" and q.query_id in covered]
    path = OUT / "visual.json"
    done = json.load(open(path, encoding="utf-8")) if path.is_file() else {"rankings": {}, "meta": {}}
    client = httpx.Client(base_url=api_base(), headers={"Authorization": f"Bearer {api_token()}"}, timeout=700,
                          trust_env=False)
    for sysname in systems:
        out_name = f"V|{sysname}"
        done["rankings"].setdefault(out_name, {})
        done["meta"].setdefault(out_name, {})
        todo = [q for q in vq if q.query_id not in done["rankings"][out_name]]
        if os.environ.get("J_RR_LIMIT"):
            todo = todo[:int(os.environ["J_RR_LIMIT"])]
        log("visual rerank", sysname, len(todo), "queries")
        for n, q in enumerate(todo, 1):
            ranked = [u for u, _ in rankings[sysname][q.query_id]]
            pages, meta = visual_call(client, q.text, ranked, unit_by_id)
            done["meta"][out_name][q.query_id] = meta
            if pages:
                # pages of the visual top go first (m0 order); the rest keeps the first-stage order (unit level)
                first_units = {}
                for u in ranked:
                    first_units.setdefault(unit_by_id[u]["page_id"], u)
                head = [first_units[p] for p in pages if p in first_units]
                rest = [u for u in ranked if unit_by_id[u]["page_id"] not in set(pages)]
                done["rankings"][out_name][q.query_id] = [[u, 0.0] for u in head + rest]
            dump_atomic(done, path)
            log(out_name, n, "/", len(todo), meta)
    client.close()


# ------------------------------------------------------------------ stage: eval
def canon_pages() -> set[str]:
    path = OUT / "canon_pages.json"
    if not path.is_file():
        from vkm_corpus.retrieval_lab.canon import CanonReader

        reader = CanonReader.from_duckdb(V0 / "vkm_corpus_canary.duckdb")
        json.dump(sorted({r["page_id"] for r in reader.pages()}), open(path, "w", encoding="utf-8"))
    return set(json.load(open(path, encoding="utf-8")))


def stage_eval():
    first = json.load(open(OUT / "rankings.json", encoding="utf-8"))
    rankings = dict(first["rankings"])
    meta_all = {}
    for extra in ("rerank.json", "visual.json"):
        if (OUT / extra).is_file():
            rr = json.load(open(OUT / extra, encoding="utf-8"))
            rankings.update(rr["rankings"])
            meta_all.update(rr["meta"])
    units, _ = load_units()
    unit_page = {u["unit_id"]: u["page_id"] for u in units}
    bench = B.load_benchmark(BENCH_DIR)
    prep = json.load(open(V0 / "prepare.json", encoding="utf-8"))
    covered = set(prep["covered_queries"])
    all_pages = canon_pages()
    judged = {q: {p: g for p, g in j.items() if p in all_pages} for q, j in bench.judgments(level="PAGE").items()}
    hn = bench.hard_negative_ids("PAGE")
    results = {"snapshot": prep["snapshot"], "n_units": len(units), "n_canon_pages": len(all_pages),
               "benchmark": {"queries": len(bench.queries), "qrels": len(bench.qrels)},
               "timing": first.get("timing", {}), "tracks": {}}
    for track in ("text", "visual"):
        queries = [q for q in bench.queries if q.track == track and q.query_id in covered]
        qids = [q.query_id for q in queries]
        test_ids = [q for q in qids if bench.splits.get(q) == "test"]
        slices = metrics.group_queries(queries, "slices")
        cats = metrics.group_queries(queries, "category")
        tr = {"n_queries_covered": len(qids), "n_test_covered": len(test_ids),
              "n_not_covered": sum(1 for q in bench.queries if q.track == track) - len(qids), "systems": {}}
        evals = {}
        for name, per_q in rankings.items():
            rq = {q: metrics.to_pages([u for u, _ in per_q.get(q, [])], unit_page) for q in qids if q in per_q}
            if not rq:
                continue
            if len(rq) < len(qids):
                tr["systems"][name] = {"status": "PARTIAL", "n": len(rq)}
                continue
            ev = metrics.evaluate(rq, judged, hard_negatives=hn, query_ids=qids)
            ev1 = metrics.evaluate(rq, judged, threshold=1, query_ids=qids)
            evals[name] = ev
            res = {"status": "OK", "overall": ev.mean(), "lenient_grade1": ev1.mean(), "test": ev.mean(test_ids),
                   "slices": ev.by_group(slices), "categories": ev.by_group(cats), "excluded": ev.excluded,
                   "per_query": {q: {k: round(v, 4) for k, v in m.items()} for q, m in ev.per_query.items()}}
            meta = meta_all.get(name)
            if meta:
                ok = [m for q, m in meta.items() if q in qids and m.get("status") == 200]
                lat = [m["latency_s"] for m in ok]
                res["rerank"] = {"calls_ok": len(ok),
                                 "errors": sum(1 for q, m in meta.items() if q in qids and m.get("status") != 200),
                                 "latency_s_p50": float(np.percentile(lat, 50)) if lat else None,
                                 "latency_s_p95": float(np.percentile(lat, 95)) if lat else None,
                                 "rejected_total": sum(m.get("rejected", 0) for m in ok),
                                 "sent_total": sum(m.get("n_sent", 0) for m in ok),
                                 "truncated_total": sum(m.get("truncated", 0) for m in ok),
                                 "fallback_depth12": sum(1 for m in ok if m.get("depth") == 12),
                                 "fallback_depth7": sum(1 for m in ok if m.get("depth") == 7),
                                 "fail_open": sum(1 for q, m in meta.items() if q in qids and m.get("fail_open")),
                                 "rejected_codes": sorted({c for m in ok for c in m.get("rejected_codes", [])})}
            tr["systems"][name] = res
        base = evals.get("A|bm25")
        for name, ev in evals.items():
            if name == "A|bm25" or base is None:
                continue
            sig = {}
            for metric in ("ndcg@10", "recall@10", "recall@50", "mrr@10"):
                sig[metric] = metrics.compare(ev, base, metric)
                sig[metric + "|test"] = metrics.compare(ev, base, metric, test_ids)
            tr["systems"][name]["vs_bm25"] = sig
            src = name.split("|", 1)[1] if name[:2] in ("R|", "V|") else None
            if src and src in evals:
                tr["systems"][name]["vs_first_stage"] = {m: metrics.compare(ev, evals[src], m)
                                                         for m in ("ndcg@10", "recall@10", "mrr@10")}
        results["tracks"][track] = tr
        rows = sorted(((n, r["overall"]) for n, r in tr["systems"].items() if r.get("status") == "OK"),
                      key=lambda x: -x[1]["ndcg@10"])
        log(f"== {track}: {len(qids)} covered queries ({len(test_ids)} test)")
        for n, o in rows:
            v = tr["systems"][n].get("vs_bm25", {}).get("ndcg@10", {})
            log(f"{n:45s} nDCG@10 {o['ndcg@10']:.3f} R@10 {o['recall@10']:.3f} R@50 {o['recall@50']:.3f} "
                f"MRR {o['mrr@10']:.3f} j@10 {o['judged@10']:.2f} d {v.get('delta', 0):+.3f} "
                f"p {v.get('p_value', 1):.4f}")
    json.dump(results, open(OUT / "results.json", "w", encoding="utf-8"), indent=1, ensure_ascii=False,
              default=float)


if __name__ == "__main__":
    stage = sys.argv[1]
    if stage == "first":
        stage_first()
    elif stage == "rerank":
        stage_rerank(sys.argv[2].split(","))
    elif stage == "bgem3":
        stage_bgem3()
    elif stage == "visual":
        stage_visual(sys.argv[2].split(","))
    elif stage == "eval":
        stage_eval()
