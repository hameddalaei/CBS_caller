"""Filesystem operations: scan source, move+rename to staging, locate and archive results."""

import fnmatch
import glob
import logging
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("fal.files")


class FileOperationError(Exception):
    pass


class FileManager:
    def __init__(self, cfg):
        self.source = Path(cfg.require("paths.source_dir"))
        self.staging = Path(cfg.require("paths.staging_dir"))
        self.results = Path(cfg.require("paths.results_dir"))
        self.archive = Path(cfg.get("paths.archive_dir") or "archive")

        self.file_glob = cfg.get("source.file_glob", "*")
        self.min_age = float(cfg.get("source.min_age_seconds", 0))

        self._rename_re = re.compile(cfg.require("rename.pattern"))
        self._rename_repl = str(cfg.require("rename.replacement"))
        self._on_no_match = cfg.get("rename.on_no_match", "keep_name")
        self._on_exists = cfg.get("rename.on_target_exists", "error")

        self._use_tmp = bool(cfg.get("staging.use_temp_suffix", True))
        self._tmp_suffix = cfg.get("staging.temp_suffix", ".part")

        self.specs = cfg.require("results.files")
        self._stable_seconds = float(cfg.get("results.stable_seconds", 2))
        self._after_import = cfg.get("results.after_import", "keep")

    # -- source ------------------------------------------------------------
    def list_source_files(self):
        if not self.source.is_dir():
            raise FileOperationError(f"Source directory does not exist: {self.source}")
        now = time.time()
        names = []
        for path in self.source.glob(self.file_glob):
            if not path.is_file():
                continue
            if self.min_age and now - path.stat().st_mtime < self.min_age:
                continue
            names.append(path.name)
        return sorted(names)

    # -- rename + move -----------------------------------------------------
    def target_name(self, original, job_id):
        now = datetime.now()
        repl = (
            self._rename_repl.replace("{job_id}", f"{int(job_id):06d}")
            .replace("{date}", now.strftime("%Y%m%d"))
            .replace("{datetime}", now.strftime("%Y%m%d%H%M%S"))
        )
        new_name, count = self._rename_re.subn(repl, original, count=1)
        if count == 0:
            if self._on_no_match == "fail":
                raise FileOperationError(f"Rename pattern does not match file name '{original}'")
            log.warning("Rename pattern does not match '%s'; keeping the original name", original)
            new_name = original
        if not new_name or Path(new_name).name != new_name:
            raise FileOperationError(f"Rename produced an invalid file name: {new_name!r}")
        return new_name

    def move_to_staging(self, original, target):
        """Move source/original to staging/target. Returns 'moved' or 'already_moved'."""
        src = self.source / original
        dst = self.staging / target
        try:
            self.staging.mkdir(parents=True, exist_ok=True)
            if not src.exists():
                if dst.exists():
                    return "already_moved"
                raise FileOperationError(f"Source file is missing: {src}")
            if dst.exists() and self._on_exists != "overwrite":
                raise FileOperationError(f"Target file already exists: {dst}")
            self._atomic_move(src, dst)
            return "moved"
        except OSError as exc:
            raise FileOperationError(f"Cannot move '{src}' to '{dst}': {exc}") from exc

    def _atomic_move(self, src, dst):
        try:
            os.replace(src, dst)  # same filesystem: atomic
            return
        except OSError:
            pass
        # Different filesystem: copy to a temporary name first so that the
        # external SFTP job never sees a half-written file.
        tmp = dst.with_name(dst.name + self._tmp_suffix) if self._use_tmp else dst
        shutil.copy2(src, tmp)
        if tmp != dst:
            os.replace(tmp, dst)
        os.remove(src)

    # -- results -----------------------------------------------------------
    def find_results(self, renamed_name):
        """Return {spec_name: [Path, ...]} for every configured result pattern."""
        stem = Path(renamed_name).stem
        try:
            names = sorted(os.listdir(self.results))
        except FileNotFoundError:
            names = []
        found = {}
        for spec in self.specs:
            pattern = (
                spec["glob"]
                .replace("{stem}", glob.escape(stem))
                .replace("{filename}", glob.escape(renamed_name))
            )
            found[spec["name"]] = [
                self.results / n
                for n in names
                if fnmatch.fnmatchcase(n, pattern) and (self.results / n).is_file()
            ]
        return found

    def readiness(self, found):
        """Return (ready, missing_names, unstable_names) for a find_results() mapping."""
        missing, unstable = [], []
        now = time.time()
        for spec in self.specs:
            paths = found.get(spec["name"], [])
            if not paths:
                if spec.get("required", True):
                    missing.append(spec["name"])
                continue
            for path in paths:
                try:
                    if now - path.stat().st_mtime < self._stable_seconds:
                        unstable.append(path.name)
                except FileNotFoundError:
                    missing.append(spec["name"])
        return (not missing and not unstable), missing, unstable

    def finish_result_files(self, paths):
        """Apply results.after_import (keep / move / delete) to imported files."""
        if self._after_import == "keep":
            return
        for path in paths:
            try:
                if self._after_import == "delete":
                    path.unlink()
                elif self._after_import == "move":
                    self.archive.mkdir(parents=True, exist_ok=True)
                    target = self.archive / path.name
                    if target.exists():
                        target = self.archive / f"{path.stem}.{datetime.now():%Y%m%d%H%M%S}{path.suffix}"
                    shutil.move(str(path), str(target))
            except OSError as exc:
                log.warning("Could not %s result file %s: %s", self._after_import, path, exc)
