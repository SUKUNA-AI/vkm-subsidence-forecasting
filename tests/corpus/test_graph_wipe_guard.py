"""DOCUMENT destruction is refused before any write; no Neo4j service involved."""
from __future__ import annotations

import pytest

from vkm_corpus.config import load_settings
from vkm_corpus.graph import loader, schema as S
from vkm_corpus.graph.common import ProjectionError
from vkm_corpus.graph.synthetic import synthetic_input


@pytest.fixture(scope="module")
def inp():
    value = synthetic_input()
    try:
        yield value
    finally:
        value.close()


class GuardDriver:
    """Tiny labelled graph fixture; any write, DDL or auto-commit fails the test."""

    def __init__(self, ns=S.Namespace(), *, nodes=(), edges=(), bad_probe=None, unavailable=False):
        self.ns, self.nodes, self.edges = ns, list(nodes), list(edges)
        self.bad_probe, self.unavailable = bad_probe, unavailable
        self.reads, self.writes = [], []

    def execute_query(self, query, *, parameters_=None, routing_=None, **kw):
        text, p = str(query), parameters_ or {}
        if routing_ != "r":
            self.writes.append(text)
            pytest.fail("preflight rejection must precede all graph writes")
        self.reads.append(text)
        if text.startswith("CALL dbms.components()"):
            return [{"name": "Neo4j Kernel", "versions": ["synthetic"], "edition": "community"}], None, None
        if text.startswith("// vkm-document:scientific-nodes"):
            if self.unavailable:
                raise OSError("synthetic unavailable graph")
            if self.bad_probe is not None:
                return self.bad_probe, None, None
            found = False
            for node in self.nodes:
                labels = node.get("labels", [])
                scoped = (any(l.startswith(p["namespace"]) for l in labels) if self.ns.is_test else
                          not any(l.startswith(p["test_prefix"]) for l in labels))
                found |= (bool(set(labels) & set(p["protected_labels"])) or scoped and (
                    node.get("layer") in p["protected_layers"] or node.get("kind") in p["protected_kinds"]
                    or node.get("record_sha256") is not None and node.get("record_id") is not None))
            return [{"n": int(found)}], None, None
        if text.startswith("// vkm-document:foreign-relationships"):
            found = any((self.ns.layer_label in a.get("labels", []) or
                         self.ns.layer_label in b.get("labels", [])) and
                        (rel["type"] not in p["owned_types"] or rel.get("layer") in p["protected_layers"])
                        for a, rel, b in self.edges)
            return [{"n": int(found)}], None, None
        if "RETURN type(r) AS rel_type, labels(x) AS other_labels" in text:
            rows = []
            for a, rel, b in self.edges:
                for d, x in ((a, b), (b, a)):
                    if self.ns.layer_label in d.get("labels", []) and self.ns.layer_label not in x.get("labels", []):
                        rows.append({"rel_type": rel["type"], "other_labels": x.get("labels", []), "n": 1})
            return rows, None, None
        pytest.fail(f"unexpected query: {text[:100]}")

    def session(self, **kw):
        self.writes.append("autocommit")
        pytest.fail("preflight rejection must precede auto-commit")


@pytest.mark.parametrize("node", [
    {"labels": ["EvidenceLayer"]},
    {"labels": ["Claim"]},
    {"labels": ["Observation"]},
    {"labels": ["ReviewDecision"]},
    {"labels": ["DocumentLayer", "Claim"]},  # old outside-layer check misses dual ownership
    {"labels": ["EVIDENCE"]},
    {"labels": ["ImportedRecord"], "kind": "REVIEW_DECISION"},  # no support edge yet
    {"labels": ["ImportedRecord"], "layer": "EVIDENCE"},
    {"labels": ["ImportedRecord"], "record_id": "review-1", "record_sha256": "a" * 64},
])
@pytest.mark.parametrize("cascade", [False, True])
def test_rebuild_refuses_scientific_dependencies_before_ddl_runs_or_cascade(tmp_path, node, cascade, inp):
    driver = GuardDriver(nodes=[node])
    with pytest.raises(ProjectionError, match="qualified shadow-generation") as error:
        loader.rebuild(load_settings({}), loader.RebuildOptions(cascade=cascade),
                       driver=driver, inp=inp, data_root=tmp_path)
    assert error.value.code == "E_EVIDENCE_DEPENDENCY"
    assert driver.writes == []
    assert len(driver.reads) == 2  # server info, scientific dependency probe; no DDL or cascade


@pytest.mark.parametrize("entry", ["wipe", "purge_test_namespace"])
def test_direct_destructive_helpers_cannot_bypass_guard(entry):
    ns = S.Namespace.for_test("0123abcd")
    driver = GuardDriver(ns, nodes=[{"labels": [ns.label("ReviewDecision")]}])
    args = (driver, "neo4j", ns, 100) if entry == "wipe" else (driver, "neo4j", ns)
    with pytest.raises(ProjectionError) as error:
        getattr(loader, entry)(*args)
    assert error.value.code == "E_EVIDENCE_DEPENDENCY" and driver.writes == []


@pytest.mark.parametrize("edge", [
    {"type": "SUPPORTED_BY"}, {"type": "HAS_PAGE", "layer": "EVIDENCE"}, {"type": "REVIEWED_AGAINST"},
])
@pytest.mark.parametrize("cascade", [False, True])
def test_foreign_edges_between_document_nodes_are_not_deleted(edge, cascade):
    a, b = {"labels": ["DocumentLayer", "Source"]}, {"labels": ["DocumentLayer", "Page"]}
    driver = GuardDriver(nodes=[a, b], edges=[(a, edge, b)])
    with pytest.raises(ProjectionError) as error:
        loader.document_wipe_guard(driver, "neo4j", S.Namespace(), allow_navigation=cascade)
    assert error.value.code == "E_CROSS_LAYER_LOSS" and driver.writes == []


def test_only_registered_navigation_can_be_cascaded():
    doc = {"labels": ["DocumentLayer", "Page"]}
    nav = {"labels": ["NavigationLayer", "NavSection"]}
    driver = GuardDriver(nodes=[doc, nav], edges=[(doc, {"type": "COVERS_PAGE"}, nav)])
    with pytest.raises(ProjectionError):
        loader.document_wipe_guard(driver, "neo4j", S.Namespace())
    loader.document_wipe_guard(driver, "neo4j", S.Namespace(), allow_navigation=True)
    driver.edges[0][1]["type"] = "SUPPORTED_BY"  # an edge to NAV need not be owned by NAV
    with pytest.raises(ProjectionError):
        loader.document_wipe_guard(driver, "neo4j", S.Namespace(), allow_navigation=True)
    assert driver.writes == []


@pytest.mark.parametrize("bad_probe", [[], [{"n": None}], [{"n": True}], [{"n": -1}], [{"n": 2}],
                                      [{"n": 0, "unexpected": 1}], [{"n": 0}, {"n": 0}]])
def test_missing_or_malformed_preflight_is_not_an_absence_proof(bad_probe):
    driver = GuardDriver(bad_probe=bad_probe)
    with pytest.raises(ProjectionError) as error:
        loader.wipe(driver, "neo4j", S.Namespace(), 100)
    assert error.value.code == "E_WIPE_PREFLIGHT" and driver.writes == []


def test_unavailable_safety_probe_blocks_writes():
    driver = GuardDriver(unavailable=True)
    with pytest.raises(ProjectionError) as error:
        loader.wipe(driver, "neo4j", S.Namespace(), 100)
    assert error.value.code == "E_WIPE_PREFLIGHT" and driver.writes == []


def test_empty_and_document_only_graphs_can_pass_read_preflight():
    loader.document_wipe_guard(GuardDriver(), "neo4j", S.Namespace())
    a, b = {"labels": ["DocumentLayer", "Source"]}, {"labels": ["DocumentLayer", "Page"]}
    driver = GuardDriver(nodes=[a, b], edges=[(a, {"type": "HAS_PAGE"}, b)])
    loader.document_wipe_guard(driver, "neo4j", S.Namespace())
    assert driver.writes == []


def test_namespace_isolation_does_not_treat_another_namespace_as_a_dependency():
    ns = S.Namespace.for_test("0123abcd")
    nodes = [{"labels": ["EvidenceLayer"], "kind": "CLAIM"},
             {"labels": ["VkmTest9999abcdReviewDecision"], "kind": "REVIEW_DECISION"}]
    loader.document_wipe_guard(GuardDriver(ns, nodes=nodes), "neo4j", ns)
    loader.document_wipe_guard(GuardDriver(nodes=nodes[1:]), "neo4j", S.Namespace())
