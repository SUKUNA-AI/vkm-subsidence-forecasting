"""Synthetic regressions for explicit screening and candidate provenance limits."""
from pathlib import Path
import importlib.util

import pytest


spec = importlib.util.spec_from_file_location("abc_coverage", Path(__file__).parents[2] / "benchmarks/abc_completion_v1/coverage.py")
C = importlib.util.module_from_spec(spec)
spec.loader.exec_module(C)


def row(**changes):
    return {"source_id": "TEST-SOURCE", "human_full_read": False, "exhaustive_figure_recall_proven": False,
            "source_sha_status": "MATCH", "status": "SCREENED_AUTOMATICALLY", "canonical_pages": 3,
            "pages_checked": 3, "canonical_rollup": "COMPLETE", "textless_pages": 0, **changes}


def test_every_source_exactly_once_and_missing_detected():
    C.validate_coverage([row()], {"TEST-SOURCE"})
    with pytest.raises(ValueError, match="exactly one"):
        C.validate_coverage([row(), row()], {"TEST-SOURCE"})
    with pytest.raises(ValueError, match="exactly one"):
        C.validate_coverage([row()], {"TEST-SOURCE", "MISSING-SOURCE"})


def test_automatic_reading_or_recall_cannot_be_promoted():
    for field in ("human_full_read", "exhaustive_figure_recall_proven"):
        with pytest.raises(ValueError, match="automatic screening"):
            C.validate_coverage([row(**{field: True})], {"TEST-SOURCE"})


def test_unverified_bytes_and_omitted_pages_rejected():
    with pytest.raises(ValueError, match="unverified source"):
        C.validate_coverage([row(source_sha_status="MISMATCH")], {"TEST-SOURCE"})
    with pytest.raises(ValueError, match="all existing"):
        C.validate_coverage([row(pages_checked=2)], {"TEST-SOURCE"})


def test_specific_graphic_rule_and_unrelated_parameter_distinction():
    assert "MINE_PLAN" in C.relevance("Рис. 1. План шахтного поля")
    assert "GEOLOGICAL_SECTION" in C.relevance("Figure 2. Geological section")
    assert "BOREHOLE" in C.relevance("Рис. 3. Колонка скважины")
    assert not C.relevance("Fig. 4. Laboratory creep fit")


def test_native_caption_and_ranges_preserve_locator():
    assert C.captions("Текст\nРис. 3. Геологический разрез\nДальше") == ["Рис. 3. Геологический разрез"]
    assert C.compact_ranges([5, 1, 3, 2, 5, 8, None]) == [[1, 3], [5, 5], [8, 8]]


def test_output_and_source_cannot_escape(tmp_path):
    C.assert_private_output(tmp_path / "work/coverage", tmp_path)
    with pytest.raises(ValueError, match="ignored work"):
        C.assert_private_output(tmp_path / "data/coverage", tmp_path)
    with pytest.raises(ValueError, match="escapes"):
        C.safe_source(tmp_path, "../escape.pdf")


def test_partial_and_empty_sources_remain_gaps():
    assert C.source_status(row(canonical_rollup="PARTIAL")) == "SCREENED_WITH_GAPS"
    assert C.source_status(row(textless_pages=1)) == "SCREENED_WITH_GAPS"
    assert C.source_status(row(canonical_pages=0)) == "UNRESOLVED_CANONICAL_PAGES"
    assert C.source_status(row(canonical_rollup="SKIPPED_BY_REGISTER")) == "EXCLUDED_BY_REGISTER"
    assert C.source_status(row(native_errors=[{"error": "TEST_FAILURE"}])) == "SCREENED_WITH_GAPS"


def test_different_ids_same_source_region_are_deduplicated():
    candidate = {"figure_id": "NEW-ID", "source_id": "TEST-SOURCE", "page_id": "TEST-SOURCE:p0001", "bbox_page": [10, 20, 50, 60]}
    inventory = [{"key": "OLD-KEY", "source_id": "TEST-SOURCE", "source_object_id": "OLD-ID",
                  "locator": {"page_id": "TEST-SOURCE:p0001", "bbox_pt_tl": [0, 0, 100, 100]}}]
    assert C.inventory_matches(candidate, inventory) == [{"inventory_key": "OLD-KEY", "basis": ["SOURCE_PAGE_REGION_ALREADY_COVERED"]}]
    candidate["source_id"] = "OTHER-SOURCE"
    assert C.inventory_matches(candidate, inventory) == []


def test_exact_image_and_object_id_dedup_without_page():
    candidate = {"figure_id": "F-ID", "source_id": "TEST-SOURCE", "page_id": None, "artifacts": [{"sha256": "SYNTHETIC-SHA"}]}
    old = {"key": "OLD", "source_id": "TEST-SOURCE", "locator": {"object_ids": ["F-ID"]}, "image_sha256": "SYNTHETIC-SHA"}
    assert C.inventory_matches(candidate, [old])[0]["basis"] == ["EXACT_IMAGE_BYTES", "EXACT_OBJECT_ID"]
