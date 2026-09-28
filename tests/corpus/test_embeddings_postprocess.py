"""Post-processing (pooling, Matryoshka, int8, ColBERT, BGE-M3 sparse, MaxSim) and parity metrics (§30)."""
from __future__ import annotations

import dataclasses

import pytest

np = pytest.importorskip("numpy")

from vkm_corpus.embeddings import parity  # noqa: E402
from vkm_corpus.embeddings import postprocess as pp  # noqa: E402
from vkm_corpus.embeddings.specs import get  # noqa: E402


def test_pooling_modes_respect_the_mask():
    H = np.arange(12, dtype=np.float32).reshape(4, 3)
    assert pp.pool(H, "cls").tolist() == [0, 1, 2]
    assert pp.pool(H, "last").tolist() == [9, 10, 11]
    assert pp.pool(H, "last", [True, True, False, False]).tolist() == [3, 4, 5]
    np.testing.assert_allclose(pp.pool(H, "mean", [False, True, True, False]), [4.5, 5.5, 6.5])
    with pytest.raises(ValueError):
        pp.pool(H, "max")


def test_matryoshka_truncation_renormalises_only_normalised_specs():
    v = np.array([3.0, 4.0, 12.0], dtype=np.float32)
    out = pp.finalize_dense(get("granite-311m-r2"), v, dim=2).vector
    np.testing.assert_allclose(out, [0.6, 0.8], rtol=1e-6)
    raw = pp.finalize_dense(dataclasses.replace(get("mdenseon")), v).vector
    np.testing.assert_allclose(raw, v)                   # mDenseOn has no Normalize module


def test_pplx_int8_transform():
    spec = get("pplx-embed-0.6b")
    d = pp.finalize_dense(spec, np.array([0.0, 10.0, -10.0, 0.5], dtype=np.float32))
    assert d.int8.dtype == np.int8 and d.int8.tolist() == [0, 127, -127, int(np.rint(np.tanh(0.5) * 127))]
    np.testing.assert_allclose(d.vector, np.tanh([0.0, 10.0, -10.0, 0.5]), rtol=1e-6)
    t = pp.finalize_dense(spec, np.ones(4, np.float32), dim=2)
    assert t.vector.shape == (2,) and t.int8.shape == (2,)


def test_colbert_tokens_keep_head_normalise_and_prefix():
    spec = get("jina-colbert-v2")
    rng = np.random.default_rng(0)
    H = rng.standard_normal((5, 6)).astype(np.float32)
    W = rng.standard_normal((4, 6)).astype(np.float32)
    keep = [True, False, True, True, False]
    out = pp.colbert_tokens(spec, H, keep, head=W)
    assert out.shape == (3, 4)
    np.testing.assert_allclose(np.linalg.norm(out, axis=1), 1.0, rtol=1e-5)
    np.testing.assert_allclose(out[0], pp.l2_normalize(W @ H[0]), rtol=1e-5)
    short = pp.colbert_tokens(spec, H, keep, head=W, dim=2)
    assert short.shape == (3, 2)
    np.testing.assert_allclose(np.linalg.norm(short, axis=1), 1.0, rtol=1e-5)
    with pytest.raises(ValueError):
        pp.colbert_tokens(get("granite-97m-r2"), H, None)


def test_bge_m3_sparse_max_per_token_and_specials_removed():
    H = np.array([[1.0, 0.0], [0.0, 1.0], [2.0, 0.0], [0.0, -5.0]], dtype=np.float32)
    w, b = np.array([[1.0, 1.0]], np.float32), np.array([0.0], np.float32)
    sp = pp.bge_m3_sparse(H, [0, 7, 7, 9], w, b, unused_ids=(0, 1, 2, 3))
    assert sp.token_ids.tolist() == [7] and sp.weights.tolist() == [2.0]   # id 0 special, id 9 relu→0


def test_context_chunks_and_maxsim():
    spec = get("pplx-embed-context-0.6b")
    H = np.array([[1, 0], [3, 0], [9, 9], [0, 2]], dtype=np.float32)
    chunks = pp.context_chunks(spec, H, [(0, 2), (3, 4), (4, 4)])
    np.testing.assert_allclose(chunks[0].vector, np.tanh([2.0, 0.0]), rtol=1e-6)
    np.testing.assert_allclose(chunks[2].vector, [0.0, 0.0])
    Q = pp.l2_normalize(np.array([[1, 0], [0, 1]], np.float32))
    D = pp.l2_normalize(np.array([[1, 0], [1, 1]], np.float32))
    assert pp.maxsim(Q, D) == pytest.approx(1.0 + np.sqrt(0.5))
    assert pp.maxsim(Q, np.zeros((0, 2), np.float32)) == 0.0


def test_rank_correlations():
    a = [1, 2, 3, 4, 5]
    assert parity.spearman(a, a) == pytest.approx(1.0)
    assert parity.spearman(a, a[::-1]) == pytest.approx(-1.0)
    assert parity.kendall(a, [1, 2, 3, 5, 4]) == pytest.approx(0.8)
    assert parity.ndcg_at_k([3, 1, 2], {1: 3.0, 2: 1.0}, 3) < 1.0
    assert parity.ndcg_at_k([1, 2, 3], {1: 3.0, 2: 1.0}, 3) == pytest.approx(1.0)
    assert parity.recall_at_k([1, 2, 3], {2, 9}, 2) == 0.5


def test_dense_parity_identical_and_perturbed():
    rng = np.random.default_rng(1)
    q = rng.standard_normal((6, 16)).astype(np.float32)
    d = rng.standard_normal((120, 16)).astype(np.float32)
    same = parity.dense_parity(q, d, q.copy(), d.copy())
    assert same["doc_vectors"]["cos_mean"] == pytest.approx(1.0)
    assert same["ranking"]["top10_overlap"] == 1.0 and parity.gate(same)["verdict"] == "PASS"
    noisy = parity.dense_parity(q, d, q + 0.8 * rng.standard_normal(q.shape).astype(np.float32),
                                d + 0.8 * rng.standard_normal(d.shape).astype(np.float32))
    assert noisy["ranking"]["top10_overlap"] < 0.9 and parity.gate(noisy)["verdict"] == "FAIL"


def test_late_parity_identical():
    rng = np.random.default_rng(2)
    qs = [pp.l2_normalize(rng.standard_normal((4, 8)).astype(np.float32)) for _ in range(3)]
    ds = [pp.l2_normalize(rng.standard_normal((5 + i % 3, 8)).astype(np.float32)) for i in range(60)]
    res = parity.late_parity(qs, ds, qs, ds)
    assert res["doc_tokens"]["cos_mean"] == pytest.approx(1.0, abs=1e-6) and parity.gate(res)["verdict"] == "PASS"

