"""Pipelines A–I of the retrieval lab (agent J) end to end on the synthetic canon with the hashing FakeEncoder:
units → encodings (cached) → retrieval → fusion → late → (fake) reranker → page metrics, traces, pool."""
from __future__ import annotations

import json

import pytest

pytest.importorskip("snowballstemmer")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")

from vkm_corpus.retrieval_lab import bench as B
from vkm_corpus.retrieval_lab.bm25 import EXP_PREFIX, LocalBM25, exp_index_body, exp_index_name, query_body
from vkm_corpus.retrieval_lab.canon import CanonReader
from vkm_corpus.retrieval_lab.runner import LabRun, RunConfig, fake_specs, rerank_texts_for
from vkm_corpus.retrieval_lab.textproc import analyze
from vkm_corpus.retrieval_lab.units import build_units
from vkm_corpus.search.analysis import ANALYZER_EXPECTATIONS

QUERIES = [
    ("T-LEX-001", "lexical", "распознанный текст скана", "VKM-SRC-002:p0002"),
    ("T-LEX-002", "lexical", "схема мульды", "VKM-SRC-025:p0001"),
    ("T-ENRU-001", "en_to_ru", "synthetic spine item 2 paragraph", "VKM-SRC-249:s0002"),
    ("T-LEX-003", "lexical", "окончание чужой статьи", "VKM-SRC-005:p0002"),
]


class FakeReranker:
    def __init__(self) -> None:
        self.calls = []

    def rerank(self, query, candidates, top_n=None, request_id=None):
        self.calls.append(len(candidates))
        assert len(candidates) <= 24
        return sorted(((cid, float(len(set(analyze(query)) & set(analyze(text))))) for cid, text in candidates),
                      key=lambda x: (-x[1], x[0]))


@pytest.fixture(scope="module")
def lab(tmp_path_factory):
    from vkm_corpus.testing import synthetic_canon

    tmp = tmp_path_factory.mktemp("lab")
    canon = synthetic_canon(tmp / "data", with_duckdb=True)
    reader = CanonReader.from_duckdb(canon.duckdb_path)
    rows = reader.load_all()
    units = build_units(rows["pages"], rows["blocks"], rows["figures"], rows["tables"], rows["formulas"],
                        rows["bibliography"])
    queries = [B.Query(qid, "text", text, "en" if cat == "en_to_ru" else "ru", "ru" if cat == "en_to_ru" else "any",
                       cat) for qid, cat, text, _ in QUERIES]
    qrels = [B.Qrel(qid, "PAGE", page, 3, "VERIFIED", "PAGE_INSPECTION") for qid, _, _, page in QUERIES]
    qrels.append(B.Qrel("T-LEX-001", "PAGE", "VKM-SRC-002:p0001", 0, "VERIFIED", "PAGE_INSPECTION"))
    hn = [B.HardNegative("T-LEX-001", "PAGE", "VKM-SRC-002:p0001", "SAME_WORD_OTHER_PROCESS", "PAGE_INSPECTION",
                         "VERIFIED")]
    bench = B.Benchmark(queries, qrels, hn, B.make_splits(queries))
    assert B.validate_benchmark(bench) == []
    canon_rerank = {r["object_id"]: r["text"] for r in reader._rows("SELECT object_id, text FROM rerank_text")}
    return {"reader": reader, "units": units, "bench": bench, "tmp": tmp, "canon_rerank": canon_rerank}


def _run(lab, name, pipelines, reranker=None, **cfg_kw):
    cfg = RunConfig(out_dir=lab["tmp"] / name, dense=["FAKE"], late=["FAKE"], sparse=["FAKE"], m3=["FAKE"],
                    pipelines=pipelines, **cfg_kw)
    run = LabRun(cfg, lab["bench"], lab["units"], fake_specs(), meta={"test": True},
                 rerank_texts=rerank_texts_for(lab["units"], lab["canon_rerank"]), reranker=reranker,
                 source_meta=lab["reader"].source_meta(), page_labels=lab["reader"].page_labels())
    return run, run.run()


def test_all_pipelines_run_and_report(lab):
    run, report = _run(lab, "all", ["A", "B", "B-late", "C", "D", "E", "F", "G", "H", "I", "M3-unified"])
    systems = report["systems"]
    assert len(systems) == 11
    for label, res in systems.items():
        if label.split("|")[0] in ("G", "H", "I"):
            assert res["status"] == "NOT_RUN", label
            continue
        assert res["status"] == "OK", label
        assert res["overall"]["n_queries"] == 4
        assert 0.0 <= res["overall"]["ndcg@10"] <= 1.0
        assert "test" in res["splits"] or "dev" in res["splits"]
    bm25 = systems["A|bm25=local"]["overall"]
    assert bm25["recall@10"] == 1.0, "lexical synthetic queries are found by BM25"
    out = lab["tmp"] / "all"
    assert (out / "metrics.json").is_file() and (out / "pool.tsv").is_file()
    traces = [json.loads(l) for f in (out / "trace").glob("*.jsonl") for l in f.read_text(encoding="utf-8").splitlines()]
    e_trace = next(t for t in traces if t["system"].startswith("E|"))
    stages = {s for unit in e_trace["trace"].values() for s in unit}
    assert {"bm25", "dense", "fused", "late", "final"} <= stages


def test_reranker_stage_with_fake_gateway(lab):
    rr = FakeReranker()
    _, report = _run(lab, "rerank", ["G", "I"], reranker=rr)
    assert all(v["status"] == "OK" for v in report["systems"].values())
    assert rr.calls and max(rr.calls) <= 24


def test_encodings_are_cached_and_not_recomputed(lab):
    run1, _ = _run(lab, "cache", ["B", "B-late"])
    run2, _ = _run(lab, "cache", ["B", "B-late"])
    assert run1.encode_log[0]["encoded_docs"] == len(lab["units"])
    assert all(e["encoded_docs"] == 0 and e["encoded_queries"] == 0 for e in run2.encode_log)


def test_fully_cached_run_never_loads_the_model(lab):
    _run(lab, "cache_only", ["B", "B-late", "D"])

    def refuse(spec):
        raise AssertionError(f"model {spec.key} must not be loaded: everything is cached")

    cfg = RunConfig(out_dir=lab["tmp"] / "cache_only", dense=["FAKE"], late=["FAKE"], sparse=["FAKE"],
                    pipelines=["B", "B-late", "D"])
    run = LabRun(cfg, lab["bench"], lab["units"], fake_specs(), encoder_factory=refuse,
                 source_meta=lab["reader"].source_meta(), page_labels=lab["reader"].page_labels())
    report = run.run()
    assert all(v["status"] == "OK" for v in report["systems"].values())


def test_context_variant_changes_document_text(lab):
    run_a, _ = _run(lab, "ctxA", ["B"], context_variant="A")
    run_d, _ = _run(lab, "ctxD", ["B"], context_variant="D")
    changed = sum(1 for a, d in zip(run_a.doc_texts, run_d.doc_texts) if a != d)
    assert changed > 0


def test_local_bm25_mirrors_vkm_text_analyzer():
    for analyzer, text, expected in ANALYZER_EXPECTATIONS:
        if analyzer == "vkm_text":
            assert analyze(text) == expected, text
    idx = LocalBM25.build(["a", "b", "c"], ["ползучесть сильвинита при нагрузке", "сильвин и галит", "оседания"])
    assert [u for u, _ in idx.search("в сильвините ползучесть")][:1] == ["a"]
    assert idx.search("сильвин")[0][0] == "b"


def test_experimental_index_uses_prefix_and_e_analysis():
    name = exp_index_name("Run 2026/09")
    assert name.startswith(EXP_PREFIX) and name.endswith("-units") and "/" not in name
    body = exp_index_body("run")
    assert "vkm_text" in body["settings"]["analysis"]["analyzer"]
    assert body["mappings"]["dynamic"] == "strict"
    q = query_body("оседание поверхности", 50)
    assert q["size"] == 50 and q["sort"][1] == {"unit_id": "asc"}
