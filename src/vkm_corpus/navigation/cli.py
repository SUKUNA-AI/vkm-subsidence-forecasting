"""``vkm-corpus nav …`` — navigation layer (NAV): native outlines and derived datasets, no LLM.

* ``nav outlines --out FILE [--resources ROOT] [--source SID …]`` — WORKSTATION: native outlines of the PRIVATE
  files (PDF bookmarks, EPUB navigation, DjVu outline) → one JSON per snapshot (:mod:`vkm_corpus.navigation.outline`);
* ``nav build --duckdb PATH --out DIR [--outlines FILE] [--part sections|formulas|concepts|all]`` — derived datasets
  from a DuckDB copy of the canon (opened read only) → ``<DIR>/<dataset>.parquet`` + ``manifest.json`` (rule versions,
  row counts, sha256, snapshot id).

Parts are dispatched through :data:`PARTS` (``module:function``), imported lazily; a part whose module is absent is
skipped and recorded as such. A builder has the signature ``build(con, **kwargs) -> dict[str, pyarrow.Table]`` and
receives only the keyword arguments it declares among ``outlines`` (native outlines), ``datasets`` (tables built by
earlier parts of the same run), each earlier table by its dataset name (e.g. ``section_pages``) and ``stats`` (a dict
it may fill with counters for the manifest). Tables of earlier parts are also registered in the connection as
``nav_<dataset>`` (e.g. ``nav_sections``).
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import inspect
import json
import sys
import time
from pathlib import Path
from typing import Any

PARTS: dict[str, str] = {
    "sections": "vkm_corpus.navigation.sections:build",
    "formulas": "vkm_corpus.navigation.formulas:build",
    "concepts": "vkm_corpus.navigation.concepts:build",
}
MANIFEST_FORMAT = "vkm-nav-manifest-v1"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve_part(part: str):
    """The builder callable of a part, or None when its module is not (yet) in the package."""
    module_name, func = PARTS[part].split(":")
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name:
            return None
        raise
    return getattr(module, func)


def call_builder(builder, con, available: dict[str, Any]):
    params = inspect.signature(builder).parameters
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        kwargs = dict(available)
    else:
        kwargs = {k: v for k, v in available.items() if k in params}
    return builder(con, **kwargs)


def snapshot_of(con) -> dict[str, Any]:
    try:
        row = con.execute("SELECT snapshot_id, manifest_sha256, pipeline_version FROM meta.snapshot").fetchone()
    except Exception:
        return {"snapshot_id": None, "manifest_sha256": None, "pipeline_version": None}
    return dict(zip(("snapshot_id", "manifest_sha256", "pipeline_version"), row or (None, None, None)))


def build_parts(con, out_dir: Path, parts: list[str], *, outlines: dict | None = None,
                outlines_ref: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the requested parts over an open DuckDB connection and write Parquet files + ``manifest.json``."""
    import pyarrow.parquet as pq

    from vkm_corpus.navigation.ids import RULE_VERSIONS

    out_dir.mkdir(parents=True, exist_ok=True)
    snap = snapshot_of(con)
    mpath = out_dir / "manifest.json"
    manifest: dict[str, Any] = {}
    if mpath.is_file():
        old = json.loads(mpath.read_text(encoding="utf-8"))
        if old.get("format") == MANIFEST_FORMAT and old.get("snapshot", {}).get("snapshot_id") == snap["snapshot_id"]:
            manifest = old
    manifest.update({"format": MANIFEST_FORMAT, "snapshot": snap, "layer_status": "DERIVED",
                     "review_status": "AUTO_EXTRACTED_UNREVIEWED"})
    manifest.setdefault("parts", {})
    manifest.setdefault("datasets", {})
    if outlines_ref is not None:
        manifest["outlines"] = outlines_ref
    built: dict[str, Any] = {}
    for part in parts:
        builder = resolve_part(part)
        if builder is None:
            manifest["parts"][part] = {"status": "SKIPPED_MODULE_MISSING", "rule_version": RULE_VERSIONS.get(part)}
            continue
        stats: dict[str, Any] = {}
        t0 = time.monotonic()
        # earlier datasets are also offered by name (e.g. ``section_pages=``) to builders that declare them
        tables = call_builder(builder, con, {**built, "outlines": outlines, "datasets": dict(built), "stats": stats})
        names = []
        for name, table in tables.items():
            path = out_dir / f"{name}.parquet"
            tmp = path.with_suffix(".parquet.tmp")
            pq.write_table(table, tmp, compression="zstd")
            tmp.replace(path)
            manifest["datasets"][name] = {"path": path.name, "part": part, "rows": table.num_rows,
                                          "columns": table.schema.names, "sha256": _sha256_file(path)}
            built[name] = table
            con.register(f"nav_{name}", table)
            names.append(name)
        manifest["parts"][part] = {"status": "BUILT", "rule_version": RULE_VERSIONS.get(part), "datasets": names,
                                   "seconds": round(time.monotonic() - t0, 2), "stats": stats}
    manifest["built_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    tmp = mpath.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True, default=str), encoding="utf-8")
    tmp.replace(mpath)
    return manifest


def cmd_build(args: argparse.Namespace) -> int:
    import duckdb

    outlines, ref = None, None
    if args.outlines:
        from vkm_corpus.navigation.outline import load_outlines

        outlines = load_outlines(args.outlines)
        ref = {"file": Path(args.outlines).name, "sha256": _sha256_file(Path(args.outlines)),
               "n_sources": len(outlines)}
    parts = list(PARTS) if args.part == "all" else [args.part]
    con = duckdb.connect(str(args.duckdb), read_only=True)
    try:
        manifest = build_parts(con, Path(args.out), parts, outlines=outlines, outlines_ref=ref)
    finally:
        con.close()
    summary = {"snapshot": manifest["snapshot"],
               "parts": {k: {"status": v["status"], "datasets": v.get("datasets", [])}
                         for k, v in manifest["parts"].items()},
               "rows": {k: v["rows"] for k, v in manifest["datasets"].items()}}
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


def cmd_outlines(args: argparse.Namespace) -> int:
    from vkm_corpus.navigation.outline import extract_all

    if args.resources:
        root = Path(args.resources)
    else:
        from vkm_corpus.config import load_settings

        root = load_settings().require_resources_root()
    log = (lambda msg: print(msg, file=sys.stderr)) if args.verbose else None
    data = extract_all(root, only=set(args.source) if args.source else None, log=log)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(out)
    print(json.dumps({"out": out.name, "stats": data["stats"], "extractors": data["extractors"]},
                     ensure_ascii=False, indent=1))
    return 0


# ---------------------------------------------------------------- NAV graph in Neo4j (agent G): graph-ddl/-load/-verify/-drop
def _graph_print(obj: Any) -> None:
    from vkm_corpus.graph.common import normalize_value

    print(json.dumps(normalize_value(obj), ensure_ascii=False, indent=1, sort_keys=True, default=str))


def _graph_fail(exc: Any) -> int:
    print(json.dumps({"status": "FAILED", "error": exc.as_dict()}, ensure_ascii=False, indent=1, default=str),
          file=sys.stderr)
    return 1


def _graph_options(args: argparse.Namespace, command: str):
    from vkm_corpus.graph.nav import NavLoadOptions
    from vkm_corpus.graph.nav_rows import ProjectionOptions

    return NavLoadOptions(
        nav_dir=Path(args.nav_dir), batch_nodes=getattr(args, "batch_nodes", 5_000),
        batch_rels=getattr(args, "batch_rels", 10_000), dry_run=getattr(args, "dry_run", False),
        allow_snapshot_mismatch=getattr(args, "allow_snapshot_mismatch", False),
        canon_duckdb=Path(args.canon_duckdb) if getattr(args, "canon_duckdb", None) else None,
        projection=ProjectionOptions(mentions_top_per_term=args.mentions_per_term,
                                     mentions_top_per_section=args.mentions_per_section,
                                     symbol_morphology=args.symbol_morphology,
                                     verify_files=not args.skip_file_hash),
        command=command)


def cmd_graph_ddl(args: argparse.Namespace) -> int:
    from vkm_corpus.graph import nav_schema

    if args.print:
        sys.stdout.write(nav_schema.ddl_script())
        return 0
    from vkm_corpus.config import load_settings
    from vkm_corpus.graph import client
    from vkm_corpus.graph.common import ProjectionError
    from vkm_corpus.graph.nav import apply_ddl
    from vkm_corpus.graph.schema import Namespace

    settings = load_settings()
    try:
        driver = client.connect(settings)
    except ProjectionError as exc:
        return _graph_fail(exc)
    try:
        _graph_print(apply_ddl(driver, settings.neo4j_database, Namespace()))
    except ProjectionError as exc:
        return _graph_fail(exc)
    finally:
        driver.close()
    return 0


def cmd_graph_load(args: argparse.Namespace) -> int:
    from vkm_corpus.config import load_settings
    from vkm_corpus.graph.common import ProjectionError
    from vkm_corpus.graph.nav import load

    options = _graph_options(args, " ".join(["nav", "graph-load", *sys.argv[3:]]))
    settings = None if args.dry_run else load_settings()
    try:
        receipt = load(settings, options)
    except ProjectionError as exc:
        return _graph_fail(exc)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        from vkm_corpus.graph.common import normalize_value

        out.write_bytes((json.dumps(normalize_value(receipt), ensure_ascii=False, indent=1, sort_keys=True,
                                    default=str) + "\n").encode("utf-8"))
    keys = ("status", "run_id", "receipt_ref", "input", "totals", "expected_counts", "preflight_summary",
            "checks_summary", "document", "sweep", "timings_s", "document_references")
    _graph_print({k: receipt.get(k) for k in keys if k in receipt})
    return 0 if receipt.get("status") in ("COMPLETE", "DRY_RUN") else 1


def cmd_graph_verify(args: argparse.Namespace) -> int:
    from vkm_corpus.config import load_settings
    from vkm_corpus.graph.common import ProjectionError
    from vkm_corpus.graph.nav import verify

    try:
        result = verify(load_settings(), _graph_options(args, "nav graph-verify"))
    except ProjectionError as exc:
        return _graph_fail(exc)
    _graph_print(result)
    return 0 if result["status"] == "PASS" else 1


def cmd_graph_drop(args: argparse.Namespace) -> int:
    from vkm_corpus.config import load_settings
    from vkm_corpus.graph import client
    from vkm_corpus.graph.common import ProjectionError
    from vkm_corpus.graph.nav import drop_layer

    if not args.yes:
        print("refusing to drop the NAV layer without --yes (DOCUMENT is not touched)", file=sys.stderr)
        return 2
    settings = load_settings()
    try:
        driver = client.connect(settings)
    except ProjectionError as exc:
        return _graph_fail(exc)
    try:
        _graph_print(drop_layer(driver, settings.neo4j_database))
    finally:
        driver.close()
    return 0


def _register_graph(sub) -> None:
    def projection_args(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--nav-dir", required=True,
                            help="NAV build directory (<dataset>.parquet + manifest.json), e.g. "
                                 "$VKM_DATA_ROOT/derived/navigation/<snapshot_id>")
        parser.add_argument("--mentions-per-term", type=int, default=5, help="MENTIONED_IN: top sections per term")
        parser.add_argument("--mentions-per-section", type=int, default=20, help="MENTIONED_IN: top terms per section")
        parser.add_argument("--symbol-morphology", choices=["auto", "surface"], default="auto",
                            help="SYMBOL_OF: the builder's morphology when importable (auto) or surface forms")
        parser.add_argument("--skip-file-hash", action="store_true", help="do not recompute dataset sha256")

    d = sub.add_parser("graph-ddl", help="NAV graph: apply the idempotent Neo4j DDL (constraints, indexes)")
    d.add_argument("--print", action="store_true", help="print the DDL script instead of applying it")
    d.set_defaults(func=cmd_graph_ddl)
    ld = sub.add_parser("graph-load", help="NAV graph: load a NAV build into Neo4j (MERGE by ids, checks N1-N7)")
    projection_args(ld)
    ld.add_argument("--dry-run", action="store_true", help="row counts, accounting and preflight only; no database")
    ld.add_argument("--canon-duckdb", default=None, help="dry run: also resolve DOCUMENT ids in this canonical DuckDB")
    ld.add_argument("--batch-nodes", type=int, default=5_000)
    ld.add_argument("--batch-rels", type=int, default=10_000)
    ld.add_argument("--allow-snapshot-mismatch", action="store_true",
                    help="load although the DOCUMENT graph was built from another snapshot")
    ld.add_argument("--out", default=None, help="also write the full receipt JSON here")
    ld.set_defaults(func=cmd_graph_load)
    vf = sub.add_parser("graph-verify", help="NAV graph: checks N1-N7 of the loaded layer, without writing")
    projection_args(vf)
    vf.set_defaults(func=cmd_graph_verify)
    dr = sub.add_parser("graph-drop", help="NAV graph: remove the NAV layer (before a DOCUMENT rebuild)")
    dr.add_argument("--yes", action="store_true", help="confirm")
    dr.set_defaults(func=cmd_graph_drop)
# ---------------------------------------------------------------- end NAV graph (agent G)


def register(subparsers) -> None:
    p = subparsers.add_parser("nav", help="navigation layer: outlines, sections, formulas, concepts (derived)")
    sub = p.add_subparsers(dest="nav_cmd", metavar="<command>")
    _register_graph(sub)
    o = sub.add_parser("outlines", help="native outlines of the PRIVATE files (workstation) → JSON")
    o.add_argument("--out", required=True, help="output JSON file")
    o.add_argument("--resources", default=None, help="PRIVATE clone (default: $VKM_RESOURCES_ROOT)")
    o.add_argument("--source", action="append", default=None, help="only this source id (repeatable)")
    o.add_argument("--verbose", action="store_true")
    o.set_defaults(func=cmd_outlines)
    b = sub.add_parser("build", help="derived navigation datasets from a DuckDB copy of the canon")
    b.add_argument("--duckdb", required=True, help="DuckDB file of a snapshot (opened read only)")
    b.add_argument("--out", required=True, help="output directory (<dataset>.parquet + manifest.json)")
    b.add_argument("--outlines", default=None, help="outlines JSON of `nav outlines` (same snapshot)")
    b.add_argument("--part", default="all", choices=[*PARTS, "all"])
    b.set_defaults(func=cmd_build)
    p.set_defaults(func=lambda args: (p.print_help(), 2)[1])
