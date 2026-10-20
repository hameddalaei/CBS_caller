"""Main workflow: move -> rename -> SOAP call (with retry) -> wait for results -> import -> next file."""

import logging
from datetime import datetime, timezone
from pathlib import Path

import psycopg2

from .controller import StopRequested
from .file_manager import FileOperationError
from .logging_setup import LogContext
from .redis_bus import utc_now_iso
from .soap_client import FAULT, NOT_FOUND, SUCCESS, TRANSPORT_ERROR

log = logging.getLogger("fal.orchestrator")

# Status channel values
ST_WAITING = "WAITING"                    # idle, waiting for a 'start' command
ST_RUNNING = "RUNNING"                    # a run is active
ST_PROCESSING = "PROCESSING"              # working on a file (move / SOAP call / import)
ST_WAITING_FOR_FILE = "WAITING_FOR_FILE"  # SOAP reports the file is not on the remote server yet
ST_WAITING_FOR_RESULT = "WAITING_FOR_RESULT"  # waiting for result files in results dir
ST_PAUSED = "PAUSED"
ST_STOPPED = "STOPPED"
ST_DONE = "DONE"
ST_ERROR = "ERROR"


class JobFailed(Exception):
    """A single file could not be processed (the run may continue with the next file)."""


def _now():
    return datetime.now(timezone.utc)


class Orchestrator:
    def __init__(self, cfg, db, bus, ctrl, files, soap, results):
        self.cfg, self.db, self.bus, self.ctrl = cfg, db, bus, ctrl
        self.files, self.soap, self.results = files, soap, results

        self.auto_start = bool(cfg.get("app.auto_start", False))
        self.idle_poll = float(cfg.get("app.idle_poll_seconds", 1))
        self.on_failure = cfg.get("processing.on_job_failure", "continue")
        self.retry_failed = bool(cfg.get("processing.retry_failed_on_resume", True))

        self.initial_delay = float(cfg.get("soap.initial_delay_seconds", 60))
        self.retry_interval = float(cfg.get("soap.retry_interval_seconds", 15))
        self.max_wait = float(cfg.get("soap.max_wait_seconds", 900))
        self.max_attempts = int(cfg.get("soap.max_attempts", 0))
        self.retry_transport = bool(cfg.get("soap.retry_on_transport_error", True))
        self.retry_fault = bool(cfg.get("soap.retry_on_fault", False))
        self.record_errors = bool(cfg.get("soap.record_errors_in_db", True))
        self.extra_from_db = bool(cfg.get("soap.extra_params_from_db", False))

        self.result_timeout = float(cfg.get("results.wait_timeout_seconds", 3600))
        self.result_poll = float(cfg.get("results.poll_interval_seconds", 5))

        self.run_id = None
        self.current_file = None
        self._state = None
        self._state_key = None
        self._pre_pause_state = ST_RUNNING
        self._last_status = None
        self._last_progress = None

        ctrl.on_pause = self._on_pause
        ctrl.on_resume = self._on_resume

    # ------------------------------------------------------------------ status
    def _status(self, state, message=""):
        key = (state, message, self.current_file)
        if key == self._state_key:
            return
        self._state, self._state_key = state, key
        payload = {
            "state": state,
            "message": message,
            "run_id": self.run_id,
            "file": self.current_file,
            "timestamp": utc_now_iso(),
        }
        self._last_status = payload
        self.bus.publish_status(payload)

    def _progress(self):
        if self.run_id is None:
            return
        counts = self.db.refresh_run_counts(self.run_id)
        processed = counts["done"] + counts["failed"]
        total = counts["total"]
        payload = {
            "run_id": self.run_id,
            "total": total,
            "processed": processed,
            "done": counts["done"],
            "failed": counts["failed"],
            "percent": round(processed * 100.0 / total, 2) if total else 100.0,
            "current_file": self.current_file,
            "timestamp": utc_now_iso(),
        }
        self._last_progress = payload
        self.bus.publish_progress(payload)

    def publish_snapshot(self):
        """Re-publish the last known status/progress (control command 'status'). Redis only."""
        if self._last_status:
            self.bus.publish_status(dict(self._last_status, timestamp=utc_now_iso()))
        if self._last_progress:
            self.bus.publish_progress(dict(self._last_progress, timestamp=utc_now_iso()))

    def _on_pause(self):
        self._pre_pause_state = self._state or ST_RUNNING
        if self.run_id is not None:
            self.db.update_run_state(self.run_id, "PAUSED")
        log.info("Processing paused (checkpoint saved)")
        self._status(ST_PAUSED, "Paused by control command")

    def _on_resume(self):
        if self.run_id is not None:
            self.db.update_run_state(self.run_id, "RUNNING")
        log.info("Processing resumed")
        self._state_key = None
        self._status(self._pre_pause_state, "Resumed")

    # ---------------------------------------------------------------- main loop
    def run(self):
        resumable = self.db.get_unfinished_run()
        if resumable:
            msg = (f"Unfinished run #{resumable['run_id']} found "
                   f"({resumable['done_files'] + resumable['failed_files']}/{resumable['total_files']} files processed); "
                   f"send 'start' to resume")
            log.info(msg)
            self._status(ST_WAITING, msg)
        else:
            self._status(ST_WAITING, "Waiting for 'start' command")
        if self.auto_start:
            log.info("auto_start is enabled")
            self.ctrl.handle("start")

        while not self.ctrl.shutdown_requested:
            if not self.ctrl.wait_for_start(self.idle_poll):
                continue
            self.ctrl.set_running(True)
            try:
                self._run_batch()
            except StopRequested:
                self._on_stopped()
            except Exception as exc:  # noqa: BLE001
                log.exception("Run aborted by an unexpected error: %s", exc)
                self._on_error(exc)
            finally:
                self.ctrl.set_running(False)
                self.current_file = None
                LogContext.clear("file")
        log.info("Shutdown requested; worker loop finished")

    def _on_stopped(self):
        try:
            if self.run_id is not None:
                self.db.update_run_state(self.run_id, "STOPPED")
                self._progress()
        except Exception as exc:  # noqa: BLE001
            log.error("Could not save stop checkpoint: %s", exc)
        log.info("Run stopped; checkpoint saved. Send 'start' to resume from the next unfinished file")
        self._status(ST_STOPPED, "Stopped by control command; send 'start' to resume")

    def _on_error(self, exc):
        try:
            if self.run_id is not None:
                self.db.update_run_state(self.run_id, "ERROR")
        except Exception as inner:  # noqa: BLE001
            log.error("Could not save error state: %s", inner)
        self._status(ST_ERROR, f"Run aborted: {exc}")

    # --------------------------------------------------------------------- run
    def _get_or_create_run(self):
        run = self.db.get_unfinished_run()
        if run:
            if self.retry_failed:
                reset = self.db.reset_failed_jobs(run["run_id"])
                if reset:
                    log.info("Re-queued %d previously failed file(s) of run #%d", reset, run["run_id"])
            self.db.update_run_state(run["run_id"], "RUNNING")
            log.info("Resuming run #%d from checkpoint", run["run_id"])
            return run["run_id"]
        names = self.files.list_source_files()
        if not names:
            return None
        run_id = self.db.create_run(str(self.files.source), names)
        log.info("Created run #%d with %d file(s) from %s", run_id, len(names), self.files.source)
        return run_id

    def _run_batch(self):
        run_id = self._get_or_create_run()
        if run_id is None:
            log.info("No files found in the source directory; nothing to do")
            self._status(ST_WAITING, "No files found in source directory; waiting for 'start' command")
            return
        self.run_id = run_id
        LogContext.set(run_id=run_id)
        self._status(ST_RUNNING, "Run started")
        self._progress()

        while True:
            self.ctrl.guard()
            job = self.db.next_job(run_id)
            if job is None:
                break
            self.current_file = job["original_name"]
            LogContext.set(file=self.current_file)
            self._status(ST_PROCESSING, f"Processing {self.current_file}")
            self._process_job(job)
            self._progress()
            self.current_file = None
            LogContext.clear("file")

        counts = self.db.refresh_run_counts(run_id)
        self.db.update_run_state(run_id, "DONE", finished=True)
        self._progress()
        message = f"Run finished: {counts['done']} done, {counts['failed']} failed, {counts['total']} total"
        log.info(message)
        self._status(ST_DONE, message)

    # --------------------------------------------------------------------- job
    def _process_job(self, job):
        job_id = job["job_id"]
        status = job["status"]
        log.info("Starting file (job %d, status '%s')", job_id, status)
        try:
            if status == "pending":
                job = self._stage_move(job)
                status = "moved"
            if status == "moved":
                self._call_soap(job)
                status = "soap_done"
            if status == "soap_done":
                self._collect_results(job)
        except (StopRequested, psycopg2.Error):
            raise
        except JobFailed as exc:
            self._fail_job(job, str(exc))
        except FileOperationError as exc:
            self._fail_job(job, str(exc))
        except Exception as exc:  # noqa: BLE001
            log.exception("Unexpected error while processing file")
            self._fail_job(job, f"unexpected error: {exc}")

    def _fail_job(self, job, reason):
        log.error("File FAILED: %s", reason)
        self.db.update_job(job["job_id"], status="failed", last_error=reason[:2000], finished_at=_now())
        if self.on_failure == "stop":
            log.error("processing.on_job_failure=stop: halting the run")
            self.ctrl.handle("stop")

    # -- step 1: move + rename --------------------------------------------
    def _stage_move(self, job):
        target = self.files.target_name(job["original_name"], job["job_id"])
        outcome = self.files.move_to_staging(job["original_name"], target)
        self.db.update_job(job["job_id"], renamed_name=target, status="moved", moved_at=_now())
        if outcome == "already_moved":
            log.info("File was already moved earlier; continuing as '%s'", target)
        else:
            log.info("Moved '%s' to staging as '%s'", job["original_name"], target)
        job = dict(job, renamed_name=target, status="moved")
        return job

    # -- step 2: SOAP call with retry -------------------------------------
    def _extra_params(self):
        return self.db.read_json_config() if self.extra_from_db else {}

    def _call_soap(self, job):
        job_id, run_id, name = job["job_id"], job["run_id"], job["renamed_name"]

        if job["soap_attempts"] == 0 and self.initial_delay > 0:
            self._status(ST_WAITING_FOR_FILE, f"Waiting {self.initial_delay:g}s for the external SFTP transfer")
            log.info("Waiting %gs before the first web service call (external SFTP transfer time)", self.initial_delay)
            self.ctrl.sleep(self.initial_delay)

        waited = 0.0
        while True:
            self.ctrl.guard()
            attempt = self.db.bump_soap_attempts(job_id)
            result = self.soap.call(name, self._extra_params())

            if result.outcome == SUCCESS:
                self.db.update_job(job_id, status="soap_done", soap_ok_at=_now())
                log.info("Web service call succeeded (attempt %d, HTTP %s)", attempt, result.http_status)
                return

            self._record_soap_error(run_id, job_id, name, attempt, result)
            if result.outcome == NOT_FOUND:
                log.warning("Attempt %d: web service reports that the file does not exist yet: %s", attempt, result.message)
                self._status(ST_WAITING_FOR_FILE, f"File not yet available on remote server (attempt {attempt})")
            elif result.outcome == TRANSPORT_ERROR:
                log.warning("Attempt %d: web service unreachable: %s", attempt, result.message)
                if not self.retry_transport:
                    raise JobFailed(f"Web service unreachable: {result.message}")
            elif result.outcome == FAULT:
                log.warning("Attempt %d: web service returned an error (HTTP %s): %s", attempt, result.http_status, result.message)
                if not self.retry_fault:
                    raise JobFailed(f"Web service error (HTTP {result.http_status}): {result.message}")

            if self.max_attempts and attempt >= self.max_attempts:
                raise JobFailed(f"Web service did not succeed after {attempt} attempts; last error: {result.message}")
            if waited >= self.max_wait:
                raise JobFailed(f"Web service did not succeed within {self.max_wait:g}s; last error: {result.message}")
            self.ctrl.sleep(self.retry_interval)
            waited += self.retry_interval

    def _record_soap_error(self, run_id, job_id, name, attempt, result):
        if not self.record_errors:
            return
        try:
            self.db.insert_soap_error(
                run_id, job_id, name, attempt, result.outcome, result.http_status,
                result.message, self.soap.excerpt(result),
            )
        except psycopg2.Error as exc:
            log.error("Could not store the web service error in the database: %s", exc)

    # -- step 3: wait for result files, import, evaluate -------------------
    def _collect_results(self, job):
        job_id, run_id, name = job["job_id"], job["run_id"], job["renamed_name"]
        self._status(ST_WAITING_FOR_RESULT, "Waiting for result files")
        log.info("Waiting for result files in %s", self.files.results)

        waited = 0.0
        while True:
            self.ctrl.guard()
            found = self.files.find_results(name)
            ready, missing, unstable = self.files.readiness(found)
            if ready:
                break
            if waited >= self.result_timeout:
                raise JobFailed(
                    f"Result files not complete within {self.result_timeout:g}s "
                    f"(missing: {missing or '-'}, still being written: {unstable or '-'})"
                )
            self.ctrl.sleep(self.result_poll)
            waited += self.result_poll

        self._status(ST_PROCESSING, "Importing result files")
        rows, texts, all_paths = [], [], []
        for spec in self.files.specs:
            for path in found.get(spec["name"], []):
                lines = self.results.read_lines(path)
                rows.extend(self.results.build_rows(run_id, job_id, name, spec["name"], path, lines))
                texts.append((spec["name"], path.name, "\n".join(raw for _, raw in lines)))
                all_paths.append(path)
                log.info("Result file '%s' (%s): %d line(s)", path.name, spec["name"], len(lines))
                for preview_line in self.results.preview(lines):
                    log.info("%s", preview_line)

        imported = self.db.replace_results(job_id, rows)
        log.info("Imported %d result line(s) from %d file(s) into the database", imported, len(all_paths))
        ok, reason = self.results.evaluate(texts)
        self.files.finish_result_files(all_paths)

        if not ok:
            raise JobFailed(f"Processing result is not successful: {reason}")
        self.db.update_job(job_id, status="done", finished_at=_now(), last_error=None)
        log.info("File completed successfully")
