import hashlib
import json

import pytest

from vkm_world.geometry_build.joint_borehole_handoff import CONTRACT_VERSION, read_foundation_bundle


def write_bundle(root, bad=None):
    files = {}
    data = {
        "problem": {"problem_id": "fixture", "target_ids": ["target"], "support_ids": ["support"]},
        "solution": {
            "problem_id": "fixture", "status": "COMPLETED_DECLARED_DESIGN", "target_ids": ["target"],
            "support_ids": ["support"], "input_hashes": {"problem": "fixture-hash"}, "realizations": [{
                "realization_id": "one", "probability_mode": "SET_MEMBERSHIP_ONLY", "coordinate_frame": "SH1",
                "active_relation_ids": ["r"], "relation_checks": [{"hard": True, "status": "SATISFIED", "id": "r"}],
                "positions": [
                    {"object_id": "target", "membership": "TARGET", "xy": None, "frame": None,
                     "role": "UNDERIDENTIFIED", "reason": "undetermined", "vertical_links": ["L1"],
                     "epistemic_status": "UNKNOWN", "source_factor_ids": [],
                     "scientific_use_mask": "DO_NOT_USE_AS_SPATIAL_CONTROL"},
                    {"object_id": "support", "membership": "SUPPORT", "xy": [0, 1], "frame": "SH1",
                     "role": "PRIOR_DOMINATED", "reason": "source limited", "vertical_links": [],
                     "epistemic_status": "MODEL_CHOICE", "source_factor_ids": [],
                     "scientific_use_mask": "DO_NOT_USE_AS_SPATIAL_CONTROL"},
                ],
            }],
        },
        "vertical": {"target_ids": ["target"], "support_ids": ["support"], "absolute_datum": None,
                     "records_by_object": {
                         "target": {"object_id": "target", "vertical_links": [{"link_id": "L1"}]},
                         "support": {"object_id": "support", "vertical_links": [], "reason": "no column"},
                     }},
    }
    if bad is not None:
        bad(data)
    for name, payload in data.items():
        file = root / (name + ".json")
        content = json.dumps(payload).encode()
        file.write_bytes(content)
        files[name] = {"path": file.name, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    manifest = root / "manifest.json"
    manifest.write_text(json.dumps({"schema": CONTRACT_VERSION, "files": files,
                                    "semantic_problem_sha256": "fixture-hash",
                                    "acceptance": "ACCEPTED_SPATIAL_FOUNDATION_WITH_LIMITS"}))
    return manifest


def test_complete_handoff_keeps_undetermined_position_and_own_vertical(tmp_path):
    result = read_foundation_bundle(write_bundle(tmp_path), require_acceptance=True)
    assert len(result["positions"]) == 2
    assert result["own_vertical_target_count"] == 1
    assert result["positions"][0]["xy"] is None
    assert result["datum"] is None


def test_incomplete_hand_off_is_refused_in_acceptance_mode(tmp_path):
    def make_incomplete(data):
        data["solution"]["status"] = "INCOMPLETE_RESOURCE_LIMIT"

    manifest = write_bundle(tmp_path, make_incomplete)
    assert read_foundation_bundle(manifest)["realization_count"] == 1
    with pytest.raises(ValueError, match="incomplete"):
        read_foundation_bundle(manifest, require_acceptance=True)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda cfg: cfg["positions"].pop(), "Incomplete or duplicate"),
        (lambda cfg: cfg["positions"][0].update(vertical_links=[]), "cross-object vertical"),
        (lambda cfg: cfg["positions"][1].update(xy=[False, 1]), "Invalid spatial"),
        (lambda cfg: cfg["positions"][1].update(scientific_use_mask="CONDITIONAL_LOCAL_MAP_CONTROL"),
         "Unqualified position"),
        (lambda cfg: cfg["relation_checks"][0].update(status="VIOLATED"), "hard condition"),
        (lambda cfg: cfg.update(weights={"one": 1}), "probability semantics"),
    ],
)
def test_bad_bundle_is_rejected(tmp_path, mutation, message):
    manifest = write_bundle(tmp_path, lambda data: mutation(data["solution"]["realizations"][0]))
    with pytest.raises(ValueError, match=message):
        read_foundation_bundle(manifest, require_acceptance=True)


def test_checksum_failure_is_not_hidden_by_json_validity(tmp_path):
    manifest = write_bundle(tmp_path)
    file = tmp_path / "vertical.json"
    file.write_text(file.read_text().replace("column", "object"))
    with pytest.raises(ValueError, match="Checksum"):
        read_foundation_bundle(manifest)
