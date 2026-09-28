"""``vkm-corpus nav …`` — navigation layer (NAV): native outlines and derived datasets, no LLM.

* ``nav outlines --out FILE [--resources ROOT] [--source SID …]`` — WORKSTATION: native outlines of the PRIVATE
  files (PDF bookmarks, EPUB navigation, DjVu outline) → one JSON per snapshot (:mod:`vkm_corpus.navigation.outline`);
* ``nav build --duckdb PATH --out DIR [--outlines FILE] [--part sections|formulas|parameters|concepts|all]
  [--inputs DIR]`` — derived datasets from a DuckDB copy of the canon (opened read only) → ``<DIR>/<dataset>.parquet``
  + ``manifest.json`` (rule versions, row counts, sha256, snapshot id).

Parts are dispatched through :data:`PARTS` (``module:function``), imported lazily; a part whose module is absent is
skipped and recorded as such. A builder has the signature ``build(con, **kwargs) -> dict[str, pyarrow.Table]`` and
receives only the keyword arguments it declares among ``outlines`` (native outlines), ``datasets`` (tables built by
earlier parts of the same run), each earlier table by its dataset name (e.g. ``section_pages``) and ``stats`` (a dict
it may fill with counters for the manifest). Tables of earlier parts are also registered in the connection as
``nav_<dataset>`` (e.g. ``nav_sections``). ``--inputs DIR`` offers the datasets of an earlier build of the same
snapshot (``<DIR>/<dataset>.parquet``, not rebuilt in this run) the same way, so one part can be rebuilt alone.
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
    "parameters": "vkm_corpus.navigation.parameters:build",
    "concepts": "vkm_corpus.navigation.concepts:build",
}
MANIFEST_FORMAT = "vkm-nav-manifest-v1"
# the part that produces each dataset (``--inputs`` never feeds a part its own earlier output)
_PART_OF: dict[str, str] = {
    "sections": "sections", "section_pages": "sections", "formula_context": "formulas", "formula_symbols": "formulas",
    "formula_refs": "formulas", "formula_parameters": "formulas", "parameter_candidates": "parameters",
    "parameter_summary": "parameters", "terms": "concepts", "term_mentions": "concepts", "term_edges": "concepts",
}


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


def load_inputs(directory: str | Path, skip: set[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Datasets of an earlier build (``<dir>/<dataset>.parquet``) except ``skip``: tables and their references."""
    import pyarrow.parquet as pq

    from vkm_corpus.navigation.ids import DATASETS

    tables: dict[str, Any] = {}
    refs: dict[str, Any] = {}
    for name in DATASETS:
        path = Path(directory) / f"{name}.parquet"
        if name in skip or not path.is_file():
            continue
        tables[name] = pq.read_table(path)
        refs[name] = {"file": path.name, "sha256": _sha256_file(path), "rows": tables[name].num_rows}
    return tables, refs


def build_parts(con, out_dir: Path, parts: list[str], *, outlines: dict | None = None,
                outlines_ref: dict[str, Any] | None = None, inputs: dict[str, Any] | None = None,
                inputs_ref: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the requested parts over an open DuckDB connection and write Parquet files + ``manifest.json``.
    ``inputs`` are datasets of an earlier build offered to the builders like those of earlier parts."""
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
    if inputs_ref:
        manifest["inputs"] = inputs_ref
    built: dict[str, Any] = dict(inputs or {})
    for name, table in built.items():
        con.register(f"nav_{name}", table)
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
    inputs, inputs_ref = None, None
    if getattr(args, "inputs", None):
        from vkm_corpus.navigation.ids import DATASETS

        own = set()
        for part in parts:                      # datasets produced by the parts of this run are not taken as inputs
            own |= {d for d in DATASETS if _PART_OF.get(d) == part}
        inputs, inputs_ref = load_inputs(args.inputs, own)
    con = duckdb.connect(str(args.duckdb), read_only=True)
    try:
        manifest = build_parts(con, Path(args.out), parts, outlines=outlines, outlines_ref=ref, inputs=inputs,
                               inputs_ref=inputs_ref)
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


def register(subparsers) -> None:
    p = subparsers.add_parser("nav", help="navigation layer: outlines, sections, formulas, parameters, concepts "
                                          "(derived)")
    sub = p.add_subparsers(dest="nav_cmd", metavar="<command>")
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
    b.add_argument("--inputs", default=None, help="directory of an earlier build of the same snapshot whose datasets "
                                                  "are offered to the parts (rebuild one part alone)")
    b.set_defaults(func=cmd_build)
    p.set_defaults(func=lambda args: (p.print_help(), 2)[1])
