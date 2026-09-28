"""CORE: one entry point from published commits to fresh projections (H-09).

    admit → snapshot + validator (CURRENT moves only on PASS) → DuckDB → Neo4j (wipe) → OpenSearch

A rejected commit does not block the others (admission of D). If the snapshot does not pass, the projections are not
touched and keep serving the previous ``CURRENT``. Every step lands in one receipt under ``$VKM_DATA_ROOT/receipts/``.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

from vkm_corpus.config import Settings


def _step(receipt: dict[str, Any], name: str, fn, *args, **kwargs) -> Any:
    t0 = time.monotonic()
    entry: dict[str, Any] = {"step": name}
    try:
        result = fn(*args, **kwargs)
        entry.update(status="OK", seconds=round(time.monotonic() - t0, 2))
        return result
    except Exception as exc:  # noqa: BLE001 — recorded, re-raised
        entry.update(status="FAILED", seconds=round(time.monotonic() - t0, 2), error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        receipt["steps"].append(entry)


def _summary(obj: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    if isinstance(obj, dict):
        return {k: obj[k] for k in keys if k in obj}
    return {}


def reconcile(settings: Settings, *, run_id: str, graph: bool = True, search: bool = True,
              smoke: bool = True) -> dict[str, Any]:
    from vkm_corpus.contracts.vocab import RootKind
    from vkm_corpus.duckdb.build import build_duckdb
    from vkm_corpus.parquet.admit import admit
    from vkm_corpus.parquet.layout import open_root
    from vkm_corpus.parquet.snapshot import build_snapshot

    layout = open_root(settings.require_data_root(), RootKind.CANONICAL)
    receipt: dict[str, Any] = {"kind": "RECONCILE", "run_id": run_id,
                               "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "steps": []}
    try:
        adm = _step(receipt, "admit", admit, layout)
        receipt["admission"] = {k: len(v) for k, v in adm.items()}
        receipt["rejected"] = adm.get("rejected", [])
        snap = _step(receipt, "snapshot", build_snapshot, layout, created_by_run_id=run_id)
        report = snap.get("report") or {}
        receipt["snapshot"] = {"snapshot_id": snap.get("snapshot_id"), "status": snap.get("status"),
                               "current_moved": snap.get("current_moved"), "manifest_sha256": snap.get("manifest_sha256"),
                               "blocking_failures": report.get("blocking_failures"), "counts": report.get("counts", {})}
        if snap.get("status") != "PASS" or not snap.get("current_moved"):
            receipt["result"] = "SNAPSHOT_NOT_PASSED_PROJECTIONS_UNCHANGED"
            return receipt
        sid = snap["snapshot_id"]
        duck = _step(receipt, "duckdb", build_duckdb, layout, sid)
        receipt["duckdb"] = _summary(duck, ("snapshot_id", "tables", "status", "path"))
        if graph:
            from vkm_corpus.graph.loader import RebuildOptions, rebuild

            g = _step(receipt, "graph", rebuild, settings, RebuildOptions(mode="wipe", snapshot_id=sid))
            receipt["graph"] = _summary(g, ("status", "counts", "content_digest", "expected_digest", "checks"))
        if search:
            from vkm_corpus.search.indexer import BuildOptions, build

            s = _step(receipt, "search", build, settings, BuildOptions(snapshot_id=sid, smoke=smoke, prune=True))
            receipt["search"] = _summary(s, ("status", "indices", "counts", "smoke", "doc_stream_sha256"))
        receipt["result"] = "RECONCILED"
        return receipt
    finally:
        receipt["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        out = layout.root / "receipts" / "reconcile"
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{run_id}.json").write_text(json.dumps(receipt, indent=2, ensure_ascii=False, default=str) + "\n",
                                            encoding="utf-8", newline="\n")
