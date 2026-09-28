"""Experiment runner of the lab: canon snapshot → units → encodings (cached) → pipelines → metrics, traces, receipts.

Output directory (git-ignored, ``--out``)::

    run.json            configuration, snapshot, model revisions, environment, code revision
    units.parquet       unit_id, kind, source_id, page_id, object_ids, text_sha256, n_chars, flags (no text)
    cache/<model>/<key>/   documents and queries (doc_* / query_*: dense, tokens + index, sparse) keyed by
                        text sha256; a fully cached run never loads the model
    results/<system>.jsonl ranked units per query (+ status)
    trace/<system>.jsonl   per-query rank trace of the final list (§54)
    metrics.json        per system: overall, per split, per slice, per category; exclusions; judged coverage
    pool.tsv            top-10 pages per system not yet judged (for pooling, design §12)
    receipt.json        hashes of the files above

A *system* is ``<pipeline>|bm25=<impl>|dense=<key[@dim]>|sparse=<key>|late=<key[@dim]>``.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np

from vkm_corpus.retrieval_lab import LAB_VERSION, metrics
from vkm_corpus.retrieval_lab.bench import Benchmark
from vkm_corpus.retrieval_lab.bm25 import LocalBM25
from vkm_corpus.retrieval_lab.encoders import BGEM3Encoder, FakeEncoder, ModelSpec, PplxContextEncoder, make_encoder
from vkm_corpus.retrieval_lab.pipelines import (DEFAULT_PIPELINES, DenseRetriever, LateRetriever, M3UnifiedRetriever,
                                                PipelineConfig, SparseRetriever, TextRetriever, run_pipeline)
from vkm_corpus.retrieval_lab.signatures import cache_key, doc_signature, query_signature
from vkm_corpus.retrieval_lab.units import Unit, UnitConfig, ctx_windows, render
from vkm_corpus.retrieval_lab.vectors import DenseIndex, LateIndex, SparseIndex, l2_normalize


# ---------------------------------------------------------------- encoding cache
class VectorCache:
    """Encodings of texts under one signature; rows are keyed by the sha256 of the encoded text. ``part`` separates
    documents (``doc``) from queries (``query``), so a run whose encodings are all cached never loads the model —
    this is how encodings made in another environment (e.g. the PyLate reference venv) are reused."""

    def __init__(self, root: Path, model_key: str, signature: dict[str, Any]) -> None:
        self.dir = root / "cache" / model_key / cache_key(signature)
        self.dir.mkdir(parents=True, exist_ok=True)
        sig_path = self.dir / "signature.json"
        if not sig_path.is_file():
            sig_path.write_text(json.dumps(signature, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8")

    @staticmethod
    def key(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    # dense ---------------------------------------------------------------
    def load_dense(self, part: str = "doc") -> dict[str, np.ndarray]:
        idx, mat = self.dir / f"{part}_dense_index.json", self.dir / f"{part}_dense.npy"
        if idx.is_file() and mat.is_file():
            index = json.loads(idx.read_text(encoding="utf-8"))
            matrix = np.load(mat)
            return {k: matrix[i] for k, i in index.items()}
        return {}

    def save_dense(self, items: dict[str, np.ndarray], part: str = "doc") -> None:
        keys = sorted(items)
        matrix = np.stack([np.asarray(items[k], dtype=np.float32) for k in keys]) if keys else np.zeros((0, 0))
        np.save(self.dir / f"{part}_dense.npy", matrix.astype(np.float32))
        (self.dir / f"{part}_dense_index.json").write_text(json.dumps({k: i for i, k in enumerate(keys)}),
                                                          encoding="utf-8")

    # multi-vector --------------------------------------------------------
    def load_tokens(self, part: str = "doc") -> dict[str, np.ndarray]:
        meta = self.dir / f"{part}_tokens_index.json"
        if not meta.is_file():
            return {}
        info = json.loads(meta.read_text(encoding="utf-8"))
        dtype = np.float16 if info["dtype"] == "float16" else np.float32
        path = self.dir / f"{part}_tokens.{info['dtype']}"
        if not info["offsets"] or path.stat().st_size == 0:
            return {}
        flat = np.memmap(path, dtype=dtype, mode="r").reshape(-1, info["dim"])
        return {k: flat[a:b] for k, (a, b) in info["offsets"].items()}

    def save_tokens(self, items: dict[str, np.ndarray], dtype: str = "float32", part: str = "doc") -> None:
        keys = sorted(items)
        dim = int(np.asarray(items[keys[0]]).shape[1]) if keys else 0
        offsets, pos = {}, 0
        np_dtype = np.float16 if dtype == "float16" else np.float32
        tmp = self.dir / f"{part}_tokens.{dtype}.tmp"
        with open(tmp, "wb") as f:
            for k in keys:
                arr = np.ascontiguousarray(items[k], dtype=np_dtype)
                f.write(arr.tobytes())
                offsets[k] = (pos, pos + len(arr))
                pos += len(arr)
        tmp.replace(self.dir / f"{part}_tokens.{dtype}")
        (self.dir / f"{part}_tokens_index.json").write_text(json.dumps({"dtype": dtype, "dim": dim,
                                                                        "offsets": offsets}), encoding="utf-8")

    # sparse --------------------------------------------------------------
    def load_sparse(self, part: str = "doc") -> dict[str, dict[str, float]]:
        p = self.dir / f"{part}_sparse.jsonl"
        if not p.is_file():
            return {}
        out = {}
        for line in p.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            out[rec["k"]] = rec["w"]
        return out

    def save_sparse(self, items: dict[str, dict[str, float]], part: str = "doc") -> None:
        with open(self.dir / f"{part}_sparse.jsonl", "w", encoding="utf-8") as f:
            for k in sorted(items):
                f.write(json.dumps({"k": k, "w": items[k]}, separators=(",", ":")) + "\n")


# ---------------------------------------------------------------- run config
@dataclass
class RunConfig:
    out_dir: Path
    dense: list[str] = field(default_factory=list)          # model keys, optionally key@dim
    late: list[str] = field(default_factory=list)
    sparse: list[str] = field(default_factory=list)          # learned-sparse model keys (BGE-M3 lexical, FAKE)
    m3: list[str] = field(default_factory=list)              # unified dense+sparse+multi-vector models (BGE-M3)
    pipelines: list[str] = field(default_factory=lambda: ["A", "B"])
    context_variant: str = "A"
    unit_config: UnitConfig = field(default_factory=UnitConfig)
    device: str = "cpu"
    compute_precision: str = "fp32"
    threads: int | None = 8
    batch_size: int = 16
    bm25: str = "local"
    splits: tuple[str, ...] = ("train", "dev", "test")
    track: str = "text"
    token_dtype: str = "float32"


def _split_key(ref: str) -> tuple[str, int | None]:
    key, _, dim = ref.partition("@")
    return key, (int(dim) if dim else None)


class LabRun:
    def __init__(self, cfg: RunConfig, bench: Benchmark, units: list[Unit], specs: dict[str, ModelSpec], *,
                 models_dir: str | Path | None = None, meta: dict[str, Any] | None = None,
                 rerank_texts: dict[str, str] | None = None, reranker: Any = None,
                 source_meta: dict[str, Any] | None = None, page_labels: dict[str, str] | None = None,
                 bm25_backend: Any = None, encoder_factory: Any = None) -> None:
        self.cfg, self.bench, self.units, self.specs = cfg, bench, units, specs
        self.models_dir, self.meta = models_dir, dict(meta or {})
        self.rerank_texts, self.reranker = rerank_texts or {}, reranker
        self.source_meta, self.page_labels = source_meta or {}, page_labels or {}
        self.bm25_backend = bm25_backend
        self.encoder_factory = encoder_factory or (lambda spec: make_encoder(
            spec, models_dir, device=cfg.device, precision=cfg.compute_precision, batch_size=cfg.batch_size,
            threads=cfg.threads))
        self.unit_by_id = {u.unit_id: u for u in units}
        self.unit_page = {u.unit_id: u.page_id for u in units}
        self.queries = [q for q in bench.queries if q.track == cfg.track]
        self.qtext = {q.query_id: q.text for q in self.queries}
        self.doc_texts = [render(u, cfg.context_variant, self.source_meta.get(u.source_id),
                                 self.page_labels.get(u.page_id or "")) for u in units]
        self.encode_log: list[dict[str, Any]] = []
        self._m3_memo: dict[str, dict[str, Any]] = {}
        cfg.out_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ encodings
    def _lazy_encoder(self, spec: ModelSpec) -> "_Lazy":
        return _Lazy(lambda: self.encoder_factory(spec))

    def _query_texts(self) -> tuple[list[str], list[str]]:
        texts = [self.qtext[q.query_id] for q in self.queries]
        return texts, [VectorCache.key(t) for t in texts]

    def _encode_dense(self, key: str) -> tuple[np.ndarray, dict[str, np.ndarray]]:
        spec = self.specs[key]
        sig = doc_signature(spec, mode="dense", unit_rule="vkm-units-v1", context_variant=self.cfg.context_variant,
                            compute_precision=self.cfg.compute_precision)
        cache = VectorCache(self.cfg.out_dir, key, sig)
        enc = self._lazy_encoder(spec)
        docs = cache.load_dense("doc")
        keys = [VectorCache.key(t) for t in self.doc_texts]
        missing = [i for i, k in enumerate(keys) if k not in docs]
        t_docs = 0.0
        if missing:
            t0 = time.perf_counter()
            e = enc.get()
            if isinstance(e, PplxContextEncoder):
                new = self._encode_ctx(e, missing)
            else:
                new = e.encode_docs([self.doc_texts[i] for i in missing])
            t_docs = time.perf_counter() - t0
            docs.update({keys[i]: np.asarray(v) for i, v in zip(missing, new)})
            cache.save_dense(docs, "doc")
        qtexts, qkeys = self._query_texts()
        queries = cache.load_dense("query")
        qmissing = [i for i, k in enumerate(qkeys) if k not in queries]
        t_q = 0.0
        if qmissing:
            t0 = time.perf_counter()
            new = enc.get().encode_queries([qtexts[i] for i in qmissing])
            t_q = time.perf_counter() - t0
            queries.update({qkeys[i]: np.asarray(v) for i, v in zip(qmissing, new)})
            cache.save_dense(queries, "query")
        self.encode_log.append({"model": key, "mode": "dense", "encoded_docs": len(missing), "docs_s": t_docs,
                                "encoded_queries": len(qmissing), "queries_s": t_q, "doc_signature": sig,
                                "query_signature": query_signature(spec, mode="dense",
                                                                   compute_precision=self.cfg.compute_precision),
                                **enc.runtime()})
        enc.close()
        return (np.stack([docs[k] for k in keys]),
                {q.query_id: queries[k] for q, k in zip(self.queries, qkeys)})

    def _encode_ctx(self, enc: PplxContextEncoder, missing: list[int]) -> np.ndarray:
        """pplx-context: encode whole windows of consecutive units; keep the vectors of the missing units."""
        pos = {u.unit_id: i for i, u in enumerate(self.units)}
        wanted = {self.units[i].unit_id for i in missing}
        windows = [w for w in ctx_windows(self.units) if any(u.unit_id in wanted for u in w)]
        vecs = enc.encode_windows([[self.doc_texts[pos[u.unit_id]] for u in w] for w in windows])
        by_unit = {u.unit_id: v for w, ws in zip(windows, vecs) for u, v in zip(w, ws)}
        return np.stack([by_unit[self.units[i].unit_id] for i in missing])

    @staticmethod
    def _m3_outputs(e: Any, texts: list[str], queries: bool) -> dict[str, list[Any]]:
        if isinstance(e, BGEM3Encoder):
            return e.encode_all_queries(texts) if queries else e.encode_all_docs(texts)
        dense = e.encode_queries(texts) if queries else e.encode_docs(texts)          # FakeEncoder
        return {"dense": list(dense), "sparse": e.sparse(texts), "multivector": e.tokens(texts)}

    def _encode_m3(self, key: str) -> dict[str, Any]:
        """BGE-M3 (or FAKE) dense + sparse + multi-vector for documents and queries, all cached (one encoder load)."""
        if key in self._m3_memo:
            return self._m3_memo[key]
        spec = self.specs[key]
        sig = doc_signature(spec, mode="m3", unit_rule="vkm-units-v1", context_variant=self.cfg.context_variant,
                            compute_precision=self.cfg.compute_precision)
        cache = VectorCache(self.cfg.out_dir, key, sig)
        enc = self._lazy_encoder(spec)
        result: dict[str, Any] = {}
        counts = {}
        qtexts, qkeys = self._query_texts()
        for part, texts, keys in (("doc", self.doc_texts, [VectorCache.key(t) for t in self.doc_texts]),
                                  ("query", qtexts, qkeys)):
            dense, sparse, tokens = cache.load_dense(part), cache.load_sparse(part), cache.load_tokens(part)
            missing = [i for i, k in enumerate(keys) if k not in dense or k not in sparse or k not in tokens]
            counts[part] = len(missing)
            if missing:
                out = self._m3_outputs(enc.get(), [texts[i] for i in missing], queries=(part == "query"))
                tokens = {k: np.asarray(v) for k, v in tokens.items()}
                for i, d, sp, mv in zip(missing, out["dense"], out["sparse"], out["multivector"]):
                    dense[keys[i]], sparse[keys[i]], tokens[keys[i]] = np.asarray(d), sp, np.asarray(mv)
                cache.save_dense(dense, part)
                cache.save_sparse(sparse, part)
                cache.save_tokens(tokens, self.cfg.token_dtype, part)
                tokens = cache.load_tokens(part)
            result[part] = (keys, dense, sparse, tokens)
        enc.close()
        self.encode_log.append({"model": key, "mode": "m3", "encoded_docs": counts["doc"],
                                "encoded_queries": counts["query"], "doc_signature": sig})
        dkeys, dd, ds, dt = result["doc"]
        qkeys, qd, qs, qt = result["query"]
        ids = [q.query_id for q in self.queries]
        out = {"doc_dense": np.stack([dd[k] for k in dkeys]), "doc_sparse": [ds[k] for k in dkeys],
               "doc_tokens": [np.asarray(dt[k], dtype=np.float32) for k in dkeys],
               "q_dense": {i: qd[k] for i, k in zip(ids, qkeys)}, "q_sparse": {i: qs[k] for i, k in zip(ids, qkeys)},
               "q_tokens": {i: np.asarray(qt[k], dtype=np.float32) for i, k in zip(ids, qkeys)}}
        self._m3_memo[key] = out
        return out

    def _encode_late(self, key: str) -> tuple[list[np.ndarray], dict[str, np.ndarray]]:
        spec = self.specs[key]
        if spec.family in ("bge_m3", "fake"):
            out = self._encode_m3(key)
            return out["doc_tokens"], out["q_tokens"]
        sig = doc_signature(spec, mode="multivector", unit_rule="vkm-units-v1",
                            context_variant=self.cfg.context_variant, compute_precision=self.cfg.compute_precision)
        cache = VectorCache(self.cfg.out_dir, key, sig)
        enc = self._lazy_encoder(spec)
        parts = {}
        counts = {}
        qtexts, qkeys = self._query_texts()
        for part, texts, keys in (("doc", self.doc_texts, [VectorCache.key(t) for t in self.doc_texts]),
                                  ("query", qtexts, qkeys)):
            tokens = cache.load_tokens(part)
            missing = [i for i, k in enumerate(keys) if k not in tokens]
            counts[part] = len(missing)
            if missing:
                e = enc.get()
                chunk = [texts[i] for i in missing]
                new = e.encode_queries(chunk) if part == "query" else e.encode_docs(chunk)
                merged = {k: np.asarray(v) for k, v in tokens.items()}
                merged.update({keys[i]: np.asarray(v) for i, v in zip(missing, new)})
                cache.save_tokens(merged, self.cfg.token_dtype, part)
                tokens = cache.load_tokens(part)
            parts[part] = [np.asarray(tokens[k], dtype=np.float32) for k in keys]
        enc.close()
        self.encode_log.append({"model": key, "mode": "multivector", "encoded_docs": counts["doc"],
                                "encoded_queries": counts["query"], "doc_signature": sig})
        return parts["doc"], {q.query_id: v for q, v in zip(self.queries, parts["query"])}

    def encode_only(self, keys: Sequence[str]) -> list[dict[str, Any]]:
        """Fill the encoding caches of the given models without running pipelines (e.g. in the PyLate reference
        venv); a later run in the main venv reuses them."""
        for ref in keys:
            key, _ = _split_key(ref)
            spec = self.specs[key]
            if spec.family in ("bge_m3", "fake"):
                self._encode_m3(key)
            elif spec.family == "multivector":
                self._encode_late(key)
            else:
                self._encode_dense(key)
        return self.encode_log

    # ------------------------------------------------------------ retrievers
    def bm25(self) -> TextRetriever:
        backend = self.bm25_backend or LocalBM25.build([u.unit_id for u in self.units], self.doc_texts)
        return TextRetriever(backend, self.qtext)

    def dense(self, ref: str) -> DenseRetriever:
        key, dim = _split_key(ref)
        spec = self.specs[key]
        if spec.family in ("bge_m3", "fake"):
            out = self._encode_m3(key)
            doc, qv = out["doc_dense"], out["q_dense"]
        else:
            doc, qv = self._encode_dense(key)
        index = DenseIndex.build([u.unit_id for u in self.units], doc, dim=dim)
        return DenseRetriever(index, {q: l2_normalize(np.asarray(v)) for q, v in qv.items()})

    def sparse_ret(self, ref: str) -> SparseRetriever:
        out = self._encode_m3(_split_key(ref)[0])
        return SparseRetriever(SparseIndex.build([u.unit_id for u in self.units], out["doc_sparse"]), out["q_sparse"])

    def late_ret(self, ref: str) -> LateRetriever:
        key, dim = _split_key(ref)
        docs, qv = self._encode_late(key)
        index = LateIndex.build([u.unit_id for u in self.units], docs, dim=dim)
        return LateRetriever(index, qv)

    # ------------------------------------------------------------ systems
    def systems(self) -> list[tuple[str, PipelineConfig, dict[str, str]]]:
        """(system name, pipeline, roles) for the requested pipelines × model options; a pipeline whose role has no
        model option is skipped."""
        options = {"dense": self.cfg.dense, "sparse": self.cfg.sparse, "late": self.cfg.late, "m3": self.cfg.m3}
        out = []
        for name in self.cfg.pipelines:
            p = DEFAULT_PIPELINES[name]
            need = list(p.first) + (["late"] if p.late else [])
            combos: list[dict[str, str]] = [{}]
            for role in ("dense", "sparse", "late", "m3"):
                if role in need:
                    combos = [{**c, role: o} for c in combos for o in options[role]]
            for roles in combos:
                parts = [name] + ([f"bm25={self.cfg.bm25}"] if "bm25" in need else []) +                         [f"{r}={roles[r]}" for r in ("dense", "sparse", "late", "m3") if r in roles]
                out.append(("|".join(parts), p, roles))
        return out

    def run(self) -> dict[str, Any]:
        t_start = time.time()
        systems = self.systems()
        cache: dict[tuple[str, str], Any] = {}

        def retriever(role: str, ref: str) -> Any:
            if (role, ref) not in cache:
                cache[(role, ref)] = {"dense": self.dense, "sparse": self.sparse_ret, "late": self.late_ret}[role](ref)
            return cache[(role, ref)]

        bm25 = self.bm25()
        results: dict[str, dict[str, Any]] = {}
        (self.cfg.out_dir / "results").mkdir(exist_ok=True)
        (self.cfg.out_dir / "trace").mkdir(exist_ok=True)
        for label, pcfg, roles in systems:
            rets: dict[str, Any] = {"bm25": bm25}
            for role in ("dense", "sparse", "late"):
                if role in roles:
                    rets[role] = retriever(role, roles[role])
            if "m3" in roles:
                d, s, l_ = retriever("dense", roles["m3"]), retriever("sparse", roles["m3"]), retriever("late", roles["m3"])
                rets["m3"] = M3UnifiedRetriever(d, s, l_)
            per_query = {}
            fname = hashlib.sha256(label.encode()).hexdigest()[:12]
            with open(self.cfg.out_dir / "results" / f"{fname}.jsonl", "w", encoding="utf-8") as fr, \
                    open(self.cfg.out_dir / "trace" / f"{fname}.jsonl", "w", encoding="utf-8") as ft:
                for q in self.queries:
                    res = run_pipeline(pcfg, q.query_id, q.text, rets, reranker=self.reranker,
                                       rerank_text=(self.rerank_texts.get if self.rerank_texts else None))
                    per_query[q.query_id] = res
                    fr.write(json.dumps({"system": label, "query_id": q.query_id, "status": res.status,
                                         "ranked": [[u, round(s, 6)] for u, s in res.ranked],
                                         "timings_ms": res.timings_ms, "notes": res.notes}) + "\n")
                    ft.write(json.dumps({"system": label, "query_id": q.query_id, "trace": res.trace}) + "\n")
            results[label] = {"file": fname, "per_query": per_query}
        report = self.evaluate(results)
        report["run"] = {"lab_version": LAB_VERSION, "started": t_start, "seconds": time.time() - t_start,
                         "systems": [s[0] for s in systems], "encode_log": self.encode_log, **self.meta}
        (self.cfg.out_dir / "metrics.json").write_text(json.dumps(report, indent=1, ensure_ascii=False, default=str),
                                                       encoding="utf-8")
        return report

    # ------------------------------------------------------------ evaluation
    def evaluate(self, results: dict[str, dict[str, Any]]) -> dict[str, Any]:
        judged = self.bench.judgments(level="PAGE")
        hn = self.bench.hard_negative_ids("PAGE")
        by_split = {s: [q.query_id for q in self.queries if self.bench.splits.get(q.query_id) == s]
                    for s in self.cfg.splits}
        slices = metrics.group_queries(self.queries, "slices")
        cats = metrics.group_queries(self.queries, "category")
        out: dict[str, Any] = {"systems": {}, "units": len(self.units), "queries": len(self.queries)}
        pool: dict[tuple[str, str], set[str]] = defaultdict(set)
        for label, res in results.items():
            statuses = {r.status for r in res["per_query"].values()}
            if statuses == {"NOT_RUN"}:
                out["systems"][label] = {"status": "NOT_RUN",
                                         "notes": next(iter(res["per_query"].values())).notes if res["per_query"] else []}
                continue
            rankings = {q: metrics.to_pages([u for u, _ in r.ranked], self.unit_page)
                        for q, r in res["per_query"].items()}
            for q, pages in rankings.items():
                for p in pages[:10]:
                    if p not in judged.get(q, {}):
                        pool[(q, p)].add(label)
            ev = metrics.evaluate(rankings, judged, hard_negatives=hn)
            ev_lenient = metrics.evaluate(rankings, judged, threshold=1)
            out["systems"][label] = {
                "status": "OK", "file": res["file"], "overall": ev.mean(), "lenient_overall": ev_lenient.mean(),
                "splits": ev.by_group(by_split), "slices": ev.by_group(slices), "categories": ev.by_group(cats),
                "excluded_queries": ev.excluded,
                "latency_ms": _latency(res["per_query"].values()),
            }
        with open(self.cfg.out_dir / "pool.tsv", "w", encoding="utf-8") as f:
            f.write("query_id\tpage_id\tn_systems\n")
            for (q, p), labels in sorted(pool.items()):
                f.write(f"{q}\t{p}\t{len(labels)}\n")
        return out


class _Lazy:
    """Load an encoder only when some text actually needs encoding."""

    def __init__(self, factory: Any) -> None:
        self.factory, self.enc = factory, None

    def get(self) -> Any:
        if self.enc is None:
            self.enc = self.factory()
        return self.enc

    def runtime(self) -> dict[str, Any]:
        return self.enc.runtime() if self.enc is not None and hasattr(self.enc, "runtime") else {"model_loaded": False}

    def close(self) -> None:
        if self.enc is not None:
            self.enc.close()
            self.enc = None


def _latency(results: Iterable[Any]) -> dict[str, float]:
    stages: dict[str, list[float]] = defaultdict(list)
    for r in results:
        for k, v in r.timings_ms.items():
            stages[k].append(v)
    return {k: float(np.percentile(v, 50)) for k, v in sorted(stages.items())} | \
           {f"{k}_p95": float(np.percentile(v, 95)) for k, v in sorted(stages.items())}


def rerank_texts_for(units: Sequence[Unit], canon_rerank: dict[str, str]) -> dict[str, str]:
    """Reranker passage of a unit: the unit text for block groups (= normalized texts of its blocks), the canonical
    ``rerank_text`` (rule rerank_text_v1) for single-object units."""
    out = {}
    for u in units:
        if u.kind == "BLOCK_GROUP":
            out[u.unit_id] = u.text
        else:
            out[u.unit_id] = canon_rerank.get(u.object_ids[0]) or u.text
    return out


def fake_specs() -> dict[str, ModelSpec]:
    """Model specs for tests: one fake family used as dense, sparse and late."""
    spec = FakeEncoder().spec
    return {"FAKE": spec}
