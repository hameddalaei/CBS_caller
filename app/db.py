"""PostgreSQL access layer. All table names come from config.json."""

import json
import logging
import time

import psycopg2
from psycopg2 import extras, sql

log = logging.getLogger("fal.db")

JOB_FIELDS = {"renamed_name", "status", "last_error", "moved_at", "soap_ok_at", "finished_at"}
UNFINISHED_RUN_STATES = ("RUNNING", "PAUSED", "STOPPED", "ERROR")


class Database:
    def __init__(self, cfg):
        c = cfg.section("database")
        self._dsn = {
            "host": c["host"],
            "port": int(c.get("port", 5432)),
            "dbname": c["dbname"],
            "user": c["user"],
            "password": c.get("password", ""),
            "sslmode": c.get("sslmode", "prefer"),
            "connect_timeout": int(c.get("connect_timeout", 10)),
        }
        self._retries = max(1, int(c.get("max_retries", 5)))
        self._retry_delay = float(c.get("retry_delay_seconds", 3))
        self._schema = c.get("schema", "public")
        self._auto_create = bool(c.get("auto_create_tables", True))
        tables = c.get("tables", {})
        self._names = {
            "runs": tables.get("runs", "process_runs"),
            "jobs": tables.get("jobs", "file_jobs"),
            "soap_errors": tables.get("soap_errors", "soap_call_errors"),
            "results": tables.get("results", "result_records"),
            "json_config": tables.get("json_config", "json_config"),
        }
        self.runs = self._tbl("runs")
        self.jobs = self._tbl("jobs")
        self.soap_errors = self._tbl("soap_errors")
        self.results = self._tbl("results")
        self.json_config = self._tbl("json_config")
        jc = c.get("json_config", {})
        self._jc_key = sql.Identifier(jc.get("key_column", "config_key"))
        self._jc_value = sql.Identifier(jc.get("value_column", "config_value"))
        self._conn = None

    def _tbl(self, key):
        return sql.Identifier(self._schema, self._names[key])

    # -- connection handling ----------------------------------------------
    def _connect(self):
        if self._conn is None or self._conn.closed:
            self._conn = psycopg2.connect(**self._dsn)
        return self._conn

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
        self._conn = None

    def _with_retry(self, fn):
        last = None
        for attempt in range(1, self._retries + 1):
            try:
                return fn(self._connect())
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as exc:
                last = exc
                log.warning("Database error (attempt %d/%d): %s", attempt, self._retries, str(exc).strip())
                self.close()
                if attempt < self._retries:
                    time.sleep(self._retry_delay)
        raise last

    def _exec(self, query, params=None, fetch="none"):
        def fn(conn):
            with conn:
                with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
                    cur.execute(query, params)
                    if fetch == "one":
                        return cur.fetchone()
                    if fetch == "all":
                        return cur.fetchall()
                    return cur.rowcount

        return self._with_retry(fn)

    def ping(self):
        self._exec("SELECT 1")

    # -- schema ------------------------------------------------------------
    def ensure_schema(self):
        """Create the application tables if they do not exist (names from config)."""
        if not self._auto_create:
            log.info("auto_create_tables is disabled; expecting tables to exist already")
            return
        n = self._names

        def idx(table_key, suffix):
            return sql.Identifier(f"idx_{n[table_key]}_{suffix}"[:63])

        statements = [
            sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self._schema)),
            sql.SQL(
                """CREATE TABLE IF NOT EXISTS {t} (
                    run_id        SERIAL PRIMARY KEY,
                    state         VARCHAR(20) NOT NULL,
                    source_dir    TEXT,
                    total_files   INTEGER NOT NULL DEFAULT 0,
                    done_files    INTEGER NOT NULL DEFAULT 0,
                    failed_files  INTEGER NOT NULL DEFAULT 0,
                    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
                    finished_at   TIMESTAMPTZ
                )"""
            ).format(t=self.runs),
            sql.SQL(
                """CREATE TABLE IF NOT EXISTS {t} (
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
                )"""
            ).format(t=self.jobs),
            sql.SQL("CREATE INDEX IF NOT EXISTS {i} ON {t} (run_id, status)").format(i=idx("jobs", "run_status"), t=self.jobs),
            sql.SQL(
                """CREATE TABLE IF NOT EXISTS {t} (
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
                )"""
            ).format(t=self.soap_errors),
            sql.SQL("CREATE INDEX IF NOT EXISTS {i} ON {t} (job_id)").format(i=idx("soap_errors", "job"), t=self.soap_errors),
            sql.SQL(
                """CREATE TABLE IF NOT EXISTS {t} (
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
                )"""
            ).format(t=self.results),
            sql.SQL("CREATE INDEX IF NOT EXISTS {i} ON {t} (job_id)").format(i=idx("results", "job"), t=self.results),
        ]

        # Which table each statement belongs to (None = schema). A table that
        # already exists is skipped completely, so the application also works
        # when the tables were created by another database user (e.g. init.sql).
        owners = [None, "runs", "jobs", "jobs", "soap_errors", "soap_errors", "results", "results"]
        assert len(owners) == len(statements)

        def fn(conn):
            with conn:
                with conn.cursor() as cur:
                    existing = set()
                    for key in ("runs", "jobs", "soap_errors", "results"):
                        qualified = f"{self._schema}.{self._names[key]}"
                        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (qualified,))
                        if cur.fetchone()[0]:
                            existing.add(key)
                    for owner, stmt in zip(owners, statements):
                        if owner is None and self._schema == "public":
                            continue
                        if owner in existing:
                            continue
                        cur.execute(stmt)
                    return existing

        existing = self._with_retry(fn)
        log.info("Database schema is ready (%d table(s) already existed)", len(existing))

    # -- runs --------------------------------------------------------------
    def get_unfinished_run(self):
        return self._exec(
            sql.SQL("SELECT * FROM {} WHERE state = ANY(%s) ORDER BY run_id DESC LIMIT 1").format(self.runs),
            (list(UNFINISHED_RUN_STATES),),
            fetch="one",
        )

    def create_run(self, source_dir, file_names):
        def fn(conn):
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        sql.SQL(
                            "INSERT INTO {} (state, source_dir, total_files) VALUES ('RUNNING', %s, %s) RETURNING run_id"
                        ).format(self.runs),
                        (source_dir, len(file_names)),
                    )
                    run_id = cur.fetchone()[0]
                    query = sql.SQL("INSERT INTO {} (run_id, original_name, status) VALUES %s").format(self.jobs).as_string(cur)
                    extras.execute_values(cur, query, [(run_id, name, "pending") for name in file_names], page_size=1000)
                    return run_id

        return self._with_retry(fn)

    def update_run_state(self, run_id, state, finished=False):
        self._exec(
            sql.SQL(
                "UPDATE {} SET state = %s, updated_at = now(), "
                "finished_at = CASE WHEN %s THEN now() ELSE finished_at END WHERE run_id = %s"
            ).format(self.runs),
            (state, finished, run_id),
        )

    def refresh_run_counts(self, run_id):
        row = self._exec(
            sql.SQL(
                "SELECT COUNT(*) FILTER (WHERE status = 'done') AS done, "
                "COUNT(*) FILTER (WHERE status = 'failed') AS failed, "
                "COUNT(*) AS total FROM {} WHERE run_id = %s"
            ).format(self.jobs),
            (run_id,),
            fetch="one",
        )
        self._exec(
            sql.SQL("UPDATE {} SET done_files = %s, failed_files = %s, updated_at = now() WHERE run_id = %s").format(self.runs),
            (row["done"], row["failed"], run_id),
        )
        return {"done": row["done"], "failed": row["failed"], "total": row["total"]}

    # -- jobs --------------------------------------------------------------
    def reset_failed_jobs(self, run_id):
        return self._exec(
            sql.SQL(
                "UPDATE {} SET status = CASE WHEN renamed_name IS NULL THEN 'pending' ELSE 'moved' END, "
                "soap_attempts = 0, last_error = NULL, finished_at = NULL, updated_at = now() "
                "WHERE run_id = %s AND status = 'failed'"
            ).format(self.jobs),
            (run_id,),
        )

    def next_job(self, run_id):
        return self._exec(
            sql.SQL(
                "SELECT * FROM {} WHERE run_id = %s AND status NOT IN ('done', 'failed') ORDER BY job_id LIMIT 1"
            ).format(self.jobs),
            (run_id,),
            fetch="one",
        )

    def update_job(self, job_id, **fields):
        bad = set(fields) - JOB_FIELDS
        if bad:
            raise ValueError(f"Unsupported job fields: {sorted(bad)}")
        assignments = sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(k)) for k in fields)
        query = sql.SQL("UPDATE {} SET {}, updated_at = now() WHERE job_id = %s").format(self.jobs, assignments)
        self._exec(query, list(fields.values()) + [job_id])

    def bump_soap_attempts(self, job_id):
        row = self._exec(
            sql.SQL(
                "UPDATE {} SET soap_attempts = soap_attempts + 1, updated_at = now() "
                "WHERE job_id = %s RETURNING soap_attempts"
            ).format(self.jobs),
            (job_id,),
            fetch="one",
        )
        return row["soap_attempts"]

    # -- SOAP errors -------------------------------------------------------
    def insert_soap_error(self, run_id, job_id, file_name, attempt, error_type, http_status, message, excerpt):
        self._exec(
            sql.SQL(
                "INSERT INTO {} (run_id, job_id, file_name, attempt, error_type, http_status, error_message, response_excerpt) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
            ).format(self.soap_errors),
            (run_id, job_id, file_name, attempt, error_type, http_status, message, excerpt),
        )

    # -- results -----------------------------------------------------------
    def replace_results(self, job_id, rows):
        """Atomically replace all imported result rows of a job (safe to re-run after a crash).

        rows: tuples (run_id, job_id, file_name, result_type, result_file, line_no, raw_line, parsed)
        """
        prepared = [
            (r[0], r[1], r[2], r[3], r[4], r[5], r[6], extras.Json(r[7]) if r[7] is not None else None)
            for r in rows
        ]

        def fn(conn):
            with conn:
                with conn.cursor() as cur:
                    cur.execute(sql.SQL("DELETE FROM {} WHERE job_id = %s").format(self.results), (job_id,))
                    if prepared:
                        query = sql.SQL(
                            "INSERT INTO {} (run_id, job_id, file_name, result_type, result_file, line_no, raw_line, parsed) VALUES %s"
                        ).format(self.results).as_string(cur)
                        extras.execute_values(cur, query, prepared, page_size=1000)

        self._with_retry(fn)
        return len(prepared)

    # -- Json_config (read by the web service; optionally exposed to SOAP params) -
    def read_json_config(self):
        rows = self._exec(
            sql.SQL("SELECT {k} AS k, {v} AS v FROM {t}").format(k=self._jc_key, v=self._jc_value, t=self.json_config),
            fetch="all",
        )
        out = {}
        for row in rows:
            value = row["v"]
            out[str(row["k"])] = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return out
