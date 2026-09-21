"""Behavior tests for sub-millisecond duty cycling and its execution wiring."""

from __future__ import annotations

import logging
import os
import random
import signal
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from corecycler.engine import duty, execution
from corecycler.engine.backends.base import DutyCycle, StressConfig, StressResult
from corecycler.engine.duty import DutyCycleDriver
from corecycler.engine.execution import Lane, _LaneRun
from corecycler.engine.scheduler import CoreScheduler, CoreTestStatus, SchedulerConfig

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path


@pytest.fixture
def busy_child() -> Iterator[subprocess.Popen[bytes]]:
    proc = subprocess.Popen(
        [sys.executable, "-c", "while True: pass"],
        start_new_session=True,
    )
    try:
        yield proc
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=1)


# 10s, not 1s: under `-n auto` the box runs ~16 workers plus this test's own
# busy-loop children, so a driver thread can wait far longer than it does idle.
def _wait_for_child_state(proc: subprocess.Popen[bytes], predicate: Callable[[int], bool]) -> int:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        pid, status = os.waitpid(proc.pid, os.WNOHANG | os.WUNTRACED | os.WCONTINUED)
        if pid and predicate(status):
            return status
        time.sleep(0.001)
    raise AssertionError("child did not reach the expected signal state")


def test_real_child_is_suspended_and_stop_resumes_it(busy_child: subprocess.Popen[bytes]) -> None:
    driver = DutyCycleDriver(DutyCycle(burst_us=100_000, idle_us=100_000), busy_child.pid)

    driver.start()
    thread = driver._thread
    driver.start()
    stopped = _wait_for_child_state(busy_child, os.WIFSTOPPED)
    continued = _wait_for_child_state(busy_child, os.WIFCONTINUED)
    driver.stop()

    assert os.WSTOPSIG(stopped) == signal.SIGSTOP
    assert os.WIFCONTINUED(continued)
    assert busy_child.poll() is None
    assert driver._thread is thread
    assert driver.cycles_completed >= 1
    assert not driver.stopped_early


def test_stop_resumes_child_after_driver_exception(
    busy_child: subprocess.Popen[bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    release_failure = threading.Event()

    def fail_sleep(*_args: object) -> None:
        assert release_failure.wait(1.0)
        raise RuntimeError("clock failed")

    driver = DutyCycleDriver(DutyCycle(), busy_child.pid)
    monkeypatch.setattr(driver, "_sleep_until", fail_sleep)

    driver.start()
    stopped = _wait_for_child_state(busy_child, os.WIFSTOPPED)
    release_failure.set()
    continued = _wait_for_child_state(busy_child, os.WIFCONTINUED)
    driver.stop()

    assert os.WIFSTOPPED(stopped)
    assert os.WIFCONTINUED(continued)
    assert busy_child.poll() is None
    assert driver.stopped_early


def test_exited_child_stops_cleanly() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    proc.wait(timeout=1)
    driver = DutyCycleDriver(DutyCycle(), proc.pid)

    driver.start()
    driver.stop()

    assert driver.stopped_early


def test_seeded_random_phases_are_reproducible(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(duty.os, "killpg", lambda _pgid, _sig: None)

    def trace(seed: int) -> tuple[list[float], int]:
        driver = DutyCycleDriver(DutyCycle(random_phases=True), 123, random.Random(seed))
        deadlines: list[float] = []
        now = 0.0

        def monotonic() -> float:
            return now

        def sleep_until(deadline: float) -> None:
            nonlocal now
            deadlines.append(deadline)
            now = deadline
            if len(deadlines) == 19:
                driver._stop_event.set()

        monkeypatch.setattr(duty.time, "monotonic", monotonic)
        driver._sleep_until = sleep_until  # type: ignore[method-assign]
        driver._set_realtime_priority = lambda: None  # type: ignore[method-assign]
        driver._run()
        return deadlines, driver.cycles_completed

    assert trace(17) == trace(17)
    assert trace(17) != trace(18)
    assert trace(17)[1] == 9


def test_stop_during_idle_resumes_without_waiting_for_a_burst(monkeypatch: pytest.MonkeyPatch) -> None:
    signals: list[signal.Signals] = []
    driver = DutyCycleDriver(DutyCycle(), 123)
    monkeypatch.setattr(duty.os, "killpg", lambda _pgid, sent: signals.append(sent))

    def stop_during_sleep(_deadline: float) -> None:
        driver._stop_event.set()

    driver._sleep_until = stop_during_sleep  # type: ignore[method-assign]
    driver._run_fixed()

    assert signals == [signal.SIGSTOP, signal.SIGCONT]
    assert driver.cycles_completed == 0


def test_stop_interrupts_a_long_idle_wait(busy_child: subprocess.Popen[bytes]) -> None:
    driver = DutyCycleDriver(DutyCycle(burst_us=1, idle_us=60_000_000), busy_child.pid)
    stopped = threading.Event()

    def stop_driver() -> None:
        driver.stop()
        stopped.set()

    driver.start()
    _wait_for_child_state(busy_child, os.WIFSTOPPED)
    stop_thread = threading.Thread(target=stop_driver, daemon=True)
    stop_thread.start()

    assert stopped.wait(0.5)
    assert os.WIFCONTINUED(_wait_for_child_state(busy_child, os.WIFCONTINUED))


def test_resume_os_error_is_telemetry_only(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    def fail_resume(_pgid: int, _sig: signal.Signals) -> None:
        raise PermissionError("resume denied")

    monkeypatch.setattr(duty.os, "killpg", fail_resume)
    driver = DutyCycleDriver(DutyCycle(), 123)

    with caplog.at_level(logging.WARNING):
        driver.stop()

    assert "resume denied" in caplog.text


def test_intentional_idle_clock_includes_completed_and_open_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = iter([10.0, 12.0, 14.0, 20.0, 23.0])
    driver = DutyCycleDriver(DutyCycle(), 123)
    monkeypatch.setattr(duty.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(duty.os, "killpg", lambda _pgid, _signal: None)

    driver._stop_payload()
    assert driver.intentional_idle_seconds == 2.0
    driver._continue_payload()
    assert driver.intentional_idle_seconds == 4.0
    driver._stop_payload()
    assert driver.intentional_idle_seconds == 7.0


def test_execution_starts_and_stops_driver_with_payload(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    events: list[str] = []

    class FakeDriver:
        def __init__(self, config: DutyCycle, pgid: int) -> None:
            assert config == DutyCycle()
            assert pgid == 4321

        def start(self) -> None:
            events.append("start")

        def stop(self) -> None:
            events.append("stop")

    class FakeProcess:
        pid = 4321
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

    process = FakeProcess()
    backend = MagicMock(unsafe=True)
    backend.get_command.return_value = ["payload"]
    supervisor = execution.Supervisor.__new__(execution.Supervisor)
    supervisor.backend = backend
    supervisor._containment_for = lambda _cpus: None
    supervisor.stop_event = threading.Event()
    supervisor.stop_on_first_failure = False
    supervisor.detector = MagicMock()
    supervisor.observed = []
    supervisor._drain = lambda _run: None
    supervisor._apply_mce_events = lambda _runs, _start, *, force: None
    supervisor._final_verdict = lambda run, elapsed, interrupted: StressResult(
        core_id=run.lane.core_id,
        passed=True,
        duration_seconds=elapsed,
    )

    def kill(proc: FakeProcess) -> None:
        events.append("kill")
        proc.returncode = -signal.SIGTERM

    monkeypatch.setattr(execution, "DutyCycleDriver", FakeDriver)
    monkeypatch.setattr(execution.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(execution, "kill_process_group", kill)
    monkeypatch.setattr(execution, "reap_zombies", lambda: None)
    run = _LaneRun(Lane(core_id=0, cpus=(0,), work_dir=tmp_path))
    config = StressConfig(duty_cycle=DutyCycle())

    assert supervisor._launch(run, config, time.monotonic())
    supervisor._finish([run], time.monotonic(), 0.0)

    assert events == ["start", "stop", "kill"]


def test_driver_stop_failure_is_a_startup_fault(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class FailingDriver:
        def stop(self) -> None:
            raise RuntimeError("worker did not stop")

    supervisor = execution.Supervisor.__new__(execution.Supervisor)
    supervisor.stop_on_first_failure = False
    supervisor.stop_event = threading.Event()
    run = _LaneRun(Lane(core_id=4, cpus=(4,), work_dir=tmp_path))
    run.duty_driver = FailingDriver()
    monkeypatch.setattr(execution.time, "monotonic", lambda: 13.0)

    supervisor._stop_duty_driver(run, 10.0)

    assert run.verdict == StressResult(
        core_id=4,
        passed=False,
        duration_seconds=3.0,
        error_message="Failed to stop duty-cycle driver: worker did not stop",
        error_type="startup",
    )


def test_driver_start_failure_cannot_bypass_payload_termination(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str | signal.Signals] = []

    class FakeProcess:
        pid = 4321
        returncode: int | None = None

        def poll(self) -> int | None:
            events.append("poll")
            return self.returncode

    process = FakeProcess()
    backend = MagicMock(unsafe=True)
    backend.get_command.return_value = ["payload"]
    supervisor = execution.Supervisor.__new__(execution.Supervisor)
    supervisor.backend = backend
    supervisor._containment_for = lambda _cpus: None
    supervisor.stop_event = threading.Event()
    supervisor.stop_on_first_failure = False
    supervisor.detector = MagicMock()
    supervisor.observed = []
    supervisor._drain = lambda _run: None
    supervisor._apply_mce_events = lambda _runs, _start, *, force: None
    supervisor._final_verdict = lambda run, elapsed, interrupted: StressResult(
        core_id=run.lane.core_id, passed=False, duration_seconds=elapsed
    )

    def fail_start(_thread: threading.Thread) -> None:
        raise RuntimeError("thread unavailable")

    def kill(proc: FakeProcess) -> None:
        events.append("kill")
        proc.returncode = -signal.SIGTERM

    monkeypatch.setattr(duty.threading.Thread, "start", fail_start)
    monkeypatch.setattr(duty.os, "killpg", lambda _pgid, sent: events.append(sent))
    monkeypatch.setattr(execution.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(execution, "kill_process_group", kill)
    monkeypatch.setattr(execution, "reap_zombies", lambda: None)
    run = _LaneRun(Lane(core_id=0, cpus=(0,), work_dir=tmp_path))

    assert not supervisor._launch(run, StressConfig(duty_cycle=DutyCycle()), time.monotonic())
    supervisor._finish([run], time.monotonic(), 0.0)

    assert signal.SIGCONT in events
    assert events.index(signal.SIGCONT) < events.index("kill")


def test_exit_is_observed_before_reaping_and_modulation_stops_before_poll(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []

    class FakeProcess:
        pid = 4321
        returncode: int | None = None

        def poll(self) -> int | None:
            events.append("poll")
            self.returncode = 0
            return self.returncode

    class FakeDriver:
        def stop(self) -> None:
            events.append("stop")

    supervisor = execution.Supervisor.__new__(execution.Supervisor)
    supervisor.backend = MagicMock(unsafe=True)
    supervisor.backend.poll_errors.return_value = None
    supervisor.backend.parse_output.return_value = (True, None)
    supervisor.stop_on_first_failure = False
    supervisor.stall_timeout = 30.0
    supervisor.hooks = execution.SuperviseHooks()
    supervisor._drain = lambda _run: None
    run = _LaneRun(Lane(core_id=0, cpus=(0,), work_dir=tmp_path))
    run.proc = FakeProcess()
    run.duty_driver = FakeDriver()  # type: ignore[assignment]
    monkeypatch.setattr(execution.os, "waitid", lambda *_args: object())

    assert not supervisor._poll_exits_stalls_watchdog([run], time.monotonic())
    assert events == ["stop", "poll"]


def test_stall_clock_excludes_observed_duty_cycle_idle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class FakeDriver:
        intentional_idle_seconds = 0.0

    supervisor = execution.Supervisor.__new__(execution.Supervisor)
    supervisor.stall_timeout = 1.0
    run = _LaneRun(Lane(core_id=0, cpus=(0,), work_dir=tmp_path))
    driver = FakeDriver()
    run.duty_driver = driver  # type: ignore[assignment]
    monkeypatch.setattr(
        execution,
        "cpu_times",
        MagicMock(side_effect=[(0, 100), (100, 200), (200, 300)]),
    )

    assert not supervisor._is_stalled(run, 0.0)
    driver.intentional_idle_seconds = 99.0
    assert not supervisor._is_stalled(run, 100.0)
    assert supervisor._is_stalled(run, 102.0)


def test_process_group_is_not_signaled_after_wait_reaps_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[signal.Signals] = []

    class FakeProcess:
        pid = 4321
        returncode: int | None = None
        stdout = None
        stderr = None

        def wait(self, timeout: float) -> int:
            assert timeout == 3
            self.returncode = 0
            return 0

    monkeypatch.setattr(execution.os, "getpgid", lambda _pid: 4321)
    monkeypatch.setattr(
        execution.os,
        "killpg",
        lambda _pgid, sent_signal: sent.append(sent_signal),
    )

    execution.kill_process_group(FakeProcess())  # type: ignore[arg-type]

    assert sent == [signal.SIGTERM]


def test_scheduler_threads_duty_cycle_and_skips_legacy_variable_load(tmp_path: Path) -> None:
    duty_cycle = DutyCycle()
    scheduler = CoreScheduler.__new__(CoreScheduler)
    scheduler.config = SchedulerConfig(seconds_per_core=1, variable_load=True, duty_cycle=duty_cycle)
    scheduler.stress_config = StressConfig()
    scheduler._current_core = None
    scheduler.core_status = {0: CoreTestStatus(core_id=0)}
    scheduler.on_core_start = []
    scheduler.on_core_finish = []
    scheduler.on_phase_change = []
    scheduler.results = {0: []}
    scheduler._stop_event = threading.Event()
    scheduler.backend = MagicMock()
    lane = Lane(core_id=0, cpus=(0,), work_dir=tmp_path)
    scheduler._lane_for = lambda _core_id: lane
    scheduler._run_variable_load = MagicMock(side_effect=AssertionError("legacy variable load ran"))

    class PassingSupervisor:
        @staticmethod
        def run(lanes: list[Lane], config_for: object, duration: float) -> dict[int, StressResult]:
            assert duration == 1.0
            config = config_for(lanes[0])  # type: ignore[operator]
            assert config.duty_cycle is duty_cycle
            return {0: StressResult(core_id=0, passed=True, duration_seconds=duration)}

    scheduler._supervisor = lambda _phase: PassingSupervisor()
    scheduler._set_phase = lambda _core_id, _phase: None

    scheduler._test_core(0, 1)

    scheduler._run_variable_load.assert_not_called()
    assert scheduler.results[0][0].passed
