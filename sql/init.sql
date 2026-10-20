-- Database initialisation script for the fal processor (PostgreSQL).
-- Executed automatically by the postgres container on first start
-- (mounted into /docker-entrypoint-initdb.d). All statements are idempotent.
--
-- NOTE: the application can also create the tables it owns by itself
-- (database.auto_create_tables = true in config.json). If you rename a table
-- in config.json, rename it here (or let the application create it).

-- ---------------------------------------------------------------------------
-- Run-level checkpoint: one row per processing run.
-- state: RUNNING | PAUSED | STOPPED | ERROR | DONE
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS process_runs (
    run_id        SERIAL PRIMARY KEY,
    state         VARCHAR(20) NOT NULL,
    source_dir    TEXT,
    total_files   INTEGER NOT NULL DEFAULT 0,
    done_files    INTEGER NOT NULL DEFAULT 0,
    failed_files  INTEGER NOT NULL DEFAULT 0,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ
);

-- ---------------------------------------------------------------------------
-- File-level checkpoint: one row per file of a run.
-- status: pending -> moved -> soap_done -> done | failed
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS file_jobs (
    job_id         SERIAL PRIMARY KEY,
    run_id         INTEGER NOT NULL,
    original_name  TEXT NOT NULL,
    renamed_name   TEXT,
    status         VARCHAR(20) NOT NULL DEFAULT 'pending',
    soap_attempts  INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    moved_at       TIMESTAMPTZ,
    soap_ok_at     TIMESTAMPTZ,
    finished_at    TIMESTAMPTZ,
    UNIQUE (run_id, original_name)
);
CREATE INDEX IF NOT EXISTS idx_file_jobs_run_status ON file_jobs (run_id, status);

-- ---------------------------------------------------------------------------
-- Every failed web service call (e.g. "file does not exist" while the
-- external SFTP transfer is still running).
-- error_type: NOT_FOUND | FAULT | TRANSPORT_ERROR
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS soap_call_errors (
    id                BIGSERIAL PRIMARY KEY,
    run_id            INTEGER,
    job_id            INTEGER,
    file_name         TEXT NOT NULL,
    attempt           INTEGER NOT NULL,
    error_type        VARCHAR(30) NOT NULL,
    http_status       INTEGER,
    error_message     TEXT,
    response_excerpt  TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_soap_call_errors_job ON soap_call_errors (job_id);

-- ---------------------------------------------------------------------------
-- Imported content of all result files (one row per line).
-- result_type = the "name" of the matching entry in results.files (config.json)
-- parsed      = JSON value when results.delimiter / results.parse_json_lines is used
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS result_records (
    id           BIGSERIAL PRIMARY KEY,
    run_id       INTEGER NOT NULL,
    job_id       INTEGER NOT NULL,
    file_name    TEXT NOT NULL,
    result_type  VARCHAR(100) NOT NULL,
    result_file  TEXT NOT NULL,
    line_no      INTEGER NOT NULL,
    raw_line     TEXT,
    parsed       JSONB,
    imported_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_result_records_job ON result_records (job_id);

-- ---------------------------------------------------------------------------
-- json_config: the table the SOAP web service reads its parameters from.
-- (Owned by the web service side; created here only so the stack works
-- out of the box. Table/column names are configurable in config.json.)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS json_config (
    id            SERIAL PRIMARY KEY,
    config_key    VARCHAR(100) NOT NULL UNIQUE,
    config_value  JSONB NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO json_config (config_key, config_value) VALUES
    ('remote_directory', '"/upload/fal"'),
    ('options',          '{"language": "fa", "encoding": "utf-8"}')
ON CONFLICT (config_key) DO NOTHING;

CREATE TABLE IF NOT EXISTS failed_records (
    id              BIGSERIAL PRIMARY KEY,
    account_id      VARCHAR(64)  NOT NULL,
    operation_type  VARCHAR(32)  NOT NULL,
    segment_info    VARCHAR(128) NOT NULL,
    error_code      VARCHAR(16)  NOT NULL,
    error_message   TEXT         NOT NULL,
    source_file     VARCHAR(256) NOT NULL,
    line_number     INTEGER      NOT NULL,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_failed_record UNIQUE (source_file, line_number)
);

CREATE INDEX IF NOT EXISTS idx_failed_records_account_id
    ON failed_records (account_id);

CREATE INDEX IF NOT EXISTS idx_failed_records_error_code
    ON failed_records (error_code);

CREATE INDEX IF NOT EXISTS idx_failed_records_created_at
    ON failed_records (created_at DESC);