"""Index bodies, analyzers, names and aliases of the OpenSearch projection (offline; no server)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from vkm_corpus.search import analysis as A
from vkm_corpus.search import mappings as M
from vkm_world.governance.leakage import FORBIDDEN_COLUMNS, _json_keys


def _walk(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key, value
            yield from _walk(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk(value)


@pytest.mark.parametrize("index_type", M.INDEX_TYPES)
def test_bodies_are_strict_and_references_resolve(index_type):
    body = M.index_body(index_type, {"build_id": "b", "built_from_snapshot_id": "snap-x"})
    mappings = body["mappings"]
    assert mappings["dynamic"] == "strict"
    assert mappings["_meta"]["vkm_mapping_version"] == M.MAPPING_VERSION
    assert mappings["_meta"]["built_from_snapshot_id"] == "snap-x"
    analysis = body["settings"]["analysis"]
    analyzers, normalizers = set(analysis["analyzer"]), set(analysis["normalizer"])
    for key, value in _walk(mappings["properties"]):
        if key in ("analyzer", "search_analyzer"):
            assert value in analyzers, value
        if key == "normalizer":
            assert value in normalizers, value
    for name, spec in analysis["analyzer"].items():
        for f in spec.get("filter", []):
            assert f in analysis["filter"] or f == "lowercase", (name, f)
        for c in spec.get("char_filter", []):
            assert c in analysis["char_filter"], (name, c)
    assert body["settings"]["index"]["refresh_interval"] == "-1"


def test_common_fields_identical_in_every_index():
    for index_type in M.INDEX_TYPES:
        props = M.properties(index_type)
        for name, spec in M.COMMON.items():
            if name == "text" and index_type == "pages":
                assert props["text"]["analyzer"] == "vkm_text" and props["text"]["index_options"] == "offsets"
            else:
                assert props[name] == spec, (index_type, name)


def test_required_fields_of_the_task_are_indexed():
    common = set(M.COMMON)
    assert {"id", "source_id", "work_id", "page_id", "page_index", "page_label", "object_type", "authors", "year",
            "language", "text", "is_primary_layer", "review_status", "quality_flags", "origin",
            "source_site_scope", "source_site_scope_raw", "source_site_scope_mapping", "available_latest_day",
            "available_basis", "foreign_content_work_ids", "work_copy_count", "preview_artifact_id"} <= common
    assert "caption" in M.PER_TYPE["figures"] and "caption" in M.PER_TYPE["tables"]
    assert "recognized_latex" in M.PER_TYPE["formulas"]
    assert all("bbox" in M.PER_TYPE[t] for t in ("blocks", "figures", "tables", "formulas"))
    assert "site_scope" not in common and "available_from" not in common      # H-18, H-19 names only


def test_no_forbidden_keys_anywhere():
    bodies = {t: M.index_body(t, {"build_id": "b"}) for t in M.INDEX_TYPES}
    keys = _json_keys(bodies)
    assert not keys & set(FORBIDDEN_COLUMNS)
    assert not {"quote", "verbatim_quote", "ocr_text", "page_text", "full_text"} & keys


def test_protected_forms_cover_the_paradigm_and_stay_distinct():
    forms = A.protected_forms()
    for form in ("сильвинит", "сильвинита", "сильвините", "сильвинитами"):
        assert forms[form] == "сильвинит"
    assert forms["сильвине"] == "сильвин" and forms["карналлите"] == "карналлит"
    assert all(lemma == lemma.lower() and "ё" not in lemma for lemma in A.PROTECTED_LEMMAS)
    rules = A.analysis_settings()["filter"]["vkm_protected_forms"]["rules"]
    assert "сильвините => сильвинит" in rules and len(rules) == len(set(rules))
    chain = A.analysis_settings()["analyzer"]["vkm_text"]["filter"]
    assert chain.index("vkm_protected_terms") < chain.index("vkm_protected_forms") < chain.index("vkm_ru_stem") \
        < chain.index("vkm_en_stem")


def test_names_aliases_and_build_ids():
    build_id = M.make_build_id("3FA9C2D1" + "0" * 56, datetime(2026, 9, 28, 10, 15, tzinfo=timezone.utc))
    assert build_id == "20260928t101500z-3fa9c2d1"
    name = M.index_name("vkm", "pages", build_id)
    assert name == "vkm-pages-m1-20260928t101500z-3fa9c2d1"
    assert M.parse_index_name("vkm", name) == ("pages", build_id)
    assert M.parse_index_name("vkm", "vkmtest01-pages-m1-x") is None      # test prefixes never match production
    assert M.alias_name("vkm", "figures") == "vkm-figures" and M.group_alias("vkm") == "vkm-objects"
    assert set(M.GROUP_TYPES) == {"figures", "tables", "formulas"}


def test_body_hash_is_build_independent():
    assert M.body_sha256("pages") == M.body_sha256("pages")
    assert json.dumps(M.index_body("pages", {"build_id": "a"})["settings"]) == \
        json.dumps(M.index_body("pages", {"build_id": "b"})["settings"])
