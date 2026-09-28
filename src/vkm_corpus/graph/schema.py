"""Registry of the DOCUMENT graph (graph schema ``doc-graph/1``): labels, relationship types, property whitelists, DDL.

Everything the projector writes is declared here; nothing outside the registry reaches Neo4j:

* node properties are a whitelist per label (IDs, statuses, structure, ``text_sha256``; never texts — CP-17);
* every relationship type has fixed endpoint labels and a provenance kind: ``ROW`` edges carry
  ``canonical_row_id`` (the canonical row they come from), ``RULE`` edges carry ``rule_version`` (agent D's derived
  rule, H-17), ``ROW_RULE`` edges carry both;
* relation vocabularies (work and source relations) are taken verbatim from ``vkm_corpus.contracts.vocab``; a value
  outside the registry is an error, never a generic ``RELATED`` edge (contract §61).

``Namespace`` isolates live tests: the production namespace has no prefix; a test namespace ``VkmTest<8 hex>``
prefixes every label, relationship type and DDL name, so a test run on a shared Neo4j instance can neither see nor
wipe production nodes (Community edition has a single database).
"""
from __future__ import annotations

import re
import secrets
from dataclasses import dataclass

from vkm_corpus.contracts import vocab

GRAPH_SCHEMA_VERSION = "doc-graph/1.0"
LAYER = "DOCUMENT"

# R1: one layer label per layer; only DOCUMENT is populated in v0
LAYER_LABELS: dict[str, str] = {
    "DOCUMENT": "DocumentLayer",
    "EVIDENCE": "EvidenceLayer",
    "PHYSICS": "PhysicsLayer",
    "REPRESENTATION": "RepresentationLayer",
    "OBSERVATION": "ObservationLayer",
    "PROVENANCE": "ProvenanceLayer",
}
# R5 as a DAG (H-31): layer → layers its edges may point to. The place of SolverRun is not fixed in v0.
LAYER_DEPENDENCIES: dict[str, frozenset[str]] = {
    "DOCUMENT": frozenset(),
    "EVIDENCE": frozenset({"DOCUMENT"}),
    "PHYSICS": frozenset({"EVIDENCE", "DOCUMENT"}),
    "REPRESENTATION": frozenset({"PHYSICS"}),
    "OBSERVATION": frozenset({"DOCUMENT", "EVIDENCE"}),
    "PROVENANCE": frozenset({"DOCUMENT", "EVIDENCE", "PHYSICS", "REPRESENTATION", "OBSERVATION"}),
}
# R8: DDL name prefix per layer
LAYER_DDL_PREFIX: dict[str, str] = {
    "DOCUMENT": "doc_", "EVIDENCE": "ev_", "PHYSICS": "phys_", "REPRESENTATION": "rep_", "OBSERVATION": "obs_",
    "PROVENANCE": "prov_",
}
SYSTEM_DDL_PREFIX = "sys_"
PROJECTION_RUN_LABEL = "ProjectionRun"

# Future type labels that are reserved for other layers (R2); none of them is created in v0 (task §27).
RESERVED_FUTURE_LABELS: dict[str, str] = {
    "Claim": "EVIDENCE", "Measurement": "EVIDENCE", "Experiment": "EVIDENCE", "ReviewedFormula": "EVIDENCE",
    "Law": "PHYSICS", "Parameter": "PHYSICS", "PhysicalEntity": "PHYSICS", "PhysicalWorld": "PHYSICS",
    "WorldRepresentation": "REPRESENTATION", "ObservationWorld": "OBSERVATION", "ObservationDataset": "OBSERVATION",
}

_LABEL_RE = re.compile(r"^[A-Z][A-Za-z0-9]*$")
_REL_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_PROP_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_TEST_PREFIX_RE = re.compile(r"^VkmTest[0-9a-f]{8}$")
TEST_LABEL_PREFIX = "VkmTest"

# ---------------------------------------------------------------- nodes
# common to every DOCUMENT node; `projection_run_id` is excluded from digests
NODE_COMMON: tuple[str, ...] = ("id", "schema_version", "review_status", "quality_flags", "origin", "processing_run_id",
                                "projection_run_id")
NODE_REQUIRED_COMMON: tuple[str, ...] = ("id", "schema_version", "review_status", "projection_run_id")
OBJECT_REVIEW = frozenset({vocab.ReviewStatus.AUTO_EXTRACTED_UNREVIEWED.value})
REGISTRY_ENTITY_REVIEW = frozenset({vocab.ReviewStatus.NOT_APPLICABLE.value})     # Work (H-28), Author/Venue (H-25)


@dataclass(frozen=True)
class NodeType:
    label: str
    relation: str                      # projection-input relation (vkm_corpus.graph.canon)
    key: str                           # its ID column
    properties: tuple[str, ...]        # label-specific whitelist, added to NODE_COMMON
    required: tuple[str, ...] = ()     # label-specific non-null properties (C8)
    review_statuses: frozenset[str] | None = None   # allowed review_status values (C9); None = any allowed value

    @property
    def all_properties(self) -> tuple[str, ...]:
        return NODE_COMMON + tuple(p for p in self.properties if p not in NODE_COMMON)

    @property
    def all_required(self) -> tuple[str, ...]:
        return NODE_REQUIRED_COMMON + tuple(p for p in self.required if p not in NODE_REQUIRED_COMMON)


# page_id is optional: the contract allows document-scoped objects (DOCX elements the pinned render could not place
# on a page, scope "<source>:doc"); they have no HAS_* parent (C3 skips them) and trace by source_id.
_DOC_OBJECT_REQUIRED = ("source_id", "origin", "processing_run_id")

NODE_TYPES: tuple[NodeType, ...] = (
    NodeType("Work", "e_works", "work_id",
             ("work_id", "work_type", "publication_year", "doi", "isbn", "languages", "external_ids",
              "identity_status", "curation_status", "is_container", "work_copy_count"),
             ("work_id",), REGISTRY_ENTITY_REVIEW),
    NodeType("Author", "e_authors", "author_id", ("identity_status", "script", "same_as_author_id"), (),
             REGISTRY_ENTITY_REVIEW),
    NodeType("Venue", "e_venues", "venue_id", ("venue_type", "identity_status", "issn", "same_as_venue_id"), (),
             REGISTRY_ENTITY_REVIEW),
    NodeType("Source", "e_sources", "source_id",
             ("source_id", "source_sha256", "lifecycle_status", "lifecycle_reason_code", "file_status",
              "format_detected", "source_class_raw", "priority", "site_scope_raw", "site_scope", "site_scope_mapping",
              "review_status_basis", "processing_rollup", "page_count"),
             ("source_id", "lifecycle_status", "processing_rollup")),
    NodeType("Page", "e_pages", "page_id",
             ("page_id", "source_id", "page_index", "page_kind", "page_class", "printed_page_labels", "is_spread",
              "page_status", "file_text_status", "ocr_status", "primary_text_layer", "primary_text_origin",
              "text_rule", "text_sha256", "render_artifact_id", "dup_group_id"),
             ("page_id", "source_id", "page_index", "page_status", "origin", "processing_run_id"), OBJECT_REVIEW),
    NodeType("Block", "e_blocks", "block_id",
             ("source_id", "page_id", "text_layer", "is_primary_layer", "block_type", "reading_order", "bbox",
              "bbox_space", "language", "region_origin", "text_sha256", "content_sha256"),
             _DOC_OBJECT_REQUIRED, OBJECT_REVIEW),
    NodeType("Figure", "e_figures", "figure_id",
             ("source_id", "page_id", "bbox", "bbox_space", "layout_class", "figure_type", "figure_type_method",
              "region_origin", "is_primary_layer", "image_artifact_id", "embedded_image_artifact_id",
              "vector_artifact_ids", "caption_block_id", "content_sha256"),
             _DOC_OBJECT_REQUIRED, OBJECT_REVIEW),
    NodeType("Table", "e_tables", "table_id",
             ("source_id", "page_id", "bbox", "bbox_space", "n_rows", "n_cols", "raw_format", "recognition_method",
              "region_origin", "is_primary_layer", "image_artifact_id", "continues_object_id", "content_sha256"),
             _DOC_OBJECT_REQUIRED, OBJECT_REVIEW),
    NodeType("Formula", "e_formulas", "formula_id",
             ("source_id", "page_id", "bbox", "bbox_space", "formula_kind", "raw_format", "recognition_method",
              "region_origin", "is_primary_layer", "has_latex", "image_artifact_id", "content_sha256"),
             _DOC_OBJECT_REQUIRED, OBJECT_REVIEW),
    NodeType("BibliographyEntry", "e_bibliography", "entry_id",
             ("source_id", "page_id", "citing_work_id", "citing_work_resolution", "ordinal_in_list", "parsed_doi",
              "parsed_year", "continues_on_page_id", "content_sha256"),
             _DOC_OBJECT_REQUIRED + ("citing_work_resolution",), OBJECT_REVIEW),
)
NODE_BY_LABEL: dict[str, NodeType] = {n.label: n for n in NODE_TYPES}
PAGE_OBJECT_LABELS: tuple[str, ...] = ("Block", "Figure", "Table", "Formula", "BibliographyEntry")

# ---------------------------------------------------------------- relationships
ROW, RULE, ROW_RULE = "ROW", "RULE", "ROW_RULE"
REL_COMMON: tuple[str, ...] = ("projection_run_id",)


@dataclass(frozen=True)
class RelType:
    type: str
    start: str
    end: str
    properties: tuple[str, ...]
    provenance: str                    # ROW | RULE | ROW_RULE
    merge_keys: tuple[str, ...] = ()   # identity properties besides the endpoints (MERGE pattern)
    unique_row_id: bool = False        # relationship uniqueness constraint on canonical_row_id
    symmetric: bool = False            # stored once per unordered pair, from the smaller to the larger id
    family: str = "structure"          # structure | registry_link | bibliography | derived

    @property
    def all_properties(self) -> tuple[str, ...]:
        provenance = {ROW: ("canonical_row_id",), RULE: ("rule_version",),
                      ROW_RULE: ("canonical_row_id", "rule_version")}[self.provenance]
        extra = tuple(p for p in self.properties if p not in provenance)
        return provenance + extra + REL_COMMON

    @property
    def required(self) -> tuple[str, ...]:
        return {ROW: ("canonical_row_id",), RULE: ("rule_version",),
                ROW_RULE: ("canonical_row_id", "rule_version")}[self.provenance] + REL_COMMON


_LINK_PROPS = ("basis", "curation_status")

_BASE_RELS: tuple[RelType, ...] = (
    RelType("INSTANCE_OF", "Source", "Work",
            ("link_type", "page_start", "page_end", "part_label", "printed_range") + _LINK_PROPS, ROW,
            unique_row_id=True, family="registry_link"),
    RelType("AUTHORED_BY", "Work", "Author", ("ordinal", "role", "name_as_listed"), ROW,
            merge_keys=("canonical_row_id",), unique_row_id=True, family="registry_link"),
    RelType("PUBLISHED_IN", "Work", "Venue", ("volume", "issue", "pages_range"), ROW, family="registry_link"),
    RelType("HAS_PAGE", "Source", "Page", (), ROW),
    RelType("PRECEDES", "Page", "Page", (), RULE, family="derived"),
    RelType("HAS_BLOCK", "Page", "Block", (), ROW),
    RelType("HAS_FIGURE", "Page", "Figure", (), ROW),
    RelType("HAS_TABLE", "Page", "Table", (), ROW),
    RelType("HAS_FORMULA", "Page", "Formula", (), ROW),
    RelType("HAS_BIBLIOGRAPHY_ENTRY", "Page", "BibliographyEntry", (), ROW),
    RelType("REFERENCE_OF", "BibliographyEntry", "Work", ("citing_work_resolution", "flags"), ROW_RULE,
            family="bibliography"),
    RelType("RESOLVES_TO", "BibliographyEntry", "Work",
            ("match_method", "match_score", "match_status", "matched_fields"), ROW_RULE,
            merge_keys=("canonical_row_id",), unique_row_id=True, family="bibliography"),
    RelType("CITES", "Work", "Work",
            ("via_entry_ids", "match_methods", "n_citing_entries", "n_citing_sources", "flags"), RULE,
            family="bibliography"),
    RelType("CARRIES_FOREIGN_CONTENT_OF", "Page", "Work", (), ROW_RULE, merge_keys=("canonical_row_id",),
            family="registry_link"),
)
_WORK_RELS: tuple[RelType, ...] = tuple(
    RelType(r.value, "Work", "Work", ("is_symmetric",) + _LINK_PROPS, ROW, unique_row_id=True,
            symmetric=r.value in vocab.SYMMETRIC_WORK_RELATIONS, family="registry_link")
    for r in vocab.WorkRelationType)
_SOURCE_RELS: tuple[RelType, ...] = tuple(
    RelType(r.value, "Source", "Source",
            ("from_page_start", "from_page_end", "to_page_start", "to_page_end") + _LINK_PROPS, ROW,
            unique_row_id=True, family="registry_link")
    for r in vocab.SourceRelationType)
_DUP_RELS: tuple[RelType, ...] = (
    RelType("DUPLICATE_CANDIDATE_OF", "Page", "Page", ("dup_group_id", "basis"), RULE, symmetric=True,
            family="derived"),
)
REL_TYPES: tuple[RelType, ...] = _BASE_RELS + _WORK_RELS + _SOURCE_RELS + _DUP_RELS
REL_BY_TYPE: dict[str, RelType] = {r.type: r for r in REL_TYPES}
WORK_RELATION_TYPES: frozenset[str] = frozenset(r.type for r in _WORK_RELS)
SOURCE_RELATION_TYPES: frozenset[str] = frozenset(r.type for r in _SOURCE_RELS)
HAS_OBJECT_REL: dict[str, str] = {"Block": "HAS_BLOCK", "Figure": "HAS_FIGURE", "Table": "HAS_TABLE",
                                  "Formula": "HAS_FORMULA", "BibliographyEntry": "HAS_BIBLIOGRAPHY_ENTRY"}
FORBIDDEN_GENERIC_TYPES = ("RELATED", "RELATED_TO", "RELATION", "LINKED_TO", "LINK", "HAS_RELATION")

# rule versions of derived edges (values of agent D's DerivedRule, H-17)
RULE_OF_REL: dict[str, str] = {
    "PRECEDES": vocab.DerivedRule.PAGE_SEQUENCE_V1.value,
    "REFERENCE_OF": vocab.DerivedRule.CITING_WORK_V1.value,
    "CARRIES_FOREIGN_CONTENT_OF": vocab.DerivedRule.CITING_WORK_V1.value,
    "RESOLVES_TO": vocab.DerivedRule.BIBLIOGRAPHY_MATCH_V2.value,
    "CITES": vocab.DerivedRule.CITES_V2.value,
    "DUPLICATE_CANDIDATE_OF": vocab.DerivedRule.DUPLICATE_PAGES_V1.value,
}


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


# ---------------------------------------------------------------- namespace (production vs isolated live tests)
@dataclass(frozen=True)
class Namespace:
    """Label/type/DDL-name prefixing. ``Namespace()`` is production; ``Namespace.for_test()`` isolates a test run."""

    prefix: str = ""

    def __post_init__(self) -> None:
        if self.prefix and not _TEST_PREFIX_RE.match(self.prefix):
            raise ValueError("a graph namespace prefix must be empty (production) or 'VkmTest' + 8 hex digits")

    @classmethod
    def for_test(cls, token: str | None = None) -> "Namespace":
        return cls(f"{TEST_LABEL_PREFIX}{token or secrets.token_hex(4)}")

    @property
    def is_test(self) -> bool:
        return bool(self.prefix)

    def label(self, name: str) -> str:
        out = f"{self.prefix}{name}"
        if not _LABEL_RE.match(out):
            raise ValueError(f"invalid label {out!r}")
        return out

    def rel(self, name: str) -> str:
        out = f"{self.prefix.upper()}_{name}" if self.prefix else name
        if not _REL_RE.match(out):
            raise ValueError(f"invalid relationship type {out!r}")
        return out

    def ddl(self, name: str) -> str:
        return f"{self.prefix.lower()}_{name}" if self.prefix else name

    @property
    def layer_label(self) -> str:
        return self.label(LAYER_LABELS[LAYER])

    @property
    def run_label(self) -> str:
        return self.label(PROJECTION_RUN_LABEL)

    def type_labels(self) -> list[str]:
        return [self.label(n.label) for n in NODE_TYPES]

    def rel_types(self) -> list[str]:
        return [self.rel(r.type) for r in REL_TYPES]


def q(identifier: str) -> str:
    """Backquote a validated label or relationship type for Cypher."""
    if not (_LABEL_RE.match(identifier) or _REL_RE.match(identifier)):
        raise ValueError(f"refusing to interpolate identifier {identifier!r}")
    return f"`{identifier}`"


# ---------------------------------------------------------------- DDL (Community: only property uniqueness constraints)
@dataclass(frozen=True)
class DdlItem:
    name: str
    kind: str          # CONSTRAINT | INDEX
    statement: str


_RANGE_INDEXES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Page", ("source_id", "page_index")),
    ("Page", ("dup_group_id",)),
    ("Block", ("source_id",)),
    ("Figure", ("source_id",)),
    ("Table", ("source_id",)),
    ("Formula", ("source_id",)),
    ("BibliographyEntry", ("source_id", "ordinal_in_list")),
    ("BibliographyEntry", ("citing_work_id",)),
    ("Work", ("doi",)),
)


def ddl_items(ns: Namespace = Namespace()) -> list[DdlItem]:
    """Idempotent DDL of the layer (R8). Existence/type/key constraints are Enterprise-only and are replaced by the
    projector's checks (C8)."""
    items: list[DdlItem] = []
    layer = ns.layer_label
    name = ns.ddl(f"{LAYER_DDL_PREFIX[LAYER]}layer_id_unique")
    items.append(DdlItem(name, "CONSTRAINT",
                         f"CREATE CONSTRAINT {name} IF NOT EXISTS FOR (n:{q(layer)}) REQUIRE n.id IS UNIQUE"))
    for node in NODE_TYPES:
        name = ns.ddl(f"{LAYER_DDL_PREFIX[LAYER]}{_snake(node.label)}_id_unique")
        items.append(DdlItem(name, "CONSTRAINT", f"CREATE CONSTRAINT {name} IF NOT EXISTS FOR (n:{q(ns.label(node.label))}) "
                                                 "REQUIRE n.id IS UNIQUE"))
    for rel in REL_TYPES:
        if rel.unique_row_id:
            name = ns.ddl(f"{LAYER_DDL_PREFIX[LAYER]}{rel.type.lower()}_row_unique")
            items.append(DdlItem(name, "CONSTRAINT",
                                 f"CREATE CONSTRAINT {name} IF NOT EXISTS FOR ()-[r:{q(ns.rel(rel.type))}]-() "
                                 "REQUIRE r.canonical_row_id IS UNIQUE"))
    name = ns.ddl(f"{SYSTEM_DDL_PREFIX}projection_run_id_unique")
    items.append(DdlItem(name, "CONSTRAINT",
                         f"CREATE CONSTRAINT {name} IF NOT EXISTS FOR (n:{q(ns.run_label)}) REQUIRE n.id IS UNIQUE"))
    for label, props in _RANGE_INDEXES:
        name = ns.ddl(f"{LAYER_DDL_PREFIX[LAYER]}{_snake(label)}_{'_'.join(props)}")
        cols = ", ".join(f"n.{p}" for p in props)
        items.append(DdlItem(name, "INDEX",
                             f"CREATE INDEX {name} IF NOT EXISTS FOR (n:{q(ns.label(label))}) ON ({cols})"))
    name = ns.ddl(f"{SYSTEM_DDL_PREFIX}projection_run_layer_started")
    items.append(DdlItem(name, "INDEX",
                         f"CREATE INDEX {name} IF NOT EXISTS FOR (n:{q(ns.run_label)}) ON (n.layer, n.started_at)"))
    return items


def ddl_script(ns: Namespace = Namespace()) -> str:
    """The DDL as one ``.cypher`` text (LF line endings; hashed into receipts)."""
    lines = [f"// DOCUMENT graph DDL, graph schema {GRAPH_SCHEMA_VERSION} (generated by vkm_corpus.graph.schema)"]
    lines += [item.statement + ";" for item in ddl_items(ns)]
    return "\n".join(lines) + "\n"


def validate_registry() -> list[str]:
    """Static invariants of the registry (also asserted by tests)."""
    problems: list[str] = []
    labels = [n.label for n in NODE_TYPES]
    if len(set(labels)) != len(labels):
        problems.append("duplicate node label")
    for label in labels:
        if label in LAYER_LABELS.values() or label == PROJECTION_RUN_LABEL or label in RESERVED_FUTURE_LABELS:
            problems.append(f"label {label} collides with a layer/system/reserved label")
    types = [r.type for r in REL_TYPES]
    if len(set(types)) != len(types):
        problems.append("duplicate relationship type")
    for rel in REL_TYPES:
        if rel.start not in NODE_BY_LABEL or rel.end not in NODE_BY_LABEL:
            problems.append(f"{rel.type}: unknown endpoint label")
        if rel.type in FORBIDDEN_GENERIC_TYPES or rel.type.startswith("RELATED"):
            problems.append(f"{rel.type}: generic relationship types are forbidden (contract §61)")
        if rel.provenance in (RULE, ROW_RULE) and rel.type not in RULE_OF_REL:
            problems.append(f"{rel.type}: derived edge without a rule version")
        for prop in rel.all_properties:
            if not _PROP_RE.match(prop):
                problems.append(f"{rel.type}: bad property name {prop}")
    for node in NODE_TYPES:
        for prop in node.all_properties:
            if not _PROP_RE.match(prop):
                problems.append(f"{node.label}: bad property name {prop}")
    return problems
