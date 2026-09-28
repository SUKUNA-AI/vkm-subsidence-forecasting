"""Dense vector projection (retrieval lab stage 2, task §44, §63–65): mapping from the embedding signature, units of a
snapshot, the streamed §64 checks and the build with alias swap — synthetic canon + fake OpenSearch, no services."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("pyarrow")

from vkm_corpus.embeddings.artifacts import ArtifactWriter, EmbeddingRow  # noqa: E402
from vkm_corpus.embeddings.signature import EmbeddingConfig  # noqa: E402
from vkm_corpus.graph.common import ProjectionError  # noqa: E402
from vkm_corpus.search.fakes import FakeOpenSearch  # noqa: E402
from vkm_corpus.search.vectors import (VectorBuildOptions, build_vectors, check_artifacts, knn_field,  # noqa: E402
                                       parse_vectors_index, vector_properties, vectors_alias, vectors_body,
                                       vectors_index_name)

DIM = 8


def _cfg(**kw) -> EmbeddingConfig:
    from vkm_corpus.embeddings.specs import get

    spec = get("granite-311m-r2")
    base = dict(model_id=spec.model_id, model_revision=spec.model_revision,
                weights_file="granite-311m-r2-Q8_0.gguf", weights_sha256="a" * 64, quantization="Q8_0",
                mode="dense", dimension=DIM, pooling="cls", normalization="l2", tokenizer_sha256="b" * 64)
    base.update(kw)
    return EmbeddingConfig(**base)


def _vec(seed: str) -> np.ndarray:
    rng = np.random.default_rng(abs(hash(seed)) % (2 ** 32))
    v = rng.standard_normal(DIM).astype(np.float32)
    return v / np.linalg.norm(v)


def _write(root: Path, cfg: EmbeddingConfig, units: dict[str, str], *, writer: str = "w0", vec=None) -> Path:
    w = ArtifactWriter(root, "dense", cfg, writer_id=writer)
    rows = [EmbeddingRow(object_id=u, text_hash=th, vector=(vec or _vec)(u), source_id="VKM-SRC-001",
                         page_id="VKM-SRC-001:p0001", object_type="BLOCK_GROUP") for u, th in units.items()]
    w.write_part(rows)
    return w.dir


# ---------------------------------------------------------------- mapping
def test_knn_mapping_follows_the_signature():
    f = knn_field(_cfg())
    assert f["type"] == "knn_vector" and f["dimension"] == DIM
    assert f["method"]["engine"] == "lucene" and f["method"]["name"] == "hnsw"
    assert f["method"]["space_type"] == "innerproduct"                   # L2-normalised → inner product (= cosine)
    assert knn_field(_cfg(normalization="none"))["method"]["space_type"] == "cosinesimil"
    assert knn_field(_cfg(dimension=512))["dimension"] == 512              # Matryoshka dimension of the signature
    assert knn_field(_cfg(storage_precision="int8"))["data_type"] == "byte"
    with pytest.raises(ProjectionError) as exc:
        knn_field(_cfg(storage_precision="binary"))
    assert exc.value.code == "E_MODE_UNSUPPORTED"
    body = vectors_body(_cfg(), {"build_id": "b1"})
    assert body["settings"]["index"]["knn"] is True and body["mappings"]["dynamic"] == "strict"
    props = vector_properties(_cfg())
    for name in ("id", "unit_kind", "object_ids", "page_id", "source_id", "work_id", "snapshot_id",
                 "config_signature", "source_site_scope", "available_latest_day", "year", "dup_group_id", "vector"):
        assert name in props, name
    assert "text" not in props and "figure_type" not in props            # no text, no object-type fields
    name = vectors_index_name("vkm", "20260928t120000z-abcd1234-0011aabb")
    assert name == "vkm-vectors-m1-20260928t120000z-abcd1234-0011aabb" and vectors_alias("vkm") == "vkm-vectors"
    assert parse_vectors_index("vkm", name) == "20260928t120000z-abcd1234-0011aabb"
    assert parse_vectors_index("vkm", "vkm-pages-m1-x") is None


# ---------------------------------------------------------------- §64
def test_check_artifacts_accepts_complete_current_embeddings(tmp_path):
    cfg = _cfg()
    units = {f"u1-{i:016x}": f"{i:064x}" for i in range(6)}
    d = _write(tmp_path, cfg, units)
    rep, selection = check_artifacts(d, cfg, units)
    assert rep.ok and rep.current == 6 and rep.expected == 6 and set(selection) == set(units)
    # orphans (units that left the canon) and older texts are history: reported, not a failure, not projected
    _write(tmp_path, cfg, {"u1-orphan000000000": "f" * 64, "u1-0000000000000000": "e" * 64}, writer="w1")
    rep, selection = check_artifacts(d, cfg, units)
    assert rep.ok and rep.orphaned == 1 and rep.stale_rows == 1 and "u1-orphan000000000" not in selection


@pytest.mark.parametrize("fault", ["missing", "duplicate", "nan", "norm", "checksum", "signature"])
def test_check_artifacts_refuses(tmp_path, fault):
    cfg = _cfg()
    units = {f"u1-{i:016x}": f"{i:064x}" for i in range(4)}
    expected = dict(units)
    if fault == "missing":
        expected["u1-notembedded0000"] = "c" * 64
    d = _write(tmp_path, cfg, units)
    if fault == "duplicate":
        _write(tmp_path, cfg, {"u1-0000000000000001": units["u1-0000000000000001"]}, writer="w1")
    if fault == "nan":
        _write(tmp_path, cfg, {"u1-nan0000000000000": "d" * 64}, writer="w1",
               vec=lambda u: np.full(DIM, np.nan, dtype=np.float32))
        expected["u1-nan0000000000000"] = "d" * 64
    if fault == "norm":
        _write(tmp_path, cfg, {"u1-norm000000000000": "d" * 64}, writer="w1",
               vec=lambda u: np.ones(DIM, dtype=np.float32))
        expected["u1-norm000000000000"] = "d" * 64
    if fault == "checksum":
        part = sorted(d.glob("part-*.parquet"))[0]
        part.write_bytes(part.read_bytes() + b"tamper")
    if fault == "signature":
        other = _cfg(quantization="Q6_K")                                 # another model artifact = another signature
        rep, _ = check_artifacts(d, other, expected)
        assert not rep.ok and rep.signature_mismatch == 4 and len(rep.missing) == 4
        return
    rep, _ = check_artifacts(d, cfg, expected)
    assert not rep.ok
    assert {"missing": rep.missing, "duplicate": rep.duplicates, "nan": rep.bad_vectors, "norm": rep.bad_vectors,
            "checksum": rep.problems}[fault]


# ---------------------------------------------------------------- units + build on the synthetic canon
@pytest.fixture(scope="module")
def canon_env(tmp_path_factory):
    pytest.importorskip("duckdb")
    from vkm_corpus.config import load_settings
    from vkm_corpus.graph.common import load_snapshot
    from vkm_corpus.search.vectors import export_units_at, page_metadata
    from vkm_corpus.testing import synthetic_canon

    canon = synthetic_canon(tmp_path_factory.mktemp("vectors") / "data")
    snapshot = load_snapshot(canon.root)
    man = export_units_at(canon.root, snapshot)
    settings = load_settings({"VKM_DATA_ROOT": str(canon.root), "VKM_DATA_ROLE": "canonical"})
    return {"canon": canon, "snapshot": snapshot, "units": man, "settings": settings,
            "pages": page_metadata(snapshot)}


def _units(env) -> dict[str, dict]:
    rows = [json.loads(line) for line in (Path(env["units"]["dir"]) / "units.jsonl").read_text("utf-8").splitlines()]
    return {r["unit_id"]: r for r in rows}


def test_export_units_is_idempotent_and_carries_no_text_in_the_index_rows(canon_env):
    from vkm_corpus.search.vectors import export_units_at

    man = canon_env["units"]
    assert man["status"] == "WRITTEN" and man["snapshot_id"] == canon_env["snapshot"].snapshot_id
    assert man["text_rule"] == "vkm-units-v1/A" and man["count"] > 5
    assert {"BLOCK_GROUP", "FIGURE"} <= set(man["by_kind"])
    units = _units(canon_env)
    assert all(set(u) >= {"unit_id", "kind", "page_id", "source_id", "object_ids", "text_hash"} for u in units.values())
    assert not any("text" in u for u in units.values())
    docs = [json.loads(line) for line in (Path(man["dir"]) / "docs.jsonl").read_text("utf-8").splitlines()]
    assert {d["object_id"] for d in docs} == set(units) and all(d["text"] for d in docs)   # `embed encode` input
    again = export_units_at(canon_env["canon"].root, canon_env["snapshot"])
    assert again["status"] == "EXISTS" and again["units_sha256"] == man["units_sha256"]


def _embed_all(env, tmp: Path, cfg: EmbeddingConfig, *, skip: int = 0) -> Path:
    units = _units(env)
    root = env["canon"].root
    w = ArtifactWriter(root, "dense", cfg, writer_id=f"t{tmp.name[-8:]}".replace("_", "-")[:30])
    ids = sorted(units)[skip:]
    w.write_part([EmbeddingRow(object_id=u, text_hash=units[u]["text_hash"], vector=_vec(u),
                               source_id=units[u]["source_id"], page_id=units[u]["page_id"],
                               object_type=units[u]["kind"]) for u in ids])
    return w.dir


def test_build_vectors_checks_then_swaps_the_alias(canon_env, tmp_path):
    cfg = _cfg(weights_sha256="2" * 64)
    art = _embed_all(canon_env, tmp_path, cfg)
    client = FakeOpenSearch()
    options = VectorBuildOptions(embeddings=str(art))
    receipt = build_vectors(canon_env["settings"], options, client=client, page_meta=canon_env["pages"])
    assert receipt["status"] == "COMPLETE" and receipt["checks_64"]["ok"]
    index = receipt["index"]
    assert client.aliases[index] == {"vkm-vectors"}
    docs = client.indices_[index]["docs"]
    units = _units(canon_env)
    assert set(docs) == set(units)
    doc = docs[sorted(docs)[0]]
    assert len(doc["vector"]) == DIM and doc["snapshot_id"] == canon_env["snapshot"].snapshot_id
    assert doc["config_signature"] == cfg.signature() and doc["page_id"] in canon_env["pages"]
    assert "source_site_scope" in doc and "text" not in doc
    meta = client.indices_[index]["meta"]
    assert meta["build_status"] == "COMPLETE" and meta["dimension"] == DIM and meta["vector_count"] == len(units)
    assert meta["model_key"] == "granite-311m-r2" and meta["space_type"] == "innerproduct"
    assert (canon_env["canon"].root / receipt["receipt_ref"]).is_file()
    # idempotent re-run: nothing rebuilt
    again = build_vectors(canon_env["settings"], VectorBuildOptions(embeddings=str(art), skip_if_current=True),
                          client=client, page_meta=canon_env["pages"])
    assert again["status"] == "SKIPPED_CURRENT" and again["build_id"] == receipt["build_id"]
    # a new build moves the alias; the previous COMPLETE build is kept for rollback, older ones deleted by name
    second = build_vectors(canon_env["settings"], options, client=client, page_meta=canon_env["pages"])
    assert client.aliases[second["index"]] == {"vkm-vectors"} and not client.aliases.get(index)
    assert index in client.indices_


def test_build_vectors_refuses_incomplete_or_foreign_embeddings(canon_env, tmp_path):
    cfg = _cfg(weights_sha256="3" * 64)
    art = _embed_all(canon_env, tmp_path, cfg, skip=1)                     # one unit not embedded
    client = FakeOpenSearch()
    with pytest.raises(ProjectionError) as exc:
        build_vectors(canon_env["settings"], VectorBuildOptions(embeddings=str(art)), client=client,
                      page_meta=canon_env["pages"])
    assert exc.value.code == "E_CHECK_FAILED" and exc.value.details["n_missing"] == 1
    assert client.indices_ == {} and client.aliases == {}                   # nothing imported, alias untouched
    with pytest.raises(ProjectionError) as exc:
        build_vectors(canon_env["settings"], VectorBuildOptions(embeddings=str(art), snapshot_id="SNAP-OTHER"),
                      client=client, page_meta=canon_env["pages"])
    assert exc.value.code == "E_REFUSED"
    report = tmp_path / "encode.json"                                      # `embed encode` report as the manifest
    report.write_text(json.dumps([{"kind": "multivector", "dir": str(art)}]), encoding="utf-8")
    with pytest.raises(ProjectionError) as exc:
        build_vectors(canon_env["settings"], VectorBuildOptions(embeddings=str(report)), client=client)
    assert exc.value.code == "E_PREFLIGHT"
    staging = canon_env["settings"].__class__(**{**canon_env["settings"].__dict__, "data_role": "producer"})
    with pytest.raises(ProjectionError) as exc:
        build_vectors(staging, VectorBuildOptions(embeddings=str(art)), client=client)
    assert exc.value.code == "E_STAGING_ROOT"
