"""Preflight of the projection input: nothing is written to Neo4j or OpenSearch if a blocking check fails.

Every check reports the number of violations and examples (never only the first). Codes: ``E_DUPLICATE_ID``,
``E_DANGLING_REFERENCE``, ``E_PAGE_GAP``, ``E_FORBIDDEN_STATUS``, ``E_UNKNOWN_VALUE``, ``E_UNKNOWN_RELATION_TYPE``,
``E_RELATION_CONFLICT``. Informational findings (links that give no ``INSTANCE_OF`` edge, entries without a citing
work) are WARN and go to the receipt.
"""
from __future__ import annotations

from typing import Any

from vkm_corpus.contracts import vocab
from vkm_corpus.graph import schema as S
from vkm_corpus.graph.canon import ProjectionInput, _sql_list
from vkm_corpus.graph.common import CheckResult, ProjectionError, check, failures
from vkm_corpus.graph.rules import ensure_rule_views

_NODE_KEYS: tuple[tuple[str, str], ...] = tuple((n.relation, n.key) for n in S.NODE_TYPES)

# (title, sql returning offending keys)
_DANGLING: tuple[tuple[str, str], ...] = (
    ("pages.source_id → sources", "SELECT page_id FROM e_pages WHERE source_id NOT IN (SELECT source_id FROM e_sources)"),
    *[(f"{rel}.page_id → pages (same source)",
       f"SELECT o.{key} FROM {rel} o LEFT JOIN e_pages p ON p.page_id = o.page_id "
       f"WHERE o.page_id IS NOT NULL AND (p.page_id IS NULL OR p.source_id <> o.source_id)")
      for rel, key in (("e_blocks", "block_id"), ("e_figures", "figure_id"), ("e_tables", "table_id"),
                       ("e_formulas", "formula_id"), ("e_bibliography", "entry_id"))],
    ("source_work_links.source_id → sources",
     "SELECT link_id FROM e_source_work_links WHERE source_id NOT IN (SELECT source_id FROM e_sources)"),
    ("instance links (D work_sources) → sources, works",
     "SELECT link_id FROM e_instance_links WHERE source_id NOT IN (SELECT source_id FROM e_sources) "
     "OR work_id NOT IN (SELECT work_id FROM e_works)"),
    ("foreign content pages → pages, works",
     "SELECT page_id FROM e_foreign_pages WHERE page_id NOT IN (SELECT page_id FROM e_pages) "
     "OR (work_id IS NOT NULL AND work_id NOT IN (SELECT work_id FROM e_works))"),
    ("source_work_links.work_id → works",
     "SELECT link_id FROM e_source_work_links WHERE work_id IS NOT NULL AND curation_status <> 'REJECTED' "
     "AND work_id NOT IN (SELECT work_id FROM e_works)"),
    ("work_relations (curated) endpoints → works",
     "SELECT relation_id FROM e_work_relations WHERE curation_status = 'CURATED' AND "
     "(from_work_id NOT IN (SELECT work_id FROM e_works) OR to_work_id NOT IN (SELECT work_id FROM e_works))"),
    ("source_relations (curated) endpoints → sources",
     "SELECT relation_id FROM e_source_relations WHERE curation_status = 'CURATED' AND "
     "(from_source_id NOT IN (SELECT source_id FROM e_sources) OR to_source_id NOT IN (SELECT source_id FROM e_sources))"),
    ("work_authors → works, authors",
     "SELECT row_id FROM e_work_authors WHERE work_id NOT IN (SELECT work_id FROM e_works) "
     "OR author_id NOT IN (SELECT author_id FROM e_authors)"),
    ("works.venue_id → venues",
     "SELECT work_id FROM e_works WHERE venue_id IS NOT NULL AND venue_id NOT IN (SELECT venue_id FROM e_venues)"),
    ("bibliography.citing_work_id → works",
     "SELECT entry_id FROM e_bibliography WHERE citing_work_id IS NOT NULL "
     "AND citing_work_id NOT IN (SELECT work_id FROM e_works)"),
    ("bibliography_links → entries, works",
     "SELECT link_id FROM e_bibliography_links WHERE accepted AND (entry_id NOT IN (SELECT entry_id FROM e_bibliography) "
     "OR cited_work_id NOT IN (SELECT work_id FROM e_works))"),
    ("cites → works",
     "SELECT citing_work_id || '->' || cited_work_id FROM e_cites WHERE citing_work_id NOT IN (SELECT work_id FROM e_works) "
     "OR cited_work_id NOT IN (SELECT work_id FROM e_works)"),
    ("cites.entry_ids → bibliography",
     "SELECT e FROM (SELECT unnest(entry_ids) AS e FROM e_cites) WHERE e NOT IN (SELECT entry_id FROM e_bibliography)"),
    ("page_sequence → pages",
     "SELECT from_page_id FROM e_page_sequence WHERE from_page_id NOT IN (SELECT page_id FROM e_pages) "
     "OR to_page_id NOT IN (SELECT page_id FROM e_pages)"),
    ("page_duplicates → pages",
     "SELECT page_id FROM e_page_duplicates WHERE page_id NOT IN (SELECT page_id FROM e_pages)"),
    ("figures.caption_block_id → blocks",
     "SELECT figure_id FROM e_figures WHERE caption_block_id IS NOT NULL "
     "AND caption_block_id NOT IN (SELECT block_id FROM e_blocks)"),
)

# status/vocabulary columns: relation → [(column, allowed values)]
_REVIEW = frozenset(v.value for v in vocab.ReviewStatus)
_VOCAB_COLUMNS: tuple[tuple[str, str, frozenset[str], str], ...] = (
    ("e_sources", "lifecycle_status", frozenset(v.value for v in vocab.LifecycleStatus), "E_UNKNOWN_VALUE"),
    ("e_sources", "processing_rollup", frozenset(v.value for v in vocab.SourceProcessingStatus), "E_UNKNOWN_VALUE"),
    ("e_sources", "site_scope_mapping", frozenset(v.value for v in vocab.SiteScopeMapping), "E_UNKNOWN_VALUE"),
    ("e_source_work_links", "link_type", frozenset(v.value for v in vocab.SourceWorkLinkType), "E_UNKNOWN_RELATION_TYPE"),
    ("e_work_relations", "relation", frozenset(S.WORK_RELATION_TYPES), "E_UNKNOWN_RELATION_TYPE"),
    ("e_source_relations", "relation", frozenset(S.SOURCE_RELATION_TYPES), "E_UNKNOWN_RELATION_TYPE"),
    ("e_bibliography_links", "match_status", frozenset(v.value for v in vocab.MatchStatus), "E_UNKNOWN_VALUE"),
    ("e_pages", "page_status", frozenset(v.value for v in vocab.ProcessingStatus), "E_UNKNOWN_VALUE"),
    *[(rel, "origin", frozenset(v.value for v in vocab.Origin), "E_UNKNOWN_VALUE")
      for rel in ("e_pages", "e_blocks", "e_figures", "e_tables", "e_formulas", "e_bibliography")],
    *[(rel, "review_status", _REVIEW, "E_UNKNOWN_VALUE")
      for rel in ("e_sources", "e_works", "e_authors", "e_venues", "e_pages", "e_blocks", "e_figures", "e_tables",
                  "e_formulas", "e_bibliography")],
)
_STATUS_COLUMNS: tuple[tuple[str, str], ...] = tuple(
    (rel, c) for rel, cols in (
        ("e_sources", ("review_status", "processing_rollup", "lifecycle_status")),
        ("e_works", ("review_status", "curation_status", "identity_status")),
        ("e_authors", ("review_status",)), ("e_venues", ("review_status",)),
        ("e_pages", ("review_status", "page_status")), ("e_blocks", ("review_status",)),
        ("e_figures", ("review_status",)), ("e_tables", ("review_status",)), ("e_formulas", ("review_status",)),
        ("e_bibliography", ("review_status",)), ("e_bibliography_links", ("match_status", "curation_status")),
        ("e_source_work_links", ("curation_status",)), ("e_work_relations", ("curation_status",)),
        ("e_source_relations", ("curation_status",)),
    ) for c in cols)


def _keys(inp: ProjectionInput, sql: str, limit: int = 50) -> list[Any]:
    return [next(iter(r.values())) for r in inp.fetch(f"SELECT * FROM ({sql}) LIMIT {int(limit)}")]


def _n(inp: ProjectionInput, sql: str) -> int:
    return int(inp.scalar(f"SELECT count(*) FROM ({sql})"))


def _violations(inp: ProjectionInput, sql: str) -> tuple[int, list[Any]]:
    n = _n(inp, sql)
    return n, (_keys(inp, sql) if n else [])


def run_preflight(inp: ProjectionInput) -> list[CheckResult]:
    ensure_rule_views(inp)
    results: list[CheckResult] = []

    # P01 duplicate keys inside each relation
    dup: list[str] = []
    for rel, key in (*_NODE_KEYS, ("e_source_work_links", "link_id"), ("e_work_relations", "relation_id"),
                     ("e_source_relations", "relation_id"), ("e_work_authors", "row_id"),
                     ("e_bibliography_links", "link_id"), ("e_work_copies", "work_id")):
        dup += [f"{rel}:{k}" for k in _keys(inp, f"SELECT {key} FROM {rel} GROUP BY {key} HAVING count(*) > 1")]
        dup += [f"{rel}:<null key>"] * _n(inp, f"SELECT 1 FROM {rel} WHERE {key} IS NULL")
    results.append(check("P01", "keys unique and non-null in every relation", dup, code="E_DUPLICATE_ID"))

    # P02 node ids unique across labels (layer-wide uniqueness constraint)
    union = " UNION ALL ".join(f"SELECT {key} AS id FROM {rel}" for rel, key in _NODE_KEYS)
    n, ex = _violations(inp, f"SELECT id FROM ({union}) GROUP BY id HAVING count(*) > 1")
    results.append(check("P02", "node ids unique across labels", n if not ex else ex, code="E_DUPLICATE_ID"))

    # P03 dangling references
    dangling: list[str] = []
    for title, sql in _DANGLING:
        dangling += [f"{title}: {k}" for k in _keys(inp, sql, 20)]
    total = sum(_n(inp, sql) for _, sql in _DANGLING)
    results.append(check("P03", "references resolve (FK integrity of the projection input)",
                         dangling if total == len(dangling) else total, code="E_DANGLING_REFERENCE",
                         details={"examples": dangling[:50]} if total else None))

    # P04 page indices contiguous 1..n and equal to page_count; skipped sources have no pages
    gap_sql = """
        WITH pc AS (SELECT source_id, count(*) AS n, min(page_index) AS lo, max(page_index) AS hi,
                           count(DISTINCT page_index) AS nd FROM e_pages GROUP BY source_id)
        SELECT s.source_id FROM e_sources s LEFT JOIN pc ON pc.source_id = s.source_id
        WHERE (pc.n IS NOT NULL AND (pc.lo <> 1 OR pc.hi <> pc.n OR pc.nd <> pc.n))
           OR (s.page_count IS NOT NULL AND coalesce(pc.n, 0) <> s.page_count
               AND s.processing_rollup NOT IN ('PARTIAL', 'FAILED', 'NEEDS_REVIEW', 'UNSUPPORTED'))
           OR (s.processing_rollup = 'SKIPPED_BY_REGISTER' AND coalesce(pc.n, 0) <> 0)"""
    n, ex = _violations(inp, gap_sql)
    results.append(check("P04", "page indices 1..n per source, n = page_count; skipped sources have no pages",
                         ex if n == len(ex) else n, code="E_PAGE_GAP"))

    # P05 forbidden statuses anywhere (task §14, §49)
    forbidden = _sql_list(vocab.FORBIDDEN_STATUS_VALUES)
    bad: list[str] = []
    for rel, column in _STATUS_COLUMNS:
        bad += [f"{rel}.{column}={v}" for v in _keys(inp, f"SELECT DISTINCT {column} FROM {rel} "
                                                           f"WHERE {column} IN {forbidden}")]
    results.append(check("P05", "no FACT/REVIEWED_*/ACCEPTED_*/epistemic status in the document layer", bad,
                         code="E_FORBIDDEN_STATUS"))

    # P06 closed vocabularies
    for i, (rel, column, allowed, code) in enumerate(_VOCAB_COLUMNS):
        values = _keys(inp, f"SELECT DISTINCT {column} FROM {rel} WHERE {column} IS NULL "
                            f"OR {column} NOT IN {_sql_list(allowed)}")
        if column == "review_status" and rel in ("e_authors", "e_venues", "e_works"):
            values += [f"{v} (must be NOT_APPLICABLE)" for v in _keys(
                inp, f"SELECT DISTINCT review_status FROM {rel} WHERE review_status <> 'NOT_APPLICABLE'")]
        if values:
            results.append(check(f"P06.{i:02d}", f"{rel}.{column} in its vocabulary",
                                 [f"{rel}.{column}={v}" for v in values], code=code))
    if not any(r.check_id.startswith("P06") for r in results):
        results.append(check("P06", "closed vocabularies", [], code="E_UNKNOWN_VALUE"))

    # P07 relation conflicts
    conflicts: list[str] = []
    conflicts += [f"several primary links: {k}" for k in _keys(
        inp, "SELECT source_id FROM e_source_work_links WHERE is_primary AND curation_status <> 'REJECTED' "
             "GROUP BY source_id HAVING count(*) > 1")]
    conflicts += [f"primary FOREIGN_CONTENT: {k}" for k in _keys(
        inp, "SELECT link_id FROM e_source_work_links WHERE is_primary AND link_type = 'FOREIGN_CONTENT'")]
    conflicts += [f"NOT_SAME on one work: {k}" for k in _keys(
        inp, "SELECT relation_id FROM e_work_relations WHERE relation = 'NOT_SAME' AND from_work_id = to_work_id")]
    sym = _sql_list(vocab.SYMMETRIC_WORK_RELATIONS)
    conflicts += [f"symmetric pair stored twice: {k}" for k in _keys(
        inp, f"SELECT a.relation_id FROM e_work_relations a JOIN e_work_relations b ON a.relation = b.relation "
             f"AND a.from_work_id = b.to_work_id AND a.to_work_id = b.from_work_id AND a.relation_id < b.relation_id "
             f"WHERE a.relation IN {sym} AND a.curation_status = 'CURATED' AND b.curation_status = 'CURATED'")]
    results.append(check("P07", "no conflicting links (one primary link, NOT_SAME, symmetric pairs)", conflicts,
                         code="E_RELATION_CONFLICT"))

    # P08 (WARN) informational: links without INSTANCE_OF, entries without citing work
    reasons = inp.fetch("SELECT reason, count(*) AS n FROM x_links_not_projected GROUP BY reason ORDER BY reason")
    info = {r["reason"]: r["n"] for r in reasons}
    unexpected = {k: v for k, v in info.items()
                  if k not in ("FOREIGN_CONTENT_PAGE_EDGES", "FOREIGN_CONTENT_UNIDENTIFIED", "SOURCE_NOT_ACTIVE")}
    results.append(check("P08", "source→work links that give no INSTANCE_OF (reasons in details)",
                         sum(unexpected.values()), code="W_LINK_NOT_PROJECTED", warn_only=True,
                         details={"by_reason": info}))

    # P09 (WARN) document-scoped objects without a page (DOCX elements not aligned to a render page, H-50)
    pageless = []
    for rel, key in (("e_blocks", "block_id"), ("e_figures", "figure_id"), ("e_tables", "table_id"),
                     ("e_formulas", "formula_id"), ("e_bibliography", "entry_id")):
        pageless += [f"{rel}:{k}" for k in _keys(inp, f"SELECT {key} FROM {rel} WHERE page_id IS NULL", 20)]
    results.append(check("P09", "objects without a page (projected as nodes without HAS_* edge)", pageless,
                         code="W_OBJECT_WITHOUT_PAGE", warn_only=True))

    # P10 (WARN) copies of a work (CP-25): D's n_sources_active, when D provides it, equals the ACTIVE sources among
    # INSTANCE_OF (the index uses D's value, the graph the edges); until then E counts itself
    provided = int(inp.scalar("SELECT count(*) FROM e_work_copies WHERE n_sources_active IS NOT NULL"))
    mismatch = _keys(inp, f"""
        SELECT c.work_id FROM e_work_copies c
        WHERE c.n_sources_active IS NOT NULL AND c.n_sources_active <> (
            SELECT count(DISTINCT i.source_id) FROM x_instance_links i JOIN e_sources s ON s.source_id = i.source_id
            WHERE i.work_id = c.work_id AND s.lifecycle_status = '{vocab.LifecycleStatus.ACTIVE.value}')""")
    results.append(check("P10", "work_copy_count: D's n_sources_active equals ACTIVE sources among INSTANCE_OF",
                         mismatch, code="W_COPY_COUNT_DIFFERS_FROM_D", warn_only=True,
                         details={"work_copy_count_source": "D.work_copy_counts" if provided else "E (D column absent)"}))

    # P11 (WARN) rule versions carried by D's derived rows equal the registry's expectation
    drift = []
    for rel, table in (("PRECEDES", "e_page_sequence"), ("CITES", "e_cites"), ("REFERENCE_OF", "e_bibliography"),
                       ("DUPLICATE_CANDIDATE_OF", "e_page_duplicates"), ("CARRIES_FOREIGN_CONTENT_OF", "e_foreign_pages"),
                       ("RESOLVES_TO", "e_bibliography_links")):
        drift += [f"{rel}: {v}" for v in _keys(inp, f"SELECT DISTINCT rule_version FROM {table} "
                                                    f"WHERE rule_version IS DISTINCT FROM '{S.RULE_OF_REL[rel]}'")]
    results.append(check("P11", "derived rule versions of D's rows match the graph registry", drift,
                         code="W_RULE_VERSION_DRIFT", warn_only=True))
    return results


def require_preflight(inp: ProjectionInput) -> list[CheckResult]:
    """Run the preflight; raise the first blocking failure's code with all failures in the details."""
    results = run_preflight(inp)
    bad = failures(results)
    if bad:
        raise ProjectionError(bad[0].code or "E_PREFLIGHT", f"preflight failed: {', '.join(r.check_id for r in bad)}",
                              stage="preflight", details={"checks": [r.as_dict() for r in results]})
    return results
