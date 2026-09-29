"""Closed vocabularies of the contract (CP-16 p. 2, H-01): exact values, completeness of side tables, and the AST rule
"StrEnum classes are declared only in vkm_corpus.contracts"."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from vkm_corpus.contracts import vocab as v

ROOT = Path(__file__).resolve().parents[2]
SCANNED = ("src/vkm_corpus", "src/vkm_cad", "src/vkm_drawio")
ALLOWED_DIR = ROOT / "src" / "vkm_corpus" / "contracts"


def _enum_bases(node: ast.ClassDef) -> set[str]:
    names = set()
    for base in node.bases:
        if isinstance(base, ast.Name):
            names.add(base.id)
        elif isinstance(base, ast.Attribute):
            names.add(base.attr)
    return names


def test_strenums_are_declared_only_in_contracts():
    offenders = []
    for area in SCANNED:
        base = ROOT / area
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            if ALLOWED_DIR in path.parents:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    bases = _enum_bases(node)
                    if "StrEnum" in bases or {"str", "Enum"} <= bases:
                        offenders.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno} {node.name}")
    assert offenders == [], "declare vocabularies in vkm_corpus.contracts.vocab: " + "; ".join(offenders)


def _values(enum) -> list[str]:
    return [m.value for m in enum]


def test_cp16_vocabularies_are_exact():
    assert _values(v.ProcessingStatus) == ["NATIVE_OK", "EMBEDDED_TEXT_OK", "OCR_REQUIRED", "OCR_OK", "PARTIAL",
                                           "UNSUPPORTED", "FAILED", "NEEDS_REVIEW", "SKIPPED_BY_REGISTER",
                                           "NOT_PROCESSED"]
    assert _values(v.ReviewStatus) == ["UNSEEN", "QUICK_LOOK_ONLY", "AUTO_EXTRACTED_UNREVIEWED",
                                       "RELEVANT_SECTIONS_REVIEWED", "FULLY_REVIEWED", "NOT_APPLICABLE"]
    assert _values(v.Origin) == ["NATIVE", "EMBEDDED_OCR", "OCR", "DERIVED", "REGISTRY", "CURATED"]
    assert _values(v.TextLayer) == ["PDF_TEXT_LAYER", "PDF_EMBEDDED_OCR_LAYER", "DJVU_EMBEDDED_OCR_LAYER",
                                    "EPUB_XHTML", "DOCX_XML", "GLM_OCR", "NONE"]
    assert _values(v.RegionOrigin) == ["PDF_TEXT_BLOCK", "PDF_XOBJECT", "VECTOR_CLUSTER", "NATIVE_TABLE_FINDER",
                                       "LAYOUT_MODEL", "EPUB_ELEMENT", "DOCX_ELEMENT", "DJVU_TEXT_ZONE",
                                       "OCR_MODEL"]
    assert _values(v.ModelRole) == ["LAYOUT", "RECOGNITION"]
    assert _values(v.BboxSpace) == ["PAGE_PT_TL", "IMAGE_PIXEL", "DRAWING_UNITS", "GEO", "NONE"]
    assert _values(v.MatchStatus) == ["CANDIDATE", "AUTO_EXACT_ID_MATCH", "AUTO_STRONG_MATCH", "REJECTED"]
    assert _values(v.LifecycleStatus) == ["ACTIVE", "ABSENT_BY_REGISTER", "RETIRED"]
    assert _values(v.SkipReason) == ["ARCHIVE_DELETED_AFTER_ASSEMBLY", "RETIRED_NOT_EVIDENCE"]
    assert _values(v.Materialization) == ["STORED", "NOT_STORED_REPRODUCIBLE"]
    assert _values(v.SiteScopeMapping) == ["EXACT", "CASE", "SYNONYM", "LOSSY", "MULTI", "AMBIGUOUS", "NOT_A_SCOPE"]
    assert {"CLASSIFY", "LAYOUT", "OCR", "COMMIT"} <= set(_values(v.Stage))
    assert {"LAYOUT_RAW", "OCR_RAW", "RUN_LOG", "VECTOR_PATHS_JSON", "VECTOR_SVG", "CAD_DWG", "CAD_DXF",
            "CAD_GEOMETRY_JSONL", "CAD_LOG"} <= set(_values(v.ArtifactKind))
    assert "REPAIRED_COPY" not in _values(v.ArtifactKind)          # H-37
    assert _values(v.IdentityStatus)[0] == "NAME_KEY_ONLY"          # H-15


def test_forbidden_statuses_are_not_representable():
    status_enums = [v.ReviewStatus, v.ProcessingStatus, v.SourceProcessingStatus, v.CurationStatus, v.MatchStatus,
                    v.RunStatus, v.OcrStatus, v.FileTextStatus]
    for enum in status_enums:
        assert not (set(_values(enum)) & v.FORBIDDEN_STATUS_VALUES), enum.__name__
    assert {"FACT", "REVIEWED_MEASUREMENT", "ACCEPTED_PARAMETER", "ACCEPTED_FORMULA"} <= v.FORBIDDEN_STATUS_VALUES


def test_c_error_codes_are_absorbed():
    c_codes = {"E_FILE_ABSENT", "E_LFS_POINTER", "E_SHA_MISMATCH", "E_OPEN_FAILED", "E_ENCRYPTED_NEEDS_PASSWORD",
               "E_PERMISSION_POLICY", "E_UNSUPPORTED_FORMAT", "E_RETIRED_ARCHIVE", "E_PAGE_COUNT_MISMATCH",
               "E_DECODER_MISSING", "E_RENDER_FAILED", "E_TIMEOUT", "E_MEMORY_LIMIT", "E_LAYOUT_FAILED", "E_OCR_HTTP",
               "E_OCR_TIMEOUT", "E_OCR_TRUNCATED", "E_OCR_EMPTY_ON_INK", "E_NORMALIZE_FAILED",
               "E_NATIVE_OCR_CONFLICT"}
    assert set(v.C_ERROR_CODE_MAP) == c_codes
    assert v.C_ERROR_CODE_MAP["E_RETIRED_ARCHIVE"] is None           # CP-05: SKIPPED_BY_REGISTER, not an error
    for code in v.C_ERROR_CODE_MAP.values():
        assert code is None or code in set(v.ErrorCode)


@pytest.mark.parametrize("table,enum", [(v.QUALITY_FLAG_SCOPE, v.QualityFlag), (v.ARTIFACT_KIND_DIR, v.ArtifactKind),
                                        (v.ARTIFACT_KIND_RETENTION, v.ArtifactKind),
                                        (v.PAGE_KIND_UNIT, v.PageKind)])
def test_side_tables_cover_every_member(table, enum):
    assert set(table) == set(enum)


def test_side_tables_are_consistent():
    assert set(v.OBJECT_KIND_CODE.values()) == set("bftmc")
    assert v.ARTIFACT_KIND_RETENTION[v.ArtifactKind.LAYOUT_RAW] == v.RetentionClass.KEEP_RAW   # H-03
    assert v.ARTIFACT_KIND_RETENTION[v.ArtifactKind.RUN_LOG] == v.RetentionClass.KEEP_RAW      # H-11
    assert set(v.TEXT_LAYER_ORIGIN) == set(v.TextLayer) - {v.TextLayer.NONE}
    assert v.TEXT_LAYER_ORIGIN[v.TextLayer.DJVU_EMBEDDED_OCR_LAYER] == v.Origin.EMBEDDED_OCR  # H-02
    assert v.CRS_REQUIRED_SPACES == {"GEO", "DRAWING_UNITS"}                                   # H-20
    assert v.DatePrecision.YEAR.value == "year"
    from vkm_world.core.provenance import DATE_PRECISIONS
    assert tuple(_values(v.DatePrecision)) == DATE_PRECISIONS
    assert v.INSTANCE_LINK_TYPES.isdisjoint({v.SourceWorkLinkType.FOREIGN_CONTENT})          # H-16


def test_every_vocabulary_is_exported_and_upper_case_or_documented():
    lower_ok = {"PageUnit", "DatePrecision", "TextRule", "DerivedRule"}
    for name, enum in v.all_vocabularies().items():
        assert len(enum) > 0
        if name not in lower_ok:
            assert all(m.value == m.value.upper() for m in enum), name


PROJECTION_AREAS = ("src/vkm_corpus/graph", "src/vkm_corpus/search")
_E_LITERAL = __import__("re").compile(r"""["'](E_[A-Z][A-Z0-9_]*)["']""")


def test_projection_error_literals_are_in_error_code():
    """Every "E_*" literal used by the graph/search projections (agent E) is a member of ErrorCode (H-01)."""
    found: dict[str, str] = {}
    for area in PROJECTION_AREAS:
        base = ROOT / area
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            for m in _E_LITERAL.finditer(path.read_text(encoding="utf-8")):
                found.setdefault(m.group(1), path.relative_to(ROOT).as_posix())
    known = {c.value for c in v.ErrorCode}
    missing = {code: where for code, where in found.items() if code not in known}
    assert missing == {}, f"add these codes to vkm_corpus.contracts.vocab.ErrorCode: {missing}"
    assert len(v.PROJECTION_ERROR_CODES) == 46 and "E_BAD_FILTER" in v.PROJECTION_ERROR_CODES
