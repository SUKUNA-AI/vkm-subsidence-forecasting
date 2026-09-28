"""``vkm-corpus search build``: versioned indices from one CANONICAL snapshot, checks, atomic alias swap, rollback.

Build: create ``<prefix>-<type>-m1-<build_id>`` for all five types (refresh off, no replicas) → stream documents with
bulk ``create`` (a duplicate ID fails loudly) while hashing the stream (``doc_stream_sha256``) → refresh, restore the
refresh interval, force-merge to one segment → checks S1 (counts), S3 (sampled documents equal the generated ones),
S4 (analyzer expectations), S5 (optional Russian smoke on the new indices) → mark ``build_status = COMPLETE`` in
``_meta`` → one atomic ``_aliases`` call moves every type alias and the group alias ``<prefix>-objects``. A failed build
never touches the aliases and its indices are deleted (``--keep-failed`` keeps them for diagnosis).

Retention: the aliased build and the previous COMPLETE build are kept (rollback target); older builds are deleted
only with ``--prune`` and only by exact name (the cluster refuses wildcard deletes: ``destructive_requires_name``).
"""
from __future__ import annotations

import hashlib
import heapq
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vkm_corpus.config import Settings
from vkm_corpus.graph.canon import ProjectionInput
from vkm_corpus.graph.common import (LOCKS_DIR, PROJECTOR_VERSION, CheckResult, FileLock, ProjectionError, StreamDigest,
                                     canonical_json, check, failures, load_snapshot, require_canonical_root,
                                     utc_now, verify_snapshot_files, write_receipt)
from vkm_corpus.graph.preflight import require_preflight
from vkm_corpus.search.analysis import ANALYSIS_VERSION, ANALYZER_EXPECTATIONS, analysis_sha256
from vkm_corpus.search.client import connect, server_info
from vkm_corpus.search.documents import doc_line, expected_doc_counts, iter_documents
from vkm_corpus.search.mappings import (GROUP_TYPES, INDEX_TYPES, MAPPING_VERSION, alias_name, body_sha256,
                                        group_alias, index_body, index_name, make_build_id, parse_index_name)
from vkm_corpus.versions import PIPELINE_VERSION

BUILDING, COMPLETE = "BUILDING", "COMPLETE"
_PREFIX_RE = re.compile(r"^[a-z][a-z0-9]{1,30}$")


@dataclass
class BuildOptions:
    snapshot_id: str | None = None
    prefix: str | None = None
    plan_only: bool = False
    keep_failed: bool = False
    prune: bool = False
    keep_builds: int = 2
    smoke: bool = False
    hash_files: bool = True
    chunk_docs: int = 1000
    chunk_bytes: int = 10 * 1024 * 1024
    sample: int = 200
    command: str = "search build"


def check_prefix(prefix: str) -> str:
    prefix = prefix.lower()
    if not _PREFIX_RE.match(prefix):
        raise ProjectionError("E_BAD_PREFIX", f"index prefix {prefix!r} must match {_PREFIX_RE.pattern}",
                              stage="plan")
    return prefix


# ---------------------------------------------------------------- aliases and builds
def alias_targets(client: Any, alias: str) -> list[str]:
    from opensearchpy.exceptions import NotFoundError

    try:
        return sorted(client.indices.get_alias(name=alias).keys())
    except NotFoundError:
        return []


def list_builds(client: Any, prefix: str) -> dict[str, list[dict[str, Any]]]:
    """Our indices by type, oldest first: ``{type: [{index, build_id, meta}]}``."""
    mappings = client.indices.get_mapping(index=f"{prefix}-*", params={"allow_no_indices": "true"})
    out: dict[str, list[dict[str, Any]]] = {t: [] for t in INDEX_TYPES}
    for name, body in mappings.items():
        parsed = parse_index_name(prefix, name)
        if parsed is None:
            continue
        meta = (body.get("mappings") or {}).get("_meta") or {}
        out[parsed[0]].append({"index": name, "build_id": parsed[1], "meta": meta})
    for builds in out.values():
        builds.sort(key=lambda b: b["build_id"])
    return out


def swap_aliases(client: Any, prefix: str, new_indices: dict[str, str]) -> list[dict[str, Any]]:
    """One atomic ``_aliases`` request: type aliases and the group alias move to ``new_indices``."""
    actions: list[dict[str, Any]] = []
    for index_type, name in sorted(new_indices.items()):
        alias = alias_name(prefix, index_type)
        actions += [{"remove": {"index": old, "alias": alias}} for old in alias_targets(client, alias) if old != name]
        actions.append({"add": {"index": name, "alias": alias}})
    group = group_alias(prefix)
    moved = {t for t in new_indices if t in GROUP_TYPES}
    for old in alias_targets(client, group):
        parsed = parse_index_name(prefix, old)
        if parsed and parsed[0] in moved and old != new_indices[parsed[0]]:
            actions.append({"remove": {"index": old, "alias": group}})
    actions += [{"add": {"index": new_indices[t], "alias": group}} for t in sorted(moved)]
    client.indices.update_aliases(body={"actions": actions})
    return actions


def rollback(client: Any, prefix: str, types: tuple[str, ...] = INDEX_TYPES) -> dict[str, Any]:
    """Move the aliases of ``types`` to the previous COMPLETE build."""
    prefix = check_prefix(prefix)
    builds = list_builds(client, prefix)
    targets: dict[str, str] = {}
    for index_type in types:
        current = alias_targets(client, alias_name(prefix, index_type))
        current_build = parse_index_name(prefix, current[0])[1] if current else None
        older = [b for b in builds[index_type] if b["meta"].get("build_status") == COMPLETE
                 and (current_build is None or b["build_id"] < current_build)]
        if not older:
            raise ProjectionError("E_NO_PREVIOUS_BUILD", f"{index_type}: no earlier COMPLETE build to roll back to",
                                  stage="rollback")
        targets[index_type] = older[-1]["index"]
    actions = swap_aliases(client, prefix, targets)
    return {"targets": targets, "actions": actions}


def prune(client: Any, prefix: str, keep: int = 2) -> list[str]:
    """Delete (by exact name) builds beyond the aliased one and ``keep - 1`` older COMPLETE builds."""
    prefix = check_prefix(prefix)
    deleted: list[str] = []
    for index_type, builds in list_builds(client, prefix).items():
        current = set(alias_targets(client, alias_name(prefix, index_type)))
        kept = set(current)
        complete_older = [b["index"] for b in builds if b["index"] not in current
                          and b["meta"].get("build_status") == COMPLETE]
        kept |= set(complete_older[-max(keep - 1, 0):]) if keep > 1 else set()
        newest_current = max((parse_index_name(prefix, c)[1] for c in current), default=None)
        for b in builds:
            if b["index"] in kept:
                continue
            if newest_current is not None and b["build_id"] > newest_current:
                continue                                  # a newer build may be in progress: never touch it
            client.indices.delete(index=b["index"])
            deleted.append(b["index"])
    return deleted


def delete_indices(client: Any, names: list[str]) -> list[str]:
    from opensearchpy.exceptions import NotFoundError

    done = []
    for name in names:
        try:
            client.indices.delete(index=name)
            done.append(name)
        except NotFoundError:
            pass
    return done


def status(client: Any, prefix: str) -> dict[str, Any]:
    """Alias → index → ``_meta`` and counts for every type (for ``/status`` of agent G)."""
    prefix = check_prefix(prefix)
    out: dict[str, Any] = {"server": server_info(client), "prefix": prefix, "mapping_version": MAPPING_VERSION,
                           "analysis_version": ANALYSIS_VERSION, "aliases": {}}
    builds = list_builds(client, prefix)
    for index_type in INDEX_TYPES:
        alias = alias_name(prefix, index_type)
        targets = alias_targets(client, alias)
        entry: dict[str, Any] = {"alias": alias, "indices": targets}
        if targets:
            meta = next((b["meta"] for b in builds[index_type] if b["index"] == targets[0]), {})
            entry.update({"build_id": meta.get("build_id"), "built_from_snapshot_id": meta.get("built_from_snapshot_id"),
                          "canonical_manifest_sha256": meta.get("canonical_manifest_sha256"),
                          "doc_stream_sha256": meta.get("doc_stream_sha256"), "build_status": meta.get("build_status"),
                          "count": int(client.count(index=alias)["count"])})
        entry["builds"] = [b["build_id"] for b in builds[index_type]]
        out["aliases"][index_type] = entry
    out["group_alias"] = {"alias": group_alias(prefix), "indices": alias_targets(client, group_alias(prefix))}
    snapshots = {e.get("built_from_snapshot_id") for e in out["aliases"].values() if e.get("indices")}
    out["consistent_snapshot"] = len(snapshots) == 1
    return out


# ---------------------------------------------------------------- build
def _load_index(client: Any, index: str, inp: ProjectionInput, index_type: str, build_id: str,
                options: BuildOptions) -> dict[str, Any]:
    from opensearchpy import helpers

    digest = StreamDigest(index_type)
    heap: list[tuple[int, str, dict[str, Any]]] = []

    def actions():
        for doc in iter_documents(inp, index_type):
            digest.add((doc["id"],), doc_line(doc))
            h = int(hashlib.sha256(f"{build_id}|{doc['id']}".encode()).hexdigest()[:15], 16)
            if len(heap) < options.sample:
                heapq.heappush(heap, (-h, doc["id"], doc))
            elif -heap[0][0] > h:
                heapq.heapreplace(heap, (-h, doc["id"], doc))
            yield {"_op_type": "create", "_index": index, "_id": doc["id"], "_source": doc}

    ok = 0
    errors: list[Any] = []
    for success, item in helpers.streaming_bulk(client, actions(), chunk_size=options.chunk_docs,
                                                max_chunk_bytes=options.chunk_bytes, raise_on_error=False,
                                                raise_on_exception=False, max_retries=5, initial_backoff=2):
        if success:
            ok += 1
        elif len(errors) < 20:
            errors.append(item)
        else:
            errors.append(None)
    if errors or ok != digest.count:
        raise ProjectionError("E_BULK_FAILED", f"{index_type}: {len(errors)} failed bulk item(s), "
                              f"{ok} of {digest.count} indexed", stage="index",
                              details={"errors": [e for e in errors if e is not None][:20]})
    return {"docs": ok, "doc_stream_sha256": digest.hexdigest(), "sample": {d["id"]: d for _, _, d in heap}}


def _finalize(client: Any, index: str) -> None:
    client.indices.refresh(index=index)
    client.indices.put_settings(index=index, body={"index": {"refresh_interval": None}})
    client.indices.forcemerge(index=index, params={"max_num_segments": 1})
    client.indices.refresh(index=index)


def analyzer_checks(client: Any, index: str) -> CheckResult:
    bad = []
    for analyzer, text, expected in ANALYZER_EXPECTATIONS:
        tokens = [t["token"] for t in client.indices.analyze(index=index, body={"analyzer": analyzer,
                                                                                 "text": text})["tokens"]]
        if tokens != expected:
            bad.append(f"{analyzer}: {text!r} → {tokens} (expected {expected})")
    return check("S4", "analyzer expectations (morphology, ё=е, protected terms, LaTeX)", bad,
                 code="E_ANALYZER_MISMATCH")


def verify_build(client: Any, indices: dict[str, str], expected: dict[str, int],
                 loaded: dict[str, dict[str, Any]]) -> list[CheckResult]:
    results: list[CheckResult] = []
    diff = []
    for index_type, name in indices.items():
        got = int(client.count(index=name)["count"])
        if got != expected[index_type] or got != loaded[index_type]["docs"]:
            diff.append(f"{index_type}: index {got}, canon {expected[index_type]}, streamed {loaded[index_type]['docs']}")
    results.append(check("S1", "document count of every index equals the canon", diff, code="E_COUNT_MISMATCH"))
    mismatch = []
    for index_type, name in indices.items():
        sample = loaded[index_type]["sample"]
        if not sample:
            continue
        found = client.mget(index=name, body={"ids": sorted(sample)})["docs"]
        for item in found:
            want = sample.get(item["_id"])
            if not item.get("found") or canonical_json(item.get("_source")) != canonical_json(want):
                mismatch.append(f"{index_type} {item['_id']}")
    results.append(check("S3", "sampled documents in the index equal the generated documents", mismatch,
                         code="E_TRACE_MISMATCH"))
    results.append(analyzer_checks(client, indices["pages"]))
    return results


def build(settings: Settings, options: BuildOptions | None = None, *, client: Any = None,
          inp: ProjectionInput | None = None, data_root: Path | None = None) -> dict[str, Any]:
    """Build all indices and swap the aliases; returns the receipt."""
    options = options or BuildOptions()
    prefix = check_prefix(options.prefix or settings.opensearch_index_prefix)
    own_input = inp is None
    if own_input:
        root = require_canonical_root(settings)
    else:
        if data_root is None:
            raise ValueError("an injected projection input needs an explicit data_root for receipts")
        root = data_root
    t0 = time.monotonic()
    timings: dict[str, float] = {}
    receipt: dict[str, Any] = {"engine": "opensearch", "prefix": prefix, "command": options.command,
                               "mapping_version": MAPPING_VERSION, "analysis_version": ANALYSIS_VERSION,
                               "analysis_sha256": analysis_sha256(), "projector_version": PROJECTOR_VERSION,
                               "pipeline_version": PIPELINE_VERSION, "started_at": utc_now(), "status": "PLANNED",
                               "body_sha256": {t: body_sha256(t) for t in INDEX_TYPES}}
    with FileLock(root / LOCKS_DIR / f"opensearch-build-{prefix}.lock"):
        try:
            started = time.monotonic()
            if own_input:
                snapshot = load_snapshot(root, options.snapshot_id)
                receipt["snapshot_files"] = verify_snapshot_files(snapshot, hash_files=options.hash_files)
                inp = ProjectionInput.from_snapshot(snapshot)
            assert inp is not None
            receipt["input"] = {**inp.info.as_dict(), "derived_sql": inp.derived_sql, "mapping": inp.mapping_report}
            receipt["preflight"] = [r.as_dict() for r in require_preflight(inp)]
            expected = expected_doc_counts(inp)
            receipt["expected_counts"] = expected
            timings["input"] = round(time.monotonic() - started, 3)
            if options.plan_only:
                receipt["status"] = "PLAN_ONLY"
                receipt["timings_s"] = timings
                return receipt
            return _build_indices(settings, options, prefix, inp, root, receipt, expected, timings, t0, client)
        finally:
            if own_input and inp is not None:
                inp.close()


def _build_indices(settings: Settings, options: BuildOptions, prefix: str, inp: ProjectionInput, root: Path,
                   receipt: dict[str, Any], expected: dict[str, int], timings: dict[str, float], t0: float,
                   client: Any) -> dict[str, Any]:
    client = client or connect(settings)
    receipt["server"] = server_info(client)
    health = client.cluster.health()
    receipt["cluster_health"] = health.get("status")
    if health.get("status") not in ("green", "yellow"):
        raise ProjectionError("E_SERVER_UNAVAILABLE", f"cluster health {health.get('status')}", stage="index",
                              retryable=True)
    build_id = make_build_id(inp.info.manifest_sha256)
    receipt["build_id"] = build_id
    indices = {t: index_name(prefix, t, build_id) for t in INDEX_TYPES}
    receipt["indices"] = indices
    created: list[str] = []
    try:
        started = time.monotonic()
        meta = {"build_id": build_id, "built_from_snapshot_id": inp.info.snapshot_id,
                "canonical_manifest_sha256": inp.info.manifest_sha256, "projector_version": PROJECTOR_VERSION,
                "build_status": BUILDING, "prefix": prefix}
        for index_type, name in indices.items():
            client.indices.create(index=name, body=index_body(index_type, meta))
            created.append(name)
        loaded = {t: _load_index(client, indices[t], inp, t, build_id, options) for t in INDEX_TYPES}
        timings["index"] = round(time.monotonic() - started, 3)
        started = time.monotonic()
        for name in indices.values():
            _finalize(client, name)
        timings["finalize"] = round(time.monotonic() - started, 3)
        started = time.monotonic()
        checks = verify_build(client, indices, expected, loaded)
        if options.smoke:
            from vkm_corpus.search.smoke import run_smoke

            checks += run_smoke(client, prefix, indices=indices)
        timings["verify"] = round(time.monotonic() - started, 3)
        receipt["checks"] = [c.as_dict() for c in checks]
        receipt["doc_stream_sha256"] = {t: loaded[t]["doc_stream_sha256"] for t in INDEX_TYPES}
        receipt["counts"] = {t: loaded[t]["docs"] for t in INDEX_TYPES}
        bad = failures(checks)
        if bad:
            raise ProjectionError(bad[0].code or "E_BUILD_CHECK_FAILED",
                                  f"build checks failed: {', '.join(c.check_id for c in bad)}", stage="verify")
        for index_type, name in indices.items():
            client.indices.put_mapping(index=name, body={"_meta": {
                **meta, "vkm_mapping_version": MAPPING_VERSION, "analysis_version": ANALYSIS_VERSION,
                "object_kind": index_body(index_type, {})["mappings"]["_meta"]["object_kind"],
                "build_status": COMPLETE, "doc_stream_sha256": loaded[index_type]["doc_stream_sha256"],
                "doc_count": loaded[index_type]["docs"], "completed_at": utc_now().isoformat()}})
        receipt["alias_actions"] = swap_aliases(client, prefix, indices)
        if options.prune:
            receipt["pruned"] = prune(client, prefix, keep=options.keep_builds)
        receipt["status"] = COMPLETE
    except ProjectionError as exc:
        receipt["status"] = "FAILED"
        receipt["error"] = exc.as_dict()
        if created and not options.keep_failed:
            receipt["deleted_failed_indices"] = delete_indices(client, created)
        raise
    except Exception as exc:
        receipt["status"] = "FAILED"
        receipt["error"] = {"code": "E_INTERNAL", "stage": "index", "message": f"{type(exc).__name__}: {exc}"}
        if created and not options.keep_failed:
            receipt["deleted_failed_indices"] = delete_indices(client, created)
        raise
    finally:
        receipt["finished_at"] = utc_now()
        timings["total"] = round(time.monotonic() - t0, 3)
        receipt["timings_s"] = timings
        run_id = receipt.get("build_id") or f"plan-{int(time.time())}"
        receipt["receipt_ref"] = write_receipt(root, "opensearch", f"{prefix}-{run_id}", receipt)
    return receipt
