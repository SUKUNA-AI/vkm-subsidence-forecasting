-- VKM corpus DuckDB interface, part 0: stable views over the materialised canonical tables (schema canonical).
-- The builder (vkm_corpus.duckdb.build) creates canonical.<dataset> strictly from the snapshot manifest; these files
-- are executed in name order by the builder AND by the graph/search projector (H-17): one definition of every rule.
-- "tables" is quoted: it is a keyword.

CREATE OR REPLACE VIEW sources AS SELECT * FROM canonical.sources;
CREATE OR REPLACE VIEW works_all AS SELECT * FROM canonical.works;                    -- incl. MERGED_INTO tombstones
CREATE OR REPLACE VIEW works AS SELECT * FROM canonical.works WHERE status = 'ACTIVE';
CREATE OR REPLACE VIEW documents AS SELECT * FROM canonical.documents;
CREATE OR REPLACE VIEW pages AS SELECT * FROM canonical.pages;
CREATE OR REPLACE VIEW blocks AS SELECT * FROM canonical.blocks;
CREATE OR REPLACE VIEW figures AS SELECT * FROM canonical.figures;
CREATE OR REPLACE VIEW "tables" AS SELECT * FROM canonical."tables";
CREATE OR REPLACE VIEW formulas AS SELECT * FROM canonical.formulas;
CREATE OR REPLACE VIEW bibliography_entries AS SELECT * FROM canonical.bibliography_entries;
CREATE OR REPLACE VIEW source_work_links AS SELECT * FROM canonical.source_work_links;
CREATE OR REPLACE VIEW work_relations AS SELECT * FROM canonical.work_relations WHERE curation_status = 'CURATED';
CREATE OR REPLACE VIEW source_relations AS SELECT * FROM canonical.source_relations WHERE curation_status = 'CURATED';
CREATE OR REPLACE VIEW authors AS SELECT * FROM canonical.authors;
CREATE OR REPLACE VIEW work_authors AS SELECT * FROM canonical.work_authors;
CREATE OR REPLACE VIEW venues AS SELECT * FROM canonical.venues;
CREATE OR REPLACE VIEW processing_steps AS SELECT * FROM canonical.processing_steps;
CREATE OR REPLACE VIEW errors AS SELECT * FROM canonical.errors;

-- one row per blob: the first registration (re-registration of the same content is allowed)
CREATE OR REPLACE VIEW artifacts AS
  SELECT * FROM canonical.artifacts
  QUALIFY row_number() OVER (PARTITION BY artifact_id ORDER BY created_at, created_by_run_id) = 1;

-- one row per run: END wins over START; a run with START only is a visible crash (has_end = false)
CREATE OR REPLACE VIEW processing_runs AS
  SELECT r.*, (count(*) FILTER (WHERE r.record_phase = 'END') OVER (PARTITION BY r.processing_run_id) > 0) AS has_end
  FROM canonical.processing_runs r
  QUALIFY row_number() OVER (PARTITION BY r.processing_run_id ORDER BY (r.record_phase = 'END') DESC) = 1;
