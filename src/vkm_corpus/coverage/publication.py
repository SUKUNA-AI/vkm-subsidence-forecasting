"""Pinned accounting publication, receiving admission and restored-closure checks.

Approval/context and the current policy file are operator inputs, never taken
from an incoming descriptor. All errors are codes: no private values in logs.
"""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import Field, field_validator, model_validator

from vkm_corpus.contracts.access import AccessContext, ResourcePolicy
from vkm_corpus.coverage import accounting as A
from vkm_corpus.parquet.atomic import sha256_of, write_bytes
from vkm_evidence.contracts import StrictModel, Sha256, canonical_bytes, record_hash

# Closure limits (owner decision 06.10.2026, chat): a full-base publication after the 93-source OCR v2 recommit is
# ~504k files with a ~101 MB descriptor; the earlier 200k / 64 MiB caps were sized for small intakes.
# Raised again 07.10.2026 (owner, chat): the OCR v2 closure froze at 990k files / 222 MiB, so re-adding
# VKM-SRC-053 / -230 does not fit 1M / 256 MiB.
MAX_FILES = 2_000_000
MAX_BYTES = 512 * 1024 * 1024


class PublicationBlocked(ValueError):
    pass


class FileRef(StrictModel):
    path: str = Field(min_length=1, max_length=2048)
    sha256: Sha256
    size_bytes: int = Field(ge=0)

    @field_validator("path")
    @classmethod
    def _relative(cls, value):
        p = PurePosixPath(value)
        if p.is_absolute() or any(c in value for c in ("\\", ":", "\n", "\r", "\x00")) or \
                any(s in ("", ".", "..") for s in value.split("/")):
            raise ValueError("PUBLICATION_PATH_INVALID")
        return value


class PublicationSource(StrictModel):
    source_id: str = Field(pattern=r"^VKM-SRC-\d{3}$")
    source_sha256: Sha256
    commit_id: str = Field(pattern=r"^CMT-[a-f0-9]{16}$")
    marker: FileRef
    binding: FileRef | None = None


class SnapshotBase(StrictModel):
    snapshot_id: str = Field(pattern=r"^snap-[A-Za-z0-9-]+$")
    manifest_sha256: Sha256
    source_heads: dict[str, str]
    registry_head: str | None = None


class PublicationRequest(StrictModel):
    campaign_sha256: Sha256
    policy_sha256: Sha256
    max_total_bytes: int = Field(gt=0, le=1024 ** 4)
    base: SnapshotBase | None = None
    sources: tuple[PublicationSource, ...] = Field(min_length=1, max_length=10_000)
    registry: FileRef | None = None

    @model_validator(mode="after")
    def _scope(self):
        keys = [s.source_id for s in self.sources]
        if len(set(keys)) != len(keys):
            raise ValueError("PUBLICATION_DUPLICATE_SOURCE")
        base = self.base.source_heads if self.base else {}
        if not set(base).issubset(keys):
            raise ValueError("PUBLICATION_DROPPED_BASE_SOURCE")
        for source in self.sources:
            if source.binding is None and base.get(source.source_id) != source.commit_id:
                raise ValueError("PUBLICATION_NEW_SOURCE_ACCOUNTING_REQUIRED")
        return self


class PublicationDescriptor(PublicationRequest):
    schema_version: Literal["vkm-accounting-publication/1"] = "vkm-accounting-publication/1"
    files: tuple[FileRef, ...] = Field(max_length=MAX_FILES)

    @model_validator(mode="after")
    def _files(self):
        paths = [f.path for f in self.files]
        if paths != sorted(set(paths)):
            raise ValueError("PUBLICATION_FILES_NOT_UNIQUE_SORTED")
        return self


class PublicationApproval(StrictModel):
    """Operator-owned frozen campaign binding. Incoming files cannot grant access."""
    descriptor_sha256: Sha256
    campaign_sha256: Sha256
    policy_sha256: Sha256
    source_ids: tuple[str, ...] = Field(min_length=1, max_length=10_000)
    context: AccessContext

    @model_validator(mode="after")
    def _sources(self):
        import re
        if len(set(self.source_ids)) != len(self.source_ids) or any(
                not re.fullmatch(r"VKM-SRC-\d{3}", s) for s in self.source_ids):
            raise ValueError("PUBLICATION_APPROVAL_SCOPE_INVALID")
        return self


def _json(path: Path, *, limit=MAX_BYTES):
    if path.stat().st_size > limit:
        raise PublicationBlocked("PUBLICATION_JSON_LIMIT")
    def unique(pairs):
        result = {}
        for k, v in pairs:
            if k in result:
                raise PublicationBlocked("PUBLICATION_DUPLICATE_JSON_KEY")
            result[k] = v
        return result
    return json.loads(path.read_bytes(), object_pairs_hook=unique)


def authorize(policy_path, policy_sha256, source_ids, context):
    if policy_path is None:
        raise PublicationBlocked("PUBLICATION_CURRENT_POLICY_REQUIRED")
    path = Path(policy_path).absolute()
    A._path(path.parent, path.name)
    if not path.is_file() or path.stat().st_size > MAX_BYTES or sha256_of(path) != policy_sha256:
        raise PublicationBlocked("PUBLICATION_CURRENT_POLICY_CHANGED")
    payload = _json(path)
    if set(payload) != {"schema", "policies"} or payload["schema"] != "vkm-source-policy/1":
        raise PublicationBlocked("PUBLICATION_POLICY_INVALID")
    for sid in source_ids:
        if sid not in payload["policies"]:
            raise PermissionError("RESOURCE_POLICY_UNCLASSIFIED")
        ResourcePolicy.model_validate(payload["policies"][sid]).require(context)
    return payload["policies"]


def file_ref(root, relative):
    path = A._path(root, relative)
    if not path.is_file():
        raise PublicationBlocked("PUBLICATION_FILE_MISSING")
    return FileRef(path=relative, sha256=sha256_of(path), size_bytes=path.stat().st_size)


def read_ref(root, reference):
    ref = FileRef.model_validate(reference)
    path = A._path(root, ref.path)
    if not path.is_file():
        raise PublicationBlocked("PUBLICATION_FILE_MISSING")
    if path.stat().st_size != ref.size_bytes or sha256_of(path) != ref.sha256:
        raise PublicationBlocked("PUBLICATION_FILE_CHANGED")
    return path


def descriptor_path(digest):
    # The SHA is validated by all public callers' typed contracts.
    return f"accounting/publications/{digest}.json"


def _marker(root, ref, key):
    from vkm_corpus import ids
    marker = _json(read_ref(root, ref))
    if marker.get("key") != key or ids.commit_id(marker) != marker.get("commit_id") or \
            not ref.path.startswith("canonical/_commits/"):
        raise PublicationBlocked("PUBLICATION_MARKER_IDENTITY_INVALID")
    return marker


def _source_lineage(root, source_id, commit_id):
    from vkm_corpus.parquet.commits import accounting_lineage
    return accounting_lineage(A._commit_marker(root, source_id, commit_id),
        lambda cid: A._commit_marker(root, source_id, cid))


def _authorize_partition(path, dataset, policies, context):
    """Only attribution columns are read until every actual source is allowed.

    A policy inventory is not evidence that a mixed run contains only its keys.
    Content columns (text/messages/values) are never materialised by this gate.
    """
    import re
    import pyarrow.parquet as pq
    from vkm_corpus.contracts import arrow as ca
    from vkm_corpus.contracts.datasets import DATASETS
    if dataset not in DATASETS:
        raise PublicationBlocked("PUBLICATION_PARTITION_DATASET_UNKNOWN")
    parquet = pq.ParquetFile(path)
    names = parquet.schema_arrow.names
    # Resolve recognizable attribution even for a malformed schema; no raw row
    # is hashed/published before this policy preflight.
    columns = [n for n in names if n == "source_id" or n.endswith("_source_id")
               or n == "page_id" or n.endswith("_page_id")]
    for batch in parquet.iter_batches(batch_size=1024, columns=columns):
        for row in batch.to_pylist():
            for name, value in row.items():
                if value is None:
                    continue
                if not isinstance(value, str) or not re.match(r"^VKM-SRC-\d{3}(?::|$)", value):
                    raise PublicationBlocked("PUBLICATION_SOURCE_ATTRIBUTION_INVALID")
                sid = value[:11]
                if sid not in policies:
                    raise PermissionError("RESOURCE_POLICY_UNCLASSIFIED")
                ResourcePolicy.model_validate(policies[sid]).require(context)
    expected = set(ca.arrow_schema(dataset).names)
    kv = parquet.schema_arrow.metadata or {}
    version = (kv.get(b"vkm.schema_version") or b"").decode("utf-8", "replace")
    fingerprint = (kv.get(b"vkm.schema_fingerprint") or b"").decode("utf-8", "replace")
    if version != DATASETS[dataset].version and ca.readable_schema(dataset, version, fingerprint):
        # an unchanged base partition of an exact allowlisted historical schema (e.g. tables 0.1.0/0.1.2) is
        # published as is: its column set is the current one minus the columns that version did not have
        from vkm_corpus.contracts.datasets import historical_omissions
        expected -= historical_omissions(dataset, version)
    if set(names) != expected:
        raise PublicationBlocked("PUBLICATION_PARTITION_SCHEMA_INVALID")


def _closure(root, request, *, policies, context):
    """One source at a time. The finite file map contains hashes, never row text."""
    from vkm_corpus.contracts.vocab import ARTIFACT_KIND_DIR
    files = {}
    runs = set()
    total_bytes = 0
    verified_blobs = set()
    # Publication includes shared run journals and optionally the registry.
    # Its operator therefore needs access to the complete policy inventory.
    for sid, policy in policies.items():
        if sid.startswith("VKM-SRC-"):
            ResourcePolicy.model_validate(policy).require(context)
    def add(ref):
        nonlocal total_bytes
        if len(files) >= MAX_FILES and ref.path not in files:
            raise PublicationBlocked("PUBLICATION_FILE_COUNT_LIMIT")
        if ref.path in files and files[ref.path] != ref:
            raise PublicationBlocked("PUBLICATION_FILE_IDENTITY_COLLISION")
        if ref.path in files:
            return
        if ref.path not in files:
            total_bytes += ref.size_bytes
            if total_bytes > request.max_total_bytes:
                raise PublicationBlocked("PUBLICATION_BYTE_BUDGET_EXCEEDED")
        if ref.path.startswith("canonical/") and ref.path.endswith(".parquet"):
            _authorize_partition(A._path(root, ref.path), ref.path.split("/")[1], policies, context)
        read_ref(root, ref)
        files[ref.path] = ref
        if ref.path.startswith("artifacts/"):
            verified_blobs.add(ref.sha256)
    def add_path(path):
        if path in files:
            return files[path]
        if path not in files and A._path(root, path).stat().st_size > request.max_total_bytes - total_bytes:
            raise PublicationBlocked("PUBLICATION_BYTE_BUDGET_EXCEEDED")
        if path.startswith("canonical/") and path.endswith(".parquet"):
            _authorize_partition(A._path(root, path), path.split("/")[1], policies, context)
        ref = file_ref(root, path)
        add(ref)
        return ref
    def blob(digest):
        if digest in verified_blobs:
            return
        # Exact content-addressed filename only; no corpus-wide recursive glob.
        paths = []
        for kind in sorted(set(ARTIFACT_KIND_DIR.values())):
            directory = A._path(root, f"artifacts/{kind}/{digest[:2]}/{digest[2:4]}")
            paths.extend(directory.glob(digest + ".*"))
        if not paths:
            raise PublicationBlocked("PUBLICATION_RAW_BLOB_MISSING")
        for path in paths:
            ref = add_path(path.relative_to(Path(root)).as_posix())
            if ref.sha256 != digest:
                raise PublicationBlocked("PUBLICATION_RAW_BLOB_CHANGED")
    def marker_files(marker):
        runs.add(marker["processing_run_id"])
        for entry in [*marker["datasets"].values(), *marker.get("artifact_index_files", [])]:
            add(FileRef(path="canonical/" + entry["path"], sha256=entry["sha256"], size_bytes=entry["bytes"]))
        for item in marker.get("artifact_blobs", []):
            add(FileRef(path="artifacts/" + item["storage_relpath"], sha256=item["artifact_id"][7:], size_bytes=item["bytes"]))
    base = request.base.source_heads if request.base else {}
    for source in request.sources:
        if source.binding and source.binding.path != f"accounting/commits/{source.binding.sha256}.json":
            raise PublicationBlocked("PUBLICATION_BINDING_PATH_INVALID")
        add(source.marker)
        marker = _marker(root, source.marker, source.source_id)
        for ancestor in _source_lineage(root, source.source_id, source.commit_id)[1:]:
            ancestor_path = f"canonical/_commits/run={ancestor['processing_run_id']}/{source.source_id}__{ancestor['commit_id']}.json"
            add_path(ancestor_path)
        if (marker["commit_id"] != source.commit_id or marker["source_sha256"] != source.source_sha256
                or (source.commit_id != base.get(source.source_id) and marker.get("parent_commit_id") != base.get(source.source_id))):
            raise PublicationBlocked("PUBLICATION_SOURCE_OR_BASE_MISMATCH")
        marker_files(marker)
        if source.binding is None:
            if marker.get("accounting_required"):
                raise PublicationBlocked("PUBLICATION_REQUIRED_BINDING_MISSING")
            continue
        add(source.binding)
        receipt = _json(read_ref(root, source.binding))
        if receipt["report"]["path"] != f"accounting/reports/{receipt['report']['sha256']}.json":
            raise PublicationBlocked("PUBLICATION_REPORT_PATH_INVALID")
        report_ref = add_path(receipt["report"]["path"])
        if report_ref.sha256 != receipt["report"]["sha256"]:
            raise PublicationBlocked("PUBLICATION_REPORT_HASH_MISMATCH")
        report = _json(read_ref(root, report_ref), limit=A.MAX_REPORT_BYTES)
        if A.verify_binding(root, source.binding.model_dump(), source_id=source.source_id,
                            commit_id=source.commit_id) != "ACCOUNTED":
            raise PublicationBlocked("PUBLICATION_SOURCE_NOT_ACCOUNTED")
        A.verify_committed_outputs(root, report, marker)
        ledger = A.CoverageLedger.model_validate(report["ledger"])
        expected = report.get("expected", {})
        if (ledger.report()["status"] != "ACCOUNTED" or report.get("gaps") or report.get("unmatched_output_ids")
                or expected.get("denominator_state") != "KNOWN" or not expected.get("pagination_agrees")
                or any(expected.get(k) for k in ("missing_indices", "extra_indices", "duplicate_indices", "invalid_index_count"))
                or any(u.source_id != source.source_id or u.source_sha256 != source.source_sha256 for u in ledger.units)):
            raise PublicationBlocked("PUBLICATION_ACCOUNTING_INCOMPLETE")
        pages = {u.unit_id for u in ledger.units if u.unit_kind == "PAGE"}
        count, unit = expected.get("expected_count"), expected.get("unit")
        if (type(count) is not int or not 0 <= count <= A.LIMIT or unit not in {"p", "r", "s"}
                or marker.get("page_count") != count
                or pages != {f"{source.source_id}:{unit}{i:04d}" for i in range(1, count + 1)}
                or report["report"].get("ledger_sha256") != record_hash(ledger)):
            raise PublicationBlocked("PUBLICATION_DENOMINATOR_MISMATCH")
        claimed = {}
        for attempt in ledger.attempts:
            for oid, digest in attempt.object_outputs.items():
                if oid in claimed and claimed[oid] != digest:
                    raise PublicationBlocked("PUBLICATION_ATTEMPT_OUTPUT_CONFLICT")
                claimed[oid] = digest
        if claimed != report["output_objects"]:
            raise PublicationBlocked("PUBLICATION_ATTEMPT_OUTPUT_MISMATCH")
        raw = set(report["raw_artifacts"].values())
        for ref in report["ocr_events"]:
            if ref["path"] != f"accounting/events/{source.source_id}/{source.source_sha256}/{ref['sha256']}.json":
                raise PublicationBlocked("PUBLICATION_EVENT_PATH_INVALID")
            event_ref = add_path(ref["path"])
            if event_ref.sha256 != ref["sha256"]:
                raise PublicationBlocked("PUBLICATION_EVENT_CHANGED")
            event = _json(read_ref(root, event_ref))
            if event.get("source_id") != source.source_id or event.get("source_sha256") != source.source_sha256:
                raise PublicationBlocked("PUBLICATION_EVENT_SOURCE_MISMATCH")
            raw.update(event.get("outputs", []))
        for digest in sorted(raw):
            if not isinstance(digest, str) or not A.SHA.fullmatch(digest):
                raise PublicationBlocked("PUBLICATION_BLOB_ID_INVALID")
            blob(digest)
    if request.registry:
        # Registry is a corpus-wide unit. Do not read its rows/partitions with a
        # narrower grant than its authoritative source-policy inventory.
        for sid, policy in policies.items():
            if sid.startswith("VKM-SRC-"):
                ResourcePolicy.model_validate(policy).require(context)
        add(request.registry)
        registry = _marker(root, request.registry, "REGISTRY")
        parent = request.base.registry_head if request.base else None
        if registry["commit_id"] != parent and registry.get("parent_commit_id") != parent:
            raise PublicationBlocked("PUBLICATION_REGISTRY_BASE_MISMATCH")
        marker_files(registry)
    if request.base:
        # The existing snapshot's journals remain visible after a source update.
        # Include their pinned bytes explicitly; do not inherit arbitrary runs
        # merely because they happen to exist on the receiving root.
        relative = f"canonical/_snapshots/{request.base.snapshot_id}.json"
        base_ref = add_path(relative)
        if base_ref.sha256 != request.base.manifest_sha256:
            raise PublicationBlocked("PUBLICATION_BASE_MANIFEST_CHANGED")
        base_manifest = _json(read_ref(root, base_ref))
        if base_manifest.get("source_heads") != request.base.source_heads or base_manifest.get("registry_head") != request.base.registry_head:
            raise PublicationBlocked("PUBLICATION_BASE_HEADS_CHANGED")
        runs.update(base_manifest.get("runs", {}))
        for dataset in ("processing_runs", "processing_steps", "errors", "artifacts"):
            for entry in base_manifest.get("datasets", {}).get(dataset, {}).get("files", []):
                add(FileRef(path="canonical/" + entry["path"], sha256=entry["sha256"], size_bytes=entry["bytes"]))
    for run in sorted(runs):
        from vkm_corpus.ids import grammar
        if not grammar.COMPILED["run"].fullmatch(run):
            raise PublicationBlocked("PUBLICATION_RUN_ID_INVALID")
        for phase in ("START", "END"):
            relative = f"canonical/_runs/run={run}/{phase}.json"
            if A._path(root, relative).is_file():
                ref = add_path(relative)
                journal = _json(read_ref(root, ref))
                if journal.get("processing_run_id") != run:
                    raise PublicationBlocked("PUBLICATION_RUN_IDENTITY_MISMATCH")
                for entry in journal.get("files", []):
                    add(FileRef(path="canonical/" + entry["path"], sha256=entry["sha256"], size_bytes=entry["bytes"]))
        # Preserve crashed-run parts as well. These are bounded, selected run
        # directories, not a recursive scan of the corpus or surviving objects.
        for dataset in ("processing_runs", "processing_steps", "errors", "artifacts"):
            directory = A._path(root, f"canonical/{dataset}/run={run}")
            for path in directory.glob("**/*.parquet"):
                add_path(path.relative_to(Path(root)).as_posix())
        import pyarrow.parquet as pq
        for ref in tuple(files.values()):
            if ref.path.startswith(f"canonical/artifacts/run={run}/"):
                for batch in pq.ParquetFile(read_ref(root, ref)).iter_batches(batch_size=1024,
                        columns=["artifact_id", "storage_relpath", "size_bytes", "materialization"]):
                    for row in batch.to_pylist():
                        if row["materialization"] == "STORED":
                            add(FileRef(path="artifacts/" + row["storage_relpath"], sha256=row["artifact_id"][7:],
                                        size_bytes=row["size_bytes"]))
    return tuple(files[p] for p in sorted(files))


def freeze_publication(root, request: PublicationRequest, *, policy_path, context: AccessContext):
    request = PublicationRequest.model_validate(request)
    policies = authorize(policy_path, request.policy_sha256, [s.source_id for s in request.sources], context)
    files = _closure(Path(root), request, policies=policies, context=context)
    descriptor = PublicationDescriptor(**request.model_dump(), files=files)
    data = canonical_bytes(descriptor)
    if len(data) > MAX_BYTES:
        raise PublicationBlocked("PUBLICATION_DESCRIPTOR_LIMIT")
    digest = record_hash(descriptor)
    authorize(policy_path, request.policy_sha256, [s.source_id for s in request.sources], context)
    write_bytes(A._path(root, "accounting/tmp"), A._path(root, descriptor_path(digest)), data)
    return FileRef(path=descriptor_path(digest), sha256=digest, size_bytes=len(data))


def verify_publication(root, approval: PublicationApproval, *, policy_path, receiving=False):
    approval = PublicationApproval.model_validate(approval)
    policies = authorize(policy_path, approval.policy_sha256, approval.source_ids, approval.context)
    path = A._path(root, descriptor_path(approval.descriptor_sha256))
    if not path.is_file():
        raise PublicationBlocked("PUBLICATION_DESCRIPTOR_MISSING")
    descriptor = PublicationDescriptor.model_validate(_json(path))
    if (sha256_of(path) != approval.descriptor_sha256 or descriptor.campaign_sha256 != approval.campaign_sha256
            or descriptor.policy_sha256 != approval.policy_sha256
            or set(approval.source_ids) != {s.source_id for s in descriptor.sources}):
        raise PublicationBlocked("PUBLICATION_APPROVAL_MISMATCH")
    files = _closure(Path(root), descriptor, policies=policies, context=approval.context)
    if files != descriptor.files:
        raise PublicationBlocked("PUBLICATION_CLOSURE_CHANGED")
    if receiving and descriptor.base:
        base = descriptor.base
        path = A._path(root, f"canonical/_snapshots/{base.snapshot_id}.json")
        if not path.is_file() or sha256_of(path) != base.manifest_sha256:
            raise PublicationBlocked("PUBLICATION_BASE_SNAPSHOT_CHANGED")
        old = _json(path)
        if old["source_heads"] != base.source_heads or old["registry_head"] != base.registry_head:
            raise PublicationBlocked("PUBLICATION_BASE_HEADS_CHANGED")
    authorize(policy_path, approval.policy_sha256, approval.source_ids, approval.context)
    return descriptor


def snapshot_accounting(root, manifest, *, approval=None, policy_path=None):
    """Read-only restore/validation gate; no missing-sidecar auto-upgrade."""
    ref = manifest.get("inputs", {}).get("accounting_publication")
    if approval is not None:
        authorize(policy_path, approval.policy_sha256, approval.source_ids, approval.context)
    if ref is None:
        # Required commit markers are independently checked: stripping the
        # snapshot input cannot turn a modern source into historical data.
        for sid, commit_id in manifest.get("source_heads", {}).items():
            marker = _source_lineage(root, sid, commit_id)[0]
            if marker.get("accounting_required"):
                raise PublicationBlocked("PUBLICATION_SNAPSHOT_BINDING_REQUIRED")
        return {"status": "NOT_AVAILABLE", "scientific_admission": "NOT_ESTABLISHED"}
    if approval is None or policy_path is None or ref.get("sha256") != approval.descriptor_sha256:
        raise PublicationBlocked("PUBLICATION_OPERATOR_APPROVAL_REQUIRED")
    descriptor = verify_publication(root, approval, policy_path=policy_path, receiving=True)
    if (ref.get("path") != descriptor_path(approval.descriptor_sha256)
            or manifest["source_heads"] != {s.source_id: s.commit_id for s in descriptor.sources}
            or manifest.get("registry_head") != (_marker(root, descriptor.registry, "REGISTRY")["commit_id"]
                if descriptor.registry else descriptor.base.registry_head if descriptor.base else None)):
        raise PublicationBlocked("PUBLICATION_SNAPSHOT_HEADS_MISMATCH")
    # Matching names alone are insufficient: the snapshot must actually read
    # the partitions of those markers, including registry and empty datasets.
    expected_files = {}
    markers = []
    for source in descriptor.sources:
        marker = _marker(root, source.marker, source.source_id)
        head = manifest.get("head_commits", {}).get(source.source_id, {})
        if (head.get("commit_id") != source.commit_id or head.get("source_sha256") != source.source_sha256
                or "canonical/" + head.get("marker_path", "") != source.marker.path):
            raise PublicationBlocked("PUBLICATION_SNAPSHOT_MARKER_MISMATCH")
        markers.append(marker)
    if manifest.get("registry_head"):
        head = manifest.get("head_commits", {}).get("REGISTRY", {})
        registry = _marker(root, file_ref(root, "canonical/" + head.get("marker_path", "")), "REGISTRY")
        if registry["commit_id"] != manifest["registry_head"] or head.get("commit_id") != registry["commit_id"]:
            raise PublicationBlocked("PUBLICATION_SNAPSHOT_REGISTRY_MISMATCH")
        markers.append(registry)
    for marker in markers:
        for dataset, entry in marker["datasets"].items():
            expected_files.setdefault(dataset, {})[entry["path"]] = entry
    for dataset, expected in expected_files.items():
        actual_list = manifest.get("datasets", {}).get(dataset, {}).get("files", [])
        actual = {entry["path"]: entry for entry in actual_list}
        if len(actual) != len(actual_list) or actual != expected:
            raise PublicationBlocked("PUBLICATION_SNAPSHOT_PARTITIONS_MISMATCH")
    closure = {f.path: f for f in descriptor.files}
    for dataset in manifest.get("datasets", {}).values():
        for entry in dataset.get("files", []):
            ref = closure.get("canonical/" + entry["path"])
            if ref is None or (ref.sha256, ref.size_bytes) != (entry["sha256"], entry["bytes"]):
                raise PublicationBlocked("PUBLICATION_SNAPSHOT_UNAPPROVED_PARTITION")
    return {"status": "VERIFIED", "descriptor_sha256": approval.descriptor_sha256,
        "accounted_sources": sum(s.binding is not None for s in descriptor.sources),
        "historical_not_available": sum(s.binding is None for s in descriptor.sources),
        "scientific_admission": "NOT_ESTABLISHED"}


def require_receiving_base(layout, descriptor, digest):
    from vkm_corpus.parquet.reader import current_snapshot_id, load_manifest
    current = current_snapshot_id(layout)
    wanted = descriptor.base.snapshot_id if descriptor.base else None
    if current == wanted:
        return
    # Lost ACK after the exact snapshot was already published is idempotent.
    if current:
        existing = load_manifest(layout, current)
        if (existing.get("inputs", {}).get("accounting_publication", {}).get("sha256") == digest
                and existing["source_heads"] == {s.source_id: s.commit_id for s in descriptor.sources}):
            return
    raise PublicationBlocked("PUBLICATION_STALE_BASE")
