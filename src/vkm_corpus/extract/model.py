"""Internal extraction results (independent of the canonical row schema).

The pipeline builds these objects from stage caches; ``vkm_corpus.extract.to_canon`` is the single place that maps them
onto the canonical datasets of the contract (pages, blocks, figures, tables, formulas, documents, processing steps,
errors, artifacts). Vocabulary values used here are the contract values (CP-16); new values are raised as decision
notes, never declared here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from vkm_corpus.contracts.models import TableContinuationCandidate
from vkm_corpus.contracts.fieldtypes import Sha256Hex

BBox = tuple[float, float, float, float]  # PAGE_PT_TL: points, origin top-left of the displayed page, y down


@dataclass
class SourceInput:
    source_id: str
    canonical_path: str
    sha256: str
    size_bytes: int | None
    lifecycle: str = "ACTIVE"           # ACTIVE | ABSENT_BY_REGISTER | RETIRED
    skip_reason: str | None = None      # ARCHIVE_DELETED_AFTER_ASSEMBLY | RETIRED_NOT_EVIDENCE
    register_notes: str = ""
    evidence_scope: str = ""             # register evidence_scope, verbatim (source scope of every object)

    @property
    def number(self) -> str:
        return self.source_id.rsplit("-", 1)[-1]


@dataclass
class Pagination:
    unit: str                    # p | r | s
    page_kind: str               # PDF_PAGE | DJVU_PAGE | DOCX_RENDERED_PAGE | EPUB_SPINE_ITEM
    basis: str                   # PDF_PAGE_TREE | DJVU_PAGE_ORDER | DOCX_PINNED_RENDER | EPUB_SPINE
    count: int
    method: str
    check_count: int | None = None
    check_method: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def agree(self) -> bool:
        return self.check_count is None or self.check_count == self.count


@dataclass
class ErrorRec:
    code: str
    stage: str
    message: str
    retryable: bool = False
    page_index: int | None = None
    tool: str = "vkm_corpus"
    tool_version: str | None = None
    severity: str = "ERROR"
    exception_type: str | None = None


@dataclass
class ModelRef:
    role: str               # LAYOUT | RECOGNITION
    model_id: str
    model_revision: str


@dataclass
class StepRec:
    stage: str
    page_index: int | None
    outcome: str            # EXECUTED | REUSED_CACHED | SKIPPED_UP_TO_DATE | SKIPPED_BY_POLICY | NOT_ATTEMPTED
    status: str             # processing status of the step
    stage_signature: str
    extractor_id: str
    extractor_version: str
    config_hash: str
    call_signature: str | None = None
    model_id: str | None = None
    model_revision: str | None = None
    input_artifact_ids: list[str] = field(default_factory=list)
    output_artifact_ids: list[str] = field(default_factory=list)
    n_objects_out: int = 0
    attempt: int = 1
    started_at: Any = None
    finished_at: Any = None
    reason_code: str | None = None


@dataclass
class ObjectBase:
    page_index: int
    bbox: BBox | None
    origin: str                          # NATIVE | EMBEDDED_OCR | OCR | DERIVED
    region_origin: str                   # PDF_TEXT_BLOCK | PDF_XOBJECT | VECTOR_CLUSTER | NATIVE_TABLE_FINDER |
    #                                      LAYOUT_MODEL | EPUB_ELEMENT | DOCX_ELEMENT | DJVU_TEXT_ZONE | OCR_MODEL
    extractor_id: str
    extractor_version: str
    generation: str                      # semantic extraction generation (H-14), part of producer_key
    raw_config_hash: str                 # config affecting the raw output (dpi, prompt, thresholds)
    models: list[ModelRef] = field(default_factory=list)
    raw_artifact_id: str | None = None
    raw_artifacts: list[tuple[str, str]] = field(default_factory=list)  # (role, artifact_id)
    raw_locator: str | None = None
    quality_flags: list[str] = field(default_factory=list)
    reading_order: int | None = None
    anchor_ordinal: int | None = None    # only for objects without geometry (EPUB, DOCX)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class BlockX(ObjectBase):
    text: str = ""
    block_type: str = "TEXT"
    text_layer: str = "PDF_TEXT_LAYER"
    is_primary_layer: bool = True
    native_order: int | None = None
    recognition_confidence: float | None = None
    language: str | None = None


@dataclass
class FigureX(ObjectBase):
    layout_class: str = "LAYOUT_REGION"      # RASTER_IMAGE | VECTOR_GRAPHICS | MIXED | LAYOUT_REGION
    layout_label: str | None = None
    layout_score: float | None = None
    figure_label: str | None = None
    caption: str | None = None
    caption_block_index: int | None = None   # index into SourceResult.blocks
    image_artifact_id: str | None = None
    image_dpi: int | None = None
    embedded_image_artifact_id: str | None = None
    embedded_image_transcoded: bool | None = None
    original_filter: str | None = None
    vector_artifacts: list[tuple[str, str]] = field(default_factory=list)  # (PATHS_JSON|SVG, artifact_id)


@dataclass
class TableX(ObjectBase):
    table_label: str | None = None
    caption: str | None = None
    caption_block_index: int | None = None
    raw_format: str = "HTML"
    raw_output: str = ""
    recognition_method: str = "OCR_GLM"      # NATIVE_FIND_TABLES | OCR_GLM | BOTH_AGREE | BOTH_DISAGREE | DOCX_XML | XHTML
    n_rows: int | None = None
    n_cols: int | None = None
    cells: list[dict[str, Any]] = field(default_factory=list)
    normalized_text: str | None = None
    structure_confidence: float | None = None
    image_artifact_id: str | None = None
    image_dpi: int | None = None
    layout_score: float | None = None
    continuation_candidate: TableContinuationCandidate | None = None
    extraction_signature: Sha256Hex | None = None


@dataclass
class FormulaX(ObjectBase):
    formula_kind: str = "UNKNOWN"            # DISPLAY | INLINE | UNKNOWN
    equation_label: str | None = None
    raw_format: str = "LATEX"                # LATEX | OMML | MATHML | TEXT | IMAGE_ONLY
    raw_output: str | None = None
    normalized_latex: str | None = None
    latex_parse_ok: bool | None = None
    native_glyph_text: str | None = None
    recognition_method: str = "OCR_GLM"      # OCR_GLM | NATIVE_OMML | EPUB_IMAGE_OCR | MATHML
    recognition_confidence: float | None = None
    image_artifact_id: str | None = None
    layout_score: float | None = None


@dataclass
class PageX:
    page_index: int
    page_kind: str
    width_pt: float | None = None
    height_pt: float | None = None
    rotation_deg: int = 0
    page_box: str | None = None              # CROPBOX | MEDIABOX | DJVU_IMAGE
    native_dpi: float | None = None
    bbox_space: str = "PAGE_PT_TL"
    page_class: str | None = None
    class_flags: list[str] = field(default_factory=list)
    route: str | None = None                 # NATIVE | NATIVE_REPAIR | EMBEDDED_TEXT | OCR_REQUIRED | EMPTY
    route_reason: str | None = None
    native_text_status: str = "NOT_CHECKED"
    native_char_count: int | None = None
    ocr_status: str = "NOT_REQUIRED"
    recognized_char_count: int | None = None
    page_status: str = "NOT_PROCESSED"
    primary_text_origin: str = "NONE"        # NATIVE | EMBEDDED_OCR | OCR | NONE
    primary_text_layer: str | None = None
    embedded_layer_evidence: str | None = None  # HIDDEN_TEXT_LAYER | VISIBLE_OVER_IMAGE | DJVU_TXT
    text_layer_producer: str | None = None
    printed_page_raw: str | None = None
    printed_page_labels: list[str] = field(default_factory=list)
    printed_label_origin: str = "NONE"
    printed_label_extractor: str | None = None
    is_spread: bool | None = None
    native_raw_artifact_id: str | None = None
    layout_raw_artifact_id: str | None = None
    ocr_raw_artifact_ids: list[str] = field(default_factory=list)
    render_artifact_id: str | None = None    # 1024 px preview (visual rerank, API)
    render_dpi: int | None = None
    layout_render_artifact_id: str | None = None
    spine_href: str | None = None
    plain_text_sha256: str | None = None     # sha256 of PyMuPDF page.get_text() (Phase-1 regression, K-11)
    native_ocr_cer: float | None = None
    quality_flags: list[str] = field(default_factory=list)
    models: list[ModelRef] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class DocumentX:
    file_format: str
    format_version: str | None
    container_detail: str | None
    pagination: Pagination | None
    document_class: str = "UNKNOWN"
    is_encrypted: bool = False
    text_extraction_permitted: bool | None = None
    has_native_page_labels: bool | None = None
    pagination_render_profile: str | None = None
    pagination_artifact_id: str | None = None
    file_meta: dict[str, str | None] = field(default_factory=dict)
    file_identifiers: list[str] = field(default_factory=list)
    file_languages: list[str] = field(default_factory=list)
    native_raw_artifact_id: str | None = None
    quality_flags: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class SourceResult:
    source: SourceInput
    status: str = "NOT_PROCESSED"            # source processing status (roll-up)
    reason_code: str | None = None
    document: DocumentX | None = None
    pages: list[PageX] = field(default_factory=list)
    blocks: list[BlockX] = field(default_factory=list)
    figures: list[FigureX] = field(default_factory=list)
    tables: list[TableX] = field(default_factory=list)
    formulas: list[FormulaX] = field(default_factory=list)
    steps: list[StepRec] = field(default_factory=list)
    errors: list[ErrorRec] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
