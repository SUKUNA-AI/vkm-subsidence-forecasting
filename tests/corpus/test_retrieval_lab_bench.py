"""Benchmark files of the retrieval lab (agent J): schema, ID grammar, splits, public hygiene."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from vkm_corpus.retrieval_lab import bench as B

ROOT = Path(__file__).resolve().parents[2]
BENCH = ROOT / "benchmarks" / "retrieval_v0"


def _q(qid: str, cat: str, track: str = "text", lang: str = "ru", target: str = "ru", **kw) -> B.Query:
    return B.Query(qid, track, kw.pop("text", f"запрос {qid}"), lang, target, cat, **kw)


def test_real_benchmark_is_valid():
    bench = B.load_benchmark(BENCH)
    problems = B.validate_benchmark(bench, min_text=120, min_visual=30)
    assert problems == [], "\n".join(problems[:30])


def test_real_benchmark_covers_every_category_and_slice():
    bench = B.load_benchmark(BENCH)
    text = [q for q in bench.queries if q.track == "text"]
    assert {q.category for q in text} == set(B.CATEGORIES)
    assert all(sum(1 for q in text if q.category == c) >= 5 for c in B.CATEGORIES)
    slices = {s for q in text for s in q.slices}
    assert slices == set(B.SLICES)
    assert len({q.text for q in bench.queries}) == len(bench.queries), "duplicate query texts"


def test_real_benchmark_has_no_forbidden_keys_or_text_columns():
    for line in (BENCH / "queries.jsonl").read_text(encoding="utf-8").splitlines():
        assert not (B._json_keys(json.loads(line)) & B.FORBIDDEN_KEYS)
    header = (BENCH / "qrels.tsv").read_text(encoding="utf-8").splitlines()[0].split("\t")
    assert tuple(header) == B.QRELS_COLUMNS


def test_real_splits_match_the_rule():
    bench = B.load_benchmark(BENCH)
    assert bench.splits == B.make_splits(bench.queries), "splits.json is stale: run `retrieval-lab splits --write`"


def test_splits_are_deterministic_stratified_and_local():
    qs = [_q(f"T-SALT-{i:03d}", "salt_mechanics") for i in range(1, 11)] + \
         [_q(f"T-LEX-{i:03d}", "lexical") for i in range(1, 8)]
    a, b = B.make_splits(qs), B.make_splits(list(reversed(qs)))
    assert a == b
    salt = [a[q.query_id] for q in qs if q.category == "salt_mechanics"]
    assert (salt.count("train"), salt.count("dev"), salt.count("test")) == (3, 2, 5)
    c = B.make_splits(qs + [_q("T-LEX-008", "lexical")])
    assert all(c[q.query_id] == a[q.query_id] for q in qs if q.category == "salt_mechanics")


def test_validation_catches_schema_errors(tmp_path):
    qs = [_q("T-SALT-001", "salt_mechanics"), _q("T-SALT-001", "salt_mechanics"), _q("T-LEX-002", "salt_mechanics"),
          _q("T-RUEN-001", "ru_to_en", lang="ru", target="ru"), _q("V-FIELD-001", "mine_field_map", track="visual")]
    qrels = [B.Qrel("T-SALT-001", "PAGE", "VKM-SRC-014:p0186", 3, "VERIFIED", "EVIDENCE_VNEXT"),
             B.Qrel("T-SALT-001", "PAGE", "VKM-SRC-14:p186", 2, "VERIFIED", "EVIDENCE_VNEXT"),
             B.Qrel("T-SALT-001", "PAGE", "VKM-SRC-014:p0187", 5, "GUESS", "LLM"),
             B.Qrel("T-XXX-001", "PAGE", "VKM-SRC-014:p0188", 1, "VERIFIED", "PAGE_INSPECTION")]
    hn = [B.HardNegative("T-SALT-001", "PAGE", "VKM-SRC-052:p0010", "GENERAL_TEXTBOOK_NOT_SITE", "PAGE_INSPECTION",
                         "VERIFIED")]
    problems = B.validate_benchmark(B.Benchmark(qs, qrels, hn))
    joined = "\n".join(problems)
    for needle in ("duplicate query_id", "code LEX", "ru_to_en must be", "bad level/doc_id", "grade 5",
                   "status GUESS", "basis LLM", "unknown query", "must also be in qrels"):
        assert needle in joined, needle


def test_roundtrip_files(tmp_path):
    qs = [_q("T-FORM-001", "formula_method", slices=("russian", "physics"), expected_kinds=("FORMULA",))]
    qrels = [B.Qrel("T-FORM-001", "PAGE", "VKM-SRC-014:p0186", 3, "VERIFIED", "EVIDENCE_VNEXT+PAGE_INSPECTION",
                    ("EV-VN-S014-0101",)),
             B.Qrel("T-FORM-001", "OBJECT", "VKM-SRC-014:p0186:m0123456789ab", 3, "CANDIDATE", "PAGE_INSPECTION")]
    B.write_queries(tmp_path / "queries.jsonl", qs)
    B.write_qrels(tmp_path / "qrels.tsv", qrels)
    B.write_hard_negatives(tmp_path / "hard_negatives.tsv", [])
    bench = B.load_benchmark(tmp_path)
    assert bench.queries == qs and sorted(bench.qrels, key=str) == sorted(qrels, key=str)
    assert bench.judgments() == {"T-FORM-001": {"VKM-SRC-014:p0186": 3}}
    assert bench.judgments(level="OBJECT", statuses=("VERIFIED", "CANDIDATE")) == {
        "T-FORM-001": {"VKM-SRC-014:p0186:m0123456789ab": 3}}
    assert B.validate_benchmark(bench) == []
