"""TERM_DICTIONARY_V1 stage B (retrieval lab venv, GPU for the query encoders): the local copy of scheme E (P-E) and
every search both arms need (PREREGISTRATION §3).

P-E = lab BM25 over the snapshot units (no BIB_ENTRY) + dense jina-v5-nano (the nano-gpu contour of V2) → page lists
(best unit), RRF k = 60, late MaxSim (mLateOn query tokens encoded here, the token pack of V1) over the units of the
RRF top-100 pages → ``late_rescore`` of V1. A tier restricts every stage to the units of the given sources. Expansion
texts add their own BM25 and dense legs to the RRF; the late stage scores the original query.

Environment: ``TD_WORK`` (``stage_a.json``; writes ``stage_b.json``), ``J_V1`` (V1 work dir: ``units_*.jsonl``,
``cache/late_*``, ``cache/units_ids.json``), ``V2_WORK`` (``vec/nano-gpu``), ``VKM_MODELS_DIR``; ``TD_DEVICE``
(cuda / cpu), ``TD_VRAM`` (fraction of the GPU memory for this process). ``TD_LATE=0`` — the exploratory variant
without the late stage (RRF order: requests with ``late = false`` and the code default ``LATE_DEFAULT``; CORE runs
with the late stage by default) → ``stage_b_nolate.json``; added after the preregistered results were read.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from vkm_corpus.retrieval_lab import fusion  # noqa: E402
from vkm_corpus.retrieval_lab.encoders import load_specs, make_encoder  # noqa: E402
from vkm_corpus.retrieval_lab.textproc import analyze  # noqa: E402

WORK = Path(os.environ["TD_WORK"])
V1 = Path(os.environ["J_V1"])
V2 = Path(os.environ["V2_WORK"])
MODELS = os.environ.get("VKM_MODELS_DIR", "")
DEVICE = os.environ.get("TD_DEVICE", "cuda")
DEPTH = 100
RRF_K = 60
K1, BM25_B = 1.2, 0.75
DOSSIER_LIMIT = 50                         # pages the dossier asks per formulation and tier (RETRIEVAL_UNITS)
LATE = os.environ.get("TD_LATE", "1") != "0"   # 0: exploratory RRF-only variant (not preregistered)


def log(*a) -> None:
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


def read_jsonl(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


class Engine:
    def __init__(self) -> None:
        t0 = time.time()
        final = [u for u in read_jsonl(V1 / "units_final.jsonl") if u["kind"] != "BIB_ENTRY"]
        dense_ids = json.load(open(V2 / "vec" / "nano-gpu" / "docs_ids.json", encoding="utf-8"))
        dense_rows = {u["unit_id"]: u for u in read_jsonl(V1 / "units_dense.jsonl")}
        # --- BM25 over the final units (lab analyzer; Lucene idf; length normalisation)
        self.f_ids = [u["unit_id"] for u in final]
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
        self.D = np.load(V2 / "vec" / "nano-gpu" / "docs.f32.npy", mmap_mode="r")
        self.d_ids = dense_ids
        self.d_page = np.array([dense_rows[u]["page_id"] or "" for u in dense_ids], dtype=object)
        self.d_src = np.array([dense_rows[u]["source_id"] for u in dense_ids], dtype=object)
        self.D = np.ascontiguousarray(self.D, dtype=np.float32)
        log("dense", self.D.shape)
        # --- late: V1 token pack (union order), units of each page in the final collection
        union = json.load(open(V1 / "cache" / "units_ids.json", encoding="utf-8"))
        pos = {u: i for i, u in enumerate(union)}
        self.offsets = np.load(V1 / "cache" / "late_offsets.npy")
        self.tok = np.load(V1 / "cache" / "late_tokens.f16.npy", mmap_mode="r")
        self.page_units: dict[str, list[int]] = defaultdict(list)
        for u in final:
            i = pos.get(u["unit_id"])
            if i is not None and self.offsets[i + 1] > self.offsets[i]:
                self.page_units[u["page_id"]].append(i)
        # --- query encoders
        specs = load_specs(REPO / "benchmarks/retrieval_v0/configs/models.json")
        if DEVICE.startswith("cuda") and os.environ.get("TD_VRAM"):
            import torch

            torch.cuda.set_per_process_memory_fraction(float(os.environ["TD_VRAM"]))
        prec = "bf16" if DEVICE.startswith("cuda") else "fp32"
        self.enc_dense = make_encoder(specs["D2"], MODELS, device=DEVICE, precision=prec)
        self.enc_late = make_encoder(specs["L2"], MODELS, device=DEVICE, precision="fp32")
        self._qd: dict[str, np.ndarray] = {}
        self._ql: dict[str, np.ndarray] = {}
        self._mask: dict[tuple, tuple[np.ndarray, np.ndarray]] = {}
        log("engine ready", round(time.time() - t0, 1), "s")

    # ------------------------------------------------------------------ encoders (cached per text)
    def encode(self, texts: list[str]) -> None:
        new = sorted({t for t in texts if t not in self._qd})
        for s in range(0, len(new), 64):
            chunk = new[s:s + 64]
            for t, v in zip(chunk, self.enc_dense.encode_queries(chunk)):
                self._qd[t] = np.asarray(v, dtype=np.float32)
            for t, m in zip(chunk, self.enc_late.encode_queries(chunk)):
                self._ql[t] = np.asarray(m, dtype=np.float32)

    # ------------------------------------------------------------------ legs
    def _masks(self, sources: tuple[str, ...] | None) -> tuple[np.ndarray | None, np.ndarray | None]:
        if not sources:
            return None, None
        if sources not in self._mask:
            s = set(sources)
            self._mask[sources] = (np.fromiter((x in s for x in self.f_src), bool, len(self.f_src)),
                                   np.fromiter((x in s for x in self.d_src), bool, len(self.d_src)))
        return self._mask[sources]

    @staticmethod
    def _pages(scores: np.ndarray, page: np.ndarray, depth: int) -> list[tuple[str, float]]:
        """Pages by their best unit (first occurrence of the unit ranking), ties by position."""
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
                if not p or p in seen:
                    continue
                seen.add(p)
                out.append((p, float(scores[j])))
                if len(out) >= depth:
                    return out
            if n >= n_ok:
                return out
            n = min(n_ok, n * 4)

    def bm25_leg(self, text: str, fmask: np.ndarray | None) -> list[tuple[str, float]]:
        terms = [self.vocab[t] for t in set(analyze(text)) if t in self.vocab]
        if not terms:
            return []
        s = np.asarray(self.bm25[terms].sum(axis=0)).ravel()
        s = np.where(s > 0, s, -np.inf)
        if fmask is not None:
            s = np.where(fmask, s, -np.inf)
        return self._pages(s, self.f_page, DEPTH)

    def dense_leg(self, text: str, dmask: np.ndarray | None) -> list[tuple[str, float]]:
        s = self.D @ self._qd[text]
        if dmask is not None:
            s = np.where(dmask, s, -np.inf)
        return self._pages(s, self.d_page, DEPTH)

    def late_scores(self, text: str, pages: list[str]) -> dict[str, float]:
        q = self._ql[text]
        out = {}
        for p in pages:
            best = -math.inf
            for i in self.page_units.get(p, ()):
                m = np.asarray(self.tok[self.offsets[i]:self.offsets[i + 1]], dtype=np.float32)
                v = float((m @ q.T).max(axis=0).sum())
                best = max(best, v)
            if math.isfinite(best):
                out[p] = best
        return out

    def search(self, query: str, sources: tuple[str, ...] | None = None, expansions: tuple[str, ...] = (),
               depth: int = DEPTH) -> list[str]:
        fmask, dmask = self._masks(sources)
        legs = {"bm25": self.bm25_leg(query, fmask)[:DEPTH], "dense": self.dense_leg(query, dmask)[:DEPTH]}
        for n, x in enumerate(expansions, 1):
            legs[f"bm25~x{n}"] = self.bm25_leg(x, fmask)[:DEPTH]
            legs[f"dense~x{n}"] = self.dense_leg(x, dmask)[:DEPTH]
        fused = fusion.rrf(legs, k=RRF_K)[:2 * DEPTH]
        if not LATE:
            return [p for p, _ in fused][:depth]
        head = [p for p, _ in fused[:DEPTH]]
        ps = self.late_scores(query, head)
        scored = sorted(((p, ps[p]) for p in head if p in ps), key=lambda x: (-x[1], head.index(x[0])))
        unscored = [p for p in head if p not in ps]
        return ([p for p, _ in scored] + unscored + [p for p, _ in fused[DEPTH:]])[:depth]


def main() -> None:
    a = json.load(open(WORK / "stage_a.json", encoding="utf-8"))
    core = tuple(a["dossier"]["core_sources"])
    eng = Engine()
    # --- Q1: every formulation × tier of both arms (the dossier searches the core tier and the whole corpus)
    requests = set()
    for q in a["dossier"]["queries"].values():
        for arm in ("D0", "D1"):
            for f in q[arm]["formulations"]:
                requests.add((f["text"], "CORE"))
                requests.add((f["text"], "ALL"))
    texts = sorted({t for t, _ in requests})
    # --- Q2: the original text queries and their translations
    rt = a["retrieval_translations"]
    texts += [v["text"] for v in rt.values()] + [v["translation"] for v in rt.values() if v["translation"]]
    t0 = time.time()
    eng.encode(texts)
    log("encoded", len(set(texts)), "texts", round(time.time() - t0, 1), "s")
    # control: my encodings of the benchmark queries against V2's nano-gpu query vectors
    qids = json.load(open(V2 / "vec" / "nano-gpu" / "queries_ids.json", encoding="utf-8"))
    qv = np.load(V2 / "vec" / "nano-gpu" / "queries.f32.npy")
    cos = [float(np.dot(eng._qd[rt[q]["text"]], qv[i])) for i, q in enumerate(qids) if q in rt]
    control = {"n": len(cos), "min_cos": round(min(cos), 6), "mean_cos": round(sum(cos) / len(cos), 6)}
    log("dense query encoder vs V2", control)
    engine = "P-E (local copy of scheme E)" if LATE else "P-E without the late stage (exploratory)"
    out: dict = {"engine": engine, "control_dense_query_vs_v2": control, "dossier": {},
                 "hybrid": {"H0": {}, "H1": {}}}
    t0 = time.time()
    for n, (text, tier) in enumerate(sorted(requests), 1):
        out["dossier"][f"{tier}\t{text}"] = eng.search(text, core if tier == "CORE" else None, depth=DOSSIER_LIMIT)
        if n % 200 == 0:
            log("dossier searches", n, "/", len(requests), round(time.time() - t0, 1), "s")
    for qid, v in sorted(rt.items()):
        out["hybrid"]["H0"][qid] = eng.search(v["text"])
        out["hybrid"]["H1"][qid] = eng.search(v["text"], expansions=(v["translation"],)) if v["translation"] \
            else out["hybrid"]["H0"][qid]
    log("hybrid done", round(time.time() - t0, 1), "s")
    name = "stage_b.json" if LATE else "stage_b_nolate.json"
    (WORK / name).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
