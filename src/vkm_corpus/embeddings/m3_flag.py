"""BGE-M3 reference encodings with the official FlagEmbedding ``BGEM3FlagModel`` on CPU (fp32) → derived artifacts.

One forward pass per text gives the three BGE-M3 outputs, exactly as FlagEmbedding computes them:

* dense — CLS hidden state, L2-normalised (1024);
* sparse — lexical weights ``relu(sparse_linear(h))``, max per token id, special tokens (cls/eos/pad/unk) dropped
  (FlagEmbedding ``_process_token_weights``); stored as sorted token ids + float32 weights;
* multi-vector — ``colbert_linear`` on every token but CLS (the final ``</s>`` kept), L2-normalised (1024 per token);
  stored as float16 token matrices with their token ids.

Each output is a versioned artifact config (``derived/embeddings/{dense,sparse,multivector}/BAAI__bge-m3/<rev>/<sig>``)
whose signature includes the weights and head sha256, max length, normalisation, text rule and the backend
``flagembedding-cpu-fp32``. Objects already embedded under a config are skipped (§46), so an interrupted run resumes.
Queries are encoded too (``queries.npz`` + query-config signatures), so a benchmark can score all three outputs.

``python -m vkm_corpus.embeddings.m3_flag --model-dir D --docs docs.jsonl [--units units.jsonl] --queries q.jsonl
--data-root OUT --out LABDIR``
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

from vkm_corpus.embeddings.artifacts import ArtifactWriter, EmbeddingRow, existing_hashes, read_table, validate
from vkm_corpus.embeddings.reembed import CanonObject, plan
from vkm_corpus.embeddings.signature import EmbeddingConfig, QueryConfig, text_hash
from vkm_corpus.embeddings.tokenize import file_sha256

BACKEND = "flagembedding-cpu-fp32"
MODEL_ID = "BAAI/bge-m3"
REVISION = "5617a9f61b028005a4858fdac845db406aefb181"


def _log(msg: str, **kw: Any) -> None:
    print(json.dumps({"t": time.strftime("%H:%M:%S"), "msg": msg, **kw}, ensure_ascii=False), flush=True)


def configs(model_dir: Path, *, max_len: int, text_rule: str, mv_precision: str) -> dict[str, EmbeddingConfig]:
    w = file_sha256(model_dir / "pytorch_model.bin")
    tok = file_sha256(model_dir / "tokenizer.json")
    common = dict(model_id=MODEL_ID, model_revision=REVISION, weights_file="pytorch_model.bin", weights_sha256=w,
                  quantization="F32", text_rule=text_rule, max_len=max_len, tokenizer_sha256=tok, backend=BACKEND)
    return {
        "dense": EmbeddingConfig(mode="dense", dimension=1024, pooling="cls", normalization="l2",
                                 storage_precision="float32", **common),
        "sparse": EmbeddingConfig(mode="sparse", dimension=250002, pooling="max_per_token_id", normalization="none",
                                  output_transform="relu", heads_sha256=file_sha256(model_dir / "sparse_linear.pt"),
                                  late={"drop_special_tokens": ["<s>", "</s>", "<pad>", "<unk>"]},
                                  storage_precision="float32", **common),
        "multivector": EmbeddingConfig(mode="multivector", dimension=1024, pooling="none", normalization="l2",
                                       heads_sha256=file_sha256(model_dir / "colbert_linear.pt"),
                                       late={"drop_special_tokens": ["<s>"], "token_dim": 1024,
                                             "normalize_tokens": True, "head": "colbert_linear", "style": "bge-m3"},
                                       storage_precision=mv_precision, **common),
    }


def query_configs(doc: dict[str, EmbeddingConfig], *, max_len: int) -> dict[str, QueryConfig]:
    out = {}
    for kind, c in doc.items():
        out[kind] = QueryConfig(model_id=c.model_id, model_revision=c.model_revision, weights_sha256=c.weights_sha256,
                                quantization=c.quantization, query_instruction="", dimension=c.dimension,
                                pooling=c.pooling, normalization=c.normalization, tokenizer_sha256=c.tokenizer_sha256,
                                max_len=max_len, output_transform=c.output_transform, late=c.late,
                                heads_sha256=c.heads_sha256, backend=BACKEND)
    return out


def load_units(docs: Path, units: Path | None) -> tuple[dict[str, str], dict[str, CanonObject]]:
    meta: dict[str, dict[str, Any]] = {}
    if units and units.exists():
        with units.open(encoding="utf-8") as fh:
            for line in fh:
                r = json.loads(line)
                meta[r["unit_id"]] = r
    texts: dict[str, str] = {}
    canon: dict[str, CanonObject] = {}
    with docs.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            r = json.loads(line)
            oid, t = r["object_id"], r["text"]
            m = meta.get(oid, {})
            texts[oid] = t
            canon[oid] = CanonObject(oid, text_hash(t), source_id=m.get("source_id"), page_id=m.get("page_id"),
                                     object_type=m.get("kind"))
    return texts, canon


def _sparse_arrays(lex: dict[str, Any]) -> tuple[list[int], list[float]]:
    items = sorted((int(k), float(v)) for k, v in lex.items())
    return [k for k, _ in items], [v for _, v in items]


def validate_multivector(directory: Path, config: EmbeddingConfig, expected: dict[str, str]) -> dict[str, Any]:
    """§64 checks of a (large) multi-vector artifact part by part, vectorised (no Python lists of floats)."""
    import pyarrow.parquet as pq

    from vkm_corpus.embeddings.artifacts import MANIFEST_PREFIX
    from vkm_corpus.embeddings.signature import embedding_signature

    sig = config.signature()
    seen: dict[str, str] = {}
    dup, bad, sig_bad, tokens = [], [], 0, 0
    for man in sorted(Path(directory).glob(f"{MANIFEST_PREFIX}*.json")):
        for part in json.loads(man.read_text(encoding="utf-8"))["parts"]:
            f = Path(directory) / part["file"]
            if file_sha256(f) != part["sha256"]:
                bad.append(f"checksum:{part['file']}")
                continue
            t = pq.read_table(f)
            oids = t.column("object_id").to_pylist()
            hashes = t.column("text_hash").to_pylist()
            cfg = t.column("config_hash").to_pylist()
            esig = t.column("embedding_signature").to_pylist()
            counts = np.asarray(t.column("token_count").to_numpy(), dtype=np.int64)
            dims = np.asarray(t.column("dimension").to_numpy(), dtype=np.int64)
            vals = t.column("vectors").combine_chunks()
            flat = np.asarray(vals.values.to_numpy(zero_copy_only=False), dtype=np.float32)
            offs = np.asarray(vals.offsets.to_numpy(), dtype=np.int64)
            for i, oid in enumerate(oids):
                if cfg[i] != sig:
                    sig_bad += 1
                if oid in seen:
                    dup.append(oid)
                seen[oid] = hashes[i]
                ok = (esig[i] == embedding_signature(oid, hashes[i], sig) and counts[i] > 0 and dims[i] == 1024
                      and offs[i + 1] - offs[i] == counts[i] * dims[i])
                if ok:
                    m = flat[offs[i]:offs[i + 1]].reshape(counts[i], dims[i])
                    ok = bool(np.all(np.isfinite(m)) and np.all(np.abs(np.linalg.norm(m, axis=1) - 1.0) <= 5e-3))
                if not ok:
                    bad.append(oid)
            tokens += int(counts.sum())
    missing = sorted(set(expected) - set(seen))
    unexpected = sorted(set(seen) - set(expected))
    stale = sum(1 for o, h in seen.items() if o in expected and expected[o] != h)
    ok = not (missing or unexpected or dup or bad or sig_bad or stale)
    return {"ok": ok, "expected": len(expected), "rows_current": len(seen), "tokens": tokens,
            "n_missing": len(missing), "n_unexpected": len(unexpected), "n_duplicates": len(dup),
            "n_bad_vectors": len(bad), "stale_rows": stale, "signature_mismatch": sig_bad,
            "missing": missing[:20], "bad_vectors": bad[:20]}


def encode_queries(model, qpath: Path, out: Path, qcfg: dict[str, QueryConfig], *, max_len: int, batch: int) -> dict:
    rows = [json.loads(line) for line in qpath.open(encoding="utf-8") if line.strip()]
    ids = [r["query_id"] for r in rows]
    t0 = time.perf_counter()
    res = model.encode_queries([r["text"] for r in rows], batch_size=batch, max_length=max_len, return_dense=True,
                               return_sparse=True, return_colbert_vecs=True)
    secs = time.perf_counter() - t0
    mv = [np.asarray(m, dtype=np.float32) for m in res["colbert_vecs"]]
    sp = [_sparse_arrays(lex) for lex in res["lexical_weights"]]
    out.mkdir(parents=True, exist_ok=True)
    np.savez(out / "queries.npz", query_ids=np.asarray(ids), dense=np.asarray(res["dense_vecs"], dtype=np.float32),
             mv=np.concatenate(mv), mv_offsets=np.cumsum([0] + [len(m) for m in mv]),
             sparse_ids=np.asarray([i for s in sp for i in s[0]], dtype=np.int32),
             sparse_weights=np.asarray([w for s in sp for w in s[1]], dtype=np.float32),
             sparse_offsets=np.cumsum([0] + [len(s[0]) for s in sp]))
    meta = {"n": len(ids), "seconds": round(secs, 2), "max_len": max_len, "source": qpath.name, "dir": str(out),
            "source_sha256": file_sha256(qpath), "file_sha256": file_sha256(out / "queries.npz"),
            "query_signatures": {k: c.signature() for k, c in qcfg.items()},
            "query_configs": {k: c.as_dict() for k, c in qcfg.items()},
            "layout": "npz: query_ids[n]; dense[n,1024] (L2); mv[sum tokens,1024] (L2) + mv_offsets[n+1]; "
                      "sparse_ids/sparse_weights + sparse_offsets[n+1] (FlagEmbedding lexical weights)"}
    (out / "queries.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")
    return meta


def export_lab_cache(receipt_path: Path, docs: Path, queries_jsonl: Path, dest: Path) -> dict[str, Any]:
    """The same encodings in the Retrieval Lab ``VectorCache`` layout (rows keyed by sha256 of the encoded text):
    ``{doc,query}_dense.npy`` + ``_dense_index.json``, ``{doc,query}_tokens.float16`` + ``_tokens_index.json``,
    ``{doc,query}_sparse.jsonl``; ``signature.json`` names the backend and the artifact signatures. A copy, not a new
    encoding: the derived artifacts stay the source of truth."""
    import pyarrow.parquet as pq

    from vkm_corpus.embeddings.artifacts import MANIFEST_PREFIX

    rec = json.loads(receipt_path.read_text(encoding="utf-8"))
    key_of: dict[str, str] = {}
    with docs.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                r = json.loads(line)
                key_of[r["object_id"]] = text_hash(r["text"])     # sha256 of the encoded text (= VectorCache.key)
    dest.mkdir(parents=True, exist_ok=True)

    def parts(kind: str):
        d = Path(rec["configs"][kind]["dir"])
        for man in sorted(d.glob(f"{MANIFEST_PREFIX}*.json")):
            for part in json.loads(man.read_text(encoding="utf-8"))["parts"]:
                yield pq.read_table(d / part["file"])

    dense: dict[str, np.ndarray] = {}
    for t in parts("dense"):
        vals = t.column("vector").combine_chunks()
        flat = np.asarray(vals.values.to_numpy(zero_copy_only=False), dtype=np.float32).reshape(-1, 1024)
        for oid, v in zip(t.column("object_id").to_pylist(), flat):
            dense.setdefault(key_of[oid], v)
    keys = sorted(dense)
    np.save(dest / "doc_dense.npy", np.stack([dense[k] for k in keys]).astype(np.float32))
    (dest / "doc_dense_index.json").write_text(json.dumps({k: i for i, k in enumerate(keys)}), encoding="utf-8")

    with open(dest / "doc_sparse.jsonl", "w", encoding="utf-8") as f:
        seen: set[str] = set()
        for t in parts("sparse"):
            for oid, ids, ws in zip(t.column("object_id").to_pylist(), t.column("token_ids").to_pylist(),
                                    t.column("weights").to_pylist()):
                k = key_of[oid]
                if k not in seen:
                    seen.add(k)
                    f.write(json.dumps({"k": k, "w": {str(i): float(w) for i, w in zip(ids, ws)}},
                                       separators=(",", ":")) + "\n")

    offsets: dict[str, tuple[int, int]] = {}
    pos = 0
    with open(dest / "doc_tokens.float16", "wb") as f:
        for t in parts("multivector"):
            vals = t.column("vectors").combine_chunks()
            flat = np.asarray(vals.values.to_numpy(zero_copy_only=False), dtype=np.float16)
            offs = np.asarray(vals.offsets.to_numpy(), dtype=np.int64)
            for i, oid in enumerate(t.column("object_id").to_pylist()):
                k = key_of[oid]
                if k in offsets:
                    continue
                m = flat[offs[i]:offs[i + 1]]
                f.write(np.ascontiguousarray(m).tobytes())
                n = len(m) // 1024
                offsets[k] = (pos, pos + n)
                pos += n
    (dest / "doc_tokens_index.json").write_text(json.dumps({"dtype": "float16", "dim": 1024, "offsets": offsets}),
                                                encoding="utf-8")

    qrows = [json.loads(line) for line in queries_jsonl.open(encoding="utf-8") if line.strip()]
    qtext = {r["query_id"]: r["text"] for r in qrows}
    qdir = Path((rec.get("queries") or {}).get("dir") or receipt_path.parent / "queries")
    z = np.load(qdir / "queries.npz") if (qdir / "queries.npz").exists() else None
    if z is not None:
        qids = [str(x) for x in z["query_ids"]]
        qkeys = [text_hash(qtext[q]) for q in qids]
        np.save(dest / "query_dense.npy", np.asarray(z["dense"], dtype=np.float32)[np.argsort(qkeys)])
        (dest / "query_dense_index.json").write_text(json.dumps({k: i for i, k in enumerate(sorted(qkeys))}),
                                                     encoding="utf-8")
        so, sid, sw = z["sparse_offsets"], z["sparse_ids"], z["sparse_weights"]
        with open(dest / "query_sparse.jsonl", "w", encoding="utf-8") as f:
            for j in np.argsort(qkeys):
                a, b = int(so[j]), int(so[j + 1])
                f.write(json.dumps({"k": qkeys[j], "w": {str(int(i)): float(w) for i, w in zip(sid[a:b], sw[a:b])}},
                                   separators=(",", ":")) + "\n")
        mo, mv = z["mv_offsets"], np.asarray(z["mv"], dtype=np.float16)
        qoff, pos = {}, 0
        with open(dest / "query_tokens.float16", "wb") as f:
            for j in np.argsort(qkeys):
                m = mv[int(mo[j]):int(mo[j + 1])]
                f.write(np.ascontiguousarray(m).tobytes())
                qoff[qkeys[j]] = (pos, pos + len(m))
                pos += len(m)
        (dest / "query_tokens_index.json").write_text(json.dumps({"dtype": "float16", "dim": 1024, "offsets": qoff}),
                                                      encoding="utf-8")
    sig = {"model_id": MODEL_ID, "model_revision": REVISION, "backend": BACKEND, "mode": "m3",
           "unit_rule": "vkm-units-v1", "compute_precision": "fp32", "token_dtype": "float16",
           "max_doc_tokens": rec["settings"]["max_len"], "max_query_tokens": rec["settings"]["query_max_len"],
           "artifact_signatures": {k: v["signature"] for k, v in rec["configs"].items()},
           "query_signatures": (rec.get("queries") or {}).get("query_signatures"),
           "note": "copy of the derived artifacts in the Retrieval Lab VectorCache layout; keys = sha256(text)"}
    (dest / "signature.json").write_text(json.dumps(sig, indent=1, ensure_ascii=False, sort_keys=True),
                                         encoding="utf-8")
    return {"dest": str(dest), "doc_keys": len(keys), "doc_token_rows": sum(b - a for a, b in offsets.values()),
            "query_keys": len(z["query_ids"]) if z is not None else 0,
            "tokens_file_bytes": (dest / "doc_tokens.float16").stat().st_size}


def main(argv: list[str] | None = None) -> int:
    import sys

    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == "export-lab-cache":
        ep = argparse.ArgumentParser(prog="vkm-m3-flag export-lab-cache")
        ep.add_argument("--receipt", required=True)
        ep.add_argument("--docs", required=True)
        ep.add_argument("--queries", required=True)
        ep.add_argument("--dest", required=True)
        e = ep.parse_args(args[1:])
        print(json.dumps(export_lab_cache(Path(e.receipt), Path(e.docs), Path(e.queries), Path(e.dest))))
        return 0
    ap = argparse.ArgumentParser(prog="vkm-m3-flag")
    ap.add_argument("--model-dir", required=True)
    ap.add_argument("--docs", required=True)
    ap.add_argument("--units")
    ap.add_argument("--queries")
    ap.add_argument("--data-root", required=True)
    ap.add_argument("--out", required=True, help="lab dir: receipt, query outputs, log")
    ap.add_argument("--text-rule", default="vkm-units-v1/A")
    ap.add_argument("--max-len", type=int, default=512)
    ap.add_argument("--query-max-len", type=int, default=128)
    ap.add_argument("--threads", type=int, default=12)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--chunk", type=int, default=768)
    ap.add_argument("--mv-precision", default="float16", choices=["float16", "float32"])
    ap.add_argument("--writer", default="core-cpu-flag")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args(argv)

    import torch

    torch.set_num_threads(a.threads)
    from FlagEmbedding import BGEM3FlagModel
    import FlagEmbedding
    import transformers

    model_dir, out, root = Path(a.model_dir), Path(a.out), Path(a.data_root)
    out.mkdir(parents=True, exist_ok=True)
    cfgs = configs(model_dir, max_len=a.max_len, text_rule=a.text_rule, mv_precision=a.mv_precision)
    qcfgs = query_configs(cfgs, max_len=a.query_max_len)
    texts, canon = load_units(Path(a.docs), Path(a.units) if a.units else None)
    if a.limit:
        keep = sorted(canon)[: a.limit]
        texts, canon = {k: texts[k] for k in keep}, {k: canon[k] for k in keep}
    writers = {k: ArtifactWriter(root, k, c, writer_id=a.writer) for k, c in cfgs.items()}
    todo: set[str] = set()
    plans = {}
    for k, w in writers.items():
        try:
            existing = existing_hashes(read_table(w.dir, verify=False))
        except FileNotFoundError:
            existing = {}
        p = plan(cfgs[k].signature(), canon.values(), existing)
        plans[k] = p.summary()
        todo |= {o.object_id for o in p.to_embed}
    _log("plan", units=len(canon), to_embed=len(todo), plans=plans,
         signatures={k: c.signature() for k, c in cfgs.items()})

    t_load = time.perf_counter()
    model = BGEM3FlagModel(str(model_dir), use_fp16=False, devices="cpu", batch_size=a.batch,
                           passage_max_length=a.max_len, query_max_length=a.query_max_len)
    load_s = time.perf_counter() - t_load
    tokenizer = model.tokenizer
    order = sorted(todo, key=lambda o: len(texts[o]))
    done, tokens, t0 = 0, 0, time.perf_counter()
    for i in range(0, len(order), a.chunk):
        ids = order[i:i + a.chunk]
        batch_texts = [texts[o] for o in ids]
        res = model.encode_corpus(batch_texts, batch_size=a.batch, max_length=a.max_len, return_dense=True,
                                  return_sparse=True, return_colbert_vecs=True)
        tok_ids = tokenizer(batch_texts, truncation=True, max_length=a.max_len)["input_ids"]
        rows: dict[str, list[EmbeddingRow]] = {"dense": [], "sparse": [], "multivector": []}
        for j, oid in enumerate(ids):
            o = canon[oid]
            base = dict(object_id=oid, text_hash=o.text_hash, source_id=o.source_id, page_id=o.page_id,
                        object_type=o.object_type, worker=a.writer, backend=BACKEND)
            rows["dense"].append(EmbeddingRow(vector=np.asarray(res["dense_vecs"][j], dtype=np.float32), **base))
            sid, sw = _sparse_arrays(res["lexical_weights"][j])
            rows["sparse"].append(EmbeddingRow(token_ids=sid, weights=sw, **base))
            m = np.asarray(res["colbert_vecs"][j], dtype=np.float32)
            kept = tok_ids[j][1:]
            rows["multivector"].append(EmbeddingRow(vectors=m, token_ids=kept if len(kept) == len(m) else None,
                                                    **base))
            tokens += len(tok_ids[j])
        for k, w in writers.items():
            w.write_part(rows[k])
        done += len(ids)
        el = time.perf_counter() - t0
        _log("chunk", done=done, of=len(order), seconds=round(el, 1), units_per_s=round(done / el, 2),
             tokens_per_s=round(tokens / el, 1), eta_min=round((len(order) - done) / max(done / el, 1e-9) / 60, 1))
    enc_s = time.perf_counter() - t0

    qmeta = encode_queries(model, Path(a.queries), out / "queries", qcfgs, max_len=a.query_max_len,
                           batch=a.batch) if a.queries else None
    expected = {o.object_id: o.text_hash for o in canon.values()}
    val = {}
    for k in ("dense", "sparse"):
        val[k] = validate(writers[k].dir, cfgs[k], expected).as_dict()
        val[k] = {x: v for x, v in val[k].items() if x not in ("missing", "unexpected", "duplicates", "bad_vectors")}
    val["multivector"] = validate_multivector(writers["multivector"].dir, cfgs["multivector"], expected)
    pip = Path("/opt/pip-freeze.txt")
    receipt = {
        "created_at": time.strftime("%FT%T%z"), "backend": BACKEND, "model_id": MODEL_ID, "model_revision": REVISION,
        "model_files": {n: file_sha256(model_dir / n) for n in ("pytorch_model.bin", "colbert_linear.pt",
                                                                "sparse_linear.pt", "tokenizer.json", "config.json")},
        "runtime": {"FlagEmbedding": FlagEmbedding.__version__ if hasattr(FlagEmbedding, "__version__") else
                    "see pip_freeze", "torch": torch.__version__, "transformers": transformers.__version__,
                    "python": platform.python_version(), "threads": a.threads, "batch": a.batch,
                    "compute_dtype": "float32", "device": "cpu", "host": "CORE (Ryzen 7 5800X)",
                    "pip_freeze": pip.read_text().splitlines() if pip.exists() else None},
        "inputs": {"docs": Path(a.docs).name, "docs_sha256": file_sha256(Path(a.docs)),
                   "units": Path(a.units).name if a.units else None,
                   "units_sha256": file_sha256(Path(a.units)) if a.units else None, "n_units": len(canon)},
        "settings": {"max_len": a.max_len, "query_max_len": a.query_max_len, "text_rule": a.text_rule,
                     "multivector_storage": a.mv_precision, "normalize_embeddings": True, "use_fp16": False},
        "configs": {k: {"signature": c.signature(), "dir": str(writers[k].dir), "config": c.as_dict()}
                    for k, c in cfgs.items()},
        "plan": plans,
        "timing": {"load_s": round(load_s, 2), "encode_s": round(enc_s, 1), "units_encoded": done,
                   "tokens": tokens, "units_per_s": round(done / enc_s, 2) if done else None,
                   "tokens_per_s": round(tokens / enc_s, 1) if done else None},
        "queries": qmeta, "validation": val,
    }
    (out / "RECEIPT.json").write_text(json.dumps(receipt, indent=1, ensure_ascii=False), encoding="utf-8")
    _log("done", validation={k: v["ok"] for k, v in val.items()}, timing=receipt["timing"])
    return 0 if all(v["ok"] for v in val.values()) else 1


if __name__ == "__main__":
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    raise SystemExit(main())
