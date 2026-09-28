"""Live checks of the RX580 service on CORE (markers gpu + services; skipped → NOT_RUN without ``VKM_RX580_URL``).

Acceptance §66–70 (RX580 part): real GPU inference, 8 GB recognised, both production models loaded AND resident at the
same time, concurrent dense + late requests answered.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import urllib.request

import pytest

URL = os.environ.get("VKM_RX580_URL", "").rstrip("/")
TOKEN = os.environ.get("VKM_RX580_TOKEN", "")

pytestmark = [pytest.mark.gpu, pytest.mark.services,
              pytest.mark.skipif(not URL, reason="VKM_RX580_URL not set (NOT_RUN)")]


def _get(path: str):
    with urllib.request.urlopen(URL + path, timeout=30) as r:
        return json.loads(r.read())


def _post(path: str, body: dict):
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {TOKEN}"} if TOKEN else {})})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def test_both_models_resident_on_8gb_rx580():
    h = _get("/health")
    assert h["status"] == "ok"
    assert h["device"]["vram_total_mib"] == 8192
    roles = {m["role"]: m for m in h["models"]}
    assert set(roles) == {"dense", "late"}
    for m in roles.values():
        assert m["loaded"] and m["resident"] and m["vram_mib"] and m["vram_mib"] > 50


def test_concurrent_dense_and_late_queries():
    texts = [f"оседание земной поверхности {i}" for i in range(8)]
    with concurrent.futures.ThreadPoolExecutor(8) as ex:
        futs = [ex.submit(_post, "/embed/query", {"text": t, "role": r, "include_vectors": False})
                for t in texts for r in ("dense", "late")]
        res = [f.result() for f in futs]
    assert all(("dense" in r) or ("late" in r) for r in res)
    after = _get("/health")
    assert all(m["restarts"] == 0 for m in after["models"])
