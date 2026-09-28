"""BGE-M3 FlagEmbedding reference encodings: configs/signatures, unit loading, multi-vector validation and the export
into the Retrieval Lab cache layout (no torch/FlagEmbedding needed: artifacts are written with the ArtifactWriter)."""
from __future__ import annotations

import json

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("pyarrow")

from vkm_corpus.embeddings import m3_flag  # noqa: E402
from vkm_corpus.embeddings.artifacts import ArtifactWriter, EmbeddingRow  # noqa: E402
from vkm_corpus.embeddings.signature import text_hash  # noqa: E402


@pytest.fixture()
def model_dir(tmp_path):
    d = tmp_path / "bge-m3"
    d.mkdir()
    for name in ("pytorch_model.bin", "tokenizer.json", "sparse_linear.pt", "colbert_linear.pt", "config.json"):
        (d / name).write_bytes(name.encode())
    return d


def test_configs_carry_backend_and_differ_per_output(model_dir):
    cfgs = m3_flag.configs(model_dir, max_len=512, text_rule="vkm-units-v1/A", mv_precision="float16")
    sigs = {k: c.signature() for k, c in cfgs.items()}
    assert len(set(sigs.values())) == 3
    assert all(c.as_dict()["backend"] == "flagembedding-cpu-fp32" for c in cfgs.values())
    assert cfgs["sparse"].heads_sha256 != cfgs["multivector"].heads_sha256 and cfgs["dense"].heads_sha256 == ""
    other = m3_flag.configs(model_dir, max_len=256, text_rule="vkm-units-v1/A", mv_precision="float16")
    assert other["dense"].signature() != sigs["dense"]                   # max_len is part of the signature
    q = m3_flag.query_configs(cfgs, max_len=128)
    assert q["dense"].as_dict()["backend"] == "flagembedding-cpu-fp32" and q["dense"].max_len == 128


def test_load_units_joins_metadata_and_hashes_text(tmp_path):
    docs = tmp_path / "docs.jsonl"
    units = tmp_path / "units.jsonl"
    docs.write_text("\n".join(json.dumps({"object_id": f"u{i}", "text": f"текст {i}"}, ensure_ascii=False)
                              for i in range(3)), encoding="utf-8")
    units.write_text(json.dumps({"unit_id": "u1", "kind": "FORMULA", "source_id": "VKM-SRC-005",
                                 "page_id": "VKM-SRC-005:p0001"}), encoding="utf-8")
    texts, canon = m3_flag.load_units(docs, units)
    assert len(texts) == 3 and canon["u1"].object_type == "FORMULA" and canon["u0"].source_id is None
    assert canon["u2"].text_hash == text_hash("текст 2")
    assert m3_flag._sparse_arrays({"7": 0.5, "3": np.float32(0.25)}) == ([3, 7], [0.25, 0.5])


def _write_artifacts(root, model_dir, n=5):
    cfgs = m3_flag.configs(model_dir, max_len=512, text_rule="vkm-units-v1/A", mv_precision="float16")
    rng = np.random.default_rng(0)
    rows = {"dense": [], "sparse": [], "multivector": []}
    texts = {f"u{i}": f"unit text {i}" for i in range(n)}
    for oid, t in texts.items():
        base = dict(object_id=oid, text_hash=text_hash(t), backend=m3_flag.BACKEND)
        v = rng.standard_normal(1024).astype(np.float32)
        rows["dense"].append(EmbeddingRow(vector=v / np.linalg.norm(v), **base))
        rows["sparse"].append(EmbeddingRow(token_ids=[5, 17], weights=[0.1, 0.3], **base))
        m = rng.standard_normal((3, 1024)).astype(np.float32)
        rows["multivector"].append(EmbeddingRow(vectors=m / np.linalg.norm(m, axis=1, keepdims=True),
                                                token_ids=[10, 11, 2], **base))
    writers = {k: ArtifactWriter(root, k, c, writer_id="t") for k, c in cfgs.items()}
    for k, w in writers.items():
        w.write_part(rows[k])
    return cfgs, writers, texts


def test_validate_multivector_vectorised(tmp_path, model_dir):
    cfgs, writers, texts = _write_artifacts(tmp_path / "root", model_dir)
    expected = {o: text_hash(t) for o, t in texts.items()}
    rep = m3_flag.validate_multivector(writers["multivector"].dir, cfgs["multivector"], expected)
    assert rep["ok"] and rep["rows_current"] == 5 and rep["tokens"] == 15
    rep = m3_flag.validate_multivector(writers["multivector"].dir, cfgs["multivector"], {**expected, "u9": "x"})
    assert not rep["ok"] and rep["n_missing"] == 1


def test_export_lab_cache_layout(tmp_path, model_dir):
    cfgs, writers, texts = _write_artifacts(tmp_path / "root", model_dir)
    docs = tmp_path / "docs.jsonl"
    docs.write_text("\n".join(json.dumps({"object_id": o, "text": t}) for o, t in texts.items()), encoding="utf-8")
    queries = tmp_path / "queries.jsonl"
    queries.write_text(json.dumps({"query_id": "Q1", "text": "запрос"}, ensure_ascii=False), encoding="utf-8")
    run = tmp_path / "run"
    (run / "queries").mkdir(parents=True)
    np.savez(run / "queries" / "queries.npz", query_ids=np.asarray(["Q1"]), dense=np.ones((1, 1024), np.float32),
             mv=np.ones((2, 1024), np.float32), mv_offsets=np.asarray([0, 2]),
             sparse_ids=np.asarray([4], np.int32), sparse_weights=np.asarray([0.2], np.float32),
             sparse_offsets=np.asarray([0, 1]))
    receipt = {"configs": {k: {"signature": c.signature(), "dir": str(writers[k].dir)} for k, c in cfgs.items()},
               "settings": {"max_len": 512, "query_max_len": 128}, "queries": {"query_signatures": {}}}
    (run / "RECEIPT.json").write_text(json.dumps(receipt), encoding="utf-8")
    info = m3_flag.export_lab_cache(run / "RECEIPT.json", docs, queries, tmp_path / "cache")
    assert info["doc_keys"] == 5 and info["doc_token_rows"] == 15 and info["query_keys"] == 1
    c = tmp_path / "cache"
    index = json.loads((c / "doc_dense_index.json").read_text())
    assert set(index) == {text_hash(t) for t in texts.values()}
    assert np.load(c / "doc_dense.npy").shape == (5, 1024)
    tok = json.loads((c / "doc_tokens_index.json").read_text())
    flat = np.memmap(c / "doc_tokens.float16", dtype=np.float16, mode="r").reshape(-1, 1024)
    a, b = tok["offsets"][text_hash(texts["u0"])]
    assert b - a == 3 and flat.shape == (15, 1024)
    sparse = [json.loads(line) for line in (c / "doc_sparse.jsonl").read_text().splitlines()]
    assert sparse[0]["w"] == {"5": pytest.approx(0.1), "17": pytest.approx(0.3)}
    assert json.loads((c / "query_sparse.jsonl").read_text())["k"] == text_hash("запрос")
    assert json.loads((c / "signature.json").read_text())["backend"] == "flagembedding-cpu-fp32"
