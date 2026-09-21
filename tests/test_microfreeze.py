"""Behavior tests for non-verdict micro-freeze telemetry."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from corecycler.engine import microfreeze
from corecycler.engine.microfreeze import Hitch, MicroFreezeMonitor

_REAL_START = MicroFreezeMonitor.start
_REAL_STOP = MicroFreezeMonitor.stop


@pytest.fixture(autouse=True)
def real_freeze_monitor(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite-wide guard stubs the thread out; this module is what tests it."""
    monkeypatch.setattr(MicroFreezeMonitor, "start", _REAL_START)
    monkeypatch.setattr(MicroFreezeMonitor, "stop", _REAL_STOP)


if TYPE_CHECKING:
    from pathlib import Path


def _run_with_latencies(
    monitor: MicroFreezeMonitor,
    monkeypatch: pytest.MonkeyPatch,
    latencies: list[float],
) -> None:
    now = 0.0
    remaining = iter(latencies)

    def monotonic() -> float:
        return now

    def sleep(_seconds: float) -> None:
        nonlocal now
        try:
            now += next(remaining)
        except StopIteration:
            monitor._stop_event.set()

    monitor._configure_thread = lambda: None  # type: ignore[method-assign]
    monkeypatch.setattr(microfreeze.time, "monotonic", monotonic)
    monkeypatch.setattr(microfreeze.time, "sleep", sleep)
    monitor._run()


def test_only_hitches_over_threshold_are_recorded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monitor = MicroFreezeMonitor(tmp_path / "breadcrumb", threshold_ms=15.0)

    _run_with_latencies(monitor, monkeypatch, [0.020, 0.005])

    assert monitor.hitches() == (Hitch(monotonic_ts=0.020, latency_ms=20.0),)
    assert monitor.worst_ms() == 20.0


def test_hitch_window_drops_entries_older_than_sixty_seconds(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monitor = MicroFreezeMonitor(tmp_path / "breadcrumb")
    monitor._hitches.extend((Hitch(1.0, 20.0), Hitch(2.0, 30.0)))
    monkeypatch.setattr(microfreeze.time, "monotonic", lambda: 61.5)

    assert monitor.hitches() == (Hitch(2.0, 30.0),)
    assert monitor._hitches.maxlen == 60_000


def test_periodic_breadcrumb_contains_context_and_worst_latency(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "microfreeze.breadcrumb"
    monitor = MicroFreezeMonitor(path)
    monitor.set_context("core=3 offsets=0:-20,3:-35")

    _run_with_latencies(monitor, monkeypatch, [1.001])

    content = path.read_text(encoding="utf-8")
    assert "timestamp=" in content
    assert "context=core=3 offsets=0:-20,3:-35" in content
    assert "worst_latency_ms=1001.000" in content


def test_breadcrumb_write_error_is_logged_and_swallowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def fail_write(_path: Path, _content: str, *, durable: bool) -> None:
        assert durable
        raise OSError("disk unavailable")

    monkeypatch.setattr(microfreeze, "atomic_write", fail_write)
    monitor = MicroFreezeMonitor(tmp_path / "breadcrumb")

    with caplog.at_level(logging.WARNING):
        monitor._write_breadcrumb()

    assert "disk unavailable" in caplog.text


def test_monitor_start_is_idempotent_and_stop_joins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monitor = MicroFreezeMonitor(tmp_path / "breadcrumb")
    monkeypatch.setattr(monitor, "_configure_thread", lambda: None)

    monitor.start()
    thread = monitor._thread
    monitor.start()
    assert monitor._thread is thread, "a second start must not spawn a second thread"
    monitor.stop()

    assert thread is not None
    assert thread.daemon
    assert not thread.is_alive()


def test_timed_out_writer_remains_owned_and_blocks_replacement(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    threads: list[Mock] = []

    def make_thread(**_kwargs: object) -> Mock:
        thread = Mock()
        thread.is_alive.return_value = True
        threads.append(thread)
        return thread

    monkeypatch.setattr(microfreeze.threading, "Thread", make_thread)
    monitor = MicroFreezeMonitor(tmp_path / "breadcrumb")

    monitor.start()
    writer = threads[0]
    stop_event = monitor._stop_event
    monitor.stop()

    writer.join.assert_called_once_with(timeout=2.0)
    assert monitor._thread is writer
    assert stop_event.is_set()

    monitor.start()
    assert threads == [writer]
    assert monitor._stop_event is stop_event
    assert stop_event.is_set()

    writer.is_alive.return_value = False
    monitor.start()

    assert len(threads) == 2
    assert monitor._thread is threads[1]
    assert monitor._stop_event is not stop_event
    assert not monitor._stop_event.is_set()


def test_thread_configuration_is_best_effort(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def fail_affinity(_pid: int, _cpus: set[int]) -> None:
        raise OSError("affinity unavailable")

    def fail_scheduler(_pid: int, _policy: int, _param: object) -> None:
        raise PermissionError("scheduler unavailable")

    monkeypatch.setattr(microfreeze.os, "sched_setaffinity", fail_affinity)
    monkeypatch.setattr(microfreeze.os, "sched_setscheduler", fail_scheduler)
    monitor = MicroFreezeMonitor(tmp_path / "breadcrumb", cpu=7)

    with caplog.at_level(logging.DEBUG):
        monitor._configure_thread()

    assert "affinity unavailable" in caplog.text
    assert "scheduler unavailable" in caplog.text
