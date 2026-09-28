"""Score head of jina-reranker-m0 and the parity gate logic (agent F; CP-18 parity is blocking)."""
from __future__ import annotations

import math

import pytest

from vkm_corpus.retrieval.parity import LAYOUT_MAX_ABS_DIFF, PARITY_MAX_ABS_DIFF, PARITY_SPEARMAN_MIN, compare, spearman


def _npz(path, w1_scale=0.0, b2=0.0, bias=2.65):
    np = pytest.importorskip("numpy")
    np.savez(path, W1=np.eye(1536, dtype=np.float32) * w1_scale, b1=np.zeros(1536, dtype=np.float32),
             W2=np.ones((1536, 1), dtype=np.float32), b2=np.array([b2], dtype=np.float32),
             logit_bias=np.array([bias], dtype=np.float32))
    return path


def test_head_formula(tmp_path):
    from vkm_corpus.retrieval.m0_head import M0Head

    head = M0Head.from_npz(_npz(tmp_path / "mlp.npz", w1_scale=1.0, b2=0.5))
    np = pytest.importorskip("numpy")
    h = np.zeros((2, 1536), dtype=np.float32)
    h[1, :3] = [1.0, -2.0, 0.25]                       # relu → 1 + 0.25
    s = head.scores(h)
    assert s[0] == pytest.approx(1 / (1 + math.exp(-(0.5 - 2.65))), abs=1e-6)
    assert s[1] == pytest.approx(1 / (1 + math.exp(-(1.25 + 0.5 - 2.65))), abs=1e-6)


def test_head_pins_and_shapes(tmp_path):
    from vkm_corpus.retrieval.m0_head import M0Head

    path = _npz(tmp_path / "mlp.npz")
    with pytest.raises(ValueError, match="sha256 mismatch"):
        M0Head.from_npz(path, expected_sha256="f" * 64)
    head = M0Head.from_npz(path)
    with pytest.raises(ValueError):
        head.scores([[0.0] * 10])


def test_spearman():
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert spearman([1, 2, 3, 4, 5, 6], [1, 2, 3, 4, 6, 5]) == pytest.approx(1 - 6 * 2 / (6 * 35))


def test_compare_verdicts():
    ref = {"q1": {"a": 0.9, "b": 0.5, "c": 0.1}, "q2": {"a": 0.2, "b": 0.8, "c": 0.4}}
    close = {"q1": {"a": 0.89, "b": 0.51, "c": 0.12}, "q2": {"a": 0.21, "b": 0.79, "c": 0.41}}
    rep = compare(ref, close)
    assert rep["verdict"] == "PASS" and rep["max_abs_diff"] <= PARITY_MAX_ABS_DIFF and rep["min_spearman"] == 1.0
    swapped = {"q1": {"a": 0.5, "b": 0.9, "c": 0.1}, "q2": close["q2"]}
    bad = compare(ref, swapped)
    assert bad["verdict"] == "FAIL" and bad["per_query"]["q1"]["top1_candidate"] == "b"
    assert bad["per_query"]["q1"]["clear_swaps"] == 1
    layout = compare(ref, close, max_abs_diff=LAYOUT_MAX_ABS_DIFF)
    assert layout["verdict"] == "FAIL"                  # 0.01 differences are not a layout-only change
    with pytest.raises(ValueError):
        compare(ref, {"q1": {"a": 1.0}, "q2": close["q2"]})
    assert PARITY_SPEARMAN_MIN == 0.9 and PARITY_MAX_ABS_DIFF == 0.05
