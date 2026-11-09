# Validation report — 2026-10-21

## Expanded test suite

The maintained suite is now in `testcode/`. The expanded run discovered **140 tests: 138 passed and 2 live-service tests skipped**, with **90% combined statement/branch coverage**. Run `.\.venv\Scripts\python.exe testcode\run_tests.py --coverage`. Current reports are in `testcode/reports/`; the original review results below are retained as historical evidence. Live-service tests now also require explicit `--live` opt-in.

## Original review result

**37 tests passed; 2 live-service tests skipped (39 discovered).**
Python syntax checks passed for 15 files; `pip check` found no broken requirements.
`main.py --validate-config` passed with a fake test password. No production service
was contacted and no input data in `data/` was processed.

Local test environment: Windows, Python 3.14.6, requests 2.34.2, redis 8.1.0,
psycopg2-binary 2.9.13. Dependencies are isolated in `.venv`.
The Dockerfile targets Python 3.12; that container was not built or tested here.

## Sample synchronization

`sample/sample json.txt` contains two SOAP XML envelopes, not JSON. The request
template now preserves their operation names, namespaces, header hierarchy, and
capitalized `FileName`. The response is parsed structurally; `ResultCode=0` is
required at its expected path. XML prefixes are not hardcoded.

Result patterns match `filename_SUMMERY.msg`, `filename_SUCC.msg`, and
`filename_FAILED.msg` exactly after substituting the current input stem. No
sample file was edited. The staged prefix/extension match the example request;
incoming files remain `*.txt` unless configured otherwise.

## Reproduced defects fixed

1. HTTP 200 with a nonzero business code could be treated as success.
2. A success regex could override a failing HTTP status.
3. Missing/malformed/non-SOAP responses could be treated as successful.
4. Missing XML parameters silently became empty strings.
5. The old mock did not understand the sample's capitalized `FileName` or output suffixes.
6. Timestamp-based names were not checkpointed before movement; recovery could use a different name.
7. Result files could be removed before the final database checkpoint.
8. Exception logs were dropped by the Redis handler due to a nonexistent `formatException` method.
9. Validation accepted invalid intervals, string booleans and duplicate output types.
10. Interrupted condition-variable waits subtracted the entire requested sleep.

Also corrected failed-job reset to use persisted stage timestamps, and added a
pre-request check so an already-exhausted attempt limit does not send another
request. Live PostgreSQL execution of the reset SQL remains unverified here;
an opt-in test exercises it when a dedicated test DSN is supplied.

## What was tested

- Sample XML structure and namespace-prefix independence.
- XML escaping, required parameters, missing/duplicate/nonzero ResultCode,
  SOAP faults, missing remote files, HTTP errors, malformed/HTML responses.
- Configuration types, numeric bounds, XML and regex validation.
- Real temporary filesystem moves, no overwrite, exact result matching,
  empty summary rejection and failed-record detection.
- Simulated crash after move and before the final result checkpoint.
- Preservation of failed result files and request attempt limits.
- Redis exception payloads, command parsing, pause/resume/stop and early wakeups.
- **Four loopback HTTP workflow tests**: two-file processing with retries/import,
  HTTP-200 business rejection, failed result records, and resuming `soap_done`
  without a new SOAP call. These use a database double and mocked Redis publisher.

Repeat with:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
$env:SOAP_PASSWORD = 'test-only'
.\.venv\Scripts\python.exe main.py --validate-config
.\.venv\Scripts\python.exe -m pip check
```

Logs are in `validation/tests.txt`; the initial regression run is preserved in
`validation/baseline-tests.txt`. The initial suite included duplicate inherited
tests that were removed from the final suite; counts are not directly comparable.
The original 19 source/config/documentation files are backed up in
`validation/before-validation.zip`.

## Required before production validation

- Real SOAP endpoint, SOAPAction/WSDL, credentials, ownership/operator IDs,
  accepted MessageSeq format and retry/idempotency rules are not provided.
- All three sample result files are **empty**. The current whole-word-success
  summary rule and nonempty-failed-file rule are provisional. Confirm real data
  and whether a header-only failed file is valid. The mock output is synthetic.
- SOAP acceptance is distinct from successful completion of all input records.
  No real result-file semantics have been verified.
- Docker is not available in this environment; no image build, Compose startup,
  real PostgreSQL/Redis integration, SFTP transfer or real Huawei call was tested.
- To run live tests, set `FAL_TEST_POSTGRES_DSN` and/or `FAL_TEST_REDIS_URL` to
  dedicated test services, then rerun the suite. Without these values the tests
  deliberately skip. Real PostgreSQL table creation, transactions and reset SQL
  need that verification.
- The workflow is single-worker. External SOAP execution and local checkpoints
  are not one transaction, so exactly-once external execution is not guaranteed.
- Timeout budgets count retry/poll sleeps, not strict wall-clock deadlines.
  Result stability is based on modification age and is not a writer lock.
- A finished `DONE` run is not reopened by `start`; new files are scanned only
  for a new run. Failed jobs are retried only when resuming an unfinished run.

The code is validated for the tested local scenarios, **not certified ready for
production against the real service**.
