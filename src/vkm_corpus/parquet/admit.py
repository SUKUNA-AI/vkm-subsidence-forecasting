"""Admission of published commits on the CANONICAL root (H-08, H-09).

``admit(layout)`` examines every commit marker without an admission record. A commit is ADMITTED when its format and
id are intact, every partition file exists with its sha256 and size, its schema version/fingerprint is known to this
code, every stored artifact blob it lists exists and hashes to its id, it names the current admitted head of its key
as parent, and (for sources) its source_sha256 equals the admitted register. Otherwise it is REJECTED with a reason
code — rejected commits never block other commits. A commit whose parent has not arrived yet stays pending.
Records ``_admission/<CMT>.json`` are immutable.
"""
from __future__ import annotations

import json
import os
import socket
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from vkm_corpus import ids
from vkm_corpus.contracts import arrow as ca
from vkm_corpus.contracts.datasets import DATASETS, DOCUMENT_DATASETS, REGISTRY_DATASETS
from vkm_corpus.contracts.vocab import AdmissionStatus, ErrorCode, RootKind
from vkm_corpus.parquet.atomic import create_exclusive, dump_json, sha256_of, write_json
from vkm_corpus.parquet.blobs import check_blob
from vkm_corpus.parquet.commits import REGISTRY_KEY, chain_head, list_markers
from vkm_corpus.parquet.layout import CanonLayout, RootError


class LockedError(RuntimeError):
    """Another admission/snapshot writer holds the root lock."""


@contextmanager
def root_lock(layout: CanonLayout) -> Iterator[None]:
    path = layout.canonical / layout.LOCK
    body = {"pid": os.getpid(), "host": socket.gethostname(),
            "since": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if not create_exclusive(path, dump_json(body)):
        raise LockedError("the canonical root is locked by another admit/snapshot (remove the lock explicitly "
                          "with `vkm-corpus canon unlock` if that process is dead)")
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def load_admissions(layout: CanonLayout) -> dict[str, dict[str, Any]]:
    base = layout.canonical / "_admission"
    out = {}
    if base.is_dir():
        for p in sorted(base.glob("CMT-*.json")):
            rec = json.loads(p.read_text(encoding="utf-8"))
            out[rec["commit_id"]] = rec
    return out


def admitted_heads(layout: CanonLayout) -> dict[str, dict[str, Any]]:
    """Admitted head marker of every key (sources and REGISTRY)."""
    markers = {m["commit_id"]: m for m in list_markers(layout)}
    adm = {c for c, r in load_admissions(layout).items() if r["status"] == AdmissionStatus.ADMITTED}
    admitted = [m for c, m in markers.items() if c in adm]
    out = {}
    for key in sorted({m["key"] for m in admitted}):
        head, _ = chain_head(admitted, key)
        if head:
            out[key] = markers[head]
    return out


def _registry_shas(layout: CanonLayout, registry_marker: dict[str, Any] | None) -> dict[str, str] | None:
    if registry_marker is None:
        return None
    import pyarrow.parquet as pq

    entry = registry_marker["datasets"]["sources"]
    t = pq.read_table(layout.path(entry["path"]), columns=["source_id", "source_sha256"])
    return dict(zip(t.column("source_id").to_pylist(), t.column("source_sha256").to_pylist()))


def verify_commit(layout: CanonLayout, marker: dict[str, Any], register: dict[str, str] | None) -> list[list[str]]:
    """Problems of one commit as ``[reason_code, detail]`` (empty = admissible, except the parent check)."""
    problems: list[list[str]] = []
    body = {k: v for k, v in marker.items() if not k.startswith("_")}
    if body.get("commit_format") != "1" or ids.commit_id(body) != body.get("commit_id"):
        return [[ErrorCode.COMMIT_FAILED, "marker format or commit id does not verify"]]
    scope_ds = REGISTRY_DATASETS if body["key"] == REGISTRY_KEY else DOCUMENT_DATASETS
    if set(body.get("datasets", {})) != set(scope_ds):
        problems.append([ErrorCode.COMMIT_FAILED, "marker does not list exactly the datasets of its scope"])
    for name, e in list(body.get("datasets", {}).items()) + [("artifacts", e) for e in
                                                               body.get("artifact_index_files", [])]:
        spec = DATASETS.get(name)
        if spec is None or e.get("schema_version") != spec.version or \
                e.get("schema_fingerprint") != ca.schema_fingerprint(name):
            problems.append([ErrorCode.SCHEMA_UNKNOWN, f"{name}: schema {e.get('schema_version')} unknown to this "
                                                       "code"])
            continue
        p = layout.path(e["path"])
        if not p.is_file():
            problems.append([ErrorCode.COMMIT_FAILED, f"missing partition {e['path']}"])
        elif p.stat().st_size != e["bytes"] or sha256_of(p) != e["sha256"]:
            problems.append([ErrorCode.COMMIT_FAILED, f"sha256/size mismatch {e['path']}"])
    for b in body.get("artifact_blobs", []):
        reason = check_blob(layout, b["storage_relpath"], b["artifact_id"], deep=True)
        if reason:
            code = ErrorCode.ARTIFACT_MISSING if reason == "missing" else ErrorCode.ARTIFACT_HASH_MISMATCH
            problems.append([code, f"{b['artifact_id']}: {reason}"])
    if body["key"] != REGISTRY_KEY and register is not None:
        if body["key"] not in register:
            problems.append([ErrorCode.SOURCE_BINDING_CHANGED, "source is not in the admitted register"])
        elif register[body["key"]] != body.get("source_sha256"):
            problems.append([ErrorCode.SOURCE_BINDING_CHANGED, "source_sha256 differs from the admitted register"])
    return problems


def admit(layout: CanonLayout, *, now: datetime | None = None) -> dict[str, list]:
    """Admit or reject every new commit (registry first); returns ``{admitted, rejected, pending}``."""
    layout.require(RootKind.CANONICAL)
    result: dict[str, list] = {"admitted": [], "rejected": [], "pending": []}
    with root_lock(layout):
        markers = list_markers(layout)
        by_id = {m["commit_id"]: m for m in markers}
        records = load_admissions(layout)
        admitted = {c for c, r in records.items() if r["status"] == AdmissionStatus.ADMITTED}
        rejected = {c for c, r in records.items() if r["status"] == AdmissionStatus.REJECTED}
        todo = [m for m in markers if m["commit_id"] not in records]
        ts = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        def record(m: dict[str, Any], status: str, problems: list[list[str]]) -> None:
            rec = {"commit_id": m["commit_id"], "key": m["key"], "status": status,
                   "parent_commit_id": m.get("parent_commit_id"), "marker_path": m["_path"], "decided_at": ts,
                   "problems": [[str(c), d] for c, d in problems]}
            write_json(layout.tmp, layout.path(layout.admission(m["commit_id"])), rec)
            records[m["commit_id"]] = rec
            (admitted if status == AdmissionStatus.ADMITTED else rejected).add(m["commit_id"])
            result["admitted" if status == AdmissionStatus.ADMITTED else "rejected"].append(
                {"commit_id": m["commit_id"], "key": m["key"], "problems": rec["problems"]})

        progress = True
        while progress:
            progress = False
            reg_head, _ = chain_head([by_id[c] for c in admitted if c in by_id], REGISTRY_KEY)
            register = _registry_shas(layout, by_id.get(reg_head)) if reg_head else None
            for m in sorted(todo, key=lambda x: (x["key"] != REGISTRY_KEY, x["key"], x["committed_at"])):
                if m["commit_id"] in records:
                    continue
                head, _ = chain_head([by_id[c] for c in admitted if c in by_id], m["key"])
                parent = m.get("parent_commit_id")
                if parent != head:
                    if parent is not None and parent in rejected:
                        record(m, AdmissionStatus.REJECTED, [[ErrorCode.COMMIT_CONFLICT, "parent was rejected"]])
                        progress = True
                    elif parent is None or parent in admitted:
                        record(m, AdmissionStatus.REJECTED,
                               [[ErrorCode.COMMIT_CONFLICT, f"parent {parent} is not the admitted head {head}"]])
                        progress = True
                    continue            # parent not published yet or still pending: decide later
                problems = verify_commit(layout, m, register)
                record(m, AdmissionStatus.REJECTED if problems else AdmissionStatus.ADMITTED, problems)
                progress = True
                if m["key"] == REGISTRY_KEY:
                    break               # re-read the register before checking sources
        result["pending"] = sorted(m["commit_id"] for m in todo if m["commit_id"] not in records)
    return result


def require_canonical(layout: CanonLayout) -> None:
    if layout.kind() != RootKind.CANONICAL:
        raise RootError("admission runs only on the CANONICAL root")
