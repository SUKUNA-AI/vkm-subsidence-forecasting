"""Token-vector pack of late interaction (agent L): §64 checks before packing, the memory-mapped store, page/object
targets, hot reload through packs/CURRENT, refusal of a pack of another model, and the ``embed pack`` CLI."""
from __future__ import annotations

import hashlib
import json

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("pyarrow")

from vkm_corpus.embeddings.artifacts import ArtifactWriter, EmbeddingRow  # noqa: E402
from vkm_corpus.embeddings.pack import (  # noqa: E402
    PACKS_DIR,
    POINTER,
    PackError,
    PackHandle,
    PackStore,
    build_pack,
    prune,
    publish,
    verify_pack,
)
from vkm_corpus.embeddings.postprocess import l2_normalize, maxsim  # noqa: E402
from vkm_corpus.embeddings.signature import EmbeddingConfig, text_hash  # noqa: E402

DIM = 8
P1, P2, P3 = "VKM-SRC-001:p0001", "VKM-SRC-001:p0002", "VKM-SRC-002:p0001"
# unit id, kind, page, object ids, token count
UNITS = [("u1-00000000000000a1", "BLOCK_GROUP", P1, ["VKM-SRC-001:p0001:b01", "VKM-SRC-001:p0001:b02"], 5),
         ("u1-00000000000000a2", "FIGURE", P1, ["VKM-SRC-001:p0001:f01"], 3),
         ("u1-00000000000000b1", "FORMULA", P2, ["VKM-SRC-001:p0002:e01"], 4),
         ("u1-0000000000000000", "BLOCK_GROUP", P3, ["VKM-SRC-002:p0001:b01"], 6),
         ("u1-00000000000000c9", "TABLE", P3, ["VKM-SRC-002:p0001:t01"], 2)]


def _cfg(model="lightonai/mLateOn", dim=DIM, weights="a" * 64) -> EmbeddingConfig:
    return EmbeddingConfig(model_id=model, model_revision="e" * 40, weights_file="mlateon-Q8_0.gguf",
                           weights_sha256=weights, quantization="Q8_0", mode="multivector", dimension=dim,
                           pooling="none", normalization="l2", tokenizer_sha256="b" * 64, heads_sha256="c" * 64,
                           text_rule="vkm-units-v1/A", max_len=512)


def _tokens(uid: str, n: int, dim: int = DIM) -> np.ndarray:
    seed = int.from_bytes(hashlib.sha256(uid.encode()).digest()[:8], "little")
    return l2_normalize(np.random.default_rng(seed).standard_normal((n, dim)).astype(np.float32))


def _units_dir(root, units=UNITS, snapshot="SNAP-1", texts=None):
    d = root / "units" / snapshot / "vkm-units-v1-A"
    d.mkdir(parents=True, exist_ok=True)
    rows = [{"unit_id": u, "kind": k, "page_id": p, "source_id": p.split(":")[0], "object_ids": o, "part": 0,
             "text_hash": text_hash((texts or {}).get(u, f"text of {u}"))} for u, k, p, o, _n in units]
    lines = "".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in rows)
    (d / "units.jsonl").write_text(lines, encoding="utf-8", newline="\n")
    man = {"schema": "vkm.embedding_units/1", "snapshot_id": snapshot, "text_rule": "vkm-units-v1/A",
           "count": len(rows), "units_sha256": hashlib.sha256(lines.encode()).hexdigest()}
    (d / "units.json").write_text(json.dumps(man), encoding="utf-8")
    return d


def _artifacts(root, units=UNITS, cfg=None, texts=None, writer="w0", nan_unit=None, skip=()):
    cfg = cfg or _cfg()
    w = ArtifactWriter(root, "multivector", cfg, writer_id=writer)
    rows = []
    for u, k, p, _o, n in units:
        if u in skip:
            continue
        m = _tokens(u, n, cfg.dimension)
        if u == nan_unit:
            m = m.copy()
            m[0, 0] = np.nan
        rows.append(EmbeddingRow(object_id=u, text_hash=text_hash((texts or {}).get(u, f"text of {u}")), page_id=p,
                                 object_type=k, vectors=m, token_ids=list(range(n)), worker=writer,
                                 backend="llama.cpp-vulkan"))
    w.write_part(rows[:2])
    w.write_part(rows[2:])
    return w.dir


def test_pack_layout_order_and_checks(tmp_path):
    art, units = _artifacts(tmp_path), _units_dir(tmp_path)
    rep = build_pack(art, units, publish_current=True)
    assert rep["status"] == "BUILT" and rep["count"] == 5 and rep["total_tokens"] == 20
    assert rep["checks_64"]["ok"] and rep["checks_64"]["n_missing"] == 0 and rep["pack_id"].startswith("SNAP-1-")
    pack = art / PACKS_DIR / rep["pack_id"]
    assert (pack / "tokens.f16").stat().st_size == 20 * DIM * 2
    man = json.loads((pack / "pack.json").read_text(encoding="utf-8"))
    assert man["config_signature"] == art.name and man["dtype"] == "float16" and man["by_kind"]["BLOCK_GROUP"] == 2
    assert man["files"]["tokens.f16"]["sha256"] == hashlib.sha256((pack / "tokens.f16").read_bytes()).hexdigest()
    ptr = json.loads((art / PACKS_DIR / POINTER).read_text(encoding="utf-8"))
    assert ptr["pack_id"] == rep["pack_id"] and ptr["snapshot_id"] == "SNAP-1"
    store = PackStore(pack, verify=True)
    # pack order: units of a page together (page id, then unit id)
    assert store.unit_ids == ["u1-00000000000000a1", "u1-00000000000000a2", "u1-00000000000000b1",
                              "u1-0000000000000000", "u1-00000000000000c9"]
    assert store.rows_for(P1, "PAGE") == [0, 1] and store.rows_for(P3, "PAGE") == [3, 4]
    assert store.rows_for("VKM-SRC-001:p0001:f01", "FIGURE") == [1]
    assert store.rows_for("VKM-SRC-001:p0001:f01", "TABLE") == []            # object of another kind
    assert store.rows_for("VKM-SRC-001:p0001:b01", "FIGURE") == []           # block groups are not objects
    assert store.rows_for("u1-00000000000000b1", "UNIT") == [2] and store.rows_for("nope", "PAGE") == []
    np.testing.assert_allclose(store.matrix(3), _tokens("u1-0000000000000000", 6), atol=2e-3)
    again = build_pack(art, units)
    assert again["status"] == "EXISTS" and again["pack_id"] == rep["pack_id"]


def test_maxsim_of_targets_matches_the_float32_rows(tmp_path):
    art, units = _artifacts(tmp_path), _units_dir(tmp_path)
    store = PackStore(art / PACKS_DIR / build_pack(art, units)["pack_id"], rss_budget_bytes=64)
    Q = _tokens("query", 4)
    got = {r.id: r for r in store.score_targets(Q, [(P1, "PAGE"), ("VKM-SRC-001:p0002:e01", "FORMULA"),
                                                    (P3, "PAGE"), ("VKM-SRC-009:p0001", "PAGE"),
                                                    ("u1-00000000000000c9", "UNIT")])}
    s = {u: maxsim(Q, _tokens(u, n)) for u, _k, _p, _o, n in UNITS}
    assert got[P1].status == "SCORED" and got[P1].units == 2 and got[P1].tokens == 8
    assert abs(got[P1].late_score - max(s["u1-00000000000000a1"], s["u1-00000000000000a2"])) < 1e-2
    assert got[P1].best_unit_id == max(("u1-00000000000000a1", "u1-00000000000000a2"), key=s.get)
    assert abs(got["VKM-SRC-001:p0002:e01"].late_score - s["u1-00000000000000b1"]) < 1e-2
    assert got["VKM-SRC-009:p0001"].status == "NO_TOKENS" and got["VKM-SRC-009:p0001"].late_score is None
    assert abs(got["u1-00000000000000c9"].late_score - s["u1-00000000000000c9"]) < 1e-2
    # batched slices (reduceat) = one product per unit
    per_unit = store.unit_scores(Q, range(len(store)), chunk_tokens=1)
    batched = store.unit_scores(Q, range(len(store)))
    assert per_unit.keys() == batched.keys() and all(abs(per_unit[r] - batched[r]) < 1e-5 for r in batched)
    assert store.get(["u1-00000000000000a2", "x"]).keys() == {"u1-00000000000000a2"}


def test_page_targets_can_exclude_bibliography_units_cp42(tmp_path):
    bib = ("u1-00000000000000a0", "BIB_ENTRY", P1, ["VKM-SRC-001:p0001:r01"], 7)
    units = [*UNITS, bib]
    art = _artifacts(tmp_path, units=units)
    store = PackStore(art / PACKS_DIR / build_pack(art, _units_dir(tmp_path, units=units))["pack_id"])
    assert len(store.rows_for(P1, "PAGE")) == 3
    rows = store.rows_for(P1, "PAGE", page_exclude_kinds=("BIB_ENTRY",))
    assert [store.kinds[r] for r in rows] == ["BLOCK_GROUP", "FIGURE"]
    Q = _tokens(bib[0], 7)[:3]                                  # the bibliography unit would win the page
    with_bib, = store.score_targets(Q, [(P1, "PAGE")])
    without, = store.score_targets(Q, [(P1, "PAGE")], page_exclude_kinds=("BIB_ENTRY",))
    assert with_bib.best_unit_id == bib[0] and with_bib.units == 3
    assert without.best_unit_id != bib[0] and without.units == 2 and without.late_score < with_bib.late_score
    ref, = store.score_targets(Q, [("VKM-SRC-001:p0001:r01", "BIB_ENTRY")], page_exclude_kinds=("BIB_ENTRY",))
    assert ref.status == "SCORED" and ref.best_unit_id == bib[0]            # explicit bibliography targets still work


BIB = [("u1-00000000000000e1", "BIB_ENTRY", P1, ["VKM-SRC-001:p0001:r01"], 4),
       ("u1-00000000000000e2", "BIB_ENTRY", P3, ["VKM-SRC-002:p0001:r01"], 3),
       ("u1-00000000000000e3", "BIB_ENTRY", P3, ["VKM-SRC-002:p0001:r02"], 5)]


def test_bibliography_units_trail_and_scan_ranks_pages_by_their_best_entry(tmp_path):
    units = [*UNITS, *BIB]
    art = _artifacts(tmp_path, units=units)
    rep = build_pack(art, _units_dir(tmp_path, units=units), packs_dir=tmp_path / "elsewhere")
    assert rep["layout"] == {"order": "page_id, unit_id", "trailing_kinds": ["BIB_ENTRY"]}
    assert (tmp_path / "elsewhere" / rep["pack_id"] / "pack.json").is_file() and not (art / PACKS_DIR).exists()
    store = PackStore(tmp_path / "elsewhere" / rep["pack_id"])
    assert store.kinds[-3:] == ["BIB_ENTRY"] * 3 and "BIB_ENTRY" not in store.kinds[:-3]
    assert list(store.kind_rows("BIB_ENTRY")) == [5, 6, 7] and store.info()["trailing_kinds"] == ["BIB_ENTRY"]
    assert store.rows_for(P1, "PAGE") == [0, 1, 5]                        # two ranges: the page and its entries
    assert store.rows_for(P1, "PAGE", page_exclude_kinds=("BIB_ENTRY",)) == [0, 1]
    Q = _tokens("u1-00000000000000e3", 5)[:2]                            # matches the second entry of P3 best
    scan = store.scan(Q, "BIB_ENTRY", top_pages=10)
    want = {u: maxsim(Q, _tokens(u, n)) for u, _k, _p, _o, n in BIB}
    assert [s["page_id"] for s in scan] == [P3, P1]
    assert scan[0]["best_unit_id"] == "u1-00000000000000e3" and scan[0]["units"] == 2 and scan[1]["units"] == 1
    assert abs(scan[0]["late_score"] - want["u1-00000000000000e3"]) < 1e-2
    assert abs(scan[1]["late_score"] - want["u1-00000000000000e1"]) < 1e-2
    assert store.scan(Q, "BIB_ENTRY", top_pages=1) == scan[:1] and store.scan(Q, "TABLE", top_pages=5)[0]["page_id"] == P3
    assert store.scan(Q, "NO_SUCH_KIND") == []
    # an older pack without the trailing region scans the same (runs of rows instead of one region)
    old = build_pack(art, _units_dir(tmp_path, units=units, snapshot="S0"), packs_dir=tmp_path / "old",
                     trailing_kinds=())
    legacy = PackStore(tmp_path / "old" / old["pack_id"])
    assert list(legacy.kind_rows("BIB_ENTRY")) == [2, 6, 7] and legacy.rows_for(P1, "PAGE") == [0, 1, 2]
    again = legacy.scan(Q, "BIB_ENTRY", top_pages=10)
    assert [(s["page_id"], s["best_unit_id"], s["units"]) for s in again] == \
        [(s["page_id"], s["best_unit_id"], s["units"]) for s in scan]
    assert all(abs(a["late_score"] - b["late_score"]) < 1e-4 for a, b in zip(again, scan))


@pytest.mark.parametrize("case", ["missing", "nan", "duplicate", "stale_text", "rule"])
def test_failed_checks_write_no_pack_and_keep_current(tmp_path, case):
    art, units = _artifacts(tmp_path), _units_dir(tmp_path)
    first = build_pack(art, units, publish_current=True)
    if case == "missing":
        units = _units_dir(tmp_path, units=[*UNITS, ("u1-00000000000000ff", "TABLE", P2, ["t9"], 2)], snapshot="S2")
    elif case == "nan":
        art = _artifacts(tmp_path / "b", nan_unit="u1-00000000000000b1")
        units = _units_dir(tmp_path / "b")
    elif case == "duplicate":
        _artifacts(tmp_path, writer="w1")                            # the same rows again under another writer
        units = _units_dir(tmp_path, snapshot="S2")
    elif case == "stale_text":
        units = _units_dir(tmp_path, snapshot="S2", texts={"u1-00000000000000a1": "a new text"})
    else:
        man = json.loads((units / "units.json").read_text(encoding="utf-8"))
        (units / "units.json").write_text(json.dumps({**man, "text_rule": "vkm-units-v1/B"}), encoding="utf-8")
    with pytest.raises(PackError) as exc:
        build_pack(art, units, publish_current=True)
    assert exc.value.code in ("E_CHECK_FAILED", "E_REFUSED")
    packs = art / PACKS_DIR
    names = sorted(p.name for p in packs.iterdir() if p.is_dir()) if packs.exists() else []
    if case == "nan":
        assert names == [] and not (packs / POINTER).exists()
    else:
        assert names == [first["pack_id"]]
        assert json.loads((packs / POINTER).read_text(encoding="utf-8"))["pack_id"] == first["pack_id"]
    if case == "missing":
        assert exc.value.details["missing"] == ["u1-00000000000000ff"]


def test_new_snapshot_pack_hot_reload_and_prune(tmp_path):
    art, units = _artifacts(tmp_path), _units_dir(tmp_path)
    p1 = build_pack(art, units, publish_current=True)["pack_id"]
    now = [0.0]
    handle = PackHandle(art, check_s=10.0, clock=lambda: now[0])
    assert handle.current().info()["pack_id"] == p1 and handle.status()["status"] == "READY"
    # a new snapshot adds a bibliography entry: only it needs encoding, the pack is rebuilt and published
    extra = ("u1-00000000000000d4", "BIB_ENTRY", P2, ["VKM-SRC-001:p0002:r01"], 3)
    ArtifactWriter(tmp_path, "multivector", _cfg(), writer_id="w2").write_part([EmbeddingRow(
        object_id=extra[0], text_hash=text_hash(f"text of {extra[0]}"), page_id=P2, object_type="BIB_ENTRY",
        vectors=_tokens(extra[0], 3), token_ids=[1, 2, 3])])
    p2 = build_pack(art, _units_dir(tmp_path, units=[*UNITS, extra], snapshot="SNAP-2"), publish_current=True)
    assert p2["status"] == "BUILT" and p2["count"] == 6 and p2["pack_id"].startswith("SNAP-2-")
    assert handle.current().info()["pack_id"] == p1                        # not re-read before check_s
    now[0] = 11.0
    cur = handle.current()
    assert cur.info()["pack_id"] == p2["pack_id"] and cur.rows_for("VKM-SRC-001:p0002:r01", "BIB_ENTRY")
    assert handle.status()["loads"] == 2
    p3 = build_pack(art, _units_dir(tmp_path, snapshot="SNAP-3"), publish_current=True, keep=2)
    assert p3["pruned"] == [p1] and sorted(p.name for p in (art / PACKS_DIR).iterdir() if p.is_dir()) == \
        sorted([p2["pack_id"], p3["pack_id"]])
    publish(art / PACKS_DIR, json.loads((art / PACKS_DIR / p2["pack_id"] / "pack.json").read_text()))
    assert prune(art / PACKS_DIR, keep=1) == []                            # the CURRENT one is kept, the newest too


def test_handle_missing_mismatch_and_direct_pack(tmp_path):
    art = _artifacts(tmp_path)
    handle = PackHandle(art)
    assert handle.status()["status"] == "MISSING"
    with pytest.raises(LookupError, match="MISSING"):
        handle.current()
    rep = build_pack(art, _units_dir(tmp_path), publish_current=True)
    bad = PackHandle(art, expect={"model_id": "lightonai/mLateOn", "weights_sha256": "f" * 64, "dimension": DIM})
    assert bad.status()["status"] == "MISMATCH" and "weights_sha256" in bad.status()["reason"]
    with pytest.raises(LookupError):
        bad.current()
    good = PackHandle(art / PACKS_DIR / rep["pack_id"], expect={"model_id": "lightonai/mLateOn", "dimension": DIM,
                                                                 "weights_sha256": "a" * 64})
    assert good.status()["status"] == "READY" and good.status()["directory_kind"] == "pack"


def test_rss_budget_releases_mapped_pages(tmp_path):
    art = _artifacts(tmp_path)
    store = PackStore(art / PACKS_DIR / build_pack(art, _units_dir(tmp_path))["pack_id"], rss_budget_bytes=100)
    for _ in range(3):
        store.unit_scores(_tokens("q", 2), range(len(store)))
    import mmap
    import sys

    if sys.platform != "win32" and hasattr(mmap, "MADV_DONTNEED"):
        assert store.releases >= 3
    store.close()


def test_verify_pack_reports_float16_deviation(tmp_path):
    art = _artifacts(tmp_path)
    rep = build_pack(art, _units_dir(tmp_path))
    v = verify_pack(art / PACKS_DIR / rep["pack_id"], art, sample=5, query_tokens=2)
    assert v["ok"] and v["sampled"] == 5 and v["missing"] == 0
    assert v["max_abs_token_value_diff"] < 1e-3 and v["maxsim_rel_dev"]["max"] < 5e-3


def test_embed_pack_cli(tmp_path, capsys):
    from vkm_corpus.embeddings.cli import main

    art, units = _artifacts(tmp_path), _units_dir(tmp_path)
    assert main(["pack", "--artifacts", str(art), "--units", str(units), "--publish"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "BUILT" and out["published"]["pack_id"] == out["pack_id"]
    fewer = _units_dir(tmp_path, units=UNITS[:2], snapshot="S2")          # 3 rows become orphaned history
    assert main(["pack", "--artifacts", str(art), "--units", str(fewer)]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["status"] == "BUILT" and res["count"] == 2 and res["checks_64"]["orphaned"] == 3
    more = _units_dir(tmp_path, units=[*UNITS, ("u1-00000000000000ee", "TABLE", P2, ["t8"], 2)], snapshot="S3")
    assert main(["pack", "--artifacts", str(art), "--units", str(more)]) == 1
    res = json.loads(capsys.readouterr().out)
    assert res["status"] == "FAILED" and res["error"]["code"] == "E_CHECK_FAILED"
