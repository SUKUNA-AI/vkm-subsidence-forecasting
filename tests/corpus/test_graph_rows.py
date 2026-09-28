"""Graph rows from the projection input: preflight, edge rules (H-16, H-17, H-25, H-49), digests (offline)."""
from __future__ import annotations

import copy
import random

import pytest

pytest.importorskip("duckdb")

from vkm_corpus.graph import schema as S  # noqa: E402
from vkm_corpus.graph.canon import ProjectionInput  # noqa: E402
from vkm_corpus.graph.common import ProjectionError, StreamDigest  # noqa: E402
from vkm_corpus.graph.preflight import require_preflight, run_preflight  # noqa: E402
from vkm_corpus.graph.rows import expected_counts, iter_nodes, iter_rels, not_projected_report  # noqa: E402
from vkm_corpus.graph.synthetic import W1, W3, W4, synthetic_input, synthetic_projection_rows  # noqa: E402
from vkm_corpus.graph.verify import expected_digest, node_line  # noqa: E402


@pytest.fixture(scope="module")
def inp():
    projection = synthetic_input()
    yield projection
    projection.close()


def rels(inp, rel_type):
    return list(iter_rels(inp, S.REL_BY_TYPE[rel_type], "RUN-X"))


def test_preflight_passes(inp):
    results = {r.check_id: r for r in run_preflight(inp)}
    assert not [r for r in results.values() if r.status == "FAIL"]
    assert results["P08"].status == "PASS"                              # only foreign-content links give no edge
    assert results["P10"].status == "PASS"
    assert results["P10"].details["work_copy_count_source"] == "E (D column absent)"


def test_expected_counts(inp):
    counts = expected_counts(inp)
    assert counts["nodes"] == {"Work": 4, "Author": 3, "Venue": 1, "Source": 7, "Page": 11, "Block": 12, "Figure": 2,
                               "Table": 1, "Formula": 2, "BibliographyEntry": 6}
    r = counts["rels"]
    assert (r["HAS_PAGE"], r["PRECEDES"], r["HAS_BLOCK"], r["INSTANCE_OF"]) == (11, 6, 12, 7)
    assert (r["REFERENCE_OF"], r["RESOLVES_TO"], r["CITES"], r["CARRIES_FOREIGN_CONTENT_OF"]) == (5, 5, 2, 1)
    assert (r["ABSTRACT_OF"], r["NOT_SAME"], r["EDITION_OF"], r["SHARES_PAGES_WITH"]) == (1, 1, 0, 1)


def test_instance_of_from_every_primary_non_foreign_link_cp25(inp):
    edges = rels(inp, "INSTANCE_OF")
    assert sorted(e.from_id for e in edges) == [f"VKM-SRC-{n}" for n in range(101, 108)]   # incl. absent/retired
    assert all(e.props["link_type"] != "FOREIGN_CONTENT" for e in edges)
    assert sorted(e.from_id for e in edges if e.to_id == W1) == ["VKM-SRC-101", "VKM-SRC-102", "VKM-SRC-106",
                                                                  "VKM-SRC-107"]
    report = not_projected_report(inp)["source_work_links"]
    assert set(report) == {"FOREIGN_CONTENT_PAGE_EDGES", "FOREIGN_CONTENT_UNIDENTIFIED"}
    assert report["FOREIGN_CONTENT_PAGE_EDGES"]["n"] == 1 and report["FOREIGN_CONTENT_UNIDENTIFIED"]["n"] == 1
    sources = {r.id: r.props for r in iter_nodes(inp, S.NODE_BY_LABEL["Source"], None)}
    assert sources["VKM-SRC-106"]["lifecycle_status"] == "ABSENT_BY_REGISTER"        # the node keeps its status
    works = {r.id: r.props for r in iter_nodes(inp, S.NODE_BY_LABEL["Work"], None)}
    assert works[W1]["work_copy_count"] == 2                                          # ACTIVE copies only (CP-25)


def test_work_copy_count_prefers_d_n_sources_active():
    rows = synthetic_projection_rows()
    rows["e_work_copies"] = [{"work_id": W1, "n_sources_total": 4, "n_sources_active": 3}]
    projection = ProjectionInput.from_rows(rows)
    try:
        works = {r.id: r.props for r in iter_nodes(projection, S.NODE_BY_LABEL["Work"], None)}
        assert works[W1]["work_copy_count"] == 3 and works[W3]["work_copy_count"] == 1   # D's value, else E's count
        p10 = {r.check_id: r for r in run_preflight(projection)}["P10"]
        assert p10.status == "WARN" and p10.examples == [W1]                             # 3 (D) != 2 ACTIVE (E)
        assert p10.details["work_copy_count_source"] == "D.work_copy_counts"
    finally:
        projection.close()


def test_foreign_pages_link_to_the_foreign_work_not_the_host(inp):
    edges = rels(inp, "CARRIES_FOREIGN_CONTENT_OF")
    assert [(e.from_id, e.to_id) for e in edges] == [("VKM-SRC-103:p0002", W1)]
    assert edges[0].props["rule_version"] == "citing_work_v1" and edges[0].props["canonical_row_id"].startswith("SWL-")


def test_shares_pages_with_keeps_page_ranges(inp):
    [edge] = rels(inp, "SHARES_PAGES_WITH")
    assert (edge.from_id, edge.to_id) == ("VKM-SRC-103", "VKM-SRC-101")
    assert (edge.props["from_page_start"], edge.props["to_page_start"]) == (2, 1)


def test_citations_come_from_d_rule_with_copy_counts(inp):
    cites = {(e.from_id, e.to_id): e.props for e in rels(inp, "CITES")}
    assert set(cites) == {(W1, W3), (W4, W3)}                       # self-citation of W3 excluded by D's rule
    assert cites[(W1, W3)]["n_citing_entries"] == 2 and cites[(W1, W3)]["n_citing_sources"] == 2
    assert cites[(W4, W3)]["flags"] == ["CITING_WORK_IS_CONTAINER"]
    assert all(p["rule_version"] == "cites_v1" for p in cites.values())
    refs = rels(inp, "REFERENCE_OF")
    assert all(e.props["canonical_row_id"] == e.from_id for e in refs)
    assert "VKM-SRC-103:p0003:c000000000001" not in {e.from_id for e in refs}     # foreign page: unknown citing work


def test_every_edge_has_canonical_row_or_rule(inp):
    for rel in S.REL_TYPES:
        for edge in rels(inp, rel.type):
            assert edge.props.get("canonical_row_id") or edge.props.get("rule_version"), rel.type
            assert set(edge.props) <= set(rel.all_properties), rel.type


def test_nodes_whitelisted_and_registry_entities_not_applicable(inp):
    for node in S.NODE_TYPES:
        rows = list(iter_nodes(inp, node, "RUN-X"))
        assert [r.id for r in rows] == sorted(r.id for r in rows)
        for row in rows:
            assert set(row.props) <= set(node.all_properties)
            assert all(row.props.get(p) is not None for p in node.all_required), (node.label, row.id)
            if node.label in ("Work", "Author", "Venue"):
                assert row.props["review_status"] == "NOT_APPLICABLE"
    work = {r.id: r.props for r in iter_nodes(inp, S.NODE_BY_LABEL["Work"], None)}
    assert work[W1]["work_copy_count"] == 2 and work[W4]["is_container"] is True


def test_duplicate_pages_are_candidates_across_sources(inp):
    [edge] = rels(inp, "DUPLICATE_CANDIDATE_OF")
    assert (edge.from_id, edge.to_id) == ("VKM-SRC-101:p0001", "VKM-SRC-102:p0001")
    pages = {r.id: r.props for r in iter_nodes(inp, S.NODE_BY_LABEL["Page"], None)}
    assert pages["VKM-SRC-101:p0001"]["dup_group_id"] == pages["VKM-SRC-102:p0001"]["dup_group_id"] \
        == edge.props["dup_group_id"]
    assert pages["VKM-SRC-101:p0002"]["dup_group_id"] == "VKM-SRC-101:p0002"


def test_expected_digest_is_stable_and_ignores_run_id():
    rows = synthetic_projection_rows()
    shuffled = copy.deepcopy(rows)
    for values in shuffled.values():
        random.Random(7).shuffle(values)
    a, b = ProjectionInput.from_rows(rows), ProjectionInput.from_rows(shuffled)
    try:
        assert expected_digest(a).digest == expected_digest(b).digest
        node = S.NODE_BY_LABEL["Page"]
        first = next(iter_nodes(a, node, "RUN-A"))
        assert node_line("Page", first.id, first.props) == node_line("Page", first.id, {**first.props,
                                                                                          "projection_run_id": "B"})
    finally:
        a.close()
        b.close()


def test_digest_changes_with_any_property():
    rows = synthetic_projection_rows()
    changed = copy.deepcopy(rows)
    changed["e_pages"][0]["page_status"] = "NEEDS_REVIEW"
    a, b = ProjectionInput.from_rows(rows), ProjectionInput.from_rows(changed)
    try:
        da, db = expected_digest(a), expected_digest(b)
        assert da.digest != db.digest
        assert [p for p, q in zip(da.parts, db.parts) if p != q][0][0] == "Page"
    finally:
        a.close()
        b.close()


def test_stream_digest_refuses_unordered_rows():
    d = StreamDigest("x")
    d.add(("b",), "b")
    with pytest.raises(ValueError):
        d.add(("a",), "a")


@pytest.mark.parametrize("mutate,code", [
    (lambda r: r["e_blocks"].append({**r["e_blocks"][0], "block_id": r["e_blocks"][0]["block_id"]}), "E_DUPLICATE_ID"),
    (lambda r: r["e_blocks"].__setitem__(0, {**r["e_blocks"][0], "page_id": "VKM-SRC-101:p0099"}),
     "E_DANGLING_REFERENCE"),
    (lambda r: r["e_pages"].__setitem__(1, {**r["e_pages"][1], "page_index": 7}), "E_PAGE_GAP"),
    (lambda r: r["e_blocks"].__setitem__(0, {**r["e_blocks"][0], "review_status": "FACT"}), "E_FORBIDDEN_STATUS"),
    (lambda r: r["e_work_relations"].__setitem__(0, {**r["e_work_relations"][0], "relation": "RELATED_TO"}),
     "E_UNKNOWN_RELATION_TYPE"),
    (lambda r: r["e_work_relations"].append({**r["e_work_relations"][1], "relation_id": "WRL-self",
                                             "to_work_id": r["e_work_relations"][1]["from_work_id"]}),
     "E_RELATION_CONFLICT"),
])
def test_preflight_defects_fail_with_their_code(mutate, code):
    rows = synthetic_projection_rows()
    mutate(rows)
    projection = ProjectionInput.from_rows(rows)
    try:
        with pytest.raises(ProjectionError) as info:
            require_preflight(projection)
        failed = {c["code"] for c in info.value.details["checks"] if c["status"] == "FAIL"}
        assert code in failed
    finally:
        projection.close()
