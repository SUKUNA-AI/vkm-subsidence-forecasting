"""GRAPH_SEARCH_V1: P-E, the local copy of the deployed scheme E (TERM_DICTIONARY_V1 §3), used by every system of
the benchmark — the base and the graph stages differ only in the stage switches.

P-E = lab BM25 over the final units of the V1 snapshot (no BIB_ENTRY; the analyzer mirroring ``vkm_text``) and dense
jina-v5-nano (the ``nano-gpu`` document vectors of V2) → page lists by the best unit, one page per duplicate group of
the canon (``duplicate_page_candidates``, as the served legs collapse them), top 100 each → RRF (k = 60) → the late
window (RRF top 100) re-scored by mLateOn MaxSim over the V1 token pack (a page = the max over its units without
BIB_ENTRY) → the window in late order, unscored pages after the scored ones, then the RRF tail.

Query vectors (dense and late token vectors) are encoded here on the CPU with the lab encoders of the same models
(``retrieval_v0/configs/models.json`` D2, L2) and cached per text under ``$GS_WORK/qvec``; nothing of the corpus is
encoded. Environment: ``GS_WORK``, ``J_V1`` (``units_final.jsonl``, ``cache/late_*``, ``cache/units_ids.json``),
``V2_WORK`` (``vec/nano-gpu``), ``VKM_MODELS_DIR``, ``GS_CANON`` (canonical DuckDB of the snapshot; duplicate pages).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import numpy as np

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from vkm_corpus.retrieval_lab import fusion  # noqa: E402
from vkm_corpus.retrieval_lab.textproc import analyze  # noqa: E402

DEPTH = 100                 # per leg (J's scheme: first stages top-100)
WINDOW = 100                # late window (LATE_CANDIDATES of the service)
RRF_K = 60
K1, BM25_B = 1.2, 0.75
TAIL = 200                  # fused pages kept after the window (the RRF tail of the served order)


def log(*a: Any) -> None:
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


def read_jsonl(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def text_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


@dataclass
class EState:
    """What the E pipeline produced for one query (all lists best first)."""

    query: str
    legs: dict[str, list[tuple[str, float]]] = field(default_factory=dict)
    fused: list[tuple[str, float]] = field(default_factory=list)
    window: list[str] = field(default_factory=list)
    late: dict[str, float] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)


class Engine:
    def __init__(self, *, encoders: bool = True) -> None:
        t0 = time.time()
        self.work = Path(os.environ["GS_WORK"])
        v1 = Path(os.environ["J_V1"])
        v2 = Path(os.environ["V2_WORK"])
        final = [u for u in read_jsonl(v1 / "units_final.jsonl") if u["kind"] != "BIB_ENTRY"]
        dense_ids = json.load(open(v2 / "vec" / "nano-gpu" / "docs_ids.json", encoding="utf-8"))
        dense_rows = {u["unit_id"]: u for u in read_jsonl(v1 / "units_dense.jsonl")}
        self.group = self._dup_groups()
        # --- BM25 over the final units (lab analyzer; Lucene idf; length normalisation)
        self.f_page = np.array([u["page_id"] or "" for u in final], dtype=object)
        self.f_src = np.array([u["source_id"] for u in final], dtype=object)
        vocab: dict[str, int] = {}
        rows, cols, tfs, dl = [], [], [], np.zeros(len(final), dtype=np.float32)
        for j, u in enumerate(final):
            tf = Counter(analyze(u["text"]))
            dl[j] = sum(tf.values())
            for term, n in tf.items():
                rows.append(vocab.setdefault(term, len(vocab)))
                cols.append(j)
                tfs.append(n)
        from scipy import sparse

        rows_a, cols_a, tf_a = np.asarray(rows), np.asarray(cols), np.asarray(tfs, dtype=np.float32)
        df = np.bincount(rows_a, minlength=len(vocab)).astype(np.float64)
        n = len(final)
        idf = np.log(1.0 + (n - df + 0.5) / (df + 0.5))
        avgdl = float(dl.mean())
        w = idf[rows_a] * tf_a * (K1 + 1) / (tf_a + K1 * (1 - BM25_B + BM25_B * dl[cols_a] / avgdl))
        self.bm25 = sparse.csr_matrix((w.astype(np.float32), (rows_a, cols_a)), shape=(len(vocab), n))
        self.vocab = vocab
        log("bm25", n, "units", len(vocab), "terms", round(time.time() - t0, 1), "s")
        # --- dense (nano-gpu vectors of the dense-index units)
        self.D = np.ascontiguousarray(np.load(v2 / "vec" / "nano-gpu" / "docs.f32.npy"), dtype=np.float32)
        self.d_page = np.array([dense_rows[u]["page_id"] or "" for u in dense_ids], dtype=object)
        self.d_src = np.array([dense_rows[u]["source_id"] for u in dense_ids], dtype=object)
        log("dense", self.D.shape)
        # --- late: V1 token pack (union order), units of each page in the final collection
        union = json.load(open(v1 / "cache" / "units_ids.json", encoding="utf-8"))
        pos = {u: i for i, u in enumerate(union)}
        self.offsets = np.load(v1 / "cache" / "late_offsets.npy")
        self.tok = np.load(v1 / "cache" / "late_tokens.f16.npy", mmap_mode="r")
        self.page_units: dict[str, list[int]] = defaultdict(list)
        for u in final:
            i = pos.get(u["unit_id"])
            if i is not None and self.offsets[i + 1] > self.offsets[i]:
                self.page_units[u["page_id"]].append(i)
        self.pages_of_source: dict[str, list[str]] = defaultdict(list)
        for p in sorted(set(self.f_page) - {""}):
            self.pages_of_source[p.split(":")[0]].append(p)
        self._mask: dict[tuple, tuple[np.ndarray, np.ndarray]] = {}
        self._qd: dict[str, np.ndarray] = {}
        self._ql: dict[str, np.ndarray] = {}
        self._late_cache: dict[tuple[str, str], float] = {}
        self.enc_dense = self.enc_late = None
        if encoders:
            self._load_encoders()
        log("engine ready", round(time.time() - t0, 1), "s")

    # ------------------------------------------------------------------ canon duplicate pages (served collapse)
    @staticmethod
    def _dup_groups() -> dict[str, str]:
        import duckdb

        con = duckdb.connect(os.environ["GS_CANON"], read_only=True)
        rows = con.execute("SELECT page_id, dup_group_id FROM main.duplicate_page_candidates").fetchall()
        con.close()
        return {p: g for p, g in rows}

    # ------------------------------------------------------------------ query encoders (CPU, cached per text)
    def _load_encoders(self) -> None:
        from vkm_corpus.retrieval_lab.encoders import load_specs, make_encoder

        import torch

        torch.set_num_threads(int(os.environ.get("GS_THREADS", "8")))
        specs = load_specs(REPO / "benchmarks/retrieval_v0/configs/models.json")
        models = os.environ["VKM_MODELS_DIR"]
        self.enc_dense = make_encoder(specs["D2"], models, device="cpu", precision="fp32")
        self.enc_late = make_encoder(specs["L2"], models, device="cpu", precision="fp32")

    def _cache_dir(self) -> Path:
        d = self.work / "qvec"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def encode(self, texts: Iterable[str]) -> None:
        """Dense and late query vectors of every text (cached on disk: ``$GS_WORK/qvec/<key>.npz``)."""
        todo = []
        for t in dict.fromkeys(texts):
            if t in self._qd:
                continue
            f = self._cache_dir() / f"{text_key(t)}.npz"
            if f.exists():
                z = np.load(f)
                self._qd[t], self._ql[t] = z["dense"], z["late"]
            else:
                todo.append(t)
        if todo and self.enc_dense is None:
            raise RuntimeError(f"{len(todo)} texts without cached query vectors and no encoders loaded")
        for s in range(0, len(todo), 32):
            chunk = todo[s:s + 32]
            dv = self.enc_dense.encode_queries(chunk)
            lv = self.enc_late.encode_queries(chunk)
            for t, d, m in zip(chunk, dv, lv):
                d = np.asarray(d, dtype=np.float32)
                m = np.asarray(m, dtype=np.float32)
                self._qd[t], self._ql[t] = d, m
                np.savez(self._cache_dir() / f"{text_key(t)}.npz", dense=d, late=m)
        if todo:
            log("encoded", len(todo), "texts")

    # ------------------------------------------------------------------ legs
    def masks(self, sources: tuple[str, ...] | None) -> tuple[np.ndarray | None, np.ndarray | None]:
        if not sources:
            return None, None
        if sources not in self._mask:
            s = set(sources)
            self._mask[sources] = (np.fromiter((x in s for x in self.f_src), bool, len(self.f_src)),
                                   np.fromiter((x in s for x in self.d_src), bool, len(self.d_src)))
        return self._mask[sources]

    def _pages(self, scores: np.ndarray, page: np.ndarray, depth: int) -> list[tuple[str, float]]:
        """Pages by their best unit (first occurrence of the unit ranking), one page per canon duplicate group;
        ties by position."""
        finite = np.isfinite(scores)
        n_ok = int(finite.sum())
        if n_ok == 0:
            return []
        n = min(n_ok, 6000)
        while True:
            top = np.argpartition(-np.where(finite, scores, -np.inf), n - 1)[:n]
            order = top[np.lexsort((top, -scores[top]))]
            out, seen = [], set()
            for j in order:
                if not finite[j]:
                    continue
                p = page[j]
                g = self.group.get(p, p)
                if not p or g in seen:
                    continue
                seen.add(g)
                out.append((p, float(scores[j])))
                if len(out) >= depth:
                    return out
            if n >= n_ok:
                return out
            n = min(n_ok, n * 4)

    def bm25_scores(self, text: str) -> np.ndarray | None:
        terms = [self.vocab[t] for t in set(analyze(text)) if t in self.vocab]
        if not terms:
            return None
        s = np.asarray(self.bm25[terms].sum(axis=0)).ravel()
        return np.where(s > 0, s, -np.inf)

    def bm25_leg(self, text: str, fmask: np.ndarray | None = None, depth: int = DEPTH) -> list[tuple[str, float]]:
        s = self.bm25_scores(text)
        if s is None:
            return []
        if fmask is not None:
            s = np.where(fmask, s, -np.inf)
        return self._pages(s, self.f_page, depth)

    def bm25_pages(self, text: str, sources: list[str] | None, depth: int) -> list[str]:
        """BM25 page search of a graph leg: G3 wordings (``sources`` None) and G4 (only the pages of ``sources``, as
        the served leg filters ``source_id``)."""
        fmask = None
        if sources is not None:
            if not sources:
                return []
            fmask = np.isin(self.f_src, np.asarray(sorted(set(sources)), dtype=object))
        return [p for p, _s in self.bm25_leg(text, fmask, depth)]

    def dense_leg(self, text: str, dmask: np.ndarray | None = None, depth: int = DEPTH) -> list[tuple[str, float]]:
        s = self.D @ self._qd[text]
        if dmask is not None:
            s = np.where(dmask, s, -np.inf)
        return self._pages(s, self.d_page, depth)

    # ------------------------------------------------------------------ late
    def late_scores(self, text: str, pages: Iterable[str]) -> dict[str, float]:
        """Late page score = max MaxSim over the page's units (no BIB_ENTRY); pages without token vectors absent."""
        q = self._ql[text]
        out = {}
        for p in pages:
            key = (text, p)
            if key in self._late_cache:
                v = self._late_cache[key]
            else:
                v = -math.inf
                for i in self.page_units.get(p, ()):
                    m = np.asarray(self.tok[self.offsets[i]:self.offsets[i + 1]], dtype=np.float32)
                    v = max(v, float((m @ q.T).max(axis=0).sum()))
                self._late_cache[key] = v
            if math.isfinite(v):
                out[p] = v
        return out

    @staticmethod
    def late_order(window: list[str], late: dict[str, float]) -> list[str]:
        """The window in late order (ties: window order), pages without a late score after the scored ones."""
        rank = {p: i for i, p in enumerate(window)}
        scored = sorted((p for p in window if p in late), key=lambda p: (-late[p], rank[p]))
        return scored + [p for p in window if p not in late]

    # ------------------------------------------------------------------ E
    def run_e(self, query: str, sources: tuple[str, ...] | None = None) -> EState:
        fmask, dmask = self.masks(sources)
        st = EState(query=query)
        st.legs = {"bm25": self.bm25_leg(query, fmask), "dense": self.dense_leg(query, dmask)}
        st.fused = fusion.rrf(st.legs, k=RRF_K)[:WINDOW + TAIL]
        st.window = [p for p, _ in st.fused[:WINDOW]]
        st.late = self.late_scores(query, st.window)
        st.order = self.late_order(st.window, st.late) + [p for p, _ in st.fused[WINDOW:]]
        return st
