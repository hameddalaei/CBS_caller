# Fal Processor

A sequential file-processing service controlled through Redis. It stages and renames
input files, calls a SOAP service, waits for result files, and imports their lines
into PostgreSQL. A separate system must perform SFTP and return the result files.

The configuration was aligned on 2026-10-21 with `sample/sample json.txt` and the
three `.msg` filenames in `sample/`. Despite its filename, the request sample is
**SOAP XML, not JSON**. Original sample files were preserved.

## Sample alignment

- Operation: `BatchScatteredChgSubBasicInfo`.
- Request: `BatchScatteredChgSubBasicInfoRequestMsg`, including the sample's
  `RequestHeader`, ownership, access security, operator, and capitalized `FileName`.
- Incoming files are still selected with `source.file_glob: "*.txt"`.
  Change that to `"*.msg"` if your original inputs use `.msg` instead.
- Staged name: `SCATTEREDCHGSUBINFO_{datetime}_{job_id}.msg`.
  The timestamp is local time and the job ID is padded to at least six digits.
- Expected outputs: `{stem}_SUMMERY.msg`, `{stem}_SUCC.msg`, `{stem}_FAILED.msg`.
  The spelling **SUMMERY** is intentional and comes directly from the sample.
  `{stem}` is the staged filename without `.msg`; matches are exact and case-sensitive.
- SOAP acceptance requires HTTP 2xx, a valid SOAP envelope, no SOAP Fault,
  and exactly one configured `ResultCode` with value `0`.
  XML namespace prefixes may change without affecting matching.

The sample does **not** provide the real endpoint, SOAPAction HTTP header, real
credentials, input-file contents, or result-file contents. `SOAP_ACTION` defaults
to empty; obtain its actual value from the service's WSDL/administrator.
The sample IDs 101/102 are defaults for illustration and must be confirmed.
`MessageSeq` is generated as a new 32-character hexadecimal UUID per call;
confirm the provider's length/format and retry rules. It can be supplied explicitly
through `soap.params.message_seq` or the optional database parameters.

### Provisional result policy

All three result files are required. The summary must contain the whole word
`success` and must not contain `fail`, `failed`, or `failure` (case-insensitive).
Any non-whitespace content in `_FAILED.msg` fails the job through its
`fail_if_nonempty: true` option. An empty summary is not accepted as success.

**This is a conservative configurable policy, not a confirmed Huawei output
specification.** All three supplied output samples are empty. Confirm actual
summary syntax and whether a failed-file header can appear without failed records,
then adjust `results.success_regex`, `failure_regex`, and `fail_if_nonempty`.
The mock's `STATUS: SUCCESS` line is synthetic test data.

## Validate and test without external services

Run from the project directory in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:SOAP_PASSWORD = 'test-only'
.\.venv\Scripts\python.exe main.py --config config.json --validate-config
.\.venv\Scripts\python.exe testcode\run_tests.py
.\.venv\Scripts\python.exe -m pip check
```

`--validate-config` does not connect to services or move files. It checks required
settings, XML, placeholders, booleans, numeric ranges, regexes and result specs.
Validation intentionally fails when `SOAP_PASSWORD` is empty.

Tests use temporary folders, fake credentials, and loopback HTTP on an ephemeral
port. Workflow tests use an in-memory database double and a mocked Redis bus.
They do not touch the production data folders or prove live database behavior.

Two additional live tests require `--live` and explicitly configured
`FAL_TEST_POSTGRES_DSN` and/or `FAL_TEST_REDIS_URL` dedicated test services.
The PostgreSQL test creates and removes its own random schema; the Redis test uses
its own random channel/key. Do not point these variables at production services.
See `testcode/README.md` for test commands and `VALIDATION.md` for review limitations.

## Run with Docker

The Dockerfile uses Python 3.12; Compose provides PostgreSQL 16 and Redis 7.

```powershell
New-Item -ItemType Directory -Force -Path data/source,data/staging,data/results,data/archive
Copy-Item .env.example .env
```

Edit `.env` to supply `SOAP_PASSWORD`, the real endpoint/IDs/action, and the database
password. The SOAP password belongs in the XML request header; HTTP basic/bearer
authentication is a separate optional setting under `soap.auth`.
Configure result parsing before using real inputs. Copy input `.txt` files into
`data/source`, then run:

```powershell
docker compose up -d --build
docker compose logs -f app
```

After the app is ready, send the start command from another terminal:

```powershell
docker compose exec redis redis-cli publish fal:control start
```

## GitHub Actions and Docker Hub

The CI/CD workflow runs the tests and configuration check before building the
production image. It publishes the image on pushes to `main` and `v*` version
tags. Configure Docker Hub credentials in GitHub Actions as described in
[GITHUB_CI_CD.md](GITHUB_CI_CD.md). The Docker build excludes `testcode/` and
`tests/`; these remain separate repository-only test code.

`app.auto_start` defaults to false. Local `./data` is mounted as `/data` and `./logs`
as `/app/logs`; `config.json` is mounted read-only. After editing that JSON file,
run `docker compose restart app`. After editing Compose environment settings,
run `docker compose up -d` so the containers receive them.

The `sql/init.sql` script runs when PostgreSQL initializes an empty volume.
Changing the environment password does not update an already-created DB user.
The sample Compose exposes PostgreSQL and Redis host ports; restrict their
network exposure and configure authentication for your deployment.

## Run Python locally

Use the environment created above and run only PostgreSQL/Redis through Compose
(or use existing test services). Do not also run the Compose app against the same
files/database.

```powershell
docker compose up -d postgres redis
$env:DB_HOST = 'localhost'
$env:REDIS_HOST = 'localhost'
$env:DB_NAME = 'fal_db'
$env:DB_USER = 'fal_user'
$env:DB_PASSWORD = 'your-configured-database-password'
$env:SOURCE_DIR = "$PWD/data/source"
$env:STAGING_DIR = "$PWD/data/staging"
$env:RESULTS_DIR = "$PWD/data/results"
$env:ARCHIVE_DIR = "$PWD/data/archive"
$env:SOAP_URL = 'http://localhost:8080/FalService'
$env:SOAP_PASSWORD = 'test-only'
.\.venv\Scripts\python.exe main.py --config config.json
```

Python does not automatically load `.env`; set environment variables in its
terminal. The `test-only` password is for the mock, not the real service.
Configuration values support `${NAME}` and `${NAME:-default}` substitutions.

### Local mock service

Start in a separate terminal from the project folder:

```powershell
.\.venv\Scripts\python.exe tools/mock_soap_server.py --port 8080 `
  --staging-dir data/staging --results-dir data/results `
  --sftp-delay 2 --process-delay 1
```

For a quick retry demonstration, set `soap.initial_delay_seconds` to 0 and
`soap.retry_interval_seconds` to 1 before starting the app. The mock measures
transfer delay from the first SOAP request that sees the staged file, using a
monotonic clock on both Windows and Linux. It performs no real SFTP.
Use `--result-code 999` to simulate business rejection on HTTP 200, or
`--failed-records` to produce a nonempty failed output.

When the app is inside Docker and the mock is on its host, use the configured
`host.docker.internal` endpoint and start the mock with `--host 0.0.0.0` so the
container can reach it. The default mock bind address is loopback only.

## Redis control and progress

| Channel | Purpose |
|---|---|
| `fal:control` | Commands to the app |
| `fal:status` | Current state and file |
| `fal:progress` | Total, processed, done, failed, percent |
| `fal:logs` | Logs, including exception details |

Commands accept plain text or JSON such as `{"command":"pause"}`.

| Command | Behavior |
|---|---|
| `start` | Resume the latest unfinished run, or create a new run |
| `pause` | Pause at the next safe checkpoint |
| `resume` | Resume a pause, or start when idle |
| `stop` | Stop processing and retain checkpoints; process stays alive |
| `status` | Republish the last in-memory status/progress |
| `shutdown` | Exit the process; Compose restart policy may restart it |

```powershell
docker compose exec redis redis-cli publish fal:control pause
docker compose exec redis redis-cli publish fal:control resume
docker compose exec redis redis-cli publish fal:control stop
docker compose exec redis redis-cli psubscribe 'fal:*'
docker compose exec redis redis-cli get fal:status:last
docker compose exec redis redis-cli get fal:progress:last
```

States: `WAITING`, `RUNNING`, `PROCESSING`, `WAITING_FOR_FILE`,
`WAITING_FOR_RESULT`, `PAUSED`, `STOPPED`, `DONE`, `ERROR`.
Progress counts successful **and failed** files; 100% or `DONE` does not mean all
files succeeded. Redis commands are Pub/Sub messages, not a durable queue.
Pause/stop cannot interrupt an already-running HTTP request or DB transaction.

## Configuration reference

| Section/key | Meaning |
|---|---|
| `paths.*` | Source, staging, results, archive directories |
| `source.file_glob`, `min_age_seconds` | Input selection and minimum modification age |
| `rename.*` | Regex rename, `{datetime}`/`{date}`/`{job_id}`, conflict policy |
| `staging.*` | Temporary `.part` suffix when copying across filesystems |
| `soap.params` | XML placeholder values; optional DB params override these |
| `soap.extra_params_from_db` | Read key/value parameters from `json_config` |
| `soap.result_code_path`, `result_desc_path`, `success_codes` | Namespaced XML response validation |
| `soap.success_patterns` | Optional additional regex requirement; never bypasses HTTP status or ResultCode |
| `soap.initial_delay_seconds` | Wait before the first attempt (default 60) |
| `soap.retry_interval_seconds` | Wait between attempts (default 15) |
| `soap.max_attempts` | Attempt limit; 0 means no count limit |
| `soap.max_wait_seconds` | Retry-sleep budget (default 900), not a wall-clock deadline |
| `soap.retry_on_transport_error`, `retry_on_fault` | Retry network errors / other service failures |
| `results.files` | Exact filename patterns, required flags, status checks, nonempty failure flags |
| `results.stable_seconds` | Required modification age (default 2); not a file lock |
| `results.wait_timeout_seconds`, `poll_interval_seconds` | Result polling budget and interval |
| `results.encoding`, `encoding_errors`, `skip_blank_lines` | Decoding and line filtering |
| `results.delimiter`, `parse_json_lines` | Optional JSONB parsing; otherwise only raw lines |
| `results.after_import` | `keep`, `move`, `delete`; cleanup only after successful completion is saved |
| `processing.on_job_failure` | `continue` or `stop` |
| `processing.retry_failed_on_resume` | Retry failed jobs of an unfinished run |
| `database.*`, `redis.*` | Connections, table/channel names |
| `logging.*` | Console, rotating file, Redis logging |

Only the first configured preview lines are logged; all retained nonblank lines
are imported into the database. Defaults: UTF-8, replacement of invalid characters,
5 preview lines, 300 characters per preview, `logs/fal_processor.log` with a 10 MB
rotation threshold and 5 backups.

## Database and recovery

| Table | Purpose |
|---|---|
| `process_runs` | Run state and counters |
| `file_jobs` | File state, persisted target name, attempts, timestamps, errors |
| `soap_call_errors` | Failed service calls and response excerpts |
| `result_records` | One row per retained result line, plus optional JSONB |
| `json_config` | Optional external-service parameters |

The app can create its four operational tables. It does **not** create
`json_config`; the Docker initialization SQL does. Table/column names are configurable.

The normal job stages are `pending -> moved -> soap_done -> done/failed`.
The timestamp-based target name is saved **before** moving the file. On retry,
failed jobs resume from their last persisted stage (`soap_ok_at`, then `moved_at`),
so successful SOAP calls are not deliberately repeated after result failure.

Imported rows are atomically replaced per job. Result cleanup happens after the
`done` checkpoint; failed outputs remain available for diagnosis/retry. A crash
after completion but before cleanup can leave output files behind.

There is no distributed transaction with the external SOAP service: a crash after
the remote service accepts a request but before the app saves `soap_done` can still
cause a duplicate request. Coordinate idempotency with the provider. Run only one
processor instance against these folders/tables; multi-worker claiming is not implemented.

`start` resumes only runs in `RUNNING`, `PAUSED`, `STOPPED`, or `ERROR`.
It does not reopen a `DONE` run, even if that run contained failed files.
New input files are discovered only at the start of a new run.

## Layout and review artifacts

- `main.py`: entry point and configuration-only validation.
- `app/`: controller, workflow, filesystem, SOAP, PostgreSQL, Redis and logging.
- `tools/mock_soap_server.py`: sample-compatible local mock.
- `testcode/`: complete test suite, runner, English/Persian instructions and coverage reports.
- `tests/`: compatibility wrappers for the original test modules.
- `VALIDATION.md`: review findings and test scope.
- `testcode/reports/latest.txt`: latest full-suite test output.
- `validation/tests.txt`: earlier validation-run output.
- `validation/before-validation.zip`: original code/config/docs before the review.

The existing `readme.pdf` and `readme.fa.html` describe the **2026-10-20 version**;
they predate this sample synchronization. Use this README for the updated settings.
