"""Derived embedding artifacts (§39–43): writer/reader round trip and the pre-import checks of §64."""
from __future__ import annotations

import json

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("pyarrow")

from vkm_corpus.embeddings.artifacts import (  # noqa: E402
    ArtifactWriter,
    EmbeddingRow,
    config_dir,
    current_rows,
    existing_hashes,
    iter_current_vectors,
    read_table,
    validate,
)
from vkm_corpus.embeddings.signature import EmbeddingConfig, text_hash  # noqa: E402


def _cfg(mode="dense", dim=4, precision="float32", normalization="l2") -> EmbeddingConfig:
    return EmbeddingConfig(model_id="org/enc", model_revision="1" * 40, weights_file="enc-Q8_0.gguf",
                           weights_sha256="a" * 64, quantization="Q8_0", mode=mode, dimension=dim, pooling="cls",
                           normalization=normalization, tokenizer_sha256="b" * 64, storage_precision=precision)


def _unit(seed: int, dim: int = 4) -> "np.ndarray":
    v = np.random.default_rng(seed).standard_normal(dim).astype(np.float32)
    return v / np.linalg.norm(v)


def _dense_rows(n: int, text: str = "t") -> list[EmbeddingRow]:
    return [EmbeddingRow(object_id=f"o{i}", text_hash=text_hash(f"{text}{i}"), source_id="VKM-SRC-001",
                         object_type="BLOCK", vector=_unit(i), worker="rx580-0", backend="llama.cpp-vulkan")
            for i in range(n)]


def test_layout_and_dense_round_trip(tmp_path):
    cfg = _cfg()
    w = ArtifactWriter(tmp_path, "dense", cfg)
    assert w.dir == config_dir(tmp_path, "dense", cfg)
    assert w.dir.relative_to(tmp_path).parts[:5] == ("derived", "embeddings", "dense", "org__enc", "1" * 40)
    assert w.dir.name == cfg.signature()
    e1 = w.write_part(_dense_rows(3))
    e2 = w.write_part(_dense_rows(2, text="u"))            # re-embedded o0, o1 with new text
    assert (e1["file"], e2["file"]) == ("part-w0-00000.parquet", "part-w0-00001.parquet")
    man = json.loads(w.manifest_path.read_text())
    assert man["rows_total"] == 5 and [p["rows"] for p in man["parts"]] == [3, 2]
    table = read_table(w.dir)
    assert table.num_rows == 5 and table.schema.field("vector").type.value_type.bit_width == 32
    expected = {"o0": text_hash("u0"), "o1": text_hash("u1"), "o2": text_hash("t2")}
    cur = current_rows(table, expected)
    assert set(cur) == {"o0", "o1", "o2"} and cur["o0"]["text_hash"] == text_hash("u0")
    vecs = dict(iter_current_vectors(w.dir, expected))
    np.testing.assert_allclose(vecs["o2"], _unit(2), rtol=1e-6)
    assert existing_hashes(table)["o0"] == {text_hash("t0"), text_hash("u0")}
    rep = validate(w.dir, cfg, expected)
    assert rep.ok and rep.rows_current == 3 and rep.stale_rows == 2


def test_validation_catches_missing_unexpected_duplicates_and_bad_vectors(tmp_path):
    cfg = _cfg()
    w = ArtifactWriter(tmp_path, "dense", cfg)
    rows = _dense_rows(3)
    rows.append(EmbeddingRow(object_id="o1", text_hash=text_hash("t1"), vector=_unit(1)))       # duplicate
    rows.append(EmbeddingRow(object_id="nan", text_hash=text_hash("n"), vector=np.array([np.nan, 0, 0, 1])))
    rows.append(EmbeddingRow(object_id="long", text_hash=text_hash("l"), vector=np.ones(4)))    # not unit norm
    w.write_part(rows)
    expected = {"o0": text_hash("t0"), "o1": text_hash("t1"), "o2": text_hash("t2"), "o9": text_hash("t9"),
                "nan": text_hash("n"), "long": text_hash("l")}
    rep = validate(w.dir, cfg, expected)
    assert not rep.ok
    assert rep.missing == ["o9"] and rep.duplicates == ["o1"] and sorted(rep.bad_vectors) == ["long", "nan"]
    rep2 = validate(w.dir, cfg, {"o0": text_hash("t0")})
    assert "o2" in rep2.unexpected


def test_checksum_mismatch_and_other_signature_are_reported(tmp_path):
    cfg = _cfg()
    w = ArtifactWriter(tmp_path, "dense", cfg)
    w.write_part(_dense_rows(2))
    part = w.dir / "part-w0-00000.parquet"
    part.write_bytes(part.read_bytes()[:-8] + b"XXXXXXXX")
    rep = validate(w.dir, cfg, {"o0": text_hash("t0"), "o1": text_hash("t1")})
    assert not rep.ok and "checksum" in rep.problems[0]
    with pytest.raises(ValueError, match="another signature"):
        (w.dir / "config.json").write_text(json.dumps({"config_signature": "0" * 64}))
        ArtifactWriter(tmp_path, "dense", cfg)


def test_parts_are_immutable(tmp_path):
    w = ArtifactWriter(tmp_path, "dense", _cfg())
    w.write_part(_dense_rows(1))
    w.manifest_path.unlink()             # a lost manifest must not lead to overwriting an existing part
    with pytest.raises(FileExistsError):
        w.write_part(_dense_rows(1))


def test_concurrent_writers_have_separate_parts_and_manifests(tmp_path):
    cfg = _cfg()
    a = ArtifactWriter(tmp_path, "dense", cfg, writer_id="rx580-0")
    b = ArtifactWriter(tmp_path, "dense", cfg, writer_id="rtx-0")
    a.write_part(_dense_rows(2))
    b.write_part([EmbeddingRow(object_id="o5", text_hash=text_hash("t5"), vector=_unit(5))])
    assert sorted(p.name for p in a.dir.glob("part-*")) == ["part-rtx-0-00000.parquet", "part-rx580-0-00000.parquet"]
    assert read_table(a.dir).num_rows == 3
    with pytest.raises(ValueError):
        ArtifactWriter(tmp_path, "dense", cfg, writer_id="../x")


def test_multivector_float16_round_trip_and_checks(tmp_path):
    cfg = _cfg(mode="multivector", dim=8, precision="float16")
    w = ArtifactWriter(tmp_path, "multivector", cfg)
    mats = {f"d{i}": np.stack([_unit(10 * i + j, 8) for j in range(3 + i)]) for i in range(3)}
    w.write_part([EmbeddingRow(object_id=k, text_hash=text_hash(k), vectors=m, token_ids=list(range(len(m))))
                  for k, m in mats.items()])
    table = read_table(w.dir)
    assert table.schema.field("vectors").type.value_type.bit_width == 16
    got = dict(iter_current_vectors(w.dir))
    for k, m in mats.items():
        assert got[k].shape == m.shape
        np.testing.assert_allclose(got[k], m, atol=2e-3)
    assert validate(w.dir, cfg, {k: text_hash(k) for k in mats}).ok


def test_sparse_and_visual_round_trip(tmp_path):
    sp = _cfg(mode="sparse", dim=0, normalization="none")
    w = ArtifactWriter(tmp_path, "sparse", sp)
    w.write_part([EmbeddingRow(object_id="s", text_hash=text_hash("s"), token_ids=[5, 9], weights=[0.2, 0.7])])
    assert validate(w.dir, sp, {"s": text_hash("s")}).ok
    w.write_part([EmbeddingRow(object_id="bad", text_hash=text_hash("b"), token_ids=[1], weights=[-0.1])])
    assert validate(w.dir, sp, {"s": text_hash("s"), "bad": text_hash("b")}).bad_vectors == ["bad"]
    vis = _cfg(mode="visual", dim=4)
    wv = ArtifactWriter(tmp_path, "visual", vis)
    wv.write_part([EmbeddingRow(object_id="f", text_hash=text_hash("img"), artifact_sha="sha256:" + "c" * 64,
                                vector=_unit(3))])
    assert read_table(wv.dir).column("artifact_sha").to_pylist() == ["sha256:" + "c" * 64]


def test_int8_storage_requires_quantised_values(tmp_path):
    cfg = _cfg(precision="int8", normalization="none")
    w = ArtifactWriter(tmp_path, "dense", cfg)
    with pytest.raises(ValueError, match="int8"):
        w.write_part([EmbeddingRow(object_id="x", text_hash="h", vector=np.zeros(4, np.float32))])
    w.write_part([EmbeddingRow(object_id="x", text_hash=text_hash("x"), vector=np.array([1, -2, 3, 127], np.int8))])
    assert read_table(w.dir).column("vector").to_pylist() == [[1, -2, 3, 127]]
