"""Main application window: tabs, toolbar, and test control."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from PySide6.QtCore import QThread, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QFont
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStatusBar,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from corecycler.config.paths import ensure_work_dir, user_home
from corecycler.config.settings import AppSettings, load_settings, save_settings
from corecycler.engine.backends import get_backend, load_all
from corecycler.engine.backends.base import StressConfig, StressResult
from corecycler.engine.scheduler import CoreScheduler, CoreTestStatus, SchedulerConfig
from corecycler.engine.topology import CPUTopology, detect_topology
from corecycler.gui.config_tab import ConfigTab
from corecycler.gui.history_tab import HistoryTab
from corecycler.gui.memory_tab import MemoryTab
from corecycler.gui.monitor_tab import MonitorTab
from corecycler.gui.results_tab import ResultsTab
from corecycler.gui.smu_tab import SMUTab
from corecycler.gui.style import format_temperature, set_semantic_style, theme
from corecycler.gui.tool_prompt import ensure_tool
from corecycler.gui.tuner_tab import TunerTab
from corecycler.gui.widgets.core_grid import CoreGridWidget
from corecycler.history.context import detect_bios_change
from corecycler.history.db import HistoryDB, adopt_legacy_root_db
from corecycler.history.logger import TestRunLogger
from corecycler.monitor.frequency import read_core_frequencies
from corecycler.monitor.hwmon import HWMonReader
from corecycler.monitor.msr import MSRReader

log = logging.getLogger(__name__)


class _WorkloadOwner(StrEnum):
    NONE = "none"
    MANUAL = "manual"
    MEMORY = "memory"
    TUNER = "tuner"


@dataclass(frozen=True, slots=True)
class _HistoryStartup:
    db: HistoryDB | None = None
    bios_changed: bool = False
    bios_old: str = ""
    bios_current: str = ""


def _start_history(settings: AppSettings) -> _HistoryStartup:
    if not settings.record_history:
        return _HistoryStartup()
    try:
        db = HistoryDB()
        try:
            adopted = adopt_legacy_root_db(db)
            if adopted:
                log.info("Adopted legacy root history: %s", adopted)
        except Exception:
            log.exception("Legacy root history adoption failed - continuing")
        recovered = db.recover_incomplete_runs()
        for run_id, started_at in recovered:
            log.info("Recovered stale session id=%d started_at=%s, marked as crashed", run_id, started_at)
        if settings.history_retention_days > 0:
            cutoff = datetime.now(UTC) - timedelta(days=settings.history_retention_days)
            db.purge_before(cutoff.isoformat())
        try:
            changed, old, current = detect_bios_change(db)
        except Exception:
            log.exception("Failed to detect BIOS change")
            return _HistoryStartup(db=db)
        return _HistoryStartup(db=db, bios_changed=changed, bios_old=old, bios_current=current)
    except Exception:
        log.exception("Failed to initialize history database")
        return _HistoryStartup()


class TestWorker(QThread):
    """Worker thread that runs the core scheduler."""

    core_started = Signal(int, int)  # core_id, cycle
    core_finished = Signal(int, object)  # core_id, StressResult
    status_updated = Signal(int, object)  # core_id, CoreTestStatus
    cycle_completed = Signal(int)
    test_completed = Signal(str)  # JSON-encoded results avoid PySide6 dict marshalling crash
    thermal_throttled = Signal(float)
    stall_detected = Signal(int)
    phase_changed = Signal(int, str)
    crashed = Signal(str)

    def __init__(self, scheduler: CoreScheduler) -> None:
        super().__init__()
        self.scheduler = scheduler

        # wire callbacks
        self.scheduler.on_core_start = [lambda cid, cyc: self.core_started.emit(cid, cyc)]
        self.scheduler.on_core_finish = [lambda cid, res: self.core_finished.emit(cid, res)]
        self.scheduler.on_status_update = [lambda cid, st: self.status_updated.emit(cid, st)]
        self.scheduler.on_cycle_complete = [lambda cyc: self.cycle_completed.emit(cyc)]
        self.scheduler.on_test_complete = [
            lambda res: self.test_completed.emit(json.dumps({str(k): [asdict(r) for r in v] for k, v in res.items()}))
        ]
        self.scheduler.on_thermal_throttle = [self.thermal_throttled.emit]
        self.scheduler.on_stall_detected = [self.stall_detected.emit]
        self.scheduler.on_phase_change = [self.phase_changed.emit]

    def run(self) -> None:
        try:
            self.scheduler.run()
        except Exception as exc:
            log.exception("Test worker crashed")
            self.crashed.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("CoreCycler")
        self.setMinimumSize(1000, 700)

        load_all()

        self._settings = load_settings()
        self._topology: CPUTopology | None = None
        self._worker: TestWorker | None = None
        self._closing = False
        self._test_start_time: float = 0
        self._hwmon = HWMonReader()
        self._msr = MSRReader()
        self._core_telemetry: dict[int, dict] = {}
        self._core_status_cache: dict[int, CoreTestStatus] = {}
        self._cached_cycle: int = 0
        self._active_test_core: int | None = None
        self._logger: TestRunLogger | None = None
        self._worker_crash: str | None = None
        self._workload_owner = _WorkloadOwner.NONE

        history = _start_history(self._settings)
        self._history_db = history.db
        self._bios_changed = history.bios_changed
        self._bios_old = history.bios_old
        self._bios_current = history.bios_current

        self._detect_cpu()
        self._setup_ui()
        self._setup_toolbar()
        self._setup_status_bar()
        self._setup_timer()
        self._refresh_workload_owner()

        self.resize(self._settings.window_width, self._settings.window_height)

    def _detect_cpu(self) -> None:
        self._topology = detect_topology()

    def _setup_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)

        # left: core grid
        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)

        cpu_label = QLabel(self._topology.model_name if self._topology else "Unknown CPU")
        cpu_label.setFont(QFont("monospace", 11, QFont.Weight.Bold))
        cpu_label.setStyleSheet("padding: 4px 6px;")
        cpu_label.setMaximumWidth(200)
        cpu_label.setWordWrap(True)
        left.addWidget(cpu_label)

        if self._topology:
            info_parts = [f"{self._topology.physical_cores}C/{self._topology.logical_cpus_count}T"]
            if self._topology.ccds > 1:
                info_parts.append(f"{self._topology.ccds} CCDs")
            if self._topology.is_x3d:
                info_parts.append("X3D V-Cache")
            if self._topology.smt_enabled:
                info_parts.append("SMT")
            info_label = QLabel(" | ".join(info_parts))
            set_semantic_style(info_label, lambda: f"color: {theme.COLOR_TEXT_DIM}; padding: 0 6px")
            left.addWidget(info_label)

        self._core_grid = CoreGridWidget(self._topology)
        self._core_grid.setMaximumWidth(200)
        left.addWidget(self._core_grid)

        main_layout.addLayout(left, stretch=0)

        # right: tabs, aligned with the CPU header on the left
        self._tabs = QTabWidget()
        self._tabs.setContentsMargins(0, 0, 0, 0)
        self._tabs.setDocumentMode(True)

        self._config_tab = ConfigTab(self._topology)
        self._config_tab.set_profile(self._settings.active_profile)
        self._tabs.addTab(self._config_tab, "Configuration")

        self._results_tab = ResultsTab()
        self._tabs.addTab(self._results_tab, "Results")

        self._monitor_tab = MonitorTab(topology=self._topology)
        self._tabs.addTab(self._monitor_tab, "Monitor")

        self._smu_tab = SMUTab(self._topology)
        self._tabs.addTab(self._smu_tab, "Curve Optimizer")

        smu = self._smu_tab.smu if hasattr(self._smu_tab, "smu") else None
        self._tuner_tab = TunerTab(self._history_db, self._topology, smu)
        self._tuner_tab.tuner_running_changed.connect(self._on_tuner_running_changed)
        self._tuner_tab.tuner_core_testing.connect(self._on_tuner_core_update)
        self._tuner_tab.tuner_core_elapsed.connect(self._on_tuner_core_elapsed)
        self._tuner_tab.tuner_core_info.connect(self._on_tuner_core_info)
        self._tabs.addTab(self._tuner_tab, "Auto-Tuner")

        self._history_tab = HistoryTab(self._history_db)
        if self._bios_changed:
            self._history_tab.set_bios_warning(self._bios_old, self._bios_current)
        self._history_tab.load_profile_requested.connect(self._on_load_co_profile)
        self._tabs.addTab(self._history_tab, "History")

        self._memory_tab = MemoryTab()
        self._memory_tab.memory_stress_started.connect(self._on_memory_stress_started)
        self._memory_tab.memory_stress_done.connect(self._on_memory_stress_done)
        self._tabs.addTab(self._memory_tab, "Memory")

        self._tabs.currentChanged.connect(self._on_tab_changed)
        main_layout.addWidget(self._tabs, stretch=2)

    def _setup_toolbar(self) -> None:
        toolbar = QToolBar("Test Control")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        self._start_btn = QPushButton("▶ Start Test")
        self._start_btn.setFixedHeight(36)
        set_semantic_style(
            self._start_btn,
            lambda: (
                f"QPushButton {{ background: {theme.BTN_GREEN}; color: white; padding: 0 16px; "
                "border-radius: 4px; font-weight: bold; font-size: 13px; } "
                f"QPushButton:hover {{ background: {theme.COLOR_PASS_DARK}; }} "
                f"QPushButton:disabled {{ background: {theme.BORDER_DIM}; color: {theme.COLOR_MUTED}; }}"
            ),
        )
        self._start_btn.clicked.connect(self._start_test)
        toolbar.addWidget(self._start_btn)

        self._stop_btn = QPushButton("⏹ Stop")
        self._stop_btn.setFixedHeight(36)
        self._stop_btn.setEnabled(False)
        set_semantic_style(
            self._stop_btn,
            lambda: (
                f"QPushButton {{ background: {theme.BTN_RED}; color: white; padding: 0 16px; "
                "border-radius: 4px; font-weight: bold; font-size: 13px; } "
                f"QPushButton:hover {{ background: {theme.COLOR_FAIL_DARK}; }} "
                f"QPushButton:disabled {{ background: {theme.BORDER_DIM}; color: {theme.COLOR_MUTED}; }}"
            ),
        )
        self._stop_btn.clicked.connect(self._stop_test)
        toolbar.addWidget(self._stop_btn)

        toolbar.addSeparator()

        # profile management
        save_action = QAction("Save Profile", self)
        save_action.triggered.connect(self._save_profile)
        toolbar.addAction(save_action)

        load_action = QAction("Load Profile", self)
        load_action.triggered.connect(self._load_profile)
        toolbar.addAction(load_action)

    def _setup_status_bar(self) -> None:
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_msg = QLabel("Ready")
        self._status_bar.addWidget(self._status_msg)

        missing: list[str] = []
        msr_missing = False
        try:
            fd = os.open("/dev/cpu/0/msr", os.O_RDONLY)
            os.close(fd)
        except OSError:
            msr_missing = True
            missing.append("MSR (clock stretch, per-core power, package power)")
        if not Path("/sys/kernel/ryzen_smu_drv/smu_args").exists() or not os.access(
            "/sys/kernel/ryzen_smu_drv/smu_args", os.W_OK
        ):
            missing.append("Curve Optimizer (SMU)")
        if missing:
            priv_label = QLabel(
                "  ⚠ " + " and ".join(missing) + " unavailable - check device permissions or run as root"
            )
            if msr_missing:
                priv_label.setToolTip(
                    "Opening /dev/cpu/N/msr needs CAP_SYS_RAWIO, which no file mode or group can "
                    "grant. Launch through the setcap launcher (services.corecycler.msrAccess on "
                    "NixOS installs it at /run/wrappers/bin/corecycler) or run as root."
                )
            set_semantic_style(priv_label, lambda: f"color: {theme.COLOR_WARN_SOFT}; font: 10px monospace")
            self._status_bar.addPermanentWidget(priv_label)

    def _setup_timer(self) -> None:
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.timeout.connect(self._update_elapsed)

    def _active_workload_owner(self) -> _WorkloadOwner:
        if self._workload_owner is not _WorkloadOwner.NONE:
            return self._workload_owner
        if self._worker and self._worker.isRunning():
            return _WorkloadOwner.MANUAL
        engine = getattr(self._tuner_tab, "_engine", None)
        if self._tuner_tab.is_running or (engine and engine.status in {"running", "paused", "validating", "hunting"}):
            return _WorkloadOwner.TUNER
        if self._history_db and self._history_db.get_active_tuner_session() is not None:
            return _WorkloadOwner.TUNER
        memory_worker = getattr(self._memory_tab, "_stress_worker", None)
        if memory_worker and memory_worker.isRunning():
            return _WorkloadOwner.MEMORY
        return _WorkloadOwner.NONE

    def _workload_is_owned(self) -> bool:
        return self._active_workload_owner() is not _WorkloadOwner.NONE

    def _refresh_workload_owner(self) -> None:
        self._workload_owner = self._active_workload_owner()
        self._apply_workload_owner()

    def _set_workload_owner(self, owner: _WorkloadOwner) -> None:
        self._workload_owner = owner
        self._apply_workload_owner()

    def _apply_workload_owner(self) -> None:
        owner = self._active_workload_owner()
        self._start_btn.setEnabled(owner is _WorkloadOwner.NONE)
        self._stop_btn.setEnabled(owner is _WorkloadOwner.MANUAL)
        self._tuner_tab.set_test_running(owner in {_WorkloadOwner.MANUAL, _WorkloadOwner.MEMORY})
        self._memory_tab.set_test_running(owner in {_WorkloadOwner.MANUAL, _WorkloadOwner.TUNER})
        self._smu_tab.set_tuner_running(owner is _WorkloadOwner.TUNER)

    def _start_test(self) -> None:
        if not self._topology:
            QMessageBox.warning(self, "Error", "CPU topology not detected")
            return

        owner = self._active_workload_owner()
        if owner is not _WorkloadOwner.NONE:
            QMessageBox.warning(
                self,
                "Workload Active",
                f"The {owner.value} workload owns the stress hardware. Stop or abort it first.",
            )
            return

        try:
            profile = self._config_tab.get_profile()
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid profile", str(exc))
            return

        # select backend
        backend = self._get_backend(profile.backend)
        if not backend:
            return

        if not backend.is_available() and not ensure_tool(self, profile.backend):
            return

        stress_config = StressConfig(
            mode=profile.get_stress_mode(),
            fft_preset=profile.get_fft_preset(),
            fft_min=profile.fft_min,
            fft_max=profile.fft_max,
            threads=profile.threads,
        )

        scheduler_config = SchedulerConfig(
            seconds_per_core=profile.seconds_per_core,
            cores_to_test=profile.cores_to_test,
            stop_on_error=profile.stop_on_error,
            cycle_count=profile.cycle_count,
            max_temperature=profile.max_temperature,
            variable_load=profile.variable_load,
            idle_stability_test=profile.idle_stability_test,
            idle_between_cores=profile.idle_between_cores,
            require_thermal_sensor=True,
        )

        try:
            work_dir = ensure_work_dir(self._settings.work_dir)
        except OSError as e:
            QMessageBox.warning(
                self,
                "Work directory unavailable",
                f"Could not create the stress work directory: {e}",
            )
            return
        try:
            scheduler = CoreScheduler(
                topology=self._topology,
                backend=backend,
                stress_config=stress_config,
                scheduler_config=scheduler_config,
                work_dir=work_dir,
            )
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to initialize scheduler: {e}")
            return

        # init results tab
        self._results_tab.init_cores(scheduler.core_status)

        # create worker
        self._core_status_cache.clear()
        self._cached_cycle = 0
        self._active_test_core = None
        self._worker_crash = None
        self._worker = TestWorker(scheduler)
        self._worker.core_started.connect(self._on_core_started)
        self._worker.core_finished.connect(self._on_core_finished)
        self._worker.status_updated.connect(self._on_status_updated)
        self._worker.status_updated.connect(self._on_status_cached)
        self._worker.cycle_completed.connect(self._on_cycle_completed)
        self._worker.cycle_completed.connect(self._on_cycle_cached)
        self._worker.test_completed.connect(self._on_test_completed)
        self._worker.crashed.connect(self._on_worker_crashed)
        self._worker.finished.connect(self._on_worker_finished)

        # History logger
        self._logger = None
        if self._settings.record_history and self._history_db and self._topology:
            try:
                smu = self._smu_tab.smu if hasattr(self, "_smu_tab") else None
                self._logger = TestRunLogger(self._history_db, self._topology, profile, smu=smu)
                self._worker.core_started.connect(self._logger.on_core_started)
                self._worker.core_finished.connect(self._logger.on_core_finished)
                self._worker.status_updated.connect(self._logger.on_status_updated)
                self._worker.cycle_completed.connect(self._logger.on_cycle_completed)
                self._worker.test_completed.connect(self._logger.on_test_completed)
                self._worker.thermal_throttled.connect(self._logger.record_thermal_event)
                self._worker.stall_detected.connect(self._logger.record_stall_event)
                self._worker.phase_changed.connect(self._logger.record_phase_change)
            except Exception:
                log.exception("Failed to create history logger")
                self._logger = None

        self._set_workload_owner(_WorkloadOwner.MANUAL)
        self._test_start_time = time.monotonic()
        self._elapsed_timer.start(1000)
        self._tabs.setCurrentWidget(self._results_tab)
        self._status_msg.setText("Testing...")

        self._worker.start()

    def _stop_test(self) -> None:
        if not self._worker:
            return
        self._stop_btn.setEnabled(False)
        self._status_msg.setText("Stopping...")

        # Disconnect logger from worker signals before stopping. This prevents
        # half-torn-down logger from receiving queued signals during shutdown
        if self._logger and self._worker:
            with contextlib.suppress(RuntimeError):
                self._worker.core_started.disconnect(self._logger.on_core_started)
                self._worker.core_finished.disconnect(self._logger.on_core_finished)
                self._worker.status_updated.disconnect(self._logger.on_status_updated)
                self._worker.cycle_completed.disconnect(self._logger.on_cycle_completed)
                self._worker.test_completed.disconnect(self._logger.on_test_completed)

        # Disconnect thread-safety cache signals
        if self._worker:
            with contextlib.suppress(RuntimeError):
                self._worker.status_updated.disconnect(self._on_status_cached)
                self._worker.cycle_completed.disconnect(self._on_cycle_cached)

        if self._logger:
            # Save any accumulated peak telemetry before stopping
            for core_id, t in self._core_telemetry.items():
                if t["max_freq"] > 0:
                    with contextlib.suppress(Exception):
                        self._logger.update_core_telemetry_peaks(
                            core_id,
                            peak_freq_mhz=t["max_freq"],
                            max_temp_c=t["max_temp"],
                            min_vcore_v=t["min_vcore"],
                            max_vcore_v=t["max_vcore"],
                        )
            try:
                self._logger.on_test_stopped()
            except Exception:
                log.exception("Failed to record test stop in history")
            self._logger = None
        self._core_telemetry.clear()

        # Signal the scheduler to stop. The worker thread will finish naturally
        # and _on_worker_finished will handle UI cleanup
        self._worker.scheduler.stop()

    def _get_backend(self, name: str):
        try:
            return get_backend(name)
        except KeyError:
            QMessageBox.warning(self, "Error", f"Unknown backend: {name}")
            return None

    @Slot(int, int)
    def _on_core_started(self, core_id: int, cycle: int) -> None:
        self._status_msg.setText(f"Testing core {core_id} (cycle {cycle + 1})")
        self._monitor_tab.set_active_core(core_id)
        self._active_test_core = core_id

    @Slot(int, object)
    def _on_status_cached(self, core_id: int, status: CoreTestStatus) -> None:
        """Cache core status from the worker thread via a thread-safe signal and slot."""
        self._core_status_cache[core_id] = status

    @Slot(int)
    def _on_cycle_cached(self, cycle: int) -> None:
        """Cache the cycle number from the worker thread via a thread-safe signal and slot."""
        self._cached_cycle = cycle

    @Slot(int, object)
    def _on_core_finished(self, core_id: int, result: StressResult) -> None:
        status = self._core_status_cache.get(core_id)
        if status:
            self._core_grid.update_core_status(core_id, status)
            self._results_tab.update_core(core_id, status)

        if result and not result.passed:
            self._results_tab.add_error(core_id, result.error_message or "Unknown error")

        # log telemetry summary for this core
        t = self._core_telemetry.pop(core_id, None)
        if t and t["max_freq"] > 0:
            extra_parts = []
            if t["min_vcore"] is not None and t["max_vcore"] is not None:
                extra_parts.append(f"Vcore: {t['min_vcore']:.4f}-{t['max_vcore']:.4f}V")
            if t["max_stretch_pct"] > 0.5:
                extra_parts.append(f"Stretch: {t['max_stretch_pct']:.1f}%")
            if t.get("core_watts") is not None:
                extra_parts.append(f"Power: {t['core_watts']:.1f}W")
            extra = ("  " + "  ".join(extra_parts)) if extra_parts else ""
            state = "PASS" if (result and result.passed) else "FAIL"
            self._results_tab.add_log(
                core_id,
                f"[{state}] Peak: {t['max_freq']:.0f} MHz, Max temp: {format_temperature(t['max_temp'])}{extra}",
            )

            # Record peak telemetry in history
            if self._logger:
                try:
                    self._logger.update_core_telemetry_peaks(
                        core_id,
                        peak_freq_mhz=t["max_freq"],
                        max_temp_c=t["max_temp"],
                        min_vcore_v=t["min_vcore"],
                        max_vcore_v=t["max_vcore"],
                    )
                except Exception:
                    log.exception("Failed to record telemetry peaks")

    @Slot(int, object)
    def _on_status_updated(self, core_id: int, status: CoreTestStatus) -> None:
        self._core_grid.update_core_status(core_id, status)

    @Slot(int)
    def _on_cycle_completed(self, cycle: int) -> None:
        self._status_msg.setText(f"Cycle {cycle + 1} complete")

    @Slot(str)
    def _on_test_completed(self, results_json: str) -> None:
        # Fail closed: a display slot must never crash the GUI on a malformed or
        # unexpectedly-shaped results payload.
        try:
            results = json.loads(results_json)
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(results, dict):
            return

        def _all_passed(r_list) -> bool:
            return (
                isinstance(r_list, list)
                and bool(r_list)
                and all(isinstance(r, dict) and r.get("passed") for r in r_list)
            )

        # Keys are stringified core_ids, values are lists of result dicts.
        # A core with no verdict (stopped before its test earned one) is not
        # tested. Counting it as failed would read as a silicon problem.
        tested = {cid: r_list for cid, r_list in results.items() if isinstance(r_list, list) and r_list}
        total = len(tested)
        passed = sum(1 for r_list in tested.values() if _all_passed(r_list))
        failed = total - passed
        elapsed = time.monotonic() - self._test_start_time

        profile = self._config_tab.get_profile()
        self._results_tab.update_summary(
            total=total,
            passed=passed,
            failed=failed,
            elapsed=elapsed,
            cycle=profile.cycle_count,
            total_cycles=profile.cycle_count,
        )

        # enable "Retest Failed" button with the list of failed cores
        failed_cores = []
        for cid, r_list in results.items():
            if isinstance(r_list, list) and r_list and not _all_passed(r_list):
                try:
                    failed_cores.append(int(cid))
                except (ValueError, TypeError):
                    continue
        self._config_tab.set_failed_cores(failed_cores)

    def _on_worker_crashed(self, message: str) -> None:
        if self._closing:
            return
        self._worker_crash = message
        if self._logger:
            try:
                self._logger.on_test_crashed(message)
            except Exception:
                log.exception("Failed to record worker crash in history")
            self._logger = None
        self._status_msg.setText(f"Test worker crashed: {message}")

    def _on_worker_finished(self) -> None:
        if self._closing:
            return
        was_stopping = not self._stop_btn.isEnabled()
        crash = self._worker_crash
        self._cleanup_worker()
        self._logger = None
        self._history_tab.refresh()
        if crash is not None:
            self._status_msg.setText(f"Test worker crashed: {crash}")
        elif was_stopping:
            self._status_msg.setText("Test stopped")
        else:
            self._status_msg.setText("Test complete")

    def _cleanup_worker(self) -> None:
        self._worker = None
        self._set_workload_owner(_WorkloadOwner.NONE)
        self._monitor_tab.set_active_core(None)
        self._elapsed_timer.stop()
        self._core_status_cache.clear()
        self._cached_cycle = 0
        self._active_test_core = None

    @Slot(bool)
    def _on_tuner_running_changed(self, running: bool) -> None:
        if running:
            self._set_workload_owner(_WorkloadOwner.TUNER)
            return
        self._set_workload_owner(_WorkloadOwner.NONE)
        self._refresh_workload_owner()

    @Slot(int, str)
    def _on_tuner_core_update(self, core_id: int, state: str) -> None:
        """Update core grid when auto-tuner changes core state."""
        status = CoreTestStatus(core_id=core_id, state=state)
        self._core_grid.update_core_status(core_id, status)

    @Slot(int, float)
    def _on_tuner_core_elapsed(self, core_id: int, elapsed: float) -> None:
        """Update core grid with live elapsed time during tuner tests."""
        status = CoreTestStatus(core_id=core_id, state="testing", elapsed_seconds=elapsed)
        self._core_grid.update_core_status(core_id, status)

    @Slot(int, int, str)
    def _on_tuner_core_info(self, core_id: int, co_offset: int, phase: str) -> None:
        """Pass CO offset and tuner phase to core grid and CO tab."""
        self._core_grid.update_core_telemetry(core_id, co_offset=co_offset, tuner_phase=phase)
        self._smu_tab.update_current_co(core_id, co_offset)

    @Slot()
    def _on_memory_stress_started(self) -> None:
        self._set_workload_owner(_WorkloadOwner.MEMORY)
        for core_id in self._core_grid._cells:
            status = CoreTestStatus(core_id=core_id, state="mem_stress")
            self._core_grid.update_core_status(core_id, status)

    @Slot(bool)
    def _on_memory_stress_done(self, passed: bool) -> None:
        self._set_workload_owner(_WorkloadOwner.NONE)
        for core_id in self._core_grid._cells:
            status = CoreTestStatus(core_id=core_id, state="pending")
            self._core_grid.update_core_status(core_id, status)

    @Slot(object, str)
    def _on_load_co_profile(self, profile: dict[int, int], source_cpu_model: str) -> None:
        self._smu_tab.set_co_profile(profile, source_cpu_model)
        self._tabs.setCurrentWidget(self._smu_tab)

    @Slot(int)
    def _on_tab_changed(self, index: int) -> None:
        """Refresh data when switching tabs."""
        widget = self._tabs.widget(index)
        # Auto-refresh CO values when switching to Curve Optimizer tab (only
        # when tuner is idle; tuner sends live updates via update_current_co())
        if widget is self._smu_tab and hasattr(self._smu_tab, "_read_all_co") and not self._tuner_tab.is_running:
            self._smu_tab._read_all_co()

    def _update_elapsed(self) -> None:
        if not self._worker:
            return
        # crash watchdog: if thread object exists but is no longer running
        if not self._worker.isRunning():
            self._cleanup_worker()
            self._status_msg.setText("Test stopped (worker exited unexpectedly)")
            return
        elapsed = time.monotonic() - self._test_start_time
        cache = self._core_status_cache
        total = len(cache)
        passed = sum(1 for s in cache.values() if s.state == "passed")
        failed = sum(1 for s in cache.values() if s.state == "failed")

        profile = self._config_tab.get_profile()
        self._results_tab.update_summary(
            total=total,
            passed=passed,
            failed=failed,
            elapsed=elapsed,
            cycle=self._cached_cycle + 1,
            total_cycles=profile.cycle_count,
        )

        # feed per-core telemetry to the grid for the active core
        self._feed_core_grid_telemetry()

    def _feed_core_grid_telemetry(self) -> None:
        """Read freq/temp/voltage/stretch and push to the active core's grid cell.

        Uses ``_active_test_core`` (set by ``_on_core_started`` signal handler
        in the GUI thread) instead of reading scheduler state directly
        across threads.
        """
        current_core = self._active_test_core
        if current_core is None:
            return

        core_info = self._topology.cores.get(current_core) if self._topology else None
        if not core_info:
            return

        logical_cpu = core_info.logical_cpus[0]

        # per-core frequency
        freqs = read_core_frequencies()
        freq = freqs.get(logical_cpu, 0)

        # MSR-based clock stretch detection (APERF/MPERF ratio)
        stretch_pct: float | None = None
        if self._msr.is_available():
            stretch_readings = self._msr.read_clock_stretch([logical_cpu])
            stretch_reading = stretch_readings.get(logical_cpu)
            if stretch_reading:
                stretch_pct = stretch_reading.stretch_pct

        # Per-core power from MSR RAPL
        core_watts: float | None = None
        if self._msr.is_available():
            power_readings = self._msr.read_core_power([logical_cpu])
            power_reading = power_readings.get(logical_cpu)
            if power_reading:
                core_watts = power_reading.watts

        hwmon_data = self._hwmon.read()
        ccd = core_info.ccd if core_info.ccd is not None else 0
        temp = hwmon_data.ccd_temperatures_c.get(ccd, hwmon_data.tctl_c)
        vcore = hwmon_data.vcore_v

        self._core_grid.update_core_telemetry(
            current_core,
            freq,
            temp,
            vcore,
            stretch_pct=stretch_pct,
        )

        # Record telemetry sample in history
        if self._logger and self._settings.record_telemetry:
            with contextlib.suppress(Exception):  # don't spam logs every second
                self._logger.record_telemetry_sample(
                    current_core,
                    freq,
                    temp,
                    vcore,
                    effective_max_mhz=None,
                )

        # track peak telemetry per core for the log
        if current_core not in self._core_telemetry:
            self._core_telemetry[current_core] = {
                "max_freq": 0.0,
                "max_stretch_pct": 0.0,
                "core_watts": None,
                "max_temp": None,
                "last_vcore": None,
                "min_vcore": None,
                "max_vcore": None,
            }
        t = self._core_telemetry[current_core]
        if freq > t["max_freq"]:
            t["max_freq"] = freq
        if stretch_pct is not None and stretch_pct > t["max_stretch_pct"]:
            t["max_stretch_pct"] = stretch_pct
        if core_watts is not None:
            t["core_watts"] = core_watts
        if temp is not None and (t["max_temp"] is None or temp > t["max_temp"]):
            t["max_temp"] = temp
        if vcore is not None:
            t["last_vcore"] = vcore
            if t["min_vcore"] is None or vcore < t["min_vcore"]:
                t["min_vcore"] = vcore
            if t["max_vcore"] is None or vcore > t["max_vcore"]:
                t["max_vcore"] = vcore

    def _save_profile(self) -> None:
        from corecycler.config.settings import save_profile

        path, _ = QFileDialog.getSaveFileName(self, "Save Profile", str(user_home()), "JSON (*.json)")
        if path:
            try:
                profile = self._config_tab.get_profile()
                save_profile(profile, Path(path))
            except Exception as e:
                QMessageBox.warning(self, "Error", f"Failed to save profile: {e}")

    def _load_profile(self) -> None:
        from corecycler.config.settings import load_profile

        path, _ = QFileDialog.getOpenFileName(self, "Load Profile", str(user_home()), "JSON (*.json)")
        if path:
            try:
                profile = load_profile(Path(path))
                self._config_tab.set_profile(profile)
            except Exception as e:
                QMessageBox.warning(self, "Error", f"Failed to load profile: {e}")

    def attempt_auto_resume(self) -> None:
        """Login-autostart entry: resume the active mid-run session, if any.

        Paused sessions are a deliberate human choice and stay paused;
        quarantined/completed ones never qualify.
        """
        from corecycler.tuner import persistence as tp

        if self._history_db is None:
            log.info("auto-resume: no database available")
            return
        session = tp.pick_auto_resume_session(self._history_db)
        if session is None:
            log.info("auto-resume: no mid-run session to resume")
            return
        engine = getattr(self._tuner_tab, "_engine", None)
        if engine is not None and engine.status != "idle":
            log.info("auto-resume: engine already active (%s)", engine.status)
            return
        log.info("auto-resume: resuming session %d (%s)", session.id, session.status)
        self._tuner_tab._resume_session(session.id)

    def closeEvent(self, event) -> None:
        self._closing = True
        manual_running = bool(self._worker and self._worker.isRunning())
        tuner_engine = getattr(self._tuner_tab, "_engine", None)
        tuner_running = bool(
            self._tuner_tab.is_running
            or (tuner_engine and tuner_engine.status in {"running", "paused", "validating", "hunting"})
        )
        memory_worker = getattr(self._memory_tab, "_stress_worker", None)
        memory_running = bool(memory_worker and memory_worker.isRunning())
        owner = self._active_workload_owner()
        workload_active = owner is not _WorkloadOwner.NONE
        if workload_active:
            reply = QMessageBox.question(
                self,
                "Test Running",
                "A workload owns the stress hardware. Stop and exit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                self._closing = False
                event.ignore()
                return

        if manual_running and self._worker:
            self._worker.scheduler.force_stop()
            if not self._worker.scheduler.force_teardown() or not self._worker.wait(5000):
                self._closing = False
                QMessageBox.warning(self, "Teardown incomplete", "The stress payload could not be confirmed stopped.")
                event.ignore()
                return
            if self._logger:
                with contextlib.suppress(RuntimeError):
                    self._worker.core_started.disconnect(self._logger.on_core_started)
                    self._worker.core_finished.disconnect(self._logger.on_core_finished)
                    self._worker.status_updated.disconnect(self._logger.on_status_updated)
                    self._worker.cycle_completed.disconnect(self._logger.on_cycle_completed)
                    self._worker.test_completed.disconnect(self._logger.on_test_completed)
                with contextlib.suppress(Exception):
                    self._logger.on_test_stopped()
                self._logger = None
            with contextlib.suppress(RuntimeError):
                self._worker.status_updated.disconnect(self._on_status_cached)
                self._worker.cycle_completed.disconnect(self._on_cycle_cached)
            with contextlib.suppress(RuntimeError, TypeError):
                self._worker.finished.disconnect(self._on_worker_finished)

        if tuner_running:
            self._tuner_tab.force_stop()
        if memory_running:
            self._memory_tab.force_stop()

        try:
            profile = self._config_tab.get_profile()
            self._settings.update_active_profile(profile)
            self._settings.window_width = self.width()
            self._settings.window_height = self.height()
            save_settings(self._settings)
        except (OSError, ValueError) as exc:
            self._closing = False
            QMessageBox.warning(self, "Settings not saved", str(exc))
            event.ignore()
            return

        self._monitor_tab.stop_monitoring()
        self._msr.close()
        if self._history_db:
            self._history_db.close()
        event.accept()
