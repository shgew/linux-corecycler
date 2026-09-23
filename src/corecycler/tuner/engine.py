"""Automated PBO Curve Optimizer tuner — core state machine and orchestrator.

Drives the coarse-to-fine search: big steps first, fine steps after failure,
confirmation at the settled value. Every state transition persists to SQLite
before acting, so the tuner resumes exactly where it left off after crash/reboot.

Test execution runs on a QThread so the GUI remains responsive.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from corecycler import __version__
from corecycler.config.paths import resolve_work_dir, user_home
from corecycler.engine.backends import get_backend, load_all
from corecycler.engine.backends.base import DutyCycle, StressConfig
from corecycler.engine.backends.stressapptest import default_memory_mb
from corecycler.engine.detector import (
    ErrorDetector,
    MCEEvent,
    harvest_kernel_mce,
    last_boot_ended_cleanly,
)
from corecycler.engine.execution import ThermalWatch
from corecycler.engine.execution import busy_fraction as _busy_fraction
from corecycler.engine.microfreeze import MicroFreezeMonitor
from corecycler.engine.parallel import ParallelStress
from corecycler.engine.scheduler import CoreScheduler, SchedulerConfig
from corecycler.history.context import capture_system_context
from corecycler.history.db import RESUMABLE_STATUSES, LegacySession
from corecycler.inhibit import SleepInhibitor
from corecycler.monitor.cpu_usage import read_cpu_times as _read_all_cpu_times
from corecycler.monitor.msr import MSRReader
from corecycler.smu.driver import core_map_blocked

from . import bisect
from . import persistence as tp
from .config import TunerConfig
from .regime import Mask, Regime
from .state import CoreState, TunerPhase

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from corecycler.engine.backends.base import StressBackend
    from corecycler.engine.topology import CPUTopology
    from corecycler.history.db import HistoryDB
    from corecycler.smu.driver import RyzenSMU

log = logging.getLogger(__name__)

# Statuses in which the session is waiting on a human, not on hardware: the
# machine may sleep again.
DORMANT_STATUSES = frozenset({"idle", "paused", "completed", "aborted", "platform_fault", "profile_quarantined"})


def _duty_cycle_for(entry: dict) -> DutyCycle | None:
    """The duty cycle a battery entry asks for, if it is a transient regime.

    Fixed metronome and random-phase are both transient workloads; the random
    variant is the one that reaches duty ratios a metronome never visits.
    """
    if entry.get("profile") != "transient":
        return None
    return DutyCycle(random_phases=bool(entry.get("random_phases", False)))


def _has_unattributed_mce(mce_json: str) -> bool:
    """True when the worker's MCE payload holds an event naming no CPU.

    Fail closed at the payload level only: a malformed payload is no evidence
    (matching _foreign_mce_by_core); an explicit cpu == -1 event is.
    """
    if not mce_json:
        return False
    try:
        raw = json.loads(mce_json)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(raw, list):
        return False
    return any(isinstance(item, dict) and item.get("cpu") == -1 for item in raw)


def _read_cpu_times(cpu: int) -> tuple[int, int] | None:
    try:
        return _read_all_cpu_times().get(cpu)
    except OSError:
        return None


def _rebooted_since(
    iso_ts: str | None,
    stat_path: str = "/proc/stat",
    *,
    previous_boot_id: str = "",
    boot_id: str = "",
) -> bool:
    if previous_boot_id and boot_id:
        return previous_boot_id != boot_id
    return True


# ------------------------------------------------------------------
# Worker thread — runs a single core test without blocking the GUI
# ------------------------------------------------------------------


_STRETCH_WARMUP_SECONDS = 5  # skip startup noise (process exec, turbo ramp)
_STRETCH_SAMPLE_INTERVAL = 5  # seconds between APERF/MPERF samples
_STRETCH_MIN_BUSY = 0.9  # a sample window counts only under sustained load


def _read_boot_id() -> str:
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        return ""


def _pick_report(results: dict[int, list], primary: int) -> tuple[int, object | None]:
    """Choose which core's verdict a multi-core scheduler run reports.

    The primary core is the default, but a failure on ANY core in the batch
    outranks the primary's pass.
    """
    primary_results = results.get(primary, [])
    report = primary_results[0] if primary_results else None
    if report is None or report.passed:
        for cid in sorted(results):
            rs = results[cid]
            if rs and not rs[0].passed:
                return cid, rs[0]
    return primary, report


def _serialize_mce(events: list[MCEEvent]) -> str:
    if not events:
        return ""
    return json.dumps(
        [
            {
                "cpu": event.cpu,
                "bank": event.bank,
                "corrected": event.corrected,
                "message": event.message,
                "raw_ts": event.raw_ts,
            }
            for event in events
        ]
    )


@dataclass(frozen=True, slots=True)
class _WorkerOutcome:
    core_id: int
    passed: bool
    error_message: str = ""
    error_type: str = ""
    duration: float = 0.0
    peak_stretch_pct: float = 0.0
    mce_json: str = ""
    results_json: str = ""

    @classmethod
    def apparatus_fault(cls, core_id: int, error: Exception) -> _WorkerOutcome:
        return cls(core_id=core_id, passed=False, error_message=str(error), error_type="startup")

    def signal_args(self) -> tuple[int, bool, str, str, float, float, str, str]:
        return (
            self.core_id,
            self.passed,
            self.error_message,
            self.error_type,
            self.duration,
            self.peak_stretch_pct,
            self.mce_json,
            self.results_json,
        )


class _TunerWorker(QThread):
    """Runs one CoreScheduler test on a background thread.

    Optionally samples APERF/MPERF clock stretch during the test via a
    background sampler thread. The sampler waits for turbo to stabilise
    after process startup, then takes periodic 5-second windows and
    reports the **peak** stretch observed — not the average over the
    whole test. This avoids false positives from startup overhead,
    turbo ramp-up, and C-state transitions before load reaches 100%.
    """

    # core_id, passed, error_msg, error_type, duration, peak_stretch_pct,
    # mce_json, results_json (per-core verdicts of a multi-core run)
    finished = Signal(int, bool, str, str, float, float, str, str)

    def __init__(
        self,
        core_id: int,
        logical_cpu: int,
        scheduler: CoreScheduler,
        msr: MSRReader | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._core_id = core_id
        self._logical_cpu = logical_cpu
        self._scheduler = scheduler
        self._msr = msr

    @property
    def scheduler(self) -> CoreScheduler:
        return self._scheduler

    def run(self) -> None:
        stretch_samples: list[float] = []
        stop_event = threading.Event()
        sampler: threading.Thread | None = None
        try:
            if self._msr and self._msr.is_available():
                sampler = threading.Thread(
                    target=self._stretch_sampler,
                    args=(stretch_samples, stop_event),
                    daemon=True,
                )
                sampler.start()
            start = time.monotonic()
            results = self._scheduler.run()
            elapsed = time.monotonic() - start
        except Exception as e:
            log.exception("Tuner worker crashed for core %d", self._core_id)
            outcome = _WorkerOutcome.apparatus_fault(self._core_id, e)
        else:
            outcome = None
        finally:
            stop_event.set()
            if sampler is not None:
                sampler.join()

        if outcome is not None:
            self.finished.emit(*outcome.signal_args())
            return

        peak_stretch = max(stretch_samples) if stretch_samples else 0.0
        mce_json = _serialize_mce(self._scheduler.observed_mce)
        report_core, report = _pick_report(results, self._core_id)
        if report is not None:
            self.finished.emit(
                report_core,
                report.passed,
                report.error_message or "",
                report.error_type or "",
                report.duration_seconds,
                peak_stretch if report_core == self._core_id else 0.0,
                mce_json,
                "",
            )
        else:
            self.finished.emit(
                self._core_id,
                False,
                "No result returned",
                "startup",
                elapsed,
                peak_stretch,
                mce_json,
                "",
            )

    def _stretch_sampler(self, samples: list[float], stop: threading.Event) -> None:
        try:
            self._sample_stretch(samples, stop)
        except Exception:
            log.debug("APERF/MPERF sampler stopped after a sensor error", exc_info=True)

    def _sample_stretch(self, samples: list[float], stop: threading.Event) -> None:
        cpu = self._logical_cpu
        msr = self._msr
        if not msr:
            return
        if stop.wait(_STRETCH_WARMUP_SECONDS):
            return
        msr.read_clock_stretch([cpu])
        busy_prev = _read_cpu_times(cpu)
        while not stop.wait(_STRETCH_SAMPLE_INTERVAL):
            readings = msr.read_clock_stretch([cpu])
            busy_now = _read_cpu_times(cpu)
            busy = _busy_fraction(busy_prev, busy_now)
            busy_prev = busy_now
            reading = readings.get(cpu)
            if reading and (busy is None or busy >= _STRETCH_MIN_BUSY):
                samples.append(reading.stretch_pct)


class _RapidTransitionWorker(_TunerWorker):
    """Runs rapid transition validation on a background thread."""

    def __init__(
        self,
        core_id: int,
        logical_cpu: int,
        scheduler: CoreScheduler,
        cores: list[int],
        duration: float,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(core_id, logical_cpu, scheduler, msr=None, parent=parent)
        self._cores = cores
        self._duration = duration

    def run(self) -> None:
        try:
            start = time.monotonic()
            result = self.scheduler.run_rapid_transitions(
                cores=self._cores,
                total_duration=self._duration,
            )
            elapsed = time.monotonic() - start
            self.finished.emit(
                self._core_id,
                result.passed,
                result.error_message or "",
                result.error_type or "",
                elapsed,
                0.0,
                _serialize_mce(self.scheduler.observed_mce),
                "",
            )
        except Exception as e:
            log.exception("Rapid transition worker crashed for core %d", self._core_id)
            self.finished.emit(*_WorkerOutcome.apparatus_fault(self._core_id, e).signal_args())


class _ParallelWorker(_TunerWorker):
    """Runs one ParallelStress batch (all lanes simultaneously) on a thread."""

    def __init__(
        self,
        core_id: int,
        logical_cpu: int,
        runner: ParallelStress,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(core_id, logical_cpu, runner, msr=None, parent=parent)

    def run(self) -> None:
        try:
            start = time.monotonic()
            raw = self._scheduler.run()
            elapsed = time.monotonic() - start
            results = {c: [r] for c, r in raw.items()}
            mce_json = _serialize_mce(self._scheduler.observed_mce)
            results_json = json.dumps(
                [
                    {
                        "core": c,
                        "passed": r.passed,
                        "error_type": r.error_type,
                        "error_message": r.error_message,
                        "duration": r.duration_seconds,
                    }
                    for c, r in raw.items()
                ]
            )
            report_core, report = _pick_report(results, self._core_id)
            if report is not None:
                self.finished.emit(
                    report_core,
                    report.passed,
                    report.error_message or "",
                    report.error_type or "",
                    report.duration_seconds,
                    0.0,
                    mce_json,
                    results_json,
                )
            else:
                self.finished.emit(
                    self._core_id,
                    False,
                    "No result returned",
                    "startup",
                    elapsed,
                    0.0,
                    mce_json,
                    results_json,
                )
        except Exception as e:
            log.exception("Parallel worker crashed for core %d", self._core_id)
            self.finished.emit(*_WorkerOutcome.apparatus_fault(self._core_id, e).signal_args())


class _SoakWorker(QThread):
    """Watches the kernel error stream with no load; any event ends the soak."""

    finished = Signal(int, bool, str, str, float, float, str, str)

    def __init__(
        self,
        core_id: int,
        duration: int,
        thermal: ThermalWatch | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._core_id = core_id
        self._duration = float(duration)
        self._thermal = thermal
        self._stop = threading.Event()
        self.detector = ErrorDetector()
        self.scheduler = self

    def stop(self) -> None:
        self._stop.set()

    force_stop = stop

    def run(self) -> None:
        try:
            self.detector.reset()
            start = time.monotonic()
            events: list[MCEEvent] = []
            while time.monotonic() - start < self._duration and not self._stop.is_set():
                if self._thermal is not None and not self._thermal.safe():
                    elapsed = time.monotonic() - start
                    temperature = self._thermal.last_temperature
                    if temperature is None:
                        self.finished.emit(
                            self._core_id,
                            False,
                            "No CPU temperature sensor available during soak",
                            "startup",
                            elapsed,
                            0.0,
                            "",
                            "",
                        )
                    else:
                        self.finished.emit(
                            self._core_id,
                            False,
                            f"CPU temperature {temperature:.1f} C exceeded the soak limit",
                            "thermal",
                            elapsed,
                            0.0,
                            "",
                            "",
                        )
                    return
                events.extend(self.detector.check_mce())
                if events:
                    break
                self._stop.wait(5.0)
            completed = time.monotonic() - start >= self._duration
            if not events:
                events.extend(self.detector.check_mce(force=True))
            elapsed = time.monotonic() - start
            if events:
                self.finished.emit(
                    self._core_id,
                    False,
                    f"kernel error during soak: {events[0].message}",
                    "mce",
                    elapsed,
                    0.0,
                    _serialize_mce(events),
                    "",
                )
            elif not completed:
                self.finished.emit(self._core_id, False, "Soak interrupted", "killed", elapsed, 0.0, "", "")
            else:
                self.finished.emit(self._core_id, True, "", "", elapsed, 0.0, "", "")
        except Exception as e:
            log.exception("Soak worker crashed")
            self.finished.emit(*_WorkerOutcome.apparatus_fault(self._core_id, e).signal_args())


# ------------------------------------------------------------------
# Engine
# ------------------------------------------------------------------


class TunerEngine(QObject):
    """Orchestrates the automated CO search.

    Emits Qt signals for GUI updates; each individual core test runs
    on a _TunerWorker QThread. This class manages the state machine
    and persists every transition.
    """

    # Signals
    core_state_changed = Signal(int, str, int)  # core_id, phase, offset
    test_completed = Signal(int, int, bool)  # core_id, offset, passed
    session_completed = Signal(str)  # JSON-encoded {core_id: best_offset}
    status_changed = Signal(str)  # global status
    progress_updated = Signal(int, int)  # cores_done, cores_total
    log_message = Signal(str)
    # Emitted when the machine fails at stock: the offsets are exonerated and
    # the search has no further question to ask.
    platform_fault = Signal(str)  # human-readable log entry
    co_drift_detected = Signal(str)  # JSON-encoded {core_id: {expected, actual}}
    validation_progress = Signal(int, int, int)  # stage, current_index, total
    worker_started = Signal(int)  # core_id — emitted when mprime actually starts
    # JSON: the launching workload plus what it is testing - cores, battery
    # position for search slots, live set for hunt probes, validation stage.
    slot_started = Signal(str)

    def __init__(
        self,
        db: HistoryDB,
        topology: CPUTopology,
        smu: RyzenSMU | None,
        backend: StressBackend,
        config: TunerConfig | None = None,
        work_dir: Path | None = None,
    ) -> None:
        super().__init__()
        self._db = db
        self._topology = topology
        self._smu = smu
        self._backend = backend
        self._config = config or TunerConfig()
        self._work_dir = work_dir or resolve_work_dir() / "tuner"

        self._msr = MSRReader()
        self._boot_id = _read_boot_id()
        # Every narrative line becomes durable: the story survives the
        # terminal and is replayed on resume.
        self.log_message.connect(self._persist_narrative)

        self._session_id: int | None = None
        self._core_states: dict[int, CoreState] = {}
        self._status: str = "idle"
        self._sleep = SleepInhibitor("Curve Optimizer tuning session running")
        self._paused = False
        self._abort_requested = False
        self._consecutive_start_failures = 0
        self._worker: _TunerWorker | None = None
        self._last_tested_core: int | None = None
        self._ccd_last_tested: dict[int, int | None] = {}  # CCD index → last core_id tested in that CCD
        # Round-robins implementations within a regime so a regime's banked time
        # is not all one instruction mix.
        self._regime_rotation: dict[str, int] = {}
        self._hunt: bisect.HuntState | None = None
        self._pending_hunt_loaded: list[int] = []
        self._pending_hunt_vector: dict[int, int] = {}
        self._battery_orders: dict[int, tuple[tuple[str, int], list[str]]] = {}
        self._freeze: MicroFreezeMonitor | None = None
        self._freeze_slot_context = ""
        self._co_applied: dict[int, int | None] = {}  # core_id → last CO value written to SMU (None = unknown)
        # core_id → most-aggressive CO value proven survivable this session (the
        # machine lived a test with it resident). Seeds at 0 (stock is always
        # safe); rebuilt from the CO journal on resume. Baselines are NOT seeded
        # here — a baseline must earn "survived" like any other value.
        self._co_survived: dict[int, int] = {}

        # Multi-core validation state
        self._validation_stage: int = 0  # 0 = not validating, 1/2/3 = stage
        self._validation_thermal_aborts: int = 0  # consecutive thermal stops in validation
        self._apparatus_fault_streak: int = 0  # consecutive faults with no verdict
        self._validation_core_index: int = 0  # index into _validation_core_order for stage 1
        self._validation_core_order: list[int] = []  # cores to cycle through in stage 1
        self._validation_half_index: int = 0  # which half to test in stage 3
        self._validation_halves: list[list[int]] = []  # [half_a, half_b] for stage 3
        # Cores flagged in_test for the validation worker currently running, so a
        # hard crash during validation is attributed on resume (the confirmed
        # offsets it re-applies are journaled survived, so only in_test arms the
        # circuit breaker for a multi-core power-interaction crash).
        self._cores_under_stress: list[int] = []

        # Incremental validation: dirty = a back-off happened since the last
        # clean pass (DONE requires one full pass with dirty False); requeue =
        # cores owing a solo re-test because their offset changed.
        self._validation_dirty = False
        self._validation_requeue: list[int] = []
        self._in_requeue = False

        # Endurance (validation stage 9): perpetual rounds of per-core and
        # all-core slots over the configured workload matrix.
        self._endurance_round = 0
        self._endurance_workload = 0
        self._endurance_index = 0
        self._worker_profile = "sustained"

        # Crash hunt: when a hard crash cannot be attributed by evidence, the
        # engine runs isolated per-core hunt slots instead of guessing.
        self._hunting = False
        self._hunt_workload: dict | None = None
        self._soaking = False
        # Post-reboot kernel-journal harvest, injectable for tests.
        self._forensics = harvest_kernel_mce

        # Clamp max_offset to CPU generation range
        if smu is not None:
            self._config.clamp_max_offset(smu.commands.co_range)

    @property
    def status(self) -> str:
        return self._status

    @property
    def session_id(self) -> int | None:
        return self._session_id

    @property
    def test_in_flight(self) -> bool:
        """True from worker start until its result has been processed."""
        return self._worker is not None

    @property
    def core_states(self) -> dict[int, CoreState]:
        return self._core_states

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, seed_offsets: dict[int, int] | None = None) -> None:
        """Start a new tuner session.

        ``seed_offsets`` carries what an earlier session learned into the new
        search as a starting point. A seeded core enters ``COARSE_SEARCH`` at
        its seeded value rather than stepping past it: a prior is a hypothesis,
        and this engine exists because offsets proven under a condition the
        machine never runs in were never proven at all. The seeded core's
        baseline stays at stock so backoff keeps the full retreat.
        """

        self._abort_requested = False
        self._paused = False
        self._consecutive_start_failures = 0
        self._validation_stage = 0
        self._validation_dirty = False
        self._validation_requeue = []
        self._in_requeue = False
        self._hunting = False
        self._soaking = False

        # Validate config
        errors = self._config.validate(self._smu.commands.co_range if self._smu is not None else None)
        if errors:
            self.log_message.emit(f"Invalid tuner config: {'; '.join(errors)}")
            return

        # A tuning session is meaningless when per-core CO addressing is
        # refused: every write would fail and every result would be noise.
        map_err = self._co_access_error()
        if map_err is not None:
            self.log_message.emit(f"Cannot start: per-core CO is unavailable — {map_err}")
            return

        cores = self._get_cores_to_test()
        missing_cores = sorted(set(cores) - set(self._topology.cores))
        if missing_cores:
            self.log_message.emit(f"Cannot start: selected cores are absent from the topology: {missing_cores}")
            return
        num_cores = len(self._topology.cores)
        ctx = capture_system_context(
            self._smu,
            num_cores,
            cpu_model=self._topology.model_name,
            ccds=self._topology.ccds,
        )
        if not ctx.complete:
            self.log_message.emit(f"Cannot start: system context is incomplete: {', '.join(ctx.missing)}")
            return
        context_id = self._db.get_or_create_context(ctx)

        self._session_id = self._db.create_tuner_session(
            self._config.to_json(),
            bios_version=ctx.bios_version,
            cpu_model=self._topology.model_name,
            context_id=context_id,
        )
        self._db.set_session_boot(self._session_id, self._boot_id)
        self._core_states = {}

        # Read current CO offsets from SMU if inheriting
        current_offsets: dict[int, int] = {}
        if self._config.inherit_current and self._smu is not None:
            for core_id in cores:
                val = self._smu.get_co_offset(core_id)
                if val is not None:
                    current_offsets[core_id] = val
            self.log_message.emit(f"Inherited current CO offsets from SMU: {current_offsets}")

        seeds = self._resolve_seeds(seed_offsets, cores)
        for core_id in cores:
            seed = seeds.get(core_id)
            start = seed if seed is not None else current_offsets.get(core_id, self._config.start_offset)
            cs = CoreState(
                core_id=core_id,
                current_offset=start,
                baseline_offset=self._config.start_offset if seed is not None else start,
                phase=TunerPhase.COARSE_SEARCH if seed is not None else TunerPhase.NOT_STARTED,
            )
            self._core_states[core_id] = cs
            self._db.upsert_tuner_core_state(self._session_id, cs)
            self._co_applied[core_id] = None  # unknown — SMU state not yet managed
        if seeds:
            self.log_message.emit(
                "Seeded from an earlier session: "
                + ", ".join(f"core {c} at {seeds[c]}" for c in sorted(seeds))
                + " — every seed is retested on the live mask before the search goes deeper"
            )

        self._transition_status("running")
        self.log_message.emit(
            f"Started tuner session {self._session_id} — "
            f"{len(cores)} cores, coarse step {self._config.coarse_step}, "
            f"fine step {self._config.fine_step}"
        )
        if any(v is not None for v in (ctx.ppt_limit_w, ctx.tdc_limit_a, ctx.edc_limit_a)):

            def _fmt(v: float | None, unit: str) -> str:
                return f"{v:.0f} {unit}" if v is not None else "unknown"

            self.log_message.emit(
                f"PBO power limits (recorded in tuning context): "
                f"PPT {_fmt(ctx.ppt_limit_w, 'W')}, "
                f"TDC {_fmt(ctx.tdc_limit_a, 'A')}, "
                f"EDC {_fmt(ctx.edc_limit_a, 'A')}"
            )

        self._run_next()

    def _co_access_error(self) -> str | None:
        if getattr(self._smu, "dry_run", False) is True:
            return "SMU Dry Run is enabled; disable it before tuning"
        return core_map_blocked(self._smu)

    def _core_state_range_error(self) -> str | None:
        unknown = sorted(set(self._core_states) - set(self._topology.cores))
        if unknown:
            return f"persisted cores are absent from the topology: {unknown}"
        if self._smu is None:
            return None
        low, high = self._smu.commands.co_range
        for cs in self._core_states.values():
            for field in (
                "current_offset",
                "best_offset",
                "coarse_fail_offset",
                "baseline_offset",
                "backoff_fail_bound",
                "backoff_pass_bound",
                "proven_offset",
            ):
                value = getattr(cs, field, None)
                if value is not None and not low <= value <= high:
                    return f"core {cs.core_id} {field}={value} is outside the hardware range {low}..{high}"
        return None

    def _load_saved_config(self, config_json: str) -> bool:
        try:
            config = TunerConfig.from_json(config_json)
        except ValueError as exc:
            self.log_message.emit(f"Invalid tuner config: {exc}")
            return False
        if config.backend != self._config.backend:
            load_all()
            try:
                backend = get_backend(config.backend)
            except KeyError:
                self.log_message.emit(f"Unknown saved backend: {config.backend}")
                return False
            if not backend.is_available():
                self.log_message.emit(f"Saved backend unavailable: {config.backend}")
                return False
            self._backend = backend
        co_range = self._smu.commands.co_range if self._smu is not None else None
        errors = config.validate(co_range)
        if errors:
            self.log_message.emit(f"Invalid saved tuner config: {'; '.join(errors)}")
            return False
        self._config = config
        return True

    def resume(self, session_id: int) -> None:
        """Resume a crashed/paused session: attribute any crash from evidence
        first, then restore baselines, then continue where the cursor points."""
        # A pause takes effect AFTER the in-flight test; resuming under a live
        # worker would rewrite SMU baselines beneath the running stress test
        # (false PASS at an untested offset) and orphan the worker thread.
        if self._worker is not None and self._worker.isRunning():
            self.log_message.emit(
                "Resume ignored: the current test is still finishing (pause takes effect after it completes)."
            )
            return
        self._abort_requested = False
        self._paused = False
        self._validation_stage = 0
        self._validation_dirty = False
        self._validation_requeue = []
        self._endurance_round = 0
        self._endurance_workload = 0
        self._endurance_index = 0
        self._in_requeue = False
        self._hunting = False
        self._hunt = None
        self._hunt_workload = None
        self._soaking = False
        self._pending_hunt_loaded = []
        self._pending_hunt_vector = {}
        self._session_id = session_id

        try:
            session = self._db.get_tuner_session(session_id, resumable=True)
        except LegacySession as exc:
            self.log_message.emit(str(exc))
            return
        if session is None:
            self.log_message.emit(f"Session {session_id} not found")
            return
        last_activity = self._db.latest_session_activity(session_id)
        rebooted = _rebooted_since(last_activity, previous_boot_id=session.boot_id, boot_id=self._boot_id)

        if not self._load_saved_config(session.config_json):
            return

        # start() refuses on an unusable per-core CO map; a resumed session
        # would otherwise grind through refused writes as apparatus faults.
        map_err = self._co_access_error()
        if map_err is not None:
            self.log_message.emit(f"Cannot resume: per-core CO is unavailable — {map_err}")
            return
        missing_cores = sorted(set(self._get_cores_to_test()) - set(self._topology.cores))
        if missing_cores:
            self.log_message.emit(f"Cannot resume: selected cores are absent from the topology: {missing_cores}")
            self.pause()
            return
        ctx = capture_system_context(
            self._smu,
            len(self._topology.cores),
            cpu_model=self._topology.model_name,
            ccds=self._topology.ccds,
        )
        if not ctx.complete:
            self.log_message.emit(f"Cannot resume: system context is incomplete: {', '.join(ctx.missing)}")
            self.pause()
            return

        self._core_states = self._db.get_tuner_core_states(session_id)
        invalid_state = self._core_state_range_error()
        if invalid_state is not None:
            self.log_message.emit(f"Cannot resume: {invalid_state}")
            self.pause()
            return

        self._co_survived = tp.journal_survived_values(self._db, session_id)

        if session.status == "profile_quarantined":
            self._reengage_quarantined(session_id)
            rebooted = False
        self.log_message.emit(
            f"Resume recovery: {'reboot detected' if rebooted else 'same boot'} "
            f"(previous={session.boot_id or 'unknown'}, current={self._boot_id or 'unknown'}; "
            f"last execution={last_activity or 'unknown'})"
        )
        if session.app_version != __version__:
            self.log_message.emit(
                f"Session created by corecycler {session.app_version or 'unknown'}; resuming with {__version__}"
            )

        # Check for CO drift — warn only when the SMU differs from what the
        # TUNER last wrote (the CO journal); validation deliberately leaves the
        # confirmed offsets applied, so a baseline comparison would flag the
        # tuner's own work. Real drift = a third party (Curve Optimizer tab,
        # another tool) changed the values behind our back.
        if self._smu is not None:
            expected_values = tp.journal_values(self._db, session_id)
            drift: dict[int, dict[str, int]] = {}
            for cs in self._core_states.values():
                actual = self._smu.get_co_offset(cs.core_id)
                expected = expected_values.get(cs.core_id, cs.baseline_offset)
                if actual is None or actual == expected:
                    continue
                # After a reboot, actual == 0 is the EXPECTED state (SMU SRAM
                # is zeroed), not drift.
                if rebooted and actual == 0:
                    continue
                drift[cs.core_id] = {"expected": expected, "actual": actual}
            if drift:
                self.log_message.emit(
                    f"CO drift detected on {len(drift)} core(s) — SMU values differ "
                    f"from the tuner's last write; something outside the tuner "
                    f"changed them. The session's values will be re-applied."
                )
                self.co_drift_detected.emit(json.dumps(drift))

        # Step 1: Attribute the crash from the strongest available evidence.
        # Priority after a reboot:
        #   1. An armed persisted hunt probe is the controlled experiment and
        #      owns its own reproduction verdict.
        #   2. Kernel-journal MCE lines name the faulting core directly.
        #   3. One in_test core that was also the sole non-stock resident is
        #      deterministically attributable.
        #   4. One un-survived journal resident with no in_test marker catches
        #      crashes during writes, restores, and idle operation.
        #   5. Multi-core or otherwise ambiguous evidence blames nobody and
        #      schedules a live-vector attribution hunt.
        # Gate: crash handling only applies when the machine actually REBOOTED
        # since the session's last persisted write. A leftover in_test flag or
        # un-survived journal row with no reboot in between is a plain app exit
        # (window closed, SIGKILL mid-test) — penalizing it would walk good
        # offsets away on every restart.
        crashed: list[int] = []
        pending_hunt = False
        try:
            self._hunt = bisect.HuntState.from_json(session.hunt_state)
        except bisect.InvalidHuntState as exc:
            self.log_message.emit(f"Persisted hunt state is invalid: {exc}. Pausing without applying CO.")
            self.pause()
            return
        clean_reboot = rebooted and last_boot_ended_cleanly(boot_id=session.boot_id)
        if clean_reboot:
            self._clear_all_in_test()
            if self._hunt is not None:
                self._hunting = True
                if self._hunt.armed:
                    self._requeue_hunt_probe()
                pending_hunt = True
            else:
                self._db.set_hunt_state(session_id, "")
        elif rebooted:
            crashed, pending_hunt = self._attribute_crash_after_reboot(session, last_activity)
            if self._paused:
                return
        else:
            self._clear_all_in_test()
            if self._hunt is not None:
                self._hunting = True
                if self._hunt.armed:
                    self._requeue_hunt_probe()
                pending_hunt = True
            else:
                self._db.set_hunt_state(session_id, "")
        self._reconcile_confirmed_evidence()
        self._db.set_session_boot(session_id, self._boot_id)

        if crashed or (rebooted and pending_hunt and not clean_reboot):
            for core_id in crashed:
                self.log_message.emit(f"Core {core_id} crash detected — applied penalty backoff")
            self._set_status(
                f"resumed after crash (cores: {crashed})" if crashed else "resumed after unattributed crash"
            )
            # Only forward progress or a convicted hunt failure clears this streak.
            streak = self._db.get_resume_crash_streak(session_id) + 1
            self._db.set_resume_crash_streak(session_id, streak)
            if streak >= self._config.resume_crash_quarantine_threshold and not pending_hunt:
                # Repeated crashes on re-engage used to dead-end here. There is
                # a better question to ask first: does the machine still die
                # with every core at stock? The hunt opens with exactly that
                # control probe, and answers platform-fault-or-not instead of
                # handing the problem back.
                self.log_message.emit(
                    f"{streak} crash-resumes in a row with no surviving test. "
                    "Asking whether the machine survives at full stock before blaming any offset."
                )
                self._start_hunt(loaded=self._pending_hunt_loaded)
                return

        # Step 2: Restore all cores to their baseline offsets.
        # After a crash and reboot, SMU SRAM is zeroed. Apply the known-stable
        # baselines (captured from BIOS/inherit_current at session start) so the
        # CPU runs at its proven-stable config. _run_next() will apply the test
        # offset only to the core being tested.
        if self._smu is not None:
            failed_cores: list[int] = []
            baselines: dict[int, int] = {}
            for cs in self._core_states.values():
                baselines[cs.core_id] = cs.baseline_offset

                try:
                    success = self._write_co_verified(cs.core_id, cs.baseline_offset)
                    if success:
                        self._co_applied[cs.core_id] = cs.baseline_offset
                    else:
                        failed_cores.append(cs.core_id)
                        self.log_message.emit(
                            f"Baseline restore failed for core {cs.core_id} at offset "
                            f"{cs.baseline_offset} — read-back mismatch or SMU rejection"
                        )
                except Exception as e:
                    failed_cores.append(cs.core_id)
                    log.warning("Failed to restore baseline for core %d: %s", cs.core_id, e)
                    self.log_message.emit(f"Baseline restore error for core {cs.core_id}: {e}")
            if failed_cores:
                # Fail closed: continuing to test on a machine whose SMU cannot
                # even restore proven baselines would produce garbage verdicts
                # and leave unknown offsets resident.
                self.log_message.emit(
                    f"Baselines could not be restored for cores {failed_cores} — "
                    f"SMU access is broken or changed since last session. Pausing; "
                    f"fix SMU access (ryzen_smu module, permissions), then Resume."
                )
                self.pause()
                return
            self.log_message.emit(f"Restored baselines: {baselines}")

        # Restore the round-robin / CCD cycling cursor from the test log so the
        # cycling order continues across the reboot instead of restarting.
        self._reconstruct_scheduling_position()

        # A reboot mid-validation that produced no kernel evidence and left
        # nothing in-test (it hit between two slots, or the in-test mark was
        # lost with the crash) must not pass silently: the machine died with
        # the profile live. A provably clean shutdown is exempt — rebooting
        # deliberately is not an incident.
        unattributed_incident = (
            rebooted
            and not clean_reboot
            and not crashed
            and not pending_hunt
            and (session.status == "validating" or session.validation_stage > 0)
        )
        if unattributed_incident:
            n = self._db.get_unattributed_crashes(session_id) + 1
            self._db.set_unattributed_crashes(session_id, n)
            self.log_message.emit(
                f"The machine went down mid-validation with the profile live, "
                f"but no kernel evidence names a core and nothing was marked "
                f"in-test — recorded unattributed incident "
                f"{n}/{self._config.max_unattributed_crash_hunts}. The final "
                f"clean validation pass is owed again."
            )
            if n >= self._config.max_unattributed_crash_hunts:
                self.log_message.emit(
                    "The machine keeps dying around validation with no "
                    "attributable evidence. Pausing for your decision: rule "
                    "out an external cause (foreign load, another tool, "
                    "power/BIOS), or lower max_offset. Resume continues "
                    "validation."
                )
                # The owed clean pass survives the pause: persist dirty with
                # the session's own cursor (the engine's is not restored yet).
                self._db.set_validation_position(
                    session_id,
                    session.validation_stage,
                    session.validation_index,
                    session.validation_half,
                    True,
                    session.validation_requeue or "[]",
                )
                self.pause()
                return

        # An unattributed crash outranks re-entering validation: find the
        # culprit in isolation first, or validation just crashes again.
        if pending_hunt:
            if (clean_reboot or not rebooted) and self._hunt is not None:
                resumed = self._hunt
            else:
                try:
                    resumed = bisect.HuntState.from_json(session.hunt_state)
                except bisect.InvalidHuntState as exc:
                    self.log_message.emit(f"Persisted hunt state is invalid: {exc}. Pausing without applying CO.")
                    self.pause()
                    return
            if resumed is not None:
                self._hunt = resumed
                self._hunt_workload = resumed.workload
                self._hunting = True
                self._transition_status("hunting")
                if resumed.armed:
                    self.log_message.emit(
                        f"Resumed session {session_id} — the machine died under hunt probe "
                        f"{resumed.in_flight or 'stock'}; recording that reproduction."
                    )
                    resumed.armed = False
                    self._record_hunt_probe(reproduced=True)
                else:
                    self.log_message.emit(f"Resumed session {session_id} — continuing the pending attribution hunt")
                self._run_next_hunt_slot()
                return
            self.log_message.emit(f"Resumed session {session_id} — starting attribution hunt")
            self._db.update_tuner_session_status(session_id, "validating")
            self._start_hunt(loaded=self._pending_hunt_loaded)
            return

        # Check if all cores are confirmed — if so, we were paused during
        # validation and should re-enter validation instead of per-core search.
        all_confirmed = all(cs.phase == TunerPhase.CONFIRMED for cs in self._core_states.values())
        if all_confirmed and self._config.auto_validate and len(self._core_states) > 1:
            profile = {cs.core_id: cs.best_offset for cs in self._core_states.values() if cs.best_offset is not None}
            self.log_message.emit(f"Resumed session {session_id} — all cores confirmed, re-entering validation")
            self._enter_auto_validation(profile, resume_from=session)
            if unattributed_incident and not self._validation_dirty:
                # The incident invalidates any clean-pass credit: even if the
                # remaining stages pass, one full clean pass is owed.
                self._validation_dirty = True
                self._save_validation_pos()
        else:
            self._transition_status("running")
            self.log_message.emit(f"Resumed session {session_id}")
            self._run_next()

    def pause(self) -> None:
        if self._session_id:
            session = self._db.get_tuner_session(self._session_id)
            if session is not None and session.status in {"platform_fault", "profile_quarantined"}:
                self.log_message.emit(f"Pause ignored: the session remains {session.status}.")
                return
        self._paused = True
        self._transition_status("paused")
        self.log_message.emit("Tuner paused - will stop after current test")

    def abort(self) -> None:
        clear_hunt = not self._hunting
        if not self._stop_and_restore("Abort"):
            return
        if clear_hunt and self._session_id is not None:
            self._db.set_hunt_state(self._session_id, "")
        self._transition_status("aborted")
        self._set_status("idle")
        self.log_message.emit("Tuner aborted")

    def shutdown(self) -> bool:
        """Stop for application exit: restore baselines and leave the session
        paused, so closing the window never discards a resumable search."""
        if not self._stop_and_restore("Shutdown"):
            return False
        session = self._db.get_tuner_session(self._session_id) if self._session_id is not None else None
        if session is not None and session.status in RESUMABLE_STATUSES:
            self._transition_status("paused")
            self._set_status("idle")
            self.log_message.emit("Tuner paused for application exit")
        return True

    def _stop_and_restore(self, action: str) -> bool:
        self._abort_requested = True
        self._stop_freeze_monitor()
        if self._worker is not None:
            with contextlib.suppress(RuntimeError):
                self._worker.finished.disconnect(self._on_test_finished)
            if self._worker.isRunning():
                with contextlib.suppress(Exception):
                    self._worker.scheduler.force_stop()
                stopped = self._worker.wait(5000)
                if not stopped:
                    self._worker.terminate()
                    stopped = self._worker.wait(3000)
                if not stopped:
                    self.log_message.emit(
                        f"{action} incomplete: stress worker is still alive; tuner ownership is retained"
                    )
                    return False
            self._worker.deleteLater()
            self._worker = None
        self._clear_all_in_test()
        if self._revert_all_to_baseline(force=True):
            return False
        self._validation_stage = 0
        self._in_requeue = False
        self._soaking = False
        if self._hunting:
            self._hunting = False
            self._requeue_hunt_probe()
        return True

    def validate_profile(self, session_id: int) -> None:
        """Re-test all confirmed values from a completed session."""
        if self._worker is not None and self._worker.isRunning():
            self.log_message.emit("Validate ignored: a test is still running — wait for it to finish.")
            return
        map_err = self._co_access_error()
        if map_err is not None:
            self.log_message.emit(f"Cannot validate: per-core CO is unavailable — {map_err}")
            return
        self._abort_requested = False
        self._paused = False
        self._validation_stage = 0
        self._validation_dirty = False
        self._validation_requeue = []
        self._in_requeue = False
        self._hunting = False
        self._soaking = False
        self._session_id = session_id

        profile = self._db.get_tuner_best_profile(session_id)
        if not profile:
            self.log_message.emit("No confirmed cores to validate")
            return

        session = self._db.get_tuner_session(session_id)
        if session and not self._load_saved_config(session.config_json):
            return
        self._db.set_session_boot(session_id, self._boot_id)
        self._db.set_validation_position(session_id, 0, 0, 0, False, "[]")

        # Reset confirmed cores to "confirming" for re-validation
        self._core_states = self._db.get_tuner_core_states(session_id)
        # Reset CO tracking — SMU state is unknown, force fresh writes
        self._co_applied = {core_id: None for core_id in self._core_states}
        for core_id, offset in profile.items():
            if core_id in self._core_states:
                cs = self._core_states[core_id]
                cs.phase = TunerPhase.CONFIRMING
                cs.current_offset = offset
                cs.best_offset = offset
                cs.confirm_attempts = 0
                cs.battery_index = 0
                self._battery_orders.pop(core_id, None)
                self._db.upsert_tuner_core_state(self._session_id, cs)

        self._transition_status("validating")
        self.log_message.emit(f"Validating {len(profile)} core(s) from session {session_id}")
        self._run_next()

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------

    def _regime_weights(self) -> dict[str, float]:
        """Share of slot time each regime earns, from its observed yield.

        A regime that has never caught anything on this silicon is cheap to
        keep and expensive to trust, so it keeps a floor share rather than
        being scheduled away: absence of failures in a regime is precisely
        the fact the search is trying to establish.
        """
        names = [str(r) for r in Regime]
        context = self.context_id()
        observed = self._db.regime_yield(context) if context is not None else {}
        # Failures per hour, with an unseen regime starting optimistic so it
        # gets real time before the weighting has any evidence to act on.
        rate = {}
        for name in names:
            failures, seconds = observed.get(name, (0, 0.0))
            rate[name] = (failures + 0.5) / (seconds / 3600.0 + 1.0)
        total = sum(rate.values())
        floor = min(max(0.0, self._config.regime_floor_pct / 100.0), 1.0 / len(names))
        adaptive = 1.0 - floor * len(names)
        return {name: floor + adaptive * rate[name] / total for name in names}

    def _slot_regimes(self, cs: CoreState) -> list[str]:
        """Yield-ranked regimes with persisted completed identities kept first."""
        if cs.phase is TunerPhase.COARSE_SEARCH:
            wanted = [str(regime) for regime in self._config.coarse_regimes]
        else:
            wanted = [str(regime) for regime in Regime]
        covered = {str(entry["regime"]) for entry in self._config.battery}
        eligible = [regime for regime in wanted if regime in covered]
        key = (str(cs.phase), cs.current_offset)
        frozen = self._battery_orders.get(cs.core_id)
        if frozen is not None and frozen[0] == key:
            return frozen[1]

        completed: list[str] = []
        if cs.battery_index > 0 and self._session_id is not None:
            for row in reversed(self._db.get_tuner_test_log(self._session_id, core_id=cs.core_id)):
                regime = row.get("regime")
                if (
                    row.get("passed")
                    and row.get("offset_tested") == cs.current_offset
                    and regime in eligible
                    and regime not in completed
                ):
                    completed.append(regime)
                    if len(completed) == cs.battery_index:
                        break
            completed.reverse()
        weights = self._regime_weights()
        remaining = sorted(
            (regime for regime in eligible if regime not in completed),
            key=lambda regime: (-weights.get(regime, 0.0), regime),
        )
        regimes = completed + remaining
        self._battery_orders[cs.core_id] = (key, regimes)
        return regimes

    def _active_regime(self, cs: CoreState) -> str | None:
        """The single regime whose verdict this slot produces, if any."""
        if self._hunting:
            return None
        if self._validation_stage == 9 and not self._in_requeue:
            workload = self._config.endurance_workloads[self._endurance_workload]
            return str(workload["regime"]) if workload.get("regime") else None
        if self._validation_stage > 0:
            return None
        regimes = self._slot_regimes(cs)
        return regimes[cs.battery_index % len(regimes)] if regimes else None

    def _resolve_anneal(self, cs: CoreState, passed: bool) -> None:
        """Settle an annealing probe: promotion is earned, demotion is free.

        A pass banks the deeper offset as the new answer and resets the bar.
        A fail returns the core to the offset it had already proven and
        doubles what the next probe costs, so a marginal depth is not retried
        forever while a genuinely better one still gets found.
        """
        contradicted = cs.backoff_fail_bound is not None and (
            cs.current_offset == cs.backoff_fail_bound
            or self._is_more_aggressive(cs.current_offset, cs.backoff_fail_bound)
        )
        if passed and not contradicted:
            cs.best_offset = cs.current_offset
            cs.proven_offset = cs.best_offset
            cs.anneal_strikes = 0
            cs.anneal_bar_hours = float(self._config.anneal_bank_hours)
            self.log_message.emit(f"Core {cs.core_id}: annealed deeper to {cs.best_offset}")
        else:
            cs.current_offset = cs.best_offset if cs.best_offset is not None else cs.current_offset
            cs.anneal_strikes += 1
            cs.anneal_bar_hours = self._anneal_bar(cs) * 2
            self.log_message.emit(
                f"Core {cs.core_id}: anneal probe failed - back to {cs.current_offset}, "
                f"next probe needs {cs.anneal_bar_hours:.0f}h clean "
                f"(strike {cs.anneal_strikes}/{self._config.anneal_max_strikes})"
            )
        cs.phase = TunerPhase.CONFIRMED
        cs.battery_index = 0
        self._battery_orders.pop(cs.core_id, None)
        if self._session_id:
            self._db.upsert_tuner_core_state(self._session_id, cs)
        self.core_state_changed.emit(cs.core_id, cs.phase, cs.current_offset)

    def _bank_clean_time(self, core_id: int, offset: int, regime: str, seconds: float) -> None:
        """Credit survived seconds to one (core, regime, offset) bucket.

        Confidence is keyed to the operating point, not the session, so it
        survives reboots; with no context there is nothing to key it to and
        nothing is banked rather than banking a claim we cannot qualify.
        """
        context = self.context_id()
        if context is None or seconds <= 0:
            return
        self._db.bank_regime_time(context, core_id, regime, offset, float(seconds))

    def _breadcrumb_path(self) -> Path:
        """Where the pre-freeze context lands.

        Durable storage, not the work dir: the whole point is to still be
        there after the machine dies, and the work dir is tmpfs.
        """
        return user_home() / ".local/share/corecycler/microfreeze.txt"

    def _start_freeze_monitor(self) -> None:
        """Begin recording scheduling hitches for the session.

        The monitor never produces a verdict. It answers the one question a
        hard freeze otherwise destroys the answer to: what was the machine
        doing a second before it stopped responding.
        """
        if self._freeze is not None:
            return
        path = self._breadcrumb_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.debug("Micro-freeze breadcrumb directory unavailable: %s", e)
            return
        self._freeze = MicroFreezeMonitor(path)
        if self._freeze_slot_context:
            self._freeze.set_context(self._freeze_slot_context)
        self._freeze.start()

    def _stop_freeze_monitor(self) -> bool:
        if self._freeze is None:
            return True
        if not self._freeze.stop():
            self.log_message.emit("Micro-freeze sampler did not stop; retaining it until its writer exits")
            return False
        self._freeze = None
        return True

    def _freeze_context(self, context: str) -> None:
        self._freeze_slot_context = context
        if self._freeze is not None:
            self._freeze.set_context(context)

    def _read_breadcrumb(self) -> str:
        """What the last breadcrumb says the machine was doing when it died."""
        try:
            raw = self._breadcrumb_path().read_text()
        except OSError:
            return ""
        fields = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
        context = fields.get("context", "").strip()
        worst = fields.get("worst_latency_ms", "").strip()
        if not context:
            return ""
        return f"{context} (worst scheduling hitch {worst}ms in the minute before the freeze)"

    def _banked_hours(self, cs: CoreState) -> float:
        """Clean hours in the weakest regime at this core's current best offset."""
        context = self.context_id()
        offset = cs.best_offset
        if context is None or offset is None:
            return 0.0
        banks = self._db.get_regime_banks(context, cs.core_id, offset)
        covered = sorted({str(entry["regime"]) for entry in self._config.battery})
        return min(banks.get(regime, 0.0) for regime in covered) / 3600.0

    def _anneal_bar(self, cs: CoreState) -> float:
        """Clean hours this core must bank before it may probe a step deeper."""
        return cs.anneal_bar_hours or float(self._config.anneal_bank_hours)

    def _anneal_candidate(self) -> int | None:
        """Pick a banked confirmed core and probe one configured fine step deeper."""
        if self._config.anneal_bank_hours <= 0:
            return None
        best: tuple[float, int] | None = None
        for core_id, cs in sorted(self._core_states.items()):
            if cs.phase is not TunerPhase.CONFIRMED or cs.best_offset is None:
                continue
            if cs.anneal_strikes >= self._config.anneal_max_strikes:
                continue
            candidate = cs.best_offset + self._config.direction * self._config.fine_step
            if self._exceeds_max(candidate) or (
                cs.backoff_fail_bound is not None
                and (candidate == cs.backoff_fail_bound or self._is_more_aggressive(candidate, cs.backoff_fail_bound))
            ):
                continue
            hours = self._banked_hours(cs)
            if hours < self._anneal_bar(cs):
                continue
            if best is None or hours > best[0]:
                best = (hours, core_id)
        if best is None:
            return None
        core_id = best[1]
        cs = self._core_states[core_id]
        cs.phase = TunerPhase.ANNEALING
        cs.current_offset = cs.best_offset + self._config.direction * self._config.fine_step
        cs.battery_index = 0
        self._battery_orders.pop(core_id, None)
        self.log_message.emit(
            f"Core {core_id}: {best[0]:.1f}h banked in every regime at {cs.best_offset} - probing {cs.current_offset}"
        )
        return core_id

    def _battery_entry(self, cs: CoreState) -> dict:
        """The battery workload this slot should run right now."""
        regimes = self._slot_regimes(cs)
        target = regimes[cs.battery_index % len(regimes)]
        candidates = [e for e in self._config.battery if str(e["regime"]) == target]
        return candidates[self._regime_rotation.get(target, 0) % len(candidates)]

    def _battery_duration(self, cs: CoreState, per_regime: int) -> int:
        """Split the slot's budget across its regimes by yield weight.

        The total is unchanged - only which regime spends it moves.
        """
        regimes = self._slot_regimes(cs)
        weights = self._regime_weights()
        share = {r: weights.get(r, 0.0) for r in regimes}
        total = sum(share.values())
        target = regimes[cs.battery_index % len(regimes)]
        return max(1, round(per_regime * len(regimes) * share[target] / total))

    def _active_workload(self, cs: CoreState | None) -> dict | None:
        """The workload entry driving the current battery, hunt, or endurance slot."""
        if self._hunting and self._hunt_workload is not None:
            return self._hunt_workload
        if self._validation_stage == 9 and not self._in_requeue:
            return self._config.endurance_workloads[self._endurance_workload]
        if self._validation_stage == 0 and cs is not None:
            return self._battery_entry(cs)
        return None

    def _get_active_stress_config(self, cs: CoreState) -> tuple[str, str, str, int | None]:
        """Return (backend, stress_mode, fft_preset, threads) for the active slot."""
        wl = self._active_workload(cs)
        if wl is None:
            return self._config.backend, self._config.stress_mode, self._config.fft_preset, None
        return wl["backend"], wl["stress_mode"], wl["fft_preset"], wl.get("threads")

    def _workload_snapshot(
        self,
        cs: CoreState,
        *,
        backend: str | None = None,
        stress_mode: str | None = None,
        fft_preset: str | None = None,
        threads: int | None = None,
        profile: str | None = None,
        tests: list[str] | tuple[str, ...] | None = None,
    ) -> dict:
        """Serializable recipe for replaying exactly the workload now launching."""
        active = self._active_workload(cs)
        workload = dict(active) if active is not None else {}
        selected_profile = profile or str(workload.get("profile", "sustained"))
        regime = workload.get("regime") or self._active_regime(cs)
        if regime is None:
            regime = (
                "transient"
                if selected_profile == "transient"
                else "boost"
                if selected_profile == "spectrum"
                else "current"
            )
        workload.update(
            regime=str(regime),
            backend=backend or str(workload.get("backend", self._config.backend)),
            stress_mode=stress_mode or str(workload.get("stress_mode", self._config.stress_mode)),
            fft_preset=fft_preset or str(workload.get("fft_preset", self._config.fft_preset)),
            profile=selected_profile,
        )
        if threads is not None:
            workload["threads"] = threads
        if tests:
            workload["tests"] = list(tests)
        return workload

    def _checkpoint_worker(self, workload: dict, loaded: list[int]) -> None:
        """Persist crash context, and arm only a hunt probe, before worker launch."""
        if self._session_id is None:
            return
        vector = tp.journal_values(self._db, self._session_id)
        if self._hunting:
            if self._hunt is None:
                self.log_message.emit("Hunt state vanished before worker launch; pausing.")
                self.pause()
                return
            self._hunt.armed = True
            self._save_hunt()
        else:
            candidates = self._hunt_candidates(vector)
            if not candidates:
                self._db.set_hunt_state(self._session_id, "")
                self._db.checkpoint()
                return
            checkpoint = bisect.begin(
                candidates,
                sorted(c for c in loaded if c in self._core_states),
            )
            checkpoint.vector = vector
            checkpoint.workload = workload
            self._db.set_hunt_state(self._session_id, checkpoint.to_json())
        self._db.checkpoint()

    def _threads_for(self, core_id: int, requested: int | None) -> int:
        """Clamp a workload's requested thread count to the core's SMT width."""
        core_info = self._topology.cores.get(core_id)
        n = max(1, len(core_info.logical_cpus) if core_info else 1)
        return n if requested is None else max(1, min(requested, n))

    def _get_backend_for_name(self, name: str) -> StressBackend:
        """Return the injected primary backend or instantiate a named tier backend."""
        if name == self._config.backend:
            return self._backend
        try:
            return get_backend(name)
        except KeyError:
            load_all()
            return get_backend(name)

    def _advance_core(self, core_id: int, passed: bool) -> None:
        """State machine transitions for a single core."""
        cs = self._core_states[core_id]
        cfg = self._config
        if cs.phase is TunerPhase.ANNEALING:
            self._resolve_anneal(cs, passed)
            return
        direction = cfg.direction  # -1 for undervolting

        # Total-function normalization: the backoff arithmetic assumes
        # best_offset is set (the crash penalty seeds it), but a persisted row
        # from an older version or a hand-edit can violate that. Fail closed to
        # the baseline instead of a TypeError mid-transition.
        if cs.best_offset is None and cs.phase in (
            TunerPhase.BACKOFF_PRECONFIRM,
            TunerPhase.BACKOFF_CONFIRMING,
        ):
            cs.best_offset = cs.baseline_offset

        if not passed and cs.phase in (TunerPhase.BACKOFF_PRECONFIRM, TunerPhase.BACKOFF_CONFIRMING):
            if cs.backoff_fail_bound is None or self._is_more_aggressive(cs.backoff_fail_bound, cs.current_offset):
                cs.backoff_fail_bound = cs.current_offset
            if cs.backoff_pass_bound is not None and not self._is_more_aggressive(
                cs.current_offset, cs.backoff_pass_bound
            ):
                cs.backoff_pass_bound = None

        if (
            not passed
            and cs.phase
            in (
                TunerPhase.CONFIRMING,
                TunerPhase.BACKOFF_PRECONFIRM,
                TunerPhase.BACKOFF_CONFIRMING,
            )
            and self._at_or_past_baseline(cs.current_offset, cs)
        ):
            self.log_message.emit(f"Core {core_id}: baseline failed; pausing without confirmation")
            self.pause()
            if self._session_id:
                self._db.upsert_tuner_core_state(self._session_id, cs)
            return

        # Contradictory-evidence guard: a PASS at/beyond the recorded fail
        # bound must never widen the bounds — failures outrank passes for
        # safety, and letting the pass through inverts the bounds so the
        # backoff binary search DIVERGES toward more aggressive values.
        if (
            passed
            and cs.phase in (TunerPhase.BACKOFF_PRECONFIRM, TunerPhase.BACKOFF_CONFIRMING)
            and self._handle_contradictory_pass(cs)
        ):
            if self._session_id:
                self._db.upsert_tuner_core_state(self._session_id, cs)
            self.core_state_changed.emit(cs.core_id, cs.phase, cs.current_offset)
            return

        match cs.phase:
            case TunerPhase.NOT_STARTED:
                # First step: enter coarse search
                cs.phase = TunerPhase.COARSE_SEARCH
                # Use inherited offset as base when inherit_current is active
                base = cs.current_offset if (cfg.inherit_current and cs.current_offset != 0) else cfg.start_offset
                cs.current_offset = base + direction * cfg.coarse_step
                if self._exceeds_max(cs.current_offset):
                    cs.current_offset = cfg.max_offset

            case TunerPhase.COARSE_SEARCH:
                if passed:
                    cs.best_offset = cs.current_offset
                    next_offset = cs.current_offset + direction * self._get_coarse_step(cs)
                    if self._exceeds_max(next_offset):
                        # Hit the limit — settle here
                        cs.phase = TunerPhase.SETTLED
                    else:
                        cs.current_offset = next_offset
                else:
                    # Coarse search failed
                    cs.coarse_fail_offset = cs.current_offset
                    if cs.best_offset is None:
                        if cs.current_offset == cfg.start_offset + direction * cfg.coarse_step:
                            self._consecutive_start_failures += 1
                        next_offset = cs.current_offset - direction * cfg.fine_step
                        cs.backoff_fail_bound = cs.current_offset
                        cs.best_offset = cs.current_offset = (
                            cs.baseline_offset if self._at_or_past_baseline(next_offset, cs) else next_offset
                        )
                        cs.backoff_mode = True
                        cs.phase = TunerPhase.BACKOFF_PRECONFIRM
                    else:
                        # Fine search between best_offset and coarse_fail
                        cs.phase = TunerPhase.FINE_SEARCH
                        cs.current_offset = cs.best_offset + direction * cfg.fine_step
                        # Never start the first fine test past the safety cap.
                        if self._exceeds_max(cs.current_offset):
                            cs.current_offset = cfg.max_offset
                        # Don't re-test the known coarse-fail offset (guaranteed
                        # fail) — settle at the last good value instead.
                        if cs.coarse_fail_offset is not None and (
                            (direction < 0 and cs.current_offset <= cs.coarse_fail_offset)
                            or (direction > 0 and cs.current_offset >= cs.coarse_fail_offset)
                        ):
                            cs.phase = TunerPhase.SETTLED

            case TunerPhase.FINE_SEARCH:
                if passed:
                    cs.best_offset = cs.current_offset
                    next_offset = cs.current_offset + direction * cfg.fine_step
                    # Stop if we'd reach or pass the coarse fail point
                    if (
                        cs.coarse_fail_offset is not None
                        and (
                            (direction < 0 and next_offset <= cs.coarse_fail_offset)
                            or (direction > 0 and next_offset >= cs.coarse_fail_offset)
                        )
                        or self._exceeds_max(next_offset)
                    ):
                        cs.phase = TunerPhase.SETTLED
                    else:
                        cs.current_offset = next_offset
                else:
                    # Fine search failed — settle at last good value
                    cs.phase = TunerPhase.SETTLED

            case TunerPhase.SETTLED:
                # Move to confirmation
                if cs.best_offset is not None:
                    cs.phase = TunerPhase.CONFIRMING
                    cs.current_offset = cs.best_offset
                else:
                    cs.phase = TunerPhase.CONFIRMING
                    cs.best_offset = cs.baseline_offset
                    cs.current_offset = cs.baseline_offset

            case TunerPhase.CONFIRMING:
                if passed:
                    cs.phase = TunerPhase.CONFIRMED
                    cs.confirm_attempts = 0
                else:
                    cs.confirm_attempts += 1
                    if cs.confirm_attempts >= cfg.max_confirm_retries:
                        # Back off and re-enter fine search
                        cs.phase = TunerPhase.FAILED_CONFIRM
                    # else: retry confirmation (stays in confirming)

            case TunerPhase.FAILED_CONFIRM:
                # Back off by one fine step and enter backoff preconfirm
                if cs.best_offset is not None:
                    new_best = cs.best_offset - direction * cfg.fine_step
                    if self._at_or_past_baseline(new_best, cs):
                        cs.phase = TunerPhase.BACKOFF_PRECONFIRM
                        cs.best_offset = cs.baseline_offset
                        cs.current_offset = cs.baseline_offset
                    else:
                        cs.best_offset = new_best
                        cs.current_offset = new_best
                        cs.phase = TunerPhase.BACKOFF_PRECONFIRM
                        cs.backoff_mode = True
                        cs.confirm_attempts = 0
                        cs.consecutive_backoff_fails = 0
                else:
                    cs.phase = TunerPhase.BACKOFF_PRECONFIRM
                    cs.best_offset = cs.baseline_offset
                    cs.current_offset = cs.baseline_offset

            case TunerPhase.BACKOFF_PRECONFIRM:
                if passed:
                    # The value that just passed IS the proven best (handles the
                    # crash-before-any-pass case where best_offset was None).
                    cs.best_offset = cs.current_offset
                    had_pass_bound = cs.backoff_pass_bound is not None
                    cs.backoff_pass_bound = cs.best_offset
                    if had_pass_bound and cs.backoff_fail_bound is not None:
                        # Binary search active — jump to midpoint
                        gap = abs(cs.backoff_fail_bound - cs.backoff_pass_bound)
                        if gap <= cfg.fine_step:
                            cs.best_offset = cs.backoff_pass_bound
                            cs.current_offset = cs.backoff_pass_bound
                            cs.phase = TunerPhase.BACKOFF_CONFIRMING
                        else:
                            # Probe the midpoint as current ONLY; best stays at the
                            # proven pass bound until the midpoint itself passes.
                            mid = cs.backoff_pass_bound + direction * (gap // 2)
                            cs.current_offset = mid
                            # Stay in backoff_preconfirm for next test
                    else:
                        # First pass in backoff — enter confirmation
                        cs.phase = TunerPhase.BACKOFF_CONFIRMING
                        cs.current_offset = cs.best_offset
                        cs.confirm_attempts = 0
                else:
                    cs.consecutive_backoff_fails += 1
                    if cs.backoff_pass_bound is not None:
                        gap = abs(cs.backoff_fail_bound - cs.backoff_pass_bound)
                        cs.best_offset = cs.backoff_pass_bound
                        if gap <= cfg.fine_step:
                            cs.phase = TunerPhase.BACKOFF_CONFIRMING
                            cs.current_offset = cs.backoff_pass_bound
                        else:
                            cs.current_offset = cs.backoff_pass_bound + direction * (gap // 2)
                    else:
                        step = cfg.fine_step
                        if cs.consecutive_backoff_fails >= cfg.midpoint_jump_threshold:
                            step = max(step, abs(cs.current_offset - cs.baseline_offset) // 2)
                            cs.consecutive_backoff_fails = 0
                        next_offset = cs.current_offset - direction * step
                        cs.best_offset = cs.current_offset = (
                            cs.baseline_offset if self._at_or_past_baseline(next_offset, cs) else next_offset
                        )

            case TunerPhase.BACKOFF_CONFIRMING:
                if passed:
                    # The confirmed value is proven — record it as best and pass bound.
                    cs.best_offset = cs.current_offset
                    cs.backoff_pass_bound = cs.current_offset
                    if cs.backoff_fail_bound is not None:
                        # Binary search: try midpoint between pass and fail bounds
                        gap = abs(cs.backoff_fail_bound - cs.backoff_pass_bound)
                        if gap <= cfg.fine_step:
                            # Converged: settle at the proven pass bound, then harden.
                            cs.best_offset = cs.backoff_pass_bound
                            cs.current_offset = cs.backoff_pass_bound
                            cs.phase = TunerPhase.CONFIRMED
                        else:
                            # Probe the midpoint as current ONLY (never recorded as
                            # best until it passes).
                            mid = cs.backoff_pass_bound + direction * (gap // 2)
                            cs.current_offset = mid
                            cs.phase = TunerPhase.BACKOFF_PRECONFIRM
                    else:
                        cs.phase = TunerPhase.CONFIRMED
                else:
                    # Confirm failed — back to preconfirm, back off
                    cs.phase = TunerPhase.BACKOFF_PRECONFIRM
                    new_offset = cs.best_offset - direction * cfg.fine_step
                    floor = self._backoff_floor(cs, new_offset)
                    if floor is not None:
                        cs.phase = TunerPhase.BACKOFF_CONFIRMING
                        cs.best_offset = floor
                        cs.current_offset = floor
                    elif self._at_or_past_baseline(new_offset, cs):
                        cs.phase = TunerPhase.BACKOFF_PRECONFIRM
                        cs.best_offset = cs.baseline_offset
                        cs.current_offset = cs.baseline_offset
                    else:
                        cs.best_offset = new_offset
                        cs.current_offset = new_offset

        if passed and cs.phase is TunerPhase.CONFIRMED:
            cs.proven_offset = cs.best_offset
        # Persist
        if self._session_id:
            self._db.upsert_tuner_core_state(self._session_id, cs)
        self.core_state_changed.emit(cs.core_id, cs.phase, cs.current_offset)

    def _handle_contradictory_pass(self, cs: CoreState) -> bool:
        """Handle a PASS at an offset at-or-beyond the recorded fail bound.

        That is contradictory evidence — intermittent instability, or stale
        persisted bounds. The conservative resolution: the failure stands, the
        pass is not allowed to widen the bounds; step back to just inside the
        fail bound and keep searching there. Returns True when handled.
        """
        fb = cs.backoff_fail_bound
        if fb is None or self._is_more_aggressive(fb, cs.current_offset):
            return False
        step_back = fb - self._config.direction * self._config.fine_step
        if self._at_or_past_baseline(step_back, cs):
            cs.phase = TunerPhase.BACKOFF_PRECONFIRM
            if self._at_or_past_baseline(fb, cs):
                self.log_message.emit(f"Core {cs.core_id}: baseline contradicts a known failure; pausing")
                self.pause()
            cs.best_offset = cs.baseline_offset
            cs.current_offset = cs.baseline_offset
        else:
            cs.phase = TunerPhase.BACKOFF_PRECONFIRM
            cs.current_offset = step_back
            if cs.best_offset is not None and self._is_more_aggressive(cs.best_offset, step_back):
                cs.best_offset = step_back
        return True

    def _get_coarse_step(self, cs: CoreState) -> int:
        """Get coarse step size, reducing near max_offset for safety."""
        distance = abs(cs.current_offset - self._config.max_offset)
        ramp_zone = self._config.coarse_step * 2
        if distance <= ramp_zone:
            return self._config.fine_step
        return self._config.coarse_step

    def _exceeds_max(self, offset: int) -> bool:
        """Check if offset exceeds max_offset in the configured direction."""
        if self._config.direction < 0:
            return offset < self._config.max_offset
        return offset > self._config.max_offset

    def _resolve_seeds(self, seed_offsets: dict[int, int] | None, cores: list[int]) -> dict[int, int]:
        """Keep the seeds this search can actually act on, clamped to its cap.

        A seed that is not more aggressive than the configured start has
        nothing to contribute, and a seed for a core outside this session is
        not this session's business.
        """
        if not seed_offsets:
            return {}
        resolved: dict[int, int] = {}
        for core_id in cores:
            seed = seed_offsets.get(core_id)
            if seed is None or not self._is_more_aggressive(seed, self._config.start_offset):
                continue
            resolved[core_id] = self._config.max_offset if self._exceeds_max(seed) else seed
        return resolved

    def _at_or_past_baseline(self, offset: int, cs: CoreState) -> bool:
        """Check if offset is at or past the core's baseline in the configured direction."""
        if self._config.direction < 0:
            return offset >= cs.baseline_offset
        return offset <= cs.baseline_offset

    def _is_more_aggressive(self, a: int, b: int) -> bool:
        """Returns True if offset a is more aggressive than b."""
        if self._config.direction == -1:
            return a < b
        return a > b

    def _backoff_floor(self, cs: CoreState, new_offset: int) -> int | None:
        """A confirmed backoff pass_bound is a hard floor for the fail paths.

        Returns the pass_bound to settle at when ``new_offset`` would be less
        aggressive than it — a fully-confirmed offset must never be abandoned
        for a weaker one — else None (the new offset is safe to use).
        """
        pb = cs.backoff_pass_bound
        if pb is not None and self._is_more_aggressive(pb, new_offset):
            return pb
        return None

    def _apply_crash_penalty(self, cs: CoreState, *, steps: int | None = None, count_crash: bool = True) -> None:
        """Apply crash penalty: backoff + hard fail bound (+ cooldown).

        ``steps`` overrides crash_penalty_steps for evidence-grade reactions —
        a corrected MCE is a warning, not a crash, so it backs off one step
        (count_crash=False keeps crash bookkeeping honest: nothing crashed).
        All safety invariants (fail-bound monotonicity, CO=0 floor, baseline
        descent, confirmation invalidation) apply identically.
        """
        crashed_offset = cs.current_offset
        invalidated_confirmation = cs.phase in (TunerPhase.CONFIRMED, TunerPhase.ANNEALING)
        cs.battery_index = 0
        cs.confirm_attempts = 0
        self._battery_orders.pop(cs.core_id, None)
        if crashed_offset == 0:
            if count_crash:
                cs.crash_count += 1
            self.log_message.emit(
                f"Core {cs.core_id}: failure at CO=0 is a platform fault, not a tunable offset; pausing."
            )
            self.pause()
            return
        # fail_bound tracks the LEAST aggressive offset known to fail. Stability is
        # monotonic (anything more aggressive than a failing offset also fails), so
        # this is the tightest SAFE bound, and it lets the backoff binary search
        # converge: a crash at a midpoint less aggressive than the old bound must
        # TIGHTEN it, otherwise the search oscillates forever.
        if cs.backoff_fail_bound is None or not self._is_more_aggressive(crashed_offset, cs.backoff_fail_bound):
            cs.backoff_fail_bound = crashed_offset
        if cs.backoff_pass_bound is not None and not self._is_more_aggressive(crashed_offset, cs.backoff_pass_bound):
            cs.backoff_pass_bound = None
        if self._session_id is not None:
            session = self._db.get_tuner_session(self._session_id)
            if session is not None and session.validation_stage > 0:
                self._validation_dirty = True
                self._db.set_validation_position(
                    self._session_id,
                    session.validation_stage,
                    session.validation_index,
                    session.validation_half,
                    True,
                    session.validation_requeue,
                )
        # Back off by crash_penalty_steps (or the caller's override)
        penalty = (steps if steps is not None else self._config.crash_penalty_steps) * self._config.fine_step
        new_offset = crashed_offset - (self._config.direction * penalty)
        # CO=0 (stock voltage) is the only axiomatically safe state. Never let a
        # backoff overshoot past 0 to the opposite, more-aggressive side.
        if self._is_more_aggressive(0, new_offset):
            new_offset = 0
        if self._at_or_past_baseline(crashed_offset, cs):
            # The crash happened at or below the baseline's aggressiveness, so the
            # baseline itself is unstable — it is NOT a safe floor. Descend the
            # baseline toward 0 so the search can never again settle on the value
            # that just crashed the machine. This is what breaks the resume loop
            # where an unstable baseline is re-applied on every boot.
            cs.baseline_offset = new_offset
            cs.current_offset = new_offset
        elif self._at_or_past_baseline(new_offset, cs):
            # Normal search crash (more aggressive than baseline): stop at the
            # proven-stable baseline.
            cs.current_offset = cs.baseline_offset
        else:
            cs.current_offset = new_offset
        if count_crash:
            cs.crash_count += 1
            cs.crash_cooldown = 2
        # A core that crashed before ever passing has no proven-safe best yet; the
        # only known-safe value is its baseline. Seed it so the backoff math (which
        # assumes best_offset is set) never produces a None offset.
        if cs.best_offset is None:
            cs.best_offset = cs.baseline_offset
        # A value resident at a hard crash can never remain "best": validation and
        # finalize re-apply best_offset, so leaving it would re-crash the box on
        # every resume. Demote it to the penalized offset (backoff-candidate
        # semantics — it must still pass a test before being confirmed again).
        elif self._is_more_aggressive(cs.best_offset, cs.current_offset):
            cs.best_offset = cs.current_offset
        if invalidated_confirmation:
            cs.anneal_strikes = self._config.anneal_max_strikes
        # Force into backoff — including CONFIRMING and CONFIRMED: a hard
        # crash at a confirmed value invalidates the confirmation, and the core
        # must re-earn it (otherwise validation re-applies the crashed value).
        if cs.phase in (
            TunerPhase.COARSE_SEARCH,
            TunerPhase.FINE_SEARCH,
            TunerPhase.CONFIRMING,
            TunerPhase.CONFIRMED,
            TunerPhase.BACKOFF_PRECONFIRM,
        ):
            cs.phase = TunerPhase.BACKOFF_PRECONFIRM
            cs.backoff_mode = True

    def _apparatus_suspect(self, core_id: int) -> bool:
        """Pause on repeated contradictions without discarding failure evidence."""
        threshold = self._config.apparatus_failure_streak
        if threshold <= 0 or self._session_id is None:
            return False
        rows = [
            r
            for r in self._db.get_tuner_test_log(self._session_id, core_id=core_id)
            if r.get("duration_seconds") is not None
        ]
        proven: dict[tuple[str | int | None, ...], int] = {}
        for r in rows:
            if not r["passed"]:
                continue
            workload = (
                r.get("backend"),
                r.get("stress_mode"),
                r.get("fft_preset"),
                r.get("threads"),
                r.get("profile"),
                r.get("regime"),
            )
            best = proven.get(workload)
            if best is None or self._is_more_aggressive(r["offset_tested"], best):
                proven[workload] = r["offset_tested"]
        streak = 0
        for r in reversed(rows):
            if r["passed"]:
                break
            workload = (
                r.get("backend"),
                r.get("stress_mode"),
                r.get("fft_preset"),
                r.get("threads"),
                r.get("profile"),
                r.get("regime"),
            )
            best = proven.get(workload)
            if best is None or self._is_more_aggressive(r["offset_tested"], best):
                break
            streak += 1
        if streak < threshold:
            return False

        self._advance_core(core_id, passed=False)
        self.log_message.emit(
            f"Repeated instability on core {core_id}: {streak} failures despite earlier passes. "
            "Failure bounds are preserved. Inspect the workload and environment before resuming."
        )
        self.pause()
        return True

    def _most_aggressive_pass(self, core_id: int) -> int | None:
        """Most aggressive offset with a real logged PASS for this core, or None."""
        best_pass: int | None = None
        for r in self._db.get_tuner_test_log(self._session_id, core_id=core_id):
            if r.get("duration_seconds") is None or not r["passed"]:
                continue
            if best_pass is None or self._is_more_aggressive(r["offset_tested"], best_pass):
                best_pass = r["offset_tested"]
        return best_pass

    def _rollback_core_to_evidence(self, cs: CoreState) -> int:
        """Reset a core to its most aggressive PROVEN pass (else baseline).

        Passes are the trustworthy evidence class — a broken apparatus can fake
        a FAIL but not a PASS. The rolled-back value still must re-earn
        confirmation (phase CONFIRMING); poisoned backoff bounds are cleared.
        Returns the rollback offset.
        """
        best_pass = self._most_aggressive_pass(cs.core_id)
        rollback = best_pass if best_pass is not None else cs.baseline_offset
        cs.current_offset = rollback
        cs.best_offset = rollback
        cs.phase = TunerPhase.CONFIRMING
        cs.confirm_attempts = 0
        cs.backoff_mode = False
        cs.consecutive_backoff_fails = 0
        cs.backoff_fail_bound = None
        cs.backoff_pass_bound = None
        self._db.upsert_tuner_core_state(self._session_id, cs)
        self.core_state_changed.emit(cs.core_id, cs.phase, cs.current_offset)
        return rollback

    def _reconcile_confirmed_evidence(self) -> None:
        """State-estimator consistency check on resume: a core CLAIMING
        confirmed/hardened status must be backed by a logged pass at least as
        aggressive as its best_offset (its confirm run logged exactly that).
        best == baseline is exempt (the null result needs no proof; baseline is
        the ambient state). A claim without evidence — corruption, a hand-edited
        row, an upstream bug — is demoted to re-earn confirmation rather than
        being re-applied as truth by validation/finalize.
        """
        for cs in self._core_states.values():
            if cs.phase not in (TunerPhase.CONFIRMED,):
                continue
            if cs.best_offset is None or cs.best_offset == cs.baseline_offset:
                continue
            proof = self._most_aggressive_pass(cs.core_id)
            if proof is not None and not self._is_more_aggressive(cs.best_offset, proof):
                continue  # a pass at-or-beyond best exists — claim is backed
            claimed_phase, claimed_best = cs.phase, cs.best_offset
            rollback = self._rollback_core_to_evidence(cs)
            self.log_message.emit(
                f"EVIDENCE MISMATCH: core {cs.core_id} claimed {claimed_phase} at "
                f"best={claimed_best} with no logged pass to back it — demoted "
                f"to re-confirm at {rollback}."
            )

    def _attribute_crash_after_reboot(self, session, since: str | None = None) -> tuple[list[int], bool]:
        """Attribute a hard crash on the resume-after-reboot path.

        Returns (penalized_core_ids, pending_hunt). An armed persisted probe is
        the controlled crash experiment and owns its reproduction verdict.
        Otherwise kernel-journal MCE lines name cores directly, and one in-test
        core is attributable only when it was also the sole non-stock resident.
        Multi-core sets and validation crashes are never guessed at: they return
        pending_hunt=True so the caller runs the live-vector attribution hunt.
        """
        session_id = self._session_id
        crashed: list[int] = []
        pending_hunt = False
        try:
            saved_hunt = bisect.HuntState.from_json(session.hunt_state)
        except bisect.InvalidHuntState as exc:
            self.log_message.emit(f"Persisted hunt state is invalid: {exc}. Pausing without applying CO.")
            self.pause()
            return [], False
        # An armed probe is itself the crash experiment. Its exact vector and
        # workload were checkpointed before the worker started, so consulting
        # general boot forensics here can only override stronger evidence (or
        # make recovery depend on a journal that the probe does not need).
        if saved_hunt is not None and saved_hunt.armed:
            self._pending_hunt_loaded = list(saved_hunt.loaded)
            self._pending_hunt_vector = dict(saved_hunt.vector)
            self._clear_all_in_test()
            return [], True
        since = since or self._db.latest_session_activity(session_id)
        forensic_events, forensics_ok = self._forensics(since or "", boot_id=session.boot_id)
        if not forensics_ok:
            self.log_message.emit(
                "Kernel-journal forensics unavailable for the session boot. Pausing without reapplying CO."
            )
            self.pause()
            return [], False
        cpu_map = self._cpu_to_core()
        residents = tp.journal_values(self._db, session_id)
        if any(
            ev.cpu >= 0
            and (
                cpu_map.get(ev.cpu) not in self._core_states
                or cpu_map.get(ev.cpu) not in residents
                or residents[cpu_map[ev.cpu]] == 0
            )
            for ev in forensic_events
        ):
            self.log_message.emit(
                "Kernel evidence names a stock or unmapped core. Pausing without blaming a tuned offset."
            )
            self.pause()
            return [], False
        forensic_by_core = self._events_by_core(forensic_events)
        if forensic_by_core:
            crashed = self._penalize_forensic_cores(forensic_by_core)
            self._clear_all_in_test()
            self._db.set_unattributed_crashes(session_id, 0)
        else:
            in_test = [cs for cs in self._core_states.values() if cs.in_test]
            live_cores = sorted(c for c, v in residents.items() if v != 0 and c in self._core_states)
            if len(live_cores) == 1:
                resident_core = self._core_states[live_cores[0]]
                resident_core.current_offset = residents[live_cores[0]]
                crashed = self._penalize_cores([resident_core], "the sole journaled non-stock resident")
            self._clear_all_in_test()
            # A CO write journaled as intent that never recorded surviving is
            # how a crash with no in_test flag at all gets caught. It is proof
            # only under the same condition the in_test branch demands: the
            # suspect was the only core away from stock. A resident offset is
            # not a write in flight, so under the live mask the whole vector
            # turns up un-survived after any freeze, and convicting on that
            # punishes whichever innocent core happened to be mid-slot.
            suspects = self._journal_suspect_cores()
            if (
                not crashed
                and saved_hunt is None
                and not in_test
                and len(suspects) == 1
                and set(live_cores) <= set(suspects)
            ):
                crashed = sorted(self._handle_journal_suspects(set()))
            if not crashed and (in_test or live_cores or saved_hunt is not None):
                self._pending_hunt_loaded = (
                    list(saved_hunt.loaded) if saved_hunt is not None else [cs.core_id for cs in in_test]
                )
                self._pending_hunt_vector = dict(saved_hunt.vector) if saved_hunt is not None else dict(residents)
                self.log_message.emit(
                    f"Crash with {len(live_cores)} core(s) holding a live offset and "
                    f"{len(in_test)} under load. Nothing here names a culprit, so the "
                    f"offsets stay where they are and an attribution hunt decides."
                )
                breadcrumb = self._read_breadcrumb()
                if breadcrumb:
                    # Context, never a verdict: it says what died, not who.
                    self.log_message.emit(f"Last breadcrumb before the freeze: {breadcrumb}")
                pending_hunt = True
        if crashed and session_id is not None:
            self._db.set_hunt_state(session_id, "")
        return crashed, pending_hunt

    def _reengage_quarantined(self, session_id: int) -> None:
        """Re-open a quarantined session on proven ground only.

        The breaker closed this session because the machine kept dying on
        re-engage, so nothing unproven may be applied again. Every offset that
        can reach the hardware drops to the most aggressive value this session
        has actually SURVIVED, stock when it has survived none: the search
        position, the baseline every other core is restored to, and best --
        which validation writes to every core it is not testing. A value a
        test passed at is journaled survived, so this demotes only what was
        never proven; fail bounds and phases are untouched, and the work done
        before the quarantine is continued rather than discarded.

        Only ever reached from an explicit resume of a named session: the
        automatic paths still exclude quarantined sessions.
        """
        pulled: list[int] = []
        for cs in self._core_states.values():
            survived = self._co_survived.get(cs.core_id, 0)
            changed = False
            for attr in ("current_offset", "baseline_offset", "best_offset"):
                value = getattr(cs, attr)
                if value is not None and self._is_more_aggressive(value, survived):
                    setattr(cs, attr, survived)
                    changed = True
            if changed:
                pulled.append(cs.core_id)
                self._db.upsert_tuner_core_state(session_id, cs)
        self._db.set_resume_crash_streak(session_id, 0)
        self._db.update_tuner_session_status(session_id, "running")
        self.log_message.emit(
            "Re-opening a QUARANTINED session. "
            + (
                f"Cores {pulled} were holding offsets this machine never survived; "
                "they restart from their proven value."
                if pulled
                else "Every core was already at a proven offset."
            )
            + " If it quarantines again, the real limits are lower than the search assumed."
        )

    def _clear_all_in_test(self) -> None:
        """Clear and persist every in_test flag — the crash has been handled
        (or ruled out); a stale flag must not re-fire a detector later."""
        for cs in self._core_states.values():
            if cs.in_test:
                cs.in_test = False
                if self._session_id is not None:
                    self._db.upsert_tuner_core_state(self._session_id, cs)

    def _cpu_to_core(self) -> dict[int, int]:
        """Logical CPU id -> physical core id, covering every SMT sibling."""
        mapping: dict[int, int] = {}
        for core_id, info in self._topology.cores.items():
            for lcpu in info.logical_cpus:
                mapping[lcpu] = core_id
        return mapping

    def _events_by_core(self, events: list[MCEEvent]) -> dict[int, list[MCEEvent]]:
        """Group kernel events by physical core; drop unattributable ones.

        Events with no CPU (kernel panic traces) prove a crash happened but
        name no core — they must not be turned into a per-core penalty.
        """
        cpu_map = self._cpu_to_core()
        out: dict[int, list[MCEEvent]] = {}
        for e in events:
            if e.cpu < 0:
                continue
            core = cpu_map.get(e.cpu)
            if core is None or core not in self._core_states:
                continue
            out.setdefault(core, []).append(e)
        return out

    def _penalize_forensic_cores(self, by_core: dict[int, list[MCEEvent]]) -> list[int]:
        """Crash-penalize exactly the cores the kernel journal named.

        The penalty anchors at the CO value the journal says was resident at
        crash time — not whatever offset the persisted search state happens to
        hold — so the fail bound lands on the value that actually died.
        """
        journal = tp.journal_values(self._db, self._session_id) if self._session_id is not None else {}
        crashed: list[int] = []
        for core_id in sorted(by_core):
            cs = self._core_states[core_id]
            resident = journal.get(core_id, cs.current_offset)
            cs.current_offset = resident
            first = by_core[core_id][0]
            self._db.insert_tuner_test_log(
                self._session_id,
                core_id,
                resident,
                cs.phase.value,
                passed=False,
                error_msg=(
                    f"Reboot after hard crash; kernel journal names this core "
                    f"({len(by_core[core_id])} MCE line(s), e.g. "
                    f"'{first.message[:120]}'). Offset {resident} was resident."
                ),
                error_type="crash",
                duration=None,
            )
            self._apply_crash_penalty(cs)
            self._db.upsert_tuner_core_state(self._session_id, cs)
            crashed.append(core_id)
            self.log_message.emit(
                f"Kernel forensics: core {core_id} named by MCE at offset "
                f"{resident} — crash penalty applied, now {cs.current_offset}."
            )
        return crashed

    def _penalize_cores(self, targets: list[CoreState], reason: str) -> list[int]:
        """Apply the crash penalty to attributed cores (synthetic log row each)."""
        crashed: list[int] = []
        for cs in targets:
            crashed_offset = cs.current_offset
            self._db.insert_tuner_test_log(
                self._session_id,
                cs.core_id,
                crashed_offset,
                cs.phase.value,
                passed=False,
                error_msg=(f"System reboot detected ({reason}). Offset {crashed_offset} caused hard crash."),
                error_type="crash",
                duration=None,
            )
            self._apply_crash_penalty(cs)
            self._db.upsert_tuner_core_state(self._session_id, cs)
            crashed.append(cs.core_id)
            logging.warning(
                "Core %d: crash detected at offset %d — applied penalty, new offset %d, crash_count=%d",
                cs.core_id,
                crashed_offset,
                cs.current_offset,
                cs.crash_count,
            )
        return crashed

    def _journal_suspect_cores(self) -> list[int]:
        """Cores whose CO write was still un-survived when the machine died."""
        return sorted({c for c, _ in tp.journal_suspects(self._db, self._session_id) if c in self._core_states})

    def _handle_journal_suspects(self, already: set[int]) -> list[int]:
        """Penalize cores whose CO value was resident, un-survived, when the box died.

        The CO write-ahead journal records every value made resident in the SMU
        before the hardware write, so a hard crash with no in_test flag (idle,
        baseline restore, post-test revert, validation) is still caught here.
        ``already`` holds cores handled by in_test detection — skipped to avoid a
        double penalty. Returns the list of core ids penalized.
        """
        if self._session_id is None:
            return []
        handled: list[int] = []
        for core_id, value in tp.journal_suspects(self._db, self._session_id):
            if core_id in already:
                continue
            cs = self._core_states.get(core_id)
            if cs is None:
                continue
            # Anchor the penalty at the value that was actually resident at crash
            # time (the journal), which may differ from the persisted offset.
            cs.current_offset = value
            self._db.insert_tuner_test_log(
                self._session_id,
                core_id,
                value,
                cs.phase.value,
                passed=False,
                error_msg=(
                    f"Reboot detected. Offset {value} was resident (CO journal) "
                    f"and not proven survivable — treated as a hard crash."
                ),
                error_type="crash",
                duration=None,
            )
            self._apply_crash_penalty(cs)
            cs.in_test = False
            self._db.upsert_tuner_core_state(self._session_id, cs)
            handled.append(core_id)
            logging.warning(
                "Core %d: CO-journal crash suspect at offset %d — penalty applied, new offset %d",
                core_id,
                value,
                cs.current_offset,
            )
        return handled

    # ------------------------------------------------------------------
    # Hardware-error evidence (cross-core MCE) and the isolated crash hunt
    # ------------------------------------------------------------------

    def _foreign_mce_by_core(self, tested_core: int, mce_json: str) -> dict[int, dict]:
        """Parse the worker's observed-MCE payload into evidence about cores
        OTHER than the tested one. Fail closed: malformed JSON is no evidence.

        Returns {core_id: {"corrected": bool, "messages": [...]}} where
        corrected is False when ANY event for that core was uncorrected.
        """
        if not mce_json:
            return {}
        try:
            raw = json.loads(mce_json)
        except (json.JSONDecodeError, TypeError):
            return {}
        if not isinstance(raw, list):
            return {}
        cpu_map = self._cpu_to_core()
        out: dict[int, dict] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            cpu = item.get("cpu")
            if not isinstance(cpu, int) or cpu < 0:
                continue
            core = cpu_map.get(cpu)
            if core is None or core == tested_core or core not in self._core_states:
                continue
            entry = out.setdefault(core, {"corrected": True, "messages": []})
            if not item.get("corrected", False):
                entry["corrected"] = False
            msg = item.get("message")
            if isinstance(msg, str):
                entry["messages"].append(msg)
        return out

    def _apply_foreign_evidence(self, foreign: dict[int, dict]) -> None:
        """Act on kernel events that named other cores during a test."""
        for core_id in sorted(foreign):
            cs = self._core_states.get(core_id)
            if cs is None:
                continue
            resident = self._co_applied.get(core_id)
            if resident is None:
                resident = cs.current_offset
            info = foreign[core_id]
            self._apply_mce_evidence(core_id, resident, info["corrected"], info["messages"])

    def _apply_mce_evidence(self, core_id: int, resident: int, corrected: bool, messages: list[str]) -> None:
        """React to a kernel hardware-error report naming this core while
        ``resident`` was its live CO value.

        Corrected error: the value is marginal — one-step penalty and re-earn
        confirmation (proportionate: the machine did not crash). Uncorrected:
        near-crash — full crash penalty. At stock (resident == 0) there is
        nothing to back off: the instability is not Curve Optimizer induced,
        so surface it loudly instead of walking a zero offset.
        """
        cs = self._core_states.get(core_id)
        if cs is None or self._session_id is None:
            return
        detail = messages[0] if messages else "kernel MCE"
        self._db.insert_tuner_test_log(
            self._session_id,
            core_id,
            resident,
            "mce_evidence",
            passed=False,
            error_msg=(f"Kernel reported a hardware error on this core at resident offset {resident}: {detail}"),
            error_type="mce",
            duration=None,
        )
        if resident == 0:
            self.log_message.emit(
                f"Core {core_id}: hardware error at STOCK settings (CO=0) — not a "
                f"Curve Optimizer problem. Check cooling, memory, or BIOS."
            )
            self.pause()
            return
        cs.current_offset = resident
        if corrected:
            self._apply_crash_penalty(cs, steps=1, count_crash=False)
            self.log_message.emit(
                f"Core {core_id}: corrected hardware error at offset {resident} — "
                f"backed off one step to {cs.current_offset}; the core must "
                f"re-earn confirmation."
            )
        else:
            self._apply_crash_penalty(cs)
            self.log_message.emit(
                f"Core {core_id}: UNCORRECTED hardware error at offset {resident} — "
                f"crash-grade penalty applied, now {cs.current_offset}."
            )
        self._db.upsert_tuner_core_state(self._session_id, cs)
        self.core_state_changed.emit(cs.core_id, cs.phase, cs.current_offset)

    def _hunt_candidates(self, vector: dict[int, int] | None = None) -> list[int]:
        """Cores whose exact crash-time journal value was not stock."""
        residents = vector
        if residents is None:
            residents = tp.journal_values(self._db, self._session_id) if self._session_id is not None else {}
        return sorted(core_id for core_id, value in residents.items() if core_id in self._core_states and value != 0)

    def _save_hunt(self) -> None:
        if self._session_id is not None and self._hunt is not None:
            self._db.set_hunt_state(self._session_id, self._hunt.to_json())

    def _start_hunt(self, observed_mttf: float = 0.0, loaded: list[int] | None = None) -> None:
        """Attribute a failure that named no core.

        Bisection over the live-offset mask, preceded by a control probe at
        full stock. The old isolated-per-core hunt could not work: it parked
        every other core at stock, deleting the whole-vector condition that
        caused the failure in the first place.
        """
        vector = dict(self._pending_hunt_vector)
        self._pending_hunt_vector = {}
        if not vector and self._session_id is not None:
            vector = tp.journal_values(self._db, self._session_id)
        candidates = self._hunt_candidates(vector)
        under_load = sorted(loaded) if loaded else sorted(self._cores_under_stress or [self._last_tested_core])
        under_load = [c for c in under_load if c in self._core_states] or sorted(self._core_states)[:1]
        self._clear_all_in_test()
        if not candidates:
            self.log_message.emit(
                "Nothing was undervolted when the machine died, so the offsets did not cause it. "
                "Treating this as a platform fault."
            )
            self._platform_fault("no core held a live offset at the time of the failure")
            return
        workload = self._hunt_workload if self._validation_stage == 4 else None
        workload = workload or self._workload_snapshot(self._core_states[under_load[0]])
        self._hunt = bisect.begin(candidates, under_load, observed_failure_time=observed_mttf)
        self._hunt.vector = vector
        self._hunt.workload = workload
        self._hunt_workload = workload
        self._hunting = True
        self._validation_stage = 0
        self._validation_thermal_aborts = 0
        self._transition_status("hunting")
        self._save_hunt()
        self.log_message.emit(
            f"Attribution hunt over {candidates}: control probe at stock first, then bisection of the live set."
        )
        self._run_next_hunt_slot()

    def _restore_hunt_stock(self) -> bool:
        failed = self._restore_stock_verified()
        if failed:
            self._quarantine_session(
                0,
                reason="unverified stock restoration after attribution hunt",
                stock_failures=failed,
            )
            return False
        return True

    def _apply_hunt_mask(self, live: list[int]) -> bool:
        """Replay the exact crash vector on ``live`` and put everyone else at stock."""
        live_set = set(live)
        vector = self._hunt.vector if self._hunt is not None else {}
        for core_id in sorted(self._topology.cores):
            target = vector.get(core_id, 0) if core_id in live_set else 0
            if self._co_applied.get(core_id) == target:
                continue
            try:
                ok = self._write_co_verified(core_id, target)
            except Exception as exc:
                self.log_message.emit(f"Hunt: failed to set core {core_id} to {target}: {exc}. Restoring stock.")
                self._restore_hunt_stock()
                if self._status != "profile_quarantined":
                    self.pause()
                return False
            if not ok:
                self.log_message.emit(f"Hunt: CO write rejected for core {core_id} at {target}. Restoring stock.")
                self._restore_hunt_stock()
                if self._status != "profile_quarantined":
                    self.pause()
                return False
            self._co_applied[core_id] = target
        return True

    def _start_rapid_transition_worker(self, cores: list[int], duration: int, workload: dict) -> None:
        from corecycler.engine.backends.base import FFTPreset, StressMode

        stress_config = StressConfig(
            mode=StressMode[workload["stress_mode"].upper()],
            fft_preset=FFTPreset[workload["fft_preset"].upper()],
            threads=workload.get("threads") or 2,
        )
        scheduler_config = SchedulerConfig(
            seconds_per_core=duration,
            cores_to_test=cores,
            stop_on_error=True,
            cycle_count=1,
            max_temperature=self._config.max_temperature_c,
            over_temp_grace_seconds=self._config.over_temp_grace_seconds,
            over_temp_hard_margin=self._config.over_temp_hard_margin_c,
            require_thermal_sensor=not self._config.allow_missing_thermal_sensor,
        )
        try:
            scheduler = CoreScheduler(
                topology=self._topology,
                backend=self._get_backend_for_name(workload["backend"]),
                stress_config=stress_config,
                scheduler_config=scheduler_config,
                work_dir=self._work_dir,
            )
        except Exception as exc:
            self._fail_test_async(cores[0], str(exc))
            return
        self._worker_profile = "transitions"
        core_info = self._topology.cores.get(cores[0])
        logical_cpu = core_info.logical_cpus[0] if core_info and core_info.logical_cpus else cores[0]
        worker = _RapidTransitionWorker(cores[0], logical_cpu, scheduler, cores, float(duration), parent=self)
        self._launch_worker(worker, workload, cores, freeze_context=f"cores {cores} (rapid transitions)")

    def _start_soak_worker(self, cores: list[int], duration: int, workload: dict) -> None:
        self._soaking = True
        thermal = ThermalWatch(
            max_temperature=self._config.max_temperature_c,
            grace_seconds=self._config.over_temp_grace_seconds,
            hard_margin=self._config.over_temp_hard_margin_c,
            require_sensor=not self._config.allow_missing_thermal_sensor,
        )
        worker = _SoakWorker(cores[0] if cores else 0, duration, thermal=thermal, parent=self)
        self._launch_worker(worker, workload, cores, freeze_context=f"cores {cores} (real-world soak)")

    def _run_next_hunt_slot(self) -> None:
        if self._abort_requested or self._paused or self._hunt is None:
            return
        live = bisect.next_live_set(self._hunt)
        if live is None:
            self._resolve_hunt()
            return
        self._hunt.armed = False
        self._save_hunt()
        if not self._apply_hunt_mask(live):
            return

        replay_duration = (self._hunt.workload or {}).get("duration_seconds")
        probe_base = (
            replay_duration if type(replay_duration) is int and replay_duration > 0 else self._config.probe_base_seconds
        )
        duration = bisect.probe_seconds(
            self._hunt,
            base=probe_base,
            mttf_multiplier=self._config.probe_mttf_multiplier,
            level_multiplier=self._config.probe_level_multiplier,
            final_multiplier=self._config.probe_final_multiplier,
        )
        # The probe replays the load that was running when the machine died
        # and varies only the offset mask. Every core in the probe is marked
        # in_test so a mid-probe reboot is attributed to the probe rather than
        # to whichever core happened to report.
        self._hunt_workload = self._hunt.workload
        loaded = [c for c in self._hunt.loaded if c in self._core_states] or sorted(self._core_states)[:1]
        self._mark_cores_under_stress(sorted(set(loaded) | set(live)))
        reporter = loaded[0]
        self._last_tested_core = reporter
        self._emit_progress()
        if self._hunt.stage is bisect.Stage.CONTROL:
            self.log_message.emit(
                f"Hunt control probe: every core at stock for {duration}s. "
                "If the machine dies here the offsets are not the cause."
            )
        else:
            self.log_message.emit(
                f"Hunt probe ({self._hunt.stage}, level {self._hunt.level}): live {live}, "
                f"every other core at stock, for {duration}s"
            )
        workload = self._hunt.workload or {}
        match workload.get("kind"):
            case "rapid_transition":
                self._start_rapid_transition_worker(loaded, duration, workload)
            case "soak":
                self._start_soak_worker(loaded, duration, workload)
            case "parallel":
                self._start_multi_core_worker(loaded, duration, workload=workload)
            case "solo":
                self._start_worker(
                    reporter,
                    duration,
                    spectrum=workload.get("profile") == "spectrum",
                    duty_cycle=_duty_cycle_for(workload),
                )
            case _ if len(loaded) > 1:
                self._start_multi_core_worker(loaded, duration, workload=workload)
            case _:
                self._start_worker(
                    reporter,
                    duration,
                    spectrum=workload.get("profile") == "spectrum",
                    duty_cycle=_duty_cycle_for(workload),
                )

    def _on_hunt_slot_finished(self, core_id: int, passed: bool, error_type: str, foreign: dict[int, dict]) -> None:
        self._soaking = False
        if self._hunt is None:
            self._hunting = False
            QTimer.singleShot(0, self._run_next)
            return
        reproduced = not passed
        live = set(self._hunt.in_flight)
        reported_stock_failure = not passed and (core_id not in live or self._hunt.vector.get(core_id, 0) == 0)
        foreign_stock_failure = any(core not in live or self._hunt.vector.get(core, 0) == 0 for core in foreign)
        inconsistent = self._hunt.stage is not bisect.Stage.CONTROL and (
            reported_stock_failure or foreign_stock_failure
        )
        if inconsistent:
            self.log_message.emit(
                "Attribution hunt saw hardware evidence on a core at stock; requeueing the probe and pausing."
            )
            self._requeue_hunt_probe()
            self.pause()
            return
        if foreign:
            reproduced = True
        self._record_hunt_probe(reproduced=reproduced)
        QTimer.singleShot(0, self._run_next_hunt_slot)

    def _requeue_hunt_probe(self) -> None:
        """Put the in-flight probe back at the head of the queue.

        A thermal stop or an apparatus fault is not an answer to the question
        the probe asked, so it must not be folded in as one.
        """
        if self._hunt is not None:
            self._hunt.armed = False
            if self._hunt.stage is bisect.Stage.CONFIRM and self._hunt.suspect is not None:
                suspect = [self._hunt.suspect]
                self._hunt.suspect = None
                self._hunt.stage = bisect.Stage.PROBE
                self._hunt.pending.insert(0, suspect)
            elif self._hunt.stage is not bisect.Stage.CONTROL and self._hunt.in_flight:
                self._hunt.queue.insert(0, list(self._hunt.in_flight))
            self._hunt.in_flight = []
            self._save_hunt()
        self._clear_all_in_test()

    def _exonerate(self, cores: Iterable[int]) -> None:
        """Halve the suspicion of cores a probe cleared.

        A core that sat at stock while the failure still reproduced did not
        cause it. Halving rather than zeroing keeps a long accusation history
        from evaporating on one probe, while making sure an old accusation
        cannot haunt a core that has since been cleared repeatedly.
        """
        for core_id in cores:
            cs = self._core_states.get(core_id)
            if cs is None or cs.suspicion == 0.0:
                continue
            cs.suspicion /= 2.0
            if self._session_id is not None:
                self._db.upsert_tuner_core_state(self._session_id, cs)

    def _record_hunt_probe(self, *, reproduced: bool) -> None:
        whole_set = (
            self._hunt is not None
            and self._hunt.stage is bisect.Stage.PROBE
            and bool(self._hunt.parent)
            and self._hunt.in_flight == self._hunt.parent
        )
        if reproduced and self._hunt is not None and self._hunt.stage is bisect.Stage.PROBE:
            # The failure happened without these cores' offsets live, which is
            # the one piece of direct exculpatory evidence a hunt produces.
            live = set(self._hunt.in_flight)
            self._exonerate(c for c in self._hunt.parent if c not in live)
        if whole_set and reproduced:
            self.log_message.emit(
                f"Hunt: {self._hunt.parent} fail together, yet every half of that set ran clean. "
                "No single core carries it, so each of them backs off one step."
            )
        bisect.record(
            self._hunt,
            reproduced=reproduced,
            control_confirmations=self._config.control_run_confirmations,
            max_no_reproduce=self._config.max_unattributed_crash_hunts,
        )
        self._save_hunt()

    def _resolve_hunt(self) -> None:
        """Act on whatever the hunt proved, and never hand back control for it."""
        state = self._hunt
        self._hunt = None
        self._hunting = False
        if self._session_id is not None:
            self._db.set_hunt_state(self._session_id, "")

        if state.stage is bisect.Stage.PLATFORM:
            self._platform_fault("the machine failed with every core at stock")
            return

        if state.stage is bisect.Stage.CULPRIT and state.found:
            for culprit in state.found:
                self._blame_core(
                    culprit,
                    "isolated by bisection of the live offset vector",
                    resident=state.vector.get(culprit),
                )
            self._after_hunt_resume(clear_incident=True)
            return

        # Nothing reproduced. Credit suspicion and let the statistical route
        # decide, but only on a clear winner: acting on a near-tie is a guess
        # dressed up as a verdict.
        self._exonerate(state.exonerated)
        self._credit_suspicion(state.vector)
        picked = self._suspicion_verdict()
        if picked is not None:
            self._blame_core(
                picked,
                "highest accumulated suspicion after the failure would not reproduce",
                resident=state.vector.get(picked),
            )
        else:
            self.log_message.emit(
                "Hunt could not reproduce the failure and no core stands out yet. "
                "Continuing the search; suspicion carries forward."
            )
        self._after_hunt_resume(clear_incident=picked is not None)

    def _after_hunt_resume(self, *, clear_incident: bool = False) -> None:
        if not self._restore_hunt_stock():
            return
        if self._session_id is not None and clear_incident:
            self._db.set_unattributed_crashes(self._session_id, 0)
            self._db.set_resume_crash_streak(self._session_id, 0)
        self._transition_status("running")
        QTimer.singleShot(0, self._run_next)

    def _blame_core(self, core_id: int, reason: str, *, resident: int | None = None) -> None:
        """Demote one core from the exact resident value and make it re-earn everything."""
        cs = self._core_states[core_id]
        cs.current_offset = resident if resident is not None else self._mask_offset(cs, Mask.LIVE)
        self._apply_crash_penalty(cs, steps=1, count_crash=False)
        cs.suspicion = 0.0
        if self._session_id is not None:
            self._db.upsert_tuner_core_state(self._session_id, cs)
        self._clear_bank(core_id)
        self.log_message.emit(f"Core {core_id}: backed off to {cs.current_offset} — {reason}.")
        self.core_state_changed.emit(cs.core_id, cs.phase, cs.current_offset)

    def _credit_suspicion(self, vector: dict[int, int] | None = None) -> None:
        """Accumulate blame from the crash-time vector, including inherited baselines."""
        live_vector = vector or self.live_vector()
        median = self._median_live_depth(live_vector)
        for core_id, cs in self._core_states.items():
            live = live_vector.get(core_id, 0)
            if live == 0:
                continue
            depth = max(1.0, abs(live) - median + 1.0)
            role = 2.0 if core_id == self._last_tested_core else 1.0
            cs.suspicion += depth * role
            if self._session_id is not None:
                self._db.upsert_tuner_core_state(self._session_id, cs)

    @staticmethod
    def _median_live_depth(vector: dict[int, int]) -> float:
        depths = sorted(abs(value) for value in vector.values() if value != 0)
        if not depths:
            return 0.0
        mid = len(depths) // 2
        return float(depths[mid]) if len(depths) % 2 else (depths[mid - 1] + depths[mid]) / 2.0

    def _suspicion_verdict(self) -> int | None:
        """The core to demote, or None when the field is too close to call."""
        failures = self._db.get_unattributed_crashes(self._session_id)
        if failures < self._config.suspicion_min_failures:
            return None
        ranked = sorted(self._core_states.values(), key=lambda cs: cs.suspicion, reverse=True)
        if len(ranked) < 2 or ranked[0].suspicion <= 0:
            return None
        runner_up = ranked[1].suspicion
        if runner_up > 0 and ranked[0].suspicion < runner_up * self._config.suspicion_separation:
            return None
        return ranked[0].core_id

    def _clear_bank(self, core_id: int) -> None:
        """Banked confidence dies with the offset that earned it."""
        context = self.context_id()
        if context is not None:
            self._db.clear_regime_banks(context, core_id)

    def context_id(self) -> int | None:
        """Database identity of the operating point confidence belongs to."""
        session = self._db.get_tuner_session(self._session_id) if self._session_id is not None else None
        return session.context_id if session is not None else None

    def _platform_fault(self, evidence: str) -> None:
        """The offsets are not the problem. Say so and stop cleanly.

        This is not a stability question handed back to the user: it is the
        answer to the question that was asked, and no further searching can
        improve it.
        """
        if not self._restore_hunt_stock():
            return
        self._hunting = False
        self._hunt = None
        if self._session_id is not None:
            self._db.set_hunt_state(self._session_id, "")
            self._db.update_tuner_session_status(self._session_id, "platform_fault")
        self._set_status("platform_fault")
        self._emit_progress()
        self.log_message.emit(
            f"PLATFORM FAULT: {evidence}. Curve Optimizer is not the cause, so the search stops here. "
            "Every core is at stock. Check the EDC/TDC/PPT current limits first (a too-low EDC reproduces "
            "exactly this symptom at idle), then memory and PSU. Consumer non-ECC DDR5 corrects silently, "
            "so the absence of a memory machine check does not clear RAM."
        )
        self.platform_fault.emit(evidence)

    def _quarantine_session(
        self,
        streak: int,
        *,
        reason: str | None = None,
        stock_failures: set[int] | None = None,
    ) -> None:
        failed = self._restore_stock_verified() if stock_failures is None else stock_failures
        for cs in self._core_states.values():
            cs.in_test = False
            if self._session_id is not None:
                self._db.upsert_tuner_core_state(self._session_id, cs)
        self._transition_status("profile_quarantined")
        self._emit_progress()
        restoration = (
            f"Stock restoration failed for cores {sorted(failed)}; offsets may still be active. "
            "Reboot before further tuning."
            if failed
            else "All physical cores verified at stock (CO=0)."
        )
        cause = reason or f"{streak} consecutive crash-resumes"
        self.log_message.emit(
            f"PROFILE QUARANTINED after {cause}. {restoration} "
            "Review cooling, BIOS PBO and the failed workload before resuming."
        )

    def _check_time_budget(self, cs: CoreState) -> bool:
        """Pause an inconclusive search instead of manufacturing confirmation."""
        if cs.cumulative_test_time <= self._config.max_core_time_seconds or cs.phase in (TunerPhase.CONFIRMED,):
            return False
        self.log_message.emit(f"Core {cs.core_id}: time budget exceeded without confirmation; pausing for review")
        self.pause()
        return True

    def _accumulate_test_time(self, cs: CoreState, duration: float) -> None:
        """Add test duration to core's cumulative time (search phases only)."""
        if cs.phase is TunerPhase.CONFIRMED:
            return
        cs.cumulative_test_time += duration

    def _is_core_available(self, cs: CoreState) -> bool:
        """Check if core is available for testing (not done, not in cooldown)."""
        if cs.crash_cooldown > 0:
            return False
        return cs.phase not in (TunerPhase.CONFIRMED,)

    def _decrement_cooldowns(self, picked_core: int) -> None:
        """Decrement crash cooldown for all cores except the one being tested."""
        for cs in self._core_states.values():
            if cs.core_id != picked_core and cs.crash_cooldown > 0:
                cs.crash_cooldown -= 1

    def _pick_next_core(self) -> int | None:
        """Select next core to test based on test_order config.

        Returns None if all cores are done (CONFIRMED) or all remaining
        active cores are in crash cooldown. Callers must distinguish these cases
        by checking whether any cooldown cores exist.
        """
        match self._config.test_order:
            case "round_robin":
                picked = self._pick_round_robin()
            case "weakest_first":
                picked = self._pick_weakest_first()
            case "ccd_alternating":
                picked = self._pick_ccd_alternating()
            case "ccd_round_robin":
                picked = self._pick_ccd_round_robin()
            case _:
                picked = self._pick_sequential()
        # Nothing left to search is not the same as nothing left to learn: a
        # core that has banked enough clean time has earned a probe deeper.
        return picked if picked is not None else self._anneal_candidate()

    def _pick_sequential(self) -> int | None:
        """Finish each core completely before moving to the next (pure selector).

        The lowest-id core that is neither done (CONFIRMED) nor in
        cooldown is driven all the way through — including its SETTLED -> CONFIRMING
        step. SETTLED is NOT deferred behind every other core's search (that would
        settle all cores first and confirm them all at the end, which is not
        "finish each core completely").
        """
        for core_id in sorted(self._core_states.keys()):
            cs = self._core_states[core_id]
            if cs.phase not in (TunerPhase.CONFIRMED,) and self._is_core_available(cs):
                return core_id
        return None

    def _pick_round_robin(self) -> int | None:
        """Cycle through all cores, one test each per round (pure selector).

        Rotation is by POSITION, not membership: when the cursor core itself
        just went terminal (or into cooldown), the cycle continues at the next
        higher id instead of snapping back to core 0 — otherwise every
        confirmation would restart the round and starve the high-id cores'
        cool-down fairness.
        """
        active = sorted(cid for cid, cs in self._core_states.items() if self._is_core_available(cs))
        if not active:
            return None
        if self._last_tested_core is not None:
            after = [c for c in active if c > self._last_tested_core]
            return after[0] if after else active[0]
        return active[0]

    def _pick_weakest_first(self) -> int | None:
        """Prioritize cores closest to settling (pure selector).

        Scoring: lower score = higher priority. Crash history adds penalty
        of crash_count * 2 to deprioritize repeatedly-crashing cores.
        """
        candidates = []
        for core_id, cs in self._core_states.items():
            if not self._is_core_available(cs):
                continue
            base_phase_score = {
                TunerPhase.FINE_SEARCH: 0,
                TunerPhase.FAILED_CONFIRM: 0,
                TunerPhase.BACKOFF_PRECONFIRM: 0,
                TunerPhase.BACKOFF_CONFIRMING: 1,
                TunerPhase.CONFIRMING: 1,
                TunerPhase.COARSE_SEARCH: 2,
                TunerPhase.SETTLED: 3,
                TunerPhase.NOT_STARTED: 4,
            }.get(cs.phase, 5)
            score = base_phase_score + (cs.crash_count * 2)
            candidates.append((score, core_id))
        if not candidates:
            return None
        candidates.sort()
        return candidates[0][1]

    def _pick_ccd_alternating(self) -> int | None:
        """Alternate between CCDs for cross-CCD thermal balance.

        Primary rule is genuine alternation: prefer a CCD different from the one
        just tested so the previously-loaded CCD cools while the other works.
        Fewest-confirmed (then lowest index) is the tie-break among the
        alternation candidates, keeping the CCDs balanced over the run.
        """
        ccd_cores: dict[int, list[int]] = {}
        for core_id, cs in self._core_states.items():
            if not self._is_core_available(cs):
                continue
            core_info = self._topology.cores.get(core_id)
            ccd = core_info.ccd if core_info and core_info.ccd is not None else 0
            ccd_cores.setdefault(ccd, []).append(core_id)

        if not ccd_cores:
            return None

        for ccd in ccd_cores:
            ccd_cores[ccd].sort()

        ccd_confirmed: dict[int, int] = {}
        for core_id, cs in self._core_states.items():
            core_info = self._topology.cores.get(core_id)
            ccd = core_info.ccd if core_info and core_info.ccd is not None else 0
            if cs.phase in (TunerPhase.CONFIRMED,):
                ccd_confirmed[ccd] = ccd_confirmed.get(ccd, 0) + 1

        candidate_ccds = sorted(ccd_cores.keys())
        # Alternate away from the last-tested CCD when another CCD still has work.
        if self._last_tested_core is not None:
            last_info = self._topology.cores.get(self._last_tested_core)
            last_ccd = last_info.ccd if last_info and last_info.ccd is not None else 0
            others = [c for c in candidate_ccds if c != last_ccd]
            if others:
                candidate_ccds = others

        target_ccd = min(candidate_ccds, key=lambda c: (ccd_confirmed.get(c, 0), c))
        return ccd_cores[target_ccd][0]

    def _pick_ccd_round_robin(self) -> int | None:
        """Round-robin with CCD interleaving — one test per core, alternating CCDs.

        Order: CCD0[0]→CCD1[0]→CCD0[1]→CCD1[1]→CCD0[2]→CCD1[2]...
        Each core gets cool-down time between tests.
        """
        ccd_cores: dict[int, list[int]] = {}
        for core_id, cs in self._core_states.items():
            if not self._is_core_available(cs):
                continue
            core_info = self._topology.cores.get(core_id)
            ccd = core_info.ccd if core_info and core_info.ccd is not None else 0
            ccd_cores.setdefault(ccd, []).append(core_id)

        if not ccd_cores:
            return None

        for ccd in ccd_cores:
            ccd_cores[ccd].sort()

        sorted_ccds = sorted(ccd_cores.keys())

        if len(sorted_ccds) < 2:
            return self._pick_round_robin()

        # Pick CCD: alternate from last tested core's CCD
        if self._last_tested_core is not None:
            last_info = self._topology.cores.get(self._last_tested_core)
            last_ccd = last_info.ccd if last_info and last_info.ccd is not None else 0
            other_ccds = [c for c in sorted_ccds if c != last_ccd and c in ccd_cores]
            target_ccd = other_ccds[0] if other_ccds else sorted_ccds[0]
        else:
            target_ccd = sorted_ccds[0]

        cores = ccd_cores[target_ccd]

        # Within this CCD, rotate from the last tested POSITION (the cursor
        # core may itself have gone terminal — same rationale as round_robin).
        last_in_ccd = self._ccd_last_tested.get(target_ccd)
        if last_in_ccd is not None:
            after = [c for c in cores if c > last_in_ccd]
            return after[0] if after else cores[0]
        return cores[0]

    def _reconstruct_scheduling_position(self) -> None:
        """Re-derive the round-robin / CCD cycling position from the test log.

        ``_last_tested_core`` and ``_ccd_last_tested`` are in-memory cursors, not
        persisted state — the test log is their source of truth. Rebuilding them on
        resume keeps the cycling order (and its cross-CCD cool-down) continuous
        across a reboot instead of silently restarting from core 0. Synthetic
        crash-recovery rows (duration is NULL) are skipped — they are not real
        tests and must not move the cursor.
        """
        if self._session_id is None:
            return
        real = [e for e in self._db.get_tuner_test_log(self._session_id) if e.get("duration_seconds") is not None]
        if not real:
            return
        self._last_tested_core = real[-1]["core_id"]
        for entry in real:  # ascending by id: the last write per CCD wins
            core_info = self._topology.cores.get(entry["core_id"])
            if core_info and core_info.ccd is not None:
                self._ccd_last_tested[core_info.ccd] = entry["core_id"]

    # ------------------------------------------------------------------
    # Test execution
    # ------------------------------------------------------------------

    def _run_next(self) -> None:
        """Pick next core, apply CO, run test on a worker thread."""
        if self._abort_requested or self._paused:
            return

        # Check abort-on-consecutive-failures
        if (
            self._config.abort_on_consecutive_failures > 0
            and self._consecutive_start_failures >= self._config.abort_on_consecutive_failures
        ):
            self.log_message.emit(
                f"Aborting: {self._consecutive_start_failures} consecutive cores "
                f"failed at start offset {self._config.start_offset}"
            )
            self.abort()
            return

        core_id = self._pick_next_core()
        while core_id is None:
            # Distinguish "all done" from "all active cores in cooldown"
            in_cooldown = any(
                cs.crash_cooldown > 0 and cs.phase not in (TunerPhase.CONFIRMED,) for cs in self._core_states.values()
            )
            if not in_cooldown:
                self._complete_session()
                return
            # Drain all cooldowns by 1 and retry the picker
            for cs in self._core_states.values():
                if cs.crash_cooldown > 0:
                    cs.crash_cooldown -= 1
            core_id = self._pick_next_core()

        self._decrement_cooldowns(core_id)
        cs = self._core_states[core_id]
        if cs.phase == TunerPhase.NOT_STARTED:
            self._advance_core(core_id, passed=False)  # → coarse_search
            cs = self._core_states[core_id]
        if cs.phase in (TunerPhase.SETTLED, TunerPhase.FAILED_CONFIRM):
            self._advance_core(core_id, passed=False)  # → confirming
            cs = self._core_states[core_id]
        self._last_tested_core = core_id
        cs.in_test = True
        self._db.upsert_tuner_core_state(self._session_id, cs)
        # Track per-CCD position for ccd_round_robin
        core_info = self._topology.cores.get(core_id)
        if core_info and core_info.ccd is not None:
            self._ccd_last_tested[core_info.ccd] = core_id
        self._emit_progress()
        regime = self._active_regime(cs) or "primary"
        self._freeze_context(f"core {core_id} at {cs.current_offset} ({cs.phase}, {regime})")
        self.log_message.emit(f"Testing core {core_id} at offset {cs.current_offset} (phase: {cs.phase})")

        # CO offset application — two modes:
        # 1. During validation: apply ALL confirmed offsets (testing interactions)
        # 2. During search: isolate tested core (only it has non-baseline offset)
        if self._smu is not None:
            if self._status == "validating":
                # Validation mode: apply all confirmed offsets to test interactions
                if not self._apply_validation_offsets(core_id, cs.current_offset):
                    return
            else:
                # Search runs the LIVE mask: every other core sits at its own
                # best-known offset, the only condition the machine ever really
                # runs in. Parking them at stock hides every fault an idle
                # core's own margin causes, which is exactly the blind spot
                # that let the search converge on an answer the whole-CPU test
                # then rejected.
                if not self._apply_co_mask(core_id, cs.current_offset, Mask.LIVE):
                    return

        # Determine test duration based on phase
        if cs.phase in (
            TunerPhase.CONFIRMING,
            TunerPhase.BACKOFF_CONFIRMING,
            TunerPhase.ANNEALING,
        ):
            duration = self._config.confirm_duration_seconds
        elif cs.phase == TunerPhase.BACKOFF_PRECONFIRM:
            duration = int(self._config.search_duration_seconds * self._config.backoff_preconfirm_multiplier)
        elif self._status == "validating":
            duration = self._config.validate_duration_seconds
        else:
            duration = self._config.search_duration_seconds

        # A battery entry naming the spectrum profile runs light-load coverage
        # (bursts, transitions, idle watch) instead of sustained stress; the
        # transient profile duty-cycles the payload below the scheduler's own
        # granularity, which is the only way to reach an idle->boost swing.
        entry = self._battery_entry(cs)
        self._start_worker(
            core_id,
            self._battery_duration(cs, duration),
            spectrum=entry.get("profile") == "spectrum",
            duty_cycle=_duty_cycle_for(entry),
        )

    def _fail_test_async(self, core_id: int, message: str) -> None:
        """Deliver a start-time failure on a fresh event-loop stack, like the
        worker's queued finished signal — never re-enter _on_test_finished
        synchronously (which would recurse through _run_next on a core that
        always fails to start).
        """
        QTimer.singleShot(0, lambda: self._on_test_finished(core_id, False, message, "startup", 0.0, 0.0, "", ""))

    def _launch_worker(
        self,
        worker: QThread,
        workload: dict,
        cores: list[int],
        *,
        freeze_context: str | None = None,
        emit_started: int | None = None,
        slot: dict | None = None,
    ) -> bool:
        self._worker = worker
        worker.finished.connect(self._on_test_finished)
        self._checkpoint_worker(workload, cores)
        if self._paused or self._abort_requested:
            self._clear_all_in_test()
            worker.deleteLater()
            self._worker = None
            return False
        if freeze_context is not None:
            self._freeze_context(freeze_context)
        self._start_freeze_monitor()
        worker.start()
        self.slot_started.emit(json.dumps(self._describe_slot(workload, cores, slot or {})))
        if emit_started is not None:
            self.worker_started.emit(emit_started)
        return True

    def _describe_slot(self, workload: dict, cores: list[int], extra: dict) -> dict:
        described = {**workload, **extra, "cores": sorted(cores)}
        if self._hunting and self._hunt is not None:
            described["hunt"] = {
                "stage": str(self._hunt.stage),
                "level": self._hunt.level,
                "live": sorted(self._hunt.in_flight),
            }
        elif self._validation_stage > 0:
            described["validation_stage"] = self._validation_stage
        return described

    def _start_worker(
        self,
        core_id: int,
        duration: int,
        *,
        spectrum: bool = False,
        duty_cycle: DutyCycle | None = None,
    ) -> None:
        """Launch a _TunerWorker thread for the given core.

        ``spectrum`` adds the light-load spectrum to the slot (load transitions +
        idle watch) — the load class that exposes max-boost marginality, which
        sustained stress alone cannot reach. ``duty_cycle`` cycles the payload
        at millisecond scale, which reaches the Vmin transient that neither
        sustained load nor the coarse spectrum can.
        """
        core_info = self._topology.cores.get(core_id)
        if not core_info:
            self._fail_test_async(core_id, f"Core {core_id} not found")
            return

        cs = self._core_states.get(core_id)
        backend_name, stress_mode_str, fft_preset_str, requested_threads = (
            self._get_active_stress_config(cs)
            if cs is not None
            else (self._config.backend, self._config.stress_mode, self._config.fft_preset, None)
        )
        self._worker_profile = "transient" if duty_cycle is not None else ("spectrum" if spectrum else "sustained")
        from corecycler.engine.backends.base import FFTPreset, StressMode

        try:
            _stress_mode = StressMode[stress_mode_str.upper()]
        except KeyError:
            _stress_mode = StressMode.SSE
        try:
            _fft_preset = FFTPreset[fft_preset_str.upper()]
        except KeyError:
            _fft_preset = FFTPreset.SMALL

        active = self._active_workload(cs) or {}
        tests = active.get("tests")
        stress_config = StressConfig(
            mode=_stress_mode,
            fft_preset=_fft_preset,
            threads=self._threads_for(core_id, requested_threads),
            memory_coupled=bool(active.get("memory_coupled")),
            tests=tuple(tests) if tests else None,
            duty_cycle=duty_cycle,
            test_seconds=max(1, duration // len(tests)) if tests else None,
        )
        scheduler_config = SchedulerConfig(
            seconds_per_core=duration,
            cores_to_test=[core_id],
            stop_on_error=True,
            cycle_count=1,
            max_temperature=self._config.max_temperature_c,
            over_temp_grace_seconds=self._config.over_temp_grace_seconds,
            over_temp_hard_margin=self._config.over_temp_hard_margin_c,
            require_thermal_sensor=not self._config.allow_missing_thermal_sensor,
            variable_load=spectrum,
            variable_load_interval=5.0 if spectrum else 15.0,
            idle_stability_test=15.0 if spectrum else 0.0,
            duty_cycle=duty_cycle,
        )

        try:
            backend = self._get_backend_for_name(backend_name)
            scheduler = CoreScheduler(
                topology=self._topology,
                backend=backend,
                stress_config=stress_config,
                scheduler_config=scheduler_config,
                work_dir=self._work_dir,
            )
        except Exception as e:
            self._fail_test_async(core_id, str(e))
            return

        regime = self._active_regime(cs) or str(active.get("regime", "primary"))
        logical_cpu = core_info.logical_cpus[0] if core_info.logical_cpus else core_id
        worker = _TunerWorker(
            core_id,
            logical_cpu,
            scheduler,
            msr=self._msr if self._config.stretch_threshold_pct > 0 else None,
            parent=self,
        )
        workload = self._workload_snapshot(
            cs,
            backend=backend_name,
            stress_mode=stress_mode_str,
            fft_preset=fft_preset_str,
            threads=stress_config.threads,
            profile=self._worker_profile,
            tests=tuple(tests) if tests else None,
        )
        workload["duration_seconds"] = duration
        workload["kind"] = "solo"
        slot: dict = {"core": core_id, "offset": cs.current_offset, "phase": str(cs.phase)}
        if not self._hunting and self._validation_stage == 0:
            regimes = self._slot_regimes(cs)
            slot["battery_regimes"] = regimes
            slot["battery_position"] = cs.battery_index % len(regimes) + 1
        self._launch_worker(
            worker,
            workload,
            self._cores_under_stress or [core_id],
            freeze_context=f"core {core_id} at {cs.current_offset} ({self._worker_profile}, {regime})",
            emit_started=core_id,
            slot=slot,
        )

    @Slot(int, bool, str, str, float, float, str, str)
    def _on_test_finished(
        self,
        core_id: int,
        passed: bool,
        error_msg: str,
        error_type: str,
        duration: float,
        peak_stretch_pct: float,
        mce_json: str = "",
        results_json: str = "",
    ) -> None:
        """Process test result — log, advance state machine, continue."""
        # The breadcrumb only has something to say while a payload is running.
        self._stop_freeze_monitor()
        # Check abort FIRST — if abort() already ran, don't touch any state.
        # The signal may fire after abort() disconnected it (Qt queued delivery).
        if self._abort_requested:
            # Still clean up the worker if it exists
            if self._worker is not None:
                self._worker.wait(1000)
                self._worker.deleteLater()
                self._worker = None
            return

        # Clean up worker reference
        if self._worker is not None:
            self._worker.wait(1000)
            self._worker.deleteLater()
            self._worker = None
        if self._status in DORMANT_STATUSES:
            self._sleep.release()
        if self._hunting and self._hunt is not None:
            self._hunt.armed = False
            self._save_hunt()
        elif self._session_id is not None:
            self._db.set_hunt_state(self._session_id, "")

        cs = self._core_states.get(core_id)
        if cs is None:
            return

        cs.in_test = False
        # A validation worker marks its whole stressed set in_test; the box
        # survived this result, so clear and persist all of them (not just the
        # reported core) before advancing. Keep the set: every core in it just
        # survived the same slot, and confidence is owed to all of them.
        stressed = list(self._cores_under_stress)
        self._clear_cores_under_stress()

        lacks_verdict = error_type in ("startup", "thermal")
        foreign = self._foreign_mce_by_core(-1 if self._soaking or lacks_verdict else core_id, mce_json)

        # A start-time/environment failure (missing binary, scheduler
        # construction error, harness exception) is not a stability verdict —
        # and nothing RAN, so it proves nothing about the resident offsets:
        # it must be handled BEFORE the journal is marked survived or the
        # crash-resume streak is reset. Persist the cleared in_test flag and
        # revert the never-tested offset, then pause with the reason.
        if not passed and error_type == "startup":
            if foreign:
                self._apply_foreign_evidence(foreign)
            if self._session_id:
                self._db.upsert_tuner_core_state(self._session_id, cs)
            if self._status == "validating":
                self._revert_all_to_baseline()
            else:
                self._revert_core_to_baseline(core_id)
            self.log_message.emit(
                f"Core {core_id}: test could not run — {error_msg}. "
                f"Pausing (environment issue, not a stability verdict)."
            )
            self.pause()
            return

        # Survival alone does not prove progress past the slot that rebooted.
        if self._session_id is not None:
            # Cores the kernel just named stay un-survived: surviving the test
            # does not clear an error the hardware reported minutes ago. A
            # machine check that names NO core taints every resident value —
            # fail closed and leave the whole set unproven for this test.
            if _has_unattributed_mce(mce_json):
                self.log_message.emit(
                    "Machine check without core attribution observed during the "
                    "test — resident offsets stay unproven for this run."
                )
            else:
                tp.journal_mark_survived(self._db, self._session_id, exclude_cores=sorted(foreign))
                for c, v in tp.journal_survived_values(self._db, self._session_id).items():
                    if self._is_more_aggressive(v, self._co_survived.get(c, 0)):
                        self._co_survived[c] = v

        # A thermal stop is not a stability verdict — advancing the state machine
        # or logging a fail here would push the offset the wrong way on a thermal
        # transient. Cool down and retry. Handled for the search flow,
        # validation, and hunt slots alike.
        if not passed and error_type == "thermal":
            if foreign:
                self._apply_foreign_evidence(foreign)
                if self._paused:
                    return
                if self._validation_stage > 0:
                    self._validation_stage_exit_to_search()
                    return
            if self._hunting:
                self._validation_thermal_aborts += 1
                if self._validation_thermal_aborts > self._config.max_thermal_retries:
                    self.log_message.emit(
                        "Crash hunt: thermal limit hit repeatedly — cooling cannot "
                        "sustain hunting. Fix cooling, then Resume."
                    )
                    self.abort()
                    return
                self._requeue_hunt_probe()
                self.log_message.emit("Attribution hunt: thermal stop — cooling down, retrying the same probe")
                QTimer.singleShot(
                    int(self._config.thermal_cooldown_seconds * 1000),
                    self._run_next_hunt_slot,
                )
            elif self._validation_stage == 0:
                self._handle_thermal_abort(core_id, cs, duration)
            else:
                self._handle_validation_thermal_abort(core_id)
            return

        # An apparatus fault is not a stability verdict: a stall means the load
        # never ran on the core, an external kill means something else stopped
        # the process, an unattributable machine check names nobody. Moving a
        # CO offset on any of them punishes an innocent core (85 back-offs in
        # one night came through the stall path). Retry without a verdict,
        # bounded, then stop honestly.
        if not passed and error_type in ("stall", "killed", "mce_unattributed", "unknown"):
            self._handle_apparatus_fault(core_id, error_msg, error_type, foreign)
            return

        # Reached only on a non-thermal outcome → the thermal-retry streak for
        # this core is broken; reset so the cap counts CONSECUTIVE thermal stops
        # at one offset, not lifetime thermals across the whole search.
        cs.thermal_aborts = 0
        self._apparatus_fault_streak = 0

        threshold = self._config.stretch_threshold_pct
        if passed and threshold > 0 and peak_stretch_pct > threshold:
            self.log_message.emit(
                f"Core {core_id}: active clock was {peak_stretch_pct:.1f}% below nominal. "
                "APERF/MPERF alone does not establish clock stretching or CO instability; verdict unchanged."
            )

        # Determine log phase
        phase_map = {
            TunerPhase.COARSE_SEARCH: "coarse",
            TunerPhase.FINE_SEARCH: "fine",
            TunerPhase.CONFIRMING: "confirm",
            TunerPhase.BACKOFF_PRECONFIRM: "backoff_preconfirm",
            TunerPhase.BACKOFF_CONFIRMING: "backoff_confirm",
        }
        if self._hunting:
            log_phase = "hunt"
        elif self._status == "validating" and self._validation_stage == 9:
            log_phase = "endurance"
        elif self._status == "validating" and self._validation_stage > 0:
            log_phase = f"validate_s{self._validation_stage}"
        else:
            log_phase = phase_map.get(cs.phase, "validate" if self._status == "validating" else cs.phase)

        # Log to DB (soak is a session-level watch, not one core's test — its
        # record is the narrative plus any mce_evidence rows)
        active_regime = self._active_regime(cs)
        if self._session_id and not self._soaking:
            backend, stress_mode, fft_preset, requested_threads = self._get_active_stress_config(cs)
            self._db.insert_tuner_test_log(
                self._session_id,
                core_id,
                cs.current_offset,
                log_phase,
                passed,
                error_msg=error_msg or None,
                error_type=error_type or None,
                duration=duration,
                backend=backend,
                stress_mode=stress_mode,
                fft_preset=fft_preset,
                peak_stretch_pct=peak_stretch_pct if peak_stretch_pct > 0 else None,
                threads=self._threads_for(core_id, requested_threads),
                profile=self._worker_profile,
                regime=active_regime,
            )
        if active_regime is not None and not foreign:
            pass_durations = self._parallel_pass_durations(results_json)
            if self._validation_stage == 9 and len(stressed) > 1:
                banked = [lane for lane in stressed if lane in pass_durations]
            else:
                banked = [core_id] if passed else []
            for banked_core in banked:
                live = self._co_applied.get(banked_core)
                if live is None:
                    live = self._core_states[banked_core].current_offset
                self._bank_clean_time(
                    banked_core,
                    live,
                    active_regime,
                    pass_durations.get(banked_core) or duration,
                )
            if banked and not self._hunting and self._db.get_resume_crash_streak(self._session_id):
                self._db.set_resume_crash_streak(self._session_id, 0)

        if results_json and self._session_id and self._validation_stage in (2, 3, 6, 9):
            self._log_parallel_rows(core_id, results_json, log_phase)
        status_str = "PASS" if passed else "FAIL"
        stretch_info = f" below-nominal:{peak_stretch_pct:.1f}%" if peak_stretch_pct > 0 else ""
        self.log_message.emit(
            f"Core {core_id} offset {cs.current_offset}: {status_str}{stretch_info}"
            + (f" ({error_msg})" if error_msg else "")
        )
        self.test_completed.emit(core_id, cs.current_offset, passed)

        # Revert tested core to baseline — no aggressive offset should linger.
        # Skip during validation (all confirmed offsets stay applied) and during
        # a hunt (the next slot manages the whole CO vector itself; a baseline
        # write here would put an unproven BIOS value back mid-hunt).
        if self._status not in ("validating", "hunting") and not self._revert_core_to_baseline(core_id):
            self.log_message.emit(
                f"Core {core_id}: test offset is still resident because the SMU "
                f"revert failed. Pausing (hardware-state fault, not a verdict)."
            )
            self.pause()
            return

        # Reset consecutive failure counter on any pass
        if passed:
            self._consecutive_start_failures = 0

        if not passed and self._validation_stage == 0 and not self._hunting and self._apparatus_suspect(core_id):
            return

        # Hunt slots have their own flow — a fail here is a FOUND CULPRIT.
        if self._hunting:
            self._on_hunt_slot_finished(core_id, passed, error_type, foreign)
            return

        if self._soaking:
            self._soaking = False
            if foreign:
                self._apply_foreign_evidence(foreign)
                if self._paused:
                    return
                self._validation_dirty = True
                self._save_validation_pos()
                self.log_message.emit(
                    "Soak found hardware evidence — leaving validation; the named core(s) re-earn confirmation first."
                )
                self._validation_stage_exit_to_search()
                return
            if passed:
                self.log_message.emit("Real-world soak passed — no kernel events")
                self._validation_stage = 8
                self._save_validation_pos()
                QTimer.singleShot(0, self._run_validation_next)
                return
            self._validation_dirty = True
            if self._session_id is not None:
                n = self._db.get_unattributed_crashes(self._session_id) + 1
                self._db.set_unattributed_crashes(self._session_id, n)
                if n >= self._config.max_unattributed_crash_hunts:
                    self._save_validation_pos()
                    self.log_message.emit(
                        f"Soak saw an unattributed kernel event with no core named "
                        f"({n} in a row). Pausing for your call: check the kernel "
                        f"journal around the event, PSU/memory/thermals, or lower "
                        f"max_offset, then Resume."
                    )
                    self.pause()
                    return
            self._save_validation_pos()
            self.log_message.emit(
                "Soak saw an unattributed kernel event — not a clean pass; "
                "re-proving the profile with a fresh validation pass."
            )
            QTimer.singleShot(0, self._run_validation_next)
            return

        # Hardware evidence about OTHER cores outranks the normal flow: demote
        # the named cores, and if validation was running leave it so they
        # re-earn confirmation first (validation restarts once all are back).
        if foreign:
            self._apply_foreign_evidence(foreign)
            if self._paused:
                return
            if self._validation_stage > 0:
                self.log_message.emit(
                    "Leaving validation: kernel evidence named other core(s); "
                    "they must re-earn confirmation, then validation restarts."
                )
                self._validation_stage = 0
                self._transition_status("running")
                QTimer.singleShot(0, self._run_next)
                return

        # Multi-core validation uses its own flow — don't advance per-core state machine
        if self._validation_stage > 0:
            self._on_validation_test_finished(core_id, passed, duration)
            return

        cs = self._core_states[core_id]
        self._accumulate_test_time(cs, duration)

        # An offset is only accepted once every regime in the slot's battery
        # has passed at it. A pass with regimes still to run re-tests the same
        # offset under the next one; a fail ends the slot immediately, because
        # a short fail is conclusive while a short pass proves nothing.
        regimes = self._slot_regimes(cs)
        if passed and cs.battery_index + 1 < len(regimes):
            cs.battery_index += 1
            if self._session_id:
                self._db.upsert_tuner_core_state(self._session_id, cs)
            QTimer.singleShot(0, self._run_next)
            return
        finished_regime = regimes[cs.battery_index % len(regimes)]
        self._regime_rotation[finished_regime] = self._regime_rotation.get(finished_regime, 0) + 1
        cs.battery_index = 0
        self._battery_orders.pop(core_id, None)
        self._advance_core(core_id, passed)
        if self._check_time_budget(cs):
            self._db.upsert_tuner_core_state(self._session_id, cs)
            return

        # Continue with the next test on a fresh event-loop stack (matches the
        # validation path) so a synchronous start failure cannot recurse back
        # into _on_test_finished.
        QTimer.singleShot(0, self._run_next)

    @staticmethod
    def _parallel_pass_durations(results_json: str) -> dict[int, float]:
        """Explicit PASS lanes and their observed durations; absent lanes earn nothing."""
        if not results_json:
            return {}
        try:
            entries = json.loads(results_json)
        except (json.JSONDecodeError, TypeError):
            return {}
        if not isinstance(entries, list):
            return {}
        passed: dict[int, float] = {}
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("passed") is not True:
                continue
            core_id = entry.get("core")
            lane_duration = entry.get("duration")
            if isinstance(core_id, int):
                passed[core_id] = float(lane_duration) if isinstance(lane_duration, (int, float)) else 0.0
        return passed

    def _log_parallel_rows(self, reported: int, results_json: str, phase: str) -> None:
        """Record every lane's verdict from a simultaneous stage, not only the
        reported core's. Fail closed: a malformed payload records nothing."""
        try:
            entries = json.loads(results_json)
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(entries, list):
            return
        backend, stress_mode, fft_preset, requested_threads = self._get_active_stress_config(
            self._core_states[reported]
        )
        for e in entries:
            if not isinstance(e, dict):
                continue
            core = e.get("core")
            if not isinstance(core, int) or core == reported:
                continue
            cs = self._core_states.get(core)
            if cs is None:
                continue
            offset = self._co_applied.get(core)
            if offset is None:
                offset = cs.best_offset if cs.best_offset is not None else cs.current_offset
            duration = e.get("duration")
            self._db.insert_tuner_test_log(
                self._session_id,
                core,
                offset,
                phase,
                bool(e.get("passed")),
                error_msg=e.get("error_message"),
                error_type=e.get("error_type"),
                duration=float(duration) if isinstance(duration, (int, float)) else None,
                backend=backend,
                stress_mode=stress_mode,
                fft_preset=fft_preset,
                threads=self._threads_for(core, requested_threads),
                profile="sustained",
            )

    def _handle_thermal_abort(self, core_id: int, cs: CoreState, duration: float) -> None:
        """Handle a test stopped by the thermal safety limit (not instability).

        Stopping on temperature says nothing about CO stability, so advancing the
        state machine or logging a fail would push the offset the wrong way on a
        thermal transient. Instead: revert the core, defer it (cool down while
        other cores test), and retry the SAME offset. If a core keeps hitting the
        limit, cooling cannot sustain the test — abort with a clear message rather
        than silently producing a bad tune.
        """
        cs.thermal_aborts += 1
        if not self._revert_core_to_baseline(core_id):
            self.log_message.emit(
                f"Core {core_id}: SMU revert failed during thermal handling. Pausing (hardware-state fault)."
            )
            self.pause()
            return
        # The partial test still ran real seconds — count them so a thermal loop
        # is bounded by the per-core time budget too, not only the retry cap.
        self._accumulate_test_time(cs, duration)

        if cs.thermal_aborts > self._config.max_thermal_retries:
            self.log_message.emit(
                f"Core {core_id}: thermal limit hit {cs.thermal_aborts} times at "
                f"offset {cs.current_offset} — cooling cannot sustain testing. "
                f"Lower load/ambient or improve cooling, then resume."
            )
            log.warning(
                "Aborting tune: core %d hit thermal limit %d times (max_thermal_retries=%d)",
                core_id,
                cs.thermal_aborts,
                self._config.max_thermal_retries,
            )
            self.abort()
            return

        cs.crash_cooldown = max(cs.crash_cooldown, 2)  # prefer other cores meanwhile
        self.log_message.emit(
            f"Core {core_id} offset {cs.current_offset}: thermal abort "
            f"({cs.thermal_aborts}/{self._config.max_thermal_retries}) — cooling "
            f"down, will retry same offset"
        )
        self._db.upsert_tuner_core_state(self._session_id, cs)
        # Real wall-clock cooldown before retrying the same offset: crash_cooldown
        # is only a pick-counter and gives no cooling when this is the last active
        # core. QTimer also breaks the _on_test_finished call stack (re-entrancy).
        QTimer.singleShot(int(self._config.thermal_cooldown_seconds * 1000), self._run_next)

    def _handle_validation_thermal_abort(self, core_id: int) -> None:
        """A thermal stop during validation is not a stability verdict.

        Backing off a confirmed core and restarting validation (the default
        fail path) on a thermal transient would degrade the tune. Instead: cool
        down and re-run the same validation stage. If the limit keeps tripping,
        cooling cannot sustain the test — abort with a clear message.
        """
        self._validation_thermal_aborts += 1
        if self._validation_thermal_aborts > self._config.max_thermal_retries:
            self.log_message.emit(
                "Validation: thermal limit hit repeatedly — cooling cannot "
                "sustain testing. Lower load/ambient or improve cooling, then resume."
            )
            log.warning(
                "Aborting validation: thermal limit hit %d times",
                self._validation_thermal_aborts,
            )
            self.abort()
            return
        self.log_message.emit(
            f"Validation: core {core_id} thermal abort "
            f"({self._validation_thermal_aborts}/{self._config.max_thermal_retries}) "
            f"— cooling down, re-running the same step"
        )
        QTimer.singleShot(
            int(self._config.thermal_cooldown_seconds * 1000),
            self._run_validation_requeue if self._in_requeue else self._run_validation_next,
        )

    def _handle_apparatus_fault(self, core_id: int, error_msg: str, error_type: str, foreign: dict[int, dict]) -> None:
        """Retry the current step after a fault that proves nothing about the
        silicon; after max_apparatus_retries consecutive faults, stop honestly.

        No CO offset moves here: the search bounds, validation back-offs and
        crash penalties all stay untouched. Hardware evidence about OTHER
        cores that arrived with the fault is still applied — evidence outranks
        the retry.
        """
        self._soaking = False
        if foreign:
            self._apply_foreign_evidence(foreign)
            if self._paused:
                return
            if self._validation_stage > 0:
                self.log_message.emit(
                    "Leaving validation: kernel evidence named other core(s); "
                    "they must re-earn confirmation, then validation restarts."
                )
                self._validation_stage = 0
                self._transition_status("running")
                QTimer.singleShot(0, self._run_next)
                return

        self._apparatus_fault_streak += 1
        limit = self._config.max_apparatus_retries
        if self._apparatus_fault_streak > limit:
            self.log_message.emit(
                f"Stress apparatus failed {self._apparatus_fault_streak} times in "
                f"a row ({error_type}: {error_msg}) — the environment cannot run "
                f"this test, and repeating it would prove nothing. Stopping with "
                f"offsets reverted to baseline; fix the cause (backend install, "
                f"foreign load, permissions), then Resume."
            )
            self.abort()
            return

        self.log_message.emit(
            f"Core {core_id}: apparatus fault ({error_msg}) — retrying the same "
            f"step without a verdict "
            f"({self._apparatus_fault_streak}/{limit})"
        )
        if self._hunting:
            self._requeue_hunt_probe()
            QTimer.singleShot(0, self._run_next_hunt_slot)
        elif self._validation_stage > 0:
            QTimer.singleShot(
                0,
                self._run_validation_requeue if self._in_requeue else self._run_validation_next,
            )
        else:
            if not self._revert_core_to_baseline(core_id):
                self.log_message.emit(
                    f"Core {core_id}: test offset is still resident because the "
                    f"SMU revert failed. Pausing (hardware-state fault)."
                )
                self.pause()
                return
            QTimer.singleShot(0, self._run_next)

    def _complete_session(self) -> None:
        """All cores done — enter auto-validation or finalize session."""
        profile = {}
        for cs in self._core_states.values():
            if cs.best_offset is not None:
                profile[cs.core_id] = cs.best_offset

        # CONFIRMED is the only phase a core rests in; the per-slot regime
        # battery is what an offset must survive to reach it.
        all_done = all(cs.phase is TunerPhase.CONFIRMED for cs in self._core_states.values())
        if not all_done:
            return

        # If auto_validate is on and we just finished per-core search (not
        # already validating), enter multi-core validation instead of completing.
        if (
            self._config.auto_validate
            and self._validation_stage == 0
            and len(profile) > 1  # single-core has nothing to cross-validate
        ):
            self.log_message.emit(f"All {len(profile)} cores confirmed — entering multi-core validation")
            session = self._db.get_tuner_session(self._session_id) if self._session_id is not None else None
            self._enter_auto_validation(profile, resume_from=session)
            return

        self._finalize_session(profile)

    def _finalize_session(self, profile: dict[int, int]) -> None:
        if self._validation_dirty:
            self.log_message.emit(
                "Refusing to declare completion: a final clean validation pass is still owed. "
                "Reverting to baselines and pausing."
            )
            self._revert_all_to_baseline()
            self._save_validation_pos()
            self.pause()
            return
        failed: list[int] = []
        for core_id, offset in profile.items():
            if not self._write_co_verified(core_id, offset):
                failed.append(core_id)
                break
        if failed:
            stock_failed = self._restore_stock_verified()
            self._quarantine_session(
                0,
                reason=f"confirmed profile write failed for core {failed[0]}",
                stock_failures=stock_failed,
            )
            return
        self.log_message.emit("Applied confirmed CO profile to SMU")
        self._transition_status("completed")
        if self._session_id:
            self._db.set_unattributed_crashes(self._session_id, 0)
        self._validation_stage = 0
        self._validation_requeue = []
        self._save_validation_pos()
        self._set_status("idle")
        self._emit_progress()
        self.log_message.emit(f"Tuner complete - {len(profile)} cores confirmed")
        self.session_completed.emit(json.dumps(profile))

    # ------------------------------------------------------------------
    # Multi-core validation (3-stage)
    # ------------------------------------------------------------------

    def _enter_auto_validation(self, profile: dict[int, int], resume_from=None) -> None:
        """Begin or CONTINUE the multi-core validation sequence.

        Stage 1: Per-core with all offsets live — stress each core individually
                 while all other cores hold their confirmed offsets.
        Stage 2: All-core coverage with all offsets applied.
        Stage 3: Half-core load — half tested / half idle, rotating.

        ``resume_from`` (a TunerSession) restores the persisted cursor so a
        reboot, app restart, or a search interlude after a penalty continues
        where validation was — never a full stage-1 restart. Cores whose
        current best has no logged stage-1 pass (their offset changed since)
        are requeued for a solo re-test first; every other core's coverage is
        still valid — raising one core's voltage cannot destabilize others.
        """
        self._transition_status("validating")

        # Stage-1 order is deterministic (sorted), so a restored index means
        # the same cores; halves are CCD-split (or even/odd), also stable.
        self._validation_core_order = sorted(profile.keys())
        self._validation_halves = [h for h in self._split_cores_into_halves(profile) if h]
        self._validation_thermal_aborts = 0

        if resume_from is not None and resume_from.validation_stage > 0:
            # Clamp below the terminal soak (7) and finalize sentinel (8): a
            # resume re-runs synthetic stages, never lands straight in the soak.
            # Endurance (9) is its own perpetual stage and resumes in place.
            stage = resume_from.validation_stage
            self._validation_stage = 9 if stage == 9 else min(stage, 6)
            self._endurance_round = max(0, resume_from.endurance_round)
            self._endurance_workload = max(
                0, min(resume_from.endurance_workload, len(self._config.endurance_workloads))
            )
            self._endurance_index = max(0, min(resume_from.endurance_index, len(self._validation_core_order)))
            self._validation_core_index = max(0, min(resume_from.validation_index, len(self._validation_core_order)))
            self._validation_half_index = max(0, min(resume_from.validation_half, len(self._validation_halves)))
            self._validation_dirty = self._validation_dirty or resume_from.validation_dirty
            try:
                raw = json.loads(resume_from.validation_requeue)
            except (json.JSONDecodeError, TypeError):
                raw = []  # fail closed: a corrupt cursor loses only the hint
            requeue = [c for c in raw if isinstance(c, int) and c in self._core_states] if isinstance(raw, list) else []
            if self._validation_stage >= 2:
                for c in self._validation_core_order:
                    if c not in requeue and not self._has_stage1_pass_at_current_best(c):
                        requeue.append(c)
            self._validation_requeue = requeue
            self.log_message.emit(
                f"Continuing validation at stage {self._validation_stage} "
                f"(position preserved; {len(requeue)} core(s) owe a solo re-test)"
            )
            self._save_validation_pos()
            self._save_endurance_pos()
            if requeue:
                self._run_validation_requeue()
            else:
                self._run_validation_next()
            return

        self._validation_core_index = 0
        self._validation_half_index = 0
        self._validation_stage = 1
        self._validation_dirty = False
        self._validation_requeue = []
        self._endurance_round = 0
        self._endurance_workload = 0
        self._endurance_index = 0
        self._save_endurance_pos()
        self.log_message.emit("Validation stage 1: per-core with all offsets live")
        self.validation_progress.emit(1, 0, len(self._validation_core_order))
        self._save_validation_pos()
        self._run_validation_next()

    def _save_validation_pos(self) -> None:
        """Persist the validation cursor after every transition, so progress
        survives power loss and app restarts alike."""
        if self._session_id is None:
            return
        self._db.set_validation_position(
            self._session_id,
            self._validation_stage,
            self._validation_core_index,
            self._validation_half_index,
            self._validation_dirty,
            json.dumps(self._validation_requeue),
        )
        log.debug(
            "validation cursor: stage=%d index=%d half=%d dirty=%s requeue=%s",
            self._validation_stage,
            self._validation_core_index,
            self._validation_half_index,
            self._validation_dirty,
            self._validation_requeue,
        )

    def _has_stage1_pass_at_current_best(self, core_id: int) -> bool:
        """True when the test log holds a real all-offsets-live solo pass at the
        core's CURRENT best offset — the evidence a solo re-test would reproduce.
        Endurance slots run with every offset live too, so they count."""
        if self._session_id is None:
            return False
        cs = self._core_states.get(core_id)
        if cs is None or cs.best_offset is None:
            return False
        for r in self._db.get_tuner_test_log(self._session_id, core_id=core_id):
            if (
                r.get("phase") in ("validate_s1", "endurance")
                and r.get("passed")
                and r.get("offset_tested") == cs.best_offset
                and r.get("duration_seconds") is not None
            ):
                return True
        return False

    def _run_validation_requeue(self) -> None:
        """Solo re-test (all offsets live) for cores whose offset changed —
        the only coverage a one-core back-off invalidates. When the queue
        drains, the pending stage reruns."""
        if self._abort_requested or self._paused:
            return
        if not self._validation_requeue:
            self._in_requeue = False
            self._save_validation_pos()
            QTimer.singleShot(0, self._run_validation_next)
            return
        self._in_requeue = True
        core_id = self._validation_requeue[0]
        cs = self._core_states[core_id]
        offset = cs.best_offset if cs.best_offset is not None else cs.baseline_offset
        self.log_message.emit(
            f"Validation re-test: core {core_id} solo at {offset} "
            f"({len(self._validation_requeue)} owed), then stage "
            f"{self._validation_stage} reruns"
        )
        if self._smu is not None and not self._apply_validation_offsets(core_id, offset):
            return
        self._last_tested_core = core_id
        self._mark_cores_under_stress([core_id])
        self._start_worker(core_id, self._config.validate_duration_seconds)

    def _split_cores_into_halves(self, profile: dict[int, int]) -> list[list[int]]:
        """Split confirmed cores into two halves for stage 3.

        Uses CCD boundaries when available (tests cross-CCD power interactions).
        Falls back to even index / odd index split.
        """
        cores = sorted(profile.keys())
        ccd_groups: dict[int, list[int]] = {}
        for core_id in cores:
            core_info = self._topology.cores.get(core_id)
            ccd = core_info.ccd if core_info and core_info.ccd is not None else 0
            ccd_groups.setdefault(ccd, []).append(core_id)

        if len(ccd_groups) >= 2:
            # Split by CCD — half_a = first CCD(s), half_b = remaining
            sorted_ccds = sorted(ccd_groups.keys())
            mid = len(sorted_ccds) // 2
            half_a = []
            half_b = []
            for i, ccd in enumerate(sorted_ccds):
                if i < mid:
                    half_a.extend(ccd_groups[ccd])
                else:
                    half_b.extend(ccd_groups[ccd])
            return [sorted(half_a), sorted(half_b)]

        # Single CCD — split by index
        return [cores[::2], cores[1::2]]

    def _get_validation_stage_count(self) -> int:
        """Total enabled validation stages (3 base + transitions/spectrum/memory/soak)."""
        return (
            3
            + int(self._config.validate_transitions)
            + int(self._config.validate_spectrum)
            + int(self._config.validate_memory)
            + int(self._config.validate_soak)
        )

    def _run_validation_stage4(self) -> None:
        """S4: Rapid transition stress — all cores, load/idle cycling.

        Runs rapid load/idle transitions on all confirmed cores using the
        scheduler's run_rapid_transitions(). This catches instability during
        idle↔boost transitions that sustained stress tests miss.
        """
        cores = self._validation_core_order
        self.log_message.emit(f"Validation stage 4: rapid load/idle transitions on {len(cores)} cores")
        self.validation_progress.emit(4, 0, 1)

        # Apply all confirmed offsets
        if self._smu is not None:
            first_core = cores[0]
            cs = self._core_states[first_core]
            offset = cs.best_offset if cs.best_offset is not None else cs.baseline_offset
            if not self._apply_validation_offsets(first_core, offset):
                return

        checkpoint = self._workload_snapshot(
            self._core_states[cores[0]],
            threads=2,
            profile="spectrum",
        )
        checkpoint["duration_seconds"] = self._config.validate_duration_seconds
        checkpoint["kind"] = "rapid_transition"
        self._hunt_workload = checkpoint
        self._last_tested_core = cores[0]
        self._mark_cores_under_stress(cores)
        self._start_rapid_transition_worker(cores, self._config.validate_duration_seconds, checkpoint)

    def _run_validation_next(self) -> None:
        """Dispatch the next validation test based on current stage."""
        if self._abort_requested or self._paused:
            return

        match self._validation_stage:
            case 1:
                self._run_validation_stage1()
            case 2:
                self._run_validation_stage2()
            case 3:
                self._run_validation_stage3()
            case 4:
                if self._config.validate_transitions:
                    self._run_validation_stage4()
                else:
                    self._validation_stage = 5
                    self._validation_core_index = 0
                    self._save_validation_pos()
                    QTimer.singleShot(0, self._run_validation_next)
            case 5:
                if self._config.validate_spectrum:
                    self._run_validation_stage5()
                else:
                    self._validation_stage = 6
                    self._save_validation_pos()
                    QTimer.singleShot(0, self._run_validation_next)
            case 6:
                if self._config.validate_memory and self._get_memory_backend() is not None:
                    self._run_validation_memory()
                else:
                    if self._config.validate_memory:
                        self.log_message.emit(
                            "Validation stage 6 (memory load) skipped: no memory "
                            "stress tool (stressapptest) is installed."
                        )
                    self._validation_stage = 7
                    self._save_validation_pos()
                    QTimer.singleShot(0, self._run_validation_next)
            case 7:
                # A dirty pass skips the soak; the final clean pass earns it.
                if self._config.validate_soak and not self._validation_dirty:
                    self._run_validation_soak()
                else:
                    self._validation_stage = 8
                    self._save_validation_pos()
                    QTimer.singleShot(0, self._run_validation_next)
            case 9:
                self._run_endurance_next()
            case _:
                # All stages complete. If any back-off happened along the way,
                # the profile changed mid-pass — run ONE final complete pass
                # that must come through clean before DONE is declared.
                if self._validation_dirty:
                    self.log_message.emit(
                        "All stages passed, but cores were backed off along the "
                        "way — running one final clean validation pass to prove "
                        "the finished profile."
                    )
                    self._validation_dirty = False
                    self._validation_stage = 1
                    self._validation_core_index = 0
                    self._validation_half_index = 0
                    self._validation_requeue = []
                    self._save_validation_pos()
                    QTimer.singleShot(0, self._run_validation_next)
                    return
                profile = {
                    cs.core_id: cs.best_offset for cs in self._core_states.values() if cs.best_offset is not None
                }
                self.log_message.emit("All validation stages passed in one clean pass")
                self._validation_core_index = 0
                self._validation_half_index = 0
                if self._config.endurance:
                    self._enter_endurance()
                    return
                self._finalize_session(profile)

    def _run_validation_stage1(self) -> None:
        """Stage 1: test each core individually with all offsets applied."""
        if self._validation_core_index >= len(self._validation_core_order):
            # Stage 1 complete — advance to stage 2
            self._validation_stage = 2
            self._save_validation_pos()
            self.log_message.emit("Validation stage 1 passed — stage 2: all-core coverage")
            QTimer.singleShot(0, self._run_validation_next)
            return

        core_id = self._validation_core_order[self._validation_core_index]
        cs = self._core_states[core_id]
        offset = cs.best_offset if cs.best_offset is not None else cs.baseline_offset

        self.log_message.emit(
            f"Validation 1/{len(self._validation_core_order)}: core {core_id} at offset {offset} (all offsets live)"
        )
        self.validation_progress.emit(1, self._validation_core_index, len(self._validation_core_order))

        # Apply all confirmed offsets
        if self._smu is not None and not self._apply_validation_offsets(core_id, offset):
            return

        self._last_tested_core = core_id
        self._mark_cores_under_stress([core_id])
        self._start_worker(core_id, self._config.validate_duration_seconds)

    def _run_validation_stage2(self) -> None:
        """Stage 2: all cores stressed simultaneously — full package power
        draw, one pinned process per core, per-core verdicts."""
        cores = self._validation_core_order
        self.log_message.emit(
            f"Validation stage 2: stressing all {len(cores)} cores "
            f"simultaneously ({self._config.validate_duration_seconds}s, "
            f"all offsets applied)"
        )
        self.validation_progress.emit(2, 0, 1)

        # Apply all confirmed offsets
        if self._smu is not None:
            first_core = cores[0]
            cs = self._core_states[first_core]
            offset = cs.best_offset if cs.best_offset is not None else cs.baseline_offset
            if not self._apply_validation_offsets(first_core, offset):
                return

        self._last_tested_core = cores[0]
        self._mark_cores_under_stress(cores)
        self._start_multi_core_worker(cores, self._config.validate_duration_seconds)

    # ------------------------------------------------------------------
    # Endurance (validation stage 9): perpetual confirmation of the live profile
    # ------------------------------------------------------------------

    def _enter_endurance(self) -> None:
        """Replace completion with an endless confirmation loop over the
        configured workload matrix. Cores stay CONFIRMED; a failing slot backs
        its core off one fine step, exactly like any other validation stage."""
        self._validation_stage = 9
        self._endurance_round = 0
        self._endurance_workload = 0
        self._endurance_index = 0
        self._validation_dirty = False
        if self._session_id:
            # The clean pass just proved the profile; stale unexplained
            # incidents must not haunt the next resume (mirrors finalize).
            self._db.set_unattributed_crashes(self._session_id, 0)
        self._save_validation_pos()
        self._save_endurance_pos()
        self.log_message.emit(
            f"All validation stages passed - entering endurance: "
            f"{len(self._config.endurance_workloads)} workload(s), "
            f"{self._config.endurance_slot_seconds}s slots doubling each round up to "
            f"{self._config.endurance_slot_max_seconds}s. Runs until stopped; "
            f"'corecycler status' shows accumulated evidence."
        )
        QTimer.singleShot(0, self._run_validation_next)

    def _save_endurance_pos(self) -> None:
        if self._session_id is None:
            return
        self._db.set_endurance_position(
            self._session_id,
            self._endurance_round,
            self._endurance_workload,
            self._endurance_index,
        )

    def _endurance_duration(self) -> int:
        """Slot length for the current round: doubles each round, capped."""
        grown = self._config.endurance_slot_seconds * 2 ** min(self._endurance_round, 20)
        return min(grown, self._config.endurance_slot_max_seconds)

    def _run_endurance_next(self) -> None:
        """Run the next endurance slot: one solo slot per core (all offsets
        live), then one all-core slot, per workload, per round."""
        workloads = self._config.endurance_workloads
        if self._endurance_workload >= len(workloads):
            self._finish_endurance_round()
            return

        order = self._validation_core_order
        wl = workloads[self._endurance_workload]
        spectrum = wl.get("profile") == "spectrum"
        duration = self._endurance_duration()
        label = tp.workload_label(
            wl["backend"], wl["stress_mode"], wl["fft_preset"], wl.get("threads"), wl.get("profile")
        )
        slots = len(order) + (0 if spectrum else 1)
        prefix = f"Endurance r{self._endurance_round} {self._endurance_workload + 1}/{len(workloads)} {label}"

        if self._endurance_index < len(order):
            core_id = order[self._endurance_index]
            cs = self._core_states[core_id]
            offset = cs.best_offset if cs.best_offset is not None else cs.baseline_offset
            self.log_message.emit(f"{prefix}: core {core_id} at {offset} for {duration}s (all offsets live)")
            self.validation_progress.emit(9, self._endurance_index, slots)
            if self._smu is not None and not self._apply_validation_offsets(core_id, offset):
                return
            self._last_tested_core = core_id
            self._mark_cores_under_stress([core_id])
            self._start_worker(
                core_id,
                duration,
                spectrum=spectrum,
                duty_cycle=_duty_cycle_for(wl),
            )
            return

        if self._endurance_index == len(order) and not spectrum:
            self.log_message.emit(f"{prefix}: all {len(order)} cores for {duration}s")
            self.validation_progress.emit(9, len(order), slots)
            if self._smu is not None:
                first_core = order[0]
                cs = self._core_states[first_core]
                offset = cs.best_offset if cs.best_offset is not None else cs.baseline_offset
                if not self._apply_validation_offsets(first_core, offset):
                    return
            self._last_tested_core = order[0]
            self._mark_cores_under_stress(order)
            self._start_multi_core_worker(order, duration, workload=wl)
            return

        self._endurance_workload += 1
        self._endurance_index = 0
        self._save_endurance_pos()
        QTimer.singleShot(0, self._run_validation_next)

    def _finish_endurance_round(self) -> None:
        """Report the round's evidence ledger and start the next, longer one."""
        clean = not self._validation_dirty
        self._endurance_round += 1
        self.log_message.emit(
            f"Endurance round {self._endurance_round - 1} complete "
            f"({'clean' if clean else 'with back-offs'}) - next slots {self._endurance_duration()}s"
        )
        if self._session_id:
            summary = tp.evidence_summary(self._db, self._session_id, self._core_states, self._config.direction)
            for core_id in sorted(self._core_states):
                cs = self._core_states[core_id]
                self.log_message.emit(tp.format_evidence_line(core_id, cs.best_offset, summary.get(core_id, {})))
            if clean:
                self._db.set_unattributed_crashes(self._session_id, 0)
        self._validation_dirty = False
        self._endurance_workload = 0
        self._endurance_index = 0
        self._save_validation_pos()
        self._save_endurance_pos()
        # A round of clean endurance is exactly the evidence annealing spends.
        # Probing now, at the round boundary, keeps the vector improving for
        # as long as the machine is left running instead of freezing it at
        # whatever the first search pass happened to find.
        if self._anneal_candidate() is not None:
            self._validation_stage_exit_to_search()
            return
        QTimer.singleShot(0, self._run_validation_next)

    def _run_validation_memory(self) -> None:
        """Stage 6: all cores stressed simultaneously under a MEMORY load with
        all offsets live — catches CO marginality that only shows under
        memory-controller pressure, invisible to the CPU-only stages."""
        cores = self._validation_core_order
        backend = self._get_memory_backend()
        if backend is None:
            # Availability was checked before dispatch; a race is treated as a
            # skip, never a silicon verdict.
            self._validation_stage = 7
            self._save_validation_pos()
            QTimer.singleShot(0, self._run_validation_next)
            return
        self.log_message.emit(
            f"Validation stage 6: memory-load stress on all {len(cores)} cores "
            f"simultaneously ({self._config.validate_duration_seconds}s, "
            f"all offsets applied)"
        )
        self.validation_progress.emit(6, 0, 1)

        if self._smu is not None:
            first_core = cores[0]
            cs = self._core_states[first_core]
            offset = cs.best_offset if cs.best_offset is not None else cs.baseline_offset
            if not self._apply_validation_offsets(first_core, offset):
                return

        self._last_tested_core = cores[0]
        self._mark_cores_under_stress(cores)
        self._start_multi_core_worker(
            cores,
            self._config.validate_duration_seconds,
            backend=backend,
            memory_mb=default_memory_mb(len(cores)),
        )

    def _run_validation_stage3(self) -> None:
        """Stage 3: alternating half-core load — catches voltage transients."""
        if self._validation_half_index >= len(self._validation_halves):
            # Stage 3 complete — advance to S4 (rapid transitions) or finalize
            self._validation_stage = 4
            self._save_validation_pos()
            self.log_message.emit("Validation stage 3 passed")
            QTimer.singleShot(0, self._run_validation_next)
            return

        half = self._validation_halves[self._validation_half_index]
        half_label = "A" if self._validation_half_index == 0 else "B"
        self.log_message.emit(
            f"Validation stage 3{half_label}: cores {half} loaded simultaneously, the other half idle at their offsets"
        )
        self.validation_progress.emit(3, self._validation_half_index, len(self._validation_halves))

        # Apply all confirmed offsets (even idle cores hold their offsets)
        if self._smu is not None:
            first_core = half[0]
            cs = self._core_states[first_core]
            offset = cs.best_offset if cs.best_offset is not None else cs.baseline_offset
            if not self._apply_validation_offsets(first_core, offset):
                return

        self._last_tested_core = half[0]
        self._mark_cores_under_stress(half)
        self._start_multi_core_worker(half, self._config.validate_duration_seconds)

    def _run_validation_stage5(self) -> None:
        """Stage 5: per-core light-load spectrum with all offsets live —
        max-boost bursts, load transitions and idle watch."""
        order = self._validation_core_order
        if self._validation_core_index >= len(order):
            self._validation_stage = 6
            self._validation_core_index = 0
            self._save_validation_pos()
            self.log_message.emit("Validation stage 5 passed")
            QTimer.singleShot(0, self._run_validation_next)
            return
        core_id = order[self._validation_core_index]
        cs = self._core_states[core_id]
        offset = cs.best_offset if cs.best_offset is not None else cs.baseline_offset
        self.log_message.emit(
            f"Validation 5/{len(order)}: core {core_id} spectrum at {offset} "
            f"(bursts + transitions + idle, all offsets live)"
        )
        self.validation_progress.emit(5, self._validation_core_index, len(order))
        if self._smu is not None and not self._apply_validation_offsets(core_id, offset):
            return
        self._last_tested_core = core_id
        self._mark_cores_under_stress([core_id])
        self._start_worker(core_id, self._config.spectrum_slot_seconds, spectrum=True)

    def _run_validation_soak(self) -> None:
        """Validation stage 7: watch kernel errors and temperature without synthetic load."""
        cores = self._validation_core_order
        self.log_message.emit(
            f"Validation stage 7: real-world soak - watching kernel errors and temperature "
            f"for {self._config.soak_duration_seconds}s with no synthetic load."
        )
        self.validation_progress.emit(7, 0, 1)
        if self._smu is not None and cores:
            first = cores[0]
            cs = self._core_states[first]
            offset = cs.best_offset if cs.best_offset is not None else cs.baseline_offset
            if not self._apply_validation_offsets(first, offset):
                return
        self._last_tested_core = cores[0] if cores else None
        workload = {
            **self._workload_snapshot(self._core_states[cores[0]]),
            "kind": "soak",
            "duration_seconds": self._config.soak_duration_seconds,
        }
        self._mark_cores_under_stress(cores)
        self._start_soak_worker(cores, self._config.soak_duration_seconds, workload)

    def _get_memory_backend(self):
        """The memory stress backend (stressapptest) if installed, else None.

        A missing memory tool is an environment condition, not a stability
        verdict — the memory validation stage skips rather than fails on it.
        """
        try:
            backend = get_backend("stressapptest")
        except KeyError:
            return None
        return backend if backend.is_available() else None

    def _start_multi_core_worker(
        self,
        cores: list[int],
        duration: int,
        backend=None,
        memory_mb: int | None = None,
        workload: dict | None = None,
    ) -> None:
        """Launch every core's stress process simultaneously (one pinned
        process per core) with per-core verdicts; the worker reports the
        first failing core, else the first core's pass. ``backend`` overrides
        the configured CPU backend and ``memory_mb`` sizes each process (the
        memory stage passes stressapptest and its per-lane share of RAM, since
        every lane's process allocates at once). ``workload`` (endurance) names
        the backend, mode, preset and per-core thread count for the slot."""
        self._worker_profile = "sustained"
        self._freeze_context(f"cores {cores} ({'hunt probe' if self._hunting else f'stage {self._validation_stage}'})")
        if workload is not None:
            from corecycler.engine.backends.base import FFTPreset, StressMode

            duty = _duty_cycle_for(workload)
            tests = workload.get("tests")
            self._worker_profile = "transient" if duty is not None else "sustained"
            try:
                stress_config = StressConfig(
                    mode=StressMode[workload["stress_mode"].upper()],
                    fft_preset=FFTPreset[workload["fft_preset"].upper()],
                    threads=self._threads_for(cores[0], workload.get("threads")),
                    memory_mb=memory_mb,
                    memory_coupled=bool(workload.get("memory_coupled")),
                    tests=tuple(tests) if tests else None,
                    test_seconds=max(1, duration // len(tests)) if tests else None,
                    duty_cycle=duty,
                )
                backend = self._get_backend_for_name(workload["backend"])
            except Exception as e:
                self._fail_test_async(cores[0], str(e))
                return
        else:
            stress_config = StressConfig(
                mode=self._get_stress_mode(),
                fft_preset=self._get_fft_preset(),
                threads=2,
                memory_mb=memory_mb,
            )
        scheduler_config = SchedulerConfig(
            seconds_per_core=duration,
            cores_to_test=cores,
            stop_on_error=True,
            cycle_count=1,
            max_temperature=self._config.max_temperature_c,
            over_temp_grace_seconds=self._config.over_temp_grace_seconds,
            over_temp_hard_margin=self._config.over_temp_hard_margin_c,
            require_thermal_sensor=not self._config.allow_missing_thermal_sensor,
            duty_cycle=stress_config.duty_cycle,
        )

        try:
            runner = ParallelStress(
                topology=self._topology,
                backend=backend or self._backend,
                stress_config=stress_config,
                scheduler_config=scheduler_config,
                work_dir=self._work_dir,
            )
        except Exception as e:
            self._fail_test_async(cores[0], str(e))
            return

        core_info = self._topology.cores.get(cores[0])
        logical_cpu = core_info.logical_cpus[0] if core_info and core_info.logical_cpus else cores[0]
        worker = _ParallelWorker(cores[0], logical_cpu, runner, parent=self)
        cs = self._core_states[cores[0]]
        snapshot = (
            dict(workload)
            if workload is not None
            else self._workload_snapshot(
                cs,
                backend=getattr(backend or self._backend, "name", self._config.backend),
                stress_mode=str(stress_config.mode),
                fft_preset=str(stress_config.fft_preset),
                threads=stress_config.threads,
                profile=self._worker_profile,
            )
        )
        snapshot["duration_seconds"] = duration
        snapshot["kind"] = "parallel"
        self._launch_worker(worker, snapshot, cores)

    def _mark_cores_under_stress(self, cores: list[int]) -> None:
        """Flag every core a validation worker is about to stress as in_test and
        persist it BEFORE the worker starts.

        A hard crash during validation completes no test, and the confirmed offsets
        validation re-applies are journaled ``survived`` — so neither resume crash
        detector would see it and the quarantine breaker would never engage,
        re-applying the same profile into the same crash forever. The in_test flag
        is the one signal that attributes such a crash, so set it on the full
        stressed set (a multi-core stage reports only its first core).
        """
        self._cores_under_stress = list(cores)
        marked = False
        for core_id in cores:
            cs = self._core_states.get(core_id)
            if cs is None:
                continue
            cs.in_test = True
            if self._session_id is not None:
                self._db.upsert_tuner_core_state(self._session_id, cs)
                marked = True
        if marked:
            # The mark is the ONE signal that attributes a hard crash during
            # validation; a WAL commit alone can be lost to a freeze (the CO
            # journal's checkpoint runs BEFORE this write, so everything up to
            # it survives while the mark evaporates — observed live). Force it
            # to disk before any stress starts.
            self._db.checkpoint()

    def _clear_cores_under_stress(self) -> None:
        """Clear and persist in_test for every core marked under validation stress.

        Reaching a test result (or an abort) proves the box survived, so the whole
        stressed set must be cleared — not just the reported core (_on_test_finished
        clears that one) — or a normal completion leaves a stale in_test that would
        wrongly fire the breaker on a later resume.
        """
        for core_id in self._cores_under_stress:
            cs = self._core_states.get(core_id)
            if cs is None:
                continue
            # Persist unconditionally: _on_test_finished clears the reported core's
            # flag in memory before calling this, so a guard on cs.in_test would skip
            # persisting that core and leave its DB row in_test=True.
            cs.in_test = False
            if self._session_id is not None:
                self._db.upsert_tuner_core_state(self._session_id, cs)
        self._cores_under_stress = []

    def _find_most_aggressive_core(self) -> int | None:
        """Find the confirmed core with the highest absolute offset that can be backed off.

        Skips cores already at their baseline_offset (nothing to give).
        """
        best_core = None
        best_abs = -1
        for cs in self._core_states.values():
            if cs.best_offset is not None and cs.best_offset != cs.baseline_offset and abs(cs.best_offset) > best_abs:
                best_abs = abs(cs.best_offset)
                best_core = cs.core_id
        return best_core

    def _backoff_core(self, core_id: int) -> bool:
        """Back off a core's best_offset by one fine_step.

        Returns False if the offset is already at baseline (can't back off further).
        """
        cs = self._core_states[core_id]
        cfg = self._config
        if cs.best_offset is None:
            return False
        if cs.best_offset == cs.baseline_offset:
            return False  # already at baseline — nothing to back off

        old_offset = cs.best_offset
        new_offset = cs.best_offset - cfg.direction * cfg.fine_step
        # Clamp to baseline if we've backed off past it
        if self._at_or_past_baseline(new_offset, cs):
            cs.best_offset = cs.baseline_offset
            cs.current_offset = cs.baseline_offset
        else:
            cs.best_offset = new_offset
            cs.current_offset = new_offset

        if self._session_id:
            self._db.upsert_tuner_core_state(self._session_id, cs)

        self.log_message.emit(f"Backed off core {core_id}: offset {cs.best_offset} (was {old_offset})")
        self.core_state_changed.emit(cs.core_id, cs.phase, cs.current_offset)
        return True

    def _on_validation_test_finished(self, core_id: int, passed: bool, duration: float = 0.0) -> None:
        """Handle test result during multi-core validation stages.

        A back-off costs one solo re-test plus a rerun of the failed stage —
        never a full restart: raising one core's voltage cannot destabilize
        the others, so their existing coverage stays valid. The dirty flag
        remembers that back-offs happened; DONE still requires one final
        complete pass with zero back-offs (see the finalize sentinel).
        """
        if self._in_requeue:
            if passed:
                self._validation_thermal_aborts = 0
                if core_id in self._validation_requeue:
                    self._validation_requeue.remove(core_id)
                self._save_validation_pos()
                QTimer.singleShot(0, self._run_validation_requeue)
                return
            self._validation_dirty = True
            if not self._backoff_core(core_id):
                self._finalize_exhausted()
                return
            self._save_validation_pos()
            self.log_message.emit(
                f"Validation re-test: core {core_id} failed again — backed off one more step, retrying its solo slot"
            )
            QTimer.singleShot(0, self._run_validation_requeue)
            return

        if passed:
            self._validation_thermal_aborts = 0  # streak broken by a clean pass
            match self._validation_stage:
                case 1:
                    self._validation_core_index += 1
                case 2:
                    # Stage 2 passed — advance to stage 3
                    self._validation_stage = 3
                    self._validation_half_index = 0
                    self.log_message.emit("Validation stage 2 passed — stage 3: alternating half-core load")
                case 3:
                    self._validation_half_index += 1
                case 4:
                    self._hunt_workload = None
                    self._validation_stage = 5
                    self._validation_core_index = 0
                case 5:
                    self._validation_core_index += 1
                case 6:
                    # Memory stage passed — advance to soak (stage 7)
                    self._validation_stage = 7
                case 9:
                    self._endurance_index += 1
            self._save_validation_pos()
            self._save_endurance_pos()
            # Use QTimer to break the call stack (this is called from _on_test_finished)
            QTimer.singleShot(0, self._run_validation_next)
            return

        if self._validation_stage == 4:
            self._validation_dirty = True
            self._save_validation_pos()
            self._pending_hunt_vector = self.live_vector()
            self._pending_hunt_loaded = list(self._validation_core_order)
            self.log_message.emit(
                "Rapid-transition validation failed without naming a core; starting an attribution hunt."
            )
            self._start_hunt(observed_mttf=duration, loaded=self._pending_hunt_loaded)
            return

        target: int | None = None
        match self._validation_stage:
            case 1 | 2 | 3 | 5 | 6 | 9:
                target = core_id

        if target is None or not self._backoff_core(target):
            self._finalize_exhausted()
            return

        self._validation_dirty = True
        endurance_solo = self._validation_stage == 9 and self._endurance_index < len(self._validation_core_order)
        if self._validation_stage in (1, 5) or endurance_solo:
            # The failed slot simply retries at the new offset — the cursor
            # has not advanced, and nobody else's coverage changed.
            if endurance_solo:
                wl = self._config.endurance_workloads[self._endurance_workload]
                label = tp.workload_label(
                    wl["backend"], wl["stress_mode"], wl["fft_preset"], wl.get("threads"), wl.get("profile")
                )
                self.log_message.emit(
                    f"Endurance: core {target} backed off to {self._core_states[target].best_offset} "
                    f"after {label} - retrying its slot"
                )
            else:
                self.log_message.emit(
                    f"Validation stage {self._validation_stage}: core {target} backed "
                    f"off — retrying its slot (position kept)"
                )
            self._save_validation_pos()
            QTimer.singleShot(0, self._run_validation_next)
            return

        self._validation_requeue = [target]
        self.log_message.emit(
            f"Validation stage {self._validation_stage} failed — core {target} "
            f"backed off; solo re-test, then stage {self._validation_stage} reruns"
        )
        self._save_validation_pos()
        QTimer.singleShot(0, self._run_validation_requeue)

    def _validation_stage_exit_to_search(self) -> None:
        """Leave validation so demoted cores re-earn; the cursor stays put."""
        self._validation_stage = 0
        self._transition_status("running")
        QTimer.singleShot(0, self._run_next)

    def _finalize_exhausted(self) -> None:
        """Validation failed with nothing left to back off — fail closed.

        Completing here would stamp "confirmed" on a profile whose validation
        just FAILED. Reaching this point means real failures persisted all the
        way down to baseline values, which indicts the baseline itself or the
        environment — either way not a tuner verdict to paper over.
        """
        self.log_message.emit(
            "Validation failed and no core can be backed off further — the "
            "profile cannot be proven. Reverting all cores to baseline and "
            "pausing: failures reached baseline values, so either the baseline "
            "itself is unstable or something else on this machine interfered. "
            "Investigate, then Resume to retry validation."
        )
        self._revert_all_to_baseline()
        self._save_validation_pos()
        self.pause()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_cores_to_test(self) -> list[int]:
        if self._config.cores_to_test is not None:
            return sorted(self._config.cores_to_test)
        return sorted(self._topology.cores.keys())

    def _persist_narrative(self, message: str) -> None:
        """A narrative write must never take the engine down with it."""
        if self._session_id is None:
            return
        try:
            self._db.insert_tuner_event(self._session_id, message, self._boot_id)
        except Exception:
            log.debug("narrative write failed", exc_info=True)

    def _set_status(self, status: str) -> None:
        self._status = status
        if status in DORMANT_STATUSES and not self.test_in_flight:
            self._sleep.release()
        else:
            self._sleep.hold()
        self.status_changed.emit(status)

    def _transition_status(self, status: str) -> None:
        if self._session_id is not None:
            self._db.update_tuner_session_status(self._session_id, status)
        self._set_status(status)

    def _emit_progress(self) -> None:
        done = sum(1 for cs in self._core_states.values() if cs.phase in (TunerPhase.CONFIRMED,))
        total = len(self._core_states)
        self.progress_updated.emit(done, total)

    def _write_co_verified(self, core_id: int, offset: int) -> bool:
        if self._smu is None:
            return False
        if getattr(self._smu, "dry_run", False) is True:
            raise RuntimeError("Disable SMU Dry Run before tuning: simulated writes cannot prove offsets")
        low, high = self._smu.commands.co_range
        if not low <= offset <= high:
            self.log_message.emit(f"CO offset {offset} for core {core_id} is outside the hardware range {low}..{high}")
            return False
        survived = not self._is_more_aggressive(offset, self._co_survived.get(core_id, 0))
        if self._session_id is not None:
            tp.journal_co_intent(self._db, self._session_id, core_id, offset, survived)
        log.debug("CO write: core=%d value=%d survived=%s", core_id, offset, survived)
        for attempt in range(1, 4):
            try:
                accepted = self._smu.set_co_offset(core_id, offset)
                if accepted and self._smu.get_co_offset(core_id) == offset:
                    self._co_applied[core_id] = offset
                    return True
            except Exception:
                log.warning("CO write attempt %d failed for core %d at %d", attempt, core_id, offset, exc_info=True)
        return False

    def _restore_stock_verified(self) -> set[int]:
        failed: set[int] = set()
        for core_id in sorted(self._topology.cores):
            if not self._write_co_verified(core_id, 0):
                failed.add(core_id)
        return failed

    def _recover_co_write_failure(self, reason: str) -> bool:
        failed = self._restore_stock_verified()
        if failed:
            self._quarantine_session(0, reason=reason, stock_failures=failed)
        else:
            self.pause()
        return False

    def _apply_validation_offsets(self, test_core_id: int, test_offset: int) -> bool:
        """Apply ALL confirmed offsets during validation — testing interactions.

        Unlike isolation mode, non-tested cores keep their confirmed (best)
        offsets instead of reverting to baseline. This catches power delivery
        issues that only appear when multiple cores run aggressive offsets.

        On failure, reverts all cores to baseline to leave SMU in a known
        state, then pauses the tuner.
        """
        for core_id, cs in self._core_states.items():
            if core_id == test_core_id:
                continue
            # Use best_offset (confirmed value) if available, else baseline
            target = cs.best_offset if cs.best_offset is not None else cs.baseline_offset
            if self._co_applied.get(core_id) == target:
                continue
            try:
                success = self._write_co_verified(core_id, target)
            except Exception as e:
                self.log_message.emit(
                    f"Failed to apply validated offset for core {core_id}: {e}. Reverting to baselines and pausing."
                )
                return self._recover_co_write_failure(f"validated offset write failed for core {core_id}")
            if not success:
                self.log_message.emit(
                    f"Failed to apply validated offset for core {core_id}: read-back mismatch. "
                    f"Reverting to baselines and pausing."
                )
                return self._recover_co_write_failure(f"validated offset readback failed for core {core_id}")
            self._co_applied[core_id] = target

        # Apply test offset to target core
        try:
            success = self._write_co_verified(test_core_id, test_offset)
        except Exception:
            log.warning("Failed to apply test offset for core %d at %d", test_core_id, test_offset, exc_info=True)
            return self._recover_co_write_failure(f"test offset write failed for core {test_core_id}")
        if not success:
            self.log_message.emit(
                f"CO write failed for core {test_core_id} at {test_offset}. Restoring stock and pausing."
            )
            return self._recover_co_write_failure(f"test offset readback failed for core {test_core_id}")
        self._co_applied[test_core_id] = test_offset
        return True

    def _mask_offset(self, cs: CoreState, mask: Mask) -> int:
        """What a non-tested core sits at under the given mask.

        LIVE uses the core's best KNOWN-good offset, never its in-flight trial
        value: the trial is a hypothesis, and putting an unproven offset on a
        core we are not testing would make every other core's verdict a lie.
        """
        if mask is Mask.ISOLATED or cs.best_offset is None:
            return cs.baseline_offset
        return cs.best_offset

    def live_vector(self) -> dict[int, int]:
        """The offset vector the machine would actually run with right now."""
        return {cid: self._mask_offset(cs, Mask.LIVE) for cid, cs in self._core_states.items()}

    def _apply_co_mask(self, test_core_id: int, test_offset: int, mask: Mask) -> bool:
        """Apply the whole CO vector for one slot: the tested core at its trial
        offset, every other core at whatever ``mask`` says.

        ISOLATED parks the rest at stock, which makes a failure attributable to
        the core under test but proves nothing about a machine that never runs
        that way. LIVE leaves the rest at their best-known offsets, which is the
        only condition worth banking confidence from, and is the condition under
        which an idle core's own margin can take the machine down.

        Returns True if all SMU writes succeeded, False if any failed. On
        failure the caller must stop: the test never ran, so recording a
        "failure" at this offset would corrupt the search.
        """
        for core_id, cs in self._core_states.items():
            if core_id == test_core_id:
                continue
            target = self._mask_offset(cs, mask)
            if self._co_applied.get(core_id) == target:
                continue
            try:
                success = self._write_co_verified(core_id, target)
            except Exception as e:
                self.log_message.emit(
                    f"CO mask failed: core {core_id} could not be set to {target} - {e}. "
                    f"Stopping (SMU issue, not a core stability failure)."
                )
                return self._recover_co_write_failure(f"CO mask write failed for core {core_id}")
            if not success:
                self.log_message.emit(
                    f"CO mask failed: core {core_id} write to {target} did not read back. "
                    f"Stopping (SMU issue, not a core stability failure)."
                )
                return self._recover_co_write_failure(f"CO mask readback failed for core {core_id}")
            self._co_applied[core_id] = target

        try:
            success = self._write_co_verified(test_core_id, test_offset)
        except Exception as e:
            self.log_message.emit(f"Failed to set CO for core {test_core_id}: {e}. Stopping.")
            return self._recover_co_write_failure(f"test core write failed for core {test_core_id}")
        if not success:
            self.log_message.emit(
                f"CO write failed or read-back mismatch for core {test_core_id} "
                f"at offset {test_offset} - SMU did not apply the value. Stopping."
            )
            return self._recover_co_write_failure(f"test core readback failed for core {test_core_id}")
        self._co_applied[test_core_id] = test_offset
        return True

    def _revert_core_to_baseline(self, core_id: int) -> bool:
        """Revert a single core to its baseline offset after a test.

        Returns False when the SMU write failed: the tested (aggressive)
        offset is then still RESIDENT, so the caller must stop the flow —
        silently marching on with poisoned hardware state is how a bad
        session gets written.
        """
        if self._smu is None:
            return True
        cs = self._core_states.get(core_id)
        if cs is None:
            return True
        if self._co_applied.get(core_id) == cs.baseline_offset:
            return True  # already at baseline
        try:
            success = self._write_co_verified(core_id, cs.baseline_offset)
        except Exception as e:
            self.log_message.emit(f"Post-test baseline revert error for core {core_id}: {e}")
            return False
        if success:
            self._co_applied[core_id] = cs.baseline_offset
            return True
        self.log_message.emit(
            f"Post-test baseline revert failed for core {core_id} (offset {cs.baseline_offset}) — read-back mismatch"
        )
        return False

    def _revert_all_to_baseline(self, *, force: bool = False) -> set[int]:
        if self._smu is None:
            return set()
        failed: set[int] = set()
        for core_id, cs in self._core_states.items():
            if not force and self._co_applied.get(core_id) == cs.baseline_offset:
                continue
            if not self._write_co_verified(core_id, cs.baseline_offset):
                failed.add(core_id)
        if failed:
            stock_failed = self._restore_stock_verified()
            self._quarantine_session(
                0,
                reason=f"Baseline restoration failed for cores {sorted(failed)}",
                stock_failures=stock_failed,
            )
        return failed

    def _get_stress_mode(self):
        from corecycler.engine.backends.base import StressMode

        try:
            return StressMode[self._config.stress_mode.upper()]
        except KeyError:
            return StressMode.SSE

    def _get_fft_preset(self):
        from corecycler.engine.backends.base import FFTPreset

        try:
            return FFTPreset[self._config.fft_preset.upper()]
        except KeyError:
            return FFTPreset.SMALL
