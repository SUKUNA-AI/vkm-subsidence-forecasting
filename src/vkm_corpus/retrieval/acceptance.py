"""Acceptance of the EDGE rerankers (постановка §31, §56; project F §7; H К-20). Standard library only.

Runs on EDGE with the system ``python3`` (no project install): ``python3 acceptance.py run …``. It talks HTTP to the
gateway, the text service and llama-server on loopback, reads ``docker inspect`` / ``docker logs`` of the three
containers and samples ``nvidia-smi``. Nothing here starts, stops or reconfigures a container.

Checks (PASS/FAIL each, receipt JSON without addresses or secrets):

1. both loaded — gateway ``/health`` ready/ready, both host PIDs in ``nvidia-smi`` compute apps;
2. idle VRAM — 10 s of samples, per-process memory, ``memory.free ≥ 128 MiB``;
3. text — contract, top-3 sanity ≥ 4/5, determinism ``|Δ| ≤ 1e-4``, order invariance (top-3), 413 for 25
   candidates and for > 4096 tokens, gateway token count = service count;
4. visual — top-1 sanity ≥ 4/5 on real (synthetic) images, «visual means visual» A–F, 8 images per call,
   placement in ``/status``;
5. concurrent — text calls complete while a visual call is running;
6. burst — 12 and 16 mixed requests at once: only 200 or 503 ``RERANK_BACKEND_BUSY``;
7. no OOM — no CUDA/OOM lines in the container logs of the window, ``memory.free ≥ 128 MiB`` in every sample;
8. no reload — ``StartedAt``/``RestartCount``/PID of ``careerops-reranker``, the m0 and gateway containers unchanged,
   no model-load lines in the m0 log, same llama-server instance (media marker);
9. latency — p50/p95/max per kind and phase; GPU clocks, throttle reasons, power, temperature.

``ptext`` measures the VRAM of the text service after requests of ≈1K/2K/3K/4K tokens (CP-18 §7.6); the service
enforces ``token_budget`` itself, so no request above the gateway limit ever reaches inference.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import random
import re
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# must equal vkm_corpus.retrieval.models (checked by tests/corpus/test_rerank_acceptance.py)
MAX_TEXT_CANDIDATES = 24
MAX_VISUAL_CANDIDATES = 8
TEXT_TOKEN_BUDGET = 4096
MAX_IMAGE_BYTES = 10 * 1024 * 1024
HEADER_TOKEN = "X-VKM-Rerank-Token"
FREE_VRAM_FLOOR_MIB = 128
USABLE_VRAM_MIB = 3716
OOM_RE = re.compile(r"out of memory|CUDA error|cudaMalloc|failed to allocate|OutOfMemory|CUBLAS_STATUS_ALLOC", re.I)
LOAD_RE = re.compile(r"llama_model_load|load_tensors:|clip_model_loader|loading model", re.I)
PARITY_IMAGES = ["map_scheme", "table", "plot", "section", "text_page", "blank"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ------------------------------------------------------------------------------------------------------ host tools
def sh(cmd: list[str], timeout: float = 60.0) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout + (p.stderr if p.returncode else "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, f"{type(exc).__name__}: {exc}"


def container_state(name: str) -> dict:
    rc, out = sh(["docker", "inspect", name])
    if rc != 0:
        return {"name": name, "present": False}
    info = json.loads(out)[0]
    state = info.get("State", {})
    return {"name": name, "present": True, "status": state.get("Status"), "started_at": state.get("StartedAt"),
            "pid": state.get("Pid"), "restart_count": info.get("RestartCount"),
            "health": (state.get("Health") or {}).get("Status"), "image_id": info.get("Image"),
            "image_ref": (info.get("Config") or {}).get("Image")}


def container_logs_since(name: str, since: str) -> str:
    rc, out = sh(["docker", "logs", "--since", since, name], timeout=60)
    return out if rc == 0 else ""


def gpu_apps() -> dict[int, int]:
    rc, out = sh(["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"])
    apps = {}
    if rc == 0:
        for line in out.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2 and parts[0].isdigit():
                apps[int(parts[0])] = int(float(parts[1])) if parts[1].replace(".", "").isdigit() else -1
    return apps


GPU_FIELDS = ["memory.used", "memory.total", "memory.free", "utilization.gpu", "temperature.gpu", "power.draw",
              "clocks.sm", "clocks.mem", "clocks_event_reasons.active"]


class GpuSampler(threading.Thread):
    """``nvidia-smi -lms`` stream (GPU totals, every 200 ms) + per-process memory every second."""

    def __init__(self, period_ms: int = 200) -> None:
        super().__init__(daemon=True)
        self.period_ms = period_ms
        self.samples: list[dict] = []
        self.apps: list[dict] = []
        self._stop = threading.Event()
        self._proc = None

    def run(self) -> None:
        fields = list(GPU_FIELDS)
        self._proc = subprocess.Popen(["nvidia-smi", f"--query-gpu={','.join(fields)}", "--format=csv,noheader,nounits",
                                       f"-lms={self.period_ms}"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                      text=True)
        apps_thread = threading.Thread(target=self._apps_loop, daemon=True)
        apps_thread.start()
        for line in self._proc.stdout:
            if self._stop.is_set():
                break
            parts = [p.strip() for p in line.split(",")]
            if len(parts) != len(fields):
                continue
            row = {"t": time.time()}
            for k, v in zip(fields, parts):
                try:
                    row[k] = float(v) if k != "clocks_event_reasons.active" else v
                except ValueError:
                    row[k] = None
            self.samples.append(row)

    def _apps_loop(self) -> None:
        while not self._stop.is_set():
            self.apps.append({"t": time.time(), "apps": gpu_apps()})
            self._stop.wait(1.0)

    def stop(self) -> None:
        self._stop.set()
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    def window(self, t0: float, t1: float) -> list[dict]:
        return [s for s in self.samples if t0 <= s["t"] <= t1]


def summarize_gpu(samples: list[dict]) -> dict:
    if not samples:
        return {"n": 0}
    def stat(key):
        vals = [s[key] for s in samples if isinstance(s.get(key), float)]
        return {"min": min(vals), "max": max(vals), "mean": round(statistics.fmean(vals), 1)} if vals else None
    reasons: dict[str, int] = {}
    for s in samples:
        r = s.get("clocks_event_reasons.active")
        if r:
            reasons[r] = reasons.get(r, 0) + 1
    return {"n": len(samples), "memory_used_mib": stat("memory.used"), "memory_free_mib": stat("memory.free"),
            "utilization_pct": stat("utilization.gpu"), "temperature_c": stat("temperature.gpu"),
            "power_w": stat("power.draw"), "clock_sm_mhz": stat("clocks.sm"), "clock_mem_mhz": stat("clocks.mem"),
            "clocks_event_reasons": reasons}


# ------------------------------------------------------------------------------------------------------------ HTTP
class Http:
    def __init__(self, base: str, token: str | None = None) -> None:
        self.base, self.token = base.rstrip("/"), token

    def call(self, method: str, path: str, body: dict | bytes | None = None, timeout: float = 600.0,
             auth: bool = True) -> tuple[int, dict | None, float]:
        data = body if isinstance(body, bytes) or body is None else json.dumps(body, ensure_ascii=False).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if auth and self.token:
            req.add_header(HEADER_TOKEN, self.token)
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw, status = resp.read(), resp.status
        except urllib.error.HTTPError as exc:
            raw, status = exc.read(), exc.code
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            return 0, {"error": {"code": "CLIENT_" + type(exc).__name__, "message": str(exc)}}, \
                (time.perf_counter() - t0) * 1000
        ms = (time.perf_counter() - t0) * 1000
        try:
            return status, json.loads(raw), ms
        except ValueError:
            return status, None, ms


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(q * (len(s) - 1)))))
    return round(s[k], 1)


def lat_summary(values: list[float]) -> dict:
    return {"n": len(values), "p50": pct(values, 0.5), "p95": pct(values, 0.95),
            "max": round(max(values), 1) if values else None}


# -------------------------------------------------------------------------------------------------------- fixtures
class Fixtures:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        self.b64 = {n: base64.b64encode((root / m["file"]).read_bytes()).decode()
                    for n, m in self.manifest["images"].items()}

    def sha(self) -> dict:
        return {n: m["sha256"] for n, m in self.manifest["images"].items()}


def text_request(fx: Fixtures, query: str, order: list[int] | None = None, top_n: int | None = None) -> dict:
    docs = fx.manifest["text_docs"]
    order = order or list(range(len(docs)))
    body = {"query": query, "candidates": [{"id": docs[i]["id"], "text": docs[i]["text"]} for i in order]}
    if top_n:
        body["top_n"] = top_n
    return body


def visual_request(fx: Fixtures, query: str, names: list[str], ids: list[str] | None = None) -> dict:
    ids = ids or names
    return {"query": query, "candidates": [{"id": i, "image_base64": fx.b64[n]} for i, n in zip(ids, names)]}


def score_map(body: dict) -> dict[str, float]:
    return dict(zip(body["candidate_ids"], body["scores"]))


# ---------------------------------------------------------------------------------------------------------- checks
class Run:
    def __init__(self, args, token: str | None) -> None:
        self.args = args
        self.gw = Http(args.gateway, token)
        self.text_service = Http(args.text_service)
        self.m0 = Http(args.m0)
        self.fx = Fixtures(Path(args.fixtures))
        self.checks: dict[str, dict] = {}
        self.latency: dict[str, list[float]] = {"text": [], "visual": []}
        self.phases: dict[str, dict[str, list[float]]] = {"text": {}, "visual": {}}
        self.statuses: list[int] = []

    def record(self, kind: str, status: int, body: dict | None, ms: float) -> None:
        self.statuses.append(status)
        if status == 200 and body:
            self.latency[kind].append(ms)
            for phase, v in (body.get("latency_ms") or {}).items():
                self.phases[kind].setdefault(phase, []).append(v)

    def text(self, body: dict, timeout: float = 330) -> tuple[int, dict | None, float]:
        st, b, ms = self.gw.call("POST", "/v1/rerank/text", body, timeout=timeout)
        self.record("text", st, b, ms)
        return st, b, ms

    def visual(self, body: dict, timeout: float = 600) -> tuple[int, dict | None, float]:
        st, b, ms = self.gw.call("POST", "/v1/rerank/visual", body, timeout=timeout)
        self.record("visual", st, b, ms)
        return st, b, ms

    # 1 ---------------------------------------------------------------------------------------------------------
    def check_loaded(self, states: dict) -> None:
        st, health, _ = self.gw.call("GET", "/health", auth=False, timeout=30)
        rst, ready, _ = self.text_service.call("GET", "/readyz", auth=False, timeout=10)
        mst, _, _ = self.m0.call("GET", "/health", auth=False, timeout=10)
        apps = gpu_apps()
        pids = {k: states[k]["pid"] for k in ("text", "m0")}
        ok = (st == 200 and health and health.get("status") == "ok" and rst == 200 and mst == 200
              and all(p in apps for p in pids.values()))
        self.checks["1_both_loaded"] = {
            "pass": bool(ok), "gateway_health": health, "text_readyz": rst, "m0_health": mst,
            "text_runtime": (ready or {}).get("runtime"),
            "gpu_processes": {k: {"pid_in_compute_apps": p in apps, "used_mib": apps.get(p)} for k, p in pids.items()}}

    # 2 ---------------------------------------------------------------------------------------------------------
    def check_idle(self, sampler: GpuSampler, states: dict) -> None:
        t0 = time.time()
        time.sleep(10)
        samples = sampler.window(t0, time.time())
        apps = [a["apps"] for a in sampler.apps if t0 <= a["t"] <= time.time()]
        per = {}
        for k in ("text", "m0"):
            vals = [a.get(states[k]["pid"]) for a in apps if a.get(states[k]["pid"]) is not None]
            per[k] = {"min": min(vals), "max": max(vals)} if vals else None
        summ = summarize_gpu(samples)
        free_min = (summ.get("memory_free_mib") or {}).get("min")
        self.checks["2_idle_vram"] = {"pass": bool(free_min is not None and free_min >= FREE_VRAM_FLOOR_MIB),
                                      "gpu": summ, "per_process_mib": per, "floor_mib": FREE_VRAM_FLOOR_MIB}

    # 3 ---------------------------------------------------------------------------------------------------------
    def check_text(self) -> None:
        docs = {d["id"]: d for d in self.fx.manifest["text_docs"]}
        hits, per_query, contract_ok, first = 0, {}, True, None
        for q in self.fx.manifest["text_queries"]:
            st, b, ms = self.text(text_request(self.fx, q["query"]))
            ok = st == 200 and b and b.get("contract") == "vkm.rerank/1" and len(b["candidate_ids"]) == len(docs)
            contract_ok &= bool(ok)
            top3 = b["candidate_ids"][:3] if ok else []
            hits += q["expect"] in top3
            per_query[q["id"]] = {"status": st, "expected": q["expect"], "top3": top3, "ms": round(ms, 1),
                                  "warnings": (b or {}).get("warnings"), "n_tokens_total": (b or {}).get("n_tokens_total")}
            first = first or (q, b)
        q0, b0 = first
        st, b1, _ = self.text(text_request(self.fx, q0["query"]))
        m0_, m1 = score_map(b0), score_map(b1) if st == 200 else {}
        det = max(abs(m0_[k] - m1.get(k, 1e9)) for k in m0_) if m1 else None
        order = list(range(len(docs)))
        random.Random(7).shuffle(order)
        st, bp, _ = self.text(text_request(self.fx, q0["query"], order=order))
        perm_top3 = bp["candidate_ids"][:3] if st == 200 else []
        exact_rank = st == 200 and bp["candidate_ids"] == b0["candidate_ids"]
        too_many = {"query": "проверка лимита", "candidates": [{"id": f"x{i}", "text": f"документ {i}"}
                                                               for i in range(MAX_TEXT_CANDIDATES + 1)]}
        st25, b25, _ = self.gw.call("POST", "/v1/rerank/text", too_many)
        long_text = " ".join(["Оседание земной поверхности над выработанным пространством калийного рудника."] * 22)
        too_long = {"query": "проверка бюджета токенов",
                    "candidates": [{"id": f"L{i}", "text": long_text} for i in range(MAX_TEXT_CANDIDATES)]}
        stl, bl, _ = self.gw.call("POST", "/v1/rerank/text", too_long)
        mism = [w for r in per_query.values() for w in (r["warnings"] or []) if w.startswith("TOKEN_COUNT_DIFFERS")]
        self.checks["3_text"] = {
            "pass": bool(contract_ok and hits >= 4 and det is not None and det <= 1e-4
                         and perm_top3 == b0["candidate_ids"][:3] and st25 == 413 and stl == 413 and not mism),
            "sanity_top3_hits": f"{hits}/{len(per_query)}", "per_query": per_query,
            "determinism_max_abs_diff": det, "permutation_top3_equal": perm_top3 == b0["candidate_ids"][:3],
            "permutation_full_rank_equal": exact_rank,
            "over_24_candidates": {"status": st25, "code": ((b25 or {}).get("error") or {}).get("code")},
            "over_token_budget": {"status": stl, "code": ((bl or {}).get("error") or {}).get("code"),
                                  "n_tokens_total": (((bl or {}).get("error") or {}).get("details") or {}).get(
                                      "n_tokens_total"),
                                  "stage": ((bl or {}).get("error") or {}).get("stage")},
            "token_count_gateway_vs_service": "equal" if not mism else mism,
            "model": {k: b0.get(k) for k in ("model_id", "model_revision", "quant", "placement", "backend_version",
                                             "license")}}

    # 4 ---------------------------------------------------------------------------------------------------------
    def check_visual(self, expected_placement: str | None) -> None:
        vq = self.fx.manifest["visual_queries"]
        hits, per_query, first = 0, {}, None
        for q in vq:
            st, b, ms = self.visual(visual_request(self.fx, q["query"], PARITY_IMAGES))
            top1 = b["candidate_ids"][0] if st == 200 else None
            hits += top1 == q["expect"]
            per_query[q["id"]] = {"status": st, "expected": q["expect"], "top1": top1, "ms": round(ms, 1),
                                  "scores": {k: round(v, 4) for k, v in score_map(b).items()} if st == 200 else None}
            first = first or b
        q_sec = next(q for q in vq if q["expect"] == "section")
        q_plot = next(q for q in vq if q["expect"] == "plot")
        # A: spread
        st, ba, _ = self.visual(visual_request(self.fx, q_sec["query"], ["section", "plot"]))
        sa = score_map(ba) if st == 200 else {}
        a_ok = st == 200 and ba["candidate_ids"][0] == "section" and max(sa.values()) - min(sa.values()) >= 0.05
        # B: permutation of ids
        names = ["map_scheme", "table", "plot", "section"]
        ids1, ids2 = ["id-1", "id-2", "id-3", "id-4"], ["id-3", "id-4", "id-1", "id-2"]
        st1, b1, _ = self.visual(visual_request(self.fx, q_plot["query"], names, ids1))
        st2, b2, _ = self.visual(visual_request(self.fx, q_plot["query"], names, ids2))
        by_img1 = {n: score_map(b1)[i] for n, i in zip(names, ids1)} if st1 == 200 else {}
        by_img2 = {n: score_map(b2)[i] for n, i in zip(names, ids2)} if st2 == 200 else {}
        b_diff = max(abs(by_img1[n] - by_img2[n]) for n in names) if by_img1 and by_img2 else None
        b_ok = b_diff is not None and b_diff <= 1e-3 and sorted(names, key=lambda n: -by_img1[n]) == sorted(
            names, key=lambda n: -by_img2[n])
        # C: duplicate
        st, bc, _ = self.visual(visual_request(self.fx, q_plot["query"], ["plot", "plot", "table"], ["dup-a", "dup-b",
                                                                                                     "other"]))
        sc = score_map(bc) if st == 200 else {}
        c_diff = abs(sc["dup-a"] - sc["dup-b"]) if sc else None
        c_ok = c_diff is not None and c_diff <= 1e-3
        # D: same caption «Рис. 1», different content
        st, bd1, _ = self.visual(visual_request(self.fx, q_sec["query"], ["fig1_plot", "fig1_section"]))
        st2, bd2, _ = self.visual(visual_request(self.fx, q_plot["query"], ["fig1_plot", "fig1_section"]))
        d_ok = st == 200 and st2 == 200 and bd1["candidate_ids"][0] == "fig1_section" and \
            bd2["candidate_ids"][0] == "fig1_plot"
        # E: blank below the matching image for every query
        e_ok = all(r["scores"] and r["scores"]["blank"] < r["scores"][r["expected"]] for r in per_query.values())
        # F: negative contract
        png = self.fx.b64["table"]
        f_cases = {
            "missing_image": ({"query": "q", "candidates": [{"id": "a"}]}, 422),
            "text_field": ({"query": "q", "candidates": [{"id": "a", "image_base64": png, "text": "t"}]}, 422),
            "broken_base64": ({"query": "q", "candidates": [{"id": "a", "image_base64": "@@not-base64@@"}]}, 422),
            "not_an_image": ({"query": "q", "candidates": [{"id": "a", "image_base64": base64.b64encode(
                b"plain text, not an image").decode()}]}, 422),
            "over_10_mib": ({"query": "q", "candidates": [{"id": "a", "image_base64": base64.b64encode(
                b"\x89PNG\r\n\x1a\n" + os.urandom(MAX_IMAGE_BYTES + 1024)).decode()}]}, 413),
            "nine_images": ({"query": "q", "candidates": [{"id": f"i{i}", "image_base64": png} for i in range(9)]}, 413),
        }
        f_res = {}
        for name, (body, want) in f_cases.items():
            st, b, _ = self.gw.call("POST", "/v1/rerank/visual", body, timeout=120)
            f_res[name] = {"status": st, "want": want, "code": ((b or {}).get("error") or {}).get("code")}
        f_ok = all(v["status"] == v["want"] for v in f_res.values())
        # 8 images in one call (maximum)
        eight = PARITY_IMAGES + ["fig1_plot", "fig1_section"]
        st8, b8, ms8 = self.visual(visual_request(self.fx, q_plot["query"], eight))
        _, status, _ = self.gw.call("GET", "/status", timeout=30)
        placement = (((status or {}).get("backends") or {}).get("visual") or {}).get("placement")
        placement_ok = expected_placement is None or placement == expected_placement
        self.checks["4_visual"] = {
            "pass": bool(hits >= 4 and a_ok and b_ok and c_ok and d_ok and e_ok and f_ok and st8 == 200
                         and placement_ok),
            "sanity_top1_hits": f"{hits}/{len(per_query)}", "per_query": per_query,
            "A_spread": {"pass": a_ok, "scores": {k: round(v, 4) for k, v in sa.items()}},
            "B_permutation": {"pass": b_ok, "max_abs_diff_by_image": b_diff},
            "C_duplicate": {"pass": c_ok, "abs_diff": c_diff},
            "D_same_caption": {"pass": d_ok, "section_query_top1": (bd1 or {}).get("candidate_ids", [None])[0],
                               "plot_query_top1": (bd2 or {}).get("candidate_ids", [None])[0]},
            "E_blank_below_match": {"pass": e_ok},
            "F_negative_contract": {"pass": f_ok, "cases": f_res},
            "G_cyrillic_info": {"text_page_scores": {q: r["scores"]["text_page"] for q, r in per_query.items()
                                                     if r["scores"]}},
            "eight_images_call": {"status": st8, "ms": round(ms8, 1),
                                  "image_tokens": [r.get("image_tokens") for r in (b8 or {}).get("results", [])]},
            "status_placement": placement, "expected_placement": expected_placement,
            "model": {k: (first or {}).get(k) for k in ("model_id", "model_revision", "quant", "placement",
                                                        "backend_version", "weights_sha256", "license")}}

    # 5 ---------------------------------------------------------------------------------------------------------
    def check_concurrent(self, states: dict) -> None:
        q_plot = next(q for q in self.fx.manifest["visual_queries"] if q["expect"] == "plot")
        tq = self.fx.manifest["text_queries"][0]
        marks: dict[str, list] = {"visual": [], "text": []}
        pmon_cmd = ["nvidia-smi", "pmon", "-s", "u", "-d", "1", "-c", "150", "-o", "T"]
        if sh(["which", "stdbuf"])[0] == 0:
            pmon_cmd = ["stdbuf", "-oL"] + pmon_cmd     # line-buffered: nothing is lost on terminate
        pmon = subprocess.Popen(pmon_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)

        def run_visual():
            t0 = time.time()
            st, _, _ = self.visual(visual_request(self.fx, q_plot["query"], PARITY_IMAGES + ["fig1_plot",
                                                                                            "fig1_section"]))
            marks["visual"].append((t0, time.time(), st))

        def run_text():
            time.sleep(1.5)
            for _ in range(3):
                t0 = time.time()
                st, _, _ = self.text(text_request(self.fx, tq["query"]))
                marks["text"].append((t0, time.time(), st))

        threads = [threading.Thread(target=run_visual), threading.Thread(target=run_text)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        pmon.terminate()
        out = pmon.communicate(timeout=10)[0] if pmon else ""
        # rows: Time gpu pid type sm mem enc dec ... command ; one row per process and second
        by_second: dict[str, set] = {}
        for line in out.splitlines():
            r = line.split()
            if not r or line.startswith("#") or len(r) < 5 or not r[2].isdigit():
                continue
            if r[4] not in ("-", "0"):
                by_second.setdefault(r[0], set()).add(int(r[2]))
        pids = {states["text"]["pid"], states["m0"]["pid"]}
        both_active = sum(1 for s in by_second.values() if pids <= s)
        v_end = marks["visual"][0][1] if marks["visual"] else 0
        text_inside = all(end < v_end for _, end, _ in marks["text"])
        ok = all(st == 200 for *_, st in marks["visual"] + marks["text"]) and text_inside and len(marks["text"]) == 3
        self.checks["5_concurrent"] = {
            "pass": bool(ok), "text_finished_before_visual_end": text_inside,
            "visual_s": round(v_end - marks["visual"][0][0], 1) if marks["visual"] else None,
            "text_s": [round(e - s, 2) for s, e, _ in marks["text"]],
            "pmon_samples_both_pids_active": both_active,
            "note": "two processes, two CUDA contexts, time-sliced by the driver; no shared lock"}

    # 6 ---------------------------------------------------------------------------------------------------------
    def check_burst(self) -> None:
        results = {}
        for label, n_text, n_visual in (("burst_12", 8, 4), ("burst_16", 10, 6)):
            statuses, codes, lock = [], [], threading.Lock()
            vq = self.fx.manifest["visual_queries"]
            tq = self.fx.manifest["text_queries"]

            def fire(kind: str, i: int):
                if kind == "text":
                    st, b, _ = self.text(text_request(self.fx, tq[i % len(tq)]["query"]))
                else:
                    q = vq[i % len(vq)]
                    st, b, _ = self.visual(visual_request(self.fx, q["query"], [q["expect"], "blank"]))
                with lock:
                    statuses.append(st)
                    codes.append(((b or {}).get("error") or {}).get("code") if st != 200 else None)

            t0 = time.time()
            threads = [threading.Thread(target=fire, args=("text", i)) for i in range(n_text)] + \
                      [threading.Thread(target=fire, args=("visual", i)) for i in range(n_visual)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            bad = [(s, c) for s, c in zip(statuses, codes) if s != 200 and not (s == 503 and c == "RERANK_BACKEND_BUSY")]
            results[label] = {"n": len(statuses), "ok_200": statuses.count(200),
                              "busy_503": sum(1 for s, c in zip(statuses, codes) if s == 503 and c ==
                                              "RERANK_BACKEND_BUSY"),
                              "unexpected": bad, "wall_s": round(time.time() - t0, 1)}
        self.checks["6_burst"] = {"pass": all(not r["unexpected"] for r in results.values()), **results}


def load_token(args) -> str | None:
    if args.token_stdin:
        return sys.stdin.readline().strip() or None
    if args.token_file:
        return Path(args.token_file).read_text(encoding="utf-8").strip() or None
    env_file = os.environ.get("VKM_RERANK_TOKEN_FILE")
    if env_file:
        return Path(env_file).read_text(encoding="utf-8").strip() or None
    return os.environ.get("VKM_RERANK_TOKEN") or None


def cmd_run(args) -> int:
    token = load_token(args)
    run = Run(args, token)
    started = now_iso()
    names = {"text": args.text_container, "m0": args.m0_container, "gateway": args.gateway_container}
    before = {k: container_state(v) for k, v in names.items()}
    _, props_before, _ = run.m0.call("GET", "/props", auth=False, timeout=10)
    marker_before = (props_before or {}).get("media_marker")
    sampler = GpuSampler()
    sampler.start()
    time.sleep(1.0)
    t_start = time.time()
    # warm-up of the maximum size before the idle measurement (the gateway also warms up on every m0 start)
    run.visual(visual_request(run.fx, "прогрев", ["map_scheme"]))

    def guard(key: str, fn, *a) -> None:
        try:
            fn(*a)
        except Exception as exc:  # noqa: BLE001 - a crashed check is a failed check, the run goes on
            run.checks[key] = {"pass": False, "exception": f"{type(exc).__name__}: {exc}"[:300]}

    guard("1_both_loaded", run.check_loaded, before)
    guard("2_idle_vram", run.check_idle, sampler, before)
    guard("3_text", run.check_text)
    guard("4_visual", run.check_visual, args.expected_placement)
    guard("5_concurrent", run.check_concurrent, before)
    guard("6_burst", run.check_burst)
    t_end = time.time()
    time.sleep(2.0)
    sampler.stop()
    after = {k: container_state(v) for k, v in names.items()}
    _, props_after, _ = run.m0.call("GET", "/props", auth=False, timeout=10)
    # 7 no OOM
    logs = {k: container_logs_since(v, started) for k, v in names.items()}
    oom_lines = {k: [ln[:200] for ln in log.splitlines() if OOM_RE.search(ln)][:10] for k, log in logs.items()}
    window = sampler.window(t_start, t_end)
    summ = summarize_gpu(window)
    free_min = (summ.get("memory_free_mib") or {}).get("min")
    run.checks["7_no_oom"] = {"pass": not any(oom_lines.values()) and free_min is not None and
                              free_min >= FREE_VRAM_FLOOR_MIB, "oom_lines": oom_lines,
                              "min_free_mib": free_min, "max_used_mib": (summ.get("memory_used_mib") or {}).get("max"),
                              "floor_mib": FREE_VRAM_FLOOR_MIB, "usable_mib": USABLE_VRAM_MIB}
    # 8 no reload
    keys = ("started_at", "restart_count", "pid")
    same = {k: all(before[k].get(f) == after[k].get(f) for f in keys) for k in names}
    load_lines = [ln[:160] for ln in logs["m0"].splitlines() if LOAD_RE.search(ln)][:5]
    same_instance = marker_before is not None and marker_before == (props_after or {}).get("media_marker")
    run.checks["8_no_reload"] = {
        "pass": all(same.values()) and not load_lines and same_instance and not (props_after or {}).get("is_sleeping"),
        "unchanged": same, "before": {k: {f: before[k].get(f) for f in keys} for k in names},
        "after": {k: {f: after[k].get(f) for f in keys} for k in names}, "m0_load_lines_in_window": load_lines,
        "m0_same_instance": same_instance}
    # 9 latency
    run.checks["9_latency"] = {
        "pass": bool(run.latency["text"] and run.latency["visual"]),
        "client_ms": {k: lat_summary(v) for k, v in run.latency.items()},
        "gateway_phase_ms": {k: {p: lat_summary(v) for p, v in ph.items()} for k, ph in run.phases.items()},
        "gpu_window": summ}
    verdict = all(c["pass"] for c in run.checks.values())
    parity = None
    if args.parity_receipt and Path(args.parity_receipt).is_file():
        pr = json.loads(Path(args.parity_receipt).read_text(encoding="utf-8"))
        parity = {"verdict": pr.get("verdict"), "max_abs_diff": pr.get("max_abs_diff"),
                  "min_spearman": pr.get("min_spearman"), "file": Path(args.parity_receipt).name}
    receipt = {
        "receipt": "vkm-rerank-acceptance/1", "created_utc": now_iso(), "window_utc": [started, now_iso()],
        "verdict": "PASS" if verdict else "FAIL", "checks": run.checks, "parity": parity,
        "containers": {k: {f: after[k].get(f) for f in ("image_id", "image_ref", "status", "health")} for k in names},
        "fixtures": {"set": run.fx.manifest.get("fixture_set"), "images_sha256": run.fx.sha()},
        "http_status_counts": {str(s): run.statuses.count(s) for s in sorted(set(run.statuses))},
        "gpu_whole_run": summarize_gpu(sampler.samples),
        "note": "roles only; no addresses, secrets or document text",
    }
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"acceptance_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"verdict": receipt["verdict"], "receipt": path.name,
                      "checks": {k: v["pass"] for k, v in run.checks.items()}}, ensure_ascii=False))
    return 0 if verdict else 1


# ------------------------------------------------------------------------------------------------------------ ptext
PTEXT_SENTENCE = ("Наблюдения за оседанием земной поверхности над калийным рудником выполняют по реперам "
                  "профильных линий, результаты сравнивают с прогнозом. ")


def cmd_ptext(args) -> int:
    """VRAM of the text service after listwise requests of ≈1K/2K/3K/4K tokens (budget 4096 enforced by it)."""
    svc = Http(args.service)
    st, ready, _ = svc.call("GET", "/readyz", auth=False, timeout=10)
    if st != 200:
        print("text service not ready", file=sys.stderr)
        return 2
    runtime = ready["runtime"]
    state = container_state(args.text_container)
    pid = state["pid"]
    n_docs = MAX_TEXT_CANDIDATES

    def request(reps: int, budget: int):
        docs = [f"Документ {i}. " + PTEXT_SENTENCE * reps for i in range(n_docs)]
        body = {"query": "оседание земной поверхности", "documents": docs, "top_n": 3, "token_budget": budget,
                "expected_runtime": runtime}
        return svc.call("POST", "/v1/rerank", body, auth=False, timeout=300)

    # calibration with tiny prompts (≈ 1K tokens): tokens = base + slope · reps
    steps = [{"step": "vram_before", "text_service_vram_mib": gpu_apps().get(pid)}]
    st1, r1, _ = request(1, TEXT_TOKEN_BUDGET)
    st2, r2, _ = request(2, TEXT_TOKEN_BUDGET)
    if st1 != 200 or st2 != 200:
        print(f"calibration failed: {st1} {st2}", file=sys.stderr)
        return 3
    t1, t2 = r1["usage"]["total_tokens"], r2["usage"]["total_tokens"]
    slope, base = t2 - t1, t1 - (t2 - t1)
    for target in [int(t) for t in args.targets.split(",")]:
        budget = min(target, TEXT_TOKEN_BUDGET)      # the service refuses anything above it before inference
        reps = max(1, (budget - base) // slope)
        st, resp, ms = request(reps, budget)
        while st == 413 and reps > 1:
            reps -= 1
            st, resp, ms = request(reps, budget)
        time.sleep(1.0)
        apps = gpu_apps()
        steps.append({"target_tokens": target, "status": st,
                      "total_tokens": (resp or {}).get("usage", {}).get("total_tokens") if st == 200 else None,
                      "latency_ms": round(ms, 1), "text_service_vram_mib": apps.get(pid),
                      "gpu_used_mib": sum(v for v in apps.values() if v > 0)})
        print(json.dumps(steps[-1], ensure_ascii=False))
    after = container_state(args.text_container)
    receipt = {"receipt": "vkm-rerank-ptext/1", "created_utc": now_iso(), "steps": steps,
               "p_text_mib": max((s.get("text_service_vram_mib") or 0) for s in steps),
               "service_unchanged": all(state.get(f) == after.get(f) for f in ("started_at", "restart_count", "pid")),
               "runtime": runtime, "note": "requests above the budget are rejected by the service before inference"}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"ptext_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"p_text_mib": receipt["p_text_mib"], "service_unchanged": receipt["service_unchanged"]}))
    return 0


def cmd_snapshot(args) -> int:
    snap = {"created_utc": now_iso(), "containers": {n: container_state(n) for n in args.containers},
            "gpu_apps_mib": gpu_apps()}
    rc, out = sh(["nvidia-smi", f"--query-gpu={','.join(GPU_FIELDS[:3])}", "--format=csv,noheader,nounits"])
    snap["gpu"] = out.strip()
    print(json.dumps(snap, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="acceptance.py", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="the 9 acceptance checks")
    r.add_argument("--gateway", default="http://127.0.0.1:18084")
    r.add_argument("--text-service", default="http://127.0.0.1:18082")
    r.add_argument("--m0", default="http://127.0.0.1:18083")
    r.add_argument("--fixtures", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--token-stdin", action="store_true")
    r.add_argument("--token-file", default=None)
    r.add_argument("--text-container", default="careerops-reranker")
    r.add_argument("--m0-container", default="vkm-rerank-m0")
    r.add_argument("--gateway-container", default="vkm-rerank-gateway")
    r.add_argument("--expected-placement", default=None)
    r.add_argument("--parity-receipt", default=None)
    r.set_defaults(func=cmd_run)
    t = sub.add_parser("ptext", help="VRAM of the text service vs request size")
    t.add_argument("--service", default="http://127.0.0.1:18082")
    t.add_argument("--text-container", default="careerops-reranker")
    t.add_argument("--targets", default="1024,2048,3072,4096")
    t.add_argument("--out", required=True)
    t.set_defaults(func=cmd_ptext)
    s = sub.add_parser("snapshot", help="container states and GPU memory")
    s.add_argument("--containers", nargs="+", default=["careerops-reranker", "vkm-rerank-m0", "vkm-rerank-gateway"])
    s.set_defaults(func=cmd_snapshot)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
