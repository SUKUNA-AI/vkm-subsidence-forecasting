"""Registry of the NAV graph (graph schema ``nav-graph/1.0``): the navigation layer projected into Neo4j.

The NAV layer (``docs/corpus_platform/NAVIGATION_LAYER.md``) is DERIVED navigation — ``AUTO_EXTRACTED_UNREVIEWED``,
never evidence: «discussed together» or «formula (3.2) refers to (2.1)» is not a physical claim. Its projection is a
graph layer of its own (layer label ``NavigationLayer``, DDL prefix ``nav_``, dependency NAVIGATION → DOCUMENT) that
attaches to DOCUMENT nodes by their stable IDs and never touches them (R3: no property or label on another layer's
node). Every NAV node and relationship carries ``layer = 'NAV'``, ``snapshot_id`` (the canonical snapshot the
navigation datasets were built from) and ``rule_version`` (the rule that produced it: the dataset's own rule, or the
projection rule for derived edges — ``covers_page_v1``, ``mentions_top_v1``, ``symbol_of_v1``).

Node labels: ``NavSection``, ``FormulaSymbol``, ``ParameterCandidate``, ``Term``, ``NavTopic``, ``NavTable``
(structured tables, part ``tables``), ``ParameterValue`` (parameter candidates, part ``parameters``),
``ObjectDupGroup`` (repeated figures, tables, formulas, part ``object_duplicates``) and one ``NavMeta`` (the loaded
manifest, status and checks). Terms of the term dictionary (part ``translations``) that the concept graph did not keep
become ``Term`` nodes marked ``dictionary_only``. Relationship types are registry entries (``NavRelType``) with fixed
endpoints; a type used between several label pairs (``NAV_CHILD_OF`` of sections and topics, ``NEAR_FORMULA`` of both
kinds of parameter candidates, ``DUP_MEMBER_OF`` of figures, tables and formulas) has one entry per pair. Cross-layer
edges are owned by NAV whatever their direction (``(:Source)-[:HAS_NAV_SECTION]->``, ``(:Formula)-[:IN_SECTION]->``,
``(:Figure)-[:DUP_MEMBER_OF]->`` and the formula references between DOCUMENT nodes are created and deleted only by the
NAV loader); a DOCUMENT wipe is refused while they exist (``E_CROSS_LAYER_LOSS``): drop the NAV layer first
(``vkm-corpus nav graph-drop``). A dictionary pair, a table naming a property, a repeated figure are navigation hints —
never facts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from vkm_corpus.graph import schema as S
from vkm_corpus.graph.schema import Namespace, q

NAV_GRAPH_SCHEMA_VERSION = "nav-graph/1.1"      # 1.1: tables, parameter values, term dictionary, object duplicates
NAV_LOADER_RULE = "nav_graph_v2"
LAYER_KEY = "NAVIGATION"                          # key of the layer in vkm_corpus.graph.schema.LAYER_LABELS
LAYER_VALUE = "NAV"                               # value of the `layer` property of every NAV node and edge
LAYER_LABEL = S.LAYER_LABELS[LAYER_KEY]           # NavigationLayer
DDL_PREFIX = S.LAYER_DDL_PREFIX[LAYER_KEY]        # nav_
REVIEW_STATUS = "AUTO_EXTRACTED_UNREVIEWED"
META_LABEL = "NavMeta"
META_ID = "NAV_META"

# projection rules of derived edges (recorded as rule_version on the edges and in NavMeta)
RULE_COVERS_PAGE = "covers_page_v1"          # a section covers every page of its [page_start, page_end] range
RULE_MENTIONS = "mentions_top_v1"            # term ↔ section pairs kept: top-k per term or per section by tf-idf
RULE_SYMBOL_OF = "symbol_of_v1"              # term keys (builder morphology) of a symbol definition's leading phrase
RULE_SYMBOL_OF_SURFACE = "symbol_of_surface_v1"   # same, with normalised surface forms (morphology unavailable)
# a property of the parameters vocabulary → the term whose lemma key is the whole key of one of its labels (label_ru,
# label_en, synonyms, in that order; the builder's morphology), for TABULATES and VALUE_OF
RULE_PROPERTY_TERM = "property_term_v1"
RULE_PROPERTY_TERM_SURFACE = "property_term_surface_v1"
MENTIONS_TOP_PER_TERM = 5
MENTIONS_TOP_PER_SECTION = 20
CAPTION_CHARS = 240                          # NavTable.caption: the first characters of the printed caption

NODE_COMMON: tuple[str, ...] = ("id", "layer", "snapshot_id", "rule_version", "review_status", "projection_run_id")
REL_COMMON: tuple[str, ...] = ("layer", "snapshot_id", "rule_version", "projection_run_id")

PARTS: tuple[str, ...] = ("sections", "formulas", "concepts", "links", "topics", "tables", "parameters", "translations",
                          "object_duplicates")
DICTIONARY_TYPES: tuple[str, ...] = ("TRANSLATES_TO", "SYNONYM_OF", "ABBREVIATION_OF")
# the relation of a term_translations row → the registry entry of its edge
DICTIONARY_RELATIONS: dict[str, str] = {"TRANSLATION": "TRANSLATES_TO", "SYNONYM": "SYNONYM_OF",
                                        "ABBREVIATION": "ABBREVIATION_OF"}
# object type of an object_dup_members row → the DOCUMENT label of the member
DUP_MEMBER_LABELS: dict[str, str] = {"FIGURE": "Figure", "TABLE": "Table", "FORMULA": "Formula"}


@dataclass(frozen=True)
class NavNodeType:
    label: str
    key: str                         # the typed id column (also stored as `id`)
    part: str
    properties: tuple[str, ...]      # label-specific whitelist, added to NODE_COMMON
    datasets: tuple[str, ...] = ()   # NAV datasets the rows come from (all needed)

    @property
    def all_properties(self) -> tuple[str, ...]:
        return NODE_COMMON + tuple(p for p in self.properties if p not in NODE_COMMON)


@dataclass(frozen=True)
class NavRelType:
    name: str                        # unique registry key
    type: str                        # relationship type
    start: str                       # endpoint labels (NAV or DOCUMENT)
    end: str
    part: str
    properties: tuple[str, ...] = ()
    key: str | None = None           # merge key besides the endpoints (unique per type)
    datasets: tuple[str, ...] = ()
    symmetric: bool = False          # stored once, from the smaller to the larger id

    @property
    def all_properties(self) -> tuple[str, ...]:
        return REL_COMMON + tuple(p for p in self.properties if p not in REL_COMMON)

    @property
    def cross_layer(self) -> bool:
        return self.start in S.NODE_BY_LABEL or self.end in S.NODE_BY_LABEL

    @property
    def document_ends(self) -> tuple[str, ...]:
        return tuple(x for x in (self.start, self.end) if x in S.NODE_BY_LABEL)


NODE_TYPES: tuple[NavNodeType, ...] = (
    NavNodeType("NavSection", "section_id", "sections",
                ("section_id", "source_id", "work_id", "parent_section_id", "level", "ordinal", "numbering", "title",
                 "title_path", "method", "confidence", "page_start_id", "page_end_id", "page_start_index",
                 "page_end_index", "heading_block_id",
                 # section aggregates of agent T (optional dataset `section_aggregates`)
                 "key_terms", "key_term_ids", "central_page_ids", "n_pages", "n_units", "n_formulas", "n_figures",
                 "n_tables", "n_bib_entries"),
                ("sections",)),
    NavNodeType("FormulaSymbol", "symbol_id", "formulas",
                ("symbol_id", "source_id", "symbol", "symbol_key", "definition", "unit", "definition_key",
                 "n_formulas", "n_definitions"),
                ("formula_symbols",)),
    NavNodeType("ParameterCandidate", "parameter_id", "formulas",
                ("parameter_id", "formula_id", "source_id", "symbol", "symbol_raw", "value_text", "value", "value_min",
                 "value_max", "unit", "block_id", "in_formula", "context_kind"),
                ("formula_parameters",)),
    NavNodeType("Term", "term_id", "concepts",
                ("term_id", "lemma", "lemma_key", "name_keys", "surface_forms", "language", "kind", "n_words",
                 "df_units", "df_sources", "tf", "cvalue", "idf", "seed", "community", "morphology", "same_as_refs",
                 # terms of the term dictionary missing from `terms` (part translations): dictionary_only = true
                 "dictionary_only", "languages", "n_dictionary_pairs"),
                ("terms",)),
    NavNodeType("NavTopic", "topic_id", "topics",
                ("topic_id", "parent_topic_id", "level", "label", "label_terms", "label_term_ids", "n_children",
                 "n_sections", "n_sources", "central_section_ids", "coherence"),
                ("topics",)),
    # structured tables (agent TB, table_structure): the grid of a canonical table; cells stay in the NAV DuckDB
    NavNodeType("NavTable", "nav_table_id", "tables",
                ("nav_table_id", "table_id", "source_id", "page_id", "page_index", "section_id", "table_label",
                 "table_number", "caption", "caption_truncated", "n_rows", "n_cols", "n_cells", "n_filled_cells",
                 "n_numeric_cells", "n_header_rows", "header_method", "n_bands", "n_blocks", "orientation",
                 "recognition_method", "parse_method", "confidence", "structure_ok", "covers_region", "quality_flags",
                 "property_keys", "materials", "caption_property_key"),
                ("table_structure",)),
    # parameter candidates of parameters_v2 (PRM-…): printed values with locators — never recommended values
    NavNodeType("ParameterValue", "candidate_id", "parameters",
                ("candidate_id", "property_key", "property_label", "property_group", "symbol", "material",
                 "material_group", "value_text", "value_min", "value_max", "value_pm", "qualifier", "unit_raw",
                 "unit_canonical", "unit_si", "value_si_min", "value_si_max", "value_si_pm", "scale_hint",
                 "scale_basis", "site_hint", "site_basis", "method", "source_id", "page_id", "page_index", "section_id",
                 "block_id", "table_id", "table_row", "table_col", "formula_id", "char_start", "char_end", "language",
                 "confidence", "flags"),
                ("parameter_candidates",)),
    # groups of repeated figures, tables and formulas (agent U2, object_dup_clusters)
    NavNodeType("ObjectDupGroup", "cluster_id", "object_duplicates",
                ("cluster_id", "object_type", "kind", "match_basis", "n_members", "n_sources", "n_works", "n_groups",
                 "source_ids", "work_ids", "primary_source_id", "primary_object_id", "primary_work_id",
                 "primary_year", "primary_rule", "reference_source_id", "reference_object_id", "label", "n_edges",
                 "min_similarity", "template_share"),
                ("object_dup_clusters",)),
)
META_PROPERTIES: tuple[str, ...] = (
    "id", "layer", "snapshot_id", "rule_version", "review_status", "status", "run_id", "graph_schema_version",
    "loader_version", "rule_versions", "rules_json", "nav_manifest_sha256", "manifest_json", "datasets",
    "counts_json", "accounting_json", "checks_json", "started_at", "finished_at", "heartbeat_at",
    "loading_snapshot_id", "previous_snapshot_id", "doc_build_id", "doc_snapshot_id", "receipt_ref", "error_code")
NODE_BY_LABEL: dict[str, NavNodeType] = {n.label: n for n in NODE_TYPES}

_FORMULA_REF_PROPS = ("ref_id", "kind", "resolution", "number_text", "block_id", "n_mentions")
_DICTIONARY_PROPS = ("pair_id", "relation", "from_language", "to_language", "methods", "n_sources", "n_occurrences",
                     "cosine", "score", "status", "example_page_ids")
_DUP_MEMBER_PROPS = ("object_type", "match", "similarity", "image_distance", "dhash_distance", "transform",
                     "caption_similarity", "cell_containment", "number_containment", "header_similarity",
                     "equation_number", "context", "is_primary", "is_reference", "source_id", "work_id", "year",
                     "page_id", "page_index")
REL_TYPES: tuple[NavRelType, ...] = (
    # sections: the document tree, attached to sources and pages
    NavRelType("NAV_CHILD_OF:NavSection", "NAV_CHILD_OF", "NavSection", "NavSection", "sections",
               datasets=("sections",)),
    NavRelType("HAS_NAV_SECTION", "HAS_NAV_SECTION", "Source", "NavSection", "sections", ("ordinal",),
               datasets=("sections",)),
    NavRelType("COVERS_PAGE", "COVERS_PAGE", "NavSection", "Page", "sections", ("page_index", "deepest"),
               datasets=("sections", "section_pages")),
    # formulas: context, symbols, textual references, parameter candidates
    NavRelType("IN_SECTION", "IN_SECTION", "Formula", "NavSection", "formulas",
               ("equation_number", "kind", "number_method", "n_symbols", "n_defined_symbols", "n_refs_in",
                "n_parameters", "intro_block_id", "where_block_ids"),
               datasets=("formula_context", "sections")),
    NavRelType("DEFINED_FOR", "DEFINED_FOR", "FormulaSymbol", "Formula", "formulas",
               ("role", "in_formula", "n_occurrences", "definition", "unit", "definition_block_id", "match_method"),
               datasets=("formula_symbols",)),
    NavRelType("NAV_REFERS_TO", "NAV_REFERS_TO", "Formula", "Formula", "formulas", _FORMULA_REF_PROPS, key="ref_id",
               datasets=("formula_refs",)),
    NavRelType("NAV_BLOCK_REFERS_TO", "NAV_BLOCK_REFERS_TO", "Block", "Formula", "formulas", _FORMULA_REF_PROPS,
               key="ref_id", datasets=("formula_refs",)),
    NavRelType("NEAR_FORMULA", "NEAR_FORMULA", "ParameterCandidate", "Formula", "formulas",
               ("context_kind", "in_formula"), datasets=("formula_parameters",)),
    # concepts: the term graph
    NavRelType("CO_OCCURS", "CO_OCCURS", "Term", "Term", "concepts",
               ("edge_id", "npmi", "n_units", "n_sources", "examples", "rule"), key="edge_id",
               datasets=("term_edges", "terms"), symmetric=True),
    NavRelType("CONTAINS_TERM", "CONTAINS_TERM", "Term", "Term", "concepts",
               ("edge_id", "weight", "n_units", "n_sources", "rule"), key="edge_id", datasets=("term_edges", "terms")),
    NavRelType("SAME_TERM_AS", "SAME_TERM_AS", "Term", "Term", "concepts",
               ("edge_id", "weight", "n_units", "n_sources", "examples", "rule"), key="edge_id",
               datasets=("term_edges", "terms"), symmetric=True),
    NavRelType("DEFINED_AS", "DEFINED_AS", "Term", "Block", "concepts", ("edge_id", "page_id", "rule"),
               key="edge_id", datasets=("term_edges", "terms")),
    NavRelType("MENTIONED_IN", "MENTIONED_IN", "Term", "NavSection", "concepts",
               ("tf", "tfidf", "n_units", "page_ids", "rank_in_term", "rank_in_section"),
               datasets=("term_mentions", "terms", "sections")),
    # links between parts (derived by the projection)
    NavRelType("SYMBOL_OF", "SYMBOL_OF", "Term", "FormulaSymbol", "links", ("n_formulas", "match", "morphology"),
               datasets=("formula_symbols", "terms")),
    # topics (agent T)
    NavRelType("NAV_CHILD_OF:NavTopic", "NAV_CHILD_OF", "NavTopic", "NavTopic", "topics", datasets=("topics",)),
    NavRelType("IN_TOPIC", "IN_TOPIC", "NavSection", "NavTopic", "topics", ("similarity", "rank", "level"),
               datasets=("topic_members", "topics", "sections")),
    NavRelType("RELATED_TOPIC", "RELATED_TOPIC", "NavTopic", "NavTopic", "topics", ("cosine", "n_links", "level"),
               datasets=("topic_edges", "topics"), symmetric=True),
    # structured tables (agent TB): the grid → its canonical table, its section, the terms of the properties it names
    NavRelType("GRID_OF", "GRID_OF", "NavTable", "Table", "tables", ("parse_method", "structure_ok", "covers_region"),
               datasets=("table_structure",)),
    NavRelType("TABLE_IN_SECTION", "TABLE_IN_SECTION", "NavTable", "NavSection", "tables", ("table_number",),
               datasets=("table_structure", "sections")),
    NavRelType("TABULATES", "TABULATES", "NavTable", "Term", "tables",
               ("property_keys", "n_columns", "n_rows", "in_caption", "match"), datasets=("table_structure", "terms")),
    # parameter candidates (parameters_v2): section, the cell of a structured table, the text block, the formula,
    # the term of the property
    NavRelType("VALUE_IN_SECTION", "VALUE_IN_SECTION", "ParameterValue", "NavSection", "parameters", ("method",),
               datasets=("parameter_candidates", "sections")),
    NavRelType("IN_TABLE", "IN_TABLE", "ParameterValue", "NavTable", "parameters", ("table_row", "table_col"),
               datasets=("parameter_candidates", "table_structure")),
    NavRelType("IN_BLOCK", "IN_BLOCK", "ParameterValue", "Block", "parameters", ("method", "char_start", "char_end"),
               datasets=("parameter_candidates",)),
    NavRelType("NEAR_FORMULA:ParameterValue", "NEAR_FORMULA", "ParameterValue", "Formula", "parameters", ("method",),
               datasets=("parameter_candidates",)),
    NavRelType("VALUE_OF", "VALUE_OF", "ParameterValue", "Term", "parameters", ("property_key", "match"),
               datasets=("parameter_candidates", "terms")),
    # term dictionary (agent TR): one edge per pair (TTR-…), keyed by pair_id; a pair is not a fact
    NavRelType("TRANSLATES_TO", "TRANSLATES_TO", "Term", "Term", "translations", _DICTIONARY_PROPS, key="pair_id",
               datasets=("term_translations", "terms")),
    NavRelType("SYNONYM_OF", "SYNONYM_OF", "Term", "Term", "translations", _DICTIONARY_PROPS, key="pair_id",
               datasets=("term_translations", "terms"), symmetric=True),
    NavRelType("ABBREVIATION_OF", "ABBREVIATION_OF", "Term", "Term", "translations", _DICTIONARY_PROPS,
               key="pair_id", datasets=("term_translations", "terms")),
    # object duplicates (agent U2): members of a group of repeated figures, tables or formulas
    *(NavRelType(f"DUP_MEMBER_OF:{label}", "DUP_MEMBER_OF", label, "ObjectDupGroup", "object_duplicates",
                 _DUP_MEMBER_PROPS, datasets=("object_dup_members", "object_dup_clusters"))
      for label in DUP_MEMBER_LABELS.values()),
)
REL_BY_NAME: dict[str, NavRelType] = {r.name: r for r in REL_TYPES}
REL_TYPE_NAMES: tuple[str, ...] = tuple(sorted({r.type for r in REL_TYPES}))
NAV_LABELS: tuple[str, ...] = tuple(n.label for n in NODE_TYPES) + (META_LABEL, LAYER_LABEL)

# relationship families for path queries (concept_paths ``via``)
FAMILIES: dict[str, tuple[str, ...]] = {
    "concepts": ("CO_OCCURS", "CONTAINS_TERM", "SAME_TERM_AS"),
    "formulas": ("SYMBOL_OF", "DEFINED_FOR", "NAV_REFERS_TO", "IN_SECTION"),
    "sections": ("MENTIONED_IN", "IN_SECTION", "NAV_CHILD_OF"),
    "topics": ("IN_TOPIC", "RELATED_TOPIC", "NAV_CHILD_OF"),
    "dictionary": DICTIONARY_TYPES,
}
PATH_LABELS: tuple[str, ...] = ("Term", "FormulaSymbol", "Formula", "NavSection", "NavTopic")

_ID_PREFIX_LABEL: dict[str, str] = {"SEC-": "NavSection", "TRM-": "Term", "FSY-": "FormulaSymbol",
                                    "FPR-": "ParameterCandidate", "TOP-": "NavTopic", "TBL-": "NavTable",
                                    "PRM-": "ParameterValue", "OCL-": "ObjectDupGroup"}


def label_of_nav_id(node_id: str) -> str | None:
    """The NAV label of an id by its prefix (None: unknown prefix)."""
    for prefix, label in _ID_PREFIX_LABEL.items():
        if node_id.startswith(prefix):
            return label
    return META_LABEL if node_id == META_ID else None


# ---------------------------------------------------------------- DDL (Community: uniqueness constraints + indexes)
_RANGE_INDEXES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Term", ("lemma_key",)),
    ("Term", ("df_units",)),
    ("NavSection", ("source_id", "ordinal")),
    ("FormulaSymbol", ("source_id",)),
    ("ParameterCandidate", ("formula_id",)),
    ("NavTable", ("table_id",)),
    ("NavTable", ("source_id", "page_index")),
    ("ParameterValue", ("property_key",)),
    ("ParameterValue", ("source_id", "page_index")),
    ("ObjectDupGroup", ("object_type", "kind")),
)


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def ddl_items(ns: Namespace = Namespace()) -> list[S.DdlItem]:
    """Idempotent DDL of the NAV layer (R8: names with the ``nav_`` prefix)."""
    items: list[S.DdlItem] = []

    def constraint(name: str, pattern: str, prop: str) -> None:
        full = ns.ddl(f"{DDL_PREFIX}{name}")
        items.append(S.DdlItem(full, "CONSTRAINT",
                               f"CREATE CONSTRAINT {full} IF NOT EXISTS FOR {pattern} REQUIRE {prop} IS UNIQUE"))

    constraint("layer_id_unique", f"(n:{q(ns.label(LAYER_LABEL))})", "n.id")
    for node in NODE_TYPES:
        constraint(f"{_snake(node.label)}_id_unique", f"(n:{q(ns.label(node.label))})", "n.id")
    constraint("nav_meta_id_unique", f"(n:{q(ns.label(META_LABEL))})", "n.id")
    for rel in REL_TYPES:
        if rel.key:
            constraint(f"{rel.type.lower()}_{rel.key}_unique", f"()-[r:{q(ns.rel(rel.type))}]-()", f"r.{rel.key}")
    for label, props in _RANGE_INDEXES:
        full = ns.ddl(f"{DDL_PREFIX}{_snake(label)}_{'_'.join(props)}")
        cols = ", ".join(f"n.{p}" for p in props)
        items.append(S.DdlItem(full, "INDEX",
                               f"CREATE INDEX {full} IF NOT EXISTS FOR (n:{q(ns.label(label))}) ON ({cols})"))
    return items


def ddl_script(ns: Namespace = Namespace()) -> str:
    lines = [f"// NAV graph DDL, graph schema {NAV_GRAPH_SCHEMA_VERSION} (generated by vkm_corpus.graph.nav_schema)"]
    lines += [item.statement + ";" for item in ddl_items(ns)]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- static invariants (check N6, registry tests)
_LABEL_RE = re.compile(r"^[A-Z][A-Za-z0-9]*$")
_REL_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_PROP_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def document_labels() -> frozenset[str]:
    return frozenset(S.NODE_BY_LABEL) | frozenset(v for k, v in S.LAYER_LABELS.items() if k != LAYER_KEY) | {
        S.PROJECTION_RUN_LABEL}


def validate_registry() -> list[str]:
    """Collisions with DOCUMENT/reserved names, endpoints, names and keys (also asserted by tests and check N6)."""
    problems: list[str] = []
    nav_labels = [n.label for n in NODE_TYPES] + [META_LABEL, LAYER_LABEL]
    if len(set(nav_labels)) != len(nav_labels):
        problems.append("duplicate NAV label")
    taken = document_labels() | frozenset(S.RESERVED_FUTURE_LABELS)
    for label in nav_labels:
        if label in taken:
            problems.append(f"NAV label {label} collides with a DOCUMENT, layer, system or reserved label")
        if not _LABEL_RE.match(label):
            problems.append(f"bad label {label}")
    doc_types = {r.type for r in S.REL_TYPES}
    names = [r.name for r in REL_TYPES]
    if len(set(names)) != len(names):
        problems.append("duplicate NAV relationship registry name")
    endpoints: dict[str, set[tuple[str, str]]] = {}
    for rel in REL_TYPES:
        if rel.type in doc_types:
            problems.append(f"NAV relationship type {rel.type} collides with a DOCUMENT type")
        if rel.type in S.FORBIDDEN_GENERIC_TYPES or not _REL_RE.match(rel.type):
            problems.append(f"{rel.type}: generic or invalid relationship type (contract §61)")
        for end in (rel.start, rel.end):
            if end not in NODE_BY_LABEL and end not in S.NODE_BY_LABEL:
                problems.append(f"{rel.name}: unknown endpoint label {end}")
        if not any(end in NODE_BY_LABEL for end in (rel.start, rel.end)) and rel.start not in ("Formula", "Block"):
            problems.append(f"{rel.name}: an edge between DOCUMENT nodes must be a formula reference")
        pair = (rel.start, rel.end)
        if pair in endpoints.setdefault(rel.type, set()):
            problems.append(f"{rel.name}: duplicate endpoints for {rel.type}")
        endpoints[rel.type].add(pair)
        if rel.key and rel.key not in rel.properties:
            problems.append(f"{rel.name}: merge key {rel.key} is not a property")
        for prop in rel.all_properties:
            if not _PROP_RE.match(prop):
                problems.append(f"{rel.name}: bad property {prop}")
    for node in NODE_TYPES:
        for prop in node.all_properties:
            if not _PROP_RE.match(prop):
                problems.append(f"{node.label}: bad property {prop}")
    return problems
