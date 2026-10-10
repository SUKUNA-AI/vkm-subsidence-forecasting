-- VKM corpus DuckDB interface, part 3: object access, provenance trace (§50), rerank text (H-04), availability
-- (H-19). meta.snapshot and meta.commits are created by the builder from the manifest.

CREATE OR REPLACE VIEW all_objects AS
  SELECT object_id, object_kind, source_id, page_id, source_sha256, source_site_scope, source_site_scope_raw,
         source_site_scope_mapping, origin, NULL::VARCHAR AS text_layer, NULL::VARCHAR AS region_origin,
         NULL::BOOLEAN AS is_primary_layer, pipeline_version, processing_run_id, extractor_id, extractor_version,
         extraction_generation, model_id, model_revision, models, config_hash, raw_config_hash, extraction_signature,
         raw_content_sha256, content_sha256, raw_artifact_id, raw_artifacts, created_at, review_status, quality_flags,
         schema_version, NULL::DOUBLE AS bbox_x0, NULL::DOUBLE AS bbox_y0, NULL::DOUBLE AS bbox_x1,
         NULL::DOUBLE AS bbox_y1, 'NONE' AS bbox_space
  FROM canonical.documents
  UNION ALL BY NAME
  SELECT object_id, object_kind, source_id, page_id, source_sha256, source_site_scope, source_site_scope_raw,
         source_site_scope_mapping, origin, primary_text_layer AS text_layer, NULL::VARCHAR AS region_origin,
         NULL::BOOLEAN AS is_primary_layer, pipeline_version, processing_run_id, extractor_id, extractor_version,
         extraction_generation, model_id, model_revision, models, config_hash, raw_config_hash, extraction_signature,
         raw_content_sha256, content_sha256, raw_artifact_id, raw_artifacts, created_at, review_status, quality_flags,
         schema_version, NULL::DOUBLE AS bbox_x0, NULL::DOUBLE AS bbox_y0, NULL::DOUBLE AS bbox_x1,
         NULL::DOUBLE AS bbox_y1, bbox_space
  FROM canonical.pages
  UNION ALL BY NAME
  SELECT object_id, object_kind, source_id, page_id, source_sha256, source_site_scope, source_site_scope_raw,
         source_site_scope_mapping, origin, text_layer, region_origin, is_primary_layer, pipeline_version,
         processing_run_id, extractor_id, extractor_version, extraction_generation, model_id, model_revision, models,
         config_hash, raw_config_hash, extraction_signature, raw_content_sha256, content_sha256, raw_artifact_id,
         raw_artifacts, created_at, review_status, quality_flags, schema_version, bbox_x0, bbox_y0, bbox_x1, bbox_y1,
         bbox_space
  FROM canonical.blocks
  UNION ALL BY NAME
  SELECT object_id, object_kind, source_id, page_id, source_sha256, source_site_scope, source_site_scope_raw,
         source_site_scope_mapping, origin, text_layer, region_origin, NULL::BOOLEAN AS is_primary_layer,
         pipeline_version, processing_run_id, extractor_id, extractor_version, extraction_generation, model_id,
         model_revision, models, config_hash, raw_config_hash, extraction_signature, raw_content_sha256,
         content_sha256, raw_artifact_id, raw_artifacts, created_at, review_status, quality_flags, schema_version,
         bbox_x0, bbox_y0, bbox_x1, bbox_y1, bbox_space
  FROM canonical.figures
  UNION ALL BY NAME
  SELECT object_id, object_kind, source_id, page_id, source_sha256, source_site_scope, source_site_scope_raw,
         source_site_scope_mapping, origin, text_layer, region_origin, is_primary_layer,
         pipeline_version, processing_run_id, extractor_id, extractor_version, extraction_generation, model_id,
         model_revision, models, config_hash, raw_config_hash, extraction_signature, raw_content_sha256,
         content_sha256, raw_artifact_id, raw_artifacts, created_at, review_status, quality_flags, schema_version,
         bbox_x0, bbox_y0, bbox_x1, bbox_y1, bbox_space
  FROM canonical."tables"
  UNION ALL BY NAME
  SELECT object_id, object_kind, source_id, page_id, source_sha256, source_site_scope, source_site_scope_raw,
         source_site_scope_mapping, origin, text_layer, region_origin, is_primary_layer,
         pipeline_version, processing_run_id, extractor_id, extractor_version, extraction_generation, model_id,
         model_revision, models, config_hash, raw_config_hash, extraction_signature, raw_content_sha256,
         content_sha256, raw_artifact_id, raw_artifacts, created_at, review_status, quality_flags, schema_version,
         bbox_x0, bbox_y0, bbox_x1, bbox_y1, bbox_space
  FROM canonical.formulas
  UNION ALL BY NAME
  SELECT object_id, object_kind, source_id, page_id, source_sha256, source_site_scope, source_site_scope_raw,
         source_site_scope_mapping, origin, text_layer, region_origin, NULL::BOOLEAN AS is_primary_layer,
         pipeline_version, processing_run_id, extractor_id, extractor_version, extraction_generation, model_id,
         model_revision, models, config_hash, raw_config_hash, extraction_signature, raw_content_sha256,
         content_sha256, raw_artifact_id, raw_artifacts, created_at, review_status, quality_flags, schema_version,
         bbox_x0, bbox_y0, bbox_x1, bbox_y1, bbox_space
  FROM canonical.bibliography_entries;

-- §50 answers for one document object: what, where is the original, who/what created it, native or OCR, auto or
-- reviewed, pipeline version, model revision, source/page, rebuildable (raw kept), and the snapshot it comes from
CREATE OR REPLACE MACRO provenance_trace(oid) AS TABLE
  SELECT o.object_kind, o.object_id, o.schema_version, o.source_id, o.page_id,
         p.page_index, p.page_kind, p.printed_page_raw, p.page_status,
         o.origin, o.text_layer, o.region_origin, o.is_primary_layer, o.review_status, o.quality_flags,
         o.processing_run_id, r.run_kind, r.status AS run_status, r.has_end AS run_has_end, r.code_revision,
         r.host_role, o.pipeline_version, o.extractor_id, o.extractor_version, o.extraction_generation, o.model_id,
         o.model_revision, o.models, o.config_hash, o.raw_config_hash, o.extraction_signature, o.raw_content_sha256,
         o.content_sha256, o.created_at, o.raw_artifact_id, a.artifact_kind AS raw_artifact_kind,
         a.storage_relpath AS raw_artifact_relpath, a.materialization AS raw_materialization,
         a.retention_class AS raw_retention_class, o.raw_artifacts,
         s.canonical_path AS source_canonical_path, s.lifecycle_status, s.source_sha256 AS register_sha256,
         (o.source_sha256 IS NOT DISTINCT FROM s.source_sha256) AS source_sha256_matches_register,
         o.source_site_scope, o.source_site_scope_raw, o.source_site_scope_mapping,
         c.commit_id, o.content_sha256 || '@' || c.commit_id AS object_version,
         (o.raw_artifact_id IS NOT NULL AND a.artifact_id IS NOT NULL) AS rebuildable_from_raw,
         'CANONICAL' AS record_role,
         (SELECT snapshot_id FROM meta.snapshot) AS snapshot_id
  FROM all_objects o
  LEFT JOIN processing_runs r ON r.processing_run_id = o.processing_run_id
  LEFT JOIN artifacts a ON a.artifact_id = o.raw_artifact_id
  LEFT JOIN canonical.sources s ON s.source_id = o.source_id
  LEFT JOIN canonical.pages p ON p.page_id = o.page_id
  LEFT JOIN meta.commits c ON c.commit_key = o.source_id
  WHERE o.object_id = oid;

-- provenance of registry rows (sources, works, authors, venues, curated links): input file, its hash and row
CREATE OR REPLACE MACRO registry_trace(oid) AS TABLE
  WITH reg AS (
    SELECT object_id, object_kind, origin, processing_run_id, extractor_id, extractor_version, review_status,
           input_ref, input_sha256, input_row, content_sha256, schema_version FROM canonical.sources
    UNION ALL SELECT object_id, object_kind, origin, processing_run_id, extractor_id, extractor_version, review_status,
           input_ref, input_sha256, input_row, content_sha256, schema_version FROM canonical.works
    UNION ALL SELECT object_id, object_kind, origin, processing_run_id, extractor_id, extractor_version, review_status,
           input_ref, input_sha256, input_row, content_sha256, schema_version FROM canonical.authors
    UNION ALL SELECT object_id, object_kind, origin, processing_run_id, extractor_id, extractor_version, review_status,
           input_ref, input_sha256, input_row, content_sha256, schema_version FROM canonical.venues
    UNION ALL SELECT object_id, object_kind, origin, processing_run_id, extractor_id, extractor_version, review_status,
           input_ref, input_sha256, input_row, content_sha256, schema_version FROM canonical.source_work_links
    UNION ALL SELECT object_id, object_kind, origin, processing_run_id, extractor_id, extractor_version, review_status,
           input_ref, input_sha256, input_row, content_sha256, schema_version FROM canonical.work_relations
    UNION ALL SELECT object_id, object_kind, origin, processing_run_id, extractor_id, extractor_version, review_status,
           input_ref, input_sha256, input_row, content_sha256, schema_version FROM canonical.source_relations
    UNION ALL SELECT object_id, object_kind, origin, processing_run_id, extractor_id, extractor_version, review_status,
           input_ref, input_sha256, input_row, content_sha256, schema_version FROM canonical.work_authors
  )
  SELECT reg.*, r.run_kind, r.code_revision, r.host_role, c.commit_id,
         'CANONICAL' AS record_role, (SELECT snapshot_id FROM meta.snapshot) AS snapshot_id
  FROM reg
  LEFT JOIN processing_runs r ON r.processing_run_id = reg.processing_run_id
  LEFT JOIN meta.commits c ON c.commit_key = 'REGISTRY'
  WHERE reg.object_id = oid;

CREATE OR REPLACE MACRO objects_on_page(pid) AS TABLE
  SELECT object_kind, object_id, origin, text_layer, region_origin, is_primary_layer, review_status, quality_flags,
         bbox_x0, bbox_y0, bbox_x1, bbox_y1, bbox_space
  FROM all_objects WHERE page_id = pid AND object_kind <> 'PAGE' ORDER BY object_kind, object_id;

CREATE OR REPLACE MACRO objects_by_source(sid) AS TABLE
  SELECT object_kind, origin, count(*) AS n, count(*) FILTER (WHERE len(quality_flags) > 0) AS n_flagged
  FROM all_objects WHERE source_id = sid GROUP BY ALL ORDER BY object_kind, origin;

-- follow MERGED_INTO tombstones to the surviving work
CREATE OR REPLACE MACRO resolve_work(wid) AS TABLE
  WITH RECURSIVE chain(work_id, status, merged_into_work_id, depth) AS (
    SELECT work_id, status, merged_into_work_id, 0 FROM canonical.works WHERE work_id = wid
    UNION ALL
    SELECT w.work_id, w.status, w.merged_into_work_id, c.depth + 1
    FROM chain c JOIN canonical.works w ON w.work_id = c.merged_into_work_id
    WHERE c.status = 'MERGED_INTO' AND c.depth < 16
  )
  SELECT wid AS requested_work_id, work_id AS resolved_work_id, status, depth FROM chain
  ORDER BY depth DESC LIMIT 1;

-- rule rerank_text_v1 (= vkm_corpus.contracts.text_rules.rerank_text_v1): the only text source for rerankers (E/F/G)
CREATE OR REPLACE VIEW rerank_text AS
  WITH t AS (
    SELECT object_id, object_kind, source_id, page_id, true AS is_primary_layer,
           NULLIF(normalized_text, '') AS text FROM canonical.pages
    UNION ALL
    SELECT object_id, object_kind, source_id, page_id, is_primary_layer, NULLIF(normalized_text, '')
    FROM canonical.blocks
    UNION ALL
    SELECT object_id, object_kind, source_id, page_id, true,
           NULLIF(concat_ws(' ', NULLIF(figure_label, ''), NULLIF(caption_normalized, '')), '')
    FROM canonical.figures
    UNION ALL
    SELECT object_id, object_kind, source_id, page_id, coalesce(is_primary_layer, true),
           NULLIF(concat_ws(chr(10), NULLIF(table_label, ''), NULLIF(caption_normalized, ''),
                            NULLIF(normalized_text, '')), '')
    FROM canonical."tables"
    UNION ALL
    SELECT object_id, object_kind, source_id, page_id, coalesce(is_primary_layer, true),
           NULLIF(concat_ws(' ', NULLIF(equation_label, ''),
                            NULLIF(coalesce(normalized_latex,
                                            CASE WHEN raw_format <> 'IMAGE_ONLY' THEN raw_output END), '')), '')
    FROM canonical.formulas
    UNION ALL
    SELECT object_id, object_kind, source_id, page_id, true, NULLIF(normalized_text, '')
    FROM canonical.bibliography_entries
  )
  SELECT object_id, object_kind, source_id, page_id, is_primary_layer, text, sha256(text) AS text_sha256,
         'rerank_text_v1' AS rule
  FROM t WHERE text IS NOT NULL;

-- H-19: availability for filtering "known at t0". The canon keeps works.available_from curated (NULL = UNKNOWN);
-- the D-03 assumption (end of the publication year) exists only here, labelled ASSUMED_FROM_PUBLICATION.
CREATE OR REPLACE VIEW works_availability AS
  SELECT work_id, publication_year, available_from, available_from_precision, available_from_basis,
         CASE WHEN available_from IS NOT NULL THEN
                CASE available_from_precision
                  WHEN 'day' THEN available_from
                  WHEN 'month' THEN last_day(available_from)
                  WHEN 'decade' THEN make_date(year(available_from) - year(available_from) % 10 + 9, 12, 31)
                  ELSE make_date(year(available_from), 12, 31) END
              WHEN publication_year IS NOT NULL THEN make_date(publication_year, 12, 31)
         END AS available_latest_day,
         CASE WHEN available_from IS NOT NULL THEN available_from_basis
              WHEN publication_year IS NOT NULL THEN 'ASSUMED_FROM_PUBLICATION'
              ELSE 'UNKNOWN' END AS available_basis
  FROM canonical.works WHERE status = 'ACTIVE';
