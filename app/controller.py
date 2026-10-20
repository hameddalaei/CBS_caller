"""Start / stop / pause / resume state shared between the Redis listener and the worker."""

import threading


class StopRequested(Exception):
    """Raised inside the worker when a stop (or shutdown) was requested."""


class Controller:
    VALID_COMMANDS = ("start", "resume", "pause", "stop", "shutdown")

    def __init__(self):
        self._cv = threading.Condition()
        self._start = False
        self._pause = False
        self._stop = False
        self._shutdown = False
        self._running = False
        self.on_pause = None   # called (worker thread) when the worker actually pauses
        self.on_resume = None  # called (worker thread) when the worker continues

    # -- called from the control listener thread ---------------------------
    def handle(self, command):
        """Apply a control command. Returns False if it was ignored."""
        with self._cv:
            if command in ("start", "resume"):
                self._pause = False
                if not self._running:
                    self._stop = False
                    self._start = True
            elif command == "pause":
                if not self._running:
                    return False
                self._pause = True
            elif command == "stop":
                self._stop = True
                self._pause = False
                self._start = False
            elif command == "shutdown":
                self._shutdown = True
                self._stop = True
                self._pause = False
                self._start = False
            else:
                return False
            self._cv.notify_all()
            return True

    # -- called from the worker thread -------------------------------------
    @property
    def shutdown_requested(self):
        with self._cv:
            return self._shutdown

    def set_running(self, running):
        with self._cv:
            self._running = running

    def wait_for_start(self, timeout):
        with self._cv:
            if not self._start and not self._shutdown:
                self._cv.wait(timeout)
            if self._shutdown:
                return False
            if self._start:
                self._start = False
                return True
            return False

    def guard(self):
        """Safe point: blocks while paused, raises StopRequested when stopping."""
        announced = False
        while True:
            with self._cv:
                if self._stop:
                    raise StopRequested()
                paused = self._pause
            if not paused:
                break
            if not announced:
                announced = True
                if self.on_pause:
                    self.on_pause()
            with self._cv:
                if self._pause and not self._stop:
                    self._cv.wait(0.5)
        if announced and self.on_resume:
            self.on_resume()

    def sleep(self, seconds):
        """Interruptible sleep that honours pause and stop. Paused time is not counted."""
        remaining = float(seconds)
        while remaining > 0:
            self.guard()
            step = min(1.0, remaining)
            with self._cv:
                if not self._stop and not self._pause:
                    self._cv.wait(step)
            remaining -= step
        self.guard()
