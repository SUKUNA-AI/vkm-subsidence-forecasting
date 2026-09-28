"""Metrics, fusion and exact vector search of the retrieval lab (agent J) — pure numpy, no models."""
from __future__ import annotations

import math

import numpy as np
import pytest

from vkm_corpus.retrieval_lab import fusion, metrics
from vkm_corpus.retrieval_lab.vectors import DenseIndex, LateIndex, SparseIndex, bytes_per_vector, maxsim, quantize, truncate


def test_page_mapping_keeps_first_occurrence():
    unit_page = {"u1": "P1", "u2": "P2", "u3": "P1", "u4": None, "u5": "P3"}
    assert metrics.to_pages(["u1", "u2", "u3", "u4", "u5"], unit_page) == ["P1", "P2", "P3"]
    assert metrics.to_objects(["a", "b"], {"a": ["o1", "o2"], "b": ["o2", "o3"]}) == ["o1", "o2", "o3"]


def test_ndcg_recall_mrr_precision_known_values():
    judged = {"A": 3, "B": 2, "C": 1, "Z": 0}
    ranked = ["Z", "B", "A", "X"]
    dcg = 2 / math.log2(3) + 3 / math.log2(4)
    idcg = 3 + 2 / math.log2(3) + 1 / math.log2(4)
    assert metrics.ndcg_at(ranked, judged, 10) == pytest.approx(dcg / idcg)
    m = metrics.query_metrics(ranked, judged, threshold=2, hard_negatives={"Z"})
    assert m["recall@10"] == 1.0 and m["mrr@10"] == pytest.approx(0.5)
    assert m["p@5"] == pytest.approx(2 / 5) and m["judged@10"] == pytest.approx(3 / 4)
    assert m["hn_above_first_rel@10"] == 1.0
    lenient = metrics.query_metrics(["C"], judged, threshold=1)
    assert lenient["recall@10"] == pytest.approx(1 / 3)


def test_evaluate_excludes_queries_without_relevant_items_and_groups():
    ev = metrics.evaluate({"q1": ["a"], "q2": ["b"], "q3": ["c"]},
                          {"q1": {"a": 3}, "q2": {"b": 1}, "q3": {"x": 2}})
    assert ev.excluded == ["q2"]
    assert ev.mean()["recall@10"] == pytest.approx(0.5) and ev.mean()["n_queries"] == 2
    assert ev.by_group({"g": ["q1"]})["g"]["ndcg@10"] == pytest.approx(1.0)


def test_significance_is_deterministic_and_sensible():
    a = {f"q{i}": 0.8 for i in range(30)}
    b = {f"q{i}": 0.5 for i in range(30)}
    t1, t2 = metrics.paired_randomization(a, b), metrics.paired_randomization(a, b)
    assert t1 == t2 and t1["p_value"] < 0.01 and t1["delta"] == pytest.approx(0.3)
    ci = metrics.bootstrap_ci(a, b)
    assert ci["lo"] == pytest.approx(0.3) and ci["hi"] == pytest.approx(0.3)
    same = metrics.paired_randomization(a, a)
    assert same["p_value"] == pytest.approx(1.0)


def test_rrf_and_weighted_fusion():
    r1 = [("a", 10.0), ("b", 5.0), ("c", 1.0)]
    r2 = [("b", 0.9), ("d", 0.8)]
    fused = fusion.rrf({"bm25": r1, "dense": r2}, k=60)
    assert [x for x, _ in fused][:2] == ["b", "a"]
    assert dict(fused)["b"] == pytest.approx(1 / 62 + 1 / 61)
    w = fusion.weighted({"bm25": r1, "dense": r2}, {"bm25": 0.5, "dense": 0.5})
    assert w[0][0] == "b"
    assert fusion.rrf({"x": r1}, weights={"x": 0.0}) == []
    grid = fusion.weight_grid(["a", "b", "c"], step=0.5)
    assert len(grid) == 6 and all(sum(g.values()) == pytest.approx(1.0) for g in grid)


def test_dense_index_exact_search_truncation_and_precisions():
    rng = np.random.default_rng(0)
    vecs = rng.standard_normal((50, 32)).astype(np.float32)
    ids = [f"u{i:02d}" for i in range(50)]
    index = DenseIndex.build(ids, vecs)
    hits = index.search(vecs[7], k=3)
    assert hits[0][0] == "u07" and hits[0][1] == pytest.approx(1.0, abs=1e-5)
    small = DenseIndex.build(ids, vecs, dim=16)
    assert small.dim == 16 and small.search(vecs[7], k=1)[0][0] == "u07"
    for precision in ("fp16", "int8", "binary"):
        assert DenseIndex.build(ids, vecs, precision=precision).search(vecs[7], k=1)[0][0] == "u07"
    assert np.allclose(np.linalg.norm(truncate(vecs, 8), axis=1), 1.0)
    assert np.abs(quantize(vecs, "int8") - vecs).max() < 0.05 * np.abs(vecs).max()
    assert bytes_per_vector(1024, "binary") == 128 and bytes_per_vector(768, "fp16") == 1536


def test_ties_break_on_id():
    index = DenseIndex.build(["b", "a", "c"], np.array([[1, 0], [1, 0], [0, 1]], dtype=np.float32))
    assert [u for u, _ in index.search(np.array([1, 0]), k=2)] == ["a", "b"]


def test_sparse_and_late_interaction():
    sp = SparseIndex.build(["d1", "d2"], [{"соль": 1.0, "ползуч": 0.5}, {"соль": 0.2}])
    assert [u for u, _ in sp.search({"ползуч": 1.0, "соль": 1.0})] == ["d1", "d2"]
    q = np.eye(4, dtype=np.float32)[:2]
    d_good = np.eye(4, dtype=np.float32)[:3]
    d_bad = np.eye(4, dtype=np.float32)[2:]
    assert maxsim(q, d_good) == pytest.approx(2.0) and maxsim(q, d_bad) == pytest.approx(0.0)
    late = LateIndex.build(["good", "bad"], [d_good, d_bad])
    assert late.score(q)[0][0] == "good"
    assert late.score(q, candidates=["bad"]) == [("bad", 0.0)]
    st = late.storage("fp16")
    assert st["tokens_total"] == 5 and st["bytes"] == 5 * 4 * 2
