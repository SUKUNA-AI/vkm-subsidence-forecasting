"""NAV graph in Neo4j, offline: registry, projection rows and accounting, Cypher templates, the loader with checks
N1–N7 over the in-memory fake driver, snapshot replacement, drop, dry run and the query shaping (no Neo4j, no corpus
text: synthetic datasets of ``vkm_corpus.testing.nav_graph``)."""
from __future__ import annotations

import json

import pytest

pa = pytest.importorskip("pyarrow")
pytest.importorskip("duckdb")
pytest.importorskip("numpy")

from vkm_corpus.graph import nav as L  # noqa: E402
from vkm_corpus.graph import nav_query as Q  # noqa: E402
from vkm_corpus.graph import nav_schema as N  # noqa: E402
from vkm_corpus.graph import schema as S  # noqa: E402
from vkm_corpus.graph.common import ProjectionError  # noqa: E402
from vkm_corpus.graph.nav_rows import (NavInput, ProjectionOptions, accounting, expected_counts,  # noqa: E402
                                       iter_nodes, iter_rels, match_definition, tree_problems)
from vkm_corpus.testing import nav_graph as SY  # noqa: E402


@pytest.fixture()
def nav(tmp_path):
    ids = SY.write_synthetic_nav(tmp_path / "nav")
    return tmp_path / "nav", ids


def _options(nav_dir, **kw):
    return L.NavLoadOptions(nav_dir=nav_dir, projection=ProjectionOptions(symbol_morphology="surface"), **kw)


def _loaded(nav_dir, **kw):
    fake = SY.FakeNavNeo4j()
    SY.add_document_graph(fake, nav_dir, **kw)
    receipt = L.load(None, _options(nav_dir), driver=fake)
    return fake, receipt


# ------------------------------------------------------------------------------------------------ registry
def test_registry_is_consistent_and_does_not_collide_with_document():
    assert N.validate_registry() == []
    assert not set(N.NAV_LABELS) & set(S.NODE_BY_LABEL)
    assert not {r.type for r in N.REL_TYPES} & {r.type for r in S.REL_TYPES}
    assert S.LAYER_DEPENDENCIES["NAVIGATION"] == {"DOCUMENT"} and N.LAYER_LABEL == "NavigationLayer"
    assert {r.name for r in N.REL_TYPES if r.type == "NAV_CHILD_OF"} == {"NAV_CHILD_OF:NavSection",
                                                                         "NAV_CHILD_OF:NavTopic"}
    for rel in N.REL_TYPES:                                       # provenance on every edge
        assert {"layer", "snapshot_id", "rule_version"} <= set(rel.all_properties)
    ddl = N.ddl_script()
    items = N.ddl_items()
    assert all(i.name.startswith("nav_") and "IF NOT EXISTS" in i.statement for i in items)
    assert len({i.name for i in items}) == len(items) and "nav_co_occurs_edge_id_unique" in ddl
    assert "ENTERPRISE" not in ddl.upper() and "IS NOT NULL" not in ddl       # Community edition only
    test_ns = S.Namespace.for_test("0000abcd")
    assert all(i.name.startswith("vkmtest0000abcd_nav_") for i in N.ddl_items(test_ns))


def test_document_checks_tolerate_the_nav_layer():
    """C6/C7 of the DOCUMENT graph treat NAV nodes and types as another registered layer, not as foreign; a DOCUMENT
    rebuild can drop the dependent NAV layer first (R6 --cascade)."""
    import inspect

    from vkm_corpus.cli import build_parser
    from vkm_corpus.graph import loader, verify
    from vkm_corpus.publish import reconcile

    src = inspect.getsource(verify.run_checks)
    assert "other_layers" in src and "nav_schema.REL_TYPE_NAMES" in src
    argv = ["graph", "rebuild", "--cascade"]
    assert build_parser(argv).parse_args(argv).cascade is True
    assert loader.RebuildOptions().cascade is False and "drop_layer" in inspect.getsource(loader._execute)
    assert "cascade=True" in inspect.getsource(reconcile.reconcile)       # a new snapshot drops the stale NAV graph


# ------------------------------------------------------------------------------------------------ rows
def test_rows_counts_accounting_and_rules(nav):
    nav_dir, ids = nav
    with NavInput.open(nav_dir, ProjectionOptions(symbol_morphology="surface")) as inp:
        exp = expected_counts(inp)
        acct = accounting(inp, exp)
        assert exp["skipped"] == {}
        assert exp["nodes"] == {"NavSection": 5, "FormulaSymbol": 3, "ParameterCandidate": 1, "Term": 8,
                                "NavTopic": 3}
        r = exp["rels"]
        assert r["NAV_CHILD_OF:NavSection"] == 2 and r["HAS_NAV_SECTION"] == 3
        assert r["COVERS_PAGE"] == 4 + 2 + 2 + 2 + 3                        # full ranges: chapter 1 covers 1-4
        assert r["IN_SECTION"] == 4 and r["DEFINED_FOR"] == 5              # formula 5 has no section; bare symbols out
        assert r["NAV_REFERS_TO"] == 1 and r["NAV_BLOCK_REFERS_TO"] == 1 and r["NEAR_FORMULA"] == 1
        assert r["CO_OCCURS"] == 4 and r["CONTAINS_TERM"] == 2 and r["SAME_TERM_AS"] == 1 and r["DEFINED_AS"] == 1
        assert r["MENTIONED_IN"] == 10                                      # (term, section) pairs, windows summed
        assert r["IN_TOPIC"] == 4 and r["RELATED_TOPIC"] == 1 and r["NAV_CHILD_OF:NavTopic"] == 1
        assert r["SYMBOL_OF"] == 3                                          # σ→напряжение, ε̇→скорость ползучести, w→оседание
        for ds, a in acct.items():
            if isinstance(a, dict) and a.get("closed") is not None:
                assert a["closed"], (ds, a)
        assert acct["formula_symbols"]["skipped"]["no definition (bare occurrence)"] == 2
        assert acct["term_edges"]["folded"] == {"Term.same_as_refs": 1}
        assert acct["term_mentions"]["folded"]["windows of one (term, section) pair"] == 1
        assert acct["term_mentions"]["skipped"]["unit without a section (heading group, page)"] == 1
        assert acct["section_pages"]["folded"] == {"COVERS_PAGE.deepest": 9}
        assert acct["formula_refs"]["skipped"]["unresolved (no formula)"] == 1
        assert acct["links"]["rule_version"] == N.RULE_SYMBOL_OF_SURFACE

        nodes = {n.id: n.props for n in iter_nodes(inp, N.NODE_BY_LABEL["Term"])}
        creep = nodes[ids["terms"]["ползучесть"]]
        assert creep["same_as_refs"] == ["en:salt creep"] and "ползучести" in creep["name_keys"]
        for props in nodes.values():
            assert props["layer"] == "NAV" and props["snapshot_id"] == SY.SNAPSHOT and props["rule_version"]
            assert props["review_status"] == "AUTO_EXTRACTED_UNREVIEWED"
        covers = list(iter_rels(inp, N.REL_BY_NAME["COVERS_PAGE"]))
        ch1 = [c for c in covers if c.from_id == ids["sections"]["ch1"]]
        assert [c.to_id for c in ch1] == [SY.pid(SY.S1, i) for i in range(1, 5)]
        assert not any(c.props["deepest"] for c in ch1)                    # its children are the deepest sections
        assert all(c.props["rule_version"] == N.RULE_COVERS_PAGE for c in covers)
        mentions = {(m.from_id, m.to_id): m.props for m in iter_rels(inp, N.REL_BY_NAME["MENTIONED_IN"])}
        rate = mentions[(ids["terms"]["скорость ползучесть"], ids["sections"]["s12"])]
        assert rate["tf"] == 3 and rate["n_units"] == 2 and rate["page_ids"] == [SY.pid(SY.S1, 3), SY.pid(SY.S1, 4)]
        co = list(iter_rels(inp, N.REL_BY_NAME["CO_OCCURS"]))
        assert all(c.from_id < c.to_id and c.key.startswith("TED-") and c.props["npmi"] > 0 for c in co)
        sym = {(s.from_id, s.to_id): s.props for s in iter_rels(inp, N.REL_BY_NAME["SYMBOL_OF"])}
        rate_symbol = SY.nav_ids.symbol_id(SY.S1, "ε̇")
        assert sym[(ids["terms"]["скорость ползучесть"], rate_symbol)] == {
            "n_formulas": 2, "match": "FULL", "morphology": "surface", "layer": "NAV", "snapshot_id": SY.SNAPSHOT,
            "rule_version": N.RULE_SYMBOL_OF_SURFACE}
        assert (ids["terms"]["оседание"], SY.nav_ids.symbol_id(SY.S2, "w")) in sym     # a seed word starting it
        # agent T's layout: labels from label terms, section aggregates on NavSection (no nested lists)
        topics = {n.id: n.props for n in iter_nodes(inp, N.NODE_BY_LABEL["NavTopic"])}
        top = topics[SY.TOPIC_IDS[0]]
        assert top["label"] == "реология; соль" and top["n_children"] == 1 and top["rule_version"] == "topics_v1"
        sections = {n.id: n.props for n in iter_nodes(inp, N.NODE_BY_LABEL["NavSection"])}
        agg = sections[ids["sections"]["s12"]]
        assert agg["n_pages"] == 2 and agg["key_terms"] == ["конвергенция", "скорость ползучести"]
        assert "central_object_ids" not in agg and "n_pages" not in sections[ids["sections"]["s11"]]
        related = list(iter_rels(inp, N.REL_BY_NAME["RELATED_TOPIC"]))
        assert [(x.from_id, x.to_id, x.props["cosine"], x.props["n_links"]) for x in related] == [
            (SY.TOPIC_IDS[1], SY.TOPIC_IDS[2], 0.42, 3)]
        assert acct["section_aggregates"]["folded"] == {"NavSection properties": 1}


def test_mentions_top_k_rule(nav):
    nav_dir, _ids = nav
    with NavInput.open(nav_dir, ProjectionOptions(mentions_top_per_term=1, mentions_top_per_section=1,
                                                  symbol_morphology="surface")) as inp:
        exp = expected_counts(inp)
        acct = accounting(inp, exp)
        assert exp["rels"]["MENTIONED_IN"] < 10 and acct["term_mentions"]["closed"]
        kept = list(iter_rels(inp, N.REL_BY_NAME["MENTIONED_IN"]))
        assert all(k.props["rank_in_term"] == 1 or k.props["rank_in_section"] == 1 for k in kept)


def test_symbol_definition_matching_rule():
    key = {"скорость ползучесть": "T1", "ползучесть": "T2", "коэффициент": "T3", "коэффициент форма": "T4",
           "tensile stress": "T5", "crack growth": "T6"}
    assert match_definition("скорость ползучесть", ["скорость ползучесть", "ползучесть"], key, set()) == [
        ("T1", "скорость ползучесть", "FULL")]
    # a leading phrase, never a far one; single generic words are not concepts
    assert match_definition("коэффициент форма характеризующий влияние", ["коэффициент форма характеризующий влияние",
                                                                         "коэффициент форма", "коэффициент"],
                            key, set()) == [("T4", "коэффициент форма", "PREFIX")]
    assert match_definition("maximal tensile stress in the direction of crack growth",
                            ["maximal tensile stress in the direction of crack growth", "tensile stress",
                             "crack growth"], key, set()) == [("T5", "tensile stress", "NEAR_START")]
    assert match_definition("коэффициент нестационарный теплообмен", ["коэффициент"], key, set()) == []
    assert match_definition("коэффициент нестационарный теплообмен", ["коэффициент"], key, {"T3"}) == [
        ("T3", "коэффициент", "PREFIX")]


def test_trees_and_cycles():
    assert tree_problems([("a", "b"), ("b", "c")]) == []
    assert tree_problems([("a", "b"), ("b", "a")]) == ["on a cycle: a", "on a cycle: b"]
    assert tree_problems([("a", "b"), ("a", "c")]) == ["several parents: a"]


def test_topics_are_optional_and_unknown_columns_are_skipped(tmp_path):
    nav_dir = tmp_path / "nav"
    SY.write_synthetic_nav(nav_dir, topics=False)
    with NavInput.open(nav_dir, ProjectionOptions(symbol_morphology="surface")) as inp:
        exp = expected_counts(inp)
        assert exp["nodes"]["NavTopic"] == 0 and "NavTopic" in exp["skipped"] and "IN_TOPIC" in exp["skipped"]
    # a topics dataset with columns the projection does not know → skipped with the reason, never a crash
    import pyarrow.parquet as pq

    nav2 = tmp_path / "nav2"
    SY.write_synthetic_nav(nav2)
    pq.write_table(pa.table({"cluster": ["x"], "name": ["y"]}), nav2 / "topics.parquet")
    manifest = json.loads((nav2 / "manifest.json").read_text(encoding="utf-8"))
    manifest["datasets"]["topics"].update(rows=1, sha256=SY._sha(nav2 / "topics.parquet"))
    (nav2 / "manifest.json").write_bytes(json.dumps(manifest).encode("utf-8"))
    with NavInput.open(nav2, ProjectionOptions(symbol_morphology="surface")) as inp:
        exp = expected_counts(inp)
        assert "columns not recognised" in exp["skipped"]["NavTopic"]
        assert "IN_TOPIC" in exp["skipped"] and exp["rels"]["RELATED_TOPIC"] == 0


def test_manifest_is_checked(nav):
    nav_dir, _ids = nav
    manifest = json.loads((nav_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest["datasets"]["terms"]["sha256"] = "0" * 64
    (nav_dir / "manifest.json").write_bytes(json.dumps(manifest).encode("utf-8"))
    with pytest.raises(ProjectionError) as exc:
        NavInput.open(nav_dir)
    assert exc.value.code == "E_CANON_MANIFEST_MISMATCH"
    NavInput.open(nav_dir, ProjectionOptions(verify_files=False)).close()   # --skip-file-hash


# ------------------------------------------------------------------------------------------------ Cypher
def test_cypher_templates_are_tagged_parameterised_and_namespaced():
    ns = S.Namespace.for_test("0000abcd")
    q = L.cy_merge_rels(ns, N.REL_BY_NAME["CO_OCCURS"])
    assert q.startswith("// vkm-nav:merge-rels CO_OCCURS\n") and "$rows" in q
    assert "`VKMTEST0000ABCD_CO_OCCURS` {edge_id: row.key}" in q and "`VkmTest0000abcdTerm`" in q
    q = L.cy_merge_rels(S.Namespace(), N.REL_BY_NAME["COVERS_PAGE"])
    assert "MATCH (b:`Page` {id: row.to_id})" in q and "MERGE (a)-[r:`COVERS_PAGE`]->(b)" in q
    assert "SET n = row.props, n:`NavigationLayer`" in L.cy_merge_nodes(S.Namespace(), N.NODE_BY_LABEL["Term"])
    assert "NOT n:`NavMeta`" in L.cy_sweep_nodes(S.Namespace())
    paths = Q.cy_paths(S.Namespace(), Q.path_rel_types(["concepts", "formulas"]), 4)
    assert "allShortestPaths((a)-[:`CO_OCCURS`|`CONTAINS_TERM`|`SAME_TERM_AS`|`SYMBOL_OF`|" in paths
    assert "*..4]-(b))" in paths and "x:`NavTopic`" in paths and "$cap" in paths and "ORDER BY" not in paths
    ordered = Q.cy_paths(S.Namespace(), ["CO_OCCURS"], 2, ordered=True)
    assert "ORDER BY strength DESC" in ordered and "WHEN 'CO_OCCURS' THEN r.npmi * r.n_units" in ordered
    assert "shortestPath((a)-[:`CO_OCCURS`*..3]-(b))" in Q.cy_shortest_length(S.Namespace(), ["CO_OCCURS"], 3)
    with pytest.raises(ValueError):
        Q.cy_paths(S.Namespace(), ["CO_OCCURS"], 9)
    assert "MATCH (n:`DocumentLayer` {id: $id})" in Q.cy_neighbourhood(S.Namespace(), "DOCUMENT")
    assert "MATCH (n:`NavigationLayer` {id: $id})" in Q.cy_neighbourhood(S.Namespace(), "NAVIGATION")


# ------------------------------------------------------------------------------------------------ loader + checks
def test_load_passes_all_checks_and_leaves_document_untouched(nav):
    nav_dir, ids = nav
    fake = SY.FakeNavNeo4j()
    doc_counts = SY.add_document_graph(fake, nav_dir)
    doc_before = {k: dict(v) for k, v in fake.rels.items()}
    receipt = L.load(None, _options(nav_dir), driver=fake)
    assert receipt["status"] == "COMPLETE", receipt.get("checks")
    assert {c["check_id"]: c["status"] for c in receipt["checks"]} == {f"N{i}": "PASS" for i in range(1, 8)}
    assert receipt["document"]["counts_unchanged"] and receipt["sweep"]["nodes_deleted"] == 0
    for t in ("HAS_PAGE", "HAS_FORMULA", "HAS_BLOCK"):
        assert fake.rels[t] == doc_before[t]
    assert L.document_counts(fake, "neo4j", S.Namespace()) == doc_counts
    assert set(fake.schema_names) >= {i.name for i in N.ddl_items()}
    meta = fake.nodes[N.META_ID]["props"]
    assert meta["status"] == "COMPLETE" and meta["snapshot_id"] == SY.SNAPSHOT
    assert json.loads(meta["checks_json"]) == {f"N{i}": "PASS" for i in range(1, 8)}
    assert json.loads(meta["manifest_json"])["snapshot"]["snapshot_id"] == SY.SNAPSHOT
    assert L.nav_state(fake)["state"] == "READY"
    # nothing is written on DOCUMENT nodes (R3)
    page = fake.nodes[SY.pid(SY.S1, 1)]
    assert page["labels"] == {"Page", "DocumentLayer"} and set(page["props"]) == {"id", "source_id", "page_index"}
    # idempotent: the same load again changes nothing; graph-verify agrees without writing
    counts = L.graph_counts(fake, "neo4j", S.Namespace())
    again = L.load(None, _options(nav_dir), driver=fake)
    assert again["status"] == "COMPLETE" and L.graph_counts(fake, "neo4j", S.Namespace()) == counts
    assert again["sweep"]["rels_deleted"] == 0 and again["sweep"]["nodes_deleted"] == 0
    verified = L.verify(None, _options(nav_dir), driver=fake)
    assert verified["status"] == "PASS" and verified["nav_meta"]["run_id"] == again["run_id"]


def test_reload_with_other_rules_removes_rows_the_rules_no_longer_keep(nav):
    nav_dir, _ids = nav
    fake, first = _loaded(nav_dir)
    before = first["expected_counts"]["rels"]["MENTIONED_IN"]
    options = _options(nav_dir)
    options.projection.mentions_top_per_term = options.projection.mentions_top_per_section = 1
    second = L.load(None, options, driver=fake)
    after = second["expected_counts"]["rels"]["MENTIONED_IN"]
    assert second["status"] == "COMPLETE" and after < before
    assert second["sweep"]["rels_deleted"] == before - after
    assert L.graph_counts(fake, "neo4j", S.Namespace())["rels"]["MENTIONED_IN"] == after


def test_new_snapshot_replaces_the_old_one(nav, tmp_path):
    nav_dir, ids = nav
    fake, first = _loaded(nav_dir)
    assert first["status"] == "COMPLETE"
    stale = "SEC-00000000000000aa"
    fake.nodes[stale] = {"labels": {"NavSection", N.LAYER_LABEL},
                         "props": {"id": stale, "layer": "NAV", "snapshot_id": "snap-old", "rule_version": "x"}}
    fake.add_rel("MENTIONED_IN", ids["terms"]["оседание"], stale, layer="NAV", snapshot_id="snap-old", rule_version="x")
    nav2 = tmp_path / "nav-new"
    SY.write_synthetic_nav(nav2, snapshot_id="snap-20260929T000000Z-0000abce")
    fake.doc_run["snapshot_id"] = "snap-20260929T000000Z-0000abce"
    second = L.load(None, _options(nav2), driver=fake)
    assert second["status"] == "COMPLETE", second.get("checks")
    assert stale not in fake.nodes and second["sweep"]["nodes_deleted"] == 1 and second["sweep"]["rels_deleted"] >= 1
    meta = fake.nodes[N.META_ID]["props"]
    assert meta["snapshot_id"] == "snap-20260929T000000Z-0000abce" and meta["previous_snapshot_id"] == SY.SNAPSHOT


def test_dangling_document_reference_fails_n1(nav):
    nav_dir, ids = nav
    missing_page = SY.pid(SY.S1, 6)
    fake = SY.FakeNavNeo4j()
    SY.add_document_graph(fake, nav_dir, drop=(missing_page,))
    with pytest.raises(ProjectionError) as exc:
        L.load(None, _options(nav_dir), driver=fake)
    assert exc.value.code == "E_DANGLING_REFERENCE"
    assert fake.nodes[N.META_ID]["props"]["status"] == "FAILED"
    assert L.nav_state(fake)["state"] == "FAILED" and L.nav_state(fake)["http_status"] == 503


def test_checks_detect_document_changes_cycles_orphans_counts_and_collisions(nav):
    nav_dir, ids = nav
    fake, receipt = _loaded(nav_dir)
    assert receipt["status"] == "COMPLETE"
    exp = receipt["expected_counts"]

    def statuses(**kw):
        results, _ = L.run_checks(fake, "neo4j", S.Namespace(), expected=exp, snapshot_id=SY.SNAPSHOT, **kw)
        return {r.check_id: r.status for r in results}

    doc = L.document_counts(fake, "neo4j", S.Namespace())
    fake.add_node("Page", "VKM-SRC-101:p0099", source_id=SY.S1)             # the DOCUMENT layer changed meanwhile
    assert statuses(doc_before=doc)["N2"] == "FAIL"
    del fake.nodes["VKM-SRC-101:p0099"]
    s11, s12 = ids["sections"]["s11"], ids["sections"]["s12"]
    fake.add_rel("NAV_CHILD_OF", ids["sections"]["ch1"], s11, layer="NAV", snapshot_id=SY.SNAPSHOT, rule_version="x")
    assert statuses()["N3"] == "FAIL"
    del fake.rels["NAV_CHILD_OF"][(ids["sections"]["ch1"], s11, None)]
    covers = {k: v for k, v in fake.rels["COVERS_PAGE"].items() if k[0] == s12}
    for k in covers:
        del fake.rels["COVERS_PAGE"][k]
    st = statuses()
    assert st["N4"] == "FAIL" and st["N5"] == "FAIL" and st["N1"] == "FAIL"
    fake.rels["COVERS_PAGE"].update(covers)
    fake.nodes[ids["terms"]["оседание"]]["labels"].add("Page")               # a NAV node with a DOCUMENT label
    assert statuses()["N6"] == "FAIL"
    fake.nodes[ids["terms"]["оседание"]]["labels"].discard("Page")
    fake.nodes[ids["terms"]["оседание"]]["props"]["snapshot_id"] = "snap-other"
    assert statuses()["N7"] == "FAIL"
    fake.nodes[ids["terms"]["оседание"]]["props"]["snapshot_id"] = SY.SNAPSHOT
    assert set(statuses().values()) == {"PASS"}


def test_gate_refuses_without_a_ready_document_graph_or_on_snapshot_mismatch(nav):
    nav_dir, _ids = nav
    fake = SY.FakeNavNeo4j()
    SY.add_document_graph(fake, nav_dir)
    fake.doc_run["status"] = "LOADING"
    with pytest.raises(ProjectionError) as exc:
        L.load(None, _options(nav_dir), driver=fake)
    assert exc.value.code == "E_REFUSED" and N.META_ID not in fake.nodes
    fake.doc_run.update(status="COMPLETE", snapshot_id="snap-20200101T000000Z-00000000")
    with pytest.raises(ProjectionError):
        L.load(None, _options(nav_dir), driver=fake)
    ok = L.load(None, _options(nav_dir, allow_snapshot_mismatch=True), driver=fake)
    assert ok["status"] == "COMPLETE" and ok["document"]["snapshot_mismatch"] is True


def test_drop_removes_every_nav_edge_including_those_between_document_nodes(nav):
    nav_dir, _ids = nav
    fake, _receipt = _loaded(nav_dir)
    doc = L.document_counts(fake, "neo4j", S.Namespace())
    out = L.drop_layer(fake, "neo4j")
    assert out["nodes_deleted"] > 0 and not fake.rels.get("NAV_REFERS_TO") and not fake.rels.get("NAV_BLOCK_REFERS_TO")
    assert not [n for n in fake.nodes.values() if N.LAYER_LABEL in n["labels"]]
    assert L.document_counts(fake, "neo4j", S.Namespace()) == doc and L.nav_state(fake)["state"] == "EMPTY"


def test_dry_run_needs_no_database_and_writes_a_receipt(nav, tmp_path, capsys):
    nav_dir, _ids = nav
    from vkm_corpus.cli import main

    out = tmp_path / "receipt.json"
    code = main(["nav", "graph-load", "--nav-dir", str(nav_dir), "--dry-run", "--symbol-morphology", "surface",
                 "--out", str(out)])
    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "DRY_RUN" and printed["totals"]["nodes"] == 20
    full = json.loads(out.read_text(encoding="utf-8"))
    assert full["preflight_summary"]["FAIL"] == 0 and full["accounting"]["terms"]["closed"]
    assert main(["nav", "graph-ddl", "--print"]) == 0
    assert "CREATE CONSTRAINT nav_layer_id_unique" in capsys.readouterr().out


# ------------------------------------------------------------------------------------------------ queries
def test_concept_paths_and_neighbourhood_over_the_fake(nav):
    nav_dir, ids = nav
    fake, _receipt = _loaded(nav_dir)
    t = ids["terms"]
    rel_types = Q.path_rel_types(None)
    assert L.read(fake, "neo4j", Q.cy_shortest_length(S.Namespace(), rel_types, 4), a=t["ползучесть соль"],
                  b=t["оседание"], rel_types=rel_types, labels=list(N.PATH_LABELS)) == [{"n": 3}]
    raw = L.read(fake, "neo4j", Q.cy_paths(S.Namespace(), rel_types, 4), a=t["ползучесть соль"], b=t["оседание"],
                 cap=50, rel_types=rel_types, labels=list(N.PATH_LABELS))
    paths = Q.shape_paths(raw, 5)
    best = paths[0]
    assert best["length"] == 3
    assert [n["name"] for n in best["nodes"]] == ["ползучесть соли", "скорость ползучести", "конвергенция", "оседание"]
    assert [h["rel"] for h in best["hops"]] == ["CO_OCCURS"] * 3 and all(h["pages"] for h in best["hops"])
    assert best["hops"][2]["pages"] == [SY.pid(SY.S1, 4), SY.pid(SY.S1, 5)]
    # through formulas only: term → symbol → formula ← … (a symbol links books only through a term)
    fam = Q.path_rel_types(["formulas"])
    raw = L.read(fake, "neo4j", Q.cy_paths(S.Namespace(), fam, 4), a=t["напряжение"], b=t["скорость ползучесть"],
                 cap=50, rel_types=fam, labels=list(N.PATH_LABELS))
    via = Q.shape_paths(raw, 3)[0]
    assert [n["kind"] for n in via["nodes"]] == ["TERM", "SYMBOL", "FORMULA", "SYMBOL", "TERM"]
    assert via["nodes"][2]["name"] == "(1.2)" and via["nodes"][2]["pages"] == [SY.pid(SY.S1, 3)]
    assert all(h["pages"] for h in via["hops"])
    # neighbourhood of a formula (DOCUMENT id) and of a section (NAV id), depth 2
    rows = L.read(fake, "neo4j", Q.cy_neighbourhood(S.Namespace(), "DOCUMENT"), id=SY.F2, per_type=10)
    shaped = Q.shape_neighbourhood(rows[0]["node"], rows[0]["groups"], {}, 50)
    rels = {(e["rel"], e["direction"]) for e in shaped["edges"]}
    assert {("IN_SECTION", "out"), ("DEFINED_FOR", "in"), ("NAV_REFERS_TO", "in"), ("NAV_BLOCK_REFERS_TO", "in"),
            ("NEAR_FORMULA", "in"), ("HAS_FORMULA", "in")} <= rels
    assert shaped["node"]["kind"] == "FORMULA" and shaped["node"]["name"] == "(1.2)"
    first = [e for e in shaped["edges"] if e["layer"] == "DOCUMENT"]
    assert first and all(e["rel"] == "HAS_FORMULA" for e in first)
    sec = ids["sections"]["s12"]
    rows = L.read(fake, "neo4j", Q.cy_neighbourhood(S.Namespace(), "NAVIGATION"), id=sec, per_type=10)
    shaped = Q.shape_neighbourhood(rows[0]["node"], rows[0]["groups"], {}, 4)
    assert sum(len(e["neighbours"]) for e in shaped["edges"]) == 4          # the budget is shared fairly
    mids = Q.depth2_ids(shaped)
    second = L.read(fake, "neo4j", Q.cy_neighbourhood_2(S.Namespace(), "NAVIGATION"),
                    ids=[m for m, layer in mids if layer == "NAVIGATION"], root=sec, per_type=3)
    shaped2 = Q.shape_neighbourhood(rows[0]["node"], rows[0]["groups"], {r["mid"]: r["groups"] for r in second}, 4)
    assert shaped2["depth2"] and all(d["via"] != sec for d in shaped2["depth2"])
    assert all(n["id"] != sec for d in shaped2["depth2"] for n in d["neighbours"])


def test_term_resolution_ranking():
    rows = [{"id": "TRM-b", "lemma": "b", "lemma_key": "x y", "df_units": 1},
            {"id": "TRM-a", "lemma": "a", "lemma_key": "x", "df_units": 50},
            {"id": "TRM-c", "lemma": "c", "lemma_key": "x y", "df_units": 9}]
    ranked = Q.rank_terms(rows, "x y", ["x y", "x"])
    assert [r["term_id"] for r in ranked] == ["TRM-c", "TRM-b", "TRM-a"] and ranked[0]["match"] == "lemma_key"
    assert Q.rank_terms(rows, "TRM-a", [])[0]["term_id"] == "TRM-a"
    keys, method = Q.term_keys("Скорость  ползучести")
    assert keys and ("скорость ползучести" in keys or "скорость ползучесть" in keys) and method
    assert Q.layer_of("VKM-SRC-001:p0001") == "DOCUMENT" and Q.layer_of("SEC-0000000000000001") == "NAVIGATION"
