from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "artifacts" / "model_selection" / "t1_gate_c1_compact_screen_v1"


def test_invalidated_cuda_teardown_run_is_audited_and_never_scored() -> None:
    incidents = json.loads(
        (ARTIFACT / "execution_incident_register.json").read_text(encoding="utf-8")
    )["incidents"]
    protocol = json.loads((ARTIFACT / "protocol_freeze.json").read_text(encoding="utf-8"))
    authority = json.loads(
        (ARTIFACT / "environment" / "execution_authority.json").read_text(encoding="utf-8")
    )
    assert [item["incident_id"] for item in incidents] == [
        "C1-EXEC-001",
        "C1-EXEC-002",
        "C1-EXEC-003",
    ]
    assert incidents[0]["status"] == "MITIGATION_INSUFFICIENT_SUPERSEDED"
    assert incidents[1]["status"] == (
        "RESOLVED_WITH_FAIL_CLOSED_PARENT_VALIDATION_BEFORE_AUTHORITATIVE_SCREEN"
    )
    assert incidents[2]["status"] == "INVALIDATED_BEFORE_LABEL_ACCESS_AND_QUARANTINED"
    assert [len(item["invalidated_shards"]) for item in incidents] == [2, 1, 8]
    assert incidents[2]["invalidated_physical_inner_fits"] == 820
    assert incidents[2]["matched_runtime_benchmark"]["mean_speedup_ratio"] > 1.0
    assert incidents[2]["matched_runtime_benchmark"]["median_speedup_ratio"] > 1.0
    for incident in incidents:
        assert incident["outer_label_scoring_started"] is False
        assert incident["outer_label_access_events"] == 0
        assert incident["historical_validation_loaded"] is False
        assert incident["current_test_loaded"] is False
        assert incident["new_holdout_seen"] is False
        assert incident["invalidated_results_used_for_selection"] is False
    assert incidents[-1]["new_code_sha256"] == protocol["code_sha256"]
    assert incidents[-1]["new_environment_sha256"] == authority["environment_sha256"]


def test_fresh_gate_c1_execution_authority_matches_frozen_protocol() -> None:
    protocol = json.loads((ARTIFACT / "protocol_freeze.json").read_text(encoding="utf-8"))
    authority = json.loads(
        (ARTIFACT / "environment" / "execution_authority.json").read_text(encoding="utf-8")
    )
    manifest = json.loads(
        (ARTIFACT / "environment" / "environment_manifest.json").read_text(encoding="utf-8")
    )
    assert authority["status"] == "PASS"
    assert authority["code_sha256"] == protocol["code_sha256"]
    assert authority["config_sha256"] == protocol["config_sha256"]
    assert manifest["fresh_environment_created"] is True
    assert manifest["runtime_network_allowed"] is False
    assert manifest["external_pretrained_models"] is False
    assert manifest["absolute_paths_persisted"] is False
