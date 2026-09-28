"""Scenario B of CP-22 (agent C): stratified sample, CER of an embedded layer against GLM-OCR, source decision."""
from __future__ import annotations

import random

import pytest

pytest.importorskip("numpy")

from vkm_corpus.pipeline import scenario_b as sb  # noqa: E402


def naive(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def test_edit_distance_matches_naive():
    rnd = random.Random(7)
    alphabet = "абвгдеёжз xyz01"
    for _ in range(200):
        a = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 25)))
        b = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 25)))
        assert sb.edit_distance(a, b) == naive(a, b)


def test_sample_is_deterministic_and_spread():
    pages = list(range(1, 205))
    s = sb.sample_pages(pages, 0.05)
    assert s == sb.sample_pages(list(reversed(pages)), 0.05)
    assert len(s) == 10 and min(s) < 30 and max(s) > 170
    assert sb.sample_pages([3], 0.05) == [3] and sb.sample_pages([], 0.05) == []


def test_cer_and_decision():
    value, dist, ref = sb.cer("Синтетический слой АОРОГА", "Синтетический слой ДОРОГА")
    assert ref > 0 and dist == 1 and value == pytest.approx(1 / ref)
    rows_good = [{"page_index": i, "distance": 1, "ref_len": 100, "layer_letter_share": 0.9} for i in range(5)]
    assert sb.decide(rows_good, 0.10, 0.6)["decision"] == "KEEP_EMBEDDED_LAYER"
    rows_bad = [{"page_index": i, "distance": 30, "ref_len": 100, "layer_letter_share": 0.9} for i in range(5)]
    d = sb.decide(rows_bad, 0.10, 0.6)
    assert d["decision"] == "REOCR_SOURCE" and d["reasons"] == ["CER_ABOVE_THRESHOLD"]
    rows_garbage = [{"page_index": 1, "distance": 1, "ref_len": 100, "layer_letter_share": 0.3}]
    assert sb.decide(rows_garbage, 0.10, 0.6)["reasons"] == ["LETTER_SHARE_BELOW_MIN"]
    assert sb.decide([], 0.10, 0.6)["decision"] == "UNDECIDED"
