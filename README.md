# Fal Processor

A modular Python service that feeds hundreds of text files, one at a time, through an
external SOAP-based processing pipeline and imports the results into PostgreSQL.
Everything variable lives in `config.json`; the whole run is controlled and observed
through Redis channels.

## How it works

For every file in the source directory the service repeats this cycle:

```
source dir ──move + rename──▶ staging dir (path 1)
                                   │   (another program SFTPs the file to a remote server, ~1 min)
                                   ▼
              wait `initial_delay_seconds`, then call the SOAP web service with the new file name
                 ├─ "file does not exist" fault ─▶ store the error in DB + log, wait, retry
                 └─ success
                                   ▼
              wait for the result files in the results dir (path 2)
                                   ▼
              import ALL result files into PostgreSQL, write them to the log, evaluate success
                                   ▼
              publish progress to Redis ─▶ next file
```

The SOAP web service itself reads its parameters from the `json_config` table in the same
database; this service only passes the file name (optionally it can also forward the
`json_config` key/values, see `soap.extra_params_from_db`).

## Project layout

```
fal_processor/
├── main.py                  entry point (wiring, signals)
├── config.json              ALL settings
├── app/
│   ├── config.py            loading, ${ENV:-default} expansion, validation
│   ├── controller.py        start / stop / pause / resume state machine
│   ├── orchestrator.py      the workflow described above
│   ├── file_manager.py      scan, move + rename, locate / archive result files
│   ├── soap_client.py       SOAP call + outcome classification
│   ├── result_processor.py  reading result files, success evaluation
│   ├── db.py                PostgreSQL access (table names from config)
│   ├── redis_bus.py         publishing + control subscription
│   └── logging_setup.py     console, rotating file and Redis log handler
├── sql/init.sql             PostgreSQL schema script
├── Dockerfile
├── docker-compose.yml       postgres + redis + app
├── requirements.txt
└── tools/mock_soap_server.py   fake web service for local testing
```

## Quick start (Docker)

```bash
mkdir -p data/source data/staging data/results data/archive
cp /path/to/your/fal/*.txt data/source/

# edit config.json (SOAP url, envelope, rename rule, result patterns ...)
docker compose up -d --build
docker compose logs -f app

# begin processing
docker compose exec redis redis-cli publish fal:control start
```

Local directories `./data/*` are mounted to `/data/*` in the container. `config.json` is
mounted read-only: after editing it run `docker compose restart app`.

Database/Redis credentials can be overridden with a `.env` file next to
`docker-compose.yml` (`POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `SOAP_URL`).

### Running without Docker

```bash
pip install -r requirements.txt
# point config.json (or env vars DB_HOST, REDIS_HOST, SOURCE_DIR, ...) to your services
python main.py --config config.json
```

## Redis channels

All channel names are configurable under `redis.channels`. Messages are JSON.

| Channel (default) | Direction | Content |
|---|---|---|
| `fal:control`  | you → service | commands (see below) |
| `fal:logs`     | service → you | every log line |
| `fal:status`   | service → you | current state of the service |
| `fal:progress` | service → you | percentage of finished files |

### Control commands

Send either plain text or JSON (`{"command": "pause"}`):

| Command | Effect |
|---|---|
| `start`  | Starts a new run from the source directory, **or resumes the unfinished run** found in the database |
| `pause`  | Pauses at the next safe point (also during waits/retries). Checkpoint saved in DB |
| `resume` | Continues after `pause` (or behaves like `start` after a `stop`) |
| `stop`   | Stops at the next safe point; checkpoint stays in DB, `start` continues later |
| `status` | Re-publishes the last status and progress messages |
| `shutdown` | Stops the run and exits the process (same as SIGTERM) |

```bash
redis-cli publish fal:control start
redis-cli publish fal:control pause
redis-cli publish fal:control resume
redis-cli publish fal:control stop
redis-cli psubscribe 'fal:*'          # watch everything
redis-cli get fal:status:last         # last status (late subscribers)
redis-cli get fal:progress:last       # last progress
```

### Status values

| State | Meaning |
|---|---|
| `WAITING` | Idle, waiting for the `start` command |
| `RUNNING` | A run is active |
| `PROCESSING` | Moving/renaming, calling the web service or importing results |
| `WAITING_FOR_FILE` | Web service says the file is not on the remote server yet (SFTP in progress) |
| `WAITING_FOR_RESULT` | Web service succeeded; waiting for the result files |
| `PAUSED` / `STOPPED` | Paused / stopped by command |
| `DONE` | Run finished |
| `ERROR` | Unexpected error (e.g. database down); fix it and send `start` to resume |

```json
{"state": "WAITING_FOR_FILE", "message": "File not yet available on remote server (attempt 2)",
 "run_id": 1, "file": "fal_a.txt", "timestamp": "2026-10-20T07:33:19+00:00"}
```

### Progress message

```json
{"run_id": 1, "total": 300, "processed": 100, "done": 98, "failed": 2,
 "percent": 33.33, "current_file": "fal_101.txt", "timestamp": "..."}
```

### Log message

```json
{"timestamp": "...", "level": "WARNING", "logger": "fal.orchestrator", "run_id": 1,
 "file": "fal_a.txt", "message": "Attempt 2: web service reports that the file does not exist yet: ..."}
```

## Configuration reference (`config.json`)

Values may reference environment variables: `${NAME}` or `${NAME:-default}`.

| Key | Description |
|---|---|
| `app.auto_start` | `true` = start processing immediately when the service boots |
| `paths.source_dir` | Directory with the original fal files |
| `paths.staging_dir` | **Path 1** – files are moved here (renamed). The external SFTP job reads from here |
| `paths.results_dir` | **Path 2** – where the result files appear |
| `paths.archive_dir` | Target when `results.after_import` is `move` |
| `source.file_glob` | Which files to take, e.g. `*.txt` |
| `source.min_age_seconds` | Ignore files modified more recently than this (still being written) |
| `rename.pattern` / `rename.replacement` | Regex rename (`re.sub` syntax, group refs like `\g<name>`). Extra tokens in the replacement: `{job_id}` (6-digit), `{date}`, `{datetime}` |
| `rename.on_no_match` | `keep_name` or `fail` when the regex does not match a file name |
| `rename.on_target_exists` | `error` or `overwrite` |
| `staging.use_temp_suffix`, `staging.temp_suffix` | Cross-filesystem moves are copied to `<name>.part` first so the SFTP job never sees a half file |
| `soap.endpoint_url`, `soap_version`, `soap_action`, `timeout_seconds`, `verify_ssl`, `headers`, `auth` | Connection settings (`auth.type`: `none`, `basic`, `bearer`) |
| `soap.envelope_template` | Request XML. `{{file_name}}` is replaced (XML-escaped) with the renamed file name |
| `soap.extra_params_from_db` | `true` = also expose every `json_config` key as `{{key}}` placeholder |
| `soap.initial_delay_seconds` | Time to wait **before the first call** so the external SFTP can finish (default 60) |
| `soap.retry_interval_seconds` | Pause between retries |
| `soap.max_wait_seconds` / `soap.max_attempts` | Give up on a file after this time / number of calls (`max_attempts` 0 = unlimited) |
| `soap.not_found_patterns` | Regexes (case-insensitive) that identify the "file does not exist" answer |
| `soap.success_patterns` | Optional regexes the response must match; empty = HTTP 2xx without a SOAP Fault |
| `soap.retry_on_transport_error`, `soap.retry_on_fault` | Retry on network errors / other SOAP faults, or fail the file immediately |
| `soap.record_errors_in_db` | Store every failed call in the errors table |
| `results.files[]` | One entry per expected result file: `name`, `glob` (`{stem}` = renamed file name without extension, `{filename}` = full name), `required`, `check_status` |
| `results.wait_timeout_seconds`, `poll_interval_seconds`, `stable_seconds` | How long / how often to wait for result files, and how long a file must be unchanged before it is read |
| `results.encoding`, `encoding_errors`, `skip_blank_lines` | File reading |
| `results.delimiter` / `results.parse_json_lines` | Fill the `parsed` JSONB column (split by delimiter, or JSON per line) |
| `results.success_regex` / `failure_regex` | Applied to the result files that have `check_status: true` |
| `results.log_preview_lines`, `log_preview_max_chars` | How many lines of each result file are written to the log |
| `results.after_import` | `keep`, `move` (to archive dir) or `delete` |
| `processing.on_job_failure` | `continue` with the next file, or `stop` the run |
| `processing.retry_failed_on_resume` | Failed files are retried when a run is resumed |
| `database.*` | Connection, `schema`, `auto_create_tables`, **table names** (`database.tables.*`) and `json_config` column names |
| `redis.*` | Connection and **channel names** (`redis.channels.*`) |
| `logging.*` | Level, console, rotating log file, publish to Redis |

> Table names are quoted identifiers: use lowercase names (e.g. `json_config`, not `Json_config`),
> otherwise PostgreSQL treats them as case-sensitive.
>
> Result globs: keep the file-name token unique per file (the default rename contains `{job_id}`).
> A pattern like `{stem}_summary*` could otherwise also match another file whose name starts with the same stem.

## Database

Tables (names configurable; created by `sql/init.sql` in Docker, or by the app itself when
`database.auto_create_tables` is `true`):

| Table | Purpose |
|---|---|
| `process_runs` | Run-level checkpoint (`RUNNING`, `PAUSED`, `STOPPED`, `ERROR`, `DONE`, counters) |
| `file_jobs` | File-level checkpoint: `pending → moved → soap_done → done / failed`, attempts, last error |
| `soap_call_errors` | Every failed web service call (e.g. "file does not exist"), with HTTP status and response excerpt |
| `result_records` | Every line of every result file (`result_type`, `result_file`, `line_no`, `raw_line`, `parsed`) |
| `json_config` | Read by the web service (sample rows included) |

Useful queries:

```sql
SELECT status, count(*) FROM file_jobs WHERE run_id = 1 GROUP BY status;
SELECT file_name, attempt, error_message, created_at FROM soap_call_errors ORDER BY id DESC LIMIT 20;
SELECT result_type, line_no, raw_line FROM result_records WHERE file_name = 'FAL_000001_fal_a.txt' ORDER BY id;
```

### Checkpoints and resuming

State is persisted after every step, so after `stop`, `pause`, a crash or a restart,
`start` continues exactly where it left off: a file that was already moved is not moved
again, one whose web service call succeeded goes straight to waiting for results, and
result import is idempotent. New files added to the source directory are picked up by the
next *new* run (after the current one is `DONE`).

## Testing locally with the mock web service

```bash
python tools/mock_soap_server.py --port 8080 \
    --staging-dir data/staging --results-dir data/results \
    --sftp-delay 20 --process-delay 10
```

It answers "File does not exist" until `--sftp-delay` seconds after the file appears in the
staging directory, then answers success and creates `*_summary.txt`, `*_detail.txt`,
`*_errors.txt` in the results directory. Set `soap.initial_delay_seconds` and
`soap.retry_interval_seconds` to small values in `config.json` for a quick test
(the mock relies on Linux `ctime`, so it is for development only).

## Adapting to your web service

1. Replace `soap.envelope_template`, `soap_action` and `endpoint_url` with your service's request.
2. Adjust `soap.not_found_patterns` to the exact wording of its "file does not exist" error.
3. Adjust `results.files[]` globs to the real result-file names, and set `check_status`
   on the file that tells whether processing succeeded together with `success_regex`/`failure_regex`.
