#!/usr/bin/env python3
"""Entry point of the fal file processing service."""

import argparse
import logging
import os
import signal
import sys
import threading

from app import __version__
from app.config import Config, ConfigError
from app.controller import Controller
from app.db import Database
from app.file_manager import FileManager
from app.logging_setup import setup_logging
from app.orchestrator import Orchestrator
from app.redis_bus import RedisBus
from app.result_processor import ResultProcessor
from app.soap_client import SoapClient


def parse_args():
    parser = argparse.ArgumentParser(description="Fal file processing service")
    parser.add_argument(
        "--config",
        default=os.environ.get("CONFIG_PATH", "config.json"),
        help="Path to config.json (default: $CONFIG_PATH or ./config.json)",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    try:
        cfg = Config.load(args.config)
        cfg.validate()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    bus = RedisBus(cfg)
    setup_logging(cfg, bus)
    log = logging.getLogger("fal.main")
    log.info("Starting %s v%s (config: %s)", cfg.get("app.name", "fal-processor"), __version__, cfg.path)

    db = Database(cfg)
    try:
        db.ping()
        db.ensure_schema()
    except Exception as exc:  # noqa: BLE001
        log.error("Cannot initialise the database: %s", exc)
        return 1

    ctrl = Controller()
    orchestrator = Orchestrator(
        cfg, db, bus, ctrl, FileManager(cfg), SoapClient(cfg), ResultProcessor(cfg)
    )

    def on_control(command):
        log.info("Control command received: %s", command)
        if command == "status":
            orchestrator.publish_snapshot()
        elif not ctrl.handle(command):
            log.warning("Control command '%s' ignored in the current state (valid: start, stop, pause, resume, status, shutdown)", command)

    stop_listener = threading.Event()
    bus.start_control_listener(on_control, stop_listener)

    def on_signal(signum, _frame):
        log.info("Signal %s received; shutting down gracefully", signum)
        ctrl.handle("shutdown")

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    try:
        orchestrator.run()
    finally:
        stop_listener.set()
        db.close()
        log.info("Service stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
