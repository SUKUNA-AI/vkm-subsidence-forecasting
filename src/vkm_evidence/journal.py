"""Single-writer immutable evidence journal; HEAD is the only commit point.

The PostgreSQL queue is disposable. Acknowledged decisions live in hashed Parquet
partitions and the parent-linked commit log. An interrupted, unacknowledged write
can be retried with the same request; different payloads cannot reuse its ID.
"""
from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from pydantic import TypeAdapter

from vkm_corpus.contracts.access import AccessContext, ResourcePolicy
from vkm_corpus.parquet.atomic import create_exclusive, sha256_of, write_bytes, write_with
from vkm_evidence.contracts import (EvidenceBatch, EvidenceRecord, ObjectRef, ReviewDecision,
                                    ScientificUseAdmission, VersionRef, canonical_bytes, record_hash)

RECORD = TypeAdapter(EvidenceRecord)
MAX_BATCH_RECORDS = 4096
MAX_BATCH_BYTES = 16 * 1024 * 1024
ZERO = "0" * 64


class JournalConflict(ValueError):
    pass


class JournalCorruption(ValueError):
    pass


def _digest(value: str) -> str:
    if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise JournalCorruption("invalid journal identity")
    return value


class EvidenceJournal:
    """Runtime root must be outside tracked source files, with restrictive OS ACLs.

    object_validator is an authoritative, hash-verifying locator resolver, not a
    Boolean supplied by the caller. It is mandatory when publishing source-backed
    records. Review authority is configured by the operator, never by record data.
    """

    def __init__(self, root: Path, *, object_validator: Callable[[ObjectRef], ResourcePolicy] | None = None,
                 reviewers: frozenset[str] = frozenset()):
        self.root = Path(root).resolve()
        self.object_validator = object_validator
        self.reviewers = reviewers

    def _path(self, *parts: str) -> Path:
        candidate = self.root.joinpath(*parts)
        if not candidate.resolve().is_relative_to(self.root):
            raise JournalCorruption("journal path escapes root")
        # Reject links even if they point inside the root: immutable filenames
        # must refer to owned bytes, not a retargetable alias.
        for parent in (candidate, *candidate.parents):
            if parent == self.root:
                break
            if parent.is_symlink():
                raise JournalCorruption("symlink in journal")
        return candidate

    @property
    def revision(self) -> str:
        head = self._path("HEAD")
        return _digest(head.read_text(encoding="ascii").strip()) if head.exists() else ZERO

    @contextmanager
    def _writer(self):
        self.root.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        lock = self._path("publisher.lock")
        if not create_exclusive(lock, canonical_bytes({"token": token, "pid": os.getpid()}).decode("utf-8")):
            raise JournalConflict("publisher busy; stale locks require operator recovery")
        try:
            yield
        finally:
            if json.loads(lock.read_text(encoding="utf-8"))["token"] != token:
                raise JournalCorruption("publisher lock owner changed")
            lock.unlink()

    def commits(self, revision: str | None = None) -> list[dict]:
        current, seen, result = _digest(revision or self.revision), set(), []
        while current != ZERO:
            if current in seen:
                raise JournalCorruption("cyclic commit log")
            seen.add(current)
            path = self._path("commits", current + ".json")
            try:
                commit = json.loads(path.read_bytes())
            except (OSError, ValueError) as exc:
                raise JournalCorruption("missing or invalid commit") from exc
            if record_hash(commit) != current or commit.get("schema") != "vkm-evidence-commit/1":
                raise JournalCorruption("commit hash/schema mismatch")
            result.append({"revision": current, **commit})
            current = _digest(commit["parent"])
        return list(reversed(result))

    def iter_records(self, revision: str | None = None) -> Iterator[EvidenceRecord]:
        import pyarrow.parquet as pq

        for commit in self.commits(revision):
            path = self._path("partitions", _digest(commit["partition_sha256"]) + ".parquet")
            if sha256_of(path) != commit["partition_sha256"]:
                raise JournalCorruption("evidence partition hash mismatch")
            parquet = pq.ParquetFile(path)
            if parquet.metadata.num_rows != commit["record_count"]:
                raise JournalCorruption("evidence row count mismatch")
            for batch in parquet.iter_batches(batch_size=1024):
                for row in batch.to_pylist():
                    record = RECORD.validate_json(row["payload_json"])
                    if record_hash(record) != row["record_sha256"] or record.record_id != row["record_id"]:
                        raise JournalCorruption("evidence record identity mismatch")
                    yield record

    def records(self, revision: str | None = None) -> dict[str, EvidenceRecord]:
        result: dict[str, EvidenceRecord] = {}
        for record in self.iter_records(revision):
            previous = result.get(record.record_id)
            if previous is None:
                if record.revision != 1:
                    raise JournalCorruption("missing first record revision")
            elif record.supersedes != previous.version_ref or record.revision != previous.revision + 1:
                raise JournalCorruption("broken record revision chain")
            result.pop(record.record_id, None)
            result[record.record_id] = record
        return result

    def publish(self, request_id: str, base_revision: str, batch: EvidenceBatch,
                context: AccessContext, *, after_commit: Callable[[], None] | None = None) -> dict:
        """ACK follows durable HEAD. after_commit is used by fault-injection drills."""
        if not request_id or len(request_id) > 200:
            raise ValueError("invalid request ID")
        _digest(base_revision)
        # Detach nested mutable WorldSpec objects and revalidate before hashing.
        batch = EvidenceBatch.model_validate_json(batch.model_dump_json())
        for record in batch.records:
            record.policy.require(context)
            if record.actor != context.principal:
                raise PermissionError("record actor differs from publisher principal")
        payload = canonical_bytes({"request_id": request_id, "base_revision": base_revision,
                                   "principal": context.principal, "batch": batch.model_dump(mode="json")})
        if not batch.records or len(batch.records) > MAX_BATCH_RECORDS or len(payload) > MAX_BATCH_BYTES:
            raise ValueError("evidence batch empty or over configured bounds")
        request_sha = record_hash(json.loads(payload))
        with self._writer():
            for commit in self.commits():
                if commit["request_id"] == request_id:
                    if commit["request_sha256"] != request_sha:
                        raise JournalConflict("request ID already used with different content")
                    return self._receipt(commit)
            if self.revision != base_revision:
                raise JournalConflict("stale base revision")
            current = self.records(base_revision)
            proposed = {rid: r for rid, r in current.items() if rid not in {x.record_id for x in batch.records}}
            proposed.update({r.record_id: r for r in batch.records})
            for record in batch.records:
                record.policy.require(context)
                if record.actor != context.principal:
                    raise PermissionError("record actor differs from publisher principal")
                previous = current.get(record.record_id)
                if previous is None:
                    if record.revision != 1:
                        raise JournalConflict("correction without previous version")
                elif record.supersedes != previous.version_ref or record.revision != previous.revision + 1:
                    raise JournalConflict("stale record correction")
                if previous is not None and not record.policy.preserves(previous.policy):
                    raise PermissionError("policy downgrade requires a separate owner decision")
                if record.review_state != "UNREVIEWED" and not isinstance(record, ReviewDecision):
                    expected = {"VERIFIED_TRANSCRIPTION": "VERIFIED_TRANSCRIPTION", "SEMANTIC_REVIEWED": "SEMANTIC_REVIEWED",
                                "CONFLICT": "CONFLICT", "REJECTED": "REJECTED"}[record.review_state]
                    matching = [r for r in proposed.values() if isinstance(r, ReviewDecision)
                                and r.target == record.version_ref and r.decision == expected]
                    if not matching:
                        raise ValueError("self-declared review state lacks an exact review decision")
                from vkm_evidence.validation import version_references
                for ref in version_references(record):
                    target = proposed.get(ref.record_id)
                    if target is None or target.version_ref != ref:
                        raise JournalConflict("dangling or stale dependency")
                    target.policy.require(context)
                    if not record.policy.preserves(target.policy):
                        raise PermissionError("derived record widens dependency access")
                for support in (*record.supports, *(s for b in getattr(record, "symbols", ()) for s in b.supports)):
                    if self.object_validator is None:
                        raise ValueError("authoritative object resolver required")
                    support_policy = self.object_validator(support)
                    support_policy.require(context)
                    if not record.policy.preserves(support_policy):
                        raise PermissionError("derived record widens source access")
                if isinstance(record, ReviewDecision):
                    if context.principal not in self.reviewers or record.reviewer_authority != context.principal:
                        raise PermissionError("review authority not configured")
                    target = proposed.get(record.target.record_id)
                    if target is None or target.version_ref != record.target:
                        raise JournalConflict("review targets a missing or stale record")
                    if record.decision in {"VERIFIED_TRANSCRIPTION", "SEMANTIC_REVIEWED"}:
                        required_supports = (*target.supports, *(s for b in getattr(target, "symbols", ()) for s in b.supports))
                        if not set(required_supports).issubset(set(record.supports)):
                            raise ValueError("review does not inspect all original supports")
                if isinstance(record, ScientificUseAdmission) and record.status == "READY":
                    from vkm_evidence.validation import validate_admission
                    reasons = validate_admission(record, proposed, context)
                    if reasons:
                        raise ValueError("admission rejected: " + ",".join(reasons))
            from vkm_evidence.validation import validate_reference_graph
            validate_reference_graph(proposed)
            commit = self._write_partition(batch, request_id, request_sha, base_revision)
            revision = record_hash(commit)
            write_bytes(self._path("tmp"), self._path("commits", revision + ".json"), canonical_bytes(commit))
            write_bytes(self._path("tmp"), self._path("HEAD"), (revision + "\n").encode("ascii"), overwrite=True)
            if after_commit:
                after_commit()
            return self._receipt({"revision": revision, **commit})

    def _write_partition(self, batch: EvidenceBatch, request_id: str, request_sha: str, parent: str) -> dict:
        import pyarrow as pa
        import pyarrow.parquet as pq

        rows = [{"record_id": r.record_id, "record_sha256": record_hash(r), "kind": r.kind,
                 "revision": r.revision, "payload_json": canonical_bytes(r).decode("utf-8")} for r in batch.records]
        table = pa.Table.from_pylist(rows)
        # Address the partition by its physical bytes, not by a filename controlled by the caller.
        buffer = pa.BufferOutputStream()
        pq.write_table(table, buffer, compression="zstd")
        data = buffer.getvalue().to_pybytes()
        import hashlib
        sha = hashlib.sha256(data).hexdigest()
        write_bytes(self._path("tmp"), self._path("partitions", sha + ".parquet"), data)
        return {"schema": "vkm-evidence-commit/1", "parent": parent, "request_id": request_id,
                "request_sha256": request_sha, "partition_sha256": sha, "record_count": len(rows),
                "records": [{"id": r.record_id, "sha256": record_hash(r)} for r in batch.records]}

    @staticmethod
    def _receipt(commit: dict) -> dict:
        return {"status": "COMMITTED", "revision": commit["revision"], "parent": commit["parent"],
                "request_id": commit["request_id"], "request_sha256": commit["request_sha256"],
                "partition_sha256": commit["partition_sha256"], "record_count": commit["record_count"]}
