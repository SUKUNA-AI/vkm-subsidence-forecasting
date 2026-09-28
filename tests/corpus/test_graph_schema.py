"""DOCUMENT graph registry, DDL, namespaces and Cypher templates (offline; no server)."""
from __future__ import annotations

import re

import pytest

from vkm_corpus.contracts import vocab
from vkm_corpus.graph import cypher as C
from vkm_corpus.graph import schema as S

DOCUMENT_LABELS = {"Work", "Source", "Page", "Block", "Figure", "Table", "Formula", "BibliographyEntry", "Author",
                   "Venue"}
MANDATORY_TYPES = {"INSTANCE_OF", "AUTHORED_BY", "PUBLISHED_IN", "HAS_PAGE", "PRECEDES", "HAS_BLOCK", "HAS_FIGURE",
                   "HAS_TABLE", "HAS_FORMULA", "CITES"}
ENTERPRISE_ONLY = ("IS NOT NULL", "IS NODE KEY", "IS RELATIONSHIP KEY", "IS ::", "GRAPH TYPE", "IS TYPED")


def test_registry_is_consistent():
    assert S.validate_registry() == []
    assert {n.label for n in S.NODE_TYPES} == DOCUMENT_LABELS
    assert MANDATORY_TYPES <= set(S.REL_BY_TYPE)


def test_relation_vocabularies_follow_agent_d():
    assert S.WORK_RELATION_TYPES == {v.value for v in vocab.WorkRelationType}
    assert S.SOURCE_RELATION_TYPES == {v.value for v in vocab.SourceRelationType}
    for rel in S.REL_TYPES:
        assert rel.symmetric == (rel.type in vocab.SYMMETRIC_WORK_RELATIONS or rel.type == "DUPLICATE_CANDIDATE_OF")


def test_no_generic_relationship_types():
    for rel in S.REL_TYPES:
        assert not rel.type.startswith("RELATED") and rel.type not in S.FORBIDDEN_GENERIC_TYPES


def test_every_edge_carries_row_or_rule_provenance():
    for rel in S.REL_TYPES:
        props = rel.all_properties
        assert "canonical_row_id" in props or "rule_version" in props, rel.type
        if rel.provenance in (S.RULE, S.ROW_RULE):
            assert S.RULE_OF_REL[rel.type] in {r.value for r in vocab.DerivedRule}


def test_graph_holds_no_texts():
    text_like = {"text", "normalized_text", "caption", "caption_normalized", "latex", "normalized_latex", "raw_output",
                 "title", "name_display", "parsed_title", "register_notes"}
    for node in S.NODE_TYPES:
        assert not text_like & set(node.all_properties), node.label


def test_registry_entities_are_not_applicable_and_objects_auto():
    by = S.NODE_BY_LABEL
    for label in ("Work", "Author", "Venue"):
        assert by[label].review_statuses == {"NOT_APPLICABLE"}
    for label in ("Page",) + S.PAGE_OBJECT_LABELS:
        assert by[label].review_statuses == {"AUTO_EXTRACTED_UNREVIEWED"}


def test_layer_dependencies_form_a_dag_h31():
    deps = S.LAYER_DEPENDENCIES
    assert set(deps) == set(S.LAYER_LABELS)
    assert deps["DOCUMENT"] == frozenset()
    assert "REPRESENTATION" not in deps["OBSERVATION"]          # real observations never depend on a representation
    assert deps["OBSERVATION"] == {"DOCUMENT", "EVIDENCE"}
    order, seen = [], set()

    def visit(layer, stack=()):
        assert layer not in stack, f"cycle through {layer}"
        if layer in seen:
            return
        for dep in deps[layer]:
            visit(dep, stack + (layer,))
        seen.add(layer)
        order.append(layer)

    for layer in deps:
        visit(layer)
    assert order.index("DOCUMENT") == 0


def test_physical_entity_kinds_are_not_labels():
    # R2/H-31: 'Block' means a document text block; kinds of a future PhysicalEntity are the property entity_type
    assert "Block" in DOCUMENT_LABELS and "PhysicalEntity" in S.RESERVED_FUTURE_LABELS
    assert not {"MineBlock", "Panel", "Pillar"} & (DOCUMENT_LABELS | set(S.RESERVED_FUTURE_LABELS))


def test_ddl_is_idempotent_prefixed_and_community_only():
    script = S.ddl_script()
    items = S.ddl_items()
    assert all("IF NOT EXISTS" in i.statement for i in items)
    assert all(i.name.startswith(("doc_", "sys_")) for i in items)
    assert not any(bad in script for bad in ENTERPRISE_ONLY)
    constraints = [i for i in items if i.kind == "CONSTRAINT" and "FOR (n:" in i.statement]
    assert {re.search(r"FOR \(n:`(\w+)`\)", i.statement).group(1) for i in constraints} == \
        DOCUMENT_LABELS | {"DocumentLayer", "ProjectionRun"}
    assert script.endswith("\n") and "\r" not in script


def test_test_namespace_prefixes_everything():
    ns = S.Namespace.for_test("0123abcd")
    assert ns.label("Page") == "VkmTest0123abcdPage" and ns.layer_label == "VkmTest0123abcdDocumentLayer"
    assert ns.rel("HAS_PAGE") == "VKMTEST0123ABCD_HAS_PAGE"
    assert all(i.name.startswith("vkmtest0123abcd_") for i in S.ddl_items(ns))
    assert all("`VkmTest0123abcd" in i.statement or "VKMTEST0123ABCD_" in i.statement for i in S.ddl_items(ns))
    for bad in ("vkm", "Prod", "VkmTestXYZ", "VkmTest0123abcd; DROP"):
        with pytest.raises(ValueError):
            S.Namespace(bad)


def test_identifiers_are_validated_before_interpolation():
    with pytest.raises(ValueError):
        S.q("Page`) DETACH DELETE n //")
    assert S.q("HAS_PAGE") == "`HAS_PAGE`"


def test_templates_use_parameters_and_registry_names_only():
    ns = S.Namespace()
    for node in S.NODE_TYPES:
        text = C.node_merge(ns, node)
        assert "$rows" in text and f"`{node.label}`" in text and "`DocumentLayer`" in text
        assert "apoc." not in text and "IN TRANSACTIONS" not in text
    for rel in S.REL_TYPES:
        text = C.rel_merge(ns, rel)
        assert f"`{rel.type}`" in text and "row.from_id" in text and "row.to_id" in text
        assert "IN TRANSACTIONS" not in text and "apoc." not in text
    assert "IN TRANSACTIONS OF 10000 ROWS" in C.wipe_layer(ns, 10_000)
    with pytest.raises(ValueError):
        C.wipe_layer(ns, 10)


def test_merge_identity_of_multi_edges():
    # an author with two roles in one work → two AUTHORED_BY edges, told apart by canonical_row_id
    assert "canonical_row_id: row.props.canonical_row_id" in C.rel_merge(S.Namespace(), S.REL_BY_TYPE["AUTHORED_BY"])
    assert "{" not in C.rel_merge(S.Namespace(), S.REL_BY_TYPE["CITES"]).split("MERGE")[1].split("]")[0]
