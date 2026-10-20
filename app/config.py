"""Configuration loading, environment expansion and validation."""

import json
import os
import re
from pathlib import Path

IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_MISSING = object()

REQUIRED_KEYS = [
    "paths.source_dir",
    "paths.staging_dir",
    "paths.results_dir",
    "rename.pattern",
    "rename.replacement",
    "soap.endpoint_url",
    "soap.envelope_template",
    "database.host",
    "database.dbname",
    "database.user",
    "redis.host",
    "redis.channels.control",
    "redis.channels.logs",
    "redis.channels.status",
    "redis.channels.progress",
    "results.files",
]


class ConfigError(Exception):
    """Raised when the configuration file is missing or invalid."""


def _expand(value):
    """Expand ${VAR} and ${VAR:-default} references using environment variables."""
    if isinstance(value, str):
        def repl(match):
            default = match.group(2) if match.group(2) is not None else ""
            return os.environ.get(match.group(1), default)

        return _ENV_RE.sub(repl, value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


class Config:
    """Read-only access to the JSON configuration with dotted-path lookups."""

    def __init__(self, data, path="<memory>"):
        self._data = data
        self.path = path

    @classmethod
    def load(cls, path):
        p = Path(path)
        if not p.is_file():
            raise ConfigError(f"Config file not found: {p}")
        try:
            raw = json.loads(p.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"Config file is not valid JSON ({p}): {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError("Config root must be a JSON object")
        return cls(_expand(raw), str(p))

    def get(self, dotted, default=None):
        cur = self._data
        for part in dotted.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return default
        return cur

    def require(self, dotted):
        value = self.get(dotted, _MISSING)
        if value is _MISSING or value is None:
            raise ConfigError(f"Missing required config key: {dotted}")
        return value

    def section(self, dotted):
        value = self.get(dotted, {})
        if not isinstance(value, dict):
            raise ConfigError(f"Config key '{dotted}' must be an object")
        return value

    def validate(self):
        """Validate the configuration; raises ConfigError with a clear message."""
        for key in REQUIRED_KEYS:
            self.require(key)

        for key in ("paths.source_dir", "paths.staging_dir", "paths.results_dir",
                    "soap.endpoint_url", "rename.pattern", "database.host",
                    "database.dbname", "database.user", "redis.host"):
            if not str(self.get(key, "")).strip():
                raise ConfigError(f"Config key '{key}' must not be empty")

        regexes = {"rename.pattern": [self.get("rename.pattern")]}
        regexes["soap.not_found_patterns"] = self.get("soap.not_found_patterns", [])
        regexes["soap.success_patterns"] = self.get("soap.success_patterns", [])
        for key in ("results.success_regex", "results.failure_regex"):
            if self.get(key):
                regexes[key] = [self.get(key)]
        for key, patterns in regexes.items():
            for pattern in patterns:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ConfigError(f"Invalid regex in '{key}': {pattern!r} ({exc})") from exc

        files = self.get("results.files")
        if not isinstance(files, list) or not files:
            raise ConfigError("'results.files' must be a non-empty list")
        for spec in files:
            if not isinstance(spec, dict) or not spec.get("name") or not spec.get("glob"):
                raise ConfigError("Every entry of 'results.files' needs 'name' and 'glob'")

        identifiers = [self.get("database.schema", "public")]
        identifiers += list(self.section("database.tables").values())
        identifiers += list(self.section("database.json_config").values())
        for ident in identifiers:
            if not IDENT_RE.match(str(ident)):
                raise ConfigError(f"Invalid database identifier in config: {ident!r}")

        choices = {
            "results.after_import": ("keep", "move", "delete"),
            "processing.on_job_failure": ("continue", "stop"),
            "rename.on_no_match": ("keep_name", "fail"),
            "rename.on_target_exists": ("error", "overwrite"),
            "soap.soap_version": ("1.1", "1.2"),
            "soap.auth.type": ("none", "basic", "bearer"),
        }
        for key, allowed in choices.items():
            value = self.get(key)
            if value is not None and str(value) not in allowed:
                raise ConfigError(f"'{key}' must be one of {allowed}, got {value!r}")
