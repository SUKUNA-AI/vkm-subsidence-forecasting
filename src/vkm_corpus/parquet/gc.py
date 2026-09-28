"""Garbage of a data root: orphan partitions and stale temporary files (H-09: only older than 24 h, dry run first).

An orphan is a Parquet file under ``canonical/<dataset>/`` referenced by no commit marker, no run marker and no
crashed-run journal directory — typically the partitions of a source whose commit marker was never written. On a
CANONICAL root files referenced by any kept snapshot manifest are never orphans. Blobs under ``artifacts/`` are never
touched here (KEEP_RAW is never deleted automatically; unreferenced KEEP_REFERENCED blobs are a separate, manual
decision).
"""
from __future__ import annotations

import json
import time
from typing import Any

from vkm_corpus.contracts.datasets import STORED_DATASETS
from vkm_corpus.parquet.commits import list_markers
from vkm_corpus.parquet.layout import CanonLayout
from vkm_corpus.parquet.runs import list_runs, run_files

MIN_AGE_HOURS = 24.0


def referenced_paths(layout: CanonLayout) -> set[str]:
    refs: set[str] = set()
    for m in list_markers(layout):
        refs |= {e["path"] for e in m.get("datasets", {}).values()}
        refs |= {e["path"] for e in m.get("artifact_index_files", [])}
    for info in list_runs(layout).values():
        refs |= {f.path for f in run_files(layout, info)}
    snaps = layout.canonical / "_snapshots"
    if snaps.is_dir():
        for p in snaps.rglob("snap-*.json"):
            manifest = json.loads(p.read_text(encoding="utf-8"))
            for d in manifest.get("datasets", {}).values():
                refs |= {f["path"] for f in d.get("files", [])}
    return refs


def find_orphans(layout: CanonLayout, min_age_hours: float = MIN_AGE_HOURS) -> dict[str, list[str]]:
    now = time.time()
    refs = referenced_paths(layout)
    orphans, young = [], []
    for name in STORED_DATASETS:
        base = layout.canonical / name
        if not base.is_dir():
            continue
        for p in base.rglob("*.parquet"):
            rel = layout.rel(p)
            if rel in refs:
                continue
            (orphans if now - p.stat().st_mtime >= min_age_hours * 3600 else young).append(rel)
    stale_tmp = [p.name for p in layout.tmp.glob("*.tmp")
                 if now - p.stat().st_mtime >= min_age_hours * 3600] if layout.tmp.is_dir() else []
    return {"orphans": sorted(orphans), "too_young": sorted(young), "stale_tmp": sorted(stale_tmp)}


def collect_garbage(layout: CanonLayout, *, delete: bool = False,
                    min_age_hours: float = MIN_AGE_HOURS) -> dict[str, Any]:
    found = find_orphans(layout, min_age_hours)
    if delete:
        for rel in found["orphans"]:
            layout.path(rel).unlink(missing_ok=True)
        for name in found["stale_tmp"]:
            (layout.tmp / name).unlink(missing_ok=True)
    return {**found, "deleted": delete, "min_age_hours": min_age_hours}
