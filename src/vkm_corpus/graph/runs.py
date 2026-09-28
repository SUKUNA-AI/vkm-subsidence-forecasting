"""``ProjectionRun`` nodes: the build history of the DOCUMENT layer and the gate for readers (H-43).

A run is created before the wipe and moves ``WIPING → LOADING → VERIFYING → COMPLETE | FAILED``; ``heartbeat_at`` is
refreshed while it works. ``ProjectionRun`` carries no layer label, so the wipe never deletes the history.

Readers (VKM API/MCP, agent G) call ``graph_state``: only when the latest run of the layer is ``COMPLETE`` may the graph
be served; while a build is in progress the answer is 503 ``GRAPH_REBUILDING``, after a failed build 503
``GRAPH_UNAVAILABLE`` (in v0 the only mode is ``wipe``, so a failed build leaves a partial graph).
"""
from __future__ import annotations

import json
import secrets
from typing import Any

from vkm_corpus.graph import client
from vkm_corpus.graph.common import normalize_value, utc_stamp
from vkm_corpus.graph.schema import GRAPH_SCHEMA_VERSION, LAYER, Namespace, q

WIPING, LOADING, VERIFYING, COMPLETE, FAILED = "WIPING", "LOADING", "VERIFYING", "COMPLETE", "FAILED"
BUILDING_STATES = frozenset({WIPING, LOADING, VERIFYING})
TERMINAL_STATES = frozenset({COMPLETE, FAILED})
STALE_AFTER_SECONDS = 900        # a building run without heartbeat for this long is considered dead


def new_run_id() -> str:
    """``VKM-PRJ-DOC-<UTC>-<8 hex>``: outside the occupied prefixes, usable as a receipt file name."""
    return f"VKM-PRJ-DOC-{utc_stamp()}-{secrets.token_hex(4)}"


def start_run(driver: Any, database: str, ns: Namespace, run_id: str, props: dict[str, Any]) -> None:
    data = {k: normalize_value(v) for k, v in props.items() if v is not None}
    client.write(driver, database,
                 f"CREATE (r:{q(ns.run_label)}) SET r = $props, r.id = $run_id, r.layer = $layer, "
                 f"r.status = $status, r.graph_schema_version = $gsv, r.started_at = datetime(), "
                 f"r.heartbeat_at = datetime()",
                 props=data, run_id=run_id, layer=LAYER, status=WIPING, gsv=GRAPH_SCHEMA_VERSION)


def set_status(driver: Any, database: str, ns: Namespace, run_id: str, status: str, **fields: Any) -> None:
    data = {k: _storable(v) for k, v in fields.items() if v is not None}
    finished = ", r.finished_at = datetime()" if status in TERMINAL_STATES else ""
    client.write(driver, database,
                 f"MATCH (r:{q(ns.run_label)} {{id: $run_id}}) SET r += $fields, r.status = $status, "
                 f"r.heartbeat_at = datetime(){finished}", run_id=run_id, status=status, fields=data)


def heartbeat(driver: Any, database: str, ns: Namespace, run_id: str) -> None:
    client.write(driver, database, f"MATCH (r:{q(ns.run_label)} {{id: $run_id}}) SET r.heartbeat_at = datetime()",
                 run_id=run_id)


def _storable(value: Any) -> Any:
    """Neo4j has no map properties: structures are stored as canonical JSON strings (``*_json``)."""
    if isinstance(value, dict):
        return json.dumps(normalize_value(value), sort_keys=True, ensure_ascii=False)
    return normalize_value(value)


_RUN_FIELDS = ("id", "layer", "status", "mode", "started_at", "finished_at", "heartbeat_at", "graph_schema_version",
               "built_from_snapshot_id", "canonical_manifest_sha256", "expected_digest", "content_digest",
               "neo4j_version", "neo4j_edition", "projector_version", "error_code", "receipt_ref")


def _run_dict(node: Any) -> dict[str, Any]:
    props = dict(node)
    out = {k: normalize_value(props.get(k)) for k in _RUN_FIELDS}
    for key in ("counts_json",):
        if props.get(key):
            out[key.removesuffix("_json")] = json.loads(props[key])
    return out


def runs(driver: Any, database: str, ns: Namespace = Namespace(), limit: int = 20) -> list[dict[str, Any]]:
    rows = client.read(driver, database,
                       f"MATCH (r:{q(ns.run_label)} {{layer: $layer}}) RETURN r ORDER BY r.started_at DESC, r.id DESC "
                       f"LIMIT $limit", layer=LAYER, limit=int(limit))
    return [_run_dict(row["r"]) for row in rows]


def stale_building_runs(driver: Any, database: str, ns: Namespace,
                        stale_after: int = STALE_AFTER_SECONDS) -> tuple[list[str], list[str]]:
    """(stale, fresh) ids of runs that are still in a building state."""
    rows = client.read(driver, database,
                       f"MATCH (r:{q(ns.run_label)} {{layer: $layer}}) WHERE r.status IN $building "
                       f"RETURN r.id AS id, duration.inSeconds(r.heartbeat_at, datetime()).seconds AS age",
                       layer=LAYER, building=sorted(BUILDING_STATES))
    stale = [r["id"] for r in rows if r["age"] is None or r["age"] >= stale_after]
    fresh = [r["id"] for r in rows if r["id"] not in stale]
    return stale, fresh


def graph_state(driver: Any, database: str = "neo4j", ns: Namespace = Namespace()) -> dict[str, Any]:
    """Readiness of the DOCUMENT graph for readers (agent G): ``state``, ``http_status`` and the build identity.

    ``state`` is ``READY`` (latest run COMPLETE), ``REBUILDING`` (a run is building), ``FAILED`` (latest run failed)
    or ``EMPTY`` (never built). Everything but ``READY`` must be served as 503.
    """
    latest = runs(driver, database, ns, limit=1)
    if not latest:
        return {"state": "EMPTY", "http_status": 503, "error": "GRAPH_UNAVAILABLE", "build_id": None}
    run = latest[0]
    if run["status"] == COMPLETE:
        return {"state": "READY", "http_status": 200, "build_id": run["id"],
                "built_from_snapshot_id": run["built_from_snapshot_id"],
                "canonical_manifest_sha256": run["canonical_manifest_sha256"],
                "graph_schema_version": run["graph_schema_version"], "finished_at": run["finished_at"],
                "content_digest": run["content_digest"]}
    if run["status"] in BUILDING_STATES:
        return {"state": "REBUILDING", "http_status": 503, "error": "GRAPH_REBUILDING", "build_id": run["id"],
                "built_from_snapshot_id": run["built_from_snapshot_id"], "status": run["status"]}
    return {"state": "FAILED", "http_status": 503, "error": "GRAPH_UNAVAILABLE", "build_id": run["id"],
            "error_code": run["error_code"]}
