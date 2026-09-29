"""``vkm-corpus graph rebuild --mode wipe``: the DOCUMENT graph from one CANONICAL snapshot.

Only ``wipe`` exists in v0 (H-43): the layer is deleted and loaded again while the latest ``ProjectionRun`` is in a
building state, so readers answer 503 during the build (``vkm_corpus.graph.runs.graph_state``).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, TypeVar

from vkm_corpus.config import Settings
from vkm_corpus.graph import client
from vkm_corpus.graph import cypher as C
from vkm_corpus.graph import runs
from vkm_corpus.graph import schema as S
from vkm_corpus.graph.canon import ProjectionInput
from vkm_corpus.graph.common import (LOCKS_DIR, PROJECTOR_VERSION, CheckResult, FileLock, ProjectionError, failures,
                                     load_snapshot, require_canonical_root, sha256_bytes, utc_now,
                                     verify_snapshot_files, write_receipt)
from vkm_corpus.graph.preflight import require_preflight
from vkm_corpus.graph.rows import expected_counts, iter_nodes, iter_rels, not_projected_report
from vkm_corpus.graph.verify import expected_digest, run_checks, summarize
from vkm_corpus.logs import bind, get_logger
from vkm_corpus.versions import PIPELINE_VERSION

T = TypeVar("T")
HEARTBEAT_SECONDS = 30.0


@dataclass
class RebuildOptions:
    mode: str = "wipe"
    snapshot_id: str | None = None
    batch_size: int = 5000
    wipe_batch: int = 2_000
    plan_only: bool = False
    hash_files: bool = True
    sample_per_label: int = 200
    namespace: S.Namespace = field(default_factory=S.Namespace)
    database: str | None = None
    command: str = "graph rebuild"
    cascade: bool = False            # R6: drop the dependent NAV layer first (reload it with `nav graph-load`)


def _batches(items: Iterable[T], size: int) -> Iterator[list[T]]:
    batch: list[T] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def apply_ddl(driver: Any, database: str, ns: S.Namespace) -> dict[str, Any]:
    """Idempotent DDL, then confirm every expected constraint/index exists."""
    items = S.ddl_items(ns)
    for item in items:
        client.write(driver, database, item.statement, timeout=None)
    names = {r["name"] for r in client.read(driver, database, "SHOW CONSTRAINTS YIELD name RETURN name")}
    names |= {r["name"] for r in client.read(driver, database, "SHOW INDEXES YIELD name RETURN name")}
    missing = [i.name for i in items if i.name not in names]
    if missing:
        raise ProjectionError("E_DDL", f"DDL items missing after apply: {missing}", stage="ddl")
    return {"items": len(items), "ddl_sha256": sha256_bytes(S.ddl_script(ns).encode("utf-8"))}


def drop_namespace_ddl(driver: Any, database: str, ns: S.Namespace) -> None:
    """Drop a *test* namespace's constraints and indexes (never allowed for production)."""
    if not ns.is_test:
        raise ProjectionError("E_REFUSED", "refusing to drop production DDL", stage="cleanup")
    for item in reversed(S.ddl_items(ns)):
        kind = "CONSTRAINT" if item.kind == "CONSTRAINT" else "INDEX"
        client.write(driver, database, f"DROP {kind} {item.name} IF EXISTS", timeout=None)


def purge_test_namespace(driver: Any, database: str, ns: S.Namespace) -> dict[str, Any]:
    """Remove everything of a *test* namespace: layer nodes, its ProjectionRun nodes and its DDL."""
    if not ns.is_test:
        raise ProjectionError("E_REFUSED", "refusing to purge the production namespace", stage="cleanup")
    counters = client.run_autocommit(driver, database, C.wipe_layer(ns, 10_000))
    client.write(driver, database, f"MATCH (r:{S.q(ns.run_label)}) DETACH DELETE r")
    drop_namespace_ddl(driver, database, ns)
    left = client.read(driver, database, "MATCH (n) WHERE any(l IN labels(n) WHERE l STARTS WITH $p) "
                                         "RETURN count(n) AS n", p=ns.prefix)[0]["n"]
    return {**counters, "left": int(left)}


def wipe(driver: Any, database: str, ns: S.Namespace, batch: int) -> dict[str, Any]:
    """Delete the layer in separate bounded write transactions (LIMIT loop). One auto-commit
    ``CALL {…} IN TRANSACTIONS`` over the full-corpus graph exceeded the 1 GiB transaction memory pool
    (MemoryPoolOutOfMemoryError on 663 k nodes); a loop of small transactions keeps memory bounded."""
    batch = max(100, min(int(batch), 100_000))
    counters = {"nodes_deleted": 0, "relationships_deleted": 0, "nodes_created": 0, "transactions": 0}
    query = (f"MATCH (n:{S.q(ns.layer_label)}) WITH n LIMIT {batch} "
             f"OPTIONAL MATCH (n)-[r]-() WITH n, count(r) AS rels DETACH DELETE n RETURN count(n) AS n, sum(rels) AS r")
    while True:
        row = client.write(driver, database, query, timeout=600.0)[0]
        counters["transactions"] += 1
        counters["nodes_deleted"] += int(row["n"] or 0)
        counters["relationships_deleted"] += int(row["r"] or 0)
        if not row["n"]:
            break
    left = int(client.read(driver, database, C.count_layer(ns))[0]["n"])
    if left:
        raise ProjectionError("E_WIPE_INCOMPLETE", f"{left} layer node(s) left after the wipe", stage="wipe")
    return counters


def cross_layer_guard(driver: Any, database: str, ns: S.Namespace) -> None:
    rows = client.read(driver, database, C.cross_layer_edges(ns))
    if rows:
        raise ProjectionError("E_CROSS_LAYER_LOSS", "edges from other layers touch DOCUMENT nodes; a wipe would lose "
                              "them: rebuild with --cascade (drops the derived NAV layer) or run `nav graph-drop --yes` "
                              "first, then reload NAV with `nav graph-load`", stage="wipe", details={"edges": rows[:20]})


def _load_nodes(driver: Any, database: str, ns: S.Namespace, inp: ProjectionInput, run_id: str, batch_size: int,
                beat: Callable[[], None]) -> dict[str, int]:
    written: dict[str, int] = {}
    for node in S.NODE_TYPES:
        query = C.node_merge(ns, node)
        total = 0
        for batch in _batches(iter_nodes(inp, node, run_id), batch_size):
            rows = [{"id": r.id, "props": r.props} for r in batch]
            n = int(client.write(driver, database, query, rows=rows)[0]["written"])
            if n != len(rows):
                raise ProjectionError("E_LOAD_MISMATCH", f"{node.label}: wrote {n} of {len(rows)} nodes", stage="load")
            total += n
            beat()
        written[node.label] = total
    return written


def _load_rels(driver: Any, database: str, ns: S.Namespace, inp: ProjectionInput, run_id: str, batch_size: int,
               beat: Callable[[], None]) -> dict[str, int]:
    written: dict[str, int] = {}
    for rel in S.REL_TYPES:
        query = C.rel_merge(ns, rel)
        total = 0
        for batch in _batches(iter_rels(inp, rel, run_id), batch_size):
            rows = [{"from_id": r.from_id, "to_id": r.to_id, "props": r.props} for r in batch]
            n = int(client.write(driver, database, query, rows=rows)[0]["written"])
            if n != len(rows):
                missing = client.read(driver, database, C.rel_missing_endpoints(ns, rel), rows=rows)
                raise ProjectionError("E_DANGLING_REFERENCE", f"{rel.type}: {len(rows) - n} row(s) without an "
                                      "endpoint node", stage="load", details={"missing": missing})
            total += n
            beat()
        written[rel.type] = total
    return written


def _previous_digest(driver: Any, database: str, ns: S.Namespace, manifest_sha256: str) -> str | None:
    rows = client.read(driver, database,
                       f"MATCH (r:{S.q(ns.run_label)} {{layer: $layer, status: 'COMPLETE', "
                       "canonical_manifest_sha256: $sha, graph_schema_version: $gsv}) "
                       "RETURN r.content_digest AS d ORDER BY r.finished_at DESC LIMIT 1",
                       layer=S.LAYER, sha=manifest_sha256, gsv=S.GRAPH_SCHEMA_VERSION)
    return rows[0]["d"] if rows else None


@dataclass
class _Run:
    """State of one rebuild: receipt, timings and the ProjectionRun identity."""

    settings: Settings
    options: RebuildOptions
    root: Path
    receipt: dict[str, Any]
    timings: dict[str, float] = field(default_factory=dict)
    t0: float = field(default_factory=time.monotonic)
    run_id: str | None = None
    run_started: bool = False

    @property
    def ns(self) -> S.Namespace:
        return self.options.namespace

    @property
    def database(self) -> str:
        return self.options.database or self.settings.neo4j_database

    def lap(self, stage: str, started: float) -> None:
        self.timings[stage] = round(time.monotonic() - started, 3)

    def write(self) -> str:
        self.receipt["finished_at"] = utc_now()
        self.timings["total"] = round(time.monotonic() - self.t0, 3)
        self.receipt["timings_s"] = self.timings
        ref = write_receipt(self.root, "neo4j", self.run_id or f"plan-{int(time.time())}", self.receipt)
        self.receipt["receipt_ref"] = ref
        return ref


def rebuild(settings: Settings, options: RebuildOptions | None = None, *, driver: Any = None,
            inp: ProjectionInput | None = None, data_root: Path | None = None) -> dict[str, Any]:
    """Rebuild the layer; returns the receipt. ``inp``/``data_root`` let tests inject a projection input."""
    options = options or RebuildOptions()
    if options.mode != "wipe":
        raise ProjectionError("E_MODE_UNSUPPORTED", "v0 supports only --mode wipe (H-43)", stage="plan")
    own_input = inp is None
    if own_input:
        root = require_canonical_root(settings)
    elif data_root is None:
        raise ValueError("an injected projection input needs an explicit data_root for receipts")
    else:
        root = data_root
    ns = options.namespace
    run = _Run(settings, options, root, receipt={
        "engine": "neo4j", "layer": S.LAYER, "mode": options.mode, "command": options.command,
        "graph_schema_version": S.GRAPH_SCHEMA_VERSION, "projector_version": PROJECTOR_VERSION,
        "pipeline_version": PIPELINE_VERSION, "namespace": ns.prefix or None, "started_at": utc_now(),
        "status": "PLANNED"})
    suffix = f"-{ns.prefix.lower()}" if ns.is_test else ""
    with FileLock(root / LOCKS_DIR / f"neo4j-document-projection{suffix}.lock"):
        try:
            if own_input:
                started = time.monotonic()
                snapshot = load_snapshot(root, options.snapshot_id)
                run.receipt["snapshot_files"] = verify_snapshot_files(snapshot, hash_files=options.hash_files)
                inp = ProjectionInput.from_snapshot(snapshot)
                run.lap("snapshot", started)
            assert inp is not None
            return _execute(run, inp, driver)
        finally:
            if own_input and inp is not None:
                inp.close()


def _execute(run: _Run, inp: ProjectionInput, driver: Any) -> dict[str, Any]:
    receipt, ns, database, options = run.receipt, run.ns, run.database, run.options
    receipt["input"] = {**inp.info.as_dict(), "derived_sql": inp.derived_sql, "rule_versions": inp.rule_versions,
                        "mapping": inp.mapping_report}
    started = time.monotonic()
    preflight = require_preflight(inp)
    expected = expected_counts(inp)
    receipt["preflight"] = [r.as_dict() for r in preflight]
    receipt["expected_counts"] = expected
    receipt["not_projected"] = not_projected_report(inp)
    run.lap("preflight", started)
    if options.plan_only:
        receipt["status"] = "PLAN_ONLY"
        run.timings["total"] = round(time.monotonic() - run.t0, 3)
        receipt["timings_s"] = run.timings
        return receipt

    own_driver = driver is None
    if own_driver:
        driver = client.connect(run.settings)
    run.run_id = runs.new_run_id()
    receipt["run_id"] = run.run_id
    rlog = bind(get_logger("graph"), run_id=run.run_id, stage="rebuild")
    try:
        started = time.monotonic()
        receipt["server"] = client.server_info(driver, database)
        receipt["ddl"] = apply_ddl(driver, database, ns)
        stale, fresh = runs.stale_building_runs(driver, database, ns)
        if fresh:
            raise ProjectionError("E_PROJECTION_BUSY", f"another build is in progress: {fresh}", stage="lock",
                                  retryable=True)
        for stale_id in stale:
            runs.set_status(driver, database, ns, stale_id, runs.FAILED, error_code="E_STALE_RUN")
        receipt["stale_runs_failed"] = stale
        if options.cascade:                       # R6 --cascade: the derived NAV layer depends on DOCUMENT
            from vkm_corpus.graph.nav import drop_layer

            receipt["cascade"] = {"NAVIGATION": drop_layer(driver, database, ns)}
        cross_layer_guard(driver, database, ns)
        previous = _previous_digest(driver, database, ns, inp.info.manifest_sha256)
        run.lap("server", started)

        runs.start_run(driver, database, ns, run.run_id, {
            "mode": options.mode, "built_from_snapshot_id": inp.info.snapshot_id,
            "canonical_manifest_sha256": inp.info.manifest_sha256,
            "canonical_schema_versions": list(inp.info.schema_versions),
            "rule_versions": sorted(f"{k}={v}" for k, v in inp.rule_versions.items()),
            "derived_sql_sha256": sorted(f"{k}={v}" for k, v in inp.derived_sql.items()),
            "projector_version": PROJECTOR_VERSION, "pipeline_version": PIPELINE_VERSION,
            "neo4j_version": receipt["server"].get("version"), "neo4j_edition": receipt["server"].get("edition"),
        })
        run.run_started = True
        rlog.info("projection run started", extra={"vkm": {"status": runs.WIPING}})

        started = time.monotonic()
        receipt["wipe"] = wipe(driver, database, ns, options.wipe_batch)
        run.lap("wipe", started)

        last_beat = [time.monotonic()]

        def beat() -> None:
            if time.monotonic() - last_beat[0] >= HEARTBEAT_SECONDS:
                runs.heartbeat(driver, database, ns, run.run_id)
                last_beat[0] = time.monotonic()

        runs.set_status(driver, database, ns, run.run_id, runs.LOADING)
        started = time.monotonic()
        receipt["written"] = {"nodes": _load_nodes(driver, database, ns, inp, run.run_id, options.batch_size, beat),
                              "rels": _load_rels(driver, database, ns, inp, run.run_id, options.batch_size, beat)}
        run.lap("load", started)

        runs.set_status(driver, database, ns, run.run_id, runs.VERIFYING)
        started = time.monotonic()
        checks, exp_digest, content = run_checks(
            driver, database, ns, inp, expected_counts=expected, expected=expected_digest(inp),
            sample_per_label=options.sample_per_label, sample_seed=run.run_id, previous_digest=previous,
            progress=lambda m: rlog.info(m, extra={"vkm": {"stage": "verify"}}))
        run.lap("verify", started)
        receipt["checks"] = [c.as_dict() for c in checks]
        receipt["checks_summary"] = summarize(checks)
        receipt["expected_digest"] = exp_digest.as_dict() if exp_digest else None
        receipt["content_digest"] = content.as_dict() if content else None
        bad = failures(checks)
        status = runs.FAILED if bad else runs.COMPLETE
        receipt["status"] = status
        if bad:
            receipt["error"] = {"code": bad[0].code, "stage": "verify",
                                "message": f"checks failed: {', '.join(c.check_id for c in bad)}"}
        ref = run.write()
        runs.set_status(driver, database, ns, run.run_id, status,
                        expected_digest=exp_digest.digest if exp_digest else None,
                        content_digest=content.digest if content else None,
                        counts_json=expected, checks_json={c.check_id: c.status for c in checks},
                        error_code=bad[0].code if bad else None, receipt_ref=ref)
        rlog.info("projection run finished", extra={"vkm": {"status": status}})
        if bad:
            raise ProjectionError(bad[0].code or "E_CHECK_FAILED", receipt["error"]["message"], stage="verify",
                                  details={"receipt_ref": ref})
        return receipt
    except ProjectionError as exc:
        _fail(driver, run, exc.as_dict())
        raise
    except Exception as exc:
        _fail(driver, run, {"code": "E_INTERNAL", "stage": "rebuild", "message": f"{type(exc).__name__}: {exc}",
                            "retryable": False})
        raise
    finally:
        if own_driver and driver is not None:
            driver.close()


def _fail(driver: Any, run: _Run, error: dict[str, Any]) -> None:
    if run.receipt.get("status") == runs.FAILED and run.receipt.get("receipt_ref"):
        return                                    # checks failed: receipt and ProjectionRun are already written
    run.receipt["status"] = runs.FAILED
    run.receipt["error"] = error
    ref = run.write()
    if run.run_started:
        try:
            runs.set_status(driver, run.database, run.ns, run.run_id, runs.FAILED, error_code=error.get("code"),
                            receipt_ref=ref)
        except Exception:  # the server may be the reason of the failure
            pass


def verify_current(settings: Settings, *, driver: Any = None, inp: ProjectionInput | None = None,
                   namespace: S.Namespace | None = None, database: str | None = None,
                   sample_per_label: int = 200) -> list[CheckResult]:
    """``graph verify``: checks of the current graph against the current snapshot, without writing."""
    ns = namespace or S.Namespace()
    database = database or settings.neo4j_database
    own_input = inp is None
    if own_input:
        root = require_canonical_root(settings)
        inp = ProjectionInput.from_snapshot(load_snapshot(root))
    own_driver = driver is None
    if own_driver:
        driver = client.connect(settings)
    try:
        expected = expected_counts(inp)
        results, _, _ = run_checks(driver, database, ns, inp, expected_counts=expected,
                                   sample_per_label=sample_per_label)
        return results
    finally:
        if own_driver:
            driver.close()
        if own_input:
            inp.close()
