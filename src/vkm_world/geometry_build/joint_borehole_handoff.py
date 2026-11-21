"""Read the complete spatial and vertical transfer without importing the solver."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from pathlib import Path


CONTRACT_VERSION = "vkm.borehole_joint_adjustment/handoff/1"


def _read_bound(root, binding):
    path = (root / binding["path"]).resolve()
    if not path.is_relative_to(root):
        raise ValueError("A handoff input escapes the bundle root")
    if path.stat().st_size != binding["bytes"]:
        raise ValueError(f"Incorrect byte length: {binding['path']}")
    with path.open("rb") as source:
        content = source.read()
    if hashlib.sha256(content).hexdigest() != binding["sha256"]:
        raise ValueError(f"Checksum mismatch: {binding['path']}")
    return json.loads(content)


def _finite_xy(values):
    return (type(values) is list and len(values) == 2
            and all(type(value) in (int, float) and math.isfinite(value) for value in values))


def read_foundation_bundle(manifest_path, *, require_acceptance=False):
    """Check version, exact membership, same-owner source links and transfer eligibility."""
    manifest_path = Path(manifest_path).resolve()
    manifest = json.loads(manifest_path.read_bytes())
    if manifest["schema"] != CONTRACT_VERSION:
        raise ValueError("Unsupported spatial handoff generation")
    if require_acceptance and manifest.get("acceptance") != "ACCEPTED_SPATIAL_FOUNDATION_WITH_LIMITS":
        raise ValueError("Spatial handoff lacks required acceptance")
    configured_files = manifest["files"]
    root = manifest_path.parent
    problem = _read_bound(root, configured_files["problem"])
    result = _read_bound(root, configured_files["solution"])
    verticals = _read_bound(root, configured_files["vertical"])
    if require_acceptance and result.get("status") != "COMPLETED_DECLARED_DESIGN":
        raise ValueError("Acceptance requested for an incomplete joint design")
    if problem["problem_id"] != result["problem_id"]:
        raise ValueError("Solution does not bind the declared input generation")
    if result["input_hashes"]["problem"] != manifest["semantic_problem_sha256"]:
        raise ValueError("Semantic input mismatch")
    target = set(problem["target_ids"])
    support = set(problem["support_ids"])
    if not target or target & support or len(target) != len(problem["target_ids"]) or len(support) != len(
        problem["support_ids"]
    ):
        raise ValueError("Invalid declared target/support sets")
    known = target | support
    if (target != set(result["target_ids"]) or support != set(result["support_ids"])
            or target != set(verticals["target_ids"]) or support != set(verticals["support_ids"])):
        raise ValueError("Input/solution/vertical inventory mismatch")
    records = verticals["records_by_object"]
    if not known <= records.keys():
        raise ValueError("Missing per-object vertical disposition")
    vertical_owners = {}
    for key, record in records.items():
        required_role = "TARGET" if key in target else "SUPPORT" if key in support else "CONTEXT"
        if record["role"] != required_role:
            raise ValueError("Invalid vertical membership")
        if record["object_id"] != key or (not record["vertical_links"] and not record.get("reason")):
            raise ValueError("Invalid vertical owner or absent omission rationale")
        for link in record["vertical_links"]:
            link_id = link["link_id"]
            if link["ownership"] != key:
                raise ValueError("Foreign vertical record owner")
            if link_id in vertical_owners:
                raise ValueError("Ambiguous vertical link ownership")
            vertical_owners[link_id] = key
    if not result["realizations"]:
        raise ValueError("No complete spatial realizations")
    realization_ids = set()
    projected = []
    for configuration in result["realizations"]:
        realization_id = configuration["realization_id"]
        if realization_id in realization_ids:
            raise ValueError("Duplicate spatial realization ID")
        realization_ids.add(realization_id)
        if (configuration["probability_mode"] != "SET_MEMBERSHIP_ONLY"
                or configuration.get("weights") is not None):
            raise ValueError("Unexpected probability semantics")
        positions = configuration["positions"]
        identities = [row["object_id"] for row in positions]
        if len(identities) != len(known) or set(identities) != known:
            raise ValueError("Incomplete or duplicate per-realization inventory")
        if any(check["hard"] and check["status"] != "SATISFIED" for check in configuration["relation_checks"]):
            raise ValueError("A delivered realization violates a hard condition")
        if set(configuration["active_relation_ids"]) != {row["id"] for row in configuration["relation_checks"]}:
            raise ValueError("Mismatched active relation readback")
        for row in positions:
            key = row["object_id"]
            role = row["role"]
            if row["membership"] != ("TARGET" if key in target else "SUPPORT"):
                raise ValueError("Invalid identity membership")
            candidate = row["xy"]
            if candidate is not None and (not _finite_xy(candidate) or not row["frame"]):
                raise ValueError("Invalid spatial position")
            if candidate is None and not row["reason"]:
                raise ValueError("Missing position requires an explicit disposition")
            if row["scientific_use_mask"] == "CONDITIONAL_LOCAL_MAP_CONTROL" and (
                candidate is None or role != "IDENTIFIED_IN_DECLARED_FRAME"
                or row["epistemic_status"] != "DERIVATION" or not row["source_factor_ids"]
                or row["frame"] != configuration["coordinate_frame"]
            ):
                raise ValueError("Unqualified position promoted to a spatial control")
            expected = {link["link_id"] for link in records[key]["vertical_links"]}
            links = row["vertical_links"]
            if not isinstance(links, list) or len(set(links)) != len(links) or set(links) != expected:
                raise ValueError("Incomplete or cross-object vertical linkage")
            if any(vertical_owners[link] != key for link in links):
                raise ValueError("Foreign column assigned to a borehole")
            projected.append({"realization_id": realization_id, **row})
    return {
        "schema": CONTRACT_VERSION, "target_ids": sorted(target), "support_ids": sorted(support),
        "positions": projected, "realization_count": len(realization_ids),
        "target_role_rows": dict(Counter(row["role"] for row in projected if row["object_id"] in target)),
        "own_vertical_target_count": sum(bool(records[key]["vertical_links"]) for key in target),
        "datum": verticals["absolute_datum"], "preserved_vertical_context_count": len(records) - len(known),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--require-acceptance", action="store_true")
    parser.add_argument("--projection", type=Path)
    args = parser.parse_args()
    result = read_foundation_bundle(args.manifest, require_acceptance=args.require_acceptance)
    positions = result.pop("positions")
    if args.projection:
        with args.projection.open("x", encoding="utf-8", newline="\n") as stream:
            for row in positions:
                stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n")
    print(json.dumps(result | {"position_rows": len(positions)}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
