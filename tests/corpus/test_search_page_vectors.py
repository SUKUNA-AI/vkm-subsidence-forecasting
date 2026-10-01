"""Page-image vectors (agent VIS): the visual artifact (one row per page with a stored preview, text hash = preview
hash), the §64 checks against the CURRENT snapshot's page → preview map, the k-NN build with alias swap, rollback, the
plan's list of pages still to encode and the WORKSTATION encoder loop — synthetic canon, fake OpenSearch, fake
encoder."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("pyarrow")

from vkm_corpus.embeddings.artifacts import ArtifactWriter, validate  # noqa: E402
from vkm_corpus.embeddings.page_images import (PAGE_IMAGE_RULE, PageTodo, encode_pages, page_image_config,  # noqa: E402
                                               plan, preview_hex, preview_relpath, read_todo, write_rows)
from vkm_corpus.graph.common import ProjectionError  # noqa: E402
from vkm_corpus.search.fakes import FakeOpenSearch  # noqa: E402
from vkm_corpus.search.page_vectors import (PageVectorBuildOptions, build_page_vectors, expected_pages,  # noqa: E402
                                            page_vector_hits, pagevis_alias, pagevis_index_name, pagevis_meta,
                                            pagevis_status, parse_pagevis_index, rollback_page_vectors)

DIM = 2048


def _cfg(**kw):
    base = dict(weights_sha256="a" * 64, tokenizer_sha256="b" * 64, preprocessor_sha256="c" * 64,
                backend="sentence-transformers-cuda-bf16 (test)")
    base.update(kw)
    return page_image_config(**base)


def _vec(seed: str) -> np.ndarray:
    rng = np.random.default_rng(int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16))
    v = rng.standard_normal(DIM).astype(np.float32)
    return v / np.linalg.norm(v)


def _pages(n: int = 5, *, without: int = 1) -> dict[str, dict]:
    out = {}
    for i in range(n + without):
        pid = f"VKM-SRC-{1 + i % 2:03d}:p{i + 1:04d}"
        doc = {"source_id": pid.split(":")[0], "page_index": i + 1, "dup_group_id": pid, "year": 2000 + i}
        if i < n:
            doc["preview_artifact_id"] = "sha256:" + hashlib.sha256(pid.encode()).hexdigest()
        out[pid] = doc
    return out


def _artifact(root: Path, pages: dict[str, dict], *, cfg=None, skip: int = 0, writer: str = "w0") -> Path:
    cfg = cfg or _cfg()
    w = ArtifactWriter(root, "visual", cfg, writer_id=writer)
    exp = expected_pages(pages)
    keys = [(pid, pages[pid]["source_id"], h) for pid, h in sorted(exp.items())][skip:]
    write_rows(w, keys, np.stack([_vec(k[0]) for k in keys]), worker=writer)
    return w.dir


def test_config_names_the_image_input_and_keeps_text_signatures():
    cfg = _cfg()
    d = cfg.as_dict()
    assert cfg.mode == "visual" and cfg.text_rule == PAGE_IMAGE_RULE and cfg.dimension == DIM and cfg.max_len == 0
    assert d["image"]["preprocessor_config_sha256"] == "c" * 64
    assert cfg.document_instruction == "Represent the user's input."
    assert cfg.pooling == "last" and cfg.normalization == "l2" and cfg.quantization == "BF16"
    assert _cfg(preprocessor_sha256="d" * 64).signature() != cfg.signature()
    from vkm_corpus.embeddings.specs import get

    with pytest.raises(ValueError):
        page_image_config(get("granite-311m-r2"), weights_sha256="a", tokenizer_sha256="b", preprocessor_sha256="c",
                          backend="x")


def test_preview_ids_paths_and_todo(tmp_path):
    aid = "sha256:" + "ab" * 32
    assert preview_hex(aid) == "ab" * 32 and preview_hex("sha256:xyz") is None and preview_hex(None) is None
    assert preview_relpath(aid) == f"previews/ab/ab/{'ab' * 32}.jpg"
    todo = tmp_path / "todo.json"
    todo.write_text(json.dumps({"schema": "vkm.page_vectors_todo/1", "snapshot_id": "S",
                                "pages": [{"page_id": "VKM-SRC-001:p0001", "source_id": "VKM-SRC-001",
                                           "preview_artifact_id": aid}]}), encoding="utf-8")
    meta, pages = read_todo(todo)
    assert meta["snapshot_id"] == "S" and pages[0].text_hash == "ab" * 32
    todo.write_text(json.dumps({"schema": "other", "pages": []}), encoding="utf-8")
    with pytest.raises(ValueError):
        read_todo(todo)


def test_encode_pages_checks_files_and_skips_what_is_encoded(tmp_path):
    store = tmp_path / "artifacts"
    blobs = {}
    for i in range(3):
        data = f"jpeg-{i}".encode()
        aid = "sha256:" + hashlib.sha256(data).hexdigest()
        p = store / preview_relpath(aid)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        blobs[f"VKM-SRC-001:p{i + 1:04d}"] = aid
    todo = [PageTodo(pid, "VKM-SRC-001", aid) for pid, aid in blobs.items()]
    w = ArtifactWriter(tmp_path / "root", "visual", _cfg(), writer_id="rtx")
    seen = []

    def encode(images):
        seen.extend(images)
        return np.stack([_vec(x) * 3.0 for x in images])          # unnormalised on purpose: written L2-normalised

    rep = encode_pages(todo[:2], encode, w, artifacts_root=store, load_image=lambda p: p.read_bytes().decode(),
                       batch=1)
    assert rep == {"encoded": 2, "parts": 2} and seen == ["jpeg-0", "jpeg-1"]
    assert [p.page_id for p in plan(todo, w.dir)] == [todo[2].page_id]          # §46: only the new page
    encode_pages(plan(todo, w.dir), encode, w, artifacts_root=store, load_image=lambda p: p.read_bytes().decode())
    assert validate(w.dir, w.config, {p.page_id: p.text_hash for p in todo}).ok
    (store / preview_relpath(todo[0].preview_artifact_id)).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="does not hash"):
        encode_pages(todo[:1], encode, w, artifacts_root=store, load_image=lambda p: "x")
    with pytest.raises(ValueError, match="expected"):
        encode_pages(todo[1:2], lambda imgs: np.zeros((1, 3)), w, artifacts_root=store,
                     load_image=lambda p: "x")


# ---------------------------------------------------------------- build on the synthetic canon
@pytest.fixture(scope="module")
def canon_env(tmp_path_factory):
    pytest.importorskip("duckdb")
    from vkm_corpus.config import load_settings
    from vkm_corpus.graph.common import load_snapshot
    from vkm_corpus.testing import synthetic_canon

    canon = synthetic_canon(tmp_path_factory.mktemp("pagevis") / "data")
    settings = load_settings({"VKM_DATA_ROOT": str(canon.root), "VKM_DATA_ROLE": "canonical"})
    return {"canon": canon, "snapshot": load_snapshot(canon.root), "settings": settings}


def test_page_documents_of_the_snapshot_carry_previews_and_filters(canon_env):
    from vkm_corpus.search.page_vectors import page_documents

    docs = page_documents(canon_env["snapshot"])
    assert docs and all("source_id" in d for d in docs.values())
    exp = expected_pages(docs)
    assert set(exp) <= set(docs) and all(len(h) == 64 for h in exp.values())


def test_prepare_page_vectors_keeps_serving_aliases_and_old_indices(canon_env, tmp_path):
    pages = _pages()
    art = _artifact(tmp_path, pages)
    client = FakeOpenSearch()
    first = build_page_vectors(canon_env["settings"], PageVectorBuildOptions(embeddings=str(art)), client=client, pages=pages)
    old_aliases = {k: set(v) for k, v in client.aliases.items()}
    receipt = build_page_vectors(canon_env["settings"], PageVectorBuildOptions(embeddings=str(art), publish=False,
        policy_sha256="e" * 64, skip_if_current=True, keep_builds=1), client=client, pages=pages)
    assert receipt["publication"] == "PREPARED_NOT_PUBLISHED"
    assert receipt["index"] != first["index"]
    assert client.aliases == old_aliases and not client.deleted
    assert client.indices_[receipt["index"]]["meta"]["policy_sha256"] == "e" * 64
    assert receipt["alias_actions"] == [] and receipt["pruned"] == []


def test_build_checks_against_the_snapshot_then_swaps_the_alias(canon_env, tmp_path):
    pages = _pages()
    art = _artifact(tmp_path, pages)
    client = FakeOpenSearch()
    receipt = build_page_vectors(canon_env["settings"], PageVectorBuildOptions(embeddings=str(art)), client=client,
                                 pages=pages)
    assert receipt["status"] == "COMPLETE" and receipt["checks_64"]["ok"]
    assert receipt["pages"] == {"snapshot_pages": 6, "with_preview": 5, "without_preview": 1,
                                "expected_sha256": receipt["pages"]["expected_sha256"]}
    index = receipt["index"]
    assert client.aliases[index] == {"vkm-pagevis"} and index.startswith("vkm-pagevis-m1-")
    docs = client.indices_[index]["docs"]
    assert len(docs) == 5 and all(len(d["vector"]) == DIM for d in docs.values())
    d = docs["VKM-SRC-001:p0001"]
    assert d["year"] == 2000 and d["dup_group_id"] == "VKM-SRC-001:p0001" and d["text_hash"] == \
        pages["VKM-SRC-001:p0001"]["preview_artifact_id"][7:]
    meta = client.indices_[index]["meta"]
    assert meta["build_status"] == "COMPLETE" and meta["model_key"] == "qwen3-vl-emb-2b"
    assert meta["space_type"] == "innerproduct" and meta["vector_count"] == 5
    assert pagevis_meta(client, "vkm")["index"] == index
    st = pagevis_status(client, "vkm")
    assert st["count"] == 5 and st["built_from_snapshot_id"] == canon_env["snapshot"].snapshot_id
    again = build_page_vectors(canon_env["settings"], PageVectorBuildOptions(embeddings=str(art),
                                                                             skip_if_current=True),
                               client=client, pages=pages)
    assert again["status"] == "SKIPPED_CURRENT"
    second = build_page_vectors(canon_env["settings"], PageVectorBuildOptions(embeddings=str(art)), client=client,
                                pages=pages)
    assert client.aliases[second["index"]] == {"vkm-pagevis"} and index in client.indices_
    back = rollback_page_vectors(client, "vkm")
    assert back["target"] == index and client.aliases[index] == {"vkm-pagevis"}
    # the exact query answers pages by inner product, duplicate groups collapsed
    from vkm_corpus.search.page_vectors import page_vector_body

    q = _vec("VKM-SRC-001:p0003").tolist()
    hits = page_vector_hits(client.search(index="vkm-pagevis", body=page_vector_body(q, 10, {})), 3,
                            collapse_duplicates=True)
    assert hits[0].page_id == "VKM-SRC-001:p0003" and hits[0].score == pytest.approx(1.0, abs=1e-5)


def test_build_refuses_changed_or_missing_pages_and_lists_them(canon_env, tmp_path):
    pages = _pages()
    art = _artifact(tmp_path, pages, skip=1)                                # one page not encoded
    changed = {**pages, "VKM-SRC-002:p0002": {**pages["VKM-SRC-002:p0002"],
                                              "preview_artifact_id": "sha256:" + "9" * 64}}   # new preview
    client = FakeOpenSearch()
    todo = tmp_path / "todo.json"
    with pytest.raises(ProjectionError) as exc:
        build_page_vectors(canon_env["settings"], PageVectorBuildOptions(embeddings=str(art), plan_only=True,
                                                                         missing_out=str(todo)),
                           client=client, pages=changed)
    assert exc.value.code == "E_CHECK_FAILED" and exc.value.details["n_missing"] == 2
    meta, todo_pages = read_todo(todo)
    assert meta["config_signature"] == _cfg().signature()
    assert {p.page_id for p in todo_pages} == {"VKM-SRC-001:p0001", "VKM-SRC-002:p0002"}
    assert client.indices_ == {} and client.aliases == {}
    from vkm_corpus.embeddings.signature import EmbeddingConfig

    dense = ArtifactWriter(tmp_path, "dense", EmbeddingConfig(
        model_id="m", model_revision="r" * 40, weights_file="w", weights_sha256="a" * 64, quantization="Q8_0",
        mode="dense", dimension=8, pooling="cls", normalization="l2")).dir
    with pytest.raises(ProjectionError) as exc:
        build_page_vectors(canon_env["settings"], PageVectorBuildOptions(embeddings=str(dense)), client=client,
                           pages=pages)
    assert exc.value.code == "E_MODE_UNSUPPORTED"
    with pytest.raises(ProjectionError) as exc:
        build_page_vectors(canon_env["settings"], PageVectorBuildOptions(embeddings=str(art), snapshot_id="S-X"),
                           client=client, pages=pages)
    assert exc.value.code == "E_REFUSED"


def test_names_do_not_collide_with_the_page_index():
    name = pagevis_index_name("vkm", "20260929t100000z-aaaaaaaa-ef2f297d")
    assert pagevis_alias("vkm") == "vkm-pagevis"
    assert parse_pagevis_index("vkm", name) == "20260929t100000z-aaaaaaaa-ef2f297d"
    from vkm_corpus.search.mappings import parse_index_name

    assert parse_index_name("vkm", name) is None and parse_pagevis_index("vkm", "vkm-pages-m1-x") is None
