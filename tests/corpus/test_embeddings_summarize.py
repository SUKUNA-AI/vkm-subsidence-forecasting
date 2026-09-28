"""Report tables from synthetic bench results (model matrix, parity, residency modes)."""
from __future__ import annotations

from vkm_corpus.embeddings.summarize import (
    model_row,
    model_table,
    pair_rows,
    pair_table,
    parity_rows,
    parity_table,
    verdict_rows,
    verdict_table,
)


def _model_result(key="m", quant="Q8_0"):
    return {"key": key, "quant": quant, "status": "OK", "load_s": 1.0, "variant": "vk-mesa25",
            "resident_after_load": {"per_process": {key: {"vram_mib": 170.5, "gtt_mib": 40.0, "rss_mb": 300.0}}},
            "parity": {"dense": {"query_vectors": {"cos_mean": 0.9999, "cos_min": 0.999},
                                 "doc_vectors": {"cos_mean": 0.9998, "cos_p1": 0.9996, "cos_min": 0.9990, "n": 10},
                                 "ranking": {"top10_overlap": 0.98, "top50_overlap": 0.99, "spearman_top100": 0.995,
                                             "kendall_top100": 0.96, "ndcg10_agreement": 0.99},
                                 "gate": {"verdict": "PASS"}}},
            "query_latency_sequential": {"p50_ms": 10.0, "p95_ms": 12.0},
            "doc_throughput": {"docs_per_s": 80.0, "tokens_per_s": 9000.0, "gpu_busy_mean_pct": 95.0}}


def test_model_and_parity_tables():
    r = _model_result()
    row = model_row(r)
    assert row["vram_mib"] == 170.5 and row["gate"] == "PASS" and row["p95_ms"] == 12.0
    table = model_table([row, model_row(_model_result(quant="Q6_K"))])
    lines = table.splitlines()
    assert lines[2].startswith("| m | Q8_0 |") and lines[3].startswith("| m | Q6_K |")
    ptab = parity_table(parity_rows(r))
    assert "| dense |" in ptab and "PASS" in ptab


def test_verdicts_need_dev_and_canary_pass_on_the_production_backend():
    def res(key, quant, variant, verdict, section="dense"):
        r = _model_result(key, quant)
        r["variant"] = variant
        r["parity"] = {section: {**r["parity"]["dense"], "gate": {"verdict": verdict, "checks": {
            "cos_mean": True, "cos_p1": verdict == "PASS", "top10_overlap": True}}}}
        return r

    rows = verdict_rows([
        res("a", "Q8_0", "vk-mesa25", "PASS"), res("a", "Q8_0", "canary", "PASS"),
        res("b", "Q8_0", "vk-mesa25", "PASS"), res("b", "Q8_0", "canary", "FAIL"),
        res("c", "Q6_K", "vk-mesa25", "PASS"),
        res("d", "Q8_0", "vk-mesa25", "FAIL", "late"), res("d", "Q8_0", "canary", "PASS", "late"),
        res("e", "Q8_0", "hip", "PASS"), res("e", "Q8_0", "canary", "PASS"),   # research backend is not a verdict
    ])
    got = {(r["model"], r["output"]): r["verdict"] for r in rows}
    assert got == {("a", "dense"): "FINAL_CORPUS_ENCODER_APPROVED", ("b", "dense"): "NOT_APPROVED",
                   ("c", "dense"): "PENDING_CANARY", ("d", "late"): "NOT_APPROVED", ("e", "dense"): "PENDING"}
    table = verdict_table(rows)
    assert "FAIL (cos_p1)" in table and "not run" in table and "**FINAL_CORPUS_ENCODER_APPROVED**" in table


def test_pair_rows_compute_per_process_gpu_seconds():
    snap = lambda ns_d, ns_l: {"device": {"vram_used_mib": 400.0}, "per_process": {  # noqa: E731
        "d": {"vram_mib": 170.0, "engine_ns": {"compute": ns_d}, "rss_mb": 1.0},
        "l": {"vram_mib": 200.0, "engine_ns": {"compute": ns_l}, "rss_mb": 1.0}}}
    run = {"seconds": 30.0, "gpu": {"vram_peak_mib": 420.0, "gpu_busy_mean_pct": 90.0},
           "per_model": {"d": {"queries": {"p50_ms": 10, "p95_ms": 20}, "query_qps": 50, "docs_per_s": 0, "errors": 0},
                         "l": {"queries": {"p50_ms": 12, "p95_ms": 25}, "query_qps": 40, "docs_per_s": 0, "errors": 0}}}
    res = {"dense": {"key": "d"}, "late": {"key": "l"},
           "modes": {"C": {"idle": snap(0, 0), "run": run, "after": snap(10 * 10 ** 9, 12 * 10 ** 9)}}}
    rows = pair_rows(res)
    assert rows[0]["mode"] == "C" and rows[0]["dense_gpu_s"] == 10.0 and rows[0]["late_gpu_s"] == 12.0
    assert "10/20" in pair_table(rows) and "10.0/12.0" in pair_table(rows)
