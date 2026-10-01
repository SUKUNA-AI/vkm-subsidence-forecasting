"""Operational status must not inspect corpus data while admission is closed."""
from fastapi.testclient import TestClient
import pytest

from vkm_corpus.api.app import ApiConfig, create_app
from vkm_corpus.api.fixtures import synthetic_service
from vkm_corpus.update.barrier import ReceiverBarrier


@pytest.mark.parametrize("closed_by", ["generation", "drain"])
def test_closed_receiver_status_exposes_operations_without_reading_corpus(tmp_path, monkeypatch, closed_by):
    service, *_ = synthetic_service(tmp_path / "canon")
    service.deps.generation_guard = lambda: {"status": "UNAVAILABLE" if closed_by == "generation" else "READY"}
    service.deps.admission_barrier = ReceiverBarrier("synthetic-status")
    if closed_by == "drain":
        service.deps.admission_barrier.pause("synthetic-switch")
    reads = []
    original = service.canon.corpus_counts

    def counted():
        reads.append("corpus_counts")
        return original()

    monkeypatch.setattr(service.canon, "corpus_counts", counted)
    with TestClient(create_app(service, ApiConfig(read_tokens={"read": "reader"}))) as client:
        response = client.get("/v1/status", headers={"Authorization": "Bearer read"})
    assert response.status_code == 200
    assert response.json()["status"]["production_controls"]["generation"]["status"] in {"READY", "UNAVAILABLE"}
    assert reads == [], "status bypass read corpus counts while generation/drain admission was closed"
    assert "counts" not in response.json()["status"].get("canonical", {})
