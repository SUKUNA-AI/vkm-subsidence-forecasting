"""Content binding for a representation and its checkpoint byte stream."""
from __future__ import annotations

import hashlib
from io import BytesIO
import json
from pathlib import Path

import numpy as np
from scipy.sparse import csr_matrix


_REQUIRED = {"geometry", "materials", "topology", "boundary", "partition", "laws"}


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _array_digest(value, dtype):
    array = np.ascontiguousarray(value, dtype=dtype)
    digest = hashlib.sha256(_json(list(array.shape)))
    digest.update(array.tobytes())
    return digest.hexdigest()


def representation_snapshot(matrix, free_dofs, components):
    """Bind actual matrix ordering and explicit physical/representation dependencies.

    Component values are caller-provided content hashes or JSON metadata. They
    identify inputs but do not grant scientific admission to those inputs.
    """
    if not _REQUIRED <= set(components):
        raise ValueError("representation components omit a required physical dependency")
    if np.iscomplexobj(matrix.data):
        raise ValueError("complex stiffness is outside this real representation")
    raw_free = np.asarray(free_dofs)
    matrix = csr_matrix(matrix, dtype=np.float64, copy=True)
    matrix.sum_duplicates()
    matrix.sort_indices()
    free = np.asarray(free_dofs, dtype=np.int64)
    if (matrix.shape[0] != matrix.shape[1] or free.ndim != 1 or len(free) != matrix.shape[0]
            or len(np.unique(free)) != len(free) or np.any(free < 0) or not np.array_equal(raw_free, free)
            or not np.isfinite(matrix.data).all()):
        raise ValueError("invalid representation matrix or degree-of-freedom ordering")
    hashes = {
        "shape": list(matrix.shape),
        "indptr": _array_digest(matrix.indptr, "<i8"),
        "indices": _array_digest(matrix.indices, "<i8"),
        "values": _array_digest(matrix.data, "<f8"),
    }
    physical = json.loads(_json(components))
    result = {"schema": "vkm.representation_binding/1", "matrix": hashes,
              "free_dofs": _array_digest(free, "<i8"), "components": physical}
    result["signature"] = hashlib.sha256(_json(result)).hexdigest()
    return result


def assert_applicable(saved, current):
    """Reject any changed matrix, material, topology, law, partition or boundary."""
    for value in (saved, current):
        payload = {k: v for k, v in value.items() if k != "signature"}
        if value.get("signature") != hashlib.sha256(_json(payload)).hexdigest():
            raise ValueError("corrupt representation signature")
    if saved != current:
        raise ValueError("stale representation or checkpoint dependency")


def file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def physical_history_materials(field, carrier, pin, carrier_receipt, cards):
    """Verify the approved field and specific carrier, then order C2 by body ID."""
    geometry_id = pin["geometry_id"]
    field_bytes = Path(field).read_bytes()
    field_sha = hashlib.sha256(field_bytes).hexdigest()
    if field_sha != pin["source_field"]["sha256"]:
        raise ValueError("selected field bytes differ from the approved C1")
    carrier_bytes = Path(carrier).read_bytes()
    if hashlib.sha256(carrier_bytes).hexdigest() != carrier_receipt["representation_sha256"]:
        raise ValueError("mechanical carrier bytes differ from its receipt")
    if (carrier_receipt["geometry_id"], carrier_receipt["source_field_sha256"]) != (geometry_id, field_sha):
        raise ValueError("mechanical carrier receipt refers to a different C1")
    if carrier_receipt["geometry_screen_pass"] is not True:
        raise ValueError("mechanical carrier geometry has not passed its checks")
    with np.load(BytesIO(field_bytes), allow_pickle=False) as src:
        if (src["geometry_id"].item(), src["bhj_id"].item()) != (geometry_id, pin["bhj_id"]):
            raise ValueError("selected field identity differs from the approved C1")
        names = src["horizon_names"].tolist()
    if len(names) < 2 or len(set(names)) != len(names):
        raise ValueError("selected field requires unique named horizons")
    bodies = [f"BODY:{lo}:{hi}" for lo, hi in zip(names[:-1], names[1:], strict=True)]
    with np.load(BytesIO(carrier_bytes), allow_pickle=False) as src:
        if (src["geometry_id"].item(), src["source_field_sha256"].item()) != (geometry_id, field_sha):
            raise ValueError("mechanical carrier arrays refer to a different C1")
        if src["body_ids"].tolist() != bodies or src["horizon_names"].tolist() != names:
            raise ValueError("carrier body and horizon IDs mismatch")
    by_id = {}
    for card in cards:
        if not isinstance(card, dict) or "body_id" not in card:
            raise ValueError("C2 requires named material cards")
        name = card["body_id"]
        if name in by_id:
            raise ValueError(f"duplicate C2 body ID: {name}")
        by_id[name] = card
    if set(by_id) != set(bodies):
        raise ValueError("C2 body IDs mismatch")
    return [by_id[name] for name in bodies]


def verify_native_history_state(checkpoint, expected_state_sha256, world_signature, clock_year):
    """Refuse a foreign or changed native state before starting the event adapter."""
    data = Path(checkpoint).read_bytes()
    if hashlib.sha256(data).hexdigest() != expected_state_sha256:
        raise ValueError("native history checkpoint bytes mismatch")
    with np.load(BytesIO(data), allow_pickle=False) as state:
        if state["signature"].item() != world_signature or state["time_year"].item() != clock_year:
            raise ValueError("foreign native history checkpoint or clock")


def write_checkpoint_binding(path, checkpoint, representation, provenance):
    """Write a new sidecar, preserving the original checkpoint and provenance."""
    assert_applicable(representation, representation)
    record = {"schema": "vkm.bound_checkpoint/1", "checkpoint_sha256": file_digest(checkpoint),
              "representation": representation, "provenance": provenance}
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(record, stream, sort_keys=True, indent=2, allow_nan=False)
    return record


def restore_bound(checkpoint, binding, current_representation, restore):
    """Call the real restoration routine only after its byte and context guards."""
    record = json.loads(Path(binding).read_bytes())
    if record.get("schema") != "vkm.bound_checkpoint/1":
        raise ValueError("unsupported checkpoint binding")
    if file_digest(checkpoint) != record["checkpoint_sha256"]:
        raise ValueError("checkpoint bytes differ from the bound state")
    assert_applicable(record["representation"], current_representation)
    return restore(checkpoint)
