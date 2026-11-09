"""Configuration loading, environment expansion and validation."""

import json
import math
import os
import re
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

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
        except (OSError, UnicodeError) as exc:
            raise ConfigError(f"Cannot read config file: {p}") from exc
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

        for key in ("app", "paths", "source", "rename", "staging", "soap", "soap.auth",
                    "results", "processing", "database", "redis", "redis.channels", "logging"):
            self.section(key)

        for key in ("paths.source_dir", "paths.staging_dir", "paths.results_dir",
                    "soap.endpoint_url", "rename.pattern", "database.host",
                    "database.dbname", "database.user", "redis.host", "soap.envelope_template",
                    "redis.channels.control", "redis.channels.logs", "redis.channels.status", "redis.channels.progress"):
            if not isinstance(self.get(key), str) or not self.get(key).strip():
                raise ConfigError(f"Config key '{key}' must be a nonempty string")

        endpoint = urlparse(self.get("soap.endpoint_url"))
        if endpoint.scheme not in ("http", "https") or not endpoint.netloc:
            raise ConfigError("soap.endpoint_url must be an HTTP(S) URL")

        numeric = {
            "app.idle_poll_seconds": (0, False),
            "source.min_age_seconds": (0, True),
            "soap.timeout_seconds": (0, False),
            "soap.initial_delay_seconds": (0, True),
            "soap.retry_interval_seconds": (0, False),
            "soap.max_wait_seconds": (0, True),
            "soap.max_attempts": (0, True),
            "soap.response_excerpt_chars": (0, True),
            "results.wait_timeout_seconds": (0, True),
            "results.poll_interval_seconds": (0, False),
            "results.stable_seconds": (0, True),
            "results.log_preview_lines": (0, True),
            "results.log_preview_max_chars": (0, True),
            "database.port": (0, False), "database.connect_timeout": (0, False),
            "database.max_retries": (0, False), "database.retry_delay_seconds": (0, False),
            "redis.port": (0, False), "redis.db": (0, True),
            "redis.socket_timeout_seconds": (0, False), "redis.reconnect_delay_seconds": (0, False),
            "logging.max_bytes": (0, False), "logging.backup_count": (0, True),
        }
        integer_keys = {"soap.max_attempts", "soap.response_excerpt_chars", "results.log_preview_lines",
                        "results.log_preview_max_chars", "database.port", "database.connect_timeout",
                        "database.max_retries", "redis.port", "redis.db", "logging.max_bytes", "logging.backup_count"}
        for key, (minimum, inclusive) in numeric.items():
            value = self.get(key, _MISSING)
            if value is _MISSING:
                continue
            try:
                number = float(value)
                valid = not isinstance(value, bool) and math.isfinite(number)
                valid = valid and (number >= minimum if inclusive else number > minimum)
                if key in integer_keys:
                    valid = valid and number.is_integer() and int(value) == number
                if key in ("database.port", "redis.port"):
                    valid = valid and number <= 65535
                if not valid:
                    raise ValueError
            except (TypeError, ValueError, OverflowError):
                raise ConfigError(f"Invalid numeric setting: {key}") from None

        boolean_keys = ("app.auto_start", "staging.use_temp_suffix", "soap.verify_ssl",
                        "soap.extra_params_from_db", "soap.retry_on_transport_error", "soap.retry_on_fault",
                        "soap.record_errors_in_db", "results.skip_blank_lines", "results.parse_json_lines",
                        "processing.retry_failed_on_resume", "database.auto_create_tables",
                        "redis.retain_last_values", "logging.console", "logging.publish_to_redis")
        for key in boolean_keys:
            value = self.get(key, _MISSING)
            if value is not _MISSING and not isinstance(value, bool):
                raise ConfigError(f"Config key '{key}' must be a JSON boolean")

        regexes = {"rename.pattern": [self.get("rename.pattern")]}
        regexes["soap.not_found_patterns"] = self.get("soap.not_found_patterns", [])
        regexes["soap.success_patterns"] = self.get("soap.success_patterns", [])
        for key in ("results.success_regex", "results.failure_regex"):
            if self.get(key):
                regexes[key] = [self.get(key)]
        for key, patterns in regexes.items():
            if not isinstance(patterns, list) or any(not isinstance(p, str) for p in patterns):
                raise ConfigError(f"Config key '{key}' must contain string regular expressions")
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
            if not isinstance(spec['name'], str) or not isinstance(spec['glob'], str):
                raise ConfigError("Result names and globs must be strings")
            for key in ("required", "check_status", "fail_if_nonempty"):
                if key in spec and not isinstance(spec[key], bool):
                    raise ConfigError(f"Result option '{key}' must be a JSON boolean")
        if len({s['name'] for s in files}) != len(files):
            raise ConfigError("Result file names must be unique")
        if not any(s.get('required', True) for s in files):
            raise ConfigError("At least one result file must be required")

        params = self.section("soap.params")
        placeholders = set(re.findall(r"\{\{\s*(\w+)\s*\}\}", self.get("soap.envelope_template")))
        if not self.get("soap.extra_params_from_db", False):
            for key in placeholders - {"file_name", "message_seq"}:
                if params.get(key) is None or not str(params[key]).strip():
                    raise ConfigError(f"Missing SOAP parameter: soap.params.{key}")
        try:
            template_root = ET.fromstring(self.get("soap.envelope_template"))
        except ET.ParseError as exc:
            raise ConfigError("soap.envelope_template must be valid XML") from exc
        namespace = ("http://www.w3.org/2003/05/soap-envelope" if str(self.get("soap.soap_version", "1.1")) == "1.2"
                     else "http://schemas.xmlsoap.org/soap/envelope/")
        if template_root.tag != f"{{{namespace}}}Envelope" or template_root.find(f"{{{namespace}}}Body") is None:
            raise ConfigError("SOAP template namespace/Body must match soap.soap_version")
        for key in ("soap.result_code_path", "soap.result_desc_path"):
            value = self.get(key, "")
            if not isinstance(value, str):
                raise ConfigError(f"{key} must be a string")
            if value:
                try:
                    ET.Element("Body").findall(value)
                except (SyntaxError, KeyError, TypeError):
                    raise ConfigError(f"Invalid XML path: {key}") from None
        codes = self.get("soap.success_codes", ["0"])
        if not isinstance(codes, list) or not codes or any(not isinstance(c, str) or not c for c in codes):
            raise ConfigError("soap.success_codes must be a nonempty list of strings")

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
