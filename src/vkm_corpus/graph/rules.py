"""Projection rules of agent E over agent D's rules, shared by the graph and the search index (H-16, H-18, H-49).

Derived facts come from D's SQL (projection input ``e_*``); here only the representation choices are made:

* ``x_instance_links`` — which of D's instance links (``work_sources``: full/partial copy, front matter, part; not
  rejected; with a work) become ``INSTANCE_OF``: the *primary* link (H-16) of every registered source, whatever its
  lifecycle (decision CP-25: that a source is a copy of a work is a register fact; an absent or retired source keeps
  its ``lifecycle_status`` on the node — 013/025/202 → ``VKM-WRK-013`` with ``INSTANCE_OF`` from all three).
  ``FOREIGN_CONTENT`` is never an instance link. ``INSTANCE_OF_REQUIRES_ACTIVE_SOURCE`` keeps the stricter variant
  available for experiments.
* ``x_links_not_projected`` — every other non-rejected Source→Work link with the reason (receipts; never silent).
* ``x_foreign_page_links`` / ``x_page_foreign`` — D's ``foreign_content_pages``: identified foreign works become
  ``(Page)-[:CARRIES_FOREIGN_CONTENT_OF]->(Work)`` and ``foreign_content_work_ids`` of search documents.
* ``x_page_dup`` — ``dup_group_id`` from D's duplicate groups (a page outside any group is its own group).
* ``x_work_meta`` — per work: ``work_copy_count`` = number of ACTIVE sources among ``INSTANCE_OF`` (copies that can
  produce hits, CP-25/H-49) — taken from D's ``work_copy_counts.n_sources_active`` as soon as D provides it, counted
  here until then (preflight P10 compares both) — plus ordered author names/IDs, container flag and availability for
  denormalised search documents.
"""
from __future__ import annotations

from typing import Any

from vkm_corpus.contracts import vocab

FOREIGN = vocab.SourceWorkLinkType.FOREIGN_CONTENT.value
ACTIVE = vocab.LifecycleStatus.ACTIVE.value
REJECTED = vocab.CurationStatus.REJECTED.value
CURATED = vocab.CurationStatus.CURATED.value
INSTANCE_OF_REQUIRES_ACTIVE_SOURCE = False


def rule_views(require_active: bool = INSTANCE_OF_REQUIRES_ACTIVE_SOURCE) -> dict[str, str]:
    active = f"AND s.lifecycle_status = '{ACTIVE}'" if require_active else ""
    return {
        "x_instance_links": f"""
            SELECT i.* FROM e_instance_links i JOIN e_sources s ON s.source_id = i.source_id
            WHERE i.is_primary {active}""",
        "x_links_not_projected": f"""
            SELECT l.link_id, l.source_id, l.work_id, l.link_type, l.is_primary,
                   CASE WHEN l.link_type = '{FOREIGN}' AND l.work_id IS NULL THEN 'FOREIGN_CONTENT_UNIDENTIFIED'
                        WHEN l.link_type = '{FOREIGN}' THEN 'FOREIGN_CONTENT_PAGE_EDGES'
                        WHEN l.work_id IS NULL THEN 'WORK_NULL'
                        WHEN l.link_id NOT IN (SELECT link_id FROM e_instance_links) THEN 'NOT_AN_INSTANCE_LINK'
                        WHEN NOT l.is_primary THEN 'NON_PRIMARY_LINK'
                        WHEN coalesce(s.lifecycle_status, '') <> '{ACTIVE}' THEN 'SOURCE_NOT_ACTIVE'
                        ELSE 'OTHER' END AS reason
            FROM e_source_work_links l LEFT JOIN e_sources s ON s.source_id = l.source_id
            WHERE coalesce(l.curation_status, '') <> '{REJECTED}'
              AND l.link_id NOT IN (SELECT link_id FROM x_instance_links)""",
        "x_foreign_page_links": """
            SELECT page_id, source_id, work_id, link_id, rule_version FROM e_foreign_pages""",
        "x_page_foreign": """
            SELECT page_id,
                   list_sort(list_distinct(list(work_id) FILTER (WHERE work_id IS NOT NULL))) AS foreign_content_work_ids,
                   bool_or(work_id IS NULL) AS has_unidentified_foreign
            FROM x_foreign_page_links GROUP BY page_id""",
        "x_page_dup": """
            SELECT page_id, min(dup_group_id) AS dup_group_id FROM e_page_duplicates GROUP BY page_id""",
        "x_work_authors": """
            SELECT work_id, author_id, min(ordinal) AS ordinal, arg_min(name_as_listed, ordinal) AS name_as_listed
            FROM e_work_authors GROUP BY work_id, author_id""",
        "x_work_meta": f"""
            SELECT w.work_id, w.title, w.publication_year, w.languages, w.is_container, w.available_latest_day,
                   w.available_basis,
                   coalesce(dc.n_sources_active,
                            (SELECT count(DISTINCT i.source_id) FROM x_instance_links i
                             JOIN e_sources s ON s.source_id = i.source_id
                             WHERE i.work_id = w.work_id AND s.lifecycle_status = '{ACTIVE}'), 0)
                     AS work_copy_count,
                   coalesce((SELECT list(a.name_as_listed ORDER BY a.ordinal, a.author_id) FROM x_work_authors a
                             WHERE a.work_id = w.work_id), []::VARCHAR[]) AS authors,
                   coalesce((SELECT list(a.author_id ORDER BY a.ordinal, a.author_id) FROM x_work_authors a
                             WHERE a.work_id = w.work_id), []::VARCHAR[]) AS author_ids
            FROM e_works w LEFT JOIN e_work_copies dc ON dc.work_id = w.work_id""",
    }


RULE_VIEWS = rule_views()


def ensure_rule_views(inp: Any) -> None:
    """Create the ``x_*`` views once per projection input (idempotent; plain views, visible to every cursor)."""
    if getattr(inp, "_rule_views_ready", False):
        return
    for name, sql in RULE_VIEWS.items():
        inp.con.execute(f"CREATE OR REPLACE VIEW {name} AS {sql}")
    inp._rule_views_ready = True
