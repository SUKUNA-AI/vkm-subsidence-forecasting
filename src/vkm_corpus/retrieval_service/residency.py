"""Residency of the encoders on the RX580: one ``llama-server`` child process per model, started together and kept
resident (постановка лаборатории §4, §49; user directive: dense + late together, no reload unless measurements prove
it necessary).

* :class:`ModelProcess` — spawn, health wait, restart with a counter (the ``reload count`` of §4), per-process VRAM/GTT
  and GPU engine time from DRM fdinfo (only readable for own children — hence the service spawns them);
* :class:`ResidencyManager` — starts every slot, monitors (restarts a dead server, never unloads a live one) and runs
  the keepalive: every ``keepalive_s`` seconds each model that served no request in that window gets one tiny request,
  so the GPU never idles into runtime-PM suspend (BACO evicts VRAM; first query after idle ≈ 0.9 s instead of
  ≈ 20 ms). Keepalive calls are counted separately from real traffic.

No lock is shared between models; each model has its own process, client and (optional) admission limit.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from vkm_corpus.embeddings import gpu
from vkm_corpus.embeddings.llama import LlamaError, LlamaServerClient
from vkm_corpus.logs import get_logger
from vkm_corpus.retrieval_service.config import ModelSlot, ServiceConfig

LOG = get_logger("rx580.residency")


def resident_in_vram(loaded: bool, vram: int | None, gtt: int | None, *, expected: int | None = None,
                     baseline: int | None = None) -> bool:
    """The model's weights live in VRAM: not evicted by runtime PM (VRAM falls towards 0) and not spilled to GTT
    (through the 256 MiB BAR heap when GGML_VK_DISABLE_HOST_VISIBLE_VIDMEM is unset, most of the model sits in GTT).

    With a measured expectation (slot config, MODEL_MATRIX) VRAM must reach 80 % of it. Without one: 80 % of the
    post-warm-up baseline and GTT not above VRAM. Host-side buffers (staging, outputs) are GTT as well, so GTT is only
    the spill heuristic, never compared with a fixed small bound."""
    if not loaded:
        return False
    if vram is None:              # no fdinfo (external server): liveness only
        return True
    if vram <= 0:
        return False
    if expected:
        return vram >= 0.8 * expected
    if baseline and vram < 0.8 * baseline:
        return False
    return (gtt or 0) <= vram


@dataclass
class ModelState:
    starts: int = 0
    restarts: int = 0
    last_ok_at: float | None = None
    last_request_at: float = 0.0
    keepalive_sent: int = 0
    keepalive_failed: int = 0
    last_error: str | None = None
    load_s: float | None = None
    requests: int = 0
    vram_baseline: int | None = None     # bytes in VRAM right after the warm-up of the current process


class ModelProcess:
    def __init__(self, slot: ModelSlot, *, llama_server: str, log_dir: Path | None, pooled: bool) -> None:
        self.slot = slot
        self.llama_server = llama_server
        self.log_dir = log_dir
        self.proc: subprocess.Popen | None = None
        self.state = ModelState()
        self.client = LlamaServerClient(slot.endpoint, pooled=pooled)
        self.pooling = "none" if not pooled else None

    def argv(self, pooling: str) -> list[str]:
        s = self.slot
        # --cache-ram 0: no host prompt cache (default 8 GiB; causal Qwen3 encoders filled it with idle-slot KV state)
        return [self.llama_server, "-m", s.gguf, "--embeddings", "--pooling", pooling, "-ngl", "999",
                "-c", str(s.ctx), "-b", str(s.ubatch), "-ub", str(s.ubatch), "-np", str(s.parallel),
                "-t", str(s.threads), "-fa", s.flash_attn, "--cache-ram", "0", "--host", "127.0.0.1",
                "--port", str(s.port), "--no-webui", *s.extra_args]

    def start(self, pooling: str, timeout_s: float = 180.0) -> None:
        log = None
        if self.log_dir:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            log = open(self.log_dir / f"llama-{self.slot.role}-{self.slot.key}.log", "ab")
        t0 = time.perf_counter()
        self.proc = subprocess.Popen(self.argv(pooling), stdout=log or subprocess.DEVNULL, stderr=subprocess.STDOUT,
                                     start_new_session=True)
        self.state.starts += 1
        while time.perf_counter() - t0 < timeout_s:
            if self.proc.poll() is not None:
                raise RuntimeError(f"llama-server {self.slot.key} exited with {self.proc.returncode} during load")
            try:
                if self.client.health().get("http_status") == 200:
                    self.state.load_s = round(time.perf_counter() - t0, 2)
                    self.state.last_ok_at = time.time()
                    LOG.info("model resident", extra={"vkm": {"status": "loaded", "duration_ms":
                                                             int(self.state.load_s * 1000),
                                                             "model": self.slot.key, "role": self.slot.role}})
                    return
            except LlamaError:
                pass
            time.sleep(0.25)
        raise TimeoutError(f"llama-server {self.slot.key} not healthy in {timeout_s}s")

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc is not None else None

    def gpu_usage(self) -> gpu.ProcessGpu | None:
        return gpu.process_gpu(self.pid) if self.pid else None

    def stop(self) -> None:
        self.client.close()
        if self.proc is not None and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait()


@dataclass
class ResidencyManager:
    config: ServiceConfig
    poolings: dict[str, str]                                  # role → llama-server pooling
    keepalive_ids: dict[str, list[int]]                       # role → minimal token ids for the keepalive request
    processes: dict[str, ModelProcess] = field(default_factory=dict)
    _stop: threading.Event = field(default_factory=threading.Event)
    _threads: list[threading.Thread] = field(default_factory=list)

    def build(self) -> None:
        log_dir = Path(self.config.log_dir) if self.config.log_dir else None
        for slot in self.config.models:
            self.processes[slot.role] = ModelProcess(slot, llama_server=self.config.llama_server, log_dir=log_dir,
                                                     pooled=self.poolings[slot.role] != "none")

    def start(self) -> None:
        if not self.processes:
            self.build()
        if self.config.spawn:
            for role, p in self.processes.items():   # load sequentially (VRAM accounting), then serve together
                p.start(self.poolings[role])
        for role in self.processes:
            self._keepalive_once(role, force=True)  # warm-up: pipelines compiled, buffers validated into VRAM
            self._note_baseline(role)
        self._threads = [threading.Thread(target=self._monitor, name="rx580-monitor", daemon=True)]
        if self.config.keepalive_s > 0:
            self._threads.append(threading.Thread(target=self._keepalive, name="rx580-keepalive", daemon=True))
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()
        for p in self.processes.values():
            p.stop()

    def note_request(self, role: str) -> None:
        st = self.processes[role].state
        st.last_request_at = time.monotonic()
        st.requests += 1

    def _note_baseline(self, role: str) -> None:
        usage = self.processes[role].gpu_usage()
        self.processes[role].state.vram_baseline = usage.vram_bytes if usage and usage.vram_bytes else None

    # ------------------------------------------------------------------------------------------------------ loops
    def _keepalive_once(self, role: str, *, force: bool = False) -> None:
        p = self.processes[role]
        idle = time.monotonic() - p.state.last_request_at
        if not force and idle < self.config.keepalive_s:
            return
        try:
            p.client.embed_ids([self.keepalive_ids[role]])
            p.state.keepalive_sent += 1
            p.state.last_ok_at = time.time()
        except LlamaError as exc:
            p.state.keepalive_failed += 1
            p.state.last_error = str(exc)[:200]

    def _keepalive(self) -> None:
        while not self._stop.wait(self.config.keepalive_s / 2):
            for role in self.processes:
                self._keepalive_once(role)

    def _monitor(self) -> None:
        while not self._stop.wait(self.config.monitor_s):
            if not self.config.spawn or not self.config.restart_on_exit:
                continue
            for role, p in self.processes.items():
                if p.proc is not None and not p.alive():
                    LOG.error("llama-server exited, restarting", extra={"vkm": {"status": "restart", "role": role,
                                                                             "rc": p.proc.returncode}})
                    try:
                        p.start(self.poolings[role])
                        p.state.restarts += 1
                        self._keepalive_once(role, force=True)
                        self._note_baseline(role)
                    except Exception as exc:  # recorded; /health shows the model as not loaded
                        p.state.last_error = f"restart failed: {exc}"[:200]

    # ----------------------------------------------------------------------------------------------------- health
    def health(self, expected_vram: Callable[[str], int | None] | None = None) -> dict[str, Any]:
        dev = gpu.device_memory(light=True)
        models = []
        for role, p in self.processes.items():
            usage = p.gpu_usage()
            try:
                loaded = p.client.health().get("http_status") == 200
            except LlamaError:
                loaded = False
            vram = usage.vram_bytes if usage else None
            gtt = usage.gtt_bytes if usage else None
            exp = expected_vram(role) if expected_vram else None
            if exp is None and p.slot.expected_vram_mib:
                exp = int(p.slot.expected_vram_mib * 2 ** 20)
            resident = resident_in_vram(loaded, vram, gtt, expected=exp, baseline=p.state.vram_baseline)
            models.append({
                "role": role, "key": p.slot.key, "quant": p.slot.quant, "loaded": loaded, "resident": resident,
                "vram_mib": None if vram is None else round(vram / 2 ** 20, 1),
                "gtt_mib": None if usage is None else round(usage.gtt_bytes / 2 ** 20, 1),
                "pid": p.pid, "starts": p.state.starts, "restarts": p.state.restarts, "load_s": p.state.load_s,
                "requests": p.state.requests, "keepalive_sent": p.state.keepalive_sent,
                "keepalive_failed": p.state.keepalive_failed, "last_error": p.state.last_error})
        return {"device": {"vram_total_mib": None if dev.vram_total is None else round(dev.vram_total / 2 ** 20),
                           "vram_used_mib": None if dev.vram_used is None else round(dev.vram_used / 2 ** 20, 1),
                           "gtt_used_mib": None if dev.gtt_used is None else round(dev.gtt_used / 2 ** 20, 1),
                           "runtime_status": dev.runtime_status, "power_control": dev.power_control},
                "models": models, "keepalive_s": self.config.keepalive_s}
