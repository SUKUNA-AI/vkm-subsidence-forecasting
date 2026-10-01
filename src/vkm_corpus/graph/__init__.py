"""Neo4j DOCUMENT graph: a rebuildable projection of one CANONICAL snapshot (decisions CP-15, CP-17, H-16, H-17, H-43).

The graph holds identifiers, statuses, structure and ``text_sha256`` only. Texts, captions, LaTeX and bibliography
strings stay in the canonical Parquet layer (read through DuckDB) and in the search index; the graph never becomes a
second source of truth.

Pipeline of ``vkm-corpus graph rebuild --mode wipe`` (the only mode of v0, H-43)::

    CANONICAL root (.vkm_root.json, CURRENT, manifest)      refuses STAGING roots (H-07)
      → in-memory DuckDB over the manifest files + agent D's SQL rules (duckdb/sql, H-17)
      → projection input e_* (field mapping D → E in ``vkm_corpus.graph.canon`` only)
      → preflight (duplicates, dangling references, page gaps, forbidden statuses, relation vocabularies)
      → ProjectionRun(WIPING) → wipe of the layer → UNWIND/MERGE batches → ProjectionRun(VERIFYING)
      → checks C1–C16 + content_digest == expected_digest → ProjectionRun(COMPLETE | FAILED) + receipt

While the latest ``ProjectionRun`` of the layer is not ``COMPLETE`` the API answers graph requests with 503
(``vkm_corpus.graph.runs.graph_state``).

Graph layer rules (general graph contract; only DOCUMENT exists in v0)
----------------------------------------------------------------------

R1. Layers. Every node carries exactly one layer label: ``DocumentLayer``, ``EvidenceLayer``, ``PhysicsLayer``,
    ``RepresentationLayer``, ``ObservationLayer`` or ``ProvenanceLayer``. System nodes (``ProjectionRun``) carry none.
R2. Types. Type labels and relationship types are globally unique; each belongs to exactly one layer through the
    registry ``vkm_corpus.graph.schema``. A meaning is never reused: the document ``Formula`` is not a reviewed
    formula (future ``ReviewedFormula``), a document ``Figure`` is not a physical entity.
R3. Ownership. A layer projector creates, changes and deletes only its own nodes and relationship types. A
    cross-layer edge belongs to the dependent (upper) layer and points from its node to the lower layer's node, e.g.
    ``(:Claim)-[:SUPPORTED_BY]->(:Page)``. Nobody sets properties or labels on another layer's nodes.
R4. One canon per layer. Every layer is rebuilt from its own canonical records; cross-layer references are stored in
    the upper layer's canon as stable document IDs (``page_id`` + bbox + ``raw_content_sha256`` at review), never only
    in the graph.
R5. Dependencies form a DAG, not a chain (H-31)::

        EVIDENCE       → DOCUMENT
        PHYSICS        → EVIDENCE, DOCUMENT
        REPRESENTATION → PHYSICS
        OBSERVATION    → DOCUMENT, EVIDENCE      (real observation datasets never depend on a representation)
        PROVENANCE     → any layer

    A synthetic observation dataset depends on a representation only through a ``SolverRun``; the layer of
    ``SolverRun`` is deliberately not fixed in v0 and must keep the graph acyclic when it is decided. Rebuilding a
    layer requires a coordinated generation replacement of its dependants. ``--cascade`` only drops registered
    derived NAV, and is not a scientific-layer replacement protocol.
R6. The DOCUMENT in-place loader supports only ``wipe`` (H-43). Before DDL, ProjectionRun changes or NAV cascade,
    a read-only preflight refuses any scientific-layer presence (``E_EVIDENCE_DEPENDENCY``), including dual labels
    and evidence properties whose support edges are not yet materialised. Foreign-owned edges between DOCUMENT
    nodes also block the wipe (``E_CROSS_LAYER_LOSS``). A qualified shadow-generation DOCUMENT+EVIDENCE replacement
    is required; this loader implements no bypass for it. Direct wipe and test purge have the same guard.
    This preflight assumes exclusive graph-writer control; it is not a distributed transaction/lock.
R7. No manual writes. Only projectors write; MCP exposes no Cypher. Anything written by hand disappears at the next
    rebuild and is detected earlier by the digest check (``content_digest == expected_digest``).
R8. DDL of a layer is named with its prefix (``doc_``, ``ev_``, ``phys_``, ``rep_``, ``obs_``, ``prov_``; system
    ``sys_``) and is idempotent (``IF NOT EXISTS``).

Kinds of a future ``PhysicalEntity`` (mine block, panel, pillar, shaft...) are the property ``entity_type``, never
labels (H-31): the label namespace is shared by all layers and ``Block`` already means a document text block.

Edge provenance (H-16, H-17): every edge built from one canonical row carries ``canonical_row_id``; every edge built by
a derived rule carries ``rule_version`` (a value of ``vkm_corpus.contracts.vocab.DerivedRule``), and derived rules are
executed only as agent D's SQL files, never re-implemented here.
"""
from __future__ import annotations

__all__: list[str] = []
