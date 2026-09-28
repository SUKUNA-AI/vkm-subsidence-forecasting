"""Engineering measurements of the lab (task §23): timers, memory, throughput and index size accounting."""
from __future__ import annotations

import os
import statistics
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from vkm_corpus.retrieval_lab.vectors import bytes_per_vector


@dataclass
class Timer:
    samples_ms: list[float] = field(default_factory=list)

    @contextmanager
    def measure(self) -> Iterator[None]:
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.samples_ms.append((time.perf_counter() - t0) * 1000)

    def summary(self) -> dict[str, float]:
        s = sorted(self.samples_ms)
        if not s:
            return {"n": 0}
        return {"n": len(s), "mean_ms": statistics.fmean(s), "p50_ms": s[len(s) // 2],
                "p95_ms": s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))], "max_ms": s[-1]}


def peak_rss_mib() -> float:
    """Peak resident set size of this process (Linux: KiB in ru_maxrss); NaN where ``resource`` is missing."""
    try:
        import resource
    except ImportError:
        return float("nan")
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def gpu_memory(torch: Any = None) -> dict[str, float]:
    if torch is None:
        try:
            import torch  # noqa: F811
        except ImportError:
            return {}
    if not torch.cuda.is_available():
        return {}
    return {"allocated_mib": torch.cuda.memory_allocated() / 2 ** 20,
            "peak_allocated_mib": torch.cuda.max_memory_allocated() / 2 ** 20,
            "reserved_mib": torch.cuda.memory_reserved() / 2 ** 20}


def dir_size(path: str | Path) -> int:
    p = Path(path)
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.is_dir() else 0


def dense_index_size(n: int, dim: int, precision: str) -> dict[str, float]:
    b = n * bytes_per_vector(dim, precision)
    return {"n": n, "dim": dim, "precision": precision, "bytes": b, "mib": b / 2 ** 20}


def throughput(n_items: int, seconds: float) -> float:
    return n_items / seconds if seconds > 0 else float("nan")


def environment() -> dict[str, Any]:
    info: dict[str, Any] = {"cpu_count": os.cpu_count()}
    try:
        import torch

        info["torch"] = torch.__version__
        info["cuda_available"] = torch.cuda.is_available()
        info["torch_threads"] = torch.get_num_threads()
        if torch.cuda.is_available():
            info["gpu"] = torch.cuda.get_device_name(0)
    except ImportError:
        pass
    for mod in ("transformers", "sentence_transformers", "pylate", "numpy"):
        try:
            info[mod] = __import__(mod).__version__
        except Exception:  # noqa: BLE001
            info[mod] = None
    return info
