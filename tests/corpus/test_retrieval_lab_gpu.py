"""GPU policy of the retrieval lab (CP-26): the RTX is used only with an empty OCR queue and enough free VRAM."""
from __future__ import annotations

from vkm_corpus.retrieval_lab import gpu
from vkm_corpus.retrieval_lab.gpu import GpuState, parse_nvidia_smi, parse_vllm_metrics

METRICS = """# HELP vllm:num_requests_running Number of requests in model execution batches.
vllm:num_requests_running{engine="0",model_name="glm-ocr"} 16.0
vllm:num_requests_waiting{engine="0",model_name="glm-ocr"} 0.0
vllm:num_requests_waiting_by_reason{engine="0",model_name="glm-ocr",reason="capacity"} 0.0
"""


def test_parsers():
    assert parse_vllm_metrics(METRICS) == (16.0, 0.0)
    assert parse_vllm_metrics("nothing") == (None, None)
    assert parse_nvidia_smi("15840, 16303\n") == (463, 16303)
    assert parse_nvidia_smi("") == (None, None)


def test_policy_requires_idle_ocr_and_free_memory():
    assert not GpuState(16.0, 0.0, 8000, 16303).allows(2048), "OCR running"
    assert not GpuState(0.0, 3.0, 8000, 16303).allows(2048), "OCR waiting"
    assert not GpuState(0.0, 0.0, 2500, 16303).allows(2048), "free memory below need + margin"
    assert not GpuState(None, None, 16000, 16303).allows(2048), "unknown OCR state is not idle"
    assert not GpuState(0.0, 0.0, 5000, 16303).allows(2048), "coordinator rule: at least 6 GB free"
    assert GpuState(0.0, 0.0, 7000, 16303).allows(2048)


def test_idle_window_needs_every_probe_idle(monkeypatch):
    states = iter([GpuState(0.0, 0.0, 8000, 16303)] * 3 + [GpuState(4.0, 0.0, 8000, 16303)])
    monkeypatch.setattr(gpu, "probe", lambda url=None: next(states))
    ok, samples = gpu.idle_window(2048, minutes=2, poll_s=30, sleep=lambda s: None)
    assert not ok and len(samples) == 4
    monkeypatch.setattr(gpu, "probe", lambda url=None: GpuState(0.0, 0.0, 8000, 16303))
    ok, samples = gpu.idle_window(2048, minutes=1, poll_s=30, sleep=lambda s: None)
    assert ok and len(samples) == 3
