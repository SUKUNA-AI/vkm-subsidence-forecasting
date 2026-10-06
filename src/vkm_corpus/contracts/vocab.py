"""Closed vocabularies of the VKM document layer (decision CP-16, review H-01).

This module is the only place where ``StrEnum`` classes of the platform are declared (AST test
``tests/corpus/test_contracts_vocab.py``); every other package imports them from here. Adding a value is a MINOR schema
change (``0.x``: MINOR is incompatible until the schema is frozen, H-46); renaming or removing one is MAJOR.

Values are upper-case identifiers except where an external convention fixes them (date precisions, page units, text
rule ids). Enum *values* are stored in Arrow/Parquet as plain strings.
"""
from __future__ import annotations

from enum import Enum, StrEnum


class RerankNativeProfile(str, Enum):
    """Declared gateway routes; preserve the existing string Enum contract."""

    BOTH_NATIVE_V1 = "BOTH_NATIVE_V1"
    VISUAL_ONLY_TEXT_DISABLED_V2 = "VISUAL_ONLY_TEXT_DISABLED_V2"


# ---------------------------------------------------------------- processing status (CP-16 p. 2; task §44)
class ProcessingStatus(StrEnum):
    """Status of a page or a processing step. Minimum of task §44 plus H-02 and CP-05 additions."""

    NATIVE_OK = "NATIVE_OK"                      # text from the file's own (born-digital) layer
    EMBEDDED_TEXT_OK = "EMBEDDED_TEXT_OK"        # text from a foreign OCR layer embedded in the file (H-02)
    OCR_REQUIRED = "OCR_REQUIRED"                # OCR planned but not (yet) done
    OCR_OK = "OCR_OK"
    PARTIAL = "PARTIAL"
    UNSUPPORTED = "UNSUPPORTED"
    FAILED = "FAILED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    SKIPPED_BY_REGISTER = "SKIPPED_BY_REGISTER"  # source absent/retired by the register (CP-05): steps and sources only
    NOT_PROCESSED = "NOT_PROCESSED"              # known by pagination, not processed


class SourceProcessingStatus(StrEnum):
    """Roll-up of a source (view ``source_status_summary``): 251 = sum of these classes."""

    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    FAILED = "FAILED"
    UNSUPPORTED = "UNSUPPORTED"
    NOT_PROCESSED = "NOT_PROCESSED"
    SKIPPED_BY_REGISTER = "SKIPPED_BY_REGISTER"


class SkipReason(StrEnum):
    """Why the register itself excludes a source from processing (CP-05)."""

    ARCHIVE_DELETED_AFTER_ASSEMBLY = "ARCHIVE_DELETED_AFTER_ASSEMBLY"   # VKM-SRC-013
    RETIRED_NOT_EVIDENCE = "RETIRED_NOT_EVIDENCE"                       # VKM-SRC-022


class LifecycleStatus(StrEnum):
    ACTIVE = "ACTIVE"
    ABSENT_BY_REGISTER = "ABSENT_BY_REGISTER"
    RETIRED = "RETIRED"


class FileTextStatus(StrEnum):
    """State of the text layer stored in the file itself (born-digital or embedded foreign OCR)."""

    PRESENT_OK = "PRESENT_OK"
    PRESENT_BROKEN = "PRESENT_BROKEN"
    PRESENT_PARTIAL = "PRESENT_PARTIAL"
    ABSENT = "ABSENT"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    NOT_CHECKED = "NOT_CHECKED"


class OcrStatus(StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"
    REQUIRED = "REQUIRED"
    DONE = "DONE"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    SKIPPED_BY_POLICY = "SKIPPED_BY_POLICY"
    NOT_RUN = "NOT_RUN"


class PageRoute(StrEnum):
    """Processing route chosen for a page by the classifier (agent C §5.3)."""

    NATIVE = "NATIVE"
    NATIVE_REPAIR = "NATIVE_REPAIR"
    OCR_REQUIRED = "OCR_REQUIRED"
    OCR_OPTIONAL = "OCR_OPTIONAL"
    EMPTY = "EMPTY"


class StepOutcome(StrEnum):
    EXECUTED = "EXECUTED"
    REUSED_CACHED = "REUSED_CACHED"
    SKIPPED_UP_TO_DATE = "SKIPPED_UP_TO_DATE"
    SKIPPED_BY_POLICY = "SKIPPED_BY_POLICY"
    NOT_ATTEMPTED = "NOT_ATTEMPTED"


class RunStatus(StrEnum):
    STARTED = "STARTED"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


class RunKind(StrEnum):
    REGISTRY_IMPORT = "REGISTRY_IMPORT"
    EXTRACTION = "EXTRACTION"
    OCR = "OCR"
    DERIVATION = "DERIVATION"
    SNAPSHOT_BUILD = "SNAPSHOT_BUILD"
    SCHEMA_MIGRATION = "SCHEMA_MIGRATION"
    VALIDATION = "VALIDATION"
    PLAN = "PLAN"
    ADMIT = "ADMIT"


class RecordPhase(StrEnum):
    START = "START"
    END = "END"


class Stage(StrEnum):
    """Processing stages: union of the stages of agents C and D, with CLASSIFY and LAYOUT (CP-16 p. 2)."""

    REGISTRY = "REGISTRY"            # register row, file presence, sha256
    INSPECT = "INSPECT"              # container and format detection
    PAGINATE = "PAGINATE"            # page enumeration by two methods
    CLASSIFY = "CLASSIFY"            # page/document classification and route
    NATIVE_TEXT = "NATIVE_TEXT"
    NATIVE_LAYOUT = "NATIVE_LAYOUT"
    NATIVE_VECTOR = "NATIVE_VECTOR"
    NATIVE_IMAGES = "NATIVE_IMAGES"
    RENDER = "RENDER"
    LAYOUT = "LAYOUT"                # layout model (PP-DocLayoutV3), LAYOUT_RAW
    OCR = "OCR"
    IMPORTED_LAYER = "IMPORTED_LAYER"  # recognition layer produced outside the pipeline, read from its stage cache
    TABLES = "TABLES"
    FORMULAS = "FORMULAS"
    FIGURES = "FIGURES"
    CAPTIONS = "CAPTIONS"
    BIBLIOGRAPHY = "BIBLIOGRAPHY"
    NORMALIZE = "NORMALIZE"
    OBJECTS = "OBJECTS"              # assembly of canonical document objects
    COMMIT = "COMMIT"
    DERIVE = "DERIVE"
    VALIDATE = "VALIDATE"
    SNAPSHOT = "SNAPSHOT"
    ADMIT = "ADMIT"


class ErrorCode(StrEnum):
    """Closed error vocabulary; absorbs the ``E_*`` codes of agent C (``C_ERROR_CODE_MAP``, correspondence test)."""

    # source file and register
    SOURCE_FILE_MISSING = "SOURCE_FILE_MISSING"
    SOURCE_LFS_POINTER = "SOURCE_LFS_POINTER"
    SOURCE_SHA256_MISMATCH = "SOURCE_SHA256_MISMATCH"
    SOURCE_SIZE_MISMATCH = "SOURCE_SIZE_MISMATCH"
    SOURCE_UNREADABLE = "SOURCE_UNREADABLE"
    SOURCE_BINDING_CHANGED = "SOURCE_BINDING_CHANGED"
    ENCRYPTED_PASSWORD_REQUIRED = "ENCRYPTED_PASSWORD_REQUIRED"
    ENCRYPTED_NO_PERMISSION = "ENCRYPTED_NO_PERMISSION"
    PERMISSION_POLICY = "PERMISSION_POLICY"
    FORMAT_UNSUPPORTED = "FORMAT_UNSUPPORTED"
    # pagination and resources
    PAGINATION_FAILED = "PAGINATION_FAILED"
    PAGECOUNT_MISMATCH = "PAGECOUNT_MISMATCH"
    DECODER_MISSING = "DECODER_MISSING"
    RENDER_FAILED = "RENDER_FAILED"
    TIMEOUT = "TIMEOUT"
    MEMORY_LIMIT = "MEMORY_LIMIT"
    WORKER_CRASHED = "WORKER_CRASHED"
    # extraction, layout, OCR
    NATIVE_EXTRACT_FAILED = "NATIVE_EXTRACT_FAILED"
    LAYOUT_FAILED = "LAYOUT_FAILED"
    OCR_FAILED = "OCR_FAILED"
    OCR_HTTP = "OCR_HTTP"
    OCR_TIMEOUT = "OCR_TIMEOUT"
    OCR_OOM = "OCR_OOM"
    OCR_TRUNCATED = "OCR_TRUNCATED"
    OCR_EMPTY_ON_INK = "OCR_EMPTY_ON_INK"
    OCR_QUALITY_STOP = "OCR_QUALITY_STOP"                # emergency stop of CP-22 (length/empty/repetition rates)
    IMPORTED_LAYER_REFUSED = "IMPORTED_LAYER_REFUSED"    # an imported page failed its source/size checks: old layer kept
    IMPORTED_LAYER_STALE = "IMPORTED_LAYER_STALE"        # import entries of another rule/source version: old layer kept
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    MODEL_CALL_BUDGET_EXHAUSTED = "MODEL_CALL_BUDGET_EXHAUSTED"  # --max-model-calls reached
    RAW_OUTPUT_UNPARSEABLE = "RAW_OUTPUT_UNPARSEABLE"
    NORMALIZE_FAILED = "NORMALIZE_FAILED"
    NATIVE_OCR_CONFLICT = "NATIVE_OCR_CONFLICT"
    # artifacts, ids, schema, commits
    ARTIFACT_WRITE_FAILED = "ARTIFACT_WRITE_FAILED"
    ARTIFACT_HASH_MISMATCH = "ARTIFACT_HASH_MISMATCH"
    ARTIFACT_MISSING = "ARTIFACT_MISSING"
    ID_COLLISION = "ID_COLLISION"
    SCHEMA_VALIDATION_FAILED = "SCHEMA_VALIDATION_FAILED"
    SCHEMA_UNKNOWN = "SCHEMA_UNKNOWN"
    COMMIT_FAILED = "COMMIT_FAILED"
    COMMIT_CONFLICT = "COMMIT_CONFLICT"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    # projections (agent E: Neo4j graph, OpenSearch search); values kept exactly as used by the projectors
    E_ANALYZER_MISMATCH = "E_ANALYZER_MISMATCH"
    E_BAD_FILTER = "E_BAD_FILTER"
    E_BAD_KIND = "E_BAD_KIND"
    E_BAD_MODE = "E_BAD_MODE"
    E_BAD_PREFIX = "E_BAD_PREFIX"
    E_BAD_QUERY = "E_BAD_QUERY"
    E_BAD_SIZE = "E_BAD_SIZE"
    E_BUILD_CHECK_FAILED = "E_BUILD_CHECK_FAILED"
    E_BULK_FAILED = "E_BULK_FAILED"
    E_CANON_MANIFEST_MISMATCH = "E_CANON_MANIFEST_MISMATCH"
    E_CANON_MAPPING = "E_CANON_MAPPING"
    E_CHECK_FAILED = "E_CHECK_FAILED"
    E_COUNT_MISMATCH = "E_COUNT_MISMATCH"
    E_CROSS_LAYER_LOSS = "E_CROSS_LAYER_LOSS"
    E_DANGLING_REFERENCE = "E_DANGLING_REFERENCE"
    E_DDL = "E_DDL"
    E_DIGEST_MISMATCH = "E_DIGEST_MISMATCH"
    E_DUPLICATE_ID = "E_DUPLICATE_ID"
    E_FORBIDDEN_STATUS = "E_FORBIDDEN_STATUS"
    E_INTERNAL = "E_INTERNAL"
    E_INVARIANT_VIOLATION = "E_INVARIANT_VIOLATION"
    E_LOAD_MISMATCH = "E_LOAD_MISMATCH"
    E_MODE_UNSUPPORTED = "E_MODE_UNSUPPORTED"
    E_NO_CREDENTIALS = "E_NO_CREDENTIALS"
    E_NO_DATA_ROOT = "E_NO_DATA_ROOT"
    E_NO_PREVIOUS_BUILD = "E_NO_PREVIOUS_BUILD"
    E_NO_SERVICE = "E_NO_SERVICE"
    E_NO_SNAPSHOT = "E_NO_SNAPSHOT"
    E_ORPHAN_NODES = "E_ORPHAN_NODES"
    E_PAGE_GAP = "E_PAGE_GAP"
    E_PREFLIGHT = "E_PREFLIGHT"
    E_PROJECTION_BUSY = "E_PROJECTION_BUSY"
    E_REFUSED = "E_REFUSED"
    E_RELATION_CONFLICT = "E_RELATION_CONFLICT"
    E_SEARCH_FAILED = "E_SEARCH_FAILED"
    E_SERVER_UNAVAILABLE = "E_SERVER_UNAVAILABLE"
    E_SMOKE = "E_SMOKE"
    E_STAGING_ROOT = "E_STAGING_ROOT"
    E_STALE_RUN = "E_STALE_RUN"
    E_TOO_MANY_CANDIDATES = "E_TOO_MANY_CANDIDATES"
    E_TRACE_MISMATCH = "E_TRACE_MISMATCH"
    E_UNKNOWN_FILTER = "E_UNKNOWN_FILTER"
    E_UNKNOWN_POLICY_REQUIRED = "E_UNKNOWN_POLICY_REQUIRED"
    E_UNKNOWN_RELATION_TYPE = "E_UNKNOWN_RELATION_TYPE"
    E_UNKNOWN_VALUE = "E_UNKNOWN_VALUE"
    E_WIPE_INCOMPLETE = "E_WIPE_INCOMPLETE"
    E_WIPE_PREFLIGHT = "E_WIPE_PREFLIGHT"
    E_EVIDENCE_DEPENDENCY = "E_EVIDENCE_DEPENDENCY"


# error codes of the projections (agent E) — the ``E_*`` members above
PROJECTION_ERROR_CODES: frozenset[str] = frozenset(c.value for c in ErrorCode if c.value.startswith("E_"))


# E_* codes of agent C's design (§7.5) → canonical ErrorCode. ``None``: no longer an error by decision CP-05 — the
# source is SKIPPED_BY_REGISTER with a SkipReason.
C_ERROR_CODE_MAP: dict[str, ErrorCode | None] = {
    "E_FILE_ABSENT": ErrorCode.SOURCE_FILE_MISSING,
    "E_LFS_POINTER": ErrorCode.SOURCE_LFS_POINTER,
    "E_SHA_MISMATCH": ErrorCode.SOURCE_SHA256_MISMATCH,
    "E_OPEN_FAILED": ErrorCode.SOURCE_UNREADABLE,
    "E_ENCRYPTED_NEEDS_PASSWORD": ErrorCode.ENCRYPTED_PASSWORD_REQUIRED,
    "E_PERMISSION_POLICY": ErrorCode.PERMISSION_POLICY,
    "E_UNSUPPORTED_FORMAT": ErrorCode.FORMAT_UNSUPPORTED,
    "E_RETIRED_ARCHIVE": None,
    "E_PAGE_COUNT_MISMATCH": ErrorCode.PAGECOUNT_MISMATCH,
    "E_DECODER_MISSING": ErrorCode.DECODER_MISSING,
    "E_RENDER_FAILED": ErrorCode.RENDER_FAILED,
    "E_TIMEOUT": ErrorCode.TIMEOUT,
    "E_MEMORY_LIMIT": ErrorCode.MEMORY_LIMIT,
    "E_LAYOUT_FAILED": ErrorCode.LAYOUT_FAILED,
    "E_OCR_HTTP": ErrorCode.OCR_HTTP,
    "E_OCR_TIMEOUT": ErrorCode.OCR_TIMEOUT,
    "E_OCR_TRUNCATED": ErrorCode.OCR_TRUNCATED,
    "E_OCR_EMPTY_ON_INK": ErrorCode.OCR_EMPTY_ON_INK,
    "E_NORMALIZE_FAILED": ErrorCode.NORMALIZE_FAILED,
    "E_NATIVE_OCR_CONFLICT": ErrorCode.NATIVE_OCR_CONFLICT,
}


class Severity(StrEnum):
    ERROR = "ERROR"
    FATAL = "FATAL"


class HostRole(StrEnum):
    WORKSTATION = "WORKSTATION"
    CORE = "CORE"
    EDGE = "EDGE"


# ---------------------------------------------------------------- review, curation, matching (CP-06, H-25, H-28, H-29)
class ReviewStatus(StrEnum):
    """Review state (contract v0.2 §47). Automatic L1 objects are always AUTO_EXTRACTED_UNREVIEWED.

    NOT_APPLICABLE: sources absent/retired by the register, works (H-28), authors and venues (H-25) and curated
    registry links (their check is ``curation_status``)."""

    UNSEEN = "UNSEEN"
    QUICK_LOOK_ONLY = "QUICK_LOOK_ONLY"
    AUTO_EXTRACTED_UNREVIEWED = "AUTO_EXTRACTED_UNREVIEWED"
    RELEVANT_SECTIONS_REVIEWED = "RELEVANT_SECTIONS_REVIEWED"
    FULLY_REVIEWED = "FULLY_REVIEWED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


# Values that must never appear in any status column of L1 (task §14, §49); rejected by pydantic and the validator.
FORBIDDEN_STATUS_VALUES: frozenset[str] = frozenset({
    "FACT", "REVIEWED_MEASUREMENT", "ACCEPTED_PARAMETER", "ACCEPTED_FORMULA",
    # epistemic statuses of vkm_world.core.provenance.EpistemicStatus: the document layer carries none (CP-03)
    "DERIVATION", "INTERPOLATION", "MODEL_CHOICE", "ENGINEERING_ASSUMPTION", "ANALOGUE",
})


class ReviewStatusBasis(StrEnum):
    PHASE1_COVERAGE_MASTER = "PHASE1_COVERAGE_MASTER"      # 001–041: reading coverage of Phase 1 (not object review)
    INTAKE_QUICK_LOOK_MARKER = "INTAKE_QUICK_LOOK_MARKER"  # 196–251: quick-look marker in the register notes
    DEFAULT_UNSEEN = "DEFAULT_UNSEEN"                      # 042–195
    LIFECYCLE = "LIFECYCLE"                                # 013, 022: not processed by the register


class CurationStatus(StrEnum):
    AUTO_PROPOSED = "AUTO_PROPOSED"
    CURATED = "CURATED"
    REJECTED = "REJECTED"


class MatchStatus(StrEnum):
    """Bibliography entry → Work match (H-29, CP-41): no curated matches in v0.

    AUTO_EXACT_ID_MATCH: equal DOI or ISBN-13. AUTO_STRONG_MATCH: the normalised title of the entry equals the work's
    (≥ 20 characters) in the same year, or is ≥ 0.90 similar with a shared author surname in the same year — and the
    entry points to no other work at that level. Everything weaker (or ambiguous) stays CANDIDATE."""

    CANDIDATE = "CANDIDATE"
    AUTO_EXACT_ID_MATCH = "AUTO_EXACT_ID_MATCH"
    AUTO_STRONG_MATCH = "AUTO_STRONG_MATCH"
    REJECTED = "REJECTED"


# statuses that resolve an entry to a work (graph RESOLVES_TO, view ``cites``); CANDIDATE is only a suggestion
ACCEPTED_MATCH_STATUSES: frozenset[str] = frozenset({MatchStatus.AUTO_EXACT_ID_MATCH, MatchStatus.AUTO_STRONG_MATCH})


class MatchMethod(StrEnum):
    DOI_EXACT = "DOI_EXACT"
    ISBN_EXACT = "ISBN_EXACT"
    TITLE_AUTHOR_YEAR = "TITLE_AUTHOR_YEAR"
    TITLE_YEAR = "TITLE_YEAR"
    PHASE1_CITATION_GRAPH = "PHASE1_CITATION_GRAPH"


class CitingWorkResolution(StrEnum):
    UNIQUE_LINK = "UNIQUE_LINK"
    RANGED_COMPONENT = "RANGED_COMPONENT"
    AMBIGUOUS = "AMBIGUOUS"
    FOREIGN_CONTENT = "FOREIGN_CONTENT"
    NO_LINK = "NO_LINK"


class DerivedRule(StrEnum):
    """Versions of the derived SQL rules (``vkm_corpus/duckdb/sql``); carried as ``rule_version`` (H-17)."""

    CITING_WORK_V1 = "citing_work_v1"
    BIBLIOGRAPHY_MATCH_V1 = "bibliography_match_v1"
    BIBLIOGRAPHY_MATCH_V2 = "bibliography_match_v2"
    CITES_V1 = "cites_v1"
    CITES_V2 = "cites_v2"
    PAGE_SEQUENCE_V1 = "page_sequence_v1"
    DUPLICATE_PAGES_V1 = "duplicate_pages_v1"
    SOURCE_SCOPE_V1 = "source_scope_v1"


# ---------------------------------------------------------------- origin and text layers (CP-16 p. 2, H-02, H-03)
class Origin(StrEnum):
    NATIVE = "NATIVE"                # the file's own born-digital structures (text layer, vectors, XML, XHTML)
    EMBEDDED_OCR = "EMBEDDED_OCR"    # foreign OCR layer embedded in a scan (PDF invisible text, DjVu TXTz) — not NATIVE
    OCR = "OCR"                      # output of a recognition model (GLM-OCR, imported PaddleOCR-VL); model mandatory
    DERIVED = "DERIVED"              # computed from other canonical rows (render pages of DOCX, authors, matches)
    REGISTRY = "REGISTRY"            # loaded from PRIVATE 00_registry files
    CURATED = "CURATED"              # human correction through a future review overlay; not written in v0


DOCUMENT_ORIGINS: frozenset[str] = frozenset({Origin.NATIVE, Origin.EMBEDDED_OCR, Origin.OCR, Origin.DERIVED})
TEXT_ORIGINS: frozenset[str] = frozenset({Origin.NATIVE, Origin.EMBEDDED_OCR, Origin.OCR})


class TextLayer(StrEnum):
    PDF_TEXT_LAYER = "PDF_TEXT_LAYER"
    PDF_EMBEDDED_OCR_LAYER = "PDF_EMBEDDED_OCR_LAYER"
    DJVU_EMBEDDED_OCR_LAYER = "DJVU_EMBEDDED_OCR_LAYER"
    EPUB_XHTML = "EPUB_XHTML"
    DOCX_XML = "DOCX_XML"
    GLM_OCR = "GLM_OCR"
    PADDLEOCR_VL = "PADDLEOCR_VL"    # imported OCR v2 layer (PaddleX PP-DocLayoutV3 + PaddleOCR-VL-1.6), CHOICE_V1 pages
    NONE = "NONE"


# text layer → the origin it implies (validator; NONE implies nothing)
TEXT_LAYER_ORIGIN: dict[str, str] = {
    TextLayer.PDF_TEXT_LAYER: Origin.NATIVE,
    TextLayer.PDF_EMBEDDED_OCR_LAYER: Origin.EMBEDDED_OCR,
    TextLayer.DJVU_EMBEDDED_OCR_LAYER: Origin.EMBEDDED_OCR,
    TextLayer.EPUB_XHTML: Origin.NATIVE,
    TextLayer.DOCX_XML: Origin.NATIVE,
    TextLayer.GLM_OCR: Origin.OCR,
    TextLayer.PADDLEOCR_VL: Origin.OCR,
}
# layers produced by a recognition engine: never a file's own layer (``pages.file_text_layer``)
OCR_ENGINE_TEXT_LAYERS: frozenset[str] = frozenset(k for k, v in TEXT_LAYER_ORIGIN.items() if v == Origin.OCR)


class EmbeddedLayerEvidence(StrEnum):
    HIDDEN_TEXT_LAYER = "HIDDEN_TEXT_LAYER"      # PDF text in render mode 3 / zero opacity over a scan
    VISIBLE_OVER_IMAGE = "VISIBLE_OVER_IMAGE"    # PDF text in a visible mode under/over the page image
    DJVU_TXT = "DJVU_TXT"                        # DjVu TXTz/TXTa chunk


class RegionOrigin(StrEnum):
    """What produced the region (bbox) of an object (H-03)."""

    PDF_TEXT_BLOCK = "PDF_TEXT_BLOCK"
    PDF_XOBJECT = "PDF_XOBJECT"
    VECTOR_CLUSTER = "VECTOR_CLUSTER"
    NATIVE_TABLE_FINDER = "NATIVE_TABLE_FINDER"
    LAYOUT_MODEL = "LAYOUT_MODEL"
    EPUB_ELEMENT = "EPUB_ELEMENT"
    DOCX_ELEMENT = "DOCX_ELEMENT"
    DJVU_TEXT_ZONE = "DJVU_TEXT_ZONE"
    OCR_MODEL = "OCR_MODEL"


class ModelRole(StrEnum):
    LAYOUT = "LAYOUT"
    RECOGNITION = "RECOGNITION"


class RecognitionMethod(StrEnum):
    """How the content of a table/formula was obtained (H-24)."""

    NATIVE_FIND_TABLES = "NATIVE_FIND_TABLES"
    NATIVE_TEXT_LAYER = "NATIVE_TEXT_LAYER"
    EMBEDDED_TEXT_LAYER = "EMBEDDED_TEXT_LAYER"
    NATIVE_OMML = "NATIVE_OMML"
    NATIVE_MATHML = "NATIVE_MATHML"
    EPUB_XHTML = "EPUB_XHTML"
    DOCX_XML = "DOCX_XML"
    OCR_GLM = "OCR_GLM"
    OCR_PADDLEOCR_VL = "OCR_PADDLEOCR_VL"   # imported PaddleOCR-VL-1.6 answer (OCR v2), chosen by its postprocess
    EPUB_IMAGE_OCR = "EPUB_IMAGE_OCR"
    BOTH_AGREE = "BOTH_AGREE"
    BOTH_DISAGREE = "BOTH_DISAGREE"
    NONE = "NONE"


class RawArtifactRole(StrEnum):
    """Role of a raw artifact attached to an object (``raw_artifacts[]``, H-24)."""

    NATIVE_EXTRACT = "NATIVE_EXTRACT"
    NATIVE_TABLE_FINDER = "NATIVE_TABLE_FINDER"
    EMBEDDED_TEXT_LAYER = "EMBEDDED_TEXT_LAYER"
    LAYOUT_DETECTIONS = "LAYOUT_DETECTIONS"
    OCR_RESPONSE = "OCR_RESPONSE"
    SOURCE_MARKUP = "SOURCE_MARKUP"
    STRUCTURAL_DIAGNOSTICS = "STRUCTURAL_DIAGNOSTICS"


class VectorFormat(StrEnum):
    PATHS_JSON = "PATHS_JSON"
    SVG = "SVG"


class TextRule(StrEnum):
    """Versioned text rules of ``vkm_corpus.contracts.text_rules`` (H-04)."""

    PAGE_TEXT_V1 = "page_text_v1"
    RERANK_TEXT_V1 = "rerank_text_v1"


# ---------------------------------------------------------------- formats, pages, objects
class FileFormat(StrEnum):
    PDF = "PDF"
    DJVU = "DJVU"
    EPUB = "EPUB"
    DOCX = "DOCX"
    ZIP = "ZIP"
    IMAGE = "IMAGE"
    UNKNOWN = "UNKNOWN"


class FileStatus(StrEnum):
    PRESENT_VERIFIED = "PRESENT_VERIFIED"
    MISSING = "MISSING"
    SHA256_MISMATCH = "SHA256_MISMATCH"
    SIZE_MISMATCH = "SIZE_MISMATCH"
    LFS_POINTER_ONLY = "LFS_POINTER_ONLY"
    UNREADABLE = "UNREADABLE"


class DocumentClass(StrEnum):
    """Source-level class (agent C §2.4); thresholds are parameters of ``classifier_version``."""

    NATIVE = "NATIVE"
    SCANNED_NO_TEXT = "SCANNED_NO_TEXT"
    SCANNED_WITH_TEXT_LAYER = "SCANNED_WITH_TEXT_LAYER"
    SCANNED_PARTIAL_TEXT_LAYER = "SCANNED_PARTIAL_TEXT_LAYER"
    BROKEN_TEXT_LAYER = "BROKEN_TEXT_LAYER"
    MIXED = "MIXED"
    REFLOWABLE_EPUB = "REFLOWABLE_EPUB"
    WORD_DOCX = "WORD_DOCX"
    UNKNOWN = "UNKNOWN"


class PageClass(StrEnum):
    """Page-level class (agent C §2.1)."""

    NATIVE_TEXT = "NATIVE_TEXT"
    MIXED = "MIXED"
    VECTOR = "VECTOR"
    RASTER_SCAN = "RASTER_SCAN"
    BROKEN_TEXT_LAYER = "BROKEN_TEXT_LAYER"
    EMPTY = "EMPTY"
    REFLOWABLE = "REFLOWABLE"
    RENDERED_FROM_SOURCE = "RENDERED_FROM_SOURCE"
    UNKNOWN = "UNKNOWN"


class PageKind(StrEnum):
    PDF_PAGE = "PDF_PAGE"
    DJVU_PAGE = "DJVU_PAGE"
    DOCX_RENDERED_PAGE = "DOCX_RENDERED_PAGE"
    EPUB_SPINE_ITEM = "EPUB_SPINE_ITEM"
    IMAGE_FRAME = "IMAGE_FRAME"


class PageUnit(StrEnum):
    """Letter of the page unit inside ``page_id`` (CP-08): physical page, pinned render page, EPUB spine item."""

    PHYSICAL = "p"
    RENDER = "r"
    SPINE = "s"


PAGE_KIND_UNIT: dict[str, str] = {
    PageKind.PDF_PAGE: PageUnit.PHYSICAL,
    PageKind.DJVU_PAGE: PageUnit.PHYSICAL,
    PageKind.IMAGE_FRAME: PageUnit.PHYSICAL,
    PageKind.DOCX_RENDERED_PAGE: PageUnit.RENDER,
    PageKind.EPUB_SPINE_ITEM: PageUnit.SPINE,
}


class PaginationBasis(StrEnum):
    PDF_PAGE_TREE = "PDF_PAGE_TREE"
    DJVU_PAGE_ORDER = "DJVU_PAGE_ORDER"
    DOCX_PINNED_RENDER = "DOCX_PINNED_RENDER"
    EPUB_SPINE = "EPUB_SPINE"
    IMAGE_FRAMES = "IMAGE_FRAMES"


class PageBox(StrEnum):
    CROPBOX = "CROPBOX"
    MEDIABOX = "MEDIABOX"
    DJVU_IMAGE = "DJVU_IMAGE"
    RENDER_PDF = "RENDER_PDF"


class PrintedLabelOrigin(StrEnum):
    """Where a printed page label came from; the extractor and its version are in ``printed_label_extractor`` (H-35)."""

    PDF_PAGE_LABELS = "PDF_PAGE_LABELS"
    RUNNING_HEAD_NATIVE = "RUNNING_HEAD_NATIVE"
    RUNNING_HEAD_OCR = "RUNNING_HEAD_OCR"
    EPUB_PAGE_ANCHOR = "EPUB_PAGE_ANCHOR"
    DOCX_RENDER = "DOCX_RENDER"
    CURATED = "CURATED"
    NONE = "NONE"


class PrintedLabelStatus(StrEnum):
    CONSISTENT_SEQUENCE = "CONSISTENT_SEQUENCE"
    ISOLATED = "ISOLATED"
    CONFLICT = "CONFLICT"
    UNPARSED = "UNPARSED"
    NONE = "NONE"


class ObjectKind(StrEnum):
    SOURCE = "SOURCE"
    WORK = "WORK"
    DOCUMENT = "DOCUMENT"
    PAGE = "PAGE"
    BLOCK = "BLOCK"
    FIGURE = "FIGURE"
    TABLE = "TABLE"
    FORMULA = "FORMULA"
    BIBLIOGRAPHY_ENTRY = "BIBLIOGRAPHY_ENTRY"
    AUTHOR = "AUTHOR"
    VENUE = "VENUE"
    SOURCE_WORK_LINK = "SOURCE_WORK_LINK"
    WORK_RELATION = "WORK_RELATION"
    SOURCE_RELATION = "SOURCE_RELATION"
    WORK_AUTHOR = "WORK_AUTHOR"
    BIBLIOGRAPHY_LINK = "BIBLIOGRAPHY_LINK"


# kind letter of a page object id (CP-16 p. 3)
OBJECT_KIND_CODE: dict[str, str] = {
    ObjectKind.BLOCK: "b",
    ObjectKind.FIGURE: "f",
    ObjectKind.TABLE: "t",
    ObjectKind.FORMULA: "m",
    ObjectKind.BIBLIOGRAPHY_ENTRY: "c",
}


class BlockType(StrEnum):
    TEXT = "TEXT"
    TITLE = "TITLE"
    HEADING = "HEADING"
    ABSTRACT = "ABSTRACT"
    CAPTION = "CAPTION"
    LIST_ITEM = "LIST_ITEM"
    FOOTNOTE = "FOOTNOTE"
    PAGE_HEADER = "PAGE_HEADER"
    PAGE_FOOTER = "PAGE_FOOTER"
    PAGE_NUMBER = "PAGE_NUMBER"
    REFERENCE_LIST = "REFERENCE_LIST"
    TABLE_OF_CONTENTS = "TABLE_OF_CONTENTS"
    SIDE_TEXT = "SIDE_TEXT"
    CODE = "CODE"
    FORMULA_NUMBER = "FORMULA_NUMBER"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class FigureType(StrEnum):
    MAP = "MAP"
    MINE_PLAN = "MINE_PLAN"
    GEOLOGICAL_SECTION = "GEOLOGICAL_SECTION"
    GEOLOGICAL_COLUMN = "GEOLOGICAL_COLUMN"
    CHART = "CHART"
    PHOTO = "PHOTO"
    SCHEMATIC_DIAGRAM = "SCHEMATIC_DIAGRAM"
    RADARGRAM = "RADARGRAM"
    TABLE_IMAGE = "TABLE_IMAGE"
    OTHER = "OTHER"
    UNKNOWN_FIGURE_TYPE = "UNKNOWN_FIGURE_TYPE"


class FigureTypeMethod(StrEnum):
    MODEL_CLASSIFIER = "MODEL_CLASSIFIER"
    HEURISTIC = "HEURISTIC"
    LAYOUT_CLASS = "LAYOUT_CLASS"
    CURATED = "CURATED"
    NONE = "NONE"


class FigureLayoutClass(StrEnum):
    RASTER_IMAGE = "RASTER_IMAGE"
    VECTOR_GRAPHICS = "VECTOR_GRAPHICS"
    MIXED = "MIXED"
    CHART = "CHART"
    LAYOUT_REGION = "LAYOUT_REGION"


class TableRawFormat(StrEnum):
    HTML = "HTML"
    MARKDOWN = "MARKDOWN"
    OTSL = "OTSL"
    JSON = "JSON"
    TEXT = "TEXT"
    DOCX_XML = "DOCX_XML"
    XHTML = "XHTML"


class FormulaKind(StrEnum):
    DISPLAY = "DISPLAY"
    INLINE = "INLINE"
    UNKNOWN = "UNKNOWN"


class FormulaRawFormat(StrEnum):
    LATEX = "LATEX"
    OMML = "OMML"
    MATHML = "MATHML"
    TEXT = "TEXT"
    IMAGE_ONLY = "IMAGE_ONLY"


# ---------------------------------------------------------------- geometry (CP-16 p. 4, H-20)
class BboxSpace(StrEnum):
    """Coordinate space of a bbox or of an artifact's geometry. PAGE_PT_TL: PostScript points, top-left origin of the
    upright displayed page (after /Rotate, CropBox∩MediaBox). Not a CRS."""

    PAGE_PT_TL = "PAGE_PT_TL"
    IMAGE_PIXEL = "IMAGE_PIXEL"
    DRAWING_UNITS = "DRAWING_UNITS"
    GEO = "GEO"
    NONE = "NONE"


# spaces that require ``crs_status`` (H-20); everywhere else crs_status must be NULL
CRS_REQUIRED_SPACES: frozenset[str] = frozenset({BboxSpace.GEO, BboxSpace.DRAWING_UNITS})


class CrsStatus(StrEnum):
    EXACT_COORDINATED = "EXACT_COORDINATED"
    LOCAL_COORDINATES = "LOCAL_COORDINATES"
    UNKNOWN_CRS = "UNKNOWN_CRS"
    MAP_DIGITIZED = "MAP_DIGITIZED"
    RELATIVE = "RELATIVE"
    SCHEMATIC = "SCHEMATIC"
    UNKNOWN = "UNKNOWN"


# ---------------------------------------------------------------- artifacts (CP-16 p. 6, H-03, H-11, H-20, H-23, H-37)
class ArtifactKind(StrEnum):
    PAGE_RENDER = "PAGE_RENDER"
    PAGE_PREVIEW = "PAGE_PREVIEW"
    OCR_INPUT = "OCR_INPUT"                    # exact image sent to a model call (KEEP_RAW: reproducibility)
    FIGURE_CROP = "FIGURE_CROP"
    TABLE_CROP = "TABLE_CROP"
    FORMULA_CROP = "FORMULA_CROP"
    EMBEDDED_IMAGE = "EMBEDDED_IMAGE"
    VECTOR_PATHS_JSON = "VECTOR_PATHS_JSON"
    VECTOR_SVG = "VECTOR_SVG"
    NATIVE_RAW = "NATIVE_RAW"
    LAYOUT_RAW = "LAYOUT_RAW"
    OCR_RAW = "OCR_RAW"
    DOCX_RENDERED_PDF = "DOCX_RENDERED_PDF"
    RUN_CONFIG = "RUN_CONFIG"
    RUN_LOG = "RUN_LOG"
    RUN_PLAN = "RUN_PLAN"
    VALIDATION_REPORT = "VALIDATION_REPORT"
    CAD_DWG = "CAD_DWG"
    CAD_DXF = "CAD_DXF"
    CAD_GEOMETRY_JSONL = "CAD_GEOMETRY_JSONL"
    CAD_LOG = "CAD_LOG"


class RetentionClass(StrEnum):
    KEEP_RAW = "KEEP_RAW"                # never deleted automatically; published to CORE and backed up (H-10)
    KEEP_REFERENCED = "KEEP_REFERENCED"  # deletable only when no kept snapshot references it


class Materialization(StrEnum):
    STORED = "STORED"
    NOT_STORED_REPRODUCIBLE = "NOT_STORED_REPRODUCIBLE"   # only the recipe and hashes are kept (H-23)


# storage directory, default retention and whether NOT_STORED_REPRODUCIBLE is allowed, per artifact kind
ARTIFACT_KIND_DIR: dict[str, str] = {
    ArtifactKind.PAGE_RENDER: "page_renders",
    ArtifactKind.PAGE_PREVIEW: "previews",
    ArtifactKind.OCR_INPUT: "ocr_inputs",
    ArtifactKind.FIGURE_CROP: "figures",
    ArtifactKind.TABLE_CROP: "tables",
    ArtifactKind.FORMULA_CROP: "formulas",
    ArtifactKind.EMBEDDED_IMAGE: "embedded",
    ArtifactKind.VECTOR_PATHS_JSON: "vector",
    ArtifactKind.VECTOR_SVG: "vector",
    ArtifactKind.NATIVE_RAW: "native_raw",
    ArtifactKind.LAYOUT_RAW: "layout_raw",
    ArtifactKind.OCR_RAW: "ocr_raw",
    ArtifactKind.DOCX_RENDERED_PDF: "docx_render",
    ArtifactKind.RUN_CONFIG: "configs",
    ArtifactKind.RUN_LOG: "logs",
    ArtifactKind.RUN_PLAN: "reports",
    ArtifactKind.VALIDATION_REPORT: "reports",
    ArtifactKind.CAD_DWG: "cad",
    ArtifactKind.CAD_DXF: "cad",
    ArtifactKind.CAD_GEOMETRY_JSONL: "cad",
    ArtifactKind.CAD_LOG: "cad",
}
ARTIFACT_KIND_RETENTION: dict[str, str] = {
    **{k: RetentionClass.KEEP_REFERENCED for k in ArtifactKind},
    ArtifactKind.OCR_INPUT: RetentionClass.KEEP_RAW,
    ArtifactKind.NATIVE_RAW: RetentionClass.KEEP_RAW,
    ArtifactKind.LAYOUT_RAW: RetentionClass.KEEP_RAW,
    ArtifactKind.OCR_RAW: RetentionClass.KEEP_RAW,
    ArtifactKind.DOCX_RENDERED_PDF: RetentionClass.KEEP_RAW,
    ArtifactKind.RUN_CONFIG: RetentionClass.KEEP_RAW,
    ArtifactKind.RUN_LOG: RetentionClass.KEEP_RAW,
    ArtifactKind.RUN_PLAN: RetentionClass.KEEP_RAW,
    ArtifactKind.VALIDATION_REPORT: RetentionClass.KEEP_RAW,
}
# media type → file extension of a stored blob (``<kind_dir>/<hh>/<hh>/<sha256>.<ext>``)
MEDIA_TYPE_EXT: dict[str, str] = {
    "application/json": "json",
    "application/gzip+json": "json.gz",
    "application/x-ndjson": "jsonl",
    "application/x-ndjson+gzip": "jsonl.gz",
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/tiff": "tif",
    "image/svg+xml": "svg",
    "application/pdf": "pdf",
    "image/vnd.dxf": "dxf",
    "image/vnd.dwg": "dwg",
    "text/plain": "txt",
    "application/octet-stream": "bin",
}

# kinds that may be registered without a stored blob (reproducible from a recipe, H-23)
REPRODUCIBLE_ARTIFACT_KINDS: frozenset[str] = frozenset({
    ArtifactKind.PAGE_RENDER, ArtifactKind.NATIVE_RAW, ArtifactKind.VECTOR_SVG,
})
# CAD outputs are derived and never an input of document extraction (H-20)
CAD_ARTIFACT_KINDS: frozenset[str] = frozenset({
    ArtifactKind.CAD_DWG, ArtifactKind.CAD_DXF, ArtifactKind.CAD_GEOMETRY_JSONL, ArtifactKind.CAD_LOG,
})


# ---------------------------------------------------------------- quality flags (closed, with applicability)
class QualityFlag(StrEnum):
    # text layers (pages, blocks, tables, formulas, bibliography entries)
    LOW_OCR_CONFIDENCE = "LOW_OCR_CONFIDENCE"
    BROKEN_TEXT_LAYER = "BROKEN_TEXT_LAYER"
    GARBAGE_GLYPHS = "GARBAGE_GLYPHS"
    PUA_GLYPHS = "PUA_GLYPHS"
    MOJIBAKE_NOT_REPAIRABLE = "MOJIBAKE_NOT_REPAIRABLE"
    REPAIRABLE_CP1251_REMAP = "REPAIRABLE_CP1251_REMAP"
    ENCODING_REPAIRED = "ENCODING_REPAIRED"
    UNMAPPED_SYMBOL_GLYPHS = "UNMAPPED_SYMBOL_GLYPHS"
    FONT_WITHOUT_TOUNICODE = "FONT_WITHOUT_TOUNICODE"
    TYPE3_FONT = "TYPE3_FONT"
    LOW_FUNCTION_WORD_RATE = "LOW_FUNCTION_WORD_RATE"
    MIXED_TEXT_LAYER = "MIXED_TEXT_LAYER"
    EMBEDDED_TEXT_LAYER = "EMBEDDED_TEXT_LAYER"
    HIDDEN_TEXT_LAYER = "HIDDEN_TEXT_LAYER"
    TEXT_LAYER_VISIBLE_MODE = "TEXT_LAYER_VISIBLE_MODE"
    NATIVE_OCR_DISAGREE = "NATIVE_OCR_DISAGREE"
    LANGUAGE_UNCERTAIN = "LANGUAGE_UNCERTAIN"
    TRUNCATED = "TRUNCATED"
    REPETITION = "REPETITION"
    # pages
    ROTATED = "ROTATED"
    SKEW_CORRECTED = "SKEW_CORRECTED"
    LOW_RESOLUTION_SCAN = "LOW_RESOLUTION_SCAN"
    SPREAD_2UP = "SPREAD_2UP"
    EMPTY_PAGE = "EMPTY_PAGE"
    VECTOR_NO_TEXT = "VECTOR_NO_TEXT"
    IMAGE_NO_TEXT = "IMAGE_NO_TEXT"
    DUPLICATE_PAGE_CANDIDATE = "DUPLICATE_PAGE_CANDIDATE"
    FOREIGN_WORK_CONTENT = "FOREIGN_WORK_CONTENT"
    EPUB_PAGE_ANCHOR_CALIBRE = "EPUB_PAGE_ANCHOR_CALIBRE"
    # any page object
    BBOX_APPROX = "BBOX_APPROX"
    CROSS_PAGE_CONTINUATION = "CROSS_PAGE_CONTINUATION"
    DUPLICATE_DETECTION_DISAMBIGUATED = "DUPLICATE_DETECTION_DISAMBIGUATED"
    SCOPE_INHERITED_FROM_SOURCE = "SCOPE_INHERITED_FROM_SOURCE"
    # figures and tables
    CAPTION_NOT_FOUND = "CAPTION_NOT_FOUND"
    CAPTION_ASSOCIATION_UNCERTAIN = "CAPTION_ASSOCIATION_UNCERTAIN"
    FIGURE_TYPE_LOW_CONFIDENCE = "FIGURE_TYPE_LOW_CONFIDENCE"
    TABLE_STRUCTURE_UNCERTAIN = "TABLE_STRUCTURE_UNCERTAIN"
    SPANNING_CELLS = "SPANNING_CELLS"
    EMPTY_CELLS_RATIO = "EMPTY_CELLS_RATIO"
    NUMERIC_PARSE_FAILURES = "NUMERIC_PARSE_FAILURES"
    # formulas
    FORMULA_LATEX_UNPARSEABLE = "FORMULA_LATEX_UNPARSEABLE"
    LATEX_UNBALANCED = "LATEX_UNBALANCED"
    CONVERSION_FAILED = "CONVERSION_FAILED"
    # bibliography
    CITING_WORK_IS_CONTAINER = "CITING_WORK_IS_CONTAINER"
    # sources and documents
    LEADING_BYTES_BEFORE_HEADER = "LEADING_BYTES_BEFORE_HEADER"
    ENCRYPTED_SOURCE = "ENCRYPTED_SOURCE"
    PDF_PERMISSIONS_RESTRICTED = "PDF_PERMISSIONS_RESTRICTED"
    PAGECOUNT_DIFFERS_FROM_REGISTER_HINT = "PAGECOUNT_DIFFERS_FROM_REGISTER_HINT"
    REGISTER_NOTES_NOT_EVIDENCE = "REGISTER_NOTES_NOT_EVIDENCE"
    DJVU_TITLE_NOT_VERIFIED = "DJVU_TITLE_NOT_VERIFIED"


_TEXT_DATASETS = frozenset({"pages", "blocks", "tables", "formulas", "bibliography_entries", "figures"})
_OBJECT_DATASETS = frozenset({"blocks", "figures", "tables", "formulas", "bibliography_entries"})
_SOURCE_DATASETS = frozenset({"sources", "documents"})

# flag → datasets where it may appear (validator check E06)
QUALITY_FLAG_SCOPE: dict[str, frozenset[str]] = {
    **{f: _TEXT_DATASETS | {"documents"} for f in (
        QualityFlag.LOW_OCR_CONFIDENCE, QualityFlag.BROKEN_TEXT_LAYER, QualityFlag.GARBAGE_GLYPHS,
        QualityFlag.PUA_GLYPHS, QualityFlag.MOJIBAKE_NOT_REPAIRABLE, QualityFlag.REPAIRABLE_CP1251_REMAP,
        QualityFlag.ENCODING_REPAIRED, QualityFlag.UNMAPPED_SYMBOL_GLYPHS, QualityFlag.FONT_WITHOUT_TOUNICODE,
        QualityFlag.TYPE3_FONT, QualityFlag.LOW_FUNCTION_WORD_RATE, QualityFlag.MIXED_TEXT_LAYER,
        QualityFlag.EMBEDDED_TEXT_LAYER, QualityFlag.HIDDEN_TEXT_LAYER, QualityFlag.TEXT_LAYER_VISIBLE_MODE,
        QualityFlag.NATIVE_OCR_DISAGREE, QualityFlag.LANGUAGE_UNCERTAIN, QualityFlag.TRUNCATED,
        QualityFlag.REPETITION)},
    **{f: frozenset({"pages"}) for f in (
        QualityFlag.ROTATED, QualityFlag.SKEW_CORRECTED, QualityFlag.LOW_RESOLUTION_SCAN, QualityFlag.SPREAD_2UP,
        QualityFlag.EMPTY_PAGE, QualityFlag.VECTOR_NO_TEXT, QualityFlag.IMAGE_NO_TEXT,
        QualityFlag.DUPLICATE_PAGE_CANDIDATE, QualityFlag.FOREIGN_WORK_CONTENT, QualityFlag.EPUB_PAGE_ANCHOR_CALIBRE)},
    **{f: _OBJECT_DATASETS for f in (
        QualityFlag.BBOX_APPROX, QualityFlag.CROSS_PAGE_CONTINUATION, QualityFlag.DUPLICATE_DETECTION_DISAMBIGUATED)},
    QualityFlag.SCOPE_INHERITED_FROM_SOURCE: _OBJECT_DATASETS | {"pages", "documents"},
    **{f: frozenset({"figures", "tables"}) for f in (
        QualityFlag.CAPTION_NOT_FOUND, QualityFlag.CAPTION_ASSOCIATION_UNCERTAIN)},
    QualityFlag.FIGURE_TYPE_LOW_CONFIDENCE: frozenset({"figures"}),
    **{f: frozenset({"tables"}) for f in (
        QualityFlag.TABLE_STRUCTURE_UNCERTAIN, QualityFlag.SPANNING_CELLS, QualityFlag.EMPTY_CELLS_RATIO,
        QualityFlag.NUMERIC_PARSE_FAILURES)},
    **{f: frozenset({"formulas"}) for f in (
        QualityFlag.FORMULA_LATEX_UNPARSEABLE, QualityFlag.LATEX_UNBALANCED, QualityFlag.CONVERSION_FAILED)},
    QualityFlag.CITING_WORK_IS_CONTAINER: frozenset({"bibliography_entries", "bibliography_links"}),
    **{f: _SOURCE_DATASETS for f in (
        QualityFlag.LEADING_BYTES_BEFORE_HEADER, QualityFlag.ENCRYPTED_SOURCE, QualityFlag.PDF_PERMISSIONS_RESTRICTED,
        QualityFlag.PAGECOUNT_DIFFERS_FROM_REGISTER_HINT, QualityFlag.DJVU_TITLE_NOT_VERIFIED)},
    QualityFlag.REGISTER_NOTES_NOT_EVIDENCE: frozenset({"sources"}),
}


# ---------------------------------------------------------------- source scope (CP-07)
class SiteScopeMapping(StrEnum):
    EXACT = "EXACT"
    CASE = "CASE"
    SYNONYM = "SYNONYM"
    LOSSY = "LOSSY"
    MULTI = "MULTI"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_A_SCOPE = "NOT_A_SCOPE"


# ---------------------------------------------------------------- works, links, authors, venues (CP-09, H-15)
class WorkType(StrEnum):
    JOURNAL_ARTICLE = "JOURNAL_ARTICLE"
    CONFERENCE_PAPER = "CONFERENCE_PAPER"
    BOOK_CHAPTER = "BOOK_CHAPTER"
    MONOGRAPH = "MONOGRAPH"
    TEXTBOOK = "TEXTBOOK"
    TEACHING_MANUAL = "TEACHING_MANUAL"
    TRAINING_MANUAL = "TRAINING_MANUAL"
    PRACTICE_MANUAL = "PRACTICE_MANUAL"
    DISSERTATION = "DISSERTATION"
    DISSERTATION_ABSTRACT = "DISSERTATION_ABSTRACT"
    THESIS = "THESIS"
    PROCEEDINGS_VOLUME = "PROCEEDINGS_VOLUME"
    JOURNAL_ISSUE = "JOURNAL_ISSUE"
    NORMATIVE_DOCUMENT = "NORMATIVE_DOCUMENT"
    METHODICAL_GUIDANCE = "METHODICAL_GUIDANCE"
    TECHNICAL_REPORT = "TECHNICAL_REPORT"
    INSTITUTIONAL_REPORT = "INSTITUTIONAL_REPORT"
    PRESENTATION = "PRESENTATION"
    PATENT = "PATENT"
    DATASET = "DATASET"
    BIBLIOGRAPHIC_INDEX = "BIBLIOGRAPHIC_INDEX"
    REFERENCE_TABLES = "REFERENCE_TABLES"
    PROJECT_DATA_PACKAGE = "PROJECT_DATA_PACKAGE"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


# container works: bibliography of their chapters is attributed to the volume (H-49 flag CITING_WORK_IS_CONTAINER)
CONTAINER_WORK_TYPES: frozenset[str] = frozenset({WorkType.PROCEEDINGS_VOLUME, WorkType.JOURNAL_ISSUE})


class WorkStatus(StrEnum):
    ACTIVE = "ACTIVE"
    MERGED_INTO = "MERGED_INTO"
    WITHDRAWN = "WITHDRAWN"


class WorkIdentityStatus(StrEnum):
    VERIFIED_IN_FILE = "VERIFIED_IN_FILE"
    CATALOGUE_UNVERIFIED = "CATALOGUE_UNVERIFIED"
    FILENAME_PAGECOUNT_UNVERIFIED = "FILENAME_PAGECOUNT_UNVERIFIED"


class IdentityStatus(StrEnum):
    """Identity of an author or venue node. ``author_id`` forever means a *name-key cluster*, never a person (H-15);
    a person identity will be a separate future ``person_id``."""

    NAME_KEY_ONLY = "NAME_KEY_ONLY"
    ISSN_KEY = "ISSN_KEY"


class MetadataBasis(StrEnum):
    TITLE_PAGE_VERIFIED = "TITLE_PAGE_VERIFIED"
    TEXT_LAYER_VERIFIED = "TEXT_LAYER_VERIFIED"
    EPUB_OPF_METADATA = "EPUB_OPF_METADATA"
    EXTERNAL_CATALOGUE = "EXTERNAL_CATALOGUE"
    HUNT_TABLE = "HUNT_TABLE"
    PHASE1_COVERAGE_MASTER = "PHASE1_COVERAGE_MASTER"
    INTAKE_MANIFEST = "INTAKE_MANIFEST"
    REGISTER_NOTES = "REGISTER_NOTES"
    CATALOGUE_DATA_UNVERIFIED = "CATALOGUE_DATA_UNVERIFIED"
    FILENAME_PAGECOUNT_UNVERIFIED = "FILENAME_PAGECOUNT_UNVERIFIED"
    FILE_EMBEDDED_METADATA = "FILE_EMBEDDED_METADATA"
    CURATED_MANUAL = "CURATED_MANUAL"
    UNKNOWN = "UNKNOWN"


class ExternalIdScheme(StrEnum):
    PWL = "PWL"
    CW = "CW"
    WG = "WG"
    EXT_SRC = "EXT_SRC"
    EXTWEB = "EXTWEB"
    DOI = "DOI"
    ISBN = "ISBN"
    ASIN = "ASIN"
    URN_UUID = "URN_UUID"
    OPENALEX = "OPENALEX"


class ExternalIdRelation(StrEnum):
    SAME_WORK = "SAME_WORK"
    COMPONENT = "COMPONENT"
    OTHER_EDITION = "OTHER_EDITION"
    NOT_SAME = "NOT_SAME"
    CITED_AS = "CITED_AS"


class SourceWorkLinkType(StrEnum):
    """Source → Work. ``FOREIGN_CONTENT`` is never primary and never an INSTANCE_OF edge (H-16)."""

    FULL_COPY = "FULL_COPY"
    PARTIAL_COPY = "PARTIAL_COPY"
    FRONT_MATTER_ONLY = "FRONT_MATTER_ONLY"
    PART = "PART"
    FOREIGN_CONTENT = "FOREIGN_CONTENT"
    CONTAINS_WORK = "CONTAINS_WORK"      # reserved for curated child works of a container; invalid in v0


# link types that make a source an instance of the work (grouping rule CP-09, INSTANCE_OF edges H-16)
INSTANCE_LINK_TYPES: frozenset[str] = frozenset({
    SourceWorkLinkType.FULL_COPY, SourceWorkLinkType.PARTIAL_COPY, SourceWorkLinkType.FRONT_MATTER_ONLY,
    SourceWorkLinkType.PART,
})


class WorkRelationType(StrEnum):
    ABSTRACT_OF = "ABSTRACT_OF"                  # directed: author abstract → dissertation
    EDITION_OF = "EDITION_OF"                    # directed
    TRANSLATION_OF = "TRANSLATION_OF"            # directed
    VOLUME_SET_SIBLING = "VOLUME_SET_SIBLING"    # symmetric: volumes of one set (229 ↔ 230)
    SERIES_SIBLING = "SERIES_SIBLING"            # symmetric: parts of a series (020 ↔ 028)
    COMPANION_OF = "COMPANION_OF"                # symmetric (002 ↔ 201)
    NOT_SAME = "NOT_SAME"                        # symmetric: explicit negative link, blocks any grouping


SYMMETRIC_WORK_RELATIONS: frozenset[str] = frozenset({
    WorkRelationType.VOLUME_SET_SIBLING, WorkRelationType.SERIES_SIBLING, WorkRelationType.COMPANION_OF,
    WorkRelationType.NOT_SAME,
})


class SourceRelationType(StrEnum):
    DERIVED_FROM = "DERIVED_FROM"            # 025 DERIVED_FROM 013
    CONTAINS_COPY_OF = "CONTAINS_COPY_OF"    # 022 CONTAINS_COPY_OF 023 (022 is never unpacked)
    SHARES_PAGES_WITH = "SHARES_PAGES_WITH"  # duplicate pages across issues/articles (with page ranges)


class WorkLinkKind(StrEnum):
    """``link_kind`` column of the curated ``WORK_LINKS.csv``."""

    SOURCE_WORK = "SOURCE_WORK"
    WORK_WORK = "WORK_WORK"
    SOURCE_SOURCE = "SOURCE_SOURCE"


class LinkBasis(StrEnum):
    BOOTSTRAP_SINGLETON = "BOOTSTRAP_SINGLETON"
    REGISTER_NOTES = "REGISTER_NOTES"
    INTAKE_MANIFEST = "INTAKE_MANIFEST"
    HUNT_TABLE = "HUNT_TABLE"
    PHASE1_EVIDENCE = "PHASE1_EVIDENCE"
    REPOSITORY_AUDIT = "REPOSITORY_AUDIT"
    TITLE_PAGE_VERIFIED = "TITLE_PAGE_VERIFIED"
    SHA256_IDENTITY = "SHA256_IDENTITY"
    CURATED_MANUAL = "CURATED_MANUAL"


class AuthorRole(StrEnum):
    AUTHOR = "AUTHOR"
    EDITOR = "EDITOR"
    COMPILER = "COMPILER"
    SUPERVISOR = "SUPERVISOR"
    TRANSLATOR = "TRANSLATOR"
    CORPORATE_AUTHOR = "CORPORATE_AUTHOR"
    UNKNOWN = "UNKNOWN"


class Script(StrEnum):
    CYRL = "CYRL"
    LATN = "LATN"
    MIXED = "MIXED"
    OTHER = "OTHER"


class VenueType(StrEnum):
    JOURNAL = "JOURNAL"
    PROCEEDINGS_SERIES = "PROCEEDINGS_SERIES"
    BOOK_SERIES = "BOOK_SERIES"
    UNKNOWN = "UNKNOWN"


class AvailableBasis(StrEnum):
    """Basis of availability dates. In the canon only UNKNOWN and CURATED; ASSUMED_FROM_PUBLICATION exists only in the
    view ``works_availability`` and in the search index (H-19)."""

    UNKNOWN = "UNKNOWN"
    CURATED = "CURATED"
    ASSUMED_FROM_PUBLICATION = "ASSUMED_FROM_PUBLICATION"


class IngestionBasis(StrEnum):
    INTAKE_MANIFEST = "INTAKE_MANIFEST"
    MIGRATION_SOURCE_TEXT = "MIGRATION_SOURCE_TEXT"
    REGISTER_GIT_HISTORY = "REGISTER_GIT_HISTORY"
    UNKNOWN = "UNKNOWN"


class DatePrecision(StrEnum):
    """Same values as ``vkm_world.core.provenance.DATE_PRECISIONS``."""

    DAY = "day"
    MONTH = "month"
    YEAR = "year"
    DECADE = "decade"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------- storage, commits, validation
class DatasetClass(StrEnum):
    REGISTRY_GLOBAL = "REGISTRY_GLOBAL"      # rebuilt from PRIVATE 00_registry in one registry commit
    HEAD_PER_SOURCE = "HEAD_PER_SOURCE"      # exactly one partition per source in a snapshot (the head commit)
    APPEND_LOG = "APPEND_LOG"                # all files of all runs are history
    DERIVED_VIEW = "DERIVED_VIEW"            # SQL rule over the snapshot (duckdb/sql); never stored in Parquet


class EnvelopeProfile(StrEnum):
    DOC = "DOC"
    REG = "REG"
    LINK = "LINK"
    LOG = "LOG"


class RootKind(StrEnum):
    """Kind of a data root (H-07): producers write STAGING; only CANONICAL has snapshots, CURRENT and projections."""

    STAGING = "STAGING"
    CANONICAL = "CANONICAL"


class CommitScope(StrEnum):
    SOURCE = "SOURCE"
    REGISTRY = "REGISTRY"


class AdmissionStatus(StrEnum):
    ADMITTED = "ADMITTED"
    REJECTED = "REJECTED"


class CheckStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"
    SKIP = "SKIP"


def all_vocabularies() -> dict[str, type[Enum]]:
    """Every closed string vocabulary, including compatibility string Enums."""
    return {name: obj for name, obj in globals().items()
            if isinstance(obj, type) and issubclass(obj, Enum)
            and obj not in {Enum, StrEnum} and issubclass(obj, str)}
