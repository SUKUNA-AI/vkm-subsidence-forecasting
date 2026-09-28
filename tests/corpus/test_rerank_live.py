"""Live checks of the deployed rerank gateway (agent F). Marker ``services``; without ``VKM_RERANK_URL`` → skipped
(NOT_RUN). The full 9-point acceptance is ``acceptance.py run`` on EDGE (receipt in edge_receipts/)."""
from __future__ import annotations

import os

import pytest

pytestmark = [pytest.mark.services, pytest.mark.gpu]


@pytest.fixture(scope="module")
def client():
    if not os.environ.get("VKM_RERANK_URL"):
        pytest.skip("NOT_RUN: VKM_RERANK_URL is not set (no live rerank gateway)")
    pytest.importorskip("httpx")
    from vkm_corpus.retrieval.client import SyncRerankClient

    with SyncRerankClient.from_settings() as c:
        yield c


def test_live_health_and_status(client):
    assert client.health().status == "ok"
    st = client.status()
    assert st.backends["text"].status == "ready" and st.backends["visual"].status == "ready"
    assert st.backends["visual"].license == "CC-BY-NC-4.0"


def test_live_text_rerank(client):
    resp = client.rerank_text("оседание земной поверхности по реперам", [
        ("a", "Нивелирование реперов показывает оседание земной поверхности над выработками."),
        ("b", "Флотационное обогащение сильвинита."),
    ])
    assert resp.candidate_ids[0] == "a" and resp.n_tokens_total and resp.n_tokens_total <= 4096
