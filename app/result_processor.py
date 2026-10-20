"""Reading result files, preparing DB rows and deciding success / failure."""

import json
import logging
import re

log = logging.getLogger("fal.results")


class ResultProcessor:
    def __init__(self, cfg):
        self.encoding = cfg.get("results.encoding", "utf-8")
        self.encoding_errors = cfg.get("results.encoding_errors", "replace")
        self.skip_blank = bool(cfg.get("results.skip_blank_lines", True))
        self.delimiter = cfg.get("results.delimiter", "") or ""
        self.parse_json = bool(cfg.get("results.parse_json_lines", False))
        success = cfg.get("results.success_regex") or ""
        failure = cfg.get("results.failure_regex") or ""
        self.success_re = re.compile(success) if success else None
        self.failure_re = re.compile(failure) if failure else None
        self.preview_lines = int(cfg.get("results.log_preview_lines", 5))
        self.preview_chars = int(cfg.get("results.log_preview_max_chars", 300))
        self.check_flags = {s["name"]: bool(s.get("check_status", False)) for s in cfg.require("results.files")}

    def read_lines(self, path):
        """Return [(line_no, raw_line)] for a result file."""
        lines = []
        with open(path, "r", encoding=self.encoding, errors=self.encoding_errors) as fh:
            for number, line in enumerate(fh, 1):
                raw = line.rstrip("\r\n")
                if self.skip_blank and not raw.strip():
                    continue
                lines.append((number, raw))
        return lines

    def parse(self, raw):
        if self.parse_json:
            try:
                return json.loads(raw)
            except ValueError:
                return None
        if self.delimiter:
            return raw.split(self.delimiter)
        return None

    def build_rows(self, run_id, job_id, file_name, spec_name, path, lines):
        return [
            (run_id, job_id, file_name, spec_name, path.name, number, raw, self.parse(raw))
            for number, raw in lines
        ]

    def evaluate(self, texts):
        """texts: [(spec_name, file_name, full_text)]. Returns (ok, reason)."""
        for spec_name, file_name, text in texts:
            if not self.check_flags.get(spec_name):
                continue
            if self.failure_re and self.failure_re.search(text):
                return False, f"failure marker found in result file '{file_name}'"
            if self.success_re and not self.success_re.search(text):
                return False, f"success marker not found in result file '{file_name}'"
        return True, "ok"

    def preview(self, lines):
        shown = []
        for number, raw in lines[: max(0, self.preview_lines)]:
            text = raw if len(raw) <= self.preview_chars else raw[: self.preview_chars] + "..."
            shown.append(f"  L{number}: {text}")
        return shown
