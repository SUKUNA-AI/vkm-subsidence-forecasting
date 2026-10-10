"""``vkm-corpus`` command line.

Command groups live in their packages and are imported only when used, so the CLI starts without optional
dependencies. A group module exposes ``register(subparsers)`` that adds its parser with ``func`` defaults.
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys

from vkm_corpus.config import ConfigError, load_settings
from vkm_corpus.versions import PIPELINE_VERSION

# group name → module with register(subparsers); modules may be absent while the platform is being built
GROUPS: dict[str, str] = {
    "registry": "vkm_corpus.registry.cli",      # D: SOURCE_REGISTER, Work register, sources/works rows
    "canon": "vkm_corpus.parquet.cli",          # D: validate, commit markers, snapshot, manifest
    "duckdb": "vkm_corpus.duckdb.cli",          # D: rebuild the query layer from a snapshot
    "run": "vkm_corpus.pipeline.cli",           # C: document pipeline (--resume --force --source --page ...)
    "ocr": "vkm_corpus.ocr.cli",                # C: GLM-OCR server checks and smoke
    "graph": "vkm_corpus.graph.cli",            # E: Neo4j DOCUMENT graph projection
    "search": "vkm_corpus.search.cli",          # E: OpenSearch retrieval projection
    "rerank": "vkm_corpus.retrieval.cli",       # F: rerank gateway client and acceptance
    "api": "vkm_corpus.api.cli",                # G: VKM API
    "mcp": "vkm_corpus.mcp.cli",                # G: VKM Corpus MCP (read / admin)
    "ops": "vkm_corpus.ops.cli",                # coordinator: PostgreSQL control plane, jobs, worker
    "core": "vkm_corpus.publish.cli",           # coordinator: publish, admit, reconcile, backup
    "retrieval-lab": "vkm_corpus.retrieval_lab.cli",  # J: retrieval benchmark (derived, experimental)
    "embed": "vkm_corpus.embeddings.cli",                # K: embeddings as versioned derived artifacts
    "retrieval-service": "vkm_corpus.retrieval_service.cli",  # K: RX580 dense + late-interaction service
    "nav": "vkm_corpus.navigation.cli",                  # N: navigation layer (outlines, sections, formulas, concepts)
    "catalogues": "vkm_corpus.catalogues.cli",           # topic dossier: PUBLIC evidence catalogues as a DuckDB pack
    "evidence": "vkm_evidence.cli",                    # immutable evidence, review and admission journal
    "update": "vkm_corpus.update.cli",                 # qualified, resumable campaign contract
    "deployment": "vkm_corpus.update.operator",         # fixed operator-owned CORE receiver lifecycle
    "bootstrap": "vkm_corpus.update.bootstrap_native",  # isolated CLOSED first baseline, no serving admission
}


def _config_show(args: argparse.Namespace) -> int:
    settings = load_settings()
    print(json.dumps({"pipeline_version": PIPELINE_VERSION, **settings.redacted()}, indent=2, ensure_ascii=False))
    return 0


def build_parser(argv: list[str] | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vkm-corpus", description="VKM Corpus Platform v0")
    parser.add_argument("--version", action="version", version=f"vkm-corpus pipeline {PIPELINE_VERSION}")
    sub = parser.add_subparsers(dest="group", metavar="<group>")
    config = sub.add_parser("config", help="show resolved configuration (secrets redacted)")
    config.set_defaults(func=_config_show)
    wanted = None
    if argv:
        wanted = next((a for a in argv if not a.startswith("-")), None)
    for name, module in GROUPS.items():
        if wanted not in (None, name):
            continue
        try:
            importlib.import_module(module).register(sub)
        except ModuleNotFoundError as exc:
            if wanted == name:
                raise SystemExit(f"command group '{name}' is unavailable: {exc}") from exc
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser(argv)
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        return int(args.func(args) or 0)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
