-- VKM corpus DuckDB interface, part 1: DERIVED RULES (H-17). The only definition of citing work, bibliography
-- matches, CITES, page order and duplicate-page candidates; every derived row carries rule_version. The Neo4j and
-- OpenSearch projectors execute these same files. Citation is not agreement; a duplicate page is not independent
-- evidence; nothing here merges works or pages.

-- Source INSTANCE_OF Work (H-16, CP-25): every primary, non-FOREIGN_CONTENT, non-REJECTED link of the curated
-- register, whatever the lifecycle of the source (013 absent by register is still a registered copy of its work;
-- the Source node carries lifecycle_status).
CREATE OR REPLACE VIEW work_sources AS
  SELECT l.object_id AS canonical_row_id, l.source_id, l.work_id, l.link_type, l.is_primary, l.page_start, l.page_end,
         l.part_label, l.printed_range, l.curation_status, l.basis, s.lifecycle_status
  FROM canonical.source_work_links l
  LEFT JOIN canonical.sources s ON s.source_id = l.source_id
  WHERE l.curation_status <> 'REJECTED' AND l.work_id IS NOT NULL AND l.is_primary
    AND l.link_type IN ('FULL_COPY', 'PARTIAL_COPY', 'FRONT_MATTER_ONLY', 'PART');

-- pages that carry another work (unknown work: work_id NULL); projected as Page -> Work, never as INSTANCE_OF
CREATE OR REPLACE VIEW foreign_content_pages AS
  SELECT p.page_id, p.source_id, p.page_index, l.work_id AS foreign_work_id, l.object_id AS canonical_row_id,
         'citing_work_v1' AS rule_version
  FROM canonical.pages p
  JOIN canonical.source_work_links l
    ON l.source_id = p.source_id AND l.link_type = 'FOREIGN_CONTENT' AND l.curation_status <> 'REJECTED'
   AND p.page_index BETWEEN l.page_start AND l.page_end;

-- copies (sources) of each work (H-49: two copies are not two testimonies; CP-25): all registered copies and the
-- copies whose file is available (ACTIVE); work_copy_count = n_sources_active (search hits count available files)
CREATE OR REPLACE VIEW work_copy_counts AS
  SELECT work_id,
         count(DISTINCT source_id) AS n_sources_total,
         count(DISTINCT source_id) FILTER (WHERE lifecycle_status = 'ACTIVE') AS n_sources_active,
         count(DISTINCT source_id) FILTER (WHERE lifecycle_status = 'ACTIVE') AS work_copy_count
  FROM work_sources GROUP BY work_id;

-- every work a page belongs to (ranged links cover only their pages)
CREATE OR REPLACE VIEW work_of_page AS
  SELECT p.page_id, p.source_id, p.page_index, l.work_id, l.link_type, l.is_primary,
         (l.page_start IS NOT NULL OR l.page_end IS NOT NULL) AS has_range, l.object_id AS canonical_row_id
  FROM canonical.pages p
  JOIN canonical.source_work_links l
    ON l.source_id = p.source_id AND l.curation_status <> 'REJECTED'
   AND (l.page_start IS NULL OR p.page_index >= l.page_start)
   AND (l.page_end IS NULL OR p.page_index <= l.page_end);

-- rule citing_work_v1: an entry on a page with FOREIGN_CONTENT never cites on behalf of the host (citing_work_id
-- NULL); a unique candidate work is the citing work; several candidates -> AMBIGUOUS (NULL). DOCX entries without a
-- render page fall back to the unranged links of their source.
CREATE OR REPLACE VIEW bibliography AS
  WITH cand AS (
    SELECT e.object_id AS entry_id, w.work_id, w.link_type
    FROM canonical.bibliography_entries e JOIN work_of_page w ON w.page_id = e.page_id
    UNION ALL
    SELECT e.object_id, l.work_id, l.link_type
    FROM canonical.bibliography_entries e
    JOIN canonical.source_work_links l
      ON l.source_id = e.source_id AND l.curation_status <> 'REJECTED' AND l.page_start IS NULL AND l.page_end IS NULL
    WHERE e.page_id IS NULL
  ), pick AS (
    SELECT entry_id, count(*) AS n_candidate_works,
           CASE WHEN count(*) FILTER (WHERE link_type = 'FOREIGN_CONTENT') > 0 THEN NULL
                WHEN count(DISTINCT work_id) = 1 THEN any_value(work_id) END AS citing_work_id,
           CASE WHEN count(*) FILTER (WHERE link_type = 'FOREIGN_CONTENT') > 0 THEN 'FOREIGN_CONTENT'
                WHEN count(DISTINCT work_id) = 1 THEN 'UNIQUE_LINK'
                ELSE 'AMBIGUOUS' END AS citing_work_resolution
    FROM cand GROUP BY entry_id
  )
  SELECT e.*, p.citing_work_id,
         coalesce(p.citing_work_resolution, 'NO_LINK') AS citing_work_resolution,
         coalesce(p.n_candidate_works, 0) AS n_candidate_works,
         coalesce(w.work_type IN ('PROCEEDINGS_VOLUME', 'JOURNAL_ISSUE'), false) AS citing_work_is_container,
         'citing_work_v1' AS rule_version
  FROM canonical.bibliography_entries e
  LEFT JOIN pick p ON p.entry_id = e.object_id
  LEFT JOIN canonical.works w ON w.work_id = p.citing_work_id;

-- rule bibliography_match_v2 (row shape = contracts.models.BibliographyLinkRow; H-29, CP-41). One row per
-- (entry, work), the strongest method wins:
--   * equal DOI or ISBN-13                                                   -> AUTO_EXACT_ID_MATCH (1.0);
--   * equal normalised title of >= 20 characters and the same year           -> AUTO_STRONG_MATCH (0.95);
--   * title similarity >= 0.90 (1 - Levenshtein / longer length), a shared
--     author surname and the same year                                       -> AUTO_STRONG_MATCH (similarity);
--   * shorter equal titles, similarity 0.85-0.90, or a year off by one        -> CANDIDATE.
-- An entry resolves to at most one work: if its best level holds several works, they all stay CANDIDATE (ambiguous
-- titles of different works are never merged). No curated matches in v0.
CREATE OR REPLACE VIEW bibliography_links AS
  WITH e AS (
    SELECT object_id, parsed_doi, parsed_isbn, parsed_year,
           trim(regexp_replace(replace(lower(coalesce(parsed_title, '')), 'ё', 'е'), '[^\pL\pN]+', ' ', 'g')) AS nt,
           list_distinct(list_filter(list_transform(parsed_authors,
               a -> replace(lower(regexp_extract(a, '(\pL[\pL''’-]+)', 1)), 'ё', 'е')), s -> length(s) > 1)) AS sur
    FROM canonical.bibliography_entries
  ), was AS (
    SELECT work_id,
           list_distinct(list(replace(lower(regexp_extract(name_as_listed, '(\pL[\pL''’-]+)', 1)), 'ё', 'е'))) AS sur
    FROM canonical.work_authors GROUP BY work_id
  ), w AS (
    SELECT w.work_id, w.doi, w.isbn, w.publication_year,
           trim(regexp_replace(replace(lower(coalesce(w.title, '')), 'ё', 'е'), '[^\pL\pN]+', ' ', 'g')) AS nt,
           coalesce(a.sur, []) AS sur
    FROM canonical.works w LEFT JOIN was a ON a.work_id = w.work_id
    WHERE w.status = 'ACTIVE'
  ), pairs AS (
    SELECT e.object_id AS entry_id, w.work_id AS cited_work_id, e.parsed_year = w.publication_year AS same_year,
           1.0 - levenshtein(e.nt, w.nt) / greatest(length(e.nt), length(w.nt)) AS sim
    FROM e JOIN w ON abs(e.parsed_year - w.publication_year) <= 1 AND length(e.nt) >= 10 AND length(w.nt) >= 10
     AND len(list_intersect(e.sur, w.sur)) > 0
  ), m AS (
    SELECT e.object_id AS entry_id, w.work_id AS cited_work_id, 'DOI_EXACT' AS match_method, 1.0 AS match_score,
           'AUTO_EXACT_ID_MATCH' AS match_status, ['doi'] AS matched_fields
    FROM e JOIN w ON e.parsed_doi IS NOT NULL AND w.doi = e.parsed_doi
    UNION ALL
    SELECT e.object_id, w.work_id, 'ISBN_EXACT', 1.0, 'AUTO_EXACT_ID_MATCH', ['isbn']
    FROM e JOIN w ON len(e.parsed_isbn) > 0 AND len(w.isbn) > 0 AND len(list_intersect(e.parsed_isbn, w.isbn)) > 0
    UNION ALL
    SELECT e.object_id, w.work_id, 'TITLE_YEAR', CASE WHEN length(e.nt) >= 20 THEN 0.95 ELSE 0.6 END,
           CASE WHEN length(e.nt) >= 20 THEN 'AUTO_STRONG_MATCH' ELSE 'CANDIDATE' END, ['title', 'year']
    FROM e JOIN w ON e.parsed_year IS NOT NULL AND e.parsed_year = w.publication_year AND length(e.nt) > 0
     AND e.nt = w.nt
    UNION ALL
    SELECT entry_id, cited_work_id, 'TITLE_AUTHOR_YEAR', round(sim, 3),
           CASE WHEN sim >= 0.9 AND same_year THEN 'AUTO_STRONG_MATCH' ELSE 'CANDIDATE' END,
           ['title', 'authors', 'year']
    FROM pairs WHERE sim >= 0.85
  ), best AS (
    SELECT *, CASE match_status WHEN 'AUTO_EXACT_ID_MATCH' THEN 2 WHEN 'AUTO_STRONG_MATCH' THEN 1 ELSE 0 END AS lvl
    FROM m
    QUALIFY row_number() OVER (PARTITION BY entry_id, cited_work_id ORDER BY lvl DESC, match_score DESC,
                                                                        match_method) = 1
  ), top AS (
    SELECT *, max(lvl) OVER (PARTITION BY entry_id) AS top_lvl FROM best
  ), res AS (
    SELECT *, sum(CASE WHEN lvl = top_lvl THEN 1 ELSE 0 END) OVER (PARTITION BY entry_id) AS n_top FROM top
  )
  SELECT '0.1.0' AS schema_version,
         'BML-' || left(sha256('BML-v1|' || r.entry_id || '|' || r.cited_work_id || '|' || r.match_method), 16)
           AS object_id,
         'BIBLIOGRAPHY_LINK' AS object_kind, r.entry_id, b.source_id AS citing_source_id, b.page_id AS citing_page_id,
         b.citing_work_id, b.citing_work_resolution, b.citing_work_is_container, r.cited_work_id, r.match_method,
         r.match_score,
         CASE WHEN r.lvl > 0 AND (r.lvl < r.top_lvl OR r.n_top > 1) THEN 'CANDIDATE' ELSE r.match_status END
           AS match_status,
         r.matched_fields, 'DERIVED' AS origin,
         'AUTO_EXTRACTED_UNREVIEWED' AS review_status, 'bibliography_match_v2' AS rule_version
  FROM res r JOIN bibliography b ON b.object_id = r.entry_id;

-- rule cites_v2: Work CITES Work from accepted matches (exact identifiers and strong title matches, CP-41) with a
-- known citing work; a bibliographic index lists works, it does not cite them. Citation != agreement.
-- n_citing_entries counts entries, n_citing_sources counts files (copies of one work are not independent, H-49).
CREATE OR REPLACE VIEW cites AS
  SELECT l.citing_work_id, l.cited_work_id,
         count(DISTINCT l.entry_id) AS n_citing_entries,
         count(DISTINCT l.citing_source_id) AS n_citing_sources,
         bool_or(l.citing_work_is_container) AS citing_work_is_container,
         list(DISTINCT l.match_method ORDER BY l.match_method) AS match_methods,
         list(DISTINCT l.entry_id ORDER BY l.entry_id) AS entry_ids,
         'cites_v2' AS rule_version
  FROM bibliography_links l
  JOIN canonical.works cw ON cw.work_id = l.citing_work_id
  WHERE l.match_status IN ('AUTO_EXACT_ID_MATCH', 'AUTO_STRONG_MATCH') AND l.citing_work_id <> l.cited_work_id
    AND cw.work_type IS DISTINCT FROM 'BIBLIOGRAPHIC_INDEX'
  GROUP BY l.citing_work_id, l.cited_work_id;

-- rule page_sequence_v1: Page PRECEDES Page inside a source (physical/render/spine order)
CREATE OR REPLACE VIEW page_sequence AS
  SELECT source_id, page_id AS from_page_id,
         lead(page_id) OVER (PARTITION BY source_id ORDER BY page_index) AS to_page_id,
         'page_sequence_v1' AS rule_version
  FROM canonical.pages
  QUALIFY to_page_id IS NOT NULL;

-- rule duplicate_pages_v1: pages of DIFFERENT sources with the same page-text hash (>= 200 characters) form a
-- candidate group; candidates only, never merged (a duplicate page is not independent evidence)
CREATE OR REPLACE VIEW duplicate_page_candidates AS
  WITH groups AS (
    SELECT text_sha256, count(*) AS n_pages, count(DISTINCT source_id) AS n_sources
    FROM canonical.pages
    WHERE text_sha256 IS NOT NULL AND char_count >= 200
    GROUP BY text_sha256
    HAVING count(DISTINCT source_id) > 1
  )
  SELECT 'DUP-' || left(g.text_sha256, 16) AS dup_group_id, p.page_id, p.source_id, p.page_index, g.text_sha256,
         g.n_pages, g.n_sources, 'duplicate_pages_v1' AS rule_version
  FROM groups g JOIN canonical.pages p ON p.text_sha256 = g.text_sha256 AND p.char_count >= 200;
