"""Redis publisher (logs / status / progress) and control-channel subscriber."""

import json
import sys
import threading
import time
from datetime import datetime, timezone

import redis


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_command(raw):
    """Accept either a plain string ('pause') or JSON ({"command": "pause"})."""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    text = text.strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            text = obj.get("command") or obj.get("cmd") or ""
        elif isinstance(obj, str):
            text = obj
    except ValueError:
        pass
    return str(text).strip().lower()


class RedisBus:
    def __init__(self, cfg):
        c = cfg.section("redis")
        self._host = c["host"]
        self._port = int(c.get("port", 6379))
        self._db = int(c.get("db", 0))
        self._password = c.get("password") or None
        self._socket_timeout = float(c.get("socket_timeout_seconds", 5))
        self._reconnect_delay = float(c.get("reconnect_delay_seconds", 3))
        self._retain = bool(c.get("retain_last_values", True))
        ch = c.get("channels", {})
        self.ch_control = ch["control"]
        self.ch_logs = ch["logs"]
        self.ch_status = ch["status"]
        self.ch_progress = ch["progress"]
        self._client = None
        self._lock = threading.Lock()
        self._last_warn = 0.0

    # -- connections -------------------------------------------------------
    def _new_client(self, socket_timeout):
        return redis.Redis(
            host=self._host,
            port=self._port,
            db=self._db,
            password=self._password,
            socket_timeout=socket_timeout,
            socket_connect_timeout=self._socket_timeout,
            health_check_interval=30,
            decode_responses=True,
        )

    def _publisher(self):
        with self._lock:
            if self._client is None:
                self._client = self._new_client(self._socket_timeout)
            return self._client

    def _warn(self, message):
        now = time.monotonic()
        if now - self._last_warn > 30:
            self._last_warn = now
            print(f"[redis] {message}", file=sys.stderr, flush=True)

    # -- publishing --------------------------------------------------------
    def publish(self, channel, payload, retain=False):
        """Publish a JSON payload. Never raises: Redis problems must not stop the work."""
        try:
            data = json.dumps(payload, ensure_ascii=False, default=str)
            client = self._publisher()
            client.publish(channel, data)
            if retain and self._retain:
                client.set(f"{channel}:last", data)
            return True
        except Exception as exc:  # noqa: BLE001 - deliberately broad
            with self._lock:
                self._client = None
            self._warn(f"publish to '{channel}' failed: {exc}")
            return False

    def publish_log(self, payload):
        return self.publish(self.ch_logs, payload)

    def publish_status(self, payload):
        return self.publish(self.ch_status, payload, retain=True)

    def publish_progress(self, payload):
        return self.publish(self.ch_progress, payload, retain=True)

    # -- control subscription ---------------------------------------------
    def start_control_listener(self, callback, stop_event):
        """Run a daemon thread that calls callback(command) for each control message."""

        def loop():
            while not stop_event.is_set():
                pubsub = None
                try:
                    client = self._new_client(None)
                    pubsub = client.pubsub(ignore_subscribe_messages=True)
                    pubsub.subscribe(self.ch_control)
                    while not stop_event.is_set():
                        message = pubsub.get_message(timeout=1.0)
                        if message and message.get("type") == "message":
                            command = parse_command(message["data"])
                            if command:
                                try:
                                    callback(command)
                                except Exception as exc:  # noqa: BLE001
                                    self._warn(f"control callback failed: {exc}")
                except Exception as exc:  # noqa: BLE001
                    self._warn(f"control subscription error: {exc}")
                    stop_event.wait(self._reconnect_delay)
                finally:
                    if pubsub is not None:
                        try:
                            pubsub.close()
                        except Exception:  # noqa: BLE001
                            pass

        thread = threading.Thread(target=loop, name="redis-control", daemon=True)
        thread.start()
        return thread
