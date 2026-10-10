"""Pydantic row models of every canonical dataset (CP-16 p. 1): the single source of truth of the schema.

Arrow schemas, fingerprints and JSON Schema are generated from these classes (``arrow.py``, ``export.py``). Column order
= field order: the provenance envelope first, then dataset fields.

Envelope profiles:

* DOC — document objects (documents, pages, blocks, figures, tables, formulas, bibliography entries): processing
  provenance of task §13 plus ``origin``/``text_layer``/``region_origin``/``models[]``/``raw_artifacts[]`` (H-02, H-03,
  H-24) and the source scope ``source_site_scope*`` (H-18, copied from the source row and checked by the validator);
* REG — registry entities (sources, works, authors, venues);
* LINK — curated relations (source→work, work↔work, source↔source, work→author) and derived bibliography links;
* LOG — processing runs, steps, errors and the artifact index.

Rules enforced at row level (the canon validator repeats and extends them across rows):

* document objects are always ``AUTO_EXTRACTED_UNREVIEWED`` (task §14); FACT and the other forbidden statuses are
  not even representable (closed enums);
* ``origin = OCR`` ⇒ ``model_id``/``model_revision`` and a RECOGNITION model in ``models[]``;
  ``region_origin = LAYOUT_MODEL`` ⇒ a LAYOUT model in ``models[]`` (H-03);
* ``text_layer`` implies ``origin`` (an embedded foreign OCR layer is ``EMBEDDED_OCR``, never NATIVE — H-02);
* bbox in PAGE_PT_TL is complete and non-degenerate, or absent with ``bbox_space = NONE``;
* page text: ``text_sha256 = sha256(normalized_text)`` (rule ``page_text_v1``, H-04).
"""
from __future__ import annotations

import hashlib
import json
from typing import ClassVar

from pydantic import Field, field_validator, model_validator

from vkm_corpus.contracts.base import (
    ArtifactRecipe,
    ContractModel,
    ExternalId,
    ModelRef,
    RawArtifactRef,
    RunModelInfo,
    TableCell,
    VectorArtifactRef,
    WorkMetadataBasis,
)
from vkm_corpus.contracts.fieldtypes import (
    ArtifactId,
    AuthorId,
    CommitId,
    Date32,

    ErrorId,
    Float64,
    Int16,
    Int32,
    Int64,
    LanguageCode,
    LogicalRef,
    PageId,
    PageObjectId,
    RelPath,
    RunId,
    SemVer,
    Sha256Hex,
    SourceId,
    StepId,
    UtcDatetime,
    VenueId,
    WorkId,
)
from vkm_corpus.contracts.site_scope import SCOPE_VALUES
from vkm_corpus.contracts.vocab import (
    CRS_REQUIRED_SPACES,
    DOCUMENT_ORIGINS,
    OBJECT_KIND_CODE,
    PAGE_KIND_UNIT,
    TEXT_LAYER_ORIGIN,
    TEXT_ORIGINS,
    ArtifactKind,
    AuthorRole,
    AvailableBasis,
    BboxSpace,
    BlockType,
    CitingWorkResolution,
    CrsStatus,
    CurationStatus,
    DatePrecision,
    DerivedRule,
    DocumentClass,
    EmbeddedLayerEvidence,
    ErrorCode,
    FigureLayoutClass,
    FigureType,
    FigureTypeMethod,
    FileFormat,
    FileStatus,
    FileTextStatus,
    FormulaKind,
    FormulaRawFormat,
    HostRole,
    IdentityStatus,
    IngestionBasis,
    LifecycleStatus,
    LinkBasis,
    MatchMethod,
    MatchStatus,
    Materialization,
    ModelRole,
    ObjectKind,
    OcrStatus,
    Origin,
    PageBox,
    PageClass,
    PageKind,
    PageRoute,
    PageUnit,
    PaginationBasis,
    PrintedLabelOrigin,
    PrintedLabelStatus,
    ProcessingStatus,
    QualityFlag,
    RecognitionMethod,
    RecordPhase,
    RegionOrigin,
    RetentionClass,
    ReviewStatus,
    ReviewStatusBasis,
    RunKind,
    RunStatus,
    Script,
    Severity,
    SiteScopeMapping,
    SkipReason,
    SourceProcessingStatus,
    SourceRelationType,
    SourceWorkLinkType,
    Stage,
    StepOutcome,
    TableRawFormat,
    TextLayer,
    TextRule,
    VenueType,
    WorkIdentityStatus,
    WorkRelationType,
    WorkStatus,
    WorkType,
)
from vkm_corpus.ids import grammar


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _unique(values: list, what: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"duplicate {what}: {values}")


# ================================================================= base row
class RowModel(ContractModel):
    """Base of every dataset row."""

    OBJECT_KIND: ClassVar[str | None] = None

    schema_version: SemVer = Field(description="semver of the dataset schema at write time")


# ================================================================= DOC envelope
class DocEnvelope(RowModel):
    """Processing-provenance envelope of document objects (task §13 + CP-16)."""

    object_id: str = Field(description="stable ID (ids grammar); = page_id for pages, <source_id>:doc for documents")
    object_kind: ObjectKind
    source_id: SourceId
    page_id: PageId | None = Field(None, description="page of the object; for DOCX objects the pinned render page "
                                                     "(RENDER_DEPENDENT, H-50)")
    source_sha256: Sha256Hex = Field(description="sha256 of the processed file; must equal the register")
    source_site_scope: list[str] = Field(default_factory=list, description="scope of the whole SOURCE (vkm_world "
                                         "Scope values), not of this object (H-18)")
    source_site_scope_raw: str = Field(description="register evidence_scope, verbatim")
    source_site_scope_mapping: SiteScopeMapping
    origin: Origin = Field(description="NATIVE | EMBEDDED_OCR | OCR | DERIVED (what produced the content)")
    pipeline_version: SemVer
    processing_run_id: RunId
    extractor_id: str = Field(min_length=1)
    extractor_version: str = Field(min_length=1, description="software version; not part of the object ID (H-14)")
    extraction_generation: Int16 = Field(1, ge=1, description="semantic generation of the producer; part of the "
                                                               "object ID (H-14)")
    model_id: str | None = Field(None, description="model that produced the CONTENT (RECOGNITION); see models[]")
    model_revision: str | None = None
    models: list[ModelRef] = Field(default_factory=list, description="all models that produced region or content")
    config_hash: Sha256Hex = Field(description="sha256 of the canonical JSON of the stage configuration")
    raw_config_hash: Sha256Hex = Field(description="hash of the configuration part that affects the raw output; "
                                                   "part of producer_key")
    extraction_signature: Sha256Hex | None = Field(None, description="stage_signature of the producing step")
    raw_content_sha256: Sha256Hex | None = Field(None, description="hash of the raw content; invariant for a given "
                                                                   "object_id across snapshots (B07)")
    content_sha256: Sha256Hex = Field(description="hash of all non-envelope fields")
    raw_artifact_id: ArtifactId | None = Field(None, description="main raw output behind this row")
    raw_artifacts: list[RawArtifactRef] = Field(default_factory=list)
    created_at: UtcDatetime = Field(description="processing time (finished_at of the producing step); outside hashes")
    review_status: ReviewStatus = ReviewStatus.AUTO_EXTRACTED_UNREVIEWED
    quality_flags: list[QualityFlag] = Field(default_factory=list)

    @model_validator(mode="after")
    def _envelope_rules(self):
        if self.OBJECT_KIND is not None and self.object_kind != self.OBJECT_KIND:
            raise ValueError(f"object_kind must be {self.OBJECT_KIND}, got {self.object_kind}")
        if self.origin not in DOCUMENT_ORIGINS:
            raise ValueError(f"document objects have origin in {sorted(DOCUMENT_ORIGINS)}, got {self.origin}")
        if self.review_status != ReviewStatus.AUTO_EXTRACTED_UNREVIEWED:
            raise ValueError("automatic document objects are always AUTO_EXTRACTED_UNREVIEWED (task §14)")
        if (self.model_id is None) != (self.model_revision is None):
            raise ValueError("model_id and model_revision go together")
        roles = [m.role for m in self.models]
        _unique(roles, "model roles")
        recog = [m for m in self.models if m.role == ModelRole.RECOGNITION]
        if self.origin == Origin.OCR:
            if not (self.model_id and recog):
                raise ValueError("origin OCR requires model_id/model_revision and a RECOGNITION model in models[]")
        if self.model_id is not None:
            if not recog or (recog[0].model_id, recog[0].model_revision) != (self.model_id, self.model_revision):
                raise ValueError("model_id/model_revision must equal the RECOGNITION entry of models[]")
        bad_scope = set(self.source_site_scope) - SCOPE_VALUES
        if bad_scope:
            raise ValueError(f"source_site_scope values not in vkm_world Scope: {sorted(bad_scope)}")
        if self.source_site_scope_mapping in (SiteScopeMapping.AMBIGUOUS, SiteScopeMapping.NOT_A_SCOPE) \
                and self.source_site_scope:
            raise ValueError("AMBIGUOUS / NOT_A_SCOPE source scope must be []")
        _unique(self.quality_flags, "quality flags")
        _unique([(r.role, r.artifact_id) for r in self.raw_artifacts], "raw artifacts")
        if self.page_id is not None and not self.page_id.startswith(self.source_id + ":"):
            raise ValueError("page_id must belong to source_id")
        return self


class DocObjectEnvelope(DocEnvelope):
    """Envelope of objects located on a page: region, text layer and bbox."""

    region_origin: RegionOrigin = Field(description="what produced the region (bbox) of the object (H-03)")
    text_layer: TextLayer = Field(description="text layer of the content; implies origin (H-02)")
    bbox_x0: Float64 | None = None
    bbox_y0: Float64 | None = None
    bbox_x1: Float64 | None = None
    bbox_y1: Float64 | None = None
    bbox_space: BboxSpace = Field(description="PAGE_PT_TL (points, top-left, upright page) or NONE")
    docx_paragraph_path: str | None = Field(None, description="anchor of DOCX objects: path of the body element "
                                                              "(paragraph, table, drawing, oMath) — H-50")

    @field_validator("object_id")
    @classmethod
    def _object_id_grammar(cls, v: str) -> str:
        if not grammar.matches("object", v):
            raise ValueError(f"object_id does not match the object grammar: {v!r}")
        return v

    @model_validator(mode="after")
    def _object_rules(self):
        code = OBJECT_KIND_CODE.get(self.object_kind)
        sid, scope, tail = self.object_id.split(":")
        if sid != self.source_id or tail[0] != code:
            raise ValueError("object_id prefix/kind letter does not match source_id/object_kind")
        if self.region_origin == RegionOrigin.DOCX_ELEMENT:
            if scope != "doc" or not self.docx_paragraph_path:
                raise ValueError("DOCX objects are document-scoped (<source_id>:doc:...) with docx_paragraph_path")
        else:
            if scope == "doc":
                raise ValueError("only DOCX_ELEMENT objects are document-scoped")
            if self.page_id is None or self.object_id[: len(self.page_id) + 1] != self.page_id + ":":
                raise ValueError("object_id must start with '<page_id>:'")
        if self.region_origin == RegionOrigin.LAYOUT_MODEL and ModelRole.LAYOUT not in [m.role for m in self.models]:
            raise ValueError("region_origin LAYOUT_MODEL requires a LAYOUT model in models[] (H-03)")
        implied = TEXT_LAYER_ORIGIN.get(self.text_layer)
        if implied is not None and implied != self.origin:
            raise ValueError(f"text_layer {self.text_layer} implies origin {implied}, got {self.origin}")
        box = (self.bbox_x0, self.bbox_y0, self.bbox_x1, self.bbox_y1)
        if self.bbox_space == BboxSpace.PAGE_PT_TL:
            if any(v is None for v in box):
                raise ValueError("PAGE_PT_TL bbox needs all four coordinates")
            if not (self.bbox_x1 > self.bbox_x0 and self.bbox_y1 > self.bbox_y0):
                raise ValueError("degenerate bbox")
        elif self.bbox_space == BboxSpace.NONE:
            if any(v is not None for v in box):
                raise ValueError("bbox_space NONE requires empty bbox")
        else:
            raise ValueError("document objects use bbox_space PAGE_PT_TL or NONE in v0")
        return self


# ================================================================= documents, pages
class DocumentRow(DocEnvelope):
    """A parsed source file (one row per processed source; 013 and 022 have none)."""

    OBJECT_KIND: ClassVar[str] = ObjectKind.DOCUMENT

    format_detected: FileFormat
    format_version: str | None = None
    container_detail: str | None = None
    pagination_basis: PaginationBasis
    page_unit: PageUnit
    page_count: Int32 = Field(ge=0, description="number of page units: exactly this many rows in pages")
    page_count_check: Int32 | None = Field(None, description="the same count by a second independent method")
    page_count_check_method: str | None = None
    register_page_count_hint: Int32 | None = None
    shared_component_count: Int32 | None = Field(None, description="DjVu DJVI components (not pages)")
    pagination_render_profile: str | None = Field(None, description="DOCX: renderer, version, fonts hash")
    pagination_artifact_id: ArtifactId | None = None
    document_class: DocumentClass
    classifier_version: str | None = None
    is_encrypted: bool = False
    text_extraction_permitted: bool | None = None
    has_native_page_labels: bool | None = None
    text_layer_producer: str | None = Field(None, description="PDF Producer/Creator of the text layer, if known")
    file_meta_title: str | None = Field(None, description="embedded metadata, verbatim; a hint, never truth")
    file_meta_author: str | None = None
    file_meta_subject: str | None = None
    file_meta_keywords: str | None = None
    file_meta_creator: str | None = None
    file_meta_producer: str | None = None
    file_meta_created_raw: str | None = None
    file_meta_modified_raw: str | None = None
    file_identifiers: list[str] = Field(default_factory=list)
    file_languages: list[str] = Field(default_factory=list)
    processing_status: SourceProcessingStatus

    @model_validator(mode="after")
    def _document_rules(self):
        if self.object_id != f"{self.source_id}:doc" or self.page_id is not None:
            raise ValueError("document object_id is <source_id>:doc and page_id is NULL")
        if self.processing_status == SourceProcessingStatus.SKIPPED_BY_REGISTER:
            raise ValueError("sources skipped by the register have no document row")
        return self


class PageRow(DocEnvelope):
    """A page unit (physical PDF/DjVu page, pinned DOCX render page, EPUB spine item). Every page of the pagination
    has a row, even if it failed or was not processed (no silent page loss)."""

    OBJECT_KIND: ClassVar[str] = ObjectKind.PAGE

    page_index: Int32 = Field(ge=1, description="physical index from 1 in the page unit (= pdf_page of evidence)")
    page_kind: PageKind
    page_class: PageClass = PageClass.UNKNOWN
    page_route: PageRoute | None = None
    printed_page_raw: str | None = Field(None, description="printed label as found (PageLabels, running head...)")
    printed_page_labels: list[str] = Field(default_factory=list, description="parsed labels; two on a 2-up spread")
    printed_label_origin: PrintedLabelOrigin = PrintedLabelOrigin.NONE
    printed_label_status: PrintedLabelStatus = PrintedLabelStatus.NONE
    printed_label_extractor: str | None = Field(None, description="extractor and version that produced the label "
                                                                  "(H-35)")
    is_spread: bool | None = None
    width_pt: Float64 | None = None
    height_pt: Float64 | None = None
    rotation_deg: Int16 = 0
    page_box: PageBox | None = None
    native_dpi: Float64 | None = None
    bbox_space: BboxSpace = BboxSpace.PAGE_PT_TL
    file_text_layer: TextLayer = Field(TextLayer.NONE, description="text layer stored in the file for this page")
    file_text_status: FileTextStatus = FileTextStatus.NOT_CHECKED
    file_text_char_count: Int32 | None = None
    embedded_layer_evidence: EmbeddedLayerEvidence | None = None
    text_layer_producer: str | None = None
    ocr_status: OcrStatus = OcrStatus.NOT_RUN
    ocr_char_count: Int32 | None = None
    page_status: ProcessingStatus
    primary_text_layer: TextLayer = Field(TextLayer.NONE, description="layer used for the page text (is_primary_layer "
                                                                      "blocks)")
    primary_text_origin: Origin | None = Field(None, description="NATIVE | EMBEDDED_OCR | OCR; NULL = no text")
    normalized_text: str | None = Field(None, description="page text by text_rule (primary layer only)")
    text_rule: TextRule = TextRule.PAGE_TEXT_V1
    text_sha256: Sha256Hex | None = Field(None, description="sha256 of normalized_text (UTF-8)")
    char_count: Int32 = Field(0, ge=0)
    native_ocr_cer: Float64 | None = Field(None, description="CER of the file layer against GLM-OCR (CP-22 metric)")
    native_raw_artifact_id: ArtifactId | None = None
    layout_raw_artifact_id: ArtifactId | None = None
    ocr_raw_artifact_id: ArtifactId | None = None
    render_artifact_id: ArtifactId | None = None
    render_dpi: Int16 | None = None
    preview_artifact_id: ArtifactId | None = None
    spine_href: str | None = Field(None, description="EPUB: XHTML path inside the container (not a file system path)")

    @model_validator(mode="after")
    def _page_rules(self):
        if self.page_id is None or self.object_id != self.page_id:
            raise ValueError("page rows have object_id = page_id")
        sid, rest = self.page_id.split(":")
        if rest[0] != PAGE_KIND_UNIT[self.page_kind] or int(rest[1:]) != self.page_index:
            raise ValueError("page_id letter/index must match page_kind/page_index")
        if self.page_status == ProcessingStatus.SKIPPED_BY_REGISTER:
            raise ValueError("SKIPPED_BY_REGISTER is a source/step status, not a page status")
        if (self.primary_text_layer == TextLayer.NONE) != (self.primary_text_origin is None):
            raise ValueError("primary_text_layer NONE ⇔ primary_text_origin NULL")
        if self.primary_text_origin is not None:
            if self.primary_text_origin not in TEXT_ORIGINS:
                raise ValueError("primary_text_origin must be NATIVE, EMBEDDED_OCR or OCR")
            if TEXT_LAYER_ORIGIN[self.primary_text_layer] != self.primary_text_origin:
                raise ValueError("primary_text_layer and primary_text_origin disagree")
        expected = {ProcessingStatus.NATIVE_OK: Origin.NATIVE, ProcessingStatus.EMBEDDED_TEXT_OK: Origin.EMBEDDED_OCR,
                    ProcessingStatus.OCR_OK: Origin.OCR}.get(self.page_status)
        if expected is not None and self.primary_text_origin != expected:
            raise ValueError(f"page_status {self.page_status} requires primary_text_origin {expected} (H-02)")
        if self.page_class == PageClass.RASTER_SCAN and self.primary_text_origin == Origin.NATIVE:
            raise ValueError("a raster scan page cannot have NATIVE text (embedded OCR is EMBEDDED_OCR, H-02)")
        if self.file_text_layer == TextLayer.GLM_OCR:
            raise ValueError("file_text_layer describes the file, never GLM_OCR")
        if self.embedded_layer_evidence is not None and self.file_text_layer not in (
                TextLayer.PDF_EMBEDDED_OCR_LAYER, TextLayer.DJVU_EMBEDDED_OCR_LAYER):
            raise ValueError("embedded_layer_evidence only for embedded OCR layers")
        if (self.normalized_text is None) != (self.text_sha256 is None):
            raise ValueError("normalized_text and text_sha256 go together")
        if self.normalized_text is not None:
            if self.text_sha256 != _sha256_text(self.normalized_text):
                raise ValueError("text_sha256 must be sha256 of normalized_text")
            if self.char_count != len(self.normalized_text):
                raise ValueError("char_count must be len(normalized_text)")
        elif self.char_count != 0:
            raise ValueError("char_count must be 0 without text")
        if self.bbox_space == BboxSpace.PAGE_PT_TL:
            if self.width_pt is None or self.height_pt is None:
                raise ValueError("PAGE_PT_TL pages need width_pt and height_pt")
        elif self.bbox_space != BboxSpace.NONE:
            raise ValueError("pages use bbox_space PAGE_PT_TL or NONE")
        if self.rotation_deg not in (0, 90, 180, 270):
            raise ValueError("rotation_deg must be 0, 90, 180 or 270")
        return self


# ================================================================= page objects
class BlockRow(DocObjectEnvelope):
    """A text block of one text layer. Both layers are kept; the page text uses only ``is_primary_layer`` blocks."""

    OBJECT_KIND: ClassVar[str] = ObjectKind.BLOCK

    is_primary_layer: bool = Field(description="block of the layer used for the page text and search (H-30)")
    block_type: BlockType
    reading_order: Int32 = Field(ge=0, description="order inside (page, text layer); EPUB/DOCX: document order")
    reading_order_method: str | None = None
    native_order: Int32 | None = Field(None, description="order reported by the native extractor, if any")
    text: str = Field(description="text as extracted/recognised (not normalised)")
    normalized_text: str
    char_count: Int32 = Field(ge=0)
    language: LanguageCode | None = None
    language_confidence: Float64 | None = None
    recognition_confidence: Float64 | None = None
    raw_locator: str | None = Field(None, description="pointer into the raw artifact (JSON pointer or block number)")
    embedded_layer_evidence: EmbeddedLayerEvidence | None = None
    text_layer_producer: str | None = None

    @model_validator(mode="after")
    def _block_rules(self):
        if self.text_layer == TextLayer.NONE:
            raise ValueError("blocks always have a text layer")
        if self.char_count != len(self.text):
            raise ValueError("char_count must be len(text)")
        if self.embedded_layer_evidence is not None and self.origin != Origin.EMBEDDED_OCR:
            raise ValueError("embedded_layer_evidence only for EMBEDDED_OCR blocks")
        return self


class FigureRow(DocObjectEnvelope):
    OBJECT_KIND: ClassVar[str] = ObjectKind.FIGURE

    figure_label: str | None = Field(None, description="figure number as printed, e.g. 'Рис. 1.3'")
    caption: str | None = None
    caption_normalized: str | None = None
    caption_block_id: PageObjectId | None = None
    layout_class: FigureLayoutClass
    layout_score: Float64 | None = None
    detected_figure_type: FigureType = FigureType.UNKNOWN_FIGURE_TYPE
    figure_type_method: FigureTypeMethod = FigureTypeMethod.NONE
    figure_type_confidence: Float64 | None = None
    figure_type_threshold: Float64 | None = None
    image_artifact_id: ArtifactId | None = Field(None, description="crop of the page render")
    image_dpi: Int16 | None = None
    embedded_image_artifact_id: ArtifactId | None = Field(None, description="native embedded raster (XObject, EPUB "
                                                                            "image)")
    embedded_image_transcoded: bool | None = Field(None, description="true if the native stream was transcoded "
                                                                     "(JBIG2/CCITT → PNG), H-36")
    original_filter: str | None = Field(None, description="PDF stream filter of the embedded image, e.g. "
                                                          "JBIG2Decode")
    vector_artifacts: list[VectorArtifactRef] = Field(default_factory=list, description="native vectors in "
                                                      "PAGE_PT_TL: paths JSON and SVG (task §23)")
    raw_locator: str | None = Field(None, description="source-part-qualified XPath or pointer into the raw artifact")

    @model_validator(mode="after")
    def _figure_rules(self):
        if self.detected_figure_type != FigureType.UNKNOWN_FIGURE_TYPE:
            if (self.figure_type_method == FigureTypeMethod.NONE or self.figure_type_confidence is None
                    or self.figure_type_threshold is None
                    or self.figure_type_confidence < self.figure_type_threshold):
                raise ValueError("a figure type needs a method and confidence >= threshold; otherwise "
                                 "UNKNOWN_FIGURE_TYPE (task §22)")
        _unique([v.format for v in self.vector_artifacts], "vector artifact formats")
        if self.embedded_image_transcoded is not None and self.embedded_image_artifact_id is None:
            raise ValueError("embedded_image_transcoded without embedded_image_artifact_id")
        return self


class TableContinuationCandidate(ContractModel):
    """Explicit source-backed structural proposal; no numbering/caption heuristic and no review admission."""
    source_sha256: Sha256Hex
    target_page_index: Int32 = Field(ge=1)
    target_raw_locator: str = Field(min_length=1)
    target_raw_content_sha256: Sha256Hex
    declaration_locator: str = Field(min_length=1)
    declaration_artifact_id: ArtifactId
    basis: str
    review_status: str = "AUTO_EXTRACTED_UNREVIEWED"

    @field_validator("basis")
    @classmethod
    def _basis(cls, value):
        if value not in {"NATIVE_SOURCE_RELATION", "EXPLICIT_STRUCTURE_LINK"}:
            raise ValueError("continuation basis must be an explicit source relation or structure link")
        return value

    @field_validator("review_status")
    @classmethod
    def _unreviewed(cls, value):
        if value != "AUTO_EXTRACTED_UNREVIEWED":
            raise ValueError("a continuation candidate is unreviewed, never scientific admission")
        return value

    def declaration_sha256(self) -> str:
        raw = json.dumps(self.model_dump(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class TableContinuationProvenance(ContractModel):
    candidate: TableContinuationCandidate
    candidate_sha256: Sha256Hex
    target_object_id: PageObjectId
    target_extraction_generation: Int32 = Field(ge=1)

    @model_validator(mode="after")
    def _hash(self):
        if self.candidate_sha256 != self.candidate.declaration_sha256():
            raise ValueError("continuation candidate hash mismatch")
        return self


class TableRow(DocObjectEnvelope):
    OBJECT_KIND: ClassVar[str] = ObjectKind.TABLE

    table_label: str | None = None
    caption: str | None = None
    caption_normalized: str | None = None
    caption_block_id: PageObjectId | None = None
    recognition_method: RecognitionMethod
    raw_format: TableRawFormat
    raw_output: str = Field(description="raw recognition or native markup, always kept (task §20)")
    n_rows: Int32 | None = None
    n_cols: Int32 | None = None
    header_rows: Int16 | None = None
    cells: list[TableCell] = Field(default_factory=list, description="normalised grid; [] if structure not recovered")
    normalized_text: str | None = None
    normalized_html: str | None = None
    structure_confidence: Float64 | None = None
    image_artifact_id: ArtifactId | None = None
    image_dpi: Int16 | None = None
    continues_object_id: PageObjectId | None = Field(None, description="fragment on the next page")
    raw_locator: str | None = Field(None, description="source-part-qualified XPath or pointer into the raw artifact")
    continuation_provenance: TableContinuationProvenance | None = Field(None,
        description="explicit unreviewed continuation declaration; NULL for immutable historical fragments")

    @model_validator(mode="after")
    def _table_rules(self):
        if self.recognition_method == RecognitionMethod.NONE:
            raise ValueError("tables need a recognition_method")
        for c in self.cells:
            if (self.n_rows is not None and c.row + c.row_span > self.n_rows) or \
                    (self.n_cols is not None and c.col + c.col_span > self.n_cols):
                raise ValueError("table cell outside n_rows/n_cols")
        if self.continuation_provenance is not None:
            proof = self.continuation_provenance
            candidate = proof.candidate
            if self.continues_object_id != proof.target_object_id or self.source_sha256 != candidate.source_sha256:
                raise ValueError("continuation declaration does not bind the source/target")
            registered = {r.artifact_id for r in self.raw_artifacts} | {self.raw_artifact_id}
            if candidate.declaration_artifact_id not in registered:
                raise ValueError("continuation declaration artifact is not registered behind this source object")
            if candidate.basis == "NATIVE_SOURCE_RELATION" and self.origin != "NATIVE":
                raise ValueError("a native source relation requires a native source object")
        return self


class FormulaRow(DocObjectEnvelope):
    """Formula v0 without semantics (task §21): no variables, units or assumptions."""

    OBJECT_KIND: ClassVar[str] = ObjectKind.FORMULA

    formula_kind: FormulaKind = FormulaKind.UNKNOWN
    equation_label: str | None = Field(None, description="equation number as printed, e.g. '(3.2)'")
    recognition_method: RecognitionMethod
    raw_format: FormulaRawFormat
    raw_output: str | None = Field(None, description="raw recognition or native markup; NULL only for IMAGE_ONLY")
    normalized_latex: str | None = None
    latex_parse_ok: bool | None = None
    native_glyph_text: str | None = Field(None, description="native glyphs of the region (raw alternative, H-24)")
    recognition_confidence: Float64 | None = None
    image_artifact_id: ArtifactId | None = None
    image_dpi: Int16 | None = None

    raw_locator: str | None = Field(None, description="source-part-qualified XPath or pointer into the raw artifact")

    @model_validator(mode="after")
    def _formula_rules(self):
        if self.raw_format != FormulaRawFormat.IMAGE_ONLY and self.raw_output is None:
            raise ValueError("raw_output may be NULL only for IMAGE_ONLY formulas")
        return self


class BibliographyEntryRow(DocObjectEnvelope):
    """A reference-list entry as extracted. The citing Work is derived by SQL (view ``bibliography``), never stored."""

    OBJECT_KIND: ClassVar[str] = ObjectKind.BIBLIOGRAPHY_ENTRY

    entry_label: str | None = Field(None, description="printed number as is: '72', '[12]'")
    ordinal_in_list: Int32 | None = None
    list_block_ids: list[str] = Field(default_factory=list)
    continues_on_page_id: PageId | None = None
    text: str
    normalized_text: str
    parsed_authors: list[str] = Field(default_factory=list)
    parsed_title: str | None = None
    parsed_year_raw: str | None = None
    parsed_year: Int16 | None = None
    parsed_venue: str | None = None
    parsed_volume: str | None = None
    parsed_issue: str | None = None
    parsed_pages: str | None = None
    parsed_url: str | None = None
    parsed_doi: str | None = Field(None, description="normalised DOI (lower case, no URL prefix)")
    parsed_isbn: list[str] = Field(default_factory=list, description="ISBN-13 digits")
    parse_method: str | None = None
    parse_confidence: Float64 | None = None
    language: LanguageCode | None = None

    @field_validator("list_block_ids")
    @classmethod
    def _blocks(cls, v: list[str]) -> list[str]:
        for oid in v:
            if not grammar.matches("object", oid):
                raise ValueError(f"list_block_ids must be object ids: {oid!r}")
        return v


# ================================================================= REG / LINK envelopes
class RegEnvelope(RowModel):
    """Envelope of registry entities and curated links: input file, its hash and row answer 'where is the original'."""

    object_id: str
    object_kind: ObjectKind
    origin: Origin = Field(description="REGISTRY (loaded from PRIVATE 00_registry) or DERIVED")
    pipeline_version: SemVer
    processing_run_id: RunId
    extractor_id: str = Field(min_length=1)
    extractor_version: str = Field(min_length=1)
    config_hash: Sha256Hex
    content_sha256: Sha256Hex
    created_at: UtcDatetime
    review_status: ReviewStatus
    quality_flags: list[QualityFlag] = Field(default_factory=list)
    input_ref: LogicalRef = Field(description="logical path of the input file, e.g. PRIVATE:00_registry/...")
    input_sha256: Sha256Hex
    input_row: Int32 | None = Field(None, ge=1, description="data row number (provenance only, not identity)")

    @model_validator(mode="after")
    def _reg_rules(self):
        if self.OBJECT_KIND is not None and self.object_kind != self.OBJECT_KIND:
            raise ValueError(f"object_kind must be {self.OBJECT_KIND}")
        if self.origin not in (Origin.REGISTRY, Origin.DERIVED):
            raise ValueError("registry rows have origin REGISTRY or DERIVED")
        _unique(self.quality_flags, "quality flags")
        return self


class SourceRow(RegEnvelope):
    """A registered file (one row per register entry, 251 including 013 and 022 — CP-05). Raw register fields are
    kept verbatim; register notes are hints, never evidence (flag REGISTER_NOTES_NOT_EVIDENCE)."""

    OBJECT_KIND: ClassVar[str] = ObjectKind.SOURCE

    source_id: SourceId
    source_sha256: Sha256Hex = Field(description="register sha256 (= LFS oid); bound to source_id for life")
    size_bytes: Int64 = Field(ge=0)
    canonical_path: RelPath = Field(description="path relative to $VKM_RESOURCES_ROOT")
    original_filename: str
    file_extension: str
    format_detected: FileFormat
    file_status: FileStatus
    observed_sha256: Sha256Hex | None = None
    observed_size_bytes: Int64 | None = None
    lifecycle_status: LifecycleStatus
    register_skip_status: ProcessingStatus | None = Field(None, description="SKIPPED_BY_REGISTER when lifecycle is "
                                                                            "not ACTIVE (CP-05)")
    register_skip_reason: SkipReason | None = None
    source_class_raw: str
    priority: str
    site_scope_raw: str = Field(description="register evidence_scope, verbatim (CP-07)")
    site_scope: list[str] = Field(default_factory=list)
    site_scope_candidates: list[str] = Field(default_factory=list, description="only for AMBIGUOUS; never a filter")
    site_scope_mapping: SiteScopeMapping
    site_scope_map_version: str
    review_status_basis: ReviewStatusBasis
    evidence_coverage_raw: str | None = Field(None, description="Phase-1 CoverageLevel verbatim (001–041)")
    register_scientific_role: str
    register_migration_source: str
    register_migration_status: str
    register_notes: str
    ingestion_date: Date32 | None = None
    ingestion_date_precision: DatePrecision = DatePrecision.UNKNOWN
    ingestion_basis: IngestionBasis = IngestionBasis.UNKNOWN
    register_row_sha256: Sha256Hex = Field(description="sha256 of the canonical register row (detects row edits)")

    @model_validator(mode="after")
    def _source_rules(self):
        if self.object_id != self.source_id or self.origin != Origin.REGISTRY:
            raise ValueError("source rows: object_id = source_id, origin REGISTRY")
        inactive = self.lifecycle_status != LifecycleStatus.ACTIVE
        if inactive != (self.register_skip_status == ProcessingStatus.SKIPPED_BY_REGISTER):
            raise ValueError("lifecycle not ACTIVE ⇔ register_skip_status SKIPPED_BY_REGISTER (CP-05)")
        if self.register_skip_status not in (None, ProcessingStatus.SKIPPED_BY_REGISTER):
            raise ValueError("register_skip_status is SKIPPED_BY_REGISTER or NULL")
        if inactive != (self.register_skip_reason is not None):
            raise ValueError("register_skip_reason is set exactly for inactive sources")
        if inactive != (self.review_status == ReviewStatus.NOT_APPLICABLE):
            raise ValueError("review_status NOT_APPLICABLE ⇔ lifecycle not ACTIVE")
        if inactive != (self.review_status_basis == ReviewStatusBasis.LIFECYCLE):
            raise ValueError("review_status_basis LIFECYCLE ⇔ lifecycle not ACTIVE")
        if self.review_status == ReviewStatus.AUTO_EXTRACTED_UNREVIEWED:
            raise ValueError("a source is not an automatic object")
        bad = set(self.site_scope) - SCOPE_VALUES
        bad |= set(self.site_scope_candidates) - SCOPE_VALUES
        if bad:
            raise ValueError(f"site scope values not in vkm_world Scope: {sorted(bad)}")
        if self.site_scope_mapping in (SiteScopeMapping.AMBIGUOUS, SiteScopeMapping.NOT_A_SCOPE) and self.site_scope:
            raise ValueError("AMBIGUOUS / NOT_A_SCOPE ⇒ site_scope = []")
        if self.site_scope_candidates and self.site_scope_mapping != SiteScopeMapping.AMBIGUOUS:
            raise ValueError("site_scope_candidates only for AMBIGUOUS")
        if self.file_status == FileStatus.PRESENT_VERIFIED and self.observed_sha256 != self.source_sha256:
            raise ValueError("PRESENT_VERIFIED requires observed_sha256 = source_sha256")
        return self


class WorkRow(RegEnvelope):
    """A bibliographic work (Work ≠ Source). Built from the curated PRIVATE work register; tombstones are kept for
    ever. ``review_status`` is NOT_APPLICABLE (H-28): reading coverage is per source."""

    OBJECT_KIND: ClassVar[str] = ObjectKind.WORK

    work_id: WorkId
    status: WorkStatus
    merged_into_work_id: WorkId | None = None
    anchor_source_id: SourceId
    work_type: WorkType
    title: str | None = None
    title_en: str | None = None
    title_variants: list[str] = Field(default_factory=list)
    authors_display: str | None = Field(None, description="authors as curated, separated by '; '")
    publication_year: Int16 | None = None
    publication_year_raw: str | None = None
    publication_date: Date32 | None = None
    publication_date_precision: DatePrecision = DatePrecision.UNKNOWN
    venue_id: VenueId | None = None
    venue_display: str | None = None
    volume: str | None = None
    issue: str | None = None
    pages_range: str | None = None
    edition: str | None = None
    publisher: str | None = None
    publisher_city: str | None = None
    doi: str | None = None
    isbn: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    external_ids: list[ExternalId] = Field(default_factory=list)
    metadata_basis: WorkMetadataBasis
    identity_status: WorkIdentityStatus
    available_from: Date32 | None = Field(None, description="curated availability; NULL = UNKNOWN (no D-03 here)")
    available_from_precision: DatePrecision | None = None
    available_from_basis: AvailableBasis = AvailableBasis.UNKNOWN
    curation_status: CurationStatus
    curated_by: str | None = None
    curated_at: Date32 | None = None
    curation_receipt: str | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _work_rules(self):
        if self.object_id != self.work_id:
            raise ValueError("work rows: object_id = work_id")
        if self.review_status != ReviewStatus.NOT_APPLICABLE:
            raise ValueError("works have review_status NOT_APPLICABLE (H-28)")
        if (self.status == WorkStatus.MERGED_INTO) != (self.merged_into_work_id is not None):
            raise ValueError("merged_into_work_id is set exactly for MERGED_INTO tombstones")
        if self.merged_into_work_id == self.work_id:
            raise ValueError("a work cannot be merged into itself")
        if self.available_from_basis == AvailableBasis.ASSUMED_FROM_PUBLICATION:
            raise ValueError("ASSUMED_FROM_PUBLICATION exists only in views, never in the canon (H-19)")
        if (self.available_from is not None) != (self.available_from_basis == AvailableBasis.CURATED):
            raise ValueError("available_from is set exactly when curated")
        from vkm_corpus.ids import normalize_doi, normalize_isbn
        if self.doi is not None and normalize_doi(self.doi) != self.doi:
            raise ValueError(f"doi must be normalised: {self.doi!r}")
        for i in self.isbn:
            if normalize_isbn(i) != i:
                raise ValueError(f"isbn must be ISBN-13 digits: {i!r}")
        return self


class AuthorRow(RegEnvelope):
    """A name-key cluster (H-15): never a verified person. ``review_status`` NOT_APPLICABLE (H-25)."""

    OBJECT_KIND: ClassVar[str] = ObjectKind.AUTHOR

    author_id: AuthorId
    name_display: str
    name_key: str
    script: Script
    identity_status: IdentityStatus = IdentityStatus.NAME_KEY_ONLY
    same_as_author_id: AuthorId | None = Field(None, description="curated link only (e.g. Cyrillic ↔ Latin)")
    orcid: str | None = None
    derived_from_work_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _author_rules(self):
        if self.object_id != self.author_id or self.review_status != ReviewStatus.NOT_APPLICABLE:
            raise ValueError("authors: object_id = author_id, review_status NOT_APPLICABLE")
        if self.identity_status != IdentityStatus.NAME_KEY_ONLY:
            raise ValueError("author_id always means a name-key cluster (H-15)")
        return self


class VenueRow(RegEnvelope):
    OBJECT_KIND: ClassVar[str] = ObjectKind.VENUE

    venue_id: VenueId
    venue_type: VenueType
    title_display: str
    title_key: str
    script: Script
    issn: list[str] = Field(default_factory=list)
    identity_status: IdentityStatus = IdentityStatus.NAME_KEY_ONLY
    same_as_venue_id: VenueId | None = None
    derived_from_work_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _venue_rules(self):
        if self.object_id != self.venue_id or self.review_status != ReviewStatus.NOT_APPLICABLE:
            raise ValueError("venues: object_id = venue_id, review_status NOT_APPLICABLE")
        return self


class LinkEnvelope(RegEnvelope):
    curation_status: CurationStatus
    basis: LinkBasis
    notes: str | None = None

    @model_validator(mode="after")
    def _link_rules(self):
        if self.review_status != ReviewStatus.NOT_APPLICABLE:
            raise ValueError("curated registry links have review_status NOT_APPLICABLE (check curation_status)")
        if not grammar.matches("link", self.object_id):
            raise ValueError(f"link object_id grammar: {self.object_id!r}")
        return self


class SourceWorkLinkRow(LinkEnvelope):
    """Source → Work. Exactly one primary link per source; FOREIGN_CONTENT is never primary and always ranged."""

    OBJECT_KIND: ClassVar[str] = ObjectKind.SOURCE_WORK_LINK

    source_id: SourceId
    work_id: WorkId | None = Field(None, description="NULL only for FOREIGN_CONTENT of an unidentified work")
    link_type: SourceWorkLinkType
    is_primary: bool
    page_start: Int32 | None = Field(None, ge=1)
    page_end: Int32 | None = Field(None, ge=1)
    part_label: str | None = None
    printed_range: str | None = None

    @model_validator(mode="after")
    def _swl_rules(self):
        from vkm_corpus.ids import swl_id
        if self.object_id != swl_id(self.source_id, self.work_id, self.link_type, self.page_start, self.page_end):
            raise ValueError("SWL object_id must be derived from its composite key")
        if self.link_type == SourceWorkLinkType.CONTAINS_WORK:
            raise ValueError("CONTAINS_WORK is reserved (invalid in v0)")
        if self.link_type == SourceWorkLinkType.FOREIGN_CONTENT:
            if self.is_primary or self.page_start is None or self.page_end is None:
                raise ValueError("FOREIGN_CONTENT is never primary and always has a page range")
        elif self.work_id is None:
            raise ValueError("only FOREIGN_CONTENT may have a NULL work_id")
        if self.page_start is not None and self.page_end is not None and self.page_end < self.page_start:
            raise ValueError("page_end < page_start")
        return self


class WorkRelationRow(LinkEnvelope):
    OBJECT_KIND: ClassVar[str] = ObjectKind.WORK_RELATION

    from_work_id: WorkId
    relation: WorkRelationType
    to_work_id: WorkId
    is_symmetric: bool

    @model_validator(mode="after")
    def _wrl_rules(self):
        from vkm_corpus.contracts.vocab import SYMMETRIC_WORK_RELATIONS
        from vkm_corpus.ids import wrl_id
        if self.is_symmetric != (self.relation in SYMMETRIC_WORK_RELATIONS):
            raise ValueError("is_symmetric must follow the relation type")
        if self.is_symmetric and not self.from_work_id < self.to_work_id:
            raise ValueError("symmetric relations are stored once with from_work_id < to_work_id")
        if self.from_work_id == self.to_work_id:
            raise ValueError("a work relation needs two works")
        if self.object_id != wrl_id(self.from_work_id, self.relation, self.to_work_id):
            raise ValueError("WRL object_id must be derived from its composite key")
        return self


class SourceRelationRow(LinkEnvelope):
    OBJECT_KIND: ClassVar[str] = ObjectKind.SOURCE_RELATION

    from_source_id: SourceId
    relation: SourceRelationType
    to_source_id: SourceId
    from_page_start: Int32 | None = Field(None, ge=1)
    from_page_end: Int32 | None = Field(None, ge=1)
    to_page_start: Int32 | None = Field(None, ge=1)
    to_page_end: Int32 | None = Field(None, ge=1)

    @model_validator(mode="after")
    def _srl_rules(self):
        from vkm_corpus.ids import srl_id
        if self.from_source_id == self.to_source_id:
            raise ValueError("a source relation needs two sources")
        if self.relation == SourceRelationType.SHARES_PAGES_WITH and None in (
                self.from_page_start, self.from_page_end, self.to_page_start, self.to_page_end):
            raise ValueError("SHARES_PAGES_WITH needs both page ranges")
        if self.object_id != srl_id(self.from_source_id, self.relation, self.to_source_id, self.from_page_start,
                                    self.to_page_start):
            raise ValueError("SRL object_id must be derived from its composite key")
        return self


class WorkAuthorRow(LinkEnvelope):
    OBJECT_KIND: ClassVar[str] = ObjectKind.WORK_AUTHOR

    work_id: WorkId
    author_id: AuthorId
    ordinal: Int16 = Field(ge=1)
    role: AuthorRole
    name_as_listed: str = Field(min_length=1, description="name exactly as in the curated work row (H-15)")

    @model_validator(mode="after")
    def _wau_rules(self):
        from vkm_corpus.ids import wau_id
        if self.object_id != wau_id(self.work_id, self.ordinal):
            raise ValueError("WAU object_id must be derived from (work_id, ordinal)")
        return self


class BibliographyLinkRow(RowModel):
    """Row shape of the SQL view ``bibliography_links`` (DERIVED_VIEW, rule ``bibliography_match_v2``): never stored.

    ``match_status`` is CANDIDATE, AUTO_EXACT_ID_MATCH or AUTO_STRONG_MATCH (H-29, CP-41); CITES edges come only
    from the accepted ones."""

    OBJECT_KIND: ClassVar[str] = ObjectKind.BIBLIOGRAPHY_LINK

    object_id: str
    object_kind: ObjectKind
    entry_id: PageObjectId
    citing_source_id: SourceId
    citing_page_id: PageId | None = None
    citing_work_id: WorkId | None = None
    citing_work_resolution: CitingWorkResolution
    citing_work_is_container: bool
    cited_work_id: WorkId
    match_method: MatchMethod
    match_score: Float64 = Field(ge=0.0, le=1.0)
    match_status: MatchStatus
    matched_fields: list[str] = Field(default_factory=list)
    origin: Origin = Origin.DERIVED
    review_status: ReviewStatus = ReviewStatus.AUTO_EXTRACTED_UNREVIEWED
    rule_version: DerivedRule = DerivedRule.BIBLIOGRAPHY_MATCH_V2


# ================================================================= LOG datasets
class ProcessingRunRow(RowModel):
    """Two immutable records per run: START and END (a START without END is a visible crash, H-11)."""

    processing_run_id: RunId
    record_phase: RecordPhase
    run_kind: RunKind
    status: RunStatus
    started_at: UtcDatetime
    finished_at: UtcDatetime | None = None
    cli_command: str = Field(description="argv with logical roots instead of machine paths")
    cli_flags: list[str] = Field(default_factory=list)
    plan_only: bool = False
    selection: str | None = None
    pipeline_version: SemVer
    code_revision: str
    code_dirty: bool
    dependency_lock_sha256: Sha256Hex | None = None
    private_registry_revision: str | None = None
    source_register_sha256: Sha256Hex | None = None
    work_register_sha256: Sha256Hex | None = None
    work_links_sha256: Sha256Hex | None = None
    host_role: HostRole
    clock_source: str | None = None
    python_version: str
    platform: str
    config_hash: Sha256Hex
    config_artifact_id: ArtifactId | None = None
    log_artifact_id: ArtifactId | None = Field(None, description="RUN_LOG artifact (KEEP_RAW), H-11")
    models: list[RunModelInfo] = Field(default_factory=list)
    n_sources_planned: Int32 | None = None
    n_pages_planned: Int32 | None = None
    n_steps_executed: Int32 | None = None
    n_steps_reused: Int32 | None = None
    n_steps_failed: Int32 | None = None
    n_model_calls: Int32 | None = None
    parent_run_id: RunId | None = Field(None, description="run resumed by this one (--resume)")
    control_plane_ref: str | None = Field(None, description="optional PostgreSQL job key; the canon never needs it")
    created_at: UtcDatetime

    @model_validator(mode="after")
    def _run_rules(self):
        if self.record_phase == RecordPhase.START:
            if self.status != RunStatus.STARTED or self.finished_at is not None:
                raise ValueError("START records have status STARTED and no finished_at")
        elif self.status == RunStatus.STARTED or self.finished_at is None:
            raise ValueError("END records carry a final status and finished_at")
        return self


class ProcessingStepRow(RowModel):
    step_id: StepId
    processing_run_id: RunId
    source_id: SourceId | None = None
    page_id: PageId | None = None
    page_index: Int32 | None = None
    stage: Stage
    attempt: Int16 = Field(1, ge=1)
    outcome: StepOutcome
    status: ProcessingStatus
    reason_code: str | None = Field(None, description="SkipReason or ErrorCode behind a skip/failure")
    stage_signature: Sha256Hex
    call_signature: Sha256Hex | None = None
    source_sha256: Sha256Hex | None = None
    pipeline_version: SemVer
    extractor_id: str
    extractor_version: str
    extraction_generation: Int16 = Field(1, ge=1)
    config_hash: Sha256Hex
    model_id: str | None = None
    model_revision: str | None = None
    models: list[ModelRef] = Field(default_factory=list)
    input_artifact_ids: list[str] = Field(default_factory=list)
    output_artifact_ids: list[str] = Field(default_factory=list)
    n_objects_out: Int32 = 0
    started_at: UtcDatetime
    finished_at: UtcDatetime
    duration_ms: Int64 = Field(ge=0)
    commit_id: CommitId | None = None
    log_ref: str | None = None
    host_role: HostRole
    control_job_ref: str | None = None

    @model_validator(mode="after")
    def _step_rules(self):
        if self.finished_at < self.started_at:
            raise ValueError("finished_at < started_at")
        if self.status == ProcessingStatus.SKIPPED_BY_REGISTER:
            if self.outcome != "SKIPPED_BY_POLICY" or self.reason_code not in {r.value for r in SkipReason}:
                raise ValueError("SKIPPED_BY_REGISTER steps: outcome SKIPPED_BY_POLICY and a SkipReason code")
        for a in self.input_artifact_ids + self.output_artifact_ids:
            if not grammar.matches("artifact", a):
                raise ValueError(f"artifact ids must be sha256:<hex>: {a!r}")
        return self


class ErrorRow(RowModel):
    """Error record (task §44): code, stage, tool, message, retryable, source, page, log reference."""

    error_id: ErrorId
    processing_run_id: RunId
    step_id: StepId | None = None
    source_id: SourceId | None = None
    page_id: PageId | None = None
    page_index: Int32 | None = Field(None, description="set when pagination failed and no page_id exists")
    stage: Stage
    code: ErrorCode
    tool: str
    tool_version: str | None = None
    message: str = Field(description="sanitised: no absolute paths")
    retryable: bool
    severity: Severity = Severity.ERROR
    attempt: Int16 | None = None
    log_ref: str | None = None
    exception_type: str | None = None
    created_at: UtcDatetime


class ArtifactRow(RowModel):
    """Index row of a content-addressed blob; describes the CONTENT (context is in the referencing rows)."""

    artifact_id: ArtifactId
    artifact_kind: ArtifactKind
    media_type: str
    size_bytes: Int64 | None = Field(None, ge=0, description="NULL only for NOT_STORED_REPRODUCIBLE")
    storage_relpath: RelPath | None = Field(None, description="<kind_dir>/<hh>/<hh>/<sha256>.<ext> under "
                                                              "$VKM_DATA_ROOT/artifacts; NULL if not stored")
    retention_class: RetentionClass
    materialization: Materialization = Materialization.STORED
    recipe: ArtifactRecipe | None = None
    pixel_sha256: Sha256Hex | None = Field(None, description="hash of decoded pixels (mode, size, buffer): cache key "
                                                             "independent of PNG encoder (H-05, H-23)")
    image_width_px: Int32 | None = None
    image_height_px: Int32 | None = None
    image_dpi: Float64 | None = None
    coordinate_space: BboxSpace | None = None
    crs_status: CrsStatus | None = None
    producer_signature: Sha256Hex | None = Field(None, description="call_signature for OCR_RAW/LAYOUT_RAW")
    attempt: Int16 | None = Field(None, description="attempt number of a model call with the same call_signature")
    producer_step_id: StepId | None = None
    created_by_run_id: RunId
    registered_source_id: SourceId | None = None
    registered_page_id: PageId | None = None
    created_at: UtcDatetime

    @model_validator(mode="after")
    def _artifact_rules(self):
        from vkm_corpus.contracts.vocab import REPRODUCIBLE_ARTIFACT_KINDS
        if self.materialization == Materialization.STORED:
            if self.storage_relpath is None or self.size_bytes is None:
                raise ValueError("stored artifacts need storage_relpath and size_bytes")
        else:
            if self.artifact_kind not in REPRODUCIBLE_ARTIFACT_KINDS or self.recipe is None:
                raise ValueError("NOT_STORED_REPRODUCIBLE only for reproducible kinds and with a recipe (H-23)")
        if (self.crs_status is not None) != (self.coordinate_space in CRS_REQUIRED_SPACES):
            raise ValueError("crs_status is required for GEO/DRAWING_UNITS and forbidden otherwise (H-20)")
        return self


# ================================================================= helpers
# envelope fields per profile: excluded from content_sha256 (everything else is content)
ENVELOPE_FIELDS: dict[str, frozenset[str]] = {
    "DOC": frozenset(DocEnvelope.model_fields),
    "REG": frozenset(RegEnvelope.model_fields),
    "LINK": frozenset(LinkEnvelope.model_fields) - frozenset({"curation_status", "basis", "notes"}),
}

__all__ = [
    "RowModel", "DocEnvelope", "DocObjectEnvelope", "DocumentRow", "PageRow", "BlockRow", "FigureRow", "TableRow",
    "FormulaRow", "BibliographyEntryRow", "RegEnvelope", "SourceRow", "WorkRow", "AuthorRow", "VenueRow",
    "LinkEnvelope", "SourceWorkLinkRow", "WorkRelationRow", "SourceRelationRow", "WorkAuthorRow",
    "BibliographyLinkRow", "ProcessingRunRow", "ProcessingStepRow", "ErrorRow", "ArtifactRow", "ENVELOPE_FIELDS",
]
