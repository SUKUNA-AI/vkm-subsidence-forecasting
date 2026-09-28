"""GPU policy of the lab on the WORKSTATION RTX 5070 Ti (CP-26).

GLM-OCR (vLLM) has priority and is never stopped, restarted or unloaded. Coordinator rule (28.09): an RTX window
exists only when the OCR server has had no running or waiting requests for at least 5 minutes AND ``nvidia-smi``
reports at least 6 GB free (and at least the model need + margin) — WDDM would otherwise page vLLM memory to system
RAM, which is as unacceptable as an OOM. The run stops as soon as OCR traffic reappears (checked between batches), and
every RTX run records the OCR metrics at start and end. Otherwise the lab runs on CPU (fp32, reference precision).
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass

DEFAULT_METRICS_URL = "http://127.0.0.1:8080/metrics"
MARGIN_MIB = 1024
MIN_FREE_MIB = 6144                 # coordinator rule: ≥ 6 GB free VRAM
IDLE_MINUTES = 5.0                  # coordinator rule: OCR idle for ≥ 5 min
_METRIC = re.compile(r"^(vllm:num_requests_(?:running|waiting))\{[^}]*\}\s+([0-9.eE+-]+)\s*$", re.MULTILINE)


@dataclass
class GpuState:
    ocr_running: float | None
    ocr_waiting: float | None
    free_mib: int | None
    total_mib: int | None
    reason: str = ""

    def allows(self, need_mib: int, margin_mib: int = MARGIN_MIB) -> bool:
        if self.ocr_running is None or self.ocr_waiting is None or self.free_mib is None:
            return False
        return (self.ocr_running == 0 and self.ocr_waiting == 0
                and self.free_mib >= max(need_mib + margin_mib, MIN_FREE_MIB))

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def parse_vllm_metrics(text: str) -> tuple[float | None, float | None]:
    vals: dict[str, float] = {}
    for name, value in _METRIC.findall(text):
        vals[name] = vals.get(name, 0.0) + float(value)
    return vals.get("vllm:num_requests_running"), vals.get("vllm:num_requests_waiting")


def parse_nvidia_smi(text: str) -> tuple[int | None, int | None]:
    """``memory.used, memory.total`` (MiB, csv noheader nounits) → (free, total) of the first GPU."""
    for line in text.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            used, total = int(parts[0]), int(parts[1])
            return total - used, total
    return None, None


def probe(metrics_url: str = DEFAULT_METRICS_URL, timeout: float = 5.0) -> GpuState:
    running = waiting = None
    reason = []
    try:
        import httpx

        resp = httpx.get(metrics_url, timeout=timeout, trust_env=False)
        running, waiting = parse_vllm_metrics(resp.text)
        if running is None:
            reason.append("vLLM metrics without num_requests_*")
    except Exception as exc:  # noqa: BLE001 - OCR server down or unreachable: GPU is not considered free
        reason.append(f"vLLM metrics unavailable ({type(exc).__name__})")
    free = total = None
    smi = shutil.which("nvidia-smi")
    if smi:
        try:
            out = subprocess.run([smi, "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=timeout, check=True).stdout
            free, total = parse_nvidia_smi(out)
        except Exception as exc:  # noqa: BLE001
            reason.append(f"nvidia-smi failed ({type(exc).__name__})")
    else:
        reason.append("nvidia-smi not found")
    return GpuState(running, waiting, free, total, "; ".join(reason))


def idle_window(need_mib: int, *, minutes: float = IDLE_MINUTES, poll_s: float = 30.0,
                metrics_url: str = DEFAULT_METRICS_URL, sleep=None) -> tuple[bool, list[GpuState]]:
    """Poll for ``minutes``: the window is open only if every probe allows the model (OCR idle, VRAM free)."""
    import time

    sleep = sleep or time.sleep
    samples: list[GpuState] = []
    n = max(1, int(minutes * 60 / poll_s) + 1)
    for i in range(n):
        state = probe(metrics_url)
        samples.append(state)
        if not state.allows(need_mib):
            return False, samples
        if i + 1 < n:
            sleep(poll_s)
    return True, samples


def choose_device(need_mib: int, *, prefer: str = "auto", metrics_url: str = DEFAULT_METRICS_URL,
                  minutes: float = IDLE_MINUTES) -> tuple[str, list[GpuState]]:
    """``prefer``: ``cpu`` | ``auto`` (RTX only inside an idle window). Returns (device, probes)."""
    if prefer == "cpu":
        return "cpu", [probe(metrics_url)]
    ok, samples = idle_window(need_mib, minutes=minutes, metrics_url=metrics_url)
    return ("cuda" if ok else "cpu"), samples
