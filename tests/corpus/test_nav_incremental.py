"""Real manifest/Parquet/pack lifecycle; tiny deterministic derived builders.

No embedding model, external service or corpus is used. The sections builder is
the actual implementation; other builders expose exact input consumption.
"""
from __future__ import annotations

import copy
import json
import shutil

import duckdb
import pyarrow as pa
import pytest

from vkm_corpus.navigation import cli, dependencies as D, store
from vkm_corpus.navigation.ids import RULE_VERSIONS
from vkm_corpus.navigation.manifest import load_datasets
from test_nav_sections import Canon


@pytest.fixture
def nav(tmp_path, monkeypatch):
    canon = Canon()
    canon.source("SRC-X", 6)
    canon.con.execute("UPDATE meta.snapshot SET manifest_sha256=?", ["ab" * 32])
    seen = {}
    real = cli.resolve_part

    def resolve(part):
        if part == "sections":
            return real(part)

        def builder(con, **values):
            seen[part] = {name: table.to_pylist() for name, table in values["datasets"].items()}
            names = cli.datasets_of(part)
            # Formula/parameter outputs model deterministic, configurable transforms.
            payload = json.dumps({"part": part, "variant": values.get("variant", 0),
                                  "inputs": {name: seen[part].get(name) for name in D.upstream(part, values)}},
                                 sort_keys=True)
            return {name: pa.table({"payload": [payload]}) for name in names}

        return builder

    monkeypatch.setattr(cli, "resolve_part", resolve)
    root = tmp_path / "data"
    out = root / "derived" / "navigation" / "snap-test"
    yield canon.con, out, root, seen
    canon.con.close()


PARTS = ["sections", "formulas", "tables", "parameters", "duplicates", "object_duplicates",
         "concepts", "translations", "topics"]
OUTLINES = {"SRC-X": [{"level": 1, "title": "First fully synthetic chapter", "page_index": 1},
                      {"level": 1, "title": "Second fully synthetic chapter", "page_index": 4}]}


def initial(nav, **kwargs):
    con, out, *_ = nav
    return cli.build_parts(con, out, PARTS, **kwargs)


def test_upstream_change_invalidates_transitive_closure_and_pack_ignores_physical_files(nav):
    con, out, root, _ = nav
    old = initial(nav)
    old_topics = (out / "topics.parquet").read_bytes()
    new = cli.build_parts(con, out, ["sections"], outlines=OUTLINES)
    affected = set(PARTS) - {"sections", "duplicates"}
    assert {p for p, row in new["parts"].items() if row["status"] == "STALE_UPSTREAM"} == affected
    assert new["parts"]["duplicates"] == old["parts"]["duplicates"]
    assert (out / "topics.parquet").read_bytes() == old_topics
    assert "topics" not in new["capabilities"]
    packed = store.pack(out)
    assert set(packed["tables"]) == {"sections", "section_pages", "dup_clusters", "dup_members", "source_overlap"}
    assert store.publish(root, "snap-test").read_text().strip() == "snap-test"
    packed_con = duckdb.connect(str(out / "nav.duckdb"), read_only=True)
    assert "topics" not in {r[0] for r in packed_con.execute("SHOW TABLES").fetchall()}
    packed_con.close()
    canonical_file = root / "canonical.duckdb"
    db = duckdb.connect(str(canonical_file))
    db.execute("CREATE SCHEMA canonical")
    db.close()
    serving = store.NavStore(root, canonical_db=canonical_file)
    assert "topics" not in serving.datasets()
    with pytest.raises(store.NavUnavailable, match="no topics"):
        serving.require("topics")
    serving._con.close()


def test_identical_rebuild_reuses_downstream_and_vector_bytes(nav, tmp_path):
    con, out, _, _ = nav
    vectors = tmp_path / "vectors"
    vectors.mkdir()
    (vectors / "config.json").write_text('{"synthetic":true}', encoding="utf-8")
    (vectors / "part-000.parquet").write_bytes(b"synthetic vectors: builder does not embed")
    old = initial(nav, vectors=str(vectors))
    vector_bytes = {p.name: p.read_bytes() for p in vectors.iterdir()}
    new = cli.build_parts(con, out, ["sections"], vectors=str(vectors))
    assert all(new["parts"][p] == old["parts"][p] for p in PARTS if p != "sections")
    assert {p.name: p.read_bytes() for p in vectors.iterdir()} == vector_bytes
    assert new["parts"]["topics"]["input_identity"]["external"]["vectors"]


@pytest.mark.parametrize("part,affected", [
    ("formulas", {"tables", "parameters", "object_duplicates", "translations"}),
    ("tables", {"parameters"}), ("concepts", {"translations", "topics"}),
    ("parameters", set()), ("duplicates", {"object_duplicates"}),
])
def test_precise_affected_only_part_graph(nav, part, affected):
    con, out, *_ = nav
    old = initial(nav)
    new = cli.build_parts(con, out, [part], part_options={part: {"variant": 1}})
    assert {p for p, row in new["parts"].items() if row["status"] == "STALE_UPSTREAM"} == affected
    for p in set(PARTS) - affected - {part}:
        assert new["parts"][p] == old["parts"][p]


def test_opted_in_duplicate_context_invalidates_concepts_and_children(nav):
    con, out, *_ = nav
    initial(nav, part_options={"concepts": {"drop_duplicate_blocks": True}})
    new = cli.build_parts(con, out, ["duplicates"], part_options={"duplicates": {"variant": 1}})
    assert {p for p, row in new["parts"].items() if row["status"] == "STALE_UPSTREAM"} == {
        "object_duplicates", "concepts", "translations", "topics"}


def test_stale_external_inputs_cannot_restore_old_children_or_override_current_sections(nav, tmp_path):
    con, out, _, seen = nav
    initial(nav)
    previous = tmp_path / "old-nav"
    shutil.copytree(out, previous)
    cli.build_parts(con, out, ["sections"], outlines=OUTLINES)
    inputs, ref = cli.load_inputs(previous, "snap-test", skip=cli.datasets_of("topics"))
    new = cli.build_parts(con, out, ["topics"], inputs=inputs, inputs_ref=ref)
    current_sections = load_datasets(out)[0]["sections"].to_pylist()
    assert seen["topics"]["sections"] == current_sections
    assert "terms" not in seen["topics"] and "formula_context" not in seen["topics"]
    assert new["parts"]["topics"]["status"] == "BUILT"
    assert new["parts"]["concepts"]["status"] == "STALE_UPSTREAM"


def test_rebuilding_full_child_closure_clears_tombstones_and_pack_admits(nav):
    con, out, *_ = nav
    initial(nav)
    cli.build_parts(con, out, ["sections"], outlines=OUTLINES)
    new = cli.build_parts(con, out, PARTS[1:])
    assert all(row["status"] == "BUILT" for row in new["parts"].values())
    assert not new["invalidated_datasets"]
    assert len(store.pack(out)["tables"]) == len(new["datasets"])


@pytest.mark.parametrize("kind", ["rule", "options", "vectors"])
def test_explicit_recipe_changes_invalidate_without_requesting_the_part(nav, tmp_path, monkeypatch, kind):
    con, out, *_ = nav
    vectors = tmp_path / "vec"
    vectors.mkdir()
    (vectors / "part-0.parquet").write_bytes(b"AAAA")
    initial(nav, vectors=str(vectors))
    options = {}
    if kind == "rule":
        monkeypatch.setitem(RULE_VERSIONS, "concepts", "next_rule")
    elif kind == "options":
        options["part_options"] = {"concepts": {"variant": 7}}
    else:
        (vectors / "part-0.parquet").write_bytes(b"BBBB")
        options["vectors"] = str(vectors)
    new = cli.build_parts(con, out, ["parameters"], **options)
    expected = {"concepts", "translations", "topics"} if kind != "vectors" else {"duplicates", "object_duplicates", "topics"}
    assert expected <= {p for p, row in new["parts"].items() if row["status"] == "STALE_UPSTREAM"}


@pytest.mark.parametrize("tamper", ["restore_stale", "upstream_hash", "erase_dependency", "options"])
def test_pack_refuses_forged_dependency_manifests(nav, tamper):
    con, out, *_ = nav
    old = initial(nav)
    if tamper == "restore_stale":
        new = cli.build_parts(con, out, ["sections"], outlines=OUTLINES)
        new["datasets"]["topics"] = old["datasets"]["topics"]
        new["capabilities"].append("topics")
    else:
        new = old
        proof = new["parts"]["topics"]["input_identity"]
        if tamper == "upstream_hash":
            proof["upstream"]["sections"]["sha256"] = "00" * 32
        elif tamper == "erase_dependency":
            proof["upstream"].pop("sections")
        else:
            proof["options"] = {"min_topic_size": 3}
        new["parts"]["topics"]["input_sha256"] = D.digest(proof)
    (out / "manifest.json").write_text(json.dumps(new), encoding="utf-8")
    with pytest.raises(ValueError, match="stale|dependency"):
        store.pack(out)


def test_failed_child_rebuild_does_not_expose_previous_output(nav, monkeypatch):
    con, out, *_ = nav
    initial(nav)
    monkeypatch.setattr(cli, "resolve_part", lambda part: lambda con, **kw: None)
    new = cli.build_parts(con, out, ["concepts"])
    assert new["parts"]["concepts"]["status"] == "SKIPPED_NO_INPUT"
    assert all(new["parts"][p]["status"] == "STALE_UPSTREAM" for p in ("translations", "topics"))
    assert "terms" not in store.pack(out)["tables"]


def test_options_cannot_smuggle_unchecked_derived_inputs(nav):
    con, out, *_ = nav
    initial(nav)
    with pytest.raises(ValueError, match="override checked"):
        cli.build_parts(con, out, ["topics"], part_options={"topics": {"sections": "old_sections"}})


def test_bare_sql_fallback_cannot_bypass_input_provenance(nav):
    con, out, *_ = nav
    con.register("sections", pa.table({"forged": [1]}))
    with pytest.raises(ValueError, match="unchecked unqualified"):
        cli.build_parts(con, out, ["parameters"])


def test_rebuild_cannot_claim_historical_external_inputs_that_were_not_loaded(nav, tmp_path):
    con, out, *_ = nav
    cli.build_parts(con, out, ["sections"])
    inputs, ref = cli.load_inputs(out, "snap-test")
    other = tmp_path / "only-formulas"
    cli.build_parts(con, other, ["formulas"], inputs=inputs, inputs_ref=ref)
    with pytest.raises(ValueError, match="requires checked --inputs"):
        cli.build_parts(con, other, ["formulas"])


def test_local_upstream_change_invalidates_retained_children_through_external_parent(nav, tmp_path):
    con, out, *_ = nav
    initial(nav)
    inputs, ref = cli.load_inputs(out, "snap-test")
    other = tmp_path / "translations-only"
    cli.build_parts(con, other, ["translations"], inputs=inputs, inputs_ref=ref)
    new = cli.build_parts(con, other, ["sections"], outlines=OUTLINES)
    assert new["parts"]["translations"]["status"] == "STALE_UPSTREAM"
    assert "term_translations" not in store.pack(other)["tables"]


def test_legacy_inputs_do_not_gain_dependency_qualification(nav, tmp_path):
    con, out, *_ = nav
    cli.build_parts(con, out, ["sections"])
    manifest = json.loads((out / "manifest.json").read_text())
    manifest.pop("dependency_contract")
    (out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    inputs, ref = cli.load_inputs(out, "snap-test")
    result = cli.build_parts(con, tmp_path / "child", ["formulas"], inputs=inputs, inputs_ref=ref)
    assert result["identity_status"] == "AD_HOC_UNVERIFIED"


def test_builder_order_cannot_bind_not_yet_built_upstream(nav):
    con, out, *_ = nav
    initial(nav)
    with pytest.raises(ValueError, match="dependency order"):
        cli.build_parts(con, out, ["topics", "sections"])


def test_legacy_retained_parts_require_new_provenance_without_deleting_files(nav):
    con, out, *_ = nav
    old = initial(nav)
    old.pop("dependency_contract")
    for row in old["parts"].values():
        row.pop("input_identity")
        row.pop("input_sha256")
    (out / "manifest.json").write_text(json.dumps(old), encoding="utf-8")
    new = cli.build_parts(con, out, ["sections"])
    assert new["parts"]["topics"]["status"] == "STALE_UPSTREAM"
    assert (out / "topics.parquet").exists()
    assert set(store.pack(out)["tables"]) == {"sections", "section_pages"}


def test_file_option_identity_survives_reloading_and_same_bytes_at_another_path(nav, tmp_path):
    con, out, *_ = nav
    option = tmp_path / "term-vectors.arrow"
    option.write_bytes(b"synthetic checked option")
    old = initial(nav, part_options={"translations": {"term_vectors": str(option)}})
    recorded = old["parts"]["translations"]["options"]["term_vectors"]
    assert recorded == D.file_identity(option)
    store.pack(out)
    alternate = tmp_path / "copied"
    alternate.mkdir()
    copy = alternate / option.name
    copy.write_bytes(option.read_bytes())
    new = cli.build_parts(con, out, ["parameters"],
                          part_options={"translations": {"term_vectors": str(copy)}})
    assert new["parts"]["translations"] == old["parts"]["translations"]
    store.pack(out)


def test_option_bytes_changed_during_build_cannot_publish_new_manifest(nav, tmp_path, monkeypatch):
    con, out, *_ = nav
    initial(nav)
    before = (out / "manifest.json").read_bytes()
    option = tmp_path / "term-vectors.arrow"
    option.write_bytes(b"AAAA")
    resolve = cli.resolve_part
    builder = resolve("translations")

    def mutate(con, **kwargs):
        result = builder(con, **kwargs)
        option.write_bytes(b"BBBB")
        return result

    monkeypatch.setattr(cli, "resolve_part", lambda part: mutate if part == "translations" else resolve(part))
    with pytest.raises(ValueError, match="dependency bytes changed"):
        cli.build_parts(con, out, ["translations"],
                        part_options={"translations": {"term_vectors": str(option)}})
    assert (out / "manifest.json").read_bytes() == before
    store.pack(out)
