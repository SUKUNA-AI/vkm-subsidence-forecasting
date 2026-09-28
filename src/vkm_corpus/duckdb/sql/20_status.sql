-- VKM corpus DuckDB interface, part 2: processing status and acceptance counts (task §44, §48; CP-05; H-25).
-- 251 = COMPLETE + PARTIAL + NEEDS_REVIEW + FAILED + UNSUPPORTED + NOT_PROCESSED + SKIPPED_BY_REGISTER.

CREATE OR REPLACE VIEW page_coverage AS
  SELECT source_id, count(*) AS pages_total,
         count(*) FILTER (WHERE page_status = 'NATIVE_OK')        AS pages_native_ok,
         count(*) FILTER (WHERE page_status = 'EMBEDDED_TEXT_OK') AS pages_embedded_text_ok,
         count(*) FILTER (WHERE page_status = 'OCR_OK')           AS pages_ocr_ok,
         count(*) FILTER (WHERE page_status = 'OCR_REQUIRED')     AS pages_ocr_required,
         count(*) FILTER (WHERE page_status = 'PARTIAL')          AS pages_partial,
         count(*) FILTER (WHERE page_status = 'NEEDS_REVIEW')     AS pages_needs_review,
         count(*) FILTER (WHERE page_status = 'UNSUPPORTED')      AS pages_unsupported,
         count(*) FILTER (WHERE page_status = 'FAILED')           AS pages_failed,
         count(*) FILTER (WHERE page_status = 'NOT_PROCESSED')    AS pages_not_processed,
         min(page_index) AS min_page_index, max(page_index) AS max_page_index,
         (min(page_index) = 1 AND max(page_index) = count(*) AND count(DISTINCT page_index) = count(*)) AS contiguous
  FROM canonical.pages GROUP BY source_id;

-- latest attempt of every (source, page, stage) next to the committed page status: a failed retry never hides a good
-- committed result, and a failure is never silent
CREATE OR REPLACE VIEW processing_status AS
  SELECT st.source_id, st.page_id, st.page_index, st.stage, st.status AS last_attempt_status,
         st.outcome AS last_attempt_outcome, st.reason_code, st.processing_run_id AS last_attempt_run_id,
         st.finished_at AS last_attempt_at, st.attempt, st.stage_signature, p.page_status AS committed_page_status
  FROM canonical.processing_steps st
  LEFT JOIN canonical.pages p ON p.page_id = st.page_id
  QUALIFY row_number() OVER (PARTITION BY coalesce(st.source_id, ''), coalesce(st.page_id, ''), st.stage
                             ORDER BY st.finished_at DESC, st.attempt DESC, st.step_id DESC) = 1;

CREATE OR REPLACE VIEW source_status_summary AS
  WITH obj AS (
    SELECT source_id,
           count(*) FILTER (WHERE object_kind = 'BLOCK')              AS n_blocks,
           count(*) FILTER (WHERE object_kind = 'FIGURE')             AS n_figures,
           count(*) FILTER (WHERE object_kind = 'TABLE')              AS n_tables,
           count(*) FILTER (WHERE object_kind = 'FORMULA')            AS n_formulas,
           count(*) FILTER (WHERE object_kind = 'BIBLIOGRAPHY_ENTRY') AS n_bibliography_entries
    FROM (SELECT source_id, object_kind FROM canonical.blocks
          UNION ALL SELECT source_id, object_kind FROM canonical.figures
          UNION ALL SELECT source_id, object_kind FROM canonical."tables"
          UNION ALL SELECT source_id, object_kind FROM canonical.formulas
          UNION ALL SELECT source_id, object_kind FROM canonical.bibliography_entries)
    GROUP BY source_id
  ), err AS (
    SELECT source_id, count(*) AS n_errors, count(*) FILTER (WHERE retryable) AS n_errors_retryable
    FROM canonical.errors WHERE source_id IS NOT NULL GROUP BY source_id
  )
  SELECT s.source_id, s.file_status, s.lifecycle_status, s.register_skip_reason, s.format_detected, s.review_status,
         s.site_scope_mapping, d.document_class, d.page_count, d.processing_status AS document_processing_status,
         coalesce(pc.pages_total, 0) AS pages_total, coalesce(pc.pages_native_ok, 0) AS pages_native_ok,
         coalesce(pc.pages_embedded_text_ok, 0) AS pages_embedded_text_ok, coalesce(pc.pages_ocr_ok, 0) AS pages_ocr_ok,
         coalesce(pc.pages_failed, 0) AS pages_failed, coalesce(pc.pages_needs_review, 0) AS pages_needs_review,
         coalesce(pc.pages_not_processed, 0) AS pages_not_processed,
         coalesce(o.n_blocks, 0) AS n_blocks, coalesce(o.n_figures, 0) AS n_figures, coalesce(o.n_tables, 0) AS n_tables,
         coalesce(o.n_formulas, 0) AS n_formulas, coalesce(o.n_bibliography_entries, 0) AS n_bibliography_entries,
         coalesce(e.n_errors, 0) AS n_errors, coalesce(e.n_errors_retryable, 0) AS n_errors_retryable,
         CASE
           WHEN s.lifecycle_status <> 'ACTIVE' THEN 'SKIPPED_BY_REGISTER'
           WHEN s.file_status <> 'PRESENT_VERIFIED' THEN 'FAILED'
           WHEN d.source_id IS NULL THEN 'NOT_PROCESSED'
           WHEN d.processing_status IN ('FAILED', 'UNSUPPORTED') THEN d.processing_status
           WHEN coalesce(pc.pages_total, 0) <> d.page_count OR NOT coalesce(pc.contiguous, false) THEN 'PARTIAL'
           WHEN pc.pages_failed + pc.pages_unsupported + pc.pages_not_processed + pc.pages_ocr_required
                + pc.pages_partial > 0 THEN 'PARTIAL'
           WHEN pc.pages_needs_review > 0 THEN 'NEEDS_REVIEW'
           ELSE 'COMPLETE' END AS source_rollup
  FROM canonical.sources s
  LEFT JOIN canonical.documents d ON d.source_id = s.source_id
  LEFT JOIN page_coverage pc ON pc.source_id = s.source_id
  LEFT JOIN obj o ON o.source_id = s.source_id
  LEFT JOIN err e ON e.source_id = s.source_id;

-- acceptance counts (task §48): every source falls in exactly one roll-up class
CREATE OR REPLACE VIEW corpus_counts AS
  SELECT count(*) AS sources_total,
         count(*) FILTER (WHERE source_rollup = 'COMPLETE')            AS sources_complete,
         count(*) FILTER (WHERE source_rollup = 'PARTIAL')             AS sources_partial,
         count(*) FILTER (WHERE source_rollup = 'NEEDS_REVIEW')        AS sources_needs_review,
         count(*) FILTER (WHERE source_rollup = 'FAILED')              AS sources_failed,
         count(*) FILTER (WHERE source_rollup = 'UNSUPPORTED')         AS sources_unsupported,
         count(*) FILTER (WHERE source_rollup = 'NOT_PROCESSED')       AS sources_not_processed,
         count(*) FILTER (WHERE source_rollup = 'SKIPPED_BY_REGISTER') AS sources_skipped_by_register,
         coalesce(sum(pages_total), 0) AS pages_total,
         coalesce(sum(pages_native_ok), 0) AS pages_native,
         coalesce(sum(pages_embedded_text_ok), 0) AS pages_embedded_text,
         coalesce(sum(pages_ocr_ok), 0) AS pages_ocr,
         coalesce(sum(pages_failed), 0) AS pages_failed,
         coalesce(sum(n_blocks), 0) AS blocks,
         coalesce(sum(n_figures), 0) AS figures,
         coalesce(sum(n_tables), 0) AS "tables",
         coalesce(sum(n_formulas), 0) AS formulas,
         coalesce(sum(n_bibliography_entries), 0) AS bibliography_entries,
         (count(*) = count(*) FILTER (WHERE source_rollup IN ('COMPLETE', 'PARTIAL', 'NEEDS_REVIEW', 'FAILED',
                                      'UNSUPPORTED', 'NOT_PROCESSED', 'SKIPPED_BY_REGISTER'))) AS rollup_closed
  FROM source_status_summary;
