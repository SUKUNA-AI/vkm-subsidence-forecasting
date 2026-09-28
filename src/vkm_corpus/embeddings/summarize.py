"""Tables for the RX580 reports from bench result JSON files (model matrix §26, residency §27)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

QUANT_ORDER = {"Q8_0": 0, "Q6_K": 1, "Q5_K_M": 2, "F16": 3}


def _g(d: Any, *path, default=None):
    for p in path:
        if not isinstance(d, dict):
            return default
        d = d.get(p)
    return default if d is None else d


def model_row(r: dict[str, Any]) -> dict[str, Any]:
    key = r["key"]
    per = _g(r, "resident_after_load", "per_process", key, default={}) or {}
    par = r.get("parity", {})
    pd = par.get("dense") or par.get("late") or {}
    vec = pd.get("doc_vectors") or pd.get("doc_tokens") or {}
    rk = pd.get("ranking", {})
    lat = r.get("query_latency_sequential", {})
    thr = r.get("doc_throughput", {})
    cold = r.get("cold_after_idle") or []
    return {
        "model": key, "quant": r.get("quant"), "variant": r.get("variant", "vk-mesa25"), "status": r.get("status"),
        "load_s": r.get("load_s"),
        "vram_mib": per.get("vram_mib"), "gtt_mib": per.get("gtt_mib"), "rss_mb": per.get("rss_mb"),
        "cos_mean": vec.get("cos_mean"), "cos_p1": vec.get("cos_p1"), "top10": rk.get("top10_overlap"),
        "top50": rk.get("top50_overlap"), "spearman": rk.get("spearman_top100"),
        "gate": _g(pd, "gate", "verdict"), "p50_ms": lat.get("p50_ms"), "p95_ms": lat.get("p95_ms"),
        "docs_s": thr.get("docs_per_s"), "tok_s": thr.get("tokens_per_s"), "busy": thr.get("gpu_busy_mean_pct"),
        "cold_ms": [c.get("ms") for c in cold], "cold_state": [c.get("state_before") for c in cold],
        "error": r.get("error"),
    }


def fmt(v: Any, nd: int = 4) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{nd}f}" if abs(v) < 10 else f"{v:.1f}"
    if isinstance(v, list):
        return "/".join(fmt(x, 1) for x in v) if v else "—"
    return str(v)


def model_table(rows: list[dict[str, Any]]) -> str:
    head = ("| Model | Quant | Backend | Load s | VRAM MiB (proc) | Embed | cos mean | cos p1 | top-10 | top-50 | Spearman "
            "| Gate | q p50 ms | q p95 ms | docs/s | tok/s | busy % |")
    sep = "|" + "---|" * 17
    out = [head, sep]
    for r in sorted(rows, key=lambda r: (r["model"], QUANT_ORDER.get(r["quant"], 9), r["variant"])):
        out.append("| " + " | ".join([
            r["model"], fmt(r["quant"]), r["variant"], fmt(r["load_s"], 2), fmt(r["vram_mib"], 1),
            "yes" if r["status"] == "OK" else "FAIL", fmt(r["cos_mean"], 5), fmt(r["cos_p1"], 5), fmt(r["top10"], 3),
            fmt(r["top50"], 3), fmt(r["spearman"], 4), fmt(r["gate"]), fmt(r["p50_ms"], 1), fmt(r["p95_ms"], 1),
            fmt(r["docs_s"], 1), fmt(r["tok_s"], 0), fmt(r["busy"], 1)]) + " |")
    return "\n".join(out)


def parity_rows(r: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per parity section (dense, late) of a model result."""
    out = []
    for section, pd in (r.get("parity") or {}).items():
        q = pd.get("query_vectors") or pd.get("query_tokens") or {}
        d = pd.get("doc_vectors") or pd.get("doc_tokens") or {}
        rk = pd.get("ranking", {})
        out.append({"model": r["key"], "quant": r.get("quant"), "variant": r.get("variant", "vk-mesa25"),
                    "output": section, "q_cos_mean": q.get("cos_mean"), "q_cos_min": q.get("cos_min"),
                    "d_cos_mean": d.get("cos_mean"), "d_cos_p1": d.get("cos_p1"), "d_cos_min": d.get("cos_min"),
                    "n_docs": d.get("n"), "top10": rk.get("top10_overlap"), "top50": rk.get("top50_overlap"),
                    "spearman": rk.get("spearman_top100"), "kendall": rk.get("kendall_top100"),
                    "ndcg10": rk.get("ndcg10_agreement"), "gate": _g(pd, "gate", "verdict")})
    return out


def parity_table(rows: list[dict[str, Any]]) -> str:
    head = ("| Model | Quant | Backend | Output | q cos mean | q cos min | doc cos mean | doc cos p1 | doc cos min | top-10 | "
            "top-50 | Spearman | Kendall | nDCG@10 agr. | Gate |")
    out = [head, "|" + "---|" * 15]
    for r in sorted(rows, key=lambda r: (r["model"], QUANT_ORDER.get(r["quant"], 9), r["variant"], r["output"])):
        out.append("| " + " | ".join([
            r["model"], fmt(r["quant"]), r["variant"], r["output"], fmt(r["q_cos_mean"], 5), fmt(r["q_cos_min"], 5),
            fmt(r["d_cos_mean"], 5), fmt(r["d_cos_p1"], 5), fmt(r["d_cos_min"], 5), fmt(r["top10"], 3),
            fmt(r["top50"], 3), fmt(r["spearman"], 4), fmt(r["kendall"], 4), fmt(r["ndcg10"], 4),
            fmt(r["gate"])]) + " |")
    return "\n".join(out)


PRODUCTION_VARIANT = "vk-mesa25"      # Vulkan/RADV Mesa 25 (host release) — the backend the verdict is about
CANARY_VARIANT = "canary"             # same backend, parity probe drawn from the canary canon


def verdict_rows(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """§31 verdict per (model, quant, output): FINAL_CORPUS_ENCODER_APPROVED only when the pre-registered gate
    passes on the dev-canon probe AND on the canary-canon probe with the production backend; one FAIL →
    NOT_APPROVED; canary not run → PENDING_CANARY. Research variants (hip, mesa26, bulk, …) are not considered."""
    probes: dict[tuple[str, str, str], dict[str, dict[str, Any]]] = {}
    for r in results:
        variant = r.get("variant", PRODUCTION_VARIANT)
        if variant not in (PRODUCTION_VARIANT, CANARY_VARIANT):
            continue
        probe = "canary" if variant == CANARY_VARIANT else "dev"
        for section, pd in (r.get("parity") or {}).items():
            probes.setdefault((r["key"], r.get("quant") or "", section), {})[probe] = pd
    rows = []
    for (key, quant, section), p in probes.items():
        g = {name: _g(pd, "gate", "verdict") for name, pd in p.items()}
        if "FAIL" in g.values():
            verdict = "NOT_APPROVED"
        elif g.get("dev") == "PASS" and g.get("canary") == "PASS":
            verdict = "FINAL_CORPUS_ENCODER_APPROVED"
        elif g.get("dev") == "PASS":
            verdict = "PENDING_CANARY"
        else:
            verdict = "PENDING"
        row = {"model": key, "quant": quant, "output": section, "verdict": verdict}
        for name in ("dev", "canary"):
            pd = p.get(name) or {}
            d = pd.get("doc_vectors") or pd.get("doc_tokens") or {}
            rk = pd.get("ranking", {})
            row.update({f"{name}_gate": g.get(name), f"{name}_n": d.get("n"), f"{name}_cos_p1": d.get("cos_p1"),
                        f"{name}_top10": rk.get("top10_overlap"), f"{name}_spearman": rk.get("spearman_top100"),
                        f"{name}_failed": [c for c, ok in (_g(pd, "gate", "checks", default={}) or {}).items()
                                           if ok is False]})
        rows.append(row)
    rows.sort(key=lambda r: (r["model"], QUANT_ORDER.get(r["quant"], 9), r["output"]))
    return rows


def verdict_table(rows: list[dict[str, Any]]) -> str:
    head = ("| Model | Quant | Output | dev: n / cos p1 / top-10 / Spearman | dev gate | canary: n / cos p1 / top-10 / "
            "Spearman | canary gate | Verdict |")
    out = [head, "|" + "---|" * 8]
    for r in rows:
        cells = []
        for name in ("dev", "canary"):
            if r[f"{name}_gate"] is None:
                cells += ["—", "not run"]
                continue
            cells.append(f"{fmt(r[f'{name}_n'])} / {fmt(r[f'{name}_cos_p1'], 5)} / {fmt(r[f'{name}_top10'], 3)} / "
                         f"{fmt(r[f'{name}_spearman'], 4)}")
            failed = r[f"{name}_failed"]
            cells.append(r[f"{name}_gate"] + (f" ({', '.join(failed)})" if failed else ""))
        out.append("| " + " | ".join([r["model"], r["quant"], r["output"], *cells, f"**{r['verdict']}**"]) + " |")
    return "\n".join(out)


def _engine_s(snap_before: dict, snap_after: dict, key: str) -> float | None:
    a = _g(snap_before, "per_process", key, "engine_ns") or {}
    b = _g(snap_after, "per_process", key, "engine_ns") or {}
    if not a or not b:
        return None
    return sum(b.get(e, 0) - a.get(e, 0) for e in ("compute", "gfx")) / 1e9


def pair_rows(r: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per residency mode of a pair result (MODE A/B/C/D/D_bg)."""
    dk, lk = r["dense"]["key"], r["late"]["key"]
    rows = []
    modes = r.get("modes", {})
    prev_after = None
    for mode in ("A", "B", "C", "D", "D_bg", "D_bg_sep"):
        m = modes.get(mode)
        if not m:
            continue
        run = m.get("run", {})
        idle = m.get("idle") or prev_after or {}
        after = m.get("after", {})
        per = run.get("per_model", {})
        row = {"pair": f"{dk} + {lk}", "mode": mode, "seconds": run.get("seconds"),
               "idle_vram_mib": _g(idle, "device", "vram_used_mib"),
               "peak_vram_mib": _g(run, "gpu", "vram_peak_mib"),
               "busy_mean": _g(run, "gpu", "gpu_busy_mean_pct")}
        for role, key in (("dense", dk), ("late", lk)):
            pm = per.get(key)
            if pm is None:
                continue
            row[f"{role}_vram_mib"] = _g(after, "per_process", key, "vram_mib")
            row[f"{role}_p50"] = _g(pm, "queries", "p50_ms")
            row[f"{role}_p95"] = _g(pm, "queries", "p95_ms")
            row[f"{role}_qps"] = pm.get("query_qps")
            row[f"{role}_docs_s"] = pm.get("docs_per_s")
            row[f"{role}_errors"] = pm.get("errors")
            row[f"{role}_gpu_s"] = _engine_s(idle, after, key)
            row[f"{role}_rss_mb"] = _g(after, "per_process", key, "rss_mb")
            c0, c1 = _g(idle, "per_process", key, "cpu_s"), _g(after, "per_process", key, "cpu_s")
            row[f"{role}_cpu_s"] = round(c1 - c0, 2) if (c0 is not None and c1 is not None) else None
        bgm = per.get("bg_docs")
        row["bg_docs_s"] = bgm.get("docs_per_s") if bgm else None
        rows.append(row)
        prev_after = after
    return rows


def pair_table(rows: list[dict[str, Any]]) -> str:
    head = ("| Pair | Mode | idle VRAM | peak VRAM | dense VRAM | late VRAM | dense p50/p95 ms | late p50/p95 ms | "
            "dense q/s | late q/s | docs/s (dense / 3rd proc) | GPU busy % | GPU-s dense/late | CPU-s dense/late | "
            "RSS MB dense/late | errors |")
    out = [head, "|" + "---|" * 16]
    for r in rows:
        out.append("| " + " | ".join([
            r["pair"], r["mode"], fmt(r.get("idle_vram_mib"), 1), fmt(r.get("peak_vram_mib"), 1),
            fmt(r.get("dense_vram_mib"), 1), fmt(r.get("late_vram_mib"), 1),
            f"{fmt(r.get('dense_p50'), 1)}/{fmt(r.get('dense_p95'), 1)}",
            f"{fmt(r.get('late_p50'), 1)}/{fmt(r.get('late_p95'), 1)}",
            fmt(r.get("dense_qps"), 1), fmt(r.get("late_qps"), 1),
            f"{fmt(r.get('dense_docs_s'), 1)} / {fmt(r.get('bg_docs_s'), 1)}",
            fmt(r.get("busy_mean"), 1), f"{fmt(r.get('dense_gpu_s'), 1)}/{fmt(r.get('late_gpu_s'), 1)}",
            f"{fmt(r.get('dense_cpu_s'), 1)}/{fmt(r.get('late_cpu_s'), 1)}",
            f"{fmt(r.get('dense_rss_mb'), 0)}/{fmt(r.get('late_rss_mb'), 0)}",
            str((r.get("dense_errors") or 0) + (r.get("late_errors") or 0))]) + " |")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("results", nargs="+")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--parity", action="store_true", help="parity table (dense and late outputs separately)")
    ap.add_argument("--verdicts", action="store_true", help="§31 verdicts: dev + canary probes, production backend")
    ap.add_argument("--variants", default="", help="comma-separated variants for the matrix/parity tables (default all)")
    ap.add_argument("--models", default="", help="comma-separated model keys for the matrix/parity tables (default all)")
    ap.add_argument("--quants", default="", help="comma-separated quantizations for the matrix/parity tables")
    a = ap.parse_args(argv)
    keep = {v for v in a.variants.split(",") if v}
    keep_models = {m for m in a.models.split(",") if m}
    keep_quants = {q for q in a.quants.split(",") if q}
    rows, prows, par, raw = [], [], [], []
    for f in a.results:
        p = Path(f)
        files = sorted(p.glob("model_*.json")) if p.is_dir() else ([p] if p.name.startswith("model_") else [])
        for fp in files:
            r = json.loads(fp.read_text(encoding="utf-8"))
            raw.append(r)
            if keep and r.get("variant", PRODUCTION_VARIANT) not in keep:
                continue
            if keep_models and r.get("key") not in keep_models:
                continue
            if keep_quants and r.get("quant") not in keep_quants:
                continue
            rows.append(model_row(r))
            par.extend(parity_rows(r))
        pfiles = sorted(p.glob("pair_*.json")) if p.is_dir() else ([p] if p.name.startswith("pair_") else [])
        for fp in pfiles:
            prows.extend(pair_rows(json.loads(fp.read_text(encoding="utf-8"))))
    if a.json:
        print(json.dumps({"models": rows, "parity": par, "pairs": prows, "verdicts": verdict_rows(raw)}, indent=1,
                         ensure_ascii=False))
    elif a.verdicts:
        print(verdict_table(verdict_rows(raw)))
    elif a.parity:
        print(parity_table(par))
    else:
        if rows:
            print(model_table(rows))
        if prows:
            print()
            print(pair_table(prows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
