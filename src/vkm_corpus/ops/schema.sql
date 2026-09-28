-- VKM Corpus Platform v0 — operational control plane (PostgreSQL, EDGE).
-- Operational state only (постановка §16, H-42): losing this database loses no scientific data; every pipeline run also
-- writes processing runs/steps/errors into the canonical Parquet layer. Idempotent: safe to apply repeatedly.

CREATE SCHEMA IF NOT EXISTS ops;

CREATE TABLE IF NOT EXISTS ops.schema_version (
    version     text PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
);

-- mirror of pipeline runs (canonical truth: run markers in $VKM_DATA_ROOT)
CREATE TABLE IF NOT EXISTS ops.ingest_run (
    run_id            text PRIMARY KEY,                 -- RUN-<UTC>-<8hex>
    kind              text NOT NULL,                    -- PIPELINE | PUBLISH | RECONCILE | ACCEPTANCE
    host_role         text NOT NULL,                    -- WORKSTATION | CORE | EDGE
    state             text NOT NULL DEFAULT 'RUNNING',  -- RUNNING | SUCCEEDED | FAILED | CRASHED
    pipeline_version  text,
    code_revision     text,
    params            jsonb NOT NULL DEFAULT '{}'::jsonb,
    summary           jsonb NOT NULL DEFAULT '{}'::jsonb,
    started_at        timestamptz NOT NULL DEFAULT now(),
    finished_at       timestamptz
);

CREATE TABLE IF NOT EXISTS ops.worker (
    worker_id          text PRIMARY KEY,                -- <host_role>:<kind>:<pid>:<start epoch>
    host_role          text NOT NULL,
    kind               text NOT NULL,                   -- PIPELINE | PUBLISHER
    code_revision      text,
    pipeline_version   text,
    state              text NOT NULL DEFAULT 'IDLE',    -- IDLE | BUSY | STOPPED | LOST
    started_at         timestamptz NOT NULL DEFAULT now(),
    last_heartbeat_at  timestamptz NOT NULL DEFAULT now()
);

-- jobs follow the plan-first lifecycle (H-12): the worker is the only planner; a human confirms a plan by its hash
CREATE TABLE IF NOT EXISTS ops.job (
    job_id                 bigserial PRIMARY KEY,
    kind                   text NOT NULL,               -- REPROCESS_SOURCE | REPROCESS_PAGE | PIPELINE_RUN | PUBLISH | RECONCILE
    state                  text NOT NULL,               -- see ops.jobs.TRANSITIONS
    source_id              text,
    page_id                text,
    request                jsonb NOT NULL DEFAULT '{}'::jsonb,
    requested_by           text NOT NULL,
    requested_at           timestamptz NOT NULL DEFAULT now(),
    plan                   jsonb,
    plan_sha256            text,
    planned_by             text,
    planned_at             timestamptz,
    confirmed_plan_sha256  text,
    confirmed_by           text,
    confirmed_at           timestamptz,
    run_id                 text,
    commit_ids             jsonb NOT NULL DEFAULT '[]'::jsonb,
    snapshot_id            text,
    attempts               integer NOT NULL DEFAULT 0,
    max_attempts           integer NOT NULL DEFAULT 3,
    locked_by              text,
    lease_until            timestamptz,
    last_error_id          bigint,
    note                   text,
    updated_at             timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT job_plan_confirm CHECK (confirmed_plan_sha256 IS NULL OR confirmed_plan_sha256 = plan_sha256)
);
CREATE INDEX IF NOT EXISTS job_state_idx ON ops.job (state, job_id);
CREATE INDEX IF NOT EXISTS job_source_idx ON ops.job (source_id);

CREATE TABLE IF NOT EXISTS ops.attempt (
    attempt_id   bigserial PRIMARY KEY,
    job_id       bigint NOT NULL REFERENCES ops.job(job_id) ON DELETE CASCADE,
    worker_id    text NOT NULL,
    phase        text NOT NULL,                         -- PLAN | EXECUTE | PUBLISH
    state        text NOT NULL DEFAULT 'RUNNING',       -- RUNNING | SUCCEEDED | FAILED
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    error_id     bigint,
    log_ref      text
);

CREATE TABLE IF NOT EXISTS ops.heartbeat (
    heartbeat_id  bigserial PRIMARY KEY,
    worker_id     text NOT NULL,
    beat_at       timestamptz NOT NULL DEFAULT now(),
    run_id        text,
    job_id        bigint,
    stage         text,
    detail        jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS heartbeat_worker_idx ON ops.heartbeat (worker_id, beat_at DESC);

-- operational failures of workers and services (processing errors of objects live in the canon)
CREATE TABLE IF NOT EXISTS ops.error (
    error_id    bigserial PRIMARY KEY,
    at          timestamptz NOT NULL DEFAULT now(),
    worker_id   text,
    job_id      bigint,
    run_id      text,
    code        text NOT NULL,
    stage       text,
    tool        text,
    message     text NOT NULL,
    retryable   boolean NOT NULL DEFAULT false,
    source_id   text,
    page_id     text,
    log_ref     text
);

CREATE TABLE IF NOT EXISTS ops.review_task (
    task_id      bigserial PRIMARY KEY,
    created_at   timestamptz NOT NULL DEFAULT now(),
    object_id    text,
    source_id    text,
    page_id      text,
    kind         text NOT NULL,                         -- e.g. CHECK_OCR, CHECK_FIGURE_TYPE, CURATE_WORK_LINK
    reason       text NOT NULL,
    state        text NOT NULL DEFAULT 'OPEN',          -- OPEN | IN_PROGRESS | DONE | WONTFIX
    assigned_to  text,
    resolved_at  timestamptz,
    note         text
);

CREATE TABLE IF NOT EXISTS ops.service_state (
    service     text PRIMARY KEY,
    host_role   text NOT NULL,
    version     text,
    state       text NOT NULL,                          -- UP | DEGRADED | DOWN | BUILDING
    detail      jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

INSERT INTO ops.schema_version (version) VALUES ('ops-0.1.0') ON CONFLICT DO NOTHING;
