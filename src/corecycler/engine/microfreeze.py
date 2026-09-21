"""Scheduling-hitch telemetry for preserving pre-freeze context."""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from corecycler.config.paths import atomic_write

if TYPE_CHECKING:
    from pathlib import Path


log = logging.getLogger(__name__)

_SAMPLE_SECONDS = 0.001
_BREADCRUMB_SECONDS = 1.0
_WINDOW_SECONDS = 60.0
_MAX_WINDOW_SAMPLES = 60_000
_REALTIME_PRIORITY = 1


@dataclass(frozen=True, slots=True)
class Hitch:
    monotonic_ts: float
    latency_ms: float


class MicroFreezeMonitor:
    """Record scheduler hitches without interpreting them as instability."""

    def __init__(self, breadcrumb_path: Path, *, threshold_ms: float = 15.0, cpu: int | None = None) -> None:
        self.threshold_ms = threshold_ms
        self.cpu = cpu
        self.breadcrumb_path = breadcrumb_path
        self._context = ""
        self._hitches: deque[Hitch] = deque(maxlen=_MAX_WINDOW_SAMPLES)
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start monitoring in a daemon thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name="micro-freeze-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> bool:
        """Stop monitoring and report whether its writer terminated."""
        self._stop_event.set()
        if self._thread is None:
            return True
        self._thread.join(timeout=2.0)
        if self._thread.is_alive():
            return False
        self._thread = None
        return True

    def set_context(self, context: str) -> None:
        with self._lock:
            self._context = context

    def hitches(self) -> tuple[Hitch, ...]:
        now = time.monotonic()
        with self._lock:
            self._prune_locked(now)
            return tuple(self._hitches)

    def worst_ms(self) -> float:
        return max((hitch.latency_ms for hitch in self.hitches()), default=0.0)

    def _run(self) -> None:
        self._configure_thread()
        next_breadcrumb = time.monotonic() + _BREADCRUMB_SECONDS
        while not self._stop_event.is_set():
            started = time.monotonic()
            time.sleep(_SAMPLE_SECONDS)
            finished = time.monotonic()
            latency_ms = (finished - started) * 1000
            with self._lock:
                if latency_ms > self.threshold_ms:
                    self._hitches.append(Hitch(finished, latency_ms))
                self._prune_locked(finished)
            if finished >= next_breadcrumb:
                self._write_breadcrumb()
                next_breadcrumb = finished + _BREADCRUMB_SECONDS

    def _configure_thread(self) -> None:
        if self.cpu is not None:
            try:
                os.sched_setaffinity(0, {self.cpu})
            except OSError as exc:
                log.debug("Micro-freeze thread could not pin to CPU %d: %s", self.cpu, exc)
        try:
            os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(_REALTIME_PRIORITY))
        except OSError as exc:
            log.debug("Micro-freeze thread could not use SCHED_FIFO: %s", exc)

    def _prune_locked(self, now: float) -> None:
        cutoff = now - _WINDOW_SECONDS
        while self._hitches and self._hitches[0].monotonic_ts < cutoff:
            self._hitches.popleft()

    def _write_breadcrumb(self) -> None:
        with self._lock:
            context = self._context
            worst_ms = max((hitch.latency_ms for hitch in self._hitches), default=0.0)
        content = f"timestamp={datetime.now(UTC).isoformat()}\ncontext={context}\nworst_latency_ms={worst_ms:.3f}\n"
        try:
            atomic_write(self.breadcrumb_path, content, durable=True)
        except OSError as exc:
            log.warning("Could not write micro-freeze breadcrumb %s: %s", self.breadcrumb_path, exc)
