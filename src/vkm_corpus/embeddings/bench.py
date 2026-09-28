"""RX580 measurement harness (model matrix §26, residency §27, concurrency §29, parity §30). Runs on CORE next to the
GPU (standard library + numpy): starts pinned ``llama-server`` processes as children, feeds the reference token ids,
measures and writes one JSON result per run.

Commands::

    python -m vkm_corpus.embeddings.bench model  --key K --gguf F --ref DIR --out R.json [--quant Q]
    python -m vkm_corpus.embeddings.bench pair   --dense K:F --late K:F --ref-dense DIR --ref-late DIR --out R.json

``model`` = load, warm-up, VRAM (device + per process from DRM fdinfo), parity against the reference, query latency
p50/p95 (sequential), document throughput, cold-after-idle latency (runtime PM). ``pair`` = residency MODE A (dense
alone), B (late alone), C (both resident, alternating requests) and D (both resident, concurrent clients on both):
idle/peak VRAM, per-model VRAM, p50/p95 per model, throughput, OOM/errors, reload count (process restarts), GPU busy,
host RSS and CPU time of the servers. No lock is shared between the two servers or their clients.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import statistics
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from vkm_corpus.embeddings import gpu, parity
from vkm_corpus.embeddings import postprocess as pp
from vkm_corpus.embeddings.llama import LlamaError, LlamaServerClient
from vkm_corpus.embeddings.specs import EncoderSpec, get

LLAMA_BIN = os.environ.get("VKM_LLAMA_SERVER", "/opt/llama/bin/llama-server")
# Weights and compute buffers in plain device-local VRAM: with the default (ReBAR-first) placement a second model on a
# GPU with a 256 MiB BAR spills to GTT (system RAM over PCIe) — measured on the RX580 (BACKEND_RESEARCH §2.1).
VK_ENV = {"GGML_VK_DISABLE_HOST_VISIBLE_VIDMEM": os.environ.get("GGML_VK_DISABLE_HOST_VISIBLE_VIDMEM", "1")}


# ------------------------------------------------------------------------------------------------------------ servers
@dataclass
class ServerConfig:
    key: str
    gguf: str
    port: int
    ctx: int = 4096
    batch: int = 2048
    ubatch: int = 2048
    parallel: int = 4
    threads: int = 4
    flash_attn: str = "auto"
    extra: list[str] = field(default_factory=list)

    def pooling(self, spec: EncoderSpec) -> str:
        return "none" if (spec.pooling == "none" or spec.colbert is not None) else spec.pooling

    def argv(self, spec: EncoderSpec) -> list[str]:
        # --cache-ram 0: the server's host prompt cache (default 8 GiB) stores idle slots' KV state on every new task;
        # causal Qwen3 encoders filled it to ≈ 8.2 GiB RSS per process (measured), useless for embeddings
        return [LLAMA_BIN, "-m", self.gguf, "--embeddings", "--pooling", self.pooling(spec), "-ngl", "999",
                "-c", str(self.ctx), "-b", str(self.batch), "-ub", str(self.ubatch), "-np", str(self.parallel),
                "-t", str(self.threads), "-fa", self.flash_attn, "--cache-ram", "0", "--host", "127.0.0.1",
                "--port", str(self.port), "--no-webui", *self.extra]


def binary_info(path: str = LLAMA_BIN) -> dict[str, Any]:
    """Provenance of the llama-server binary used by a run (path + sha256)."""
    try:
        from vkm_corpus.embeddings.tokenize import file_sha256

        return {"path": path, "sha256": file_sha256(path)}
    except OSError:
        return {"path": path, "sha256": None}


class Server:
    def __init__(self, cfg: ServerConfig, log_dir: Path, label: str | None = None) -> None:
        self.cfg = cfg
        self.label = label or cfg.key          # dictionary key in results (two servers may serve one model)
        self.spec = get(cfg.key)
        self.log_path = log_dir / f"server_{cfg.key}_{cfg.port}.log"
        self.proc: subprocess.Popen | None = None
        self.starts = 0
        self.load_s: float | None = None
        self.client = LlamaServerClient(f"http://127.0.0.1:{cfg.port}", pooled=cfg.pooling(self.spec) != "none")

    def start(self, timeout_s: float = 180.0) -> None:
        import socket

        with socket.socket() as sock:   # never talk to a stale server of another run on the same port
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)   # TIME_WAIT is fine, a listener is not
            try:
                sock.bind(("127.0.0.1", self.cfg.port))
            except OSError as exc:
                raise RuntimeError(f"port {self.cfg.port} is busy (stale llama-server?)") from exc
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.log_path, "ab")
        t0 = time.perf_counter()
        env = {**os.environ, **{k: v for k, v in VK_ENV.items() if v}}
        self.proc = subprocess.Popen(self.cfg.argv(self.spec), stdout=fh, stderr=subprocess.STDOUT,
                                     start_new_session=True, env=env)
        self.starts += 1
        while time.perf_counter() - t0 < timeout_s:
            if self.proc.poll() is not None:
                raise RuntimeError(f"llama-server {self.cfg.key} exited rc={self.proc.returncode}; see {self.log_path}")
            try:
                if self.client.health().get("http_status") == 200:
                    self.load_s = time.perf_counter() - t0
                    served = str(self.client.props().get("model_path", ""))
                    if served and Path(served).name != Path(self.cfg.gguf).name:
                        raise RuntimeError(f"port {self.cfg.port} serves {served}, not {self.cfg.gguf}")
                    return
            except LlamaError:
                pass
            time.sleep(0.2)
        raise TimeoutError(f"llama-server {self.cfg.key} not healthy after {timeout_s}s")

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        self.client.close()
        if self.proc and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait()

    def host_usage(self) -> dict[str, Any]:
        """RSS (total and anon/file/shmem split, MiB) and cumulative CPU seconds of the server process (from /proc)."""
        if not self.pid:
            return {}
        try:
            stat = Path(f"/proc/{self.pid}/stat").read_text().rsplit(")", 1)[1].split()
            tick = os.sysconf("SC_CLK_TCK")
            rss_pages = int(Path(f"/proc/{self.pid}/statm").read_text().split()[1])
            out: dict[str, Any] = {"rss_mb": round(rss_pages * os.sysconf("SC_PAGE_SIZE") / 2 ** 20, 1),
                                   "cpu_s": round((int(stat[11]) + int(stat[12])) / tick, 2)}
        except (OSError, IndexError, ValueError):
            return {}
        try:
            for line in Path(f"/proc/{self.pid}/status").read_text().splitlines():
                name, _, val = line.partition(":")
                if name in ("RssAnon", "RssFile", "RssShmem", "VmSwap"):
                    out[f"{name.lower()}_mb"] = round(int(val.split()[0]) / 1024, 1)   # kB → MiB
        except (OSError, IndexError, ValueError):
            pass
        return out

    def buffers_from_log(self) -> dict[str, float]:
        """Buffer sizes llama.cpp reports at load (MiB)."""
        out: dict[str, float] = {}
        try:
            text = self.log_path.read_text(errors="replace")
        except OSError:
            return out
        for line in text.splitlines():
            for tag, key in (("Vulkan0 model buffer size", "model_vram_mib"), ("CPU_Mapped model buffer size",
                             "model_host_mib"), ("Vulkan0 KV buffer size", "kv_vram_mib"),
                             ("Vulkan0 compute buffer size", "compute_vram_mib"),
                             ("Vulkan_Host compute buffer size", "compute_host_mib")):
                if tag in line:
                    try:
                        out[key] = float(line.split("=")[1].split()[0])
                    except (IndexError, ValueError):
                        pass
        return out


# ---------------------------------------------------------------------------------------------------------- reference
@dataclass
class Reference:
    q_ids: list[list[int]]
    d_ids: list[list[int]]
    q_keep: list[list[int] | None]
    d_keep: list[list[int] | None]
    dense_q: np.ndarray | None
    dense_d: np.ndarray | None
    mv_q: list[np.ndarray] | None
    mv_d: list[np.ndarray] | None
    heads: dict[str, np.ndarray]
    meta: dict[str, Any]


def _load_mv(prefix: Path) -> list[np.ndarray] | None:
    data, off = Path(str(prefix) + "_mv.npy"), Path(str(prefix) + "_mv_off.npy")
    if not data.exists():
        return None
    X, O = np.load(data), np.load(off)
    return [X[O[i]:O[i + 1]] for i in range(len(O) - 1)]


def load_reference(ref_dir: Path) -> Reference:
    def tokens(name: str):
        ids, keep = [], []
        with open(ref_dir / f"tokens_{name}.jsonl") as fh:
            for line in fh:
                r = json.loads(line)
                ids.append(r["ids"])
                keep.append(r["keep"])
        return ids, keep

    q_ids, q_keep = tokens("query")
    if (ref_dir / "tokens_doc.jsonl").exists():
        d_ids, d_keep = tokens("doc")
    else:                                    # contextual reference: documents are the contexts
        d_ids, d_keep = [], []
        with open(ref_dir / "tokens_ctx.jsonl") as fh:
            for line in fh:
                d_ids.append(json.loads(line)["ids"])
                d_keep.append(None)
    heads = dict(np.load(ref_dir / "heads.npz")) if (ref_dir / "heads.npz").exists() else {}
    dq = np.load(ref_dir / "q_dense.npy") if (ref_dir / "q_dense.npy").exists() else None
    dd = np.load(ref_dir / "d_dense.npy") if (ref_dir / "d_dense.npy").exists() else None
    return Reference(q_ids, d_ids, q_keep, d_keep, dq, dd, _load_mv(ref_dir / "q"), _load_mv(ref_dir / "d"), heads,
                     json.loads((ref_dir / "meta.json").read_text()))


# ------------------------------------------------------------------------------------------------------ encode helpers
def encode_outputs(server: Server, ids: list[list[int]], keeps: list[list[int] | None], heads: dict[str, np.ndarray],
                   *, batch: int = 8) -> dict[str, list[np.ndarray]]:
    spec = server.spec
    dense, mv = [], []
    for i in range(0, len(ids), batch):
        res = server.client.embed_ids(ids[i:i + batch])
        for v, keep, seq in zip(res.vectors, keeps[i:i + batch], ids[i:i + batch]):
            if spec.colbert is None or spec.family == "multi":
                if server.client.pooled:
                    dense.append(pp.finalize_dense(spec, v).vector)
                else:   # token output: the spec pooling (contextual model: one chunk = mean over the sequence)
                    how = spec.pooling if spec.pooling != "none" else "mean"
                    dense.append(pp.finalize_dense(spec, pp.pool(v, how)).vector)
            if spec.colbert is not None:
                mv.append(pp.colbert_tokens(spec, v, None if keep is None else [bool(k) for k in keep],
                                            head=heads.get("colbert_w"), head_bias=heads.get("colbert_b")))
    return {"dense": dense, "mv": mv}


def encode_context_outputs(server: Server, ref_dir: Path, n_docs: int) -> tuple[list[np.ndarray], list[np.ndarray]]:
    """pplx-embed-context: token outputs of every contextual document, mean-pooled per chunk span (late chunking)."""
    spec = server.spec
    d_vec: list[np.ndarray | None] = [None] * n_docs
    with open(ref_dir / "tokens_ctx.jsonl") as fh:
        for line in fh:
            r = json.loads(line)
            H = server.client.embed_ids([r["ids"]]).vectors[0]
            for i, o in zip(r["doc_idx"], pp.context_chunks(spec, H, [tuple(x) for x in r["spans"]])):
                d_vec[i] = o.vector
    q_vec = []
    with open(ref_dir / "tokens_query.jsonl") as fh:
        for line in fh:
            r = json.loads(line)
            H = server.client.embed_ids([r["ids"]]).vectors[0]
            q_vec.append(pp.context_chunks(spec, H, [tuple(x) for x in r["spans"]])[0].vector)
    return q_vec, [v for v in d_vec if v is not None]


def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    return float(np.percentile(np.asarray(xs), q))


def latency_stats(xs: list[float]) -> dict[str, Any]:
    return {"n": len(xs), "p50_ms": round(_pct(xs, 50) * 1e3, 2) if xs else None,
            "p95_ms": round(_pct(xs, 95) * 1e3, 2) if xs else None,
            "mean_ms": round(statistics.fmean(xs) * 1e3, 2) if xs else None,
            "max_ms": round(max(xs) * 1e3, 2) if xs else None}


def sequential_queries(server: Server, q_ids: list[list[int]], keeps, heads, repeats: int) -> list[float]:
    lat = []
    for _ in range(repeats):
        for ids, keep in zip(q_ids, keeps):
            t0 = time.perf_counter()
            encode_outputs(server, [ids], [keep], heads, batch=1)
            lat.append(time.perf_counter() - t0)
    return lat


def doc_throughput(server: Server, d_ids: list[list[int]], keeps, heads, *, clients: int, batch: int,
                   limit: int) -> dict[str, Any]:
    ids, kp = d_ids[:limit], keeps[:limit]
    chunks = [(ids[i:i + batch], kp[i:i + batch]) for i in range(0, len(ids), batch)]
    t0 = time.perf_counter()
    errors = 0

    def work(ch):
        nonlocal errors
        try:
            encode_outputs(server, ch[0], ch[1], heads, batch=len(ch[0]))
        except LlamaError:
            errors += 1

    with ThreadPoolExecutor(max_workers=clients) as ex:
        list(ex.map(work, chunks))
    dt = time.perf_counter() - t0
    ntok = sum(len(x) for x in ids)
    return {"docs": len(ids), "tokens": ntok, "seconds": round(dt, 3), "docs_per_s": round(len(ids) / dt, 2),
            "tokens_per_s": round(ntok / dt, 1), "clients": clients, "batch": batch, "errors": errors}


class Sampler:
    """Background sampling of device VRAM and GPU busy percent."""

    def __init__(self, interval_s: float = 0.05) -> None:
        self.dev = gpu.find_amdgpu_device()
        self.interval_s = interval_s
        self.samples: list[tuple[float, int | None, int | None]] = []
        self._stop = threading.Event()
        self._t: threading.Thread | None = None

    def __enter__(self):
        def loop():
            while not self._stop.is_set():
                m = gpu.device_memory(self.dev)
                self.samples.append((time.perf_counter(), m.vram_used, m.gpu_busy_percent))
                time.sleep(self.interval_s)
        self._t = threading.Thread(target=loop, daemon=True)
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._t:
            self._t.join()

    def summary(self) -> dict[str, Any]:
        vram = [s[1] for s in self.samples if s[1] is not None]
        busy = [s[2] for s in self.samples if s[2] is not None]
        return {"vram_peak_mib": round(max(vram) / 2 ** 20, 1) if vram else None,
                "vram_mean_mib": round(statistics.fmean(vram) / 2 ** 20, 1) if vram else None,
                "gpu_busy_mean_pct": round(statistics.fmean(busy), 1) if busy else None,
                "gpu_busy_p95_pct": _pct([float(b) for b in busy], 95) if busy else None,
                "n_samples": len(self.samples)}


def _mib(x: int | None) -> float | None:
    return None if x is None else round(x / 2 ** 20, 1)


def snapshot(servers: list[Server]) -> dict[str, Any]:
    dev = gpu.device_memory()
    per = {}
    for s in servers:
        pg = gpu.process_gpu(s.pid) if s.pid else None
        per[s.label] = {"vram_mib": _mib(pg.vram_bytes) if pg else None, "gtt_mib": _mib(pg.gtt_bytes) if pg else None,
                          "engine_ns": pg.engine_ns if pg else None, **s.host_usage()}
    return {"device": {"vram_used_mib": _mib(dev.vram_used), "gtt_used_mib": _mib(dev.gtt_used),
                       "runtime_status": dev.runtime_status, "power_control": dev.power_control,
                       "sclk_mhz": dev.sclk_mhz, "mclk_mhz": dev.mclk_mhz, "power_w": dev.power_w},
            "per_process": per}


# ---------------------------------------------------------------------------------------------------------- commands
def cmd_model(a: argparse.Namespace) -> int:
    spec = get(a.key)
    ref = load_reference(Path(a.ref))
    log_dir = Path(a.out).parent / "logs"
    cfg = ServerConfig(a.key, a.gguf, a.port, ctx=a.ctx, batch=a.ubatch, ubatch=a.ubatch, parallel=a.parallel,
                       threads=a.threads, flash_attn=a.fa)
    srv = Server(cfg, log_dir)
    base = gpu.device_memory()
    result: dict[str, Any] = {"key": a.key, "quant": a.quant, "gguf": Path(a.gguf).name, "config": cfg.__dict__,
                              "variant": os.environ.get("TAG") or "vk-mesa25", "llama_server": LLAMA_BIN,
                              "llama_server_bin": binary_info(), "argv": cfg.argv(get(a.key))[1:], "vk_env": VK_ENV,
                              "baseline_vram_mib": _mib(base.vram_used), "started_at": time.strftime("%FT%T%z")}
    try:
        srv.start()
        result["load_s"] = round(srv.load_s or 0, 2)
        encode_outputs(srv, ref.q_ids[:4], ref.q_keep[:4], ref.heads)  # warm-up (pipelines, BO residency)
        result["resident_after_warmup"] = snapshot([srv])
        result["buffers_mib"] = srv.buffers_from_log()
        with Sampler() as smp:
            t0 = time.perf_counter()
            if (Path(a.ref) / "tokens_ctx.jsonl").exists():
                qv, dv = encode_context_outputs(srv, Path(a.ref), len(ref.dense_d))
                q_out, d_out = {"dense": qv, "mv": []}, {"dense": dv, "mv": []}
            else:
                q_out = encode_outputs(srv, ref.q_ids, ref.q_keep, ref.heads, batch=8)
                d_out = encode_outputs(srv, ref.d_ids, ref.d_keep, ref.heads, batch=8)
            result["parity_encode_s"] = round(time.perf_counter() - t0, 2)
        result["parity_phase"] = smp.summary()
        par: dict[str, Any] = {}
        if ref.dense_q is not None and q_out["dense"]:
            par["dense"] = parity.dense_parity(ref.dense_q, ref.dense_d, np.stack(q_out["dense"]),
                                               np.stack(d_out["dense"]))
            par["dense"]["gate"] = parity.gate(par["dense"])
        if ref.mv_q is not None and q_out["mv"]:
            par["late"] = parity.late_parity(ref.mv_q, ref.mv_d, q_out["mv"], d_out["mv"])
            par["late"]["gate"] = parity.gate(par["late"])
        result["parity"] = par
        if not a.parity_only:
            with Sampler() as smp:
                lat = sequential_queries(srv, ref.q_ids, ref.q_keep, ref.heads, a.repeats)
            result["query_latency_sequential"] = {**latency_stats(lat), **smp.summary()}
            with Sampler() as smp:
                result["doc_throughput"] = doc_throughput(srv, ref.d_ids, ref.d_keep, ref.heads,
                                                          clients=a.clients, batch=a.doc_batch, limit=a.doc_limit)
            result["doc_throughput"].update(smp.summary())
        result["resident_after_load"] = snapshot([srv])
        if a.cold:
            cold = []
            dev = gpu.find_amdgpu_device()
            for _ in range(a.cold):
                time.sleep(a.idle_s)
                # only the PM-core attribute: reading amdgpu pp_*/busy files would resume the device itself
                st = gpu.runtime_status(dev)
                t0 = time.perf_counter()
                encode_outputs(srv, [ref.q_ids[0]], [ref.q_keep[0]], ref.heads, batch=1)
                cold.append({"state_before": st, "ms": round((time.perf_counter() - t0) * 1e3, 1)})
            result["cold_after_idle"] = cold
        result["status"] = "OK"
    except Exception as exc:  # the matrix records failures (OOM, unsupported op, crash) as data
        result["status"] = "FAIL"
        result["error"] = f"{type(exc).__name__}: {exc}"[:800]
    finally:
        result["server_alive_at_end"] = srv.alive()
        srv.stop()
        result["finished_at"] = time.strftime("%FT%T%z")
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(result, indent=1, ensure_ascii=False))
    print(json.dumps({k: result.get(k) for k in ("key", "quant", "status", "error", "load_s")}, ensure_ascii=False))
    return 0 if result["status"] == "OK" else 1


def _mode_run(servers: list[Server], refs: dict[str, Reference], *, seconds: float, concurrent: bool,
              clients_per_model: int | dict[str, int], doc_share: dict[str, float] | float) -> dict[str, Any]:
    """Drive the resident servers for ``seconds``: queries (and a share of document batches) per model; alternating
    (MODE C: one request at a time over both models) or concurrent (MODE D: independent client threads per model)."""
    lat: dict[str, list[float]] = {s.label: [] for s in servers}
    docs_done: dict[str, int] = {s.label: 0 for s in servers}
    errors: dict[str, list[str]] = {s.label: [] for s in servers}
    clients = clients_per_model if isinstance(clients_per_model, dict) else {s.label: clients_per_model for s in servers}
    deadline = time.perf_counter() + seconds

    shares = doc_share if isinstance(doc_share, dict) else {s.label: float(doc_share) for s in servers}

    def one(s: Server, i: int) -> None:
        r = refs[s.cfg.key]
        share = shares.get(s.label, 0.0)
        if share > 0 and ((i * 2654435761) % 1000) < share * 1000:   # deterministic share, thread-safe
            j = (i * 8) % max(1, len(r.d_ids) - 8)
            encode_outputs(s, r.d_ids[j:j + 8], r.d_keep[j:j + 8], r.heads, batch=8)
            docs_done[s.label] += 8
            return
        q = i % len(r.q_ids)
        t0 = time.perf_counter()
        encode_outputs(s, [r.q_ids[q]], [r.q_keep[q]], r.heads, batch=1)
        lat[s.label].append(time.perf_counter() - t0)

    t_start = time.perf_counter()
    with Sampler() as smp:
        if not concurrent:
            i = 0
            while time.perf_counter() < deadline:
                for s in servers:
                    try:
                        one(s, i)
                    except Exception as exc:
                        errors[s.label].append(str(exc)[:200])
                i += 1
        else:
            def loop(s: Server, seed: int):
                i = seed
                while time.perf_counter() < deadline:
                    try:
                        one(s, i)
                    except Exception as exc:
                        errors[s.label].append(str(exc)[:200])
                    i += max(1, clients[s.label])
            threads = [threading.Thread(target=loop, args=(s, k)) for s in servers for k in range(clients[s.label])]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
    wall = time.perf_counter() - t_start
    out = {"seconds": round(wall, 2), "concurrent": concurrent, "clients_per_model": clients_per_model,
           "doc_share": doc_share, "gpu": smp.summary(), "per_model": {}}
    for s in servers:
        k = s.label
        out["per_model"][k] = {"queries": latency_stats(lat[k]), "query_qps": round(len(lat[k]) / wall, 2),
                               "docs": docs_done[k], "docs_per_s": round(docs_done[k] / wall, 2),
                               "errors": len(errors[k]), "error_samples": errors[k][:3], "alive": s.alive()}
    return out


def cmd_pair(a: argparse.Namespace) -> int:
    dk, dg = a.dense.split(":", 1)
    lk, lg = a.late.split(":", 1)
    refs = {dk: load_reference(Path(a.ref_dense)), lk: load_reference(Path(a.ref_late))}
    log_dir = Path(a.out).parent / "logs"
    mk = lambda k, g, port: Server(ServerConfig(k, g, port, ctx=a.ctx, batch=a.ubatch, ubatch=a.ubatch,  # noqa: E731
                                                parallel=a.parallel, threads=a.threads, flash_attn=a.fa), log_dir)
    res: dict[str, Any] = {"dense": {"key": dk, "gguf": Path(dg).name}, "late": {"key": lk, "gguf": Path(lg).name},
                           "llama_server_bin": binary_info(), "vk_env": VK_ENV,
                           "baseline": snapshot([]), "started_at": time.strftime("%FT%T%z"), "modes": {}}
    dense, late = mk(dk, dg, a.port), mk(lk, lg, a.port + 1)
    try:
        for mode, servers in (("A", [dense]), ("B", [late])):
            s = servers[0]
            s.start()
            encode_outputs(s, refs[s.cfg.key].q_ids[:4], refs[s.cfg.key].q_keep[:4], refs[s.cfg.key].heads)
            idle = snapshot([s])
            run = _mode_run([s], refs, seconds=a.seconds, concurrent=True, clients_per_model=a.clients,
                            doc_share=a.doc_share)
            res["modes"][mode] = {"idle": idle, "run": run, "after": snapshot([s]), "buffers": s.buffers_from_log()}
            s.stop()
            time.sleep(2)
        for s in (dense, late):
            s.start()
            encode_outputs(s, refs[s.cfg.key].q_ids[:4], refs[s.cfg.key].q_keep[:4], refs[s.cfg.key].heads)
        res["modes"]["C"] = {"idle": snapshot([dense, late]),
                             "run": _mode_run([dense, late], refs, seconds=a.seconds, concurrent=False,
                                              clients_per_model=1, doc_share=a.doc_share)}
        res["modes"]["C"]["after"] = snapshot([dense, late])
        res["modes"]["D"] = {"run": _mode_run([dense, late], refs, seconds=a.seconds, concurrent=True,
                                              clients_per_model=a.clients, doc_share=a.doc_share)}
        res["modes"]["D"]["after"] = snapshot([dense, late])
        # D2: interactive queries on both models while the dense model encodes documents in the background
        # (one extra client per model; the dense extra client sends only document batches)
        res["modes"]["D_bg"] = {"run": _mode_run([dense, late], refs, seconds=a.seconds, concurrent=True,
                                                 clients_per_model=a.clients + 1,
                                                 doc_share={dense.cfg.key: a.bg_doc_share, late.cfg.key: 0.0})}
        res["modes"]["D_bg"]["after"] = snapshot([dense, late])
        if a.bg_separate:
            # D_bg_sep: document encoding in a THIRD process (same dense GGUF, 2 slots) while the resident dense and
            # late servers answer queries — GPU sharing without request queueing inside one llama-server
            bg = Server(ServerConfig(dk, dg, a.port + 2, ctx=a.ctx, batch=a.ubatch, ubatch=a.ubatch, parallel=2,
                                     threads=a.threads, flash_attn=a.fa), log_dir, label="bg_docs")
            bg.start()
            encode_outputs(bg, refs[dk].d_ids[:4], refs[dk].d_keep[:4], refs[dk].heads)
            res["modes"]["D_bg_sep"] = {"idle": snapshot([dense, late, bg]),
                                        "run": _mode_run([dense, late, bg], refs, seconds=a.seconds, concurrent=True,
                                                         clients_per_model={dense.label: a.clients,
                                                                            late.label: a.clients, "bg_docs": 1},
                                                         doc_share={dense.label: 0.0, late.label: 0.0,
                                                                    "bg_docs": 1.0})}
            res["modes"]["D_bg_sep"]["after"] = snapshot([dense, late, bg])
            bg.stop()
        res["reload_count"] = {dense.cfg.key: dense.starts - 2, late.cfg.key: late.starts - 2}
        res["status"] = "OK" if dense.alive() and late.alive() else "FAIL"
    except Exception as exc:
        res["status"] = "FAIL"
        res["error"] = f"{type(exc).__name__}: {exc}"[:800]
    finally:
        for s in (dense, late):
            s.stop()
        res["finished_at"] = time.strftime("%FT%T%z")
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(res, indent=1, ensure_ascii=False))
    print(json.dumps({"status": res.get("status"), "error": res.get("error")}, ensure_ascii=False))
    return 0 if res.get("status") == "OK" else 1


def _max_abs_diff(xs: list[np.ndarray], ys: list[np.ndarray]) -> dict[str, Any]:
    if len(xs) != len(ys):
        return {"n": [len(xs), len(ys)], "identical": False, "max_abs_diff": None}
    diffs = [float(np.max(np.abs(x - y))) if x.shape == y.shape and x.size else (0.0 if x.shape == y.shape else
             float("inf")) for x, y in zip(xs, ys)]
    return {"n": len(xs), "identical": all(d == 0.0 for d in diffs), "n_identical": sum(d == 0.0 for d in diffs),
            "max_abs_diff": max(diffs) if diffs else 0.0}


def cmd_ab(a: argparse.Namespace) -> int:
    """Two llama-server binaries, one GGUF, the same token ids: the outputs must be identical (checks of patches that
    must not change numerics), memory of each process after the same work. Servers run one after the other."""
    global LLAMA_BIN
    ref = load_reference(Path(a.ref))
    n_d = min(a.limit_docs, len(ref.d_ids))
    log_dir = Path(a.out).parent / "logs"
    res: dict[str, Any] = {"key": a.key, "quant": a.quant, "gguf": Path(a.gguf).name, "vk_env": VK_ENV,
                           "bin_a": binary_info(a.bin_a), "bin_b": binary_info(a.bin_b), "n_queries": len(ref.q_ids),
                           "n_docs": n_d, "started_at": time.strftime("%FT%T%z"), "runs": {}}
    outs: dict[str, dict[str, list[np.ndarray]]] = {}
    try:
        for label, binary, port in (("a", a.bin_a, a.port), ("b", a.bin_b, a.port + 1)):
            LLAMA_BIN = binary                              # ServerConfig.argv reads the module global
            cfg = ServerConfig(a.key, a.gguf, port, ctx=a.ctx, batch=a.ubatch, ubatch=a.ubatch, parallel=a.parallel,
                               threads=a.threads, flash_attn=a.fa)
            srv = Server(cfg, log_dir, label=label)
            try:
                srv.start()
                q = encode_outputs(srv, ref.q_ids, ref.q_keep, ref.heads)
                d = encode_outputs(srv, ref.d_ids[:n_d], ref.d_keep[:n_d], ref.heads)
                outs[label] = {"q_dense": q["dense"], "q_mv": q["mv"], "d_dense": d["dense"], "d_mv": d["mv"]}
                res["runs"][label] = {"argv": cfg.argv(srv.spec)[1:], "load_s": srv.load_s, "after": snapshot([srv])}
            finally:
                srv.stop()
        res["compare"] = {k: _max_abs_diff(outs["a"][k], outs["b"][k]) for k in outs["a"] if outs["a"][k]}
        res["identical"] = all(c["identical"] for c in res["compare"].values())
        res["status"] = "OK"
    except Exception as e:  # noqa: BLE001 — recorded in the result
        res.update(status="FAIL", error=f"{type(e).__name__}: {e}")
    res["finished_at"] = time.strftime("%FT%T%z")
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(res, indent=1, ensure_ascii=False))
    print(json.dumps({"status": res["status"], "identical": res.get("identical"), "error": res.get("error")}))
    return 0 if res["status"] == "OK" and res.get("identical") else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="vkm-rx580-bench")
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--out", required=True)
    common.add_argument("--ctx", type=int, default=4096)
    common.add_argument("--ubatch", type=int, default=2048)
    common.add_argument("--parallel", type=int, default=4)
    common.add_argument("--threads", type=int, default=4)
    common.add_argument("--fa", default="auto")
    common.add_argument("--port", type=int, default=18200)
    m = sub.add_parser("model", parents=[common])
    m.add_argument("--key", required=True)
    m.add_argument("--gguf", required=True)
    m.add_argument("--quant", default="")
    m.add_argument("--ref", required=True)
    m.add_argument("--repeats", type=int, default=3)
    m.add_argument("--clients", type=int, default=2)
    m.add_argument("--doc-batch", type=int, default=8)
    m.add_argument("--doc-limit", type=int, default=512)
    m.add_argument("--cold", type=int, default=0)
    m.add_argument("--parity-only", action="store_true", help="skip latency/throughput (parity gate on another probe)")
    m.add_argument("--idle-s", type=float, default=8.0)
    m.set_defaults(func=cmd_model)
    p = sub.add_parser("pair", parents=[common])
    p.add_argument("--dense", required=True)
    p.add_argument("--late", required=True)
    p.add_argument("--ref-dense", required=True)
    p.add_argument("--ref-late", required=True)
    p.add_argument("--seconds", type=float, default=30.0)
    p.add_argument("--clients", type=int, default=2)
    p.add_argument("--doc-share", type=float, default=0.0)
    p.add_argument("--bg-doc-share", type=float, default=0.34, help="share of dense requests that are document batches "
                   "in MODE D_bg")
    p.add_argument("--bg-separate", action="store_true", help="also MODE D_bg_sep: documents in a third process")
    p.set_defaults(func=cmd_pair)
    b = sub.add_parser("ab", parents=[common], help="two llama-server binaries: identical outputs, memory")
    b.add_argument("--key", required=True)
    b.add_argument("--gguf", required=True)
    b.add_argument("--quant", default="")
    b.add_argument("--ref", required=True)
    b.add_argument("--bin-a", required=True)
    b.add_argument("--bin-b", required=True)
    b.add_argument("--limit-docs", type=int, default=512)
    b.set_defaults(func=cmd_ab)
    a = ap.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
