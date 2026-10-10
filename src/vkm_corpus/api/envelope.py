"""Response envelope ``vkm.envelope/1`` (task §35, CP-19; H-18, H-38, H-39, H-47): every object the API or MCP returns
carries one, so an agent never guesses what it is looking at.

The envelope answers §50 without guessing: what (``object_kind``, ``object_id``, ``object_version``), where the original
is (``source_id``, ``page_id``, ``page_index``, ``geometry``, ``provenance.source_sha256``), who/what created it
(``provenance``), native or OCR (``origin`` + ``text_layer``), auto or reviewed (``review_status``), canonical, raw or
projection (``layer`` + ``payload_form`` + ``projection``). Vocabularies come from :mod:`vkm_corpus.contracts.vocab`;
``layer`` and ``payload_form`` are literals until the contract carries them (decision note DN-G-D1).

The envelope is a *view* of a canonical row, never hashed into it.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vkm_corpus.contracts.vocab import (CRS_REQUIRED_SPACES, BboxSpace, CrsStatus, LifecycleStatus, ObjectKind, Origin,
                                        ProcessingStatus, RegionOrigin, ReviewStatus, TextLayer)

ENVELOPE_VERSION = "vkm.envelope/1"
API_VERSION = "0.1.0"

Layer = Literal["CANONICAL", "ARTIFACT", "PROJECTION", "OPERATIONAL", "SERVICE", "WORKSPACE"]
PayloadForm = Literal["NORMALIZED", "RAW", "BINARY", "REFERENCE"]
# object kinds of API results that are not canonical rows
ApiObjectKind = Literal["ARTIFACT", "PROCESSING_RUN", "JOB", "RERANK_RESULT", "SEARCH_RESULT", "STATUS",
                        # NAV graph in Neo4j (agent G): concept paths and neighbourhoods
                        "NAV_GRAPH_PATHS", "NAV_GRAPH_NEIGHBOURHOOD",
                        # navigation layer (DERIVED, AUTO_EXTRACTED_UNREVIEWED): docs/corpus_platform/NAVIGATION_LAYER.md
                        "NAV_OUTLINE", "NAV_SECTION", "NAV_SECTIONS", "NAV_FORMULA", "NAV_FORMULAS", "NAV_CONCEPT",
                        # topics (RAPTOR tree, agent T) and duplicates/reprints (agent U)
                        "NAV_TOPIC", "NAV_TOPICS", "NAV_SIMILAR_SECTIONS", "NAV_SECTION_TOPICS", "NAV_COPIES",
                        "NAV_SOURCE_OVERLAP",
                        # parameter-value candidates (agent P): navigation, never recommended values
                        "NAV_PARAMETERS", "NAV_PARAMETER_SUMMARY",
                        # term dictionary (agent TR): RU/EN/DE equivalents, synonyms, abbreviations — navigation
                        "NAV_TRANSLATION",
                        # structured tables (agent TB) and repeated figures/tables/formulas (agent U2), served by G2
                        "NAV_TABLE", "NAV_TABLES", "NAV_OBJECT_COPIES", "NAV_SHARED_FORMULAS",
                        # digitized chart series (agent FD2): DERIVATION values with errors — navigation
                        "NAV_FIGURE_SERIES", "NAV_FIGURE_SERIES_LIST",
                        # topic dossier (reconstruct_topic): NAV + search + PUBLIC catalogues, navigation not evidence
                        "TOPIC_DOSSIER", "EVIDENCE_QUERY", "EVIDENCE_RECORD", "EVIDENCE_DEPENDENCIES", "EVIDENCE_COMMIT"]
# interpretation flags added by the API (the canonical quality_flags stay as stored)
API_FLAGS = frozenset({
    "SCOPE_INHERITED_FROM_SOURCE",   # H-18: the area is the source's, not established for this object
    "REGISTER_NOTES_NOT_EVIDENCE",   # H-47: register notes are curation notes, never evidence
    "WORK_HAS_MULTIPLE_COPIES",      # H-49: several registered files are copies of one work
    "CITING_WORK_IS_CONTAINER",      # H-49: the citing work is a volume/issue, not the chapter/article
    "PAGE_HAS_FOREIGN_CONTENT",      # H-16: the page carries another work (record.foreign_content_work_ids);
                                     # work_id stays the INSTANCE_OF work of the source
    "AUTHOR_NAME_KEY_ONLY",          # H-15: an author id is a cluster of a name key, not a person
    "TEXT_TRUNCATED",                # the text in the record is a window (text_offset / max_chars)
    "TOMBSTONE",                     # a merged work id: follow resolved_work_id
})
L1_KINDS = frozenset({ObjectKind.DOCUMENT, ObjectKind.PAGE, ObjectKind.BLOCK, ObjectKind.FIGURE, ObjectKind.TABLE,
                      ObjectKind.FORMULA, ObjectKind.BIBLIOGRAPHY_ENTRY, ObjectKind.BIBLIOGRAPHY_LINK})


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceScope(_Model):
    """Area of the SOURCE (CP-07); on document objects it is inherited, never established for the object (H-18)."""

    raw: str | None = Field(None, description="register evidence_scope, verbatim")
    values: list[str] = Field(default_factory=list, description="normalised vkm_world Scope values")
    mapping: str | None = Field(None, description="EXACT | CASE | SYNONYM | LOSSY | MULTI | AMBIGUOUS | NOT_A_SCOPE")
    inherited_from_source: bool = False


class Geometry(_Model):
    bbox_space: BboxSpace
    bbox: list[float] | None = Field(None, description="x0, y0, x1, y1")
    unit: str | None = Field(None, description="'pt' for PAGE_PT_TL, 'px' for IMAGE_PIXEL")
    origin: Literal["TOP_LEFT"] | None = None
    crs_status: CrsStatus | None = Field(None, description="only for DRAWING_UNITS and GEO (H-20); never inferred")
    epsg: int | None = None

    @model_validator(mode="after")
    def _crs(self) -> "Geometry":
        needs = self.bbox_space in CRS_REQUIRED_SPACES
        if needs and self.crs_status is None:
            raise ValueError("crs_status is required for DRAWING_UNITS/GEO geometry")
        if not needs and (self.crs_status is not None or self.epsg is not None):
            raise ValueError("page and pixel spaces are not geographic: no crs_status/epsg")
        if self.epsg is not None and self.crs_status != CrsStatus.EXACT_COORDINATED:
            raise ValueError("an EPSG code is only valid with EXACT_COORDINATED from the source")
        return self


class ModelInfo(_Model):
    role: str
    model_id: str
    model_revision: str


class Provenance(_Model):
    """Processing provenance inline (contract §36: not scientific provenance) + the full trace link."""

    processing_run_id: str | None = None
    pipeline_version: str | None = None
    extractor_id: str | None = None
    extractor_version: str | None = None
    extraction_generation: int | None = None
    model_id: str | None = None
    model_revision: str | None = None
    models: list[ModelInfo] = Field(default_factory=list)
    config_hash: str | None = None
    source_sha256: str | None = None
    raw_artifact_id: str | None = None
    created_at: datetime | None = None
    input_ref: str | None = Field(None, description="registry rows: logical path of the curated input file")
    input_sha256: str | None = None
    input_row: int | None = None
    trace: str | None = Field(None, description="path of the full provenance trace")


class Projection(_Model):
    engine: Literal["opensearch", "neo4j", "navigation", "catalogues"]
    index_or_graph: str | None = None
    build_id: str | None = None
    built_from_snapshot_id: str | None = None
    matches_canonical_snapshot: bool | None = None


class Envelope(_Model):
    envelope_version: Literal["vkm.envelope/1"] = ENVELOPE_VERSION
    object_id: str
    object_kind: ObjectKind | ApiObjectKind
    object_version: str | None = Field(None, description="content_sha256@commit_id (H-38)")
    source_id: str | None = None
    work_id: str | None = None
    page_id: str | None = None
    page_index: int | None = Field(None, ge=1, description="physical page index from 1 (= pdf_page of evidence)")
    page_label: str | None = Field(None, description="printed page label as found (not a key)")
    review_status: ReviewStatus
    layer: Layer
    payload_form: PayloadForm
    origin: Origin | None = Field(None, description="NATIVE | EMBEDDED_OCR | OCR | DERIVED | REGISTRY | CURATED")
    text_layer: TextLayer | None = None
    region_origin: RegionOrigin | None = None
    processing_status: ProcessingStatus | None = None
    lifecycle_status: LifecycleStatus | None = None
    quality_flags: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list, description="interpretation flags of the API (API_FLAGS)")
    source_scope: SourceScope | None = None
    geometry: Geometry | None = None
    provenance: Provenance = Field(default_factory=Provenance)
    canonical_snapshot_id: str | None = None
    projection: Projection | None = None

    @model_validator(mode="after")
    def _rules(self) -> "Envelope":
        if self.layer == "CANONICAL" and not self.canonical_snapshot_id:
            raise ValueError("a CANONICAL payload names its canonical snapshot")
        if self.layer == "PROJECTION" and self.projection is None:
            raise ValueError("a PROJECTION payload carries projection info")
        if self.payload_form == "RAW" and not (self.layer == "ARTIFACT" or (
                self.layer == "CANONICAL" and self.provenance.raw_artifact_id)):
            raise ValueError("RAW content comes from an artifact, or from a canonical row with raw_artifact_id (H-39)")
        if self.page_id and not self.source_id:
            raise ValueError("page_id without source_id")
        if self.layer == "CANONICAL" and self.object_kind in L1_KINDS and \
                self.review_status != ReviewStatus.AUTO_EXTRACTED_UNREVIEWED:
            raise ValueError("automatic document objects are AUTO_EXTRACTED_UNREVIEWED (status is never inherited)")
        unknown = set(self.flags) - API_FLAGS
        if unknown:
            raise ValueError(f"unknown API flags {sorted(unknown)}")
        return self


class Item(_Model):
    envelope: Envelope
    record: dict[str, Any]


class ApiError(_Model):
    code: str
    message: str
    retryable: bool = False
    stage: str | None = None
    tool: str | None = None
    object_id: str | None = None
    source_id: str | None = None
    page_id: str | None = None
    log_ref: str | None = Field(None, description="= request_id; find the JSON log line by it")
    hint: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ApiWarning(_Model):
    code: str
    message: str
    count: int | None = None


class Meta(_Model):
    request_id: str
    api_version: str = API_VERSION
    canonical_snapshot_id: str | None = None
    elapsed_ms: float | None = None
    warnings: list[ApiWarning] = Field(default_factory=list)


class ApiResponse(_Model):
    ok: bool
    meta: Meta
    item: Item | None = None
    items: list[Item] | None = None
    next_cursor: str | None = None
    error: ApiError | None = None
