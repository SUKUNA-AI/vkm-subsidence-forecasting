"""``vkm-corpus embed …`` — embeddings tooling (agent K).

* ``embed specs`` — encoder specifications of the Retrieval Lab candidates;
* ``embed signature --key K --gguf F --quant Q --tokenizer-dir D [--mode dense] [--dim N] [--max-len N]`` — document
  config signature (§37) and query signature (§38) of a deployment;
* ``embed plan --dir ARTIFACT_DIR --canon objects.jsonl`` — re-embed plan (§46) against an artifact directory;
* ``embed validate --dir ARTIFACT_DIR --canon objects.jsonl`` — pre-import checks (§64).

``objects.jsonl`` rows: ``{"object_id", "text_hash", …}`` (the canonical objects of the config's text rule).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def _specs(args: argparse.Namespace) -> int:
    from vkm_corpus.embeddings.specs import SPECS

    rows = [{"key": k, "model_id": s.model_id, "revision": s.model_revision[:12], "family": s.family, "arch": s.arch,
             "params_m": s.params_m, "dim": s.output_dim, "pooling": s.pooling, "license": s.license}
            for k, s in SPECS.items()]
    print(json.dumps(rows, indent=1, ensure_ascii=False))
    return 0


def _signature(args: argparse.Namespace) -> int:
    from vkm_corpus.embeddings.signature import document_config, query_config
    from vkm_corpus.embeddings.specs import get
    from vkm_corpus.embeddings.tokenize import file_sha256

    spec = get(args.key)
    gguf = Path(args.gguf)
    tok_sha = file_sha256(Path(args.tokenizer_dir) / spec.tokenizer_file)
    w_sha = file_sha256(gguf)
    heads_sha = file_sha256(args.heads) if args.heads else ""
    doc = document_config(spec, mode=args.mode, weights_file=gguf.name, weights_sha256=w_sha, quantization=args.quant,
                          tokenizer_sha256=tok_sha, dimension=args.dim, max_len=args.max_len, heads_sha256=heads_sha,
                          text_rule=args.text_rule, storage_precision=args.precision)
    q = query_config(spec, weights_sha256=w_sha, quantization=args.quant, tokenizer_sha256=tok_sha,
                     dimension=args.dim, max_len=args.query_max_len, late=args.mode == "multivector",
                     heads_sha256=heads_sha)
    print(json.dumps({"document": {"signature": doc.signature(), "config": doc.as_dict()},
                      "query": {"signature": q.signature(), "config": q.as_dict()}}, indent=1, ensure_ascii=False))
    return 0


def _read_canon(path: str):
    from vkm_corpus.embeddings.reembed import CanonObject

    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                d = json.loads(line)
                out.append(CanonObject(d["object_id"], d["text_hash"], d.get("source_id"), d.get("page_id"),
                                       d.get("object_type"), d.get("content_sha256")))
    return out


def _config_of(directory: Path):
    from vkm_corpus.embeddings.signature import EmbeddingConfig

    raw = json.loads((directory / "config.json").read_text(encoding="utf-8"))
    return raw["kind"], EmbeddingConfig(**raw["config"])


def _plan(args: argparse.Namespace) -> int:
    from vkm_corpus.embeddings.artifacts import existing_hashes, read_table
    from vkm_corpus.embeddings.reembed import plan

    d = Path(args.dir)
    _, cfg = _config_of(d)
    try:
        existing = existing_hashes(read_table(d))
    except FileNotFoundError:
        existing = {}
    p = plan(cfg.signature(), _read_canon(args.canon), existing)
    print(json.dumps(p.summary(), indent=1))
    return 0


def _validate(args: argparse.Namespace) -> int:
    from vkm_corpus.embeddings.artifacts import validate

    d = Path(args.dir)
    _, cfg = _config_of(d)
    expected = {o.object_id: o.text_hash for o in _read_canon(args.canon)}
    rep = validate(d, cfg, expected)
    print(json.dumps(rep.as_dict(), indent=1, ensure_ascii=False))
    return 0 if rep.ok else 1


def _encode(args: argparse.Namespace) -> int:
    """Texts of canonical objects → running llama-servers of a service config → derived artifacts, through the
    in-memory job queue (same claim/idempotency semantics as the PostgreSQL queue), then the §64 validation."""
    import time

    import numpy as np

    from vkm_corpus.embeddings.artifacts import ArtifactWriter, existing_hashes_in, validate
    from vkm_corpus.embeddings.llama import LlamaServerClient
    from vkm_corpus.embeddings.reembed import CanonObject, plan
    from vkm_corpus.embeddings.signature import document_config, text_hash
    from vkm_corpus.embeddings.specs import get
    from vkm_corpus.embeddings.tokenize import SpecTokenizer, file_sha256
    from vkm_corpus.embeddings.worker import EmbeddingWorker, InMemoryJobQueue, LlamaDocumentEncoder
    from vkm_corpus.retrieval_service.config import load_config

    cfg = load_config(path=args.config)
    texts: dict[str, str] = {}
    canon: dict[str, CanonObject] = {}
    with open(args.docs, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            r = json.loads(line)
            texts[r["object_id"]] = r["text"]
            canon[r["object_id"]] = CanonObject(r["object_id"], text_hash(r["text"]), source_id=r.get("source_id"),
                                                page_id=r.get("page_id"),
                                                object_type=r.get("object_type") or r.get("object_kind"))

    class _Source:
        def objects(self, object_ids):
            return [(canon[i], texts[i]) for i in object_ids]

    report = []
    for slot in cfg.models:
        if args.roles and slot.role not in args.roles:
            continue
        spec = get(slot.key)
        mode = "multivector" if slot.role == "late" else "dense"
        tok_path = Path(slot.tokenizer_dir) / spec.tokenizer_file
        config = document_config(
            spec, mode=mode, weights_file=Path(slot.gguf).name,
            weights_sha256=slot.gguf_sha256 or file_sha256(slot.gguf), quantization=slot.quant,
            tokenizer_sha256=slot.tokenizer_sha256 or file_sha256(tok_path), dimension=slot.dimension,
            max_len=slot.doc_max_len, heads_sha256=slot.heads_sha256 or (file_sha256(slot.heads) if slot.heads else ""),
            text_rule=args.text_rule)
        writer = ArtifactWriter(Path(args.data_root), mode, config, writer_id=args.writer)
        existing = existing_hashes_in(writer.dir)          # key columns only (corpus scale)
        p = plan(config.signature(), canon.values(), existing)
        queue = InMemoryJobQueue()
        queue.add(config.signature(), mode, [o.object_id for o in p.to_embed], args.job_size)
        encoder = LlamaDocumentEncoder(
            spec, SpecTokenizer.from_dir(spec, slot.tokenizer_dir),
            LlamaServerClient(slot.endpoint, pooled=(mode == "dense" and spec.pooling != "none")), mode=mode,
            max_len=slot.doc_max_len, dimension=slot.dimension, heads=dict(np.load(slot.heads)) if slot.heads else {},
            backend=args.backend, batch=args.batch)
        worker = EmbeddingWorker(queue, _Source(), encoder, writer, device=args.device, name=args.writer)
        t0 = time.perf_counter()
        results = worker.run()
        seconds = time.perf_counter() - t0
        embedded = sum(r["embedded"] for r in results)
        if args.skip_validate:     # §64 runs at import (search build-vectors streams the parts; this one loads all rows)
            validation = {"ok": queue.counts().get("FAILED", 0) == 0, "skipped": True}
        else:
            validation = validate(writer.dir, config, {o.object_id: o.text_hash for o in canon.values()}).as_dict()
        report.append({"role": slot.role, "key": slot.key, "kind": mode, "config_signature": config.signature(),
                       "dir": str(writer.dir), "objects": len(canon), "plan": p.summary(), "jobs": len(results),
                       "embedded": embedded, "seconds": round(seconds, 2),
                       "objects_per_s": round(embedded / seconds, 1) if embedded and seconds else None,
                       "queue": queue.counts(), "validation": validation})
    print(json.dumps(report, indent=1, ensure_ascii=False))
    return 0 if report and all(r["validation"]["ok"] for r in report) else 1


def register(subparsers) -> None:
    p = subparsers.add_parser("embed", help="embeddings: specs, signatures, re-embed plan, artifact validation")
    sub = p.add_subparsers(dest="embed_cmd", required=True)
    s = sub.add_parser("specs", help="encoder specifications")
    s.set_defaults(func=_specs)
    s = sub.add_parser("signature", help="document and query signatures of a deployment")
    s.add_argument("--key", required=True)
    s.add_argument("--gguf", required=True)
    s.add_argument("--quant", required=True)
    s.add_argument("--tokenizer-dir", required=True)
    s.add_argument("--heads")
    s.add_argument("--mode", default="dense", choices=["dense", "sparse", "multivector", "visual", "context"])
    s.add_argument("--dim", type=int)
    s.add_argument("--max-len", type=int, default=512)
    s.add_argument("--query-max-len", type=int, default=512)
    s.add_argument("--text-rule", default="vkm-units-v1/A")
    s.add_argument("--precision", default="float32", choices=["float32", "float16", "int8", "binary"])
    s.set_defaults(func=_signature)
    s = sub.add_parser("plan", help="re-embed plan of an artifact directory (§46)")
    s.add_argument("--dir", required=True)
    s.add_argument("--canon", required=True)
    s.set_defaults(func=_plan)
    s = sub.add_parser("validate", help="pre-import checks of an artifact directory (§64)")
    s.add_argument("--dir", required=True)
    s.add_argument("--canon", required=True)
    s.set_defaults(func=_validate)
    s = sub.add_parser("encode", help="objects JSONL → llama-servers of a service config → derived artifacts (§39–46)")
    s.add_argument("--config", required=True, help="service config JSON (model slots with endpoints and sha256)")
    s.add_argument("--docs", required=True, help="JSONL rows {object_id, text, source_id?, page_id?, object_type?}")
    s.add_argument("--data-root", required=True, help="root under which derived/embeddings/… is written")
    s.add_argument("--roles", nargs="*", default=[], choices=["dense", "late"])
    s.add_argument("--text-rule", default="vkm-units-v1/A")
    s.add_argument("--writer", default="rx580-w0")
    s.add_argument("--device", default="RX580", choices=["RX580", "RTX5070"])
    s.add_argument("--backend", default="llama.cpp-vulkan")
    s.add_argument("--job-size", type=int, default=64)
    s.add_argument("--batch", type=int, default=8)
    s.add_argument("--skip-validate", action="store_true",
                   help="skip the in-memory §64 check (large corpora: `search build-vectors` validates by streaming)")
    s.set_defaults(func=_encode)


def main(argv: list[str] | None = None) -> int:
    """``python -m vkm_corpus.embeddings.cli …`` — works before the coordinator registers the ``embed`` group."""
    import sys

    parser = argparse.ArgumentParser(prog="vkm-embed")
    register(parser.add_subparsers(dest="group"))
    args = parser.parse_args(["embed", *(sys.argv[1:] if argv is None else argv)])
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
