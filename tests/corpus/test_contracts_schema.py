"""Contracts: models → Arrow description, JSON Schema export (deterministic, = committed files), forbidden keys,
row-level scientific safety rules. Pure tests (pydantic only); pyarrow checks run when pyarrow is installed."""
from __future__ import annotations

import ast
import json
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from vkm_corpus.contracts import arrow as ca
from vkm_corpus.contracts import export
from vkm_corpus.contracts.builders import build_row, content_sha256
from vkm_corpus.contracts.datasets import DATASETS, VOLATILE_COLUMNS
from vkm_corpus.testing import rows as R
from vkm_world.governance.leakage import FORBIDDEN_COLUMNS, _json_keys, scan

ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------- schema description and export
def test_every_dataset_has_a_complete_arrow_description():
    for name, spec in DATASETS.items():
        desc = ca.schema_description(name)
        assert [d[0] for d in desc] == list(spec.model.model_fields), name
        for key in spec.primary_key + spec.sort_key:
            assert key in spec.model.model_fields, (name, key)
        assert len(ca.schema_fingerprint(name)) == 64


def test_nullable_iff_optional():
    from typing import get_args

    for spec in DATASETS.values():
        for f in ca.field_specs(spec.model):
            info = spec.model.model_fields[f.name]
            admits_none = type(None) in get_args(info.annotation)
            assert f.nullable == admits_none, (spec.name, f.name)


def test_bare_int_and_naive_datetime_are_refused():
    from pydantic import BaseModel

    class Bad(BaseModel):
        n: int

    with pytest.raises(ca.ContractTypeError):
        ca.field_specs(Bad)

    class BadTs(BaseModel):
        t: datetime

    with pytest.raises(ca.ContractTypeError):
        ca.field_specs(BadTs)


def test_no_bare_int_annotation_in_contract_sources():
    src = (ROOT / "src" / "vkm_corpus" / "contracts" / "models.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.annotation, ast.Name):
            assert node.annotation.id != "int", ast.unparse(node)


def test_export_is_deterministic_and_equals_committed_files():
    first = export.render_all()
    assert first == export.render_all()
    problems = export.check()
    if problems and not export.pydantic_is_pinned():
        pytest.skip(f"pydantic is not the pinned pair {export.PINNED_PYDANTIC}: {problems[:3]}")
    assert problems == [], "run: python -m vkm_corpus.contracts.export"


def test_exported_schemas_have_no_forbidden_keys_or_classes(tmp_path):
    files = export.render_all()
    for fname, text in files.items():
        keys = _json_keys(json.loads(text))
        assert not (keys & FORBIDDEN_COLUMNS), (fname, keys & FORBIDDEN_COLUMNS)
        (tmp_path / fname).write_text(text, encoding="utf-8")
    assert scan(tmp_path, files=sorted(tmp_path.iterdir())) == []
    classes = {c.lower() for spec in DATASETS.values() for c in _model_classes(spec.model)}
    assert not (classes & FORBIDDEN_COLUMNS)
    arrow_names = {f.name for spec in DATASETS.values() for f in ca.field_specs(spec.model)}
    assert not (arrow_names & FORBIDDEN_COLUMNS)
    reserved_l2 = {"epistemic_status", "evidence_type", "scope", "scale", "reviewer", "reviewed_at", "event_time",
                   "measurement_time", "world_id", "representation_id", "solver_run_id", "seed"}
    assert not (arrow_names & reserved_l2)


def _model_classes(model) -> set[str]:
    out = {model.__name__}
    for f in model.model_fields.values():
        for t in getattr(f.annotation, "__args__", ()) + (f.annotation,):
            if isinstance(t, type):
                out.add(t.__name__)
    return out


def test_leakage_guard_catches_a_forbidden_field(tmp_path):
    from pydantic import BaseModel

    class Probe(BaseModel):
        page_text: str

    (tmp_path / "p.schema.json").write_text(json.dumps(Probe.model_json_schema()), encoding="utf-8")
    assert scan(tmp_path, files=[tmp_path / "p.schema.json"])


@pytest.mark.parametrize("name", sorted(n for n, s in DATASETS.items() if s.stored))
def test_pyarrow_schema_matches_pure_description(name):
    pytest.importorskip("pyarrow")
    schema = ca.arrow_schema(name)
    assert [[f.name, str(f.type), f.nullable] for f in schema] == ca.schema_description(name)
    assert schema.metadata[b"vkm.schema_fingerprint"].decode() == ca.schema_fingerprint(name)


# ---------------------------------------------------------------- factories are valid; content hashing
@pytest.mark.parametrize("factory", [R.make_page, R.make_block, R.make_figure, R.make_table, R.make_formula,
                                     R.make_bibliography_entry, R.make_document, R.make_source, R.make_work])
def test_factories_produce_valid_rows(factory):
    row = factory()
    assert row.schema_version == "0.1.0"


def test_content_hash_ignores_envelope_and_tracks_content():
    d = R.block_dict()
    h = content_sha256("blocks", d)
    assert content_sha256("blocks", {**d, "created_at": R.T0.replace(hour=11)}) == h
    assert content_sha256("blocks", {**d, "processing_run_id": "RUN-20260101T000000Z-ffffffff"}) == h
    assert content_sha256("blocks", {**d, "normalized_text": "другое"}) != h
    assert VOLATILE_COLUMNS == {"created_at", "processing_run_id"}


# ---------------------------------------------------------------- negative cases (scientific safety at row level)
def _bad(dataset_name: str, d: dict, match: str):
    with pytest.raises(ValidationError, match=match):
        build_row(dataset_name, d)


def test_forbidden_review_statuses_are_rejected():
    for status in ("FACT", "REVIEWED_MEASUREMENT", "ACCEPTED_PARAMETER", "ACCEPTED_FORMULA", "FULLY_REVIEWED"):
        _bad("blocks", {**R.block_dict(), "review_status": status}, "review_status|AUTO_EXTRACTED")


def test_ocr_without_model_is_rejected():
    d = R.formula_dict()
    _bad("formulas", {**d, "model_id": None, "model_revision": None}, "RECOGNITION")


def test_layout_region_without_layout_model_is_rejected():
    d = R.figure_dict()
    _bad("figures", {**d, "models": []}, "LAYOUT")


def test_embedded_ocr_layer_cannot_be_native():
    d = R.block_dict(text_layer="PDF_EMBEDDED_OCR_LAYER")
    _bad("blocks", d, "implies origin")
    page = R.page_dict(page_class="RASTER_SCAN")
    _bad("pages", page, "raster scan")


def test_page_status_must_match_text_origin():
    _bad("pages", R.page_dict(page_status="EMBEDDED_TEXT_OK"), "primary_text_origin")
    _bad("pages", R.page_dict(page_status="SKIPPED_BY_REGISTER"), "SKIPPED_BY_REGISTER")


def test_page_text_hash_is_checked():
    _bad("pages", {**R.page_dict(), "text_sha256": R.sha("something else")}, "text_sha256")


def test_naive_timestamp_is_rejected():
    _bad("blocks", {**R.block_dict(), "created_at": datetime(2026, 9, 28, 10, 0)}, "timezone-aware")


def test_unknown_quality_flag_and_duplicates_are_rejected():
    _bad("blocks", {**R.block_dict(), "quality_flags": ["NOT_A_FLAG"]}, "quality_flags")
    _bad("blocks", {**R.block_dict(), "quality_flags": ["BBOX_APPROX", "BBOX_APPROX"]}, "duplicate")


def test_figure_type_below_threshold_is_rejected():
    d = R.figure_dict(detected_figure_type="MINE_PLAN", figure_type_method="MODEL_CLASSIFIER",
                      figure_type_confidence=0.42, figure_type_threshold=0.8)
    _bad("figures", d, "UNKNOWN_FIGURE_TYPE")
    ok = R.figure_dict(detected_figure_type="MINE_PLAN", figure_type_method="MODEL_CLASSIFIER",
                       figure_type_confidence=0.93, figure_type_threshold=0.8)
    assert build_row("figures", ok).detected_figure_type == "MINE_PLAN"


def test_bbox_rules():
    _bad("blocks", {**R.block_dict(), "bbox_x1": 10.0}, "degenerate")
    _bad("blocks", {**R.block_dict(), "bbox_space": "GEO"}, "PAGE_PT_TL or NONE")
    _bad("blocks", {**R.block_dict(), "bbox_space": "NONE"}, "empty bbox")


def test_object_id_must_belong_to_page_and_kind():
    d = R.block_dict()
    _bad("blocks", {**d, "page_id": "VKM-SRC-001:p0002"}, "start with")
    _bad("blocks", {**d, "object_kind": "FIGURE"}, "object_kind")


def test_source_lifecycle_and_review_rules():
    ok = R.make_source(sid="VKM-SRC-013", lifecycle_status="ABSENT_BY_REGISTER",
                       register_skip_status="SKIPPED_BY_REGISTER", register_skip_reason="ARCHIVE_DELETED_AFTER_ASSEMBLY",
                       review_status="NOT_APPLICABLE", review_status_basis="LIFECYCLE", file_status="MISSING",
                       observed_sha256=None, observed_size_bytes=None, format_detected="UNKNOWN")
    assert ok.lifecycle_status == "ABSENT_BY_REGISTER"
    _bad("sources", R.source_dict(lifecycle_status="RETIRED"), "SKIPPED_BY_REGISTER")
    _bad("sources", R.source_dict(review_status="AUTO_EXTRACTED_UNREVIEWED"), "automatic")
    _bad("sources", R.source_dict(site_scope_mapping="AMBIGUOUS"), "site_scope")


def test_work_rules():
    _bad("works", R.work_dict(review_status="FULLY_REVIEWED"), "NOT_APPLICABLE")
    _bad("works", R.work_dict(available_from_basis="ASSUMED_FROM_PUBLICATION"), "views")
    _bad("works", R.work_dict(doi="https://doi.org/10.9999/X"), "normalised")
    _bad("works", R.work_dict(status="MERGED_INTO"), "merged_into")


def test_artifact_crs_rules():
    base = {"schema_version": "0.1.0", "artifact_id": R.artifact("x"), "artifact_kind": "CAD_DXF",
            "media_type": "image/vnd.dxf", "size_bytes": 10, "storage_relpath": "cad/aa/bb/x.dxf",
            "retention_class": "KEEP_REFERENCED", "created_by_run_id": R.RUN_ID, "created_at": R.T0}
    with pytest.raises(ValidationError, match="crs_status"):
        build_row("artifacts", {**base, "coordinate_space": "DRAWING_UNITS"})
    ok = build_row("artifacts", {**base, "coordinate_space": "DRAWING_UNITS", "crs_status": "UNKNOWN_CRS"})
    assert ok.crs_status == "UNKNOWN_CRS"
    with pytest.raises(ValidationError, match="crs_status"):
        build_row("artifacts", {**base, "coordinate_space": "PAGE_PT_TL", "crs_status": "UNKNOWN_CRS"})
    with pytest.raises(ValidationError, match="reproducible"):
        build_row("artifacts", {**base, "artifact_kind": "OCR_RAW", "materialization": "NOT_STORED_REPRODUCIBLE",
                                "storage_relpath": None, "size_bytes": None})
