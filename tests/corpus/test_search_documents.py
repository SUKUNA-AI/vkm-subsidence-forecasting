"""Search documents from the projection input (offline): one row = one document, strict fields, denormalisation by
the graph's rules (H-16, H-18, H-19, H-30, H-49), deterministic stream digest."""
from __future__ import annotations

import hashlib

import pytest

pytest.importorskip("duckdb")

from vkm_corpus.graph.synthetic import W1, W4, synthetic_input  # noqa: E402
from vkm_corpus.search.documents import (SCOPE_FLAG, doc_stream_digest, expected_doc_counts,  # noqa: E402
                                         iter_documents, object_number)
from vkm_corpus.search.mappings import INDEX_TYPES, properties  # noqa: E402


@pytest.fixture(scope="module")
def docs():
    inp = synthetic_input()
    try:
        yield {t: {d["id"]: d for d in iter_documents(inp, t)} for t in INDEX_TYPES}, expected_doc_counts(inp), inp
    finally:
        inp.close()


def test_one_row_one_document_including_failed_pages(docs):
    by_type, expected, _ = docs
    assert {t: len(v) for t, v in by_type.items()} == expected
    failed = by_type["pages"]["VKM-SRC-102:p0002"]
    assert failed["page_status"] == "FAILED" and "text" not in failed and failed["text_chars"] == 0


def test_documents_fit_the_strict_mapping(docs):
    by_type, _, _ = docs
    for index_type, items in by_type.items():
        allowed = set(properties(index_type))
        for doc in items.values():
            assert set(doc) <= allowed, (index_type, set(doc) - allowed)
            assert all(v is not None for v in doc.values())


def test_text_hash_is_of_the_indexed_text(docs):
    by_type, _, _ = docs
    for doc in list(by_type["pages"].values()) + list(by_type["blocks"].values()):
        if "text" in doc:
            assert doc["text_sha256"] == hashlib.sha256(doc["text"].encode("utf-8")).hexdigest()


def test_work_scope_availability_and_copies_are_denormalised(docs):
    by_type, _, _ = docs
    page = by_type["pages"]["VKM-SRC-101:p0001"]
    assert page["work_id"] == W1 and page["work_copy_count"] == 2
    assert page["authors"] == ["Иванов И. И.", "Петров П. П."]          # author with two roles listed once
    assert page["source_site_scope"] == ["SKRU1"] and page["source_site_scope_mapping"] == "EXACT"
    assert SCOPE_FLAG in page["projection_flags"]
    assert (page["available_latest_day"], page["available_basis"]) == ("2008-12-31", "ASSUMED_FROM_PUBLICATION")
    issue = by_type["tables"]["VKM-SRC-104:p0001:t000000000001"]
    assert issue["available_basis"] == "UNKNOWN" and "available_latest_day" not in issue and issue["work_id"] == W4
    ambiguous = by_type["pages"]["VKM-SRC-103:p0001"]
    assert ambiguous["source_site_scope"] == [] and ambiguous["source_site_scope_mapping"] == "AMBIGUOUS"


def test_foreign_pages_are_marked_not_attributed_to_the_host(docs):
    by_type, _, _ = docs
    identified = by_type["pages"]["VKM-SRC-103:p0002"]
    assert identified["foreign_content_work_ids"] == [W1] and "FOREIGN_CONTENT" in identified["projection_flags"]
    unidentified = by_type["pages"]["VKM-SRC-103:p0003"]
    assert unidentified["foreign_content_work_ids"] == []
    assert "FOREIGN_CONTENT_UNIDENTIFIED" in unidentified["projection_flags"]
    assert by_type["pages"]["VKM-SRC-101:p0001"]["foreign_content_work_ids"] == []


def test_duplicates_and_layers(docs):
    by_type, _, _ = docs
    a, b = by_type["pages"]["VKM-SRC-101:p0001"], by_type["pages"]["VKM-SRC-102:p0001"]
    assert a["dup_group_id"] == b["dup_group_id"] != a["id"]
    assert by_type["pages"]["VKM-SRC-101:p0002"]["dup_group_id"] == "VKM-SRC-101:p0002"
    layers = [d for d in by_type["blocks"].values() if d["page_id"] == "VKM-SRC-105:p0001"]
    assert sorted(d["is_primary_layer"] for d in layers) == [False, True]
    assert {d["origin"] for d in layers} == {"EMBEDDED_OCR", "OCR"}


def test_objects_carry_labels_bbox_and_previews(docs):
    by_type, _, _ = docs
    fig = by_type["figures"]["VKM-SRC-101:p0002:f000000000001"]
    assert (fig["object_label"], fig["object_label_raw"]) == ("3.1", "Рис. 3.1")
    assert fig["has_preview"] and fig["preview_artifact_id"].startswith("sha256:") and len(fig["vector_artifact_ids"]) == 2
    assert fig["bbox"]["space"] == "PAGE_PT_TL"
    formula = by_type["formulas"]["VKM-SRC-103:p0001:m000000000001"]
    assert formula["equation_label"] == "3.2" and formula["recognized_latex"].startswith("\\dot")


def test_stream_digest_is_deterministic(docs):
    _, _, inp = docs
    assert doc_stream_digest(inp, "blocks") == doc_stream_digest(inp, "blocks")


@pytest.mark.parametrize("label,number", [("Рис. 3.1", "3.1"), ("Рисунок 2", "2"), ("Таблица 1.2а", "1.2а"),
                                          ("(3.12)", "3.12"), ("Fig. 4", "4"), (None, None), ("без номера", None)])
def test_object_numbers(label, number):
    assert object_number(label) == number
