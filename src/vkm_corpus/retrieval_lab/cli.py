"""CLI of the retrieval lab: ``python -m vkm_corpus.retrieval_lab.cli <command>`` (group ``retrieval-lab`` of
``vkm-corpus`` once the coordinator registers it).

Commands: ``validate-bench``, ``splits``, ``models``, ``gpu``, ``units``, ``run``. Paths are arguments or come from
``load_settings()`` (``VKM_MODELS_DIR``, ``VKM_OPENSEARCH_URL``, ``VKM_RERANK_URL``); nothing is hard-coded.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_BENCH = REPO_ROOT / "benchmarks" / "retrieval_v0"


def _print(obj) -> None:
    print(json.dumps(obj, indent=1, ensure_ascii=False, default=str))


def _cmd_validate(args: argparse.Namespace) -> int:
    from vkm_corpus.retrieval_lab.bench import load_benchmark, validate_benchmark

    bench = load_benchmark(args.bench)
    problems = validate_benchmark(bench, min_text=args.min_text, min_visual=args.min_visual)
    _print({"stats": bench.stats(), "problems": problems[:50], "n_problems": len(problems)})
    return 1 if problems else 0


def _cmd_splits(args: argparse.Namespace) -> int:
    from vkm_corpus.retrieval_lab.bench import load_queries, make_splits, splits_document

    queries = load_queries(Path(args.bench) / "queries.jsonl")
    doc = splits_document(make_splits(queries))
    if args.write:
        (Path(args.bench) / "splits.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n",
                                                      encoding="utf-8", newline="\n")
    counts: dict[str, int] = {}
    for v in doc["assignments"].values():
        counts[v] = counts.get(v, 0) + 1
    _print({"written": bool(args.write), "counts": counts})
    return 0


def _models_dir(args: argparse.Namespace):
    if getattr(args, "models_dir", None):
        return Path(args.models_dir)
    from vkm_corpus.config import load_settings

    return load_settings().models_dir


def _cmd_models(args: argparse.Namespace) -> int:
    from vkm_corpus.retrieval_lab.encoders import load_specs

    specs = load_specs(Path(args.bench) / "configs" / "models.json")
    mdir = _models_dir(args)
    rows = []
    for s in specs.values():
        if s.family == "fake":
            continue
        present = bool(mdir and s.local_dir(mdir).is_dir())
        rows.append({"key": s.key, "model": s.model_id, "revision": s.revision[:12], "family": s.family,
                     "role": s.role, "license": s.license, "present": present})
    _print(rows)
    return 0 if all(r["present"] for r in rows) or not args.check else 1


def _cmd_gpu(args: argparse.Namespace) -> int:
    from vkm_corpus.retrieval_lab.gpu import probe

    state = probe()
    _print({**state.__dict__, "allows_2GiB_model": state.allows(2048)})
    return 0


def _reader(args: argparse.Namespace):
    from vkm_corpus.retrieval_lab.canon import CanonReader

    if args.duckdb:
        return CanonReader.from_duckdb(args.duckdb)
    from vkm_corpus.parquet.layout import CanonLayout

    return CanonReader.from_layout(CanonLayout(Path(args.canon_root)))


def _build_units(reader, sources, with_pages: bool):
    from vkm_corpus.retrieval_lab.units import UnitConfig, build_units

    rows = reader.load_all(sources)
    return build_units(rows["pages"], rows["blocks"], rows["figures"], rows["tables"], rows["formulas"],
                       rows["bibliography"], UnitConfig(with_pages=with_pages))


def _write_units(path: Path, units) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pa.table({"unit_id": [u.unit_id for u in units], "kind": [u.kind for u in units],
                      "source_id": [u.source_id for u in units], "page_id": [u.page_id for u in units],
                      "object_ids": [list(u.object_ids) for u in units], "text_sha256": [u.text_sha256 for u in units],
                      "n_chars": [len(u.text) for u in units], "flags": [list(u.flags) for u in units]})
    pq.write_table(table, path)


def _cmd_units(args: argparse.Namespace) -> int:
    from vkm_corpus.retrieval_lab.units import units_summary

    reader = _reader(args)
    units = _build_units(reader, args.sources.split(",") if args.sources else None, args.with_pages)
    summary = {"snapshot": reader.snapshot(), **units_summary(units)}
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        _write_units(out / "units.parquet", units)
    _print(summary)
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    from vkm_corpus.retrieval_lab.bench import load_benchmark
    from vkm_corpus.retrieval_lab.encoders import load_specs
    from vkm_corpus.retrieval_lab.engineering import environment
    from vkm_corpus.retrieval_lab.gpu import probe
    from vkm_corpus.retrieval_lab.runner import LabRun, RunConfig, rerank_texts_for

    bench = load_benchmark(args.bench)
    specs = load_specs(Path(args.bench) / "configs" / "models.json")
    reader = _reader(args)
    units = _build_units(reader, args.sources.split(",") if args.sources else None, False)
    split = lambda v: [x for x in (v or "").split(",") if x]  # noqa: E731
    device = args.device
    gpu_probes = []
    if device in ("auto", "cuda"):
        from vkm_corpus.retrieval_lab.gpu import choose_device

        device, samples = choose_device(args.need_mib, prefer="auto")
        gpu_probes = [s.as_dict() for s in samples]
    cfg = RunConfig(out_dir=Path(args.out), dense=split(args.dense), late=split(args.late), sparse=split(args.sparse),
                    m3=split(args.m3), pipelines=split(args.pipelines), context_variant=args.context, device=device,
                    compute_precision=args.precision, threads=args.threads, batch_size=args.batch_size, bm25=args.bm25,
                    track=args.track)
    reranker = None
    if any(p in ("G", "H", "I") for p in cfg.pipelines):
        from vkm_corpus.retrieval_lab.rerank import EdgeReranker, RerankUnavailable

        try:
            reranker = EdgeReranker.from_settings()
        except RerankUnavailable as exc:
            print(f"reranker NOT_RUN: {exc}", file=sys.stderr)
    canon_rerank = {r["object_id"]: r["text"] for r in reader._rows("SELECT object_id, text FROM rerank_text")}
    bm25_backend = None
    if args.bm25 == "opensearch":
        from vkm_corpus.retrieval_lab.bm25 import OpenSearchBM25
        from vkm_corpus.search.client import connect

        client = connect(url=args.opensearch_url) if args.opensearch_url else connect(_settings())
        docs = [{"unit_id": u.unit_id, "kind": u.kind, "source_id": u.source_id, "page_id": u.page_id,
                 "text": u.text} for u in units]
        bm25_backend = OpenSearchBM25.create(client, Path(args.out).name, docs)
    meta = {"snapshot": reader.snapshot(), "canon": reader.origin, "environment": environment(), "device": device,
            "argv": sys.argv[1:], "gpu_probes_start": gpu_probes}
    run = LabRun(cfg, bench, units, specs, models_dir=_models_dir(args), meta=meta,
                 rerank_texts=rerank_texts_for(units, canon_rerank), reranker=reranker,
                 source_meta=reader.source_meta(), page_labels=reader.page_labels(), bm25_backend=bm25_backend)
    try:
        report = run.run()
    finally:
        if bm25_backend is not None and not args.keep_index:
            bm25_backend.cleanup()
    if device == "cuda":
        report["run"]["gpu_probe_end"] = probe().as_dict()
        (Path(args.out) / "metrics.json").write_text(json.dumps(report, indent=1, ensure_ascii=False, default=str),
                                                     encoding="utf-8")
    _print({k: {"status": v.get("status"), "overall": {m: round(x, 4) for m, x in (v.get("overall") or {}).items()}}
            for k, v in report["systems"].items()})
    return 0


def _settings():
    from vkm_corpus.config import load_settings

    return load_settings()


def _add_canon_args(p: argparse.ArgumentParser) -> None:
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--duckdb", help="materialised DuckDB of a CANONICAL root (read only)")
    g.add_argument("--canon-root", help="CANONICAL data root (in-memory DuckDB over CURRENT)")
    p.add_argument("--sources", help="comma-separated source IDs (default: all in the snapshot)")


def register(subparsers) -> None:
    parser = subparsers.add_parser("retrieval-lab", help="retrieval benchmark (agent J)")
    add_commands(parser)


def add_commands(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="lab_command", metavar="<command>")
    p = sub.add_parser("validate-bench", help="validate benchmark files")
    p.add_argument("--bench", default=str(DEFAULT_BENCH))
    p.add_argument("--min-text", type=int, default=0)
    p.add_argument("--min-visual", type=int, default=0)
    p.set_defaults(func=_cmd_validate)
    p = sub.add_parser("splits", help="(re)compute train/dev/test splits")
    p.add_argument("--bench", default=str(DEFAULT_BENCH))
    p.add_argument("--write", action="store_true")
    p.set_defaults(func=_cmd_splits)
    p = sub.add_parser("models", help="model specs and local presence")
    p.add_argument("--bench", default=str(DEFAULT_BENCH))
    p.add_argument("--models-dir")
    p.add_argument("--check", action="store_true")
    p.set_defaults(func=_cmd_models)
    p = sub.add_parser("gpu", help="GPU policy state (vLLM queue, free VRAM)")
    p.set_defaults(func=_cmd_gpu)
    p = sub.add_parser("units", help="build units of a snapshot")
    _add_canon_args(p)
    p.add_argument("--with-pages", action="store_true")
    p.add_argument("--out")
    p.set_defaults(func=_cmd_units)
    p = sub.add_parser("run", help="run pipelines and metrics")
    _add_canon_args(p)
    p.add_argument("--bench", default=str(DEFAULT_BENCH))
    p.add_argument("--out", required=True, help="run directory (git-ignored work dir or data root)")
    p.add_argument("--models-dir")
    p.add_argument("--dense", default="")
    p.add_argument("--late", default="")
    p.add_argument("--sparse", default="")
    p.add_argument("--m3", default="")
    p.add_argument("--pipelines", default="A,B")
    p.add_argument("--context", default="A", choices=["A", "B", "C", "D"])
    p.add_argument("--device", default="cpu", choices=["cpu", "auto"],
                   help="auto: RTX only inside a coordinator-rule idle window (OCR idle ≥ 5 min, ≥ 6 GB free)")
    p.add_argument("--need-mib", type=int, default=4096, help="VRAM a GPU run needs (auto device)")
    p.add_argument("--precision", default="fp32", choices=["fp32", "bf16", "fp16"])
    p.add_argument("--threads", type=int, default=8)
    p.add_argument("--batch-size", type=int, default=16)
    p.add_argument("--bm25", default="local", choices=["local", "opensearch"])
    p.add_argument("--opensearch-url")
    p.add_argument("--keep-index", action="store_true")
    p.add_argument("--track", default="text", choices=["text", "visual"])
    p.set_defaults(func=_cmd_run)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="retrieval-lab")
    add_commands(parser)
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
