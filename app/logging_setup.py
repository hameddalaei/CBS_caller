"""Logging: console, rotating file and a Redis log channel."""

import logging
import threading
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path


class LogContext:
    """Thread-safe key/value context (run id, current file) attached to every record."""

    _lock = threading.Lock()
    _values = {}

    @classmethod
    def set(cls, **kwargs):
        with cls._lock:
            cls._values.update(kwargs)

    @classmethod
    def clear(cls, *keys):
        with cls._lock:
            if not keys:
                cls._values.clear()
            for key in keys:
                cls._values.pop(key, None)

    @classmethod
    def snapshot(cls):
        with cls._lock:
            return dict(cls._values)


class ContextFilter(logging.Filter):
    def filter(self, record):
        snap = LogContext.snapshot()
        record.run_id = snap.get("run_id")
        record.file = snap.get("file")
        parts = []
        if record.run_id is not None:
            parts.append(f"run {record.run_id}")
        if record.file:
            parts.append(str(record.file))
        record.ctx = f"[{' | '.join(parts)}] " if parts else ""
        return True


class RedisLogHandler(logging.Handler):
    """Publishes every log record as JSON to the Redis logs channel."""

    def __init__(self, bus):
        super().__init__()
        self._bus = bus

    def emit(self, record):
        # The redis bus reports its own problems on stderr; skip to avoid loops.
        if record.name.startswith("fal.redis"):
            return
        try:
            payload = {
                "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
                "level": record.levelname,
                "logger": record.name,
                "run_id": getattr(record, "run_id", None),
                "file": getattr(record, "file", None),
                "message": record.getMessage(),
            }
            if record.exc_info:
                payload["exception"] = self.formatException(record.exc_info)
            self._bus.publish_log(payload)
        except Exception:  # noqa: BLE001
            pass


def setup_logging(cfg, bus):
    c = cfg.section("logging")
    level = getattr(logging, str(c.get("level", "INFO")).upper(), logging.INFO)

    logger = logging.getLogger("fal")
    logger.setLevel(level)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(ctx)s%(message)s")
    ctx_filter = ContextFilter()

    if c.get("console", True):
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        console.addFilter(ctx_filter)
        logger.addHandler(console)

    log_file = c.get("file")
    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            path,
            maxBytes=int(c.get("max_bytes", 10 * 1024 * 1024)),
            backupCount=int(c.get("backup_count", 5)),
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        file_handler.addFilter(ctx_filter)
        logger.addHandler(file_handler)

    if c.get("publish_to_redis", True):
        redis_handler = RedisLogHandler(bus)
        redis_handler.addFilter(ctx_filter)
        logger.addHandler(redis_handler)

    return logger
