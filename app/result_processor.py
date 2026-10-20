"""
Processes Huawei CBS failure message files.

Failure files are named:  CHGSCATTEREDACCT_<timestamp>_FAILED.msg
Each line is pipe-delimited:
  account_id | operation_type | segment_info | error_code | error_message
"""

import glob
import logging
import os
from dataclasses import dataclass, field
from typing import Generator

from .config import get_config
from .db import get_db_connection

logger = logging.getLogger(__name__)

_EXPECTED_FIELD_COUNT = 5


@dataclass
class FailedRecord:
    account_id: str
    operation_type: str
    segment_info: str
    error_code: str
    error_message: str
    source_file: str = field(default="", repr=False)
    line_number: int = field(default=0, repr=False)


class ResultProcessorError(Exception):
    """Raised when a failure file cannot be parsed or persisted."""


class ResultProcessor:
    """
    Scans the NFS failed_dir for CHGSCATTEREDACCT_*_FAILED.msg files,
    parses each record, and persists them to the database.
    """

    def __init__(self) -> None:
        cfg = get_config()
        self._failed_dir: str = cfg["file_manager"]["failed_dir"]
        self._failed_glob: str = cfg["file_manager"]["failed_glob"]
        rp_cfg = cfg["result_processor"]
        self._separator: str = rp_cfg.get("field_separator", "|")
        self._fields: list[str] = rp_cfg["fields"]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process_all(self) -> int:
        """
        Find and process every pending failure file.

        Returns:
            Total number of failed records persisted across all files.
        """
        pattern = os.path.join(self._failed_dir, self._failed_glob)
        files = glob.glob(pattern)

        if not files:
            logger.debug("No failure files found matching %s", pattern)
            return 0

        total = 0
        for path in sorted(files):
            try:
                count = self._process_file(path)
                total += count
                self._mark_done(path)
            except ResultProcessorError as exc:
                logger.error("Failed to process %s: %s", path, exc)

        return total

    def process_file(self, path: str) -> int:
        """Process a single failure file by absolute path."""
        return self._process_file(path)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _process_file(self, path: str) -> int:
        logger.info("Processing failure file: %s", path)
        records = list(self._parse_file(path))

        if not records:
            logger.warning("No records parsed from %s", path)
            return 0

        self._persist(records)
        logger.info("Persisted %d failed records from %s", len(records), path)
        return len(records)

    def _parse_file(self, path: str) -> Generator[FailedRecord, None, None]:
        """Yield one FailedRecord per non-empty line."""
        try:
            with open(path, encoding="utf-8") as fh:
                for lineno, raw in enumerate(fh, start=1):
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        continue
                    record = self._parse_line(line, path, lineno)
                    if record is not None:
                        yield record
        except OSError as exc:
            raise ResultProcessorError(
                f"Cannot read failure file {path}: {exc}"
            ) from exc

    def _parse_line(
        self, line: str, path: str, lineno: int
    ) -> "FailedRecord | None":
        parts = [p.strip() for p in line.split(self._separator)]

        if len(parts) != _EXPECTED_FIELD_COUNT:
            logger.warning(
                "Skipping malformed line %d in %s "
                "(expected %d fields, got %d): %r",
                lineno,
                path,
                _EXPECTED_FIELD_COUNT,
                len(parts),
                line,
            )
            return None

        return FailedRecord(
            account_id=parts[0],
            operation_type=parts[1],
            segment_info=parts[2],
            error_code=parts[3],
            error_message=parts[4],
            source_file=os.path.basename(path),
            line_number=lineno,
        )

    def _persist(self, records: list[FailedRecord]) -> None:
        """Insert failed records into the database in a single transaction."""
        sql = """
            INSERT INTO failed_records (
                account_id,
                operation_type,
                segment_info,
                error_code,
                error_message,
                source_file,
                line_number,
                created_at
            ) VALUES (
                %(account_id)s,
                %(operation_type)s,
                %(segment_info)s,
                %(error_code)s,
                %(error_message)s,
                %(source_file)s,
                %(line_number)s,
                NOW()
            )
            ON CONFLICT DO NOTHING;
        """
        rows = [
            {
                "account_id": r.account_id,
                "operation_type": r.operation_type,
                "segment_info": r.segment_info,
                "error_code": r.error_code,
                "error_message": r.error_message,
                "source_file": r.source_file,
                "line_number": r.line_number,
            }
            for r in records
        ]

        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.executemany(sql, rows)
                conn.commit()
        except Exception as exc:
            raise ResultProcessorError(
                f"DB insert failed for {len(records)} records: {exc}"
            ) from exc

    @staticmethod
    def _mark_done(path: str) -> None:
        """Rename processed file to .done to prevent reprocessing."""
        done_path = path + ".done"
        try:
            os.rename(path, done_path)
            logger.debug("Renamed %s -> %s", path, done_path)
        except OSError as exc:
            logger.warning("Could not rename %s: %s", path, exc)

