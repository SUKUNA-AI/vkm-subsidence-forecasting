"""Snapshots: manifest of the admitted heads + all run journals, validation, atomic ``CURRENT`` (CANONICAL only).

A snapshot lists, per dataset, exactly the files it consists of (never a glob at read time):

* registry datasets — files of the admitted REGISTRY head commit;
* document datasets — files of the admitted head commit of every source;
* ``artifacts`` — artifact index files of those head commits plus the run-level index files;
* ``processing_runs``, ``processing_steps``, ``errors`` — files of every run (END list, or START + discovered parts
  of a crashed run: a crash stays visible, H-11).

The manifest carries per-dataset fingerprints (combined row digests, the same algorithm the DuckDB build verifies,
H-48). ``CURRENT`` moves atomically and only if the validator reports no blocking failure; otherwise the manifest is
kept under ``_snapshots/candidates/`` for inspection.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from vkm_corpus import ids
from vkm_corpus.contracts import arrow as ca
from vkm_corpus.contracts.datasets import DATASETS, STORED_DATASETS
from vkm_corpus.contracts.vocab import ArtifactKind, RootKind
from vkm_corpus.parquet.admit import admitted_heads, load_admissions, root_lock
from vkm_corpus.parquet.atomic import dump_json, write_bytes, write_json
from vkm_corpus.parquet.blobs import put_blob
from vkm_corpus.parquet.commits import REGISTRY_KEY
from vkm_corpus.parquet.layout import CanonLayout
from vkm_corpus.parquet.reader import current_snapshot_id
from vkm_corpus.parquet.runs import list_runs, run_files
from vkm_corpus.parquet.validator import ValidationOptions, validate
from vkm_corpus.parquet.writer import FileEntry
from vkm_corpus.versions import PIPELINE_VERSION

MANIFEST_VERSION = "1"


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def collect_files(layout: CanonLayout) -> tuple[dict[str, list[FileEntry]], dict[str, Any], dict[str, Any]]:
    heads = admitted_heads(layout)
    files: dict[str, list[FileEntry]] = {n: [] for n in STORED_DATASETS}
    for key, marker in heads.items():
        for name, e in marker["datasets"].items():
            files[name].append(FileEntry.from_json(e))
        for e in marker.get("artifact_index_files", []):
            files["artifacts"].append(FileEntry.from_json(e))
    runs = {}
    for rid, info in list_runs(layout).items():
        for f in run_files(layout, info):
            files[f.dataset].append(f)
        runs[rid] = {"has_end": info.end is not None,
                     "status": (info.end or {}).get("status", "STARTED"),
                     "run_kind": (info.start or {}).get("run_kind")}
    for name in files:
        seen, uniq = set(), []
        for f in sorted(files[name], key=lambda x: x.path):
            if f.path not in seen:
                seen.add(f.path)
                uniq.append(f)
        files[name] = uniq
    return files, heads, runs


def build_manifest(layout: CanonLayout, *, created_at: datetime, created_by_run_id: str | None = None,
                   inputs: dict[str, Any] | None = None) -> dict[str, Any]:
    files, heads, runs = collect_files(layout)
    datasets = {}
    for name in STORED_DATASETS:
        fl = files[name]
        dig = ca.EMPTY_DIGEST
        cdig = ca.EMPTY_DIGEST
        for f in fl:
            dig = dig + f.row_digest
            cdig = cdig + f.content_row_digest
        spec = DATASETS[name]
        datasets[name] = {
            "dataset_class": spec.dataset_class.value, "schema_version": spec.version,
            "schema_fingerprint": ca.schema_fingerprint(name), "files": [f.to_json() for f in fl],
            "rows": dig.rows, "table_fingerprint": ca.fingerprint_of(name, dig),
            "content_fingerprint": ca.fingerprint_of(name, cdig, content=True),
        }
    rejected = sorted(c for c, r in load_admissions(layout).items() if r["status"] == "REJECTED")
    head_commits = {k: {"commit_id": m["commit_id"], "source_sha256": m.get("source_sha256"),
                        "processing_run_id": m["processing_run_id"], "marker_path": m["_path"],
                        "datasets": {n: {"path": e["path"]} for n, e in m["datasets"].items()}}
                    for k, m in sorted(heads.items())}
    body = {
        "manifest_version": MANIFEST_VERSION,
        "parent_snapshot_id": current_snapshot_id(layout),
        "created_by_run_id": created_by_run_id,
        "code": {"pipeline_version": PIPELINE_VERSION,
                 "schema_versions": {n: DATASETS[n].version for n in STORED_DATASETS}},
        "inputs": dict(inputs or {}),
        "registry_head": heads.get(REGISTRY_KEY, {}).get("commit_id"),
        "source_heads": {k: m["commit_id"] for k, m in sorted(heads.items()) if k != REGISTRY_KEY},
        "head_commits": head_commits,
        "rejected_commits": rejected,
        "runs": dict(sorted(runs.items())),
        "datasets": datasets,
    }
    body["snapshot_id"] = ids.snapshot_id(created_at, body)
    body["created_at"] = _iso(created_at)
    return body


def build_snapshot(layout: CanonLayout, *, options: ValidationOptions | None = None, now: datetime | None = None,
                   created_by_run_id: str | None = None, inputs: dict[str, Any] | None = None,
                   publication_approval=None, policy_path=None) -> dict[str, Any]:
    """Build, validate and (if PASS) publish a snapshot; ``CURRENT`` moves only on PASS."""
    layout.require(RootKind.CANONICAL)
    created = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    opts = options or ValidationOptions()
    publication_approval = publication_approval or opts.publication_approval
    policy_path = policy_path if policy_path is not None else opts.policy_path
    with root_lock(layout):
        if publication_approval is not None:
            from vkm_corpus.coverage.publication import descriptor_path
            inputs = {**(inputs or {}), "accounting_publication": {
                "path": descriptor_path(publication_approval.descriptor_sha256),
                "sha256": publication_approval.descriptor_sha256}}
        manifest = build_manifest(layout, created_at=created, created_by_run_id=created_by_run_id, inputs=inputs)
        opts.publication_approval, opts.policy_path = publication_approval, policy_path
        if opts.parent_manifest is None and manifest["parent_snapshot_id"]:
            from vkm_corpus.parquet.reader import load_manifest

            opts.parent_manifest = load_manifest(layout, manifest["parent_snapshot_id"])
        report = validate(layout, manifest, opts)
        if publication_approval is not None:
            from vkm_corpus.coverage.publication import authorize
            authorize(policy_path, publication_approval.policy_sha256,
                      publication_approval.source_ids, publication_approval.context)
        if publication_approval is not None and report["status"] == "PASS" and current_snapshot_id(layout):
            from vkm_corpus.parquet.reader import load_manifest
            current = load_manifest(layout)
            if (current.get("inputs", {}).get("accounting_publication") == manifest["inputs"]["accounting_publication"]
                    and current["source_heads"] == manifest["source_heads"]
                    and current["registry_head"] == manifest["registry_head"]):
                from vkm_corpus.coverage.publication import PublicationBlocked
                # Checking a newly assembled candidate cannot certify the old
                # CURRENT manifest returned after lost ACK. Match every data
                # map and attribution input before reusing those exact bytes.
                stable = ("manifest_version", "code", "inputs", "source_heads", "registry_head",
                          "head_commits", "runs", "datasets", "rejected_commits")
                if (any(current.get(key) != manifest.get(key) for key in stable)
                        or current.get("counts") != report.get("counts", {})
                        or current.get("accounting") != report.get("accounting")
                        or current.get("validation", {}).get("status") != "PASS"):
                    raise PublicationBlocked("PUBLICATION_RETRY_MANIFEST_CHANGED")
                # Lost receiving ACK: reverify immutable closure and retain the
                # already published snapshot instead of manufacturing a new ID.
                rel = layout.snapshot_manifest(current["snapshot_id"])
                return {"snapshot_id": current["snapshot_id"], "status": "PASS", "current_moved": False,
                        "noop": True, "manifest_path": rel,
                        "manifest_sha256": hashlib.sha256(layout.path(rel).read_bytes()).hexdigest(), "report": report}
        report_bytes = dump_json(report).encode("utf-8")
        rep = put_blob(layout, ArtifactKind.VALIDATION_REPORT, report_bytes, "application/json",
                       run_id=created_by_run_id or "RUN-19700101T000000Z-00000000", created_at=created)
        manifest["validation"] = {"status": report["status"], "blocking_failures": report["blocking_failures"],
                                  "warnings": report["warnings"], "report_artifact_id": rep["artifact_id"],
                                  "report_relpath": rep["storage_relpath"]}
        manifest["counts"] = report.get("counts", {})
        manifest["accounting"] = report.get("accounting", {"status": "NOT_AVAILABLE"})
        passed = report["status"] == "PASS"
        rel = layout.snapshot_manifest(manifest["snapshot_id"], candidate=not passed)
        write_json(layout.tmp, layout.path(rel), manifest)
        if passed:
            write_bytes(layout.tmp, layout.canonical / layout.CURRENT, (manifest["snapshot_id"] + "\n").encode(),
                        overwrite=True)
        msha = hashlib.sha256(layout.path(rel).read_bytes()).hexdigest()
    return {"snapshot_id": manifest["snapshot_id"], "status": report["status"], "current_moved": passed,
            "manifest_path": rel, "manifest_sha256": msha, "report": report}
