"""Reference encoder for RX580 parity (§30): HF transformers fp32 on the WORKSTATION, same token ids as the service.

Runs in a separate WORKSTATION venv (torch CPU/CUDA, transformers, sentence-transformers, PyLate); torch is imported
lazily so the module can be imported anywhere. For every probe text it stores the token ids produced by
:class:`vkm_corpus.embeddings.tokenize.SpecTokenizer` and the reference output computed from the backbone's last hidden
states with :mod:`vkm_corpus.embeddings.postprocess` (the production post-processing). An *official-path check* then
re-encodes a sample through the library the model card prescribes (sentence-transformers / PyLate) and records the
agreement of ids and vectors, so the reference is tied to the official implementation, not to our reading of it.

Output directory (runtime data, never committed): ``meta.json``, ``tokens_{query,doc}.jsonl``,
``{q,d}_dense.npy`` (+ ``{q,d}_int8.npy``), ``{q,d}_mv.npy`` + ``{q,d}_mv_off.npy`` (token vectors, CSR offsets),
``{q,d}_sparse.jsonl`` and ``check.json``.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import time
from pathlib import Path
from typing import Any

import numpy as np

from vkm_corpus.embeddings import postprocess as pp
from vkm_corpus.embeddings.specs import EncoderSpec, get
from vkm_corpus.embeddings.tokenize import Encoded, SpecTokenizer, file_sha256


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _batches(items: list[tuple[int, Encoded]], token_budget: int, max_batch: int):
    items = sorted(items, key=lambda t: len(t[1]))
    batch: list[tuple[int, Encoded]] = []
    for it in items:
        if batch and (len(batch) >= max_batch or len(it[1]) * (len(batch) + 1) > token_budget):
            yield batch
            batch = []
        batch.append(it)
    if batch:
        yield batch


class Backbone:
    """fp32 backbone returning the last hidden state for a list of id sequences (right padding + attention mask)."""

    def __init__(self, spec: EncoderSpec, model_dir: Path, device: str) -> None:
        import torch
        from transformers import AutoModel

        self.torch = torch
        self.spec = spec
        self.device = device
        kwargs: dict[str, Any] = {"trust_remote_code": True, "dtype": torch.float32}
        if spec.key.startswith("jina-colbert-v2"):
            self.model = self._load_jina_colbert(model_dir)
        else:
            self.model = AutoModel.from_pretrained(str(model_dir), **kwargs)
        self.model.eval().to(device)

    def _load_jina_colbert(self, model_dir: Path):
        import torch
        from safetensors.torch import load_file
        from transformers import AutoConfig, AutoModel

        cfg = AutoConfig.from_pretrained(str(model_dir), trust_remote_code=True)
        cfg.use_flash_attn = False
        model = AutoModel.from_config(cfg, trust_remote_code=True, torch_dtype=torch.float32)
        sd = load_file(str(model_dir / "model.safetensors"))
        state = {k[len("roberta."):]: v.float() for k, v in sd.items() if k.startswith("roberta.")}
        missing, unexpected = model.load_state_dict(state, strict=False)
        bad_missing = [m for m in missing if "pooler" not in m]
        if bad_missing or unexpected:
            raise RuntimeError(f"jina-colbert-v2 weights mismatch: missing={bad_missing[:5]} unexpected={unexpected[:5]}")
        return model

    def hidden(self, seqs: list[tuple[int, ...]], attend_all: bool = True) -> list[np.ndarray]:
        torch = self.torch
        n = max(len(s) for s in seqs)
        ids = torch.zeros((len(seqs), n), dtype=torch.long)
        mask = torch.zeros((len(seqs), n), dtype=torch.long)
        for i, s in enumerate(seqs):
            ids[i, : len(s)] = torch.tensor(s, dtype=torch.long)
            mask[i, : len(s)] = 1
        with torch.inference_mode():
            out = self.model(input_ids=ids.to(self.device), attention_mask=mask.to(self.device))
        H = out.last_hidden_state if hasattr(out, "last_hidden_state") else out[0]
        H = H.float().cpu().numpy()
        return [H[i, : len(s)] for i, s in enumerate(seqs)]


def _heads(spec: EncoderSpec, model_dir: Path) -> dict[str, np.ndarray]:
    """Gateway-side heads of the spec as numpy arrays (also exported for the service by :func:`export_heads`)."""
    import torch
    from safetensors.numpy import load_file

    if spec.key == "bge-m3":
        c = torch.load(model_dir / "colbert_linear.pt", map_location="cpu", weights_only=True)
        s = torch.load(model_dir / "sparse_linear.pt", map_location="cpu", weights_only=True)
        return {"colbert_w": c["weight"].float().numpy(), "colbert_b": c["bias"].float().numpy(),
                "sparse_w": s["weight"].float().numpy(), "sparse_b": s["bias"].float().numpy()}
    if spec.key == "pplx-embed-late-0.6b":
        return {"colbert_w": load_file(str(model_dir / "1_Dense/model.safetensors"))["linear.weight"].astype(np.float32)}
    if spec.key == "mlateon":
        d1 = load_file(str(model_dir / "1_Dense/model.safetensors"))
        d2 = load_file(str(model_dir / "2_Dense/model.safetensors"))
        d3 = load_file(str(model_dir / "3_Dense/model.safetensors"))
        a = d1["linear.weight"].astype(np.float64) + d1["residual.weight"].astype(np.float64)   # 1536×768
        b = d2["linear.weight"].astype(np.float64) + d2["residual.weight"].astype(np.float64)   # 768×1536
        w = d3["linear.weight"].astype(np.float64) @ b @ a                                        # 128×768
        return {"colbert_w": w.astype(np.float32)}
    if spec.key.startswith("jina-colbert-v2"):
        import torch as _t
        from safetensors.torch import load_file as _lt

        return {"colbert_w": _lt(str(model_dir / "model.safetensors"))["linear.weight"].to(_t.float32).numpy()}
    return {}


def export_heads(spec: EncoderSpec, model_dir: Path, out: Path) -> dict[str, Any]:
    heads = _heads(spec, model_dir)
    if not heads:
        return {}
    np.savez(out, **heads)
    return {"file": out.name, "sha256": file_sha256(out), "arrays": {k: list(v.shape) for k, v in heads.items()}}


def _save_mv(path_prefix: Path, mats: list[np.ndarray]) -> None:
    off = np.zeros(len(mats) + 1, dtype=np.int64)
    for i, m in enumerate(mats):
        off[i + 1] = off[i] + m.shape[0]
    dim = mats[0].shape[1] if mats else 0
    data = np.concatenate(mats, axis=0) if mats else np.zeros((0, dim), np.float32)
    np.save(str(path_prefix) + "_mv.npy", data.astype(np.float32))
    np.save(str(path_prefix) + "_mv_off.npy", off)


def encode_role(spec: EncoderSpec, bb: Backbone, heads: dict[str, np.ndarray], enc: list[Encoded], *,
                token_budget: int, max_batch: int, attend_all: bool = True) -> dict[str, Any]:
    order = list(enumerate(enc))
    H_all: list[np.ndarray | None] = [None] * len(enc)
    for batch in _batches(order, token_budget, max_batch):
        hs = bb.hidden([e.ids for _, e in batch], attend_all=attend_all)
        for (i, _), h in zip(batch, hs):
            H_all[i] = h
    out: dict[str, Any] = {"dense": [], "int8": [], "mv": [], "sparse": []}
    for e, H in zip(enc, H_all):
        assert H is not None
        if spec.family in ("dense", "multi") and spec.pooling != "none":
            d = pp.finalize_dense(spec, pp.pool(H, spec.pooling))
            out["dense"].append(d.vector)
            if d.int8 is not None:
                out["int8"].append(d.int8)
        if spec.colbert is not None:
            out["mv"].append(pp.colbert_tokens(spec, H, e.keep, head=heads.get("colbert_w"),
                                               head_bias=heads.get("colbert_b")))
        if spec.sparse:
            sp = pp.bge_m3_sparse(H, e.ids, heads["sparse_w"], heads["sparse_b"], unused_ids=(0, 1, 2, 3))
            out["sparse"].append({"ids": sp.token_ids.tolist(), "weights": sp.weights.tolist()})
    return out


def official_check(spec: EncoderSpec, model_dir: Path, stok: SpecTokenizer, texts: list[str], role: str,
                   ours: dict[str, Any], enc: list[Encoded], doc_max_len: int) -> dict[str, Any]:
    """Re-encode ``texts`` through the library the model card prescribes; compare ids and vectors with ours."""
    res: dict[str, Any] = {"role": role, "n": len(texts)}
    try:
        if spec.colbert is not None and spec.key in ("pplx-embed-late-0.6b", "mlateon"):
            from pylate import models as pl

            m = pl.ColBERT(model_name_or_path=str(model_dir), device="cpu", document_length=doc_max_len,
                           trust_remote_code=True)
            is_q = role == "query"
            tok = m.tokenize(texts, is_query=is_q)
            lib_ids = [[int(x) for x, a in zip(r.tolist(), am.tolist()) if a]
                       for r, am in zip(tok["input_ids"], tok["attention_mask"])]
            res["ids_equal"] = sum(1 for a, b in zip(lib_ids, enc) if tuple(a) == b.ids)
            vecs = m.encode(texts, is_query=is_q, convert_to_numpy=True)
            cos = []
            for v, o in zip(vecs, ours["mv"]):
                v = np.asarray(v, np.float32)
                if v.shape != o.shape:
                    cos.append(float("nan"))
                    continue
                cos.append(float(np.mean(np.sum(pp.l2_normalize(v) * pp.l2_normalize(o), axis=1))))
            res["token_cos_mean"] = float(np.nanmean(cos)) if cos else None
            res["token_cos_min"] = float(np.nanmin(cos)) if cos else None
            res["shape_mismatch"] = int(sum(1 for c in cos if c != c))
            res["library"] = "pylate"
        elif spec.colbert is None or spec.family == "multi":
            if spec.key == "bge-m3" or spec.key == "pplx-embed-context-0.6b":
                res["library"] = "not-run (manual implementation mirrors FlagEmbedding / model code)"
                return res
            from sentence_transformers import SentenceTransformer

            m = SentenceTransformer(str(model_dir), device="cpu", trust_remote_code=True)
            m.max_seq_length = doc_max_len if role == "document" else m.max_seq_length
            prompt = spec.prefix(role)  # type: ignore[arg-type]
            tok = m.tokenize([prompt + t for t in texts])
            lib_ids = [[int(x) for x, a in zip(r.tolist(), am.tolist()) if a]
                       for r, am in zip(tok["input_ids"], tok["attention_mask"])]
            res["ids_equal"] = sum(1 for a, b in zip(lib_ids, enc) if tuple(a) == b.ids)
            vecs = m.encode(texts, prompt=prompt, convert_to_numpy=True, normalize_embeddings=False)
            vecs = np.asarray(vecs)
            if spec.output_transform == "tanh_int8":
                ref = np.stack(ours["int8"]).astype(np.int32)
                res["int8_equal_frac"] = float(np.mean(vecs.astype(np.int32) == ref))
                res["int8_maxabs"] = int(np.max(np.abs(vecs.astype(np.int32) - ref)))
            else:
                ref = np.stack(ours["dense"])
                c = np.sum(pp.l2_normalize(vecs) * pp.l2_normalize(ref), axis=1)
                res["cos_mean"], res["cos_min"] = float(c.mean()), float(c.min())
            res["library"] = "sentence-transformers"
    except Exception as exc:  # recorded, not fatal: the check is evidence, not a gate for the reference itself
        res["error"] = f"{type(exc).__name__}: {exc}"[:500]
    return res


CONTEXT_SEP = "<|endoftext|>"


def context_groups(docs: list[dict[str, Any]], size: int = 4) -> list[list[int]]:
    """Late-chunking probe documents: consecutive BLOCK objects of one source in groups of ``size`` (the other kinds
    stay single-chunk documents). Deterministic, order of the probe file."""
    groups: list[list[int]] = []
    cur: list[int] = []
    for i, d in enumerate(docs):
        if d.get("object_kind") != "BLOCK":
            groups.append([i])
            continue
        if cur and (len(cur) >= size or docs[cur[-1]]["source_id"] != d["source_id"]):
            groups.append(cur)
            cur = []
        cur.append(i)
    if cur:
        groups.append(cur)
    return sorted(groups, key=lambda g: g[0])


def run_context(args: argparse.Namespace, spec: EncoderSpec, stok: SpecTokenizer, model_dir: Path, out: Path,
                docs: list[dict[str, Any]], queries: list[dict[str, Any]]) -> int:
    """pplx-embed-context: chunks joined by <|endoftext|>, one bidirectional forward pass, mean pool per chunk span,
    tanh/int8 (the model's ``encode``). Queries are single-chunk documents."""
    import torch

    groups = context_groups(docs)
    ctx = []
    with open(out / "tokens_ctx.jsonl", "w", encoding="utf-8") as fh:
        for g in groups:
            enc, spans = stok.encode_context([docs[i]["text"] for i in g], sep_token=CONTEXT_SEP,
                                             max_len=args.ctx_max_len)
            spans = spans[: len(g)]
            while len(spans) < len(g):          # chunks cut off by truncation get empty spans (zero vectors)
                spans.append((len(enc.ids), len(enc.ids)))
            ctx.append((g, enc, spans))
            fh.write(json.dumps({"doc_idx": g, "ids": list(enc.ids), "spans": spans}) + "\n")
    q_ctx = [stok.encode_context([q["text"]], sep_token=CONTEXT_SEP, max_len=args.doc_max_len) for q in queries]
    with open(out / "tokens_query.jsonl", "w", encoding="utf-8") as fh:
        for q, (enc, spans) in zip(queries, q_ctx):
            fh.write(json.dumps({"id": q["qid"], "ids": list(enc.ids), "keep": None, "spans": spans}) + "\n")
    t0 = time.time()
    bb = Backbone(spec, model_dir, args.device)
    t_load = time.time() - t0
    d_vec: list[np.ndarray | None] = [None] * len(docs)
    d_i8: list[np.ndarray | None] = [None] * len(docs)
    t1 = time.time()
    for g, enc, spans in ctx:
        H = bb.hidden([enc.ids])[0]
        for i, o in zip(g, pp.context_chunks(spec, H, spans)):
            d_vec[i], d_i8[i] = o.vector, o.int8
    q_vec, q_i8 = [], []
    for enc, spans in q_ctx:
        o = pp.context_chunks(spec, bb.hidden([enc.ids])[0], spans)[0]
        q_vec.append(o.vector)
        q_i8.append(o.int8)
    t_enc = time.time() - t1
    np.save(out / "d_dense.npy", np.stack(d_vec).astype(np.float32))
    np.save(out / "d_int8.npy", np.stack(d_i8).astype(np.int8))
    np.save(out / "q_dense.npy", np.stack(q_vec).astype(np.float32))
    np.save(out / "q_int8.npy", np.stack(q_i8).astype(np.int8))
    check: dict[str, Any] = {"library": "model.encode (PPLXQwen3ContextualModel)"}
    try:
        from transformers import AutoModel

        m = AutoModel.from_pretrained(str(model_dir), trust_remote_code=True, dtype=torch.float32).eval()
        sample = [g for g, _, _ in ctx if len(g) > 1][:12]
        lib = m.encode([[docs[i]["text"] for i in g] for g in sample], quantization="int8")
        eq, mx, n = 0, 0, 0
        for g, arr in zip(sample, lib):
            for i, row in zip(g, np.asarray(arr)):
                ours = d_i8[i].astype(np.int32)
                eq += int(np.sum(ours == row.astype(np.int32)))
                mx = max(mx, int(np.max(np.abs(ours - row.astype(np.int32)))))
                n += row.size
        check.update({"contexts": len(sample), "int8_equal_frac": eq / max(n, 1), "int8_maxabs": mx})
    except Exception as exc:
        check["error"] = f"{type(exc).__name__}: {exc}"[:500]
    (out / "check.json").write_text(json.dumps([check], indent=1))
    meta = {"spec": spec.as_dict(), "mode": "context", "tokenizer_sha256": stok.tokenizer_sha256,
            "device": args.device, "dtype": "float32", "ctx_max_len": args.ctx_max_len, "n_contexts": len(ctx),
            "n_docs": len(docs), "n_queries": len(queries),
            "seconds": {"load": round(t_load, 2), "encode": round(t_enc, 2)},
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    (out / "meta.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False))
    print(json.dumps({"key": spec.key, "seconds": meta["seconds"], "checks": [check]}, ensure_ascii=False))
    return 0


def run(args: argparse.Namespace) -> int:
    import torch

    torch.set_num_threads(args.threads)
    spec = get(args.key)
    model_dir = Path(args.model_dir)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stok = SpecTokenizer.from_dir(spec, model_dir)
    docs = read_jsonl(Path(args.docs))
    queries = read_jsonl(Path(args.queries))
    if args.limit_docs:
        docs = docs[: args.limit_docs]
    if spec.key == "pplx-embed-context-0.6b":
        return run_context(args, spec, stok, model_dir, out, docs, queries)
    q_enc = [stok.encode(q["text"], "query", max_len=args.doc_max_len) for q in queries]
    d_enc = [stok.encode(d["text"], "document", max_len=args.doc_max_len) for d in docs]
    for name, items, enc in (("query", queries, q_enc), ("doc", docs, d_enc)):
        with open(out / f"tokens_{name}.jsonl", "w", encoding="utf-8") as fh:
            for it, e in zip(items, enc):
                fh.write(json.dumps({"id": it.get("qid") or it.get("object_id"), "ids": list(e.ids),
                                     "keep": None if e.keep is None else [int(k) for k in e.keep]}) + "\n")
    t0 = time.time()
    bb = Backbone(spec, model_dir, args.device)
    heads = _heads(spec, model_dir)
    head_info = export_heads(spec, model_dir, out / "heads.npz") if heads else {}
    t_load = time.time() - t0
    t1 = time.time()
    q_out = encode_role(spec, bb, heads, q_enc, token_budget=args.token_budget, max_batch=args.max_batch)
    t_q = time.time() - t1
    t2 = time.time()
    d_out = encode_role(spec, bb, heads, d_enc, token_budget=args.token_budget, max_batch=args.max_batch)
    t_d = time.time() - t2
    for pre, o in (("q", q_out), ("d", d_out)):
        if o["dense"]:
            np.save(out / f"{pre}_dense.npy", np.stack(o["dense"]).astype(np.float32))
        if o["int8"]:
            np.save(out / f"{pre}_int8.npy", np.stack(o["int8"]).astype(np.int8))
        if o["mv"]:
            _save_mv(out / pre, o["mv"])
        if o["sparse"]:
            with open(out / f"{pre}_sparse.jsonl", "w") as fh:
                for s in o["sparse"]:
                    fh.write(json.dumps(s) + "\n")
    checks = []
    if args.check > 0:
        n = min(args.check, len(queries))
        checks.append(official_check(spec, model_dir, stok, [q["text"] for q in queries[:n]], "query",
                                     {k: v[:n] for k, v in q_out.items()}, q_enc[:n], args.doc_max_len))
        n = min(args.check, len(docs))
        checks.append(official_check(spec, model_dir, stok, [d["text"] for d in docs[:n]], "document",
                                     {k: v[:n] for k, v in d_out.items()}, d_enc[:n], args.doc_max_len))
    (out / "check.json").write_text(json.dumps(checks, indent=1))
    import transformers

    meta = {
        "spec": spec.as_dict(), "model_dir_name": model_dir.name, "tokenizer_sha256": stok.tokenizer_sha256,
        "device": args.device, "dtype": "float32", "threads": args.threads, "doc_max_len": args.doc_max_len,
        "n_queries": len(queries), "n_docs": len(docs),
        "n_query_tokens": int(sum(len(e) for e in q_enc)), "n_doc_tokens": int(sum(len(e) for e in d_enc)),
        "seconds": {"load": round(t_load, 2), "queries": round(t_q, 2), "docs": round(t_d, 2)},
        "versions": {"torch": torch.__version__, "transformers": transformers.__version__,
                     "numpy": np.__version__, "python": platform.python_version()},
        "heads": head_info, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False))
    print(json.dumps({"key": spec.key, "seconds": meta["seconds"], "checks": checks}, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="reference embeddings for RX580 parity (WORKSTATION)")
    ap.add_argument("--key", required=True)
    ap.add_argument("--model-dir", required=True)
    ap.add_argument("--docs", required=True)
    ap.add_argument("--queries", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--doc-max-len", type=int, default=512)
    ap.add_argument("--ctx-max-len", type=int, default=2048, help="contextual documents (pplx-embed-context)")
    ap.add_argument("--limit-docs", type=int, default=0)
    ap.add_argument("--token-budget", type=int, default=8192)
    ap.add_argument("--max-batch", type=int, default=32)
    ap.add_argument("--threads", type=int, default=int(os.environ.get("VKM_REF_THREADS", "12")))
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--check", type=int, default=48, help="texts per role for the official-path check (0 = off)")
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
