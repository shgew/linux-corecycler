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
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start modulating the payload in a daemon thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self.stopped_early = False
        self._thread = threading.Thread(target=self._run, name=f"duty-cycle-{self.pgid}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop modulation and leave the payload process group runnable."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join()
        self._continue_payload()

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
            log.exception("Duty-cycle driver stopped unexpectedly: %s", exc)
        finally:
            self._continue_payload()

    def _run_fixed(self) -> None:
        deadline = time.monotonic()
        idle_seconds = self.duty_cycle.idle_us / 1_000_000
        burst_seconds = self.duty_cycle.burst_us / 1_000_000
        while not self._stop_event.is_set():
            os.killpg(self.pgid, signal.SIGSTOP)
            deadline += idle_seconds
            self._sleep_until(deadline)
            os.killpg(self.pgid, signal.SIGCONT)
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
                os.killpg(self.pgid, signal.SIGSTOP)
                deadline += idle_seconds
                self._sleep_until(deadline)
                os.killpg(self.pgid, signal.SIGCONT)
                if self._stop_event.is_set():
                    return
                deadline += burst_seconds
                self._sleep_until(deadline)
                self.cycles_completed += 1

    def _sleep_until(self, deadline: float) -> None:
        clock_nanosleep = getattr(time, "clock_nanosleep", None)
        if clock_nanosleep is not None:
            clock_nanosleep(time.CLOCK_MONOTONIC, getattr(time, "TIMER_ABSTIME", 1), deadline)
            return
        time.sleep(max(0.0, deadline - time.monotonic()))

    def _continue_payload(self) -> None:
        try:
            os.killpg(self.pgid, signal.SIGCONT)
        except ProcessLookupError:
            self.stopped_early = True
        except OSError as exc:
            log.warning("Could not resume duty-cycled process group %d: %s", self.pgid, exc)

    @staticmethod
    def _set_realtime_priority() -> None:
        try:
            os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(_REALTIME_PRIORITY))
        except OSError as exc:
            log.debug("Duty-cycle thread could not use SCHED_FIFO: %s", exc)
