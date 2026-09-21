"""Sub-millisecond load and idle cycling for a running stress payload."""

from __future__ import annotations

import logging
import os
import random
import signal
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from corecycler.engine.backends.base import DutyCycle


log = logging.getLogger(__name__)

_MICRO_PERIOD_SECONDS = 0.010
_MIN_RANDOM_PHASE_MS = 80.0
_MAX_RANDOM_PHASE_MS = 2000.0
_REALTIME_PRIORITY = 1


class DutyCycleDriver:
    """Modulate one process group without participating in its verdict."""

    def __init__(self, duty_cycle: DutyCycle, pgid: int, rng: random.Random | None = None) -> None:
        self.duty_cycle = duty_cycle
        self.pgid = pgid
        self.rng = rng if rng is not None else random.Random()
        self.cycles_completed = 0
        self.stopped_early = False
        self.control_error: Exception | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._idle_lock = threading.Lock()
        self._idle_started_at: float | None = None
        self._completed_idle_seconds = 0.0

    def start(self) -> None:
        """Start modulating the payload in a daemon thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self.stopped_early = False
        self.control_error = None
        thread = threading.Thread(target=self._run, name=f"duty-cycle-{self.pgid}", daemon=True)
        thread.start()
        self._thread = thread

    def stop(self) -> None:
        """Stop modulation and leave the payload process group runnable."""
        self._stop_event.set()
        try:
            if self._thread is not None:
                self._thread.join()
        finally:
            self._continue_payload()
        if self.control_error is not None:
            raise RuntimeError(f"duty-cycle control failed: {self.control_error}") from self.control_error

    def _run(self) -> None:
        try:
            self._set_realtime_priority()
            if self.duty_cycle.random_phases:
                self._run_random_phases()
            else:
                self._run_fixed()
        except ProcessLookupError:
            self.stopped_early = True
        except Exception as exc:
            self.stopped_early = True
            self.control_error = exc
            log.exception("Duty-cycle driver stopped unexpectedly: %s", exc)
        finally:
            self._continue_payload()

    def _run_fixed(self) -> None:
        deadline = time.monotonic()
        idle_seconds = self.duty_cycle.idle_us / 1_000_000
        burst_seconds = self.duty_cycle.burst_us / 1_000_000
        while not self._stop_event.is_set():
            self._stop_payload()
            deadline += idle_seconds
            self._sleep_until(deadline)
            self._continue_payload()
            if self._stop_event.is_set():
                return
            deadline += burst_seconds
            self._sleep_until(deadline)
            self.cycles_completed += 1

    def _run_random_phases(self) -> None:
        deadline = time.monotonic()
        while not self._stop_event.is_set():
            phase_seconds = self.rng.uniform(_MIN_RANDOM_PHASE_MS, _MAX_RANDOM_PHASE_MS) / 1000
            duty_fraction = self.rng.uniform(0.0, 100.0) / 100
            phase_deadline = deadline + phase_seconds
            while deadline < phase_deadline and not self._stop_event.is_set():
                period = min(_MICRO_PERIOD_SECONDS, phase_deadline - deadline)
                idle_seconds = period * (1.0 - duty_fraction)
                burst_seconds = period * duty_fraction
                self._stop_payload()
                deadline += idle_seconds
                self._sleep_until(deadline)
                self._continue_payload()
                if self._stop_event.is_set():
                    return
                deadline += burst_seconds
                self._sleep_until(deadline)
                self.cycles_completed += 1

    def _sleep_until(self, deadline: float) -> None:
        self._stop_event.wait(max(0.0, deadline - time.monotonic()))

    def _stop_payload(self) -> None:
        os.killpg(self.pgid, signal.SIGSTOP)
        with self._idle_lock:
            self._idle_started_at = time.monotonic()

    def _continue_payload(self) -> None:
        try:
            os.killpg(self.pgid, signal.SIGCONT)
        except ProcessLookupError:
            self.stopped_early = True
            self._close_idle_interval()
        except OSError as exc:
            self.stopped_early = True
            self.control_error = exc
            log.warning("Could not resume duty-cycled process group %d: %s", self.pgid, exc)
        else:
            self._close_idle_interval()

    def _close_idle_interval(self) -> None:
        with self._idle_lock:
            if self._idle_started_at is not None:
                self._completed_idle_seconds += time.monotonic() - self._idle_started_at
                self._idle_started_at = None

    @property
    def intentional_idle_seconds(self) -> float:
        with self._idle_lock:
            if self._idle_started_at is None:
                return self._completed_idle_seconds
            return self._completed_idle_seconds + time.monotonic() - self._idle_started_at

    @staticmethod
    def _set_realtime_priority() -> None:
        try:
            os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(_REALTIME_PRIORITY))
        except OSError as exc:
            log.debug("Duty-cycle thread could not use SCHED_FIFO: %s", exc)
