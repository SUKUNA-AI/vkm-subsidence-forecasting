"""Immutable logical catalogue and durable inspection receipts, outside Git."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from vkm_corpus.contracts.access import ResourcePolicy
from vkm_evidence.contracts import canonical_bytes, record_hash
from .contracts import DatasetArtifact, DatasetCatalogSnapshot
from .manifest import (DatasetVersion, confined, durable_mkdir, fsync_directory,
                       fsync_file, sha256)

MAX_JSON_BYTES = 256 * 1024 * 1024


class DatasetBlocked(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def read_json(path: Path):
    confined(path.parent, path.name)
    if path.stat().st_size > MAX_JSON_BYTES:
        raise DatasetBlocked("DATASET_METADATA_LIMIT")
    raw = path.read_bytes()
    if len(raw) > MAX_JSON_BYTES:
        raise DatasetBlocked("DATASET_METADATA_LIMIT")
    def pairs(items):
        result = {}
        for k, v in items:
            if k in result:
                raise DatasetBlocked("DUPLICATE_METADATA_KEY")
            result[k] = v
        return result
    return json.loads(raw, object_pairs_hook=pairs)


def write_once(path: Path, data: bytes):
    """Atomic no-replace publication with durable replay; retain interrupted files."""
    confined(path.parent, path.name, must_exist=False)
    durable_mkdir(path.parent)
    if path.exists():
        if path.stat().st_size != len(data) or path.read_bytes() != data:
            raise DatasetBlocked("IMMUTABLE_DATASET_OUTPUT_CONFLICT")
        fsync_file(path)
        fsync_directory(path.parent)
        return
    fd, pending = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.link(pending, path)
    except FileExistsError:
        if path.read_bytes() != data:
            raise DatasetBlocked("IMMUTABLE_DATASET_OUTPUT_CONFLICT")
    fsync_directory(path.parent)


def artifact(root, path):
    return DatasetArtifact(path=path.relative_to(root).as_posix(), sha256=sha256(path))


def checked_artifact(root, ref):
    path = confined(root, ref.path)
    if sha256(path) != ref.sha256:
        raise DatasetBlocked("DATASET_ARTIFACT_CHANGED")
    return path


def policy_key(dataset_id):
    return "DATASET:" + dataset_id


def current_policy(policies, version, context=None):
    policy = policies.get(policy_key(version.dataset_id))
    if policy is None:
        raise DatasetBlocked("DATASET_POLICY_UNCLASSIFIED")
    if not isinstance(policy, ResourcePolicy):
        policy = ResourcePolicy.model_validate(policy)
    if context is not None:
        policy.require(context)
    if (policy.access_class != version.policy.access_class or
            policy.experimental_role != version.policy.experimental_role):
        raise DatasetBlocked("DATASET_POLICY_VERSION_MISMATCH")
    return policy


def require_selection_policy(policies, entry, context=None, replacement=None):
    """Check authoritative parents before opening any selection's native metadata."""
    effective = replacement if replacement is not None else entry.policy
    if policies.get(policy_key(entry.dataset_id)) != effective:
        raise DatasetBlocked("DATASET_CATALOGUE_POLICY_CHANGED")
    if not effective.preserves(entry.policy):
        raise DatasetBlocked("DATASET_POLICY_DOWNGRADE")
    if context is not None:
        effective.require(context)
    for link in (*entry.source_links, *entry.inherited_source_links):
        parent = policies.get(link.source_id)
        if parent is None:
            raise DatasetBlocked("DATASET_SOURCE_POLICY_UNCLASSIFIED")
        if not effective.preserves(parent):
            raise DatasetBlocked("DATASET_SOURCE_POLICY_WIDENING")
        if context is not None:
            parent.require(context)


def verify_inspection(root, receipt_ref, *, version_sha256=None, request_sha256=None):
    receipt = read_json(checked_artifact(root, receipt_ref))
    if (receipt.get("schema") != "vkm-dataset-inspection-receipt/1" or receipt.get("status") != "PASS" or
            (version_sha256 is not None and receipt.get("dataset_version") != version_sha256) or
            (request_sha256 is not None and receipt.get("request_sha256") != request_sha256)):
        raise DatasetBlocked("DATASET_INSPECTION_IDENTITY_MISMATCH")
    output = DatasetArtifact.model_validate(receipt["output"])
    inspected = read_json(checked_artifact(root, output))
    if inspected.get("dataset_version") != receipt["dataset_version"] or inspected.get("entrypoint") != receipt["entrypoint"]:
        raise DatasetBlocked("DATASET_INSPECTION_OUTPUT_MISMATCH")
    return receipt


def publish_inspection(root, folder, version, entrypoint, request, produce):
    request_sha = record_hash(request)
    marker = folder / "inspection-receipt.json"
    output = folder / "inspection.json"
    if marker.exists():
        ref = artifact(root, marker)
        verify_inspection(root, ref, version_sha256=version.digest, request_sha256=request_sha)
        fsync_file(output); fsync_file(marker); fsync_directory(folder)
        return ref
    if output.exists():
        raise DatasetBlocked("INCOMPLETE_INSPECTION_REQUIRES_NEW_CAMPAIGN")
    result = produce()
    # Native values live only in this protected artifact, never runtime logs.
    raw = canonical_bytes(result)
    if len(raw) > MAX_JSON_BYTES:
        raise DatasetBlocked("DATASET_METADATA_LIMIT")
    write_once(output, raw)
    receipt = {"schema": "vkm-dataset-inspection-receipt/1", "status": "PASS",
        "dataset_version": version.digest, "entrypoint": entrypoint, "request_sha256": request_sha,
        "output": artifact(root, output).model_dump(mode="json"), "gate": "NATIVE_INSPECTION_ONLY",
        "scientific_admission": "NOT_ESTABLISHED", "files_durability": "FSYNC",
        "directories_durability": "FSYNC" if os.name == "posix" else "NOT_QUALIFIED",
        "power_loss_qualification": "NOT_RUN"}
    write_once(marker, canonical_bytes(receipt))
    return artifact(root, marker)


def verify_catalogue(root: Path, path: Path, *, policies=None, policy_sha256=None, context=None,
                     replacement_policies=None):
    path = confined(root, path.relative_to(root).as_posix())
    snapshot = DatasetCatalogSnapshot.model_validate(read_json(path))
    if path.name != snapshot.sha256 + ".json":
        raise DatasetBlocked("DATASET_CATALOGUE_IDENTITY_MISMATCH")
    if policy_sha256 is not None and snapshot.policy_sha256 != policy_sha256:
        raise DatasetBlocked("DATASET_CATALOGUE_POLICY_CHANGED")
    for entry in snapshot.entries:
        if policies is not None:
            require_selection_policy(policies, entry, context, (replacement_policies or {}).get(entry.dataset_id))
        version = DatasetVersion.from_dict(read_json(checked_artifact(root, entry.manifest)))
        if version.dataset_id != entry.dataset_id or version.digest != entry.version_sha256:
            raise DatasetBlocked("DATASET_VERSION_CHANGED")
        if (entry.policy.access_class != version.policy.access_class or
                entry.policy.experimental_role != version.policy.experimental_role):
            raise DatasetBlocked("DATASET_POLICY_VERSION_MISMATCH")
        if {s.source_id for s in entry.source_links} != set(version.source_ids):
            raise DatasetBlocked("DATASET_SOURCE_LINK_MISMATCH")
        if bool(entry.source_links) != bool(entry.source_register_sha256):
            raise DatasetBlocked("DATASET_SOURCE_REGISTER_BINDING_MISSING")
        if entry.lifecycle != "ACTIVE":
            if not entry.reason:
                raise DatasetBlocked("DATASET_LIFECYCLE_REASON_MISSING")
            continue
        if set(entry.inspections) != set(version.entrypoints):
            raise DatasetBlocked("DATASET_INSPECTION_COVERAGE_INCOMPLETE")
        expected_vectors = {p for p in version.entrypoints if Path(p).suffix.lower() in {".tab", ".mif", ".gpkg"}}
        if set(entry.conversions) != expected_vectors:
            raise DatasetBlocked("DATASET_CONVERSION_COVERAGE_INCOMPLETE")
        for name, ref in entry.inspections.items():
            receipt = verify_inspection(root, ref, version_sha256=version.digest)
            if receipt["entrypoint"] != name:
                raise DatasetBlocked("DATASET_ENTRYPOINT_MISMATCH")
        from .gis import verify_conversion_bundle
        for name, ref in entry.conversions.items():
            receipt_path = checked_artifact(root, ref)
            verify_conversion_bundle(receipt_path.parent, version.digest)
            if read_json(receipt_path)["entrypoint"] != name:
                raise DatasetBlocked("DATASET_ENTRYPOINT_MISMATCH")
    return snapshot


def publish_catalogue(root, snapshot):
    path = root / "datasets" / "catalogues" / (snapshot.sha256 + ".json")
    write_once(path, canonical_bytes(snapshot))
    return path
