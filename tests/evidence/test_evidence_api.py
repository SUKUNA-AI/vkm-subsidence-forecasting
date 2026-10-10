from datetime import datetime, timezone

import pytest

from fastapi.testclient import TestClient
from vkm_corpus.api.app import ApiConfig, create_app
from vkm_corpus.api.fixtures import synthetic_service
from vkm_corpus.contracts.access import AccessContext, ResourcePolicy
from vkm_corpus.contracts.policy_store import SourcePolicyStore
from vkm_evidence.contracts import Entity, EvidenceBatch
from vkm_evidence.journal import EvidenceJournal, ZERO
from vkm_evidence.query import EvidenceReader


def test_evidence_api_context_cursor_and_source_policy_recheck(tmp_path):
    service, *_ = synthetic_service(tmp_path / "canon")
    ctx = AccessContext(principal="owner", execution="CLOUD", granted_classes={"PUBLIC"})
    journal = EvidenceJournal(tmp_path / "journal")
    policy = ResourcePolicy(access_class="PUBLIC", policy_version="p1", authority="owner")
    journal.publish("seed", ZERO, EvidenceBatch(records=tuple(Entity(record_id=f"entity-{i}",
        recorded_at=datetime(2026, 10, 1, tzinfo=timezone.utc), actor="owner", policy=policy,
        entity_type="MINE", label="synthetic") for i in range(3))), ctx)
    service.deps.evidence = EvidenceReader(journal)
    config = ApiConfig(read_tokens={"read": "read"}, access_contexts={"read": ctx})
    client = TestClient(create_app(service, config))
    headers = {"Authorization": "Bearer read"}
    assert client.get("/v1/evidence").status_code == 401
    first = client.get("/v1/evidence?limit=1", headers=headers)
    assert first.status_code == 200, first.text
    record = first.json()["item"]["record"]
    assert record["has_more"] and record["total_permitted"] == 3
    second = client.get("/v1/evidence", params={"cursor": record["next_cursor"]}, headers=headers)
    assert second.status_code == 200 and len(second.json()["item"]["record"]["items"]) == 2
    config.access_contexts["read"] = ctx.model_copy(update={"granted_classes": frozenset()})
    stale = client.get("/v1/evidence", params={"cursor": record["next_cursor"]}, headers=headers)
    assert stale.status_code == 400
    denied = client.get("/v1/evidence/records/entity-0", headers=headers)
    missing = client.get("/v1/evidence/records/nonexistent", headers=headers)
    assert denied.status_code == missing.status_code == 404


def test_legacy_access_policy_checked_before_query_and_every_request(tmp_path):
    import json
    service, *_ = synthetic_service(tmp_path / "canon")
    ctx = AccessContext(principal="cloud", execution="CLOUD", granted_classes={"PUBLIC"})
    path = tmp_path / "policies.json"
    policy = ResourcePolicy(access_class="PUBLIC", policy_version="p1", authority="owner")
    payload = {"schema": "vkm-source-policy/1", "policies": {"VKM-SRC-001": policy.model_dump(mode="json")}}
    path.write_text(json.dumps(payload), encoding="utf-8")
    service.deps.access_policy = SourcePolicyStore(path, lambda: ("VKM-SRC-001",))
    client = TestClient(create_app(service, ApiConfig(read_tokens={"read": "read"}, access_contexts={"read": ctx})))
    headers = {"Authorization": "Bearer read"}
    assert client.get("/v1/source/VKM-SRC-001", headers=headers).status_code == 200
    payload["policies"]["VKM-SRC-001"]["access_class"] = "PRIVATE_LOCAL_ONLY"
    path.write_text(json.dumps(payload), encoding="utf-8")
    for route in ("/v1/source/VKM-SRC-001", "/v1/status", "/v1/nav/table/TBL-0000000000000000"):
        assert client.get(route, headers=headers).status_code == 403
    assert client.get("/v1/health").status_code == 200


def test_generation_gate_fails_closed_before_objects(tmp_path):
    service, *_ = synthetic_service(tmp_path / "canon")
    service.deps.generation_guard = lambda: {"status": "UNAVAILABLE"}
    client = TestClient(create_app(service, ApiConfig(read_tokens={"read": "read"})))
    response = client.get("/v1/source/VKM-SRC-001", headers={"Authorization": "Bearer read"})
    assert response.status_code == 503
    assert "served generation" in response.text
