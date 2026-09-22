"""The one supervised stress-execution loop every test path runs through."""

from __future__ import annotations

import contextlib
import logging
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from corecycler.config import tools
from corecycler.engine import containment
from corecycler.engine.backends.base import KILLED_BY_US_CODES, StressResult
from corecycler.engine.duty import DutyCycleDriver
from corecycler.monitor.cpu_usage import read_cpu_times
from corecycler.monitor.hwmon import HWMonReader

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from typing import TextIO

    from corecycler.engine.backends.base import StressBackend, StressConfig
    from corecycler.engine.detector import ErrorDetector, MCEEvent

log = logging.getLogger(__name__)

STALL_GRACE_SECONDS = 5.0
ERROR_POLL_INTERVAL = 5.0
WATCHDOG_INTERVAL = 2.0
STARTUP_WINDOW_SECONDS = 2.0
CONTAINMENT_GRACE_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class Lane:
    core_id: int
    cpus: tuple[int, ...]
    work_dir: Path
    sibling_cpus: tuple[int, ...] = ()

    @property
    def cpu_list(self) -> str:
        return containment.cpu_list(self.cpus)

    @property
    def mce_cpus(self) -> tuple[int, ...]:
        return self.sibling_cpus or self.cpus


@dataclass(slots=True)
class _LaneRun:
    lane: Lane
    proc: subprocess.Popen | None = None
    verdict: StressResult | None = None
    started_at: float = 0.0
    last_active: float = 0.0
    last_watchdog: float = 0.0
    prev_times: dict[int, tuple[int, int]] = field(default_factory=dict)
    last_stall_check: float | None = None
    observed_duty_idle_seconds: float = 0.0
    inactive_runnable_seconds: float = 0.0
    unit: str | None = None
    cgroup: str | None = None
    pgid: int | None = None
    stdout: str = ""
    stderr: str = ""
    stdout_file: TextIO | None = None
    stderr_file: TextIO | None = None
    duty_driver: DutyCycleDriver | None = None
    termination: TerminationOutcome | None = None

    @property
    def running(self) -> bool:
        return self.verdict is None and self.proc is not None and self.proc.returncode is None


@dataclass(frozen=True, slots=True)
class TerminationOutcome:
    sent_signals: tuple[int, ...] = ()
    group_gone: bool = True
    scope_gone: bool = True
    stopped_running: bool = False

    @property
    def all_gone(self) -> bool:
        return self.group_gone and self.scope_gone


@dataclass(slots=True)
class SuperviseHooks:
    on_status: Callable[[int, float], None] | None = None
    on_stall: Callable[[int], None] | None = None
    on_thermal: Callable[[float], None] | None = None


class ThermalWatch:
    """Debounced soft limit, instant hard ceiling, hysteresis after a trip."""

    HYSTERESIS = 5.0

    def __init__(
        self,
        *,
        max_temperature: float,
        grace_seconds: float,
        hard_margin: float,
        require_sensor: bool,
        read: Callable[[], float | None] | None = None,
    ) -> None:
        self.max_temperature = max_temperature
        self.grace_seconds = grace_seconds
        self.hard_margin = hard_margin
        self.require_sensor = require_sensor
        self._read = read or read_cpu_temperature
        self.tripped = False
        self._over_since: float | None = None
        self.last_temperature: float | None = None

    def safe(self) -> bool:
        temp = self._read()
        self.last_temperature = temp
        if temp is None:
            return not self.require_sensor and not self.tripped
        limit = self.max_temperature
        if temp >= limit:
            if temp >= limit + self.hard_margin:
                self.tripped = True
                return False
            if self.tripped:
                return False
            now = time.monotonic()
            if self._over_since is None:
                self._over_since = now
            if now - self._over_since >= self.grace_seconds:
                self.tripped = True
                return False
            return True
        self._over_since = None
        if self.tripped:
            if temp < limit - self.HYSTERESIS:
                self.tripped = False
                return True
            return False
        return True


def read_cpu_temperature() -> float | None:
    return HWMonReader().max_cpu_temp()


def _exited_without_reaping(proc: subprocess.Popen) -> bool:
    if proc.returncode is not None:
        return True
    try:
        return os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
    except ChildProcessError:
        return True
    except OSError:
        return False


def _wait_for_exit_without_reaping(proc: subprocess.Popen, timeout: float) -> bool | None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            exited = os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except (ChildProcessError, OSError):
            return None
        if exited is not None:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.05, remaining))


def _process_group_gone(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _wait_for_group_exit(pgid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while not _process_group_gone(pgid):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.05, remaining))
    return True


def kill_process_group(proc: subprocess.Popen, pgid: int | None = None) -> TerminationOutcome:
    if pgid is None:
        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            pgid = proc.pid
    if pgid != proc.pid:
        raise RuntimeError("Refusing to signal an unowned process group")
    running = not _exited_without_reaping(proc)
    sent: list[int] = []
    if not _process_group_gone(pgid):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pgid, signal.SIGTERM)
            sent.append(signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=3.0)
    group_gone = _wait_for_group_exit(pgid, 3.0)
    if not group_gone:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pgid, signal.SIGKILL)
            sent.append(signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=2.0)
        group_gone = _wait_for_group_exit(pgid, 2.0)
    for stream in (proc.stdout, proc.stderr):
        if stream:
            with contextlib.suppress(OSError):
                stream.close()
    return TerminationOutcome(
        tuple(sent),
        group_gone=group_gone,
        stopped_running=running and signal.SIGTERM in sent,
    )


def _kill_scope(unit: str) -> bool:
    resolution = tools.resolve("systemctl")
    if resolution.path is None:
        return False
    command = [str(resolution.path)]
    if os.geteuid() != 0:
        command.append("--user")
    scope = f"{unit}.scope"
    try:
        result = subprocess.run(
            command + ["kill", "--kill-whom=all", "--signal=SIGKILL", scope],
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return "not loaded" in result.stderr.lower()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            state = subprocess.run(
                command + ["show", "--property=ActiveState", "--value", scope],
                capture_output=True,
                text=True,
                timeout=max(0.1, deadline - time.monotonic()),
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if state.returncode != 0:
            missing = state.stderr.lower()
            return "not found" in missing or "not loaded" in missing
        if state.stdout.strip() in {"inactive", "failed"}:
            return True
        time.sleep(0.05)
    return False


def busy_fraction(prev: tuple[int, int] | None, now: tuple[int, int] | None) -> float | None:
    if prev is None or now is None:
        return None
    d_total = now[1] - prev[1]
    if d_total <= 0:
        return None
    return 1.0 - ((now[0] - prev[0]) / d_total)


class Supervisor:
    """Runs one batch of contained lanes to per-lane honest verdicts.

    A lane with no earned verdict when the batch stops early stays None:
    an invented pass would enter the evidence record as a proven offset.
    """

    def __init__(
        self,
        *,
        backend: StressBackend,
        detector: ErrorDetector,
        thermal: ThermalWatch,
        stop_event: threading.Event,
        observed: list[MCEEvent],
        poll_interval: float = 1.0,
        stall_timeout: float = 30.0,
        stop_on_first_failure: bool = True,
        phase: str = "stress",
        hooks: SuperviseHooks | None = None,
        containment_for: Callable[[tuple[int, ...]], containment.Containment | None] | None = None,
        scope_terminator: Callable[[str], bool] | None = None,
    ) -> None:
        self.backend = backend
        self.detector = detector
        self.thermal = thermal
        self.stop_event = stop_event
        self.observed = observed
        self.poll_interval = poll_interval
        self.stall_timeout = stall_timeout
        self.stop_on_first_failure = stop_on_first_failure
        self.phase = phase
        self.hooks = hooks or SuperviseHooks()
        self._containment_for = containment_for or containment.contain
        self._runs_lock = threading.Lock()
        self._teardown_lock = threading.Lock()
        self._scope_terminator = scope_terminator or _kill_scope
        self._active_runs: list[_LaneRun] = []

    def run(
        self,
        lanes: list[Lane],
        config_for: Callable[[Lane], StressConfig],
        duration: float,
    ) -> dict[int, StressResult | None]:
        runs = [_LaneRun(lane=lane) for lane in lanes]
        start = time.monotonic()
        with self._runs_lock:
            if self._active_runs:
                raise RuntimeError("supervisor is already running")
            self._active_runs = runs

        try:
            if self._thermal_failed(runs, start, startup=True):
                return {run.lane.core_id: run.verdict for run in runs}
            with contextlib.ExitStack() as resources:
                try:
                    for run in runs:
                        try:
                            run.stdout_file = resources.enter_context(
                                tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace")
                            )
                            run.stderr_file = resources.enter_context(
                                tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace")
                            )
                        except OSError as exc:
                            self._fail(
                                run, f"Failed to start stress output capture: {exc}", start, error_type="startup"
                            )
                            break
                        if not self._launch(run, config_for(run.lane), start):
                            break
                    if any(run.running for run in runs):
                        self._poll_until_done(runs, start, duration)
                finally:
                    self._finish(runs, start, duration)
            return {run.lane.core_id: run.verdict for run in runs}
        finally:
            with self._runs_lock:
                self._active_runs = []

    def _launch(self, run: _LaneRun, cfg: StressConfig, batch_start: float) -> bool:
        lane = run.lane
        cfg = replace(cfg, cpus=lane.cpus)
        try:
            self.backend.prepare(lane.work_dir, cfg)
            self.backend.assert_prepared(lane.work_dir)
            contained = self._containment_for(lane.cpus)
            prefix = list(contained.prefix) if contained is not None else []
            run.unit = contained.unit if contained is not None else None
            cmd = prefix + self.backend.get_command(cfg, lane.work_dir)
        except (OSError, RuntimeError) as exc:
            log.error("core %d: refusing stress launch: %s", lane.core_id, exc)
            self._fail(run, f"Failed to start stress test: {exc}", batch_start, error_type="startup")
            return False
        try:
            with self._teardown_lock:
                if self.stop_event.is_set():
                    return False
                run.proc = subprocess.Popen(
                    cmd,
                    stdout=run.stdout_file,
                    stderr=run.stderr_file,
                    text=True,
                    cwd=str(lane.work_dir),
                    start_new_session=True,
                )
                run.pgid = run.proc.pid
                if cfg.duty_cycle is not None:
                    run.duty_driver = DutyCycleDriver(cfg.duty_cycle, run.pgid)
                    run.duty_driver.start()
        except (OSError, RuntimeError, TypeError) as exc:
            log.error("core %d: stress process failed to start: %s", lane.core_id, exc)
            self._fail(run, f"Failed to start stress test: {exc}", batch_start, error_type="startup")
            return False
        run.started_at = time.monotonic()
        run.last_active = run.started_at
        run.last_stall_check = run.started_at
        if run.duty_driver is not None:
            run.observed_duty_idle_seconds = getattr(run.duty_driver, "intentional_idle_seconds", 0.0)
        return True

    def _poll_until_done(self, runs: list[_LaneRun], start: float, duration: float) -> None:
        deadline = start + duration
        last_error_poll = start - ERROR_POLL_INTERVAL
        while not self.stop_event.is_set():
            if self._thermal_failed(runs, start):
                break
            if self._apply_mce_events(runs, start):
                break
            now = time.monotonic()
            if now - last_error_poll >= ERROR_POLL_INTERVAL:
                last_error_poll = now
                if self._poll_backend_errors(runs, start):
                    break
            if self._poll_exits(runs, start, now):
                break
            if not any(run.running for run in runs):
                break
            if self._poll_containment(runs, start, now):
                break
            snapshot = read_cpu_times()
            if self._poll_stalls(runs, start, now, snapshot):
                break
            if now >= deadline:
                break
            for run in runs:
                if run.running and self.hooks.on_status:
                    self.hooks.on_status(run.lane.core_id, now - start)
            self.stop_event.wait(min(self.poll_interval, max(0.0, deadline - now)))

    def _thermal_failed(self, runs: list[_LaneRun], start: float, *, startup: bool = False) -> bool:
        if self.thermal.safe():
            return False
        temp = self.thermal.last_temperature
        if self.hooks.on_thermal and temp is not None:
            self.hooks.on_thermal(temp)
        first = min((run for run in runs if run.verdict is None), default=None, key=lambda run: run.lane.core_id)
        if first is not None:
            if temp is None and startup:
                message = f"Required CPU temperature sensor unavailable before {self.phase} launch"
                error_type = "startup"
            elif temp is None:
                message = f"CPU temperature sensor disappeared after a thermal trip during {self.phase}"
                error_type = "thermal"
            else:
                message = f"CPU temperature exceeded {self.thermal.max_temperature} C safety limit during {self.phase}"
                error_type = "thermal"
            self._fail(first, message, start, error_type=error_type)
        self.stop_event.set()
        return True

    def _apply_mce_events(self, runs: list[_LaneRun], start: float, *, force: bool = False) -> bool:
        try:
            events = self.detector.check_mce(force=force)
        except RuntimeError as exc:
            for run in runs:
                if run.verdict is None or run.verdict.passed:
                    self._fail(run, str(exc), start, error_type="startup")
            self.stop_event.set()
            return True
        self.observed.extend(events)
        cpu_to_run = {cpu: run for run in runs for cpu in run.lane.mce_cpus}
        hit = False
        for event in events:
            if event.cpu == -1:
                run = min(
                    (candidate for candidate in runs if candidate.verdict is None or candidate.verdict.passed),
                    default=None,
                    key=lambda candidate: candidate.lane.core_id,
                )
            else:
                run = cpu_to_run.get(event.cpu)
            can_override = (
                force
                and event.cpu != -1
                and run is not None
                and run.verdict is not None
                and run.verdict.error_type in {"startup", "stall", "killed", "timeout"}
            )
            if run is not None and (run.verdict is None or run.verdict.passed or can_override):
                error_type = "mce_unattributed" if event.cpu == -1 else "mce"
                self._fail(run, f"MCE during {self.phase}: {event.message}", start, error_type=error_type)
                hit = True
                if event.cpu == -1:
                    self.stop_event.set()
        return hit

    def _poll_backend_errors(self, runs: list[_LaneRun], start: float) -> bool:
        for run in runs:
            if run.verdict is not None:
                continue
            err = self.backend.poll_errors(run.lane.work_dir)
            if err:
                self._fail(run, err, start)
                return self.stop_on_first_failure
        return False

    def _poll_exits(self, runs: list[_LaneRun], start: float, now: float) -> bool:
        for run in runs:
            if run.verdict is not None or run.proc is None:
                continue
            rc = self._poll_process(run, start)
            if rc is None:
                continue
            self._terminate_run(run, start)
            self._drain(run)
            if run.verdict is None:
                run.verdict = self._classify_completed(run, now - start, interrupted=False)
            if run.verdict is not None and not run.verdict.passed:
                return self.stop_on_first_failure
        return False

    def _poll_containment(self, runs: list[_LaneRun], start: float, now: float) -> bool:
        for run in runs:
            if not run.running or run.unit is None or now - run.last_watchdog < WATCHDOG_INTERVAL:
                continue
            run.last_watchdog = now
            fault = self._containment_fault(run, now)
            if fault:
                self._fail(run, fault, start, error_type="startup")
                return True
        return False

    def _poll_stalls(
        self,
        runs: list[_LaneRun],
        start: float,
        now: float,
        snapshot: dict[int, tuple[int, int]],
    ) -> bool:
        if now - start < STALL_GRACE_SECONDS:
            return False
        for run in runs:
            if not run.running or not self._is_stalled(run, now, snapshot):
                continue
            if self.hooks.on_stall:
                self.hooks.on_stall(run.lane.core_id)
            self._fail(
                run,
                f"Stress test stalled on core {run.lane.core_id} "
                f"(CPU usage near 0 on CPUs {run.lane.cpu_list} for {self.stall_timeout:.0f}s)",
                start,
                error_type="stall",
            )
            return self.stop_on_first_failure
        return False

    def _poll_process(self, run: _LaneRun, start: float) -> int | None:
        proc = run.proc
        if proc is None or not _exited_without_reaping(proc):
            return None
        self._stop_duty_driver(run, start)
        return proc.poll()

    def _stop_duty_driver(self, run: _LaneRun, start: float) -> None:
        driver = run.duty_driver
        if driver is None:
            return
        run.duty_driver = None
        try:
            driver.stop()
        except RuntimeError as exc:
            log.error("core %d: duty-cycle driver failed to stop: %s", run.lane.core_id, exc)
            self._fail(run, f"Failed to stop duty-cycle driver: {exc}", start, error_type="startup")

    def _terminate_run(self, run: _LaneRun, start: float) -> bool:
        with self._teardown_lock:
            self._stop_duty_driver(run, start)
            if run.proc is None:
                return True
            if run.termination is not None and run.termination.all_gone:
                return True
            try:
                outcome = kill_process_group(run.proc, run.pgid)
                scope_gone = self._scope_terminator(run.unit) if run.unit is not None else True
                run.termination = TerminationOutcome(
                    sent_signals=outcome.sent_signals,
                    group_gone=outcome.group_gone,
                    scope_gone=scope_gone,
                    stopped_running=outcome.stopped_running,
                )
                if not run.termination.all_gone or run.proc.poll() is None:
                    raise RuntimeError("stress process group or containment scope remains alive")
            except (OSError, RuntimeError) as exc:
                self._fail(run, f"Failed to stop stress test: {exc}", start, error_type="startup")
                return False
        return True

    def force_teardown(self) -> bool:
        """Synchronously stop every active lane and confirm all owned children are gone."""
        self.stop_event.set()
        with self._runs_lock:
            runs = list(self._active_runs)
        started = min((run.started_at for run in runs if run.started_at), default=time.monotonic())
        return all(self._terminate_run(run, started) for run in runs)

    def _containment_fault(self, run: _LaneRun, now: float) -> str | None:
        if run.proc is None or run.unit is None:
            return None
        if run.cgroup is None:
            run.cgroup = containment.payload_cgroup(run.proc.pid, run.unit)
        if run.cgroup is None:
            if now - run.started_at > CONTAINMENT_GRACE_SECONDS:
                return (
                    f"scope {run.unit} never adopted the stress payload within "
                    f"{CONTAINMENT_GRACE_SECONDS:.0f}s - containment fault, not a core verdict"
                )
            return None
        effective = containment.scope_effective_cpus(run.cgroup)
        if effective is None:
            return (
                f"the kernel record for scope {run.unit} vanished while the payload ran "
                "- containment fault, not a core verdict"
            )
        if effective != set(run.lane.cpus):
            return (
                f"scope {run.unit} runs on CPUs {containment.cpu_list(effective)} "
                f"instead of {run.lane.cpu_list} - containment fault, not a core verdict"
            )
        return None

    def _is_stalled(
        self,
        run: _LaneRun,
        now: float,
        snapshot: dict[int, tuple[int, int]],
    ) -> bool:
        active = False
        any_sample = False
        for cpu in run.lane.cpus:
            current = snapshot.get(cpu)
            busy = busy_fraction(run.prev_times.get(cpu), current)
            if current is not None:
                run.prev_times[cpu] = current
            if busy is not None:
                any_sample = True
                if busy > 0.05:
                    active = True
        duty_idle = run.duty_driver.intentional_idle_seconds if run.duty_driver is not None else 0.0
        previous_check = run.last_stall_check
        run.last_stall_check = now
        idle_delta = max(0.0, duty_idle - run.observed_duty_idle_seconds)
        run.observed_duty_idle_seconds = duty_idle
        if active or not any_sample or previous_check is None:
            run.inactive_runnable_seconds = 0.0
            run.last_active = now
            return False
        run.inactive_runnable_seconds += max(0.0, now - previous_check - idle_delta)
        return run.inactive_runnable_seconds > self.stall_timeout

    def _drain(self, run: _LaneRun) -> None:

        try:
            if run.stdout_file is not None:
                run.stdout_file.seek(0)
                run.stdout = run.stdout_file.read()
            if run.stderr_file is not None:
                run.stderr_file.seek(0)
                run.stderr = run.stderr_file.read()
        except OSError as exc:
            log.warning("core %d: captured stress output unavailable: %s", run.lane.core_id, exc)
            if run.verdict is None or run.verdict.passed:
                self._fail(run, f"Captured stress output unavailable: {exc}", run.started_at, error_type="startup")

    def _fail(
        self,
        run: _LaneRun,
        message: str,
        start: float,
        *,
        error_type: str | None = None,
    ) -> None:
        run.verdict = StressResult(
            core_id=run.lane.core_id,
            passed=False,
            duration_seconds=time.monotonic() - start,
            error_message=message,
            error_type=error_type or classify_error(message),
        )
        if self.stop_on_first_failure:
            self.stop_event.set()

    def _finish(self, runs: list[_LaneRun], start: float, duration: float) -> None:
        elapsed = time.monotonic() - start
        interrupted = self.stop_event.is_set() and elapsed < duration
        for run in runs:
            self._terminate_run(run, start)
            if run.proc is not None:
                self._drain(run)
        self._apply_mce_events(runs, start, force=True)
        for run in runs:
            if run.proc is not None and (run.verdict is None or run.verdict.passed):
                runtime = run.verdict.duration_seconds if run.verdict is not None else elapsed
                run.verdict = self._classify_completed(run, runtime, interrupted and run.verdict is None)

    @staticmethod
    def _termination_matches_returncode(run: _LaneRun, returncode: int) -> bool:
        if run.termination is None:
            return False
        return any(returncode in {-sent, 128 + sent} for sent in run.termination.sent_signals)

    def _classify_completed(self, run: _LaneRun, elapsed: float, interrupted: bool) -> StressResult | None:
        returncode = run.proc.returncode if run.proc is not None else 0
        returncode = returncode if returncode is not None else 0
        if returncode == 0 and run.termination is not None and run.termination.stopped_running:
            # mprime traps SIGTERM and exits 0 after a clean shutdown; that is still our stop.
            returncode = -signal.SIGTERM
        live_error = self.backend.poll_errors(run.lane.work_dir)
        if live_error:
            return StressResult(
                core_id=run.lane.core_id,
                passed=False,
                duration_seconds=elapsed,
                error_message=live_error,
                error_type=classify_error(live_error),
            )
        if returncode in KILLED_BY_US_CODES and not self._termination_matches_returncode(run, returncode):
            return StressResult(
                core_id=run.lane.core_id,
                passed=False,
                duration_seconds=elapsed,
                error_message=f"Stress process killed externally (code {returncode}) - possible OOM or system issue",
                error_type="killed",
            )
        passed, message = self.backend.parse_output(run.stdout or "", run.stderr or "", returncode)
        if not passed:
            return StressResult(
                core_id=run.lane.core_id,
                passed=False,
                duration_seconds=elapsed,
                error_message=message,
                error_type=classify_error(message) if message else None,
            )
        if returncode not in KILLED_BY_US_CODES and elapsed < STARTUP_WINDOW_SECONDS:
            return StressResult(
                core_id=run.lane.core_id,
                passed=False,
                duration_seconds=elapsed,
                error_message=f"stress exited at startup (code {returncode}) with no work done - verdict unavailable",
                error_type="startup",
            )
        if interrupted:
            return None
        return StressResult(core_id=run.lane.core_id, passed=True, duration_seconds=elapsed)


def watch_idle(
    *,
    cpus: tuple[int, ...],
    sibling_cpus: tuple[int, ...] = (),
    duration: float,
    thermal: ThermalWatch,
    detector: ErrorDetector,
    stop_event: threading.Event,
    observed: list[MCEEvent],
    phase: str,
    poll_interval: float = 0.5,
) -> str | None:
    own = set(sibling_cpus or cpus)
    start = time.monotonic()
    while True:
        finished = time.monotonic() - start >= duration or stop_event.is_set()
        thermal_stop = not finished and not thermal.safe()
        if thermal_stop:
            stop_event.set()
        try:
            events = detector.check_mce(force=finished or thermal_stop)
        except RuntimeError as exc:
            return str(exc)
        if events:
            observed.extend(events)
            for event in events:
                if event.cpu == -1 or event.cpu in own:
                    return f"MCE during {phase}: {event.message}"
        if thermal_stop:
            return f"CPU temperature exceeded {thermal.max_temperature} C safety limit during {phase}"
        if finished:
            return None
        remaining = duration - (time.monotonic() - start)
        stop_event.wait(min(poll_interval, max(0.0, remaining)))


def classify_error(msg: str | None) -> str:
    if not msg:
        return "unknown"
    msg_lower = msg.lower()
    if (
        "failed to start" in msg_lower
        or "verdict unavailable" in msg_lower
        or "harness error" in msg_lower
        or "containment fault" in msg_lower
        or "kernel error monitor" in msg_lower
    ):
        return "startup"
    if "machine check without core attribution" in msg_lower:
        return "mce_unattributed"
    if "mce" in msg_lower or "machine check" in msg_lower:
        return "mce"
    if "temperature" in msg_lower or "thermal" in msg_lower:
        return "thermal"
    if "stall" in msg_lower:
        return "stall"
    if any(
        w in msg_lower
        for w in (
            "rounding",
            "fatal",
            "illegal",
            "sumout",
            "mismatch",
            "jacobi",
            "verification",
            "computation",
        )
    ):
        return "computation"
    if "timeout" in msg_lower:
        return "timeout"
    if "killed externally" in msg_lower:
        return "killed"
    if "crash" in msg_lower or "signal" in msg_lower:
        return "crash"
    if re.search(r"exited with code -\d+", msg_lower):
        return "crash"
    if "idle" in msg_lower:
        return "idle_instability"
    if "variable" in msg_lower or "transition" in msg_lower:
        return "load_transition"
    return "unknown"
