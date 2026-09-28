"""AMD GPU facts from sysfs and DRM fdinfo (standard library only; Linux, amdgpu).

* device-wide: VRAM total/used, GTT used, ``gpu_busy_percent``, runtime-PM status (``power/runtime_status``) — the
  RX580 on CORE is suspended to BACO after 5 s idle (``power/control = auto``) and then its VRAM buffers are evicted to
  system memory, so the first request after idle pays the resume (see the concurrency report);
* per process: ``drm-memory-vram``/``drm-memory-gtt`` and ``drm-engine-compute``/``drm-engine-gfx`` busy nanoseconds
  from ``/proc/<pid>/fdinfo`` (readable for own child processes). The engine counters give per-process GPU time, the
  memory counters give per-model resident VRAM when every model is its own ``llama-server`` process.

Paths are discovered from the PCI id of the render node; nothing here writes to sysfs.
"""
from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

DRM_ROOT = Path("/sys/class/drm")


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="ascii").strip()
    except OSError:
        return None


def _int(path: Path) -> int | None:
    v = _read(path)
    try:
        return int(v) if v is not None else None
    except ValueError:
        return None


def find_amdgpu_device(pci_id: str | None = None) -> Path | None:
    """sysfs ``device`` dir of the first amdgpu card (or of ``pci_id`` like ``0000:08:00.0``)."""
    for card in sorted(DRM_ROOT.glob("card[0-9]*")):
        dev = card / "device"
        if not (dev / "mem_info_vram_total").exists():
            continue
        if pci_id is None or os.path.basename(os.path.realpath(dev)) == pci_id:
            return dev
    return None


def runtime_status(dev: Path | None = None) -> str | None:
    """``power/runtime_status`` only (a PM-core attribute; reading it does not wake a suspended GPU)."""
    dev = dev or find_amdgpu_device()
    return _read(dev / "power/runtime_status") if dev else None


@dataclass(frozen=True)
class DeviceMemory:
    vram_total: int | None
    vram_used: int | None
    vis_vram_used: int | None
    gtt_used: int | None
    gpu_busy_percent: int | None
    runtime_status: str | None       # active | suspended | ...
    power_control: str | None        # auto | on
    sclk_mhz: int | None
    mclk_mhz: int | None
    power_w: float | None

    def as_dict(self) -> dict:
        return asdict(self)


def _current_level(path: Path) -> int | None:
    txt = _read(path)
    if not txt:
        return None
    for line in txt.splitlines():
        if line.rstrip().endswith("*"):
            digits = "".join(ch for ch in line.split(":", 1)[-1] if ch.isdigit())
            return int(digits) if digits else None
    return None


def device_memory(dev: Path | None = None, *, light: bool = False) -> DeviceMemory:
    """Device counters. ``light`` reads only the memory-manager counters and the PM-core status (safe to poll while
    the GPU may be runtime-suspended); the full read adds busy percent, clocks and power from the SMU."""
    dev = dev or find_amdgpu_device()
    if dev is None:
        return DeviceMemory(None, None, None, None, None, None, None, None, None, None)
    power = None
    if not light:
        for hw in sorted((dev / "hwmon").glob("hwmon*")):
            uw = _int(hw / "power1_average") or _int(hw / "power1_input")
            if uw is not None:
                power = round(uw / 1e6, 2)
                break
    return DeviceMemory(
        vram_total=_int(dev / "mem_info_vram_total"), vram_used=_int(dev / "mem_info_vram_used"),
        vis_vram_used=_int(dev / "mem_info_vis_vram_used"), gtt_used=_int(dev / "mem_info_gtt_used"),
        gpu_busy_percent=None if light else _int(dev / "gpu_busy_percent"),
        runtime_status=_read(dev / "power/runtime_status"), power_control=_read(dev / "power/control"),
        sclk_mhz=None if light else _current_level(dev / "pp_dpm_sclk"),
        mclk_mhz=None if light else _current_level(dev / "pp_dpm_mclk"), power_w=power)


@dataclass(frozen=True)
class ProcessGpu:
    pid: int
    vram_bytes: int
    gtt_bytes: int
    engine_ns: dict[str, int]
    clients: int

    def as_dict(self) -> dict:
        return asdict(self)


def _kib(value: str) -> int:
    parts = value.split()
    n = int(parts[0])
    unit = parts[1] if len(parts) > 1 else "KiB"
    return n * {"B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3}.get(unit, 1024)


def process_gpu(pid: int) -> ProcessGpu | None:
    """Sum over the distinct DRM clients of ``pid`` (fds of one client are counted once)."""
    base = Path(f"/proc/{pid}/fdinfo")
    try:
        fds = os.listdir(base)
    except OSError:
        return None
    seen: dict[str, dict[str, str]] = {}
    for fd in fds:
        txt = _read(base / fd)
        if not txt or "drm-driver:" not in txt:
            continue
        kv = {}
        for line in txt.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                kv[k.strip()] = v.strip()
        cid = kv.get("drm-client-id")
        if cid and cid not in seen:
            seen[cid] = kv
    if not seen:
        return None
    vram = gtt = 0
    engines: dict[str, int] = {}
    for kv in seen.values():
        vram += _kib(kv.get("drm-memory-vram", "0 KiB"))
        gtt += _kib(kv.get("drm-memory-gtt", "0 KiB"))
        for k, v in kv.items():
            if k.startswith("drm-engine-") and v.endswith("ns"):
                engines[k[len("drm-engine-"):]] = engines.get(k[len("drm-engine-"):], 0) + int(v.split()[0])
    return ProcessGpu(pid, vram, gtt, engines, len(seen))


def busy_sampler(dev: Path | None, stop, interval_s: float = 0.05) -> list[tuple[float, int | None]]:
    """Sample ``gpu_busy_percent`` until ``stop()`` is true (for GPU utilisation during a benchmark phase)."""
    dev = dev or find_amdgpu_device()
    out: list[tuple[float, int | None]] = []
    while not stop():
        out.append((time.perf_counter(), _int(dev / "gpu_busy_percent") if dev else None))
        time.sleep(interval_s)
    return out


def engine_utilisation(before: ProcessGpu | None, after: ProcessGpu | None, seconds: float,
                       engines: Iterable[str] = ("compute", "gfx")) -> float | None:
    """Fraction of wall time the process kept the GPU engines busy between two fdinfo snapshots."""
    if before is None or after is None or seconds <= 0:
        return None
    busy = sum(after.engine_ns.get(e, 0) - before.engine_ns.get(e, 0) for e in engines)
    return busy / (seconds * 1e9)
