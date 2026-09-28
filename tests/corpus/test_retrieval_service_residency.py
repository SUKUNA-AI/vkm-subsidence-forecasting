"""Residency manager with real child processes (a fake llama-server): both models start and stay resident, the
keepalive fires only when a model is idle, a crashed server is restarted and counted (reload count). POSIX only."""
from __future__ import annotations

import os
import socket
import stat
import sys
import time
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
if os.name != "posix":
    pytest.skip("process groups / killpg are POSIX-only", allow_module_level=True)

import vkm_corpus  # noqa: E402
from vkm_corpus.embeddings.llama import LlamaError  # noqa: E402
from vkm_corpus.retrieval_service.config import ModelSlot, ServiceConfig  # noqa: E402
from vkm_corpus.retrieval_service.residency import ResidencyManager  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wrapper(tmp_path: Path) -> str:
    src = Path(vkm_corpus.__file__).resolve().parents[1]
    w = tmp_path / "fake-llama-server"
    w.write_text(f"#!/bin/sh\nPYTHONPATH='{src}' exec '{sys.executable}' -m vkm_corpus.embeddings.fake_server \"$@\"\n")
    w.chmod(w.stat().st_mode | stat.S_IEXEC)
    return str(w)


def _wait(cond, timeout=10.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_two_resident_models_keepalive_and_restart(tmp_path):
    slots = (ModelSlot(role="dense", key="granite-311m-r2", gguf="d.gguf", port=_free_port(),
                       extra_args=("--dim", "8")),
             ModelSlot(role="late", key="mlateon", gguf="l.gguf", port=_free_port(),
                       extra_args=("--dim", "4", "--die-after", "3")))
    cfg = ServiceConfig(models=slots, llama_server=_wrapper(tmp_path), keepalive_s=0.3, monitor_s=0.2,
                        log_dir=str(tmp_path / "logs"))
    mgr = ResidencyManager(cfg, poolings={"dense": "cls", "late": "none"},
                           keepalive_ids={"dense": [1, 2], "late": [1, 2, 3]})
    mgr.start()
    try:
        h = mgr.health()
        assert {m["role"]: m["loaded"] for m in h["models"]} == {"dense": True, "late": True}
        assert all(m["starts"] == 1 and m["pid"] for m in h["models"])
        dense = mgr.processes["dense"]
        v = dense.client.embed_ids([[5, 6, 7]]).vectors[0]
        assert v.shape == (8,)
        # keepalive: an idle model gets tiny requests; a busy model does not
        base = dense.state.keepalive_sent
        assert _wait(lambda: dense.state.keepalive_sent >= base + 2, 5.0)
        # the late server dies after 3 requests (warm-up + keepalives); the monitor restarts it and counts it
        late = mgr.processes["late"]
        assert _wait(lambda: late.state.restarts >= 1, 15.0)
        assert _wait(lambda: late.alive(), 10.0)

        def _served() -> bool:          # the fake server keeps dying after 3 requests (warm-up and keepalives count)
            try:
                return late.client.embed_ids([[1, 2]]).vectors[0].shape == (2, 4)
            except LlamaError:
                return False

        assert _wait(_served, 10.0)
        assert mgr.health()["models"][1]["restarts"] >= 1
    finally:
        mgr.stop()
    assert not mgr.processes["dense"].alive() and not mgr.processes["late"].alive()


def test_resident_means_weights_in_vram(monkeypatch):
    """Evicted (runtime PM) or spilled-to-GTT models are loaded but not resident."""
    from vkm_corpus.embeddings import gpu
    from vkm_corpus.retrieval_service import residency as res_mod

    slot = ModelSlot(role="dense", key="granite-311m-r2", gguf="d.gguf", port=1)
    cfg = ServiceConfig(models=(slot,), keepalive_s=0)
    mgr = ResidencyManager(cfg, poolings={"dense": "cls"}, keepalive_ids={"dense": [1]})
    mgr.build()
    proc = mgr.processes["dense"]
    monkeypatch.setattr(proc.client, "health", lambda: {"http_status": 200})
    MiB = 2 ** 20
    cases = [(170 * MiB, 40 * MiB, True),      # in VRAM, host-side staging only
             (56 * MiB, 153 * MiB, False),     # weights spilled to GTT through the BAR heap
             (0, 900 * MiB, False)]            # evicted by runtime PM
    for vram, gtt, expected in cases:
        monkeypatch.setattr(proc, "gpu_usage", lambda v=vram, g=gtt: gpu.ProcessGpu(123, v, g, {}, 1))
        monkeypatch.setattr(res_mod.gpu, "device_memory", lambda *a, **k: gpu.DeviceMemory(
            8 * 1024 * MiB, None, None, None, None, "active", "auto", None, None, None))
        assert mgr.health()["models"][0]["resident"] is expected


def test_resident_criterion_expectation_and_baseline():
    from vkm_corpus.retrieval_service.residency import resident_in_vram

    MiB = 2 ** 20
    # a measured expectation makes large host buffers (e.g. a logits output buffer in pinned GTT) irrelevant
    assert resident_in_vram(True, 172 * MiB, 2103 * MiB, expected=160 * MiB)
    assert not resident_in_vram(True, 56 * MiB, 10 * MiB, expected=160 * MiB)       # spilled / partly evicted
    # without an expectation: the post-warm-up baseline and the GTT ≤ VRAM spill heuristic
    assert not resident_in_vram(True, 60 * MiB, 10 * MiB, baseline=170 * MiB)
    assert resident_in_vram(True, 171 * MiB, 60 * MiB, baseline=170 * MiB)
    assert not resident_in_vram(False, 171 * MiB, 0)
    assert resident_in_vram(True, None, None)                                          # external server: liveness
