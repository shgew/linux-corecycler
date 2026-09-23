"""Auto-Tuner tab - automated PBO Curve Optimizer search UI."""

from __future__ import annotations

import dataclasses
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from corecycler.engine.backends.base import FFTPreset, StressMode
from corecycler.gui.style import PHASE_TO_GRID, button_qss, duration_str, phase_label, status_label, theme
from corecycler.gui.tool_prompt import ensure_tool
from corecycler.history.db import RESUMABLE_STATUSES
from corecycler.history.timefmt import format_local
from corecycler.tuner import report as tuner_report
from corecycler.tuner.config import FIELD_BOUNDS, TEST_ORDERS, TunerConfig
from corecycler.tuner.engine import TunerEngine
from corecycler.tuner.regime import Regime, workload_label
from corecycler.tuner.state import TunerPhase

if TYPE_CHECKING:
    from collections.abc import Callable

    from corecycler.engine.backends.base import StressBackend
    from corecycler.engine.topology import CPUTopology
    from corecycler.history.db import HistoryDB
    from corecycler.smu.driver import RyzenSMU

log = logging.getLogger(__name__)

_PHASE_TO_GRID = PHASE_TO_GRID
_REGIMES: tuple[str, ...] = tuple(str(r) for r in Regime)

_MAX_LOG_ROWS = 2000
_LOG_RESULT_COLUMN = 6
ACTIVE_STATUSES = ("running", "validating", "hunting")

VALIDATION_STAGES: dict[int, str] = {
    1: "per-core",
    2: "all-core",
    3: "half-core",
    4: "transitions",
    5: "spectrum",
    6: "memory",
    7: "soak",
    9: "endurance",
}


def _span(seconds: float) -> str:
    return f"{seconds:g} s" if seconds < 120 else f"{seconds / 60:g} min"


def _per_offset(count: int, seconds: int) -> str:
    return f"{count} x {seconds} s = {_span(count * seconds)} per offset"


def battery_summary(cfg: TunerConfig) -> list[str]:
    """What one offset costs in each phase, from the config the engine runs."""
    coarse = list(cfg.coarse_regimes)
    full = list(_REGIMES)
    search = cfg.search_duration_seconds
    backoff = int(search * cfg.backoff_preconfirm_multiplier)
    return [
        f"Coarse: {', '.join(coarse)} - {_per_offset(len(coarse), search)}",
        f"Fine: {', '.join(full)} - {_per_offset(len(full), search)}",
        f"Backoff: {_per_offset(len(full), backoff)}",
        f"Confirm, anneal: {_per_offset(len(full), cfg.confirm_duration_seconds)}",
        "The first failing regime ends the offset. Time shifts toward regimes that catch failures, "
        f"at least {cfg.regime_floor_pct:g}% each.",
    ]


def battery_workloads(cfg: TunerConfig) -> list[str]:
    return [
        f"{name}: " + " | ".join(workload_label(entry) for entry in cfg.battery if entry["regime"] == name)
        for name in _REGIMES
    ]


def describe_slot(slot: dict, elapsed: float | None = None) -> str:
    """One line saying what the running test is and why it runs."""
    parts: list[str] = []
    hunt = slot.get("hunt")
    if hunt is not None:
        parts.append(f"Hunt probe (level {hunt['level']}): cores {hunt['live']} live, the rest at stock")
        parts.append(f"load on cores {slot['cores']}")
    else:
        stage = slot.get("validation_stage")
        if stage is not None:
            parts.append(f"Validation S{stage} ({VALIDATION_STAGES.get(stage, f'stage {stage}')})")
        if "core" in slot:
            parts.append(f"core {slot['core']} at {slot['offset']} ({phase_label(slot['phase'])})")
        else:
            parts.append(f"cores {slot['cores']}")
    regimes = slot.get("battery_regimes")
    if regimes:
        parts.append(f"regime {slot['battery_position']} of {len(regimes)}: {slot['regime']} ({', '.join(regimes)})")
    elif slot.get("regime"):
        parts.append(f"regime {slot['regime']}")
    label = workload_label(slot)
    if label:
        parts.append(label)
    duration = slot.get("duration_seconds")
    if duration:
        parts.append(f"{int(elapsed)}/{duration} s" if elapsed is not None else f"{duration} s")
    return "Now: " + " | ".join(parts)


class TunerTab(QWidget):
    """Auto-Tuner tab for the main window."""

    # Emitted when tuner starts/stops so MainWindow can disable manual test
    tuner_running_changed = Signal(bool)
    tuner_core_testing = Signal(int, str)  # core_id, state ("testing"/"passed"/"failed"/etc)
    tuner_core_elapsed = Signal(int, float)  # core_id, elapsed_seconds
    tuner_core_info = Signal(int, int, str)  # core_id, co_offset, phase for sidebar enrichment

    def __init__(
        self,
        db: HistoryDB | None,
        topology: CPUTopology | None,
        smu: RyzenSMU | None,
        backend_factory=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._topology = topology
        self._smu = smu
        self._backend_factory = backend_factory
        self._engine: TunerEngine | None = None
        self._external_test_running = False
        self._selected_core: int | None = None
        self._display_slots: list[Callable[..., None]] = []

        self._tuner_timer = QTimer(self)
        self._tuner_timer.timeout.connect(self._tick_tuner)
        self._active_test_core: int | None = None
        self._test_start_time: float = 0
        self._slot: dict | None = None
        self._slot_started_at: float = 0

        self._setup_ui()
        self._check_resume()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        # Status bar
        status_layout = QHBoxLayout()
        self._status_label = QLabel("Status: Idle")
        self._status_label.setFont(QFont("monospace", 11, QFont.Weight.Bold))
        status_layout.addWidget(self._status_label)

        self._progress_label = QLabel("")
        self._progress_label.setStyleSheet(f"color: {theme.COLOR_TEXT_DIM};")
        status_layout.addWidget(self._progress_label)
        status_layout.addStretch()
        layout.addLayout(status_layout)

        self._slot_label = QLabel("")
        self._slot_label.setWordWrap(True)
        self._slot_label.setStyleSheet(f"color: {theme.COLOR_TEXT_DIM};")
        layout.addWidget(self._slot_label)

        # Main splitter: config+table on top, log on bottom
        splitter = QSplitter(Qt.Orientation.Vertical)

        # Top section
        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)

        # Config panel with no scroll, just a plain container
        self._config_container = QWidget()
        config_inner = QVBoxLayout(self._config_container)
        config_inner.setContentsMargins(0, 0, 0, 0)
        config_inner.setSpacing(8)
        self._build_config_panel(config_inner)
        top_layout.addWidget(self._config_container)

        # Action buttons
        btn_layout = QHBoxLayout()
        self._start_btn = QPushButton("Start Tuning")
        self._start_btn.setStyleSheet(button_qss(theme.BTN_GREEN))
        self._start_btn.clicked.connect(self._on_start)
        btn_layout.addWidget(self._start_btn)

        self._pause_btn = QPushButton("Pause")
        self._pause_btn.setEnabled(False)
        self._pause_btn.clicked.connect(self._on_pause)
        btn_layout.addWidget(self._pause_btn)

        self._resume_btn = QPushButton("Resume")
        self._resume_btn.setEnabled(False)
        self._resume_btn.clicked.connect(self._on_resume)
        btn_layout.addWidget(self._resume_btn)

        self._abort_btn = QPushButton("Abort")
        self._abort_btn.setEnabled(False)
        self._abort_btn.setStyleSheet(button_qss(theme.BTN_RED))
        self._abort_btn.clicked.connect(self._on_abort)
        btn_layout.addWidget(self._abort_btn)

        btn_layout.addStretch()

        self._validate_btn = QPushButton("Validate Profile")
        self._validate_btn.setEnabled(False)
        self._validate_btn.clicked.connect(self._on_validate)
        btn_layout.addWidget(self._validate_btn)

        self._export_btn = QPushButton("Export Profile")
        self._export_btn.setEnabled(False)
        self._export_btn.clicked.connect(self._on_export)
        btn_layout.addWidget(self._export_btn)

        top_layout.addLayout(btn_layout)

        # Core status table
        self._core_table = QTableWidget()
        self._core_table.setColumnCount(10 + len(_REGIMES))
        self._core_table.setHorizontalHeaderLabels(
            [
                "Core",
                "CCD",
                "Phase",
                "Candidate",
                "Accepted",
                "BIOS",
                "Confidence",
                *(regime.capitalize() for regime in _REGIMES),
                "Suspicion",
                "Crashes",
                "Strikes",
            ]
        )
        bios_header = self._core_table.horizontalHeaderItem(5)
        bios_header.setToolTip("Accepted offset moved toward stock by the BIOS guard band, for persistent BIOS use")
        strikes_header = self._core_table.horizontalHeaderItem(self._core_table.columnCount() - 1)
        strikes_header.setToolTip("Failed anneal probes one step deeper; each doubles the clean time the next needs")
        self._core_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._core_table.verticalHeader().setVisible(False)
        self._core_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._core_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._core_table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self._core_table.currentCellChanged.connect(self._on_core_selected)
        self._install_copy_shortcut(self._core_table)
        top_layout.addWidget(self._core_table)

        splitter.addWidget(top)

        # Bottom: test log
        bottom = QWidget()
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)

        log_header = QHBoxLayout()
        log_label = QLabel("Test Log")
        log_label.setFont(QFont("monospace", 10, QFont.Weight.Bold))
        log_header.addWidget(log_label)

        self._log_filter_label = QLabel("(all cores)")
        self._log_filter_label.setStyleSheet(f"color: {theme.COLOR_TEXT_DIM};")
        log_header.addWidget(self._log_filter_label)
        log_header.addStretch()

        clear_log_btn = QPushButton("Clear")
        clear_log_btn.setFixedWidth(60)
        clear_log_btn.clicked.connect(lambda: self._log_table.setRowCount(0))
        log_header.addWidget(clear_log_btn)

        bottom_layout.addLayout(log_header)

        self._log_table = QTableWidget()
        self._log_table.setColumnCount(9)
        self._log_table.setHorizontalHeaderLabels(
            [
                "Time",
                "Core",
                "Offset",
                "Phase",
                "Regime",
                "Workload",
                "Result",
                "Duration",
                "Error",
            ]
        )
        self._log_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._log_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self._log_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._log_table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self._log_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._install_copy_shortcut(self._log_table)
        bottom_layout.addWidget(self._log_table)

        splitter.addWidget(bottom)

        events = QWidget()
        events_layout = QVBoxLayout(events)
        events_layout.setContentsMargins(0, 0, 0, 0)
        events_label = QLabel("Tuner Events")
        events_label.setFont(QFont("monospace", 10, QFont.Weight.Bold))
        events_layout.addWidget(events_label)
        self._events_view = QPlainTextEdit()
        self._events_view.setReadOnly(True)
        self._events_view.setMaximumBlockCount(_MAX_LOG_ROWS)
        events_layout.addWidget(self._events_view)
        splitter.addWidget(events)

        splitter.setSizes([400, 200, 120])
        layout.addWidget(splitter)

    def _build_config_panel(self, parent_layout: QVBoxLayout) -> None:
        columns = QHBoxLayout()
        columns.addWidget(self._build_search_panel())
        right_column = QVBoxLayout()
        right_column.addWidget(self._build_workload_panel())
        right_column.addWidget(self._build_timing_panel())
        columns.addLayout(right_column)
        columns.addWidget(self._build_battery_panel())
        parent_layout.addLayout(columns)

        button_row = QHBoxLayout()
        defaults_button = QPushButton("Load Defaults")
        defaults_button.clicked.connect(self._load_defaults)
        button_row.addWidget(defaults_button)
        button_row.addStretch()
        parent_layout.addLayout(button_row)
        self._apply_config_to_ui(TunerConfig())

    @staticmethod
    def _apply_field_bounds(
        spin: QSpinBox | QDoubleSpinBox,
        field: str,
        bounds: tuple[int | float, int | float] | None = None,
    ) -> None:
        minimum, maximum = bounds or FIELD_BOUNDS[field]
        spin.setRange(minimum, maximum)
        spin.setValue(getattr(TunerConfig(), field))

    def _build_search_panel(self) -> QGroupBox:
        group = QGroupBox("Search Parameters")
        layout = QFormLayout(group)
        layout.setSpacing(6)

        self._start_offset_spin = QSpinBox()
        self._apply_field_bounds(self._start_offset_spin, "start_offset", self._co_range())
        self._start_offset_spin.setToolTip("Starting CO value for all cores (0 = BIOS baseline)")
        layout.addRow("Start offset:", self._start_offset_spin)

        self._inherit_current_check = QCheckBox("Inherit current CO from SMU")
        self._inherit_current_check.setToolTip("Use current per-core SMU offsets as the session starting points")
        layout.addRow("", self._inherit_current_check)

        self._auto_validate_check = QCheckBox("Auto-validate after all cores confirmed")
        self._auto_validate_check.setToolTip("Run whole-profile validation after per-core confirmation")
        layout.addRow("", self._auto_validate_check)

        self._coarse_step_spin = QSpinBox()
        self._apply_field_bounds(self._coarse_step_spin, "coarse_step")
        self._coarse_step_spin.setToolTip("Step size during coarse search")
        layout.addRow("Coarse step:", self._coarse_step_spin)

        self._fine_step_spin = QSpinBox()
        self._apply_field_bounds(self._fine_step_spin, "fine_step")
        self._fine_step_spin.setToolTip("Step size during fine search")
        layout.addRow("Fine step:", self._fine_step_spin)

        self._max_offset_spin = QSpinBox()
        self._apply_field_bounds(self._max_offset_spin, "max_offset", self._co_range())
        self._max_offset_spin.setToolTip("Most aggressive offset to try")
        layout.addRow("Max offset:", self._max_offset_spin)

        self._max_retries_spin = QSpinBox()
        self._apply_field_bounds(self._max_retries_spin, "max_confirm_retries")
        self._max_retries_spin.setToolTip("Confirmation retries before backing off")
        layout.addRow("Confirm retries:", self._max_retries_spin)

        self._stretch_threshold_spin = QDoubleSpinBox()
        self._apply_field_bounds(self._stretch_threshold_spin, "stretch_threshold_pct")
        self._stretch_threshold_spin.setSingleStep(0.5)
        self._stretch_threshold_spin.setSuffix("%")
        self._stretch_threshold_spin.setToolTip("Below-nominal active-clock warning threshold; 0 disables it")
        self._configure_msr_control()
        layout.addRow("Below-nominal warning:", self._stretch_threshold_spin)

        self._order_combo = QComboBox()
        self._order_combo.addItems(TEST_ORDERS)
        layout.addRow("Test order:", self._order_combo)
        return group

    def _configure_msr_control(self) -> None:
        import os

        try:
            fd = os.open("/dev/cpu/0/msr", os.O_RDONLY)
            os.close(fd)
        except OSError:
            self._stretch_threshold_spin.setEnabled(False)
            self._stretch_threshold_spin.setToolTip(
                "MSR access unavailable. Active-clock warnings require the msr module and CAP_SYS_RAWIO."
            )
            self._stretch_threshold_spin.setStyleSheet(f"color: {theme.COLOR_MUTED};")

    def _build_workload_panel(self) -> QGroupBox:
        from corecycler.engine.backends import available_backends, load_all

        load_all()
        group = QGroupBox("Validation Stress")
        group.setToolTip("Workload for multi-core validation stages. Search slots run the regime battery instead.")
        layout = QFormLayout(group)
        layout.setSpacing(6)

        self._backend_combo = QComboBox()
        self._backend_combo.addItems(available_backends())
        layout.addRow("Backend:", self._backend_combo)

        self._mode_combo = QComboBox()
        self._mode_combo.addItems([mode.name for mode in StressMode if mode != StressMode.CUSTOM])
        layout.addRow("Mode:", self._mode_combo)

        self._fft_combo = QComboBox()
        self._fft_combo.addItems([preset.name for preset in FFTPreset if preset != FFTPreset.CUSTOM])
        layout.addRow("FFT preset:", self._fft_combo)
        return group

    def _build_timing_panel(self) -> QGroupBox:
        group = QGroupBox("Timing")
        layout = QFormLayout(group)
        layout.setSpacing(6)

        self._search_dur_spin = QSpinBox()
        self._apply_field_bounds(self._search_dur_spin, "search_duration_seconds")
        self._search_dur_spin.setSuffix("s")
        layout.addRow("Search, per regime:", self._search_dur_spin)

        self._confirm_dur_spin = QSpinBox()
        self._apply_field_bounds(self._confirm_dur_spin, "confirm_duration_seconds")
        self._confirm_dur_spin.setSuffix("s")
        layout.addRow("Confirm, per regime:", self._confirm_dur_spin)

        self._validate_dur_spin = QSpinBox()
        self._apply_field_bounds(self._validate_dur_spin, "validate_duration_seconds")
        self._validate_dur_spin.setSuffix("s")
        layout.addRow("Validate duration:", self._validate_dur_spin)
        return group

    def _build_battery_panel(self) -> QGroupBox:
        group = QGroupBox("Search Battery")
        layout = QVBoxLayout(group)
        self._battery_label = QLabel("")
        self._battery_label.setWordWrap(True)
        self._battery_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self._battery_label)
        layout.addStretch()
        self._battery_config = TunerConfig()
        self._search_dur_spin.valueChanged.connect(self._refresh_battery_summary)
        self._confirm_dur_spin.valueChanged.connect(self._refresh_battery_summary)
        return group

    def _refresh_battery_summary(self) -> None:
        cfg = dataclasses.replace(
            self._battery_config,
            search_duration_seconds=self._search_dur_spin.value(),
            confirm_duration_seconds=self._confirm_dur_spin.value(),
        )
        self._battery_label.setText("\n".join(battery_summary(cfg)))
        self._battery_label.setToolTip("\n".join(battery_workloads(cfg)))

    def _get_config(self) -> TunerConfig:
        return TunerConfig(
            start_offset=self._start_offset_spin.value(),
            coarse_step=self._coarse_step_spin.value(),
            fine_step=self._fine_step_spin.value(),
            max_offset=self._max_offset_spin.value(),
            search_duration_seconds=self._search_dur_spin.value(),
            confirm_duration_seconds=self._confirm_dur_spin.value(),
            validate_duration_seconds=self._validate_dur_spin.value(),
            max_confirm_retries=self._max_retries_spin.value(),
            stretch_threshold_pct=self._stretch_threshold_spin.value(),
            inherit_current=self._inherit_current_check.isChecked(),
            auto_validate=self._auto_validate_check.isChecked(),
            test_order=self._order_combo.currentText(),
            backend=self._backend_combo.currentText(),
            stress_mode=self._mode_combo.currentText(),
            fft_preset=self._fft_combo.currentText(),
        )

    def _load_defaults(self) -> None:
        self._apply_config_to_ui(TunerConfig())

    def _apply_config_to_ui(self, cfg: TunerConfig) -> None:
        """Reflect a TunerConfig in the config panel widgets.

        Used for defaults AND on resume: a resumed session runs its SAVED
        config, so the panel must show those values, not whatever was left
        in the boxes from before.
        """
        self._battery_config = cfg
        self._start_offset_spin.setValue(cfg.start_offset)
        self._coarse_step_spin.setValue(cfg.coarse_step)
        self._fine_step_spin.setValue(cfg.fine_step)
        self._max_offset_spin.setValue(cfg.max_offset)
        self._search_dur_spin.setValue(cfg.search_duration_seconds)
        self._confirm_dur_spin.setValue(cfg.confirm_duration_seconds)
        self._max_retries_spin.setValue(cfg.max_confirm_retries)
        self._stretch_threshold_spin.setValue(cfg.stretch_threshold_pct)
        self._validate_dur_spin.setValue(cfg.validate_duration_seconds)
        self._inherit_current_check.setChecked(cfg.inherit_current)
        self._auto_validate_check.setChecked(cfg.auto_validate)
        self._order_combo.setCurrentText(cfg.test_order)
        self._backend_combo.setCurrentText(cfg.backend)
        self._mode_combo.setCurrentText(cfg.stress_mode)
        self._fft_combo.setCurrentText(cfg.fft_preset)
        self._refresh_battery_summary()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_start(self) -> None:
        if self._external_test_running:
            return
        if not self._db or not self._topology:
            QMessageBox.warning(self, "Error", "Database or topology not available")
            return

        if self._engine is not None and (self._engine.status in ACTIVE_STATUSES or self._engine.status == "paused"):
            QMessageBox.warning(
                self,
                "Session Active",
                "A tuner session is already active or paused. Resume or abort it before starting a new one.",
            )
            return

        if not self._smu or not self._smu.is_available():
            QMessageBox.warning(
                self,
                "SMU Not Available",
                "The ryzen_smu kernel module is not loaded.\n\n"
                "The auto-tuner requires SMU access to write Curve Optimizer values.\n"
                "Load the module with: sudo modprobe ryzen_smu",
            )
            return

        reply = QMessageBox.warning(
            self,
            "Start Auto-Tuner",
            "The Auto-Tuner will iteratively modify per-core Curve Optimizer "
            "offsets via SMU writes. This may cause system instability or "
            "crashes.\n\n"
            "Positive CO offsets increase voltage and can degrade or damage "
            "hardware (especially V-Cache / X3D processors).\n\n"
            "Values are volatile and reset on reboot.\n\n"
            "Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        backend = self._get_backend()
        if backend is None:
            return

        config = self._get_config()
        errors = config.validate(self._co_range()) + config.backend_availability_errors()
        if errors:
            QMessageBox.warning(self, "Invalid Configuration", "\n".join(errors))
            return
        self._engine = TunerEngine(
            db=self._db,
            topology=self._topology,
            smu=self._smu,
            backend=backend,
            config=config,
        )
        self._wire_engine()

        self._events_view.clear()
        self._engine.start()
        if self._engine.status not in ACTIVE_STATUSES:
            QMessageBox.warning(
                self,
                "Tuner Did Not Start",
                "The engine refused to start - see the log for the reason.",
            )
            return
        self._set_running_state(True)

        # Initialize table with all cores
        for core_id in self._engine.core_states:
            self._update_core_row(core_id)

    def _on_pause(self) -> None:
        if self._engine:
            self._engine.pause()
            self._pause_btn.setEnabled(False)
            self._resume_btn.setEnabled(True)

    def _on_resume(self) -> None:
        # If we have an active paused engine, resume it directly
        if self._engine and self._engine.session_id and self._engine.status == "paused":
            self._resume_session(self._engine.session_id)
            return

        # Otherwise show session picker from DB
        if not self._db:
            return
        sessions = self._db.list_recoverable_tuner_sessions()
        if not sessions:
            QMessageBox.information(self, "No Sessions", "No recoverable tuner sessions found.")
            return
        if len(sessions) == 1 and sessions[0].status != "profile_quarantined":
            # Only one, and nothing about it needs a warning, so resume it directly
            self._resume_session(sessions[0].id)
            return

        # Multiple sessions: show picker dialog
        dialog = QDialog(self)
        dialog.setWindowTitle("Resume Tuner Session")
        dialog.setMinimumWidth(500)
        dlg_layout = QVBoxLayout(dialog)
        dlg_layout.addWidget(QLabel("Select a session to resume:"))

        session_list = QListWidget()
        for sess in sessions:
            core_states = self._db.get_tuner_core_states(sess.id)
            total = len(core_states)
            confirmed = sum(1 for cs in core_states.values() if cs.phase is TunerPhase.CONFIRMED)
            started = format_local(sess.created_at) if sess.created_at else "?"
            last = format_local(sess.updated_at) if sess.updated_at else started
            label = (
                f"#{sess.id}  started {started}  last {last}  "
                f"[{status_label(sess.status)}]  "
                f"{confirmed}/{total} cores confirmed  "
                f"({sess.cpu_model[:30] if sess.cpu_model else '?'})"
            )
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, sess.id)
            session_list.addItem(item)
        session_list.setCurrentRow(0)
        dlg_layout.addWidget(session_list)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        dlg_layout.addWidget(buttons)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        selected = session_list.currentItem()
        if selected is None:
            return
        session_id = selected.data(Qt.ItemDataRole.UserRole)
        chosen = next((s for s in sessions if s.id == session_id), None)
        if (
            chosen is not None
            and chosen.status == "profile_quarantined"
            and not self._confirm_profile_quarantine(chosen)
        ):
            return
        self._resume_session(session_id)

    def _confirm_profile_quarantine(self, session) -> bool:
        """A profile-quarantined session is only ever re-opened deliberately."""
        return (
            QMessageBox.question(
                self,
                "Session was quarantined",
                f"Session #{session.id} was quarantined after "
                f"{session.resume_crash_streak} crash-resumes: the machine kept dying "
                "when its offsets were re-applied.\n\n"
                "Resuming keeps everything it learned, but re-engages only offsets this "
                "machine has already survived; anything unproven drops to stock. If it "
                "quarantines again, the real limits are lower than the search assumed.\n\n"
                "Resume it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        )

    def _resume_session(self, session_id: int) -> None:
        """Resume a specific tuner session by ID."""
        if self._external_test_running:
            return
        if not self._smu or not self._smu.is_available():
            QMessageBox.warning(
                self,
                "SMU Not Available",
                "The ryzen_smu kernel module is not loaded.\n\n"
                "The auto-tuner requires SMU access to write Curve Optimizer values.\n"
                "Load the module with: sudo modprobe ryzen_smu",
            )
            return

        session = self._db.get_tuner_session(session_id) if self._db else None
        if session is None:
            QMessageBox.warning(self, "Error", "Session not found")
            return
        try:
            config = TunerConfig.from_json(session.config_json)
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid Configuration", str(exc))
            return
        errors = config.validate(self._co_range()) + config.backend_availability_errors()
        if errors:
            QMessageBox.warning(self, "Invalid Configuration", "\n".join(errors))
            return
        self._apply_config_to_ui(config)

        if self._engine is None:
            if not self._topology:
                QMessageBox.warning(self, "Error", "CPU topology not available")
                return
            backend = self._get_backend(config.backend)
            if backend is None:
                return
            self._engine = TunerEngine(
                db=self._db,
                topology=self._topology,
                smu=self._smu,
                backend=backend,
                config=config,
            )
            self._wire_engine()
        log.info("Resuming tuner session %d with its saved config", session_id)
        self._show_session_events(session_id)
        self._engine.resume(session_id)
        if self._engine.status not in ACTIVE_STATUSES:
            QMessageBox.warning(
                self,
                "Resume Did Not Start",
                "The engine did not resume - see the log for the reason.",
            )
            return
        self._set_running_state(True)

        # Initialize table with all cores
        for core_id in self._engine.core_states:
            self._update_core_row(core_id)

    def _on_abort(self) -> None:
        if self._engine:
            self._engine.abort()
            self._set_running_state(False)
            # Reset core sidebar states to each core's actual phase. Abort
            # doesn't emit core_state_changed, and a blanket "pending" lied
            # about confirmed/hardened cores.
            for core_id, cs in self._engine.core_states.items():
                self.tuner_core_testing.emit(core_id, _PHASE_TO_GRID[cs.phase])
                self.tuner_core_info.emit(core_id, cs.current_offset, cs.phase)
            # Stop elapsed timer
            self._active_test_core = None
            self._tuner_timer.stop()

    def _on_validate(self) -> None:
        if self._external_test_running:
            return
        if not self._engine or not self._engine.session_id:
            return
        self._engine.validate_profile(self._engine.session_id)
        if self._engine.status not in ACTIVE_STATUSES:
            QMessageBox.warning(
                self,
                "Validation Did Not Start",
                "The engine refused to validate - see the log for the reason.",
            )
            return
        self._set_running_state(True)

    def _on_export(self) -> None:
        """Export confirmed CO profile to a JSON file."""
        if not self._engine or not self._engine.session_id or not self._db:
            return
        profile = self._db.get_tuner_best_profile(self._engine.session_id)
        if not profile:
            QMessageBox.information(self, "Export", "No confirmed cores to export")
            return

        from corecycler.config.paths import user_home
        from corecycler.config.settings import save_co_profile

        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export CO Profile",
            str(user_home() / "co-profile-tuner.json"),
            "JSON (*.json)",
        )
        if not path:
            return

        cpu_model = ""
        if self._topology:
            cpu_model = self._topology.model_name

        try:
            save_co_profile(profile, Path(path), cpu_model=cpu_model, source="auto-tuner")
            QMessageBox.information(self, "Exported", f"CO profile exported to {path}")
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to export: {e}")

    # ------------------------------------------------------------------
    # Engine signals
    # ------------------------------------------------------------------

    def _wire_engine(self) -> None:
        if not self._engine:
            return
        for signal, handler in (
            (self._engine.core_state_changed, self._on_core_state_changed),
            (self._engine.worker_started, self._on_worker_started),
            (self._engine.test_completed, self._on_test_completed),
            (self._engine.session_completed, self._on_session_completed),
            (self._engine.status_changed, self._on_status_changed),
            (self._engine.progress_updated, self._on_progress_updated),
            (self._engine.log_message, self._on_log_message),
            (self._engine.slot_started, self._on_slot_started),
            (self._engine.platform_fault, self._on_platform_fault),
            (self._engine.co_drift_detected, self._on_co_drift),
            (self._engine.validation_progress, self._on_validation_progress),
        ):
            self._connect_display_signal(signal, handler)

    def _connect_display_signal(self, signal, handler: Callable[..., None]) -> None:
        def guarded(*args) -> None:
            try:
                handler(*args)
            except Exception:
                log.exception("Tuner display handler %s failed", getattr(handler, "__name__", type(handler).__name__))

        self._display_slots.append(guarded)
        signal.connect(guarded, Qt.ConnectionType.QueuedConnection)

    @Slot(str)
    def _on_co_drift(self, drift_json: str) -> None:
        """Warn user that SMU CO values differ from session baselines."""
        drift = json.loads(drift_json)
        lines = [
            f"Core {cid}: tuner last wrote {v['expected']}, found {v['actual']}"
            for cid, v in sorted(drift.items(), key=lambda x: int(x[0]))
        ]
        QMessageBox.warning(
            self,
            "CO Drift Detected",
            "CO offsets in the SMU differ from what the tuner last wrote - "
            "something outside the tuner changed them (Curve Optimizer tab, "
            "another tool).\n\n"
            "The session's own values will be re-applied before testing resumes.\n\n" + "\n".join(lines),
        )

    @Slot(int, str, int)
    def _on_core_state_changed(self, core_id: int, phase: str, offset: int) -> None:
        self._update_core_row(core_id)
        grid_state = _PHASE_TO_GRID.get(phase, "pending")

        # Only override to "testing" if this core is actively being tested.
        # _active_test_core is cleared in _on_test_completed (which fires
        # before _advance_core), so post-test state changes won't re-apply
        # the "testing" highlight to a core that just finished.
        if core_id == self._active_test_core:
            grid_state = "testing"

        self.tuner_core_testing.emit(core_id, grid_state)
        self.tuner_core_info.emit(core_id, offset, phase)

    @Slot(int)
    def _on_worker_started(self, core_id: int) -> None:
        """Mark exactly this core as 'testing' in the sidebar."""
        # Revert previous active core to its phase-appropriate state
        if self._active_test_core is not None and self._active_test_core != core_id:
            prev_cs = self._engine.core_states.get(self._active_test_core) if self._engine else None
            if prev_cs:
                prev_state = _PHASE_TO_GRID.get(prev_cs.phase, "pending")
                self.tuner_core_testing.emit(self._active_test_core, prev_state)

        self._active_test_core = core_id
        self.tuner_core_testing.emit(core_id, "testing")
        self._test_start_time = time.monotonic()
        if not self._tuner_timer.isActive():
            self._tuner_timer.start(1000)

    @Slot(int, int, bool)
    def _on_test_completed(self, core_id: int, offset: int, passed: bool) -> None:
        # Clear active test core BEFORE advance_core emits core_state_changed.
        # Without this, the state_changed handler still overrides to "testing"
        # for the just-finished core, causing brief dual-highlight in the sidebar.
        if self._active_test_core == core_id:
            self._active_test_core = None
            self._tuner_timer.stop()
        self._clear_slot()
        self._update_core_row(core_id)
        self._add_log_entry(core_id, offset, passed)

    @Slot(str)
    def _on_session_completed(self, profile_json: str) -> None:
        profile = json.loads(profile_json) if profile_json else {}
        self._active_test_core = None
        self._tuner_timer.stop()
        self._set_running_state(False)
        self._validate_btn.setEnabled(bool(profile))
        self._export_btn.setEnabled(bool(profile))
        self._notify(
            "Tuning complete",
            f"{len(profile)} core(s) confirmed. Load the profile from the Curve Optimizer or History tab.",
        )

    def _notify(self, title: str, body: str, *, urgency: str = "normal") -> None:
        # A notification must never take the app down: swallow any failure
        # (module missing, no D-Bus, no daemon), because the tune result already
        # stands by the time this runs.
        try:
            from corecycler.config.settings import load_settings

            if not load_settings().notify_on_completion:
                return
            from corecycler.notify import desktop_notify

            desktop_notify(title, body, urgency=urgency)
        except Exception:
            log.debug("desktop notification failed", exc_info=True)

    @Slot(str)
    def _on_platform_fault(self, evidence: str) -> None:
        self._notify(
            "Platform fault",
            f"Curve Optimizer offsets are not implicated: {evidence}.",
            urgency="critical",
        )

    @Slot(str)
    def _on_status_changed(self, status: str) -> None:
        if status in (*ACTIVE_STATUSES, "paused"):
            self._set_running_state(True)
        if status == "validating":
            self._status_label.setText("Status: Validating")
        else:
            self._status_label.setText(f"Status: {status_label(status)}")
            # Clear validation progress when leaving validation
            self._progress_label.setText("")
        if status not in ACTIVE_STATUSES and not (self._engine is not None and self._engine.test_in_flight):
            self._clear_slot()
        # The engine pauses ITSELF on apparatus/SMU/startup faults ("fix the
        # cause, then Resume"). The buttons must follow the engine's status,
        # or every self-pause is a dead end with Resume greyed out.
        if status == "paused":
            self._pause_btn.setEnabled(False)
            self._resume_btn.setEnabled(True)
            # A self-pause after a test that could not run never emits test_completed.
            if self._engine is not None and not self._engine.test_in_flight and self._active_test_core is not None:
                cs = self._engine.core_states.get(self._active_test_core)
                if cs is not None:
                    self.tuner_core_testing.emit(self._active_test_core, _PHASE_TO_GRID.get(cs.phase, "pending"))
                self._active_test_core = None
                self._tuner_timer.stop()
        elif status in ("running", "validating", "hunting"):
            self._pause_btn.setEnabled(True)
            self._resume_btn.setEnabled(False)
            self._abort_btn.setEnabled(True)
        elif status in ("idle", "profile_quarantined", "platform_fault"):
            self._active_test_core = None
            self._tuner_timer.stop()
            self._set_running_state(False)
            if self._engine is not None:
                for core_id, cs in self._engine.core_states.items():
                    self.tuner_core_testing.emit(core_id, _PHASE_TO_GRID[cs.phase])
                    self.tuner_core_info.emit(core_id, cs.current_offset, cs.phase)
            if status == "profile_quarantined":
                self._notify(
                    "Tuning quarantined",
                    "The machine repeatedly failed after its offsets were restored. The profile is unsafe and stock "
                    "offsets were restored. Investigate before retrying.",
                    urgency="critical",
                )

    @Slot(int, int, int)
    def _on_validation_progress(self, stage: int, current: int, total: int) -> None:
        """Update status and progress labels during multi-core validation."""
        stage_name = VALIDATION_STAGES.get(stage, f"stage {stage}")
        self._status_label.setText(f"Status: Validating S{stage} ({stage_name})")
        self._progress_label.setText(f"S{stage}: {current}/{total}")

    @Slot(int, int)
    def _on_progress_updated(self, done: int, total: int) -> None:
        self._progress_label.setText(f"{done}/{total} cores confirmed")

    @Slot(str)
    def _on_log_message(self, msg: str) -> None:
        log.info("[tuner] %s", msg)
        self._append_event(datetime.now(UTC).isoformat(), msg)

    @Slot(str)
    def _on_slot_started(self, payload: str) -> None:
        self._slot = json.loads(payload)
        self._slot_started_at = time.monotonic()
        self._slot_label.setText(describe_slot(self._slot))
        if not self._tuner_timer.isActive():
            self._tuner_timer.start(1000)

    def _clear_slot(self) -> None:
        self._slot = None
        self._slot_label.setText("")

    def _append_event(self, timestamp: str, message: str, severity: str = "info") -> None:
        marker = "" if severity == "info" else f"[{severity}] "
        self._events_view.appendPlainText(f"{format_local(timestamp)}  {marker}{message}")

    def _show_session_events(self, session_id: int) -> None:
        self._events_view.clear()
        for event in self._db.get_tuner_events(session_id):
            self._append_event(event.get("timestamp", ""), event.get("message", ""), event.get("severity", "info"))

    def _tick_tuner(self) -> None:
        if self._slot is not None:
            self._slot_label.setText(describe_slot(self._slot, time.monotonic() - self._slot_started_at))
        if self._active_test_core is not None:
            elapsed = time.monotonic() - self._test_start_time
            self.tuner_core_elapsed.emit(self._active_test_core, elapsed)
        elif self._engine is None or self._engine.status == "idle":
            self._tuner_timer.stop()

    # ------------------------------------------------------------------
    # Table updates
    # ------------------------------------------------------------------

    def _update_core_row(self, core_id: int) -> None:
        if not self._engine or not self._db or not self._engine.session_id:
            return
        cs = self._engine.core_states.get(core_id)
        if cs is None:
            return

        projection = tuner_report.core_row(self._db, self._engine.session_id, core_id)
        row = self._find_core_row(core_id)
        if row < 0:
            row = self._core_table.rowCount()
            self._core_table.insertRow(row)

        core_info = self._topology.cores.get(core_id) if self._topology else None
        ccd = core_info.ccd if core_info else None
        ccd_text = "-" if ccd is None else f"{ccd} V-Cache" if core_info.has_vcache else str(ccd)
        hours = projection["hours"]
        accepted = projection["accepted_offset"]
        bios = projection["bios_offset"]
        items = [
            str(core_id),
            ccd_text,
            phase_label(cs.phase),
            str(projection["candidate_offset"]),
            str(accepted) if accepted is not None else "-",
            str(bios) if bios is not None else "-",
            duration_str(round(projection["confidence_hours"] * 3600.0)),
            *(duration_str(round(hours.get(regime, 0.0) * 3600.0)) for regime in _REGIMES),
            f"{projection['suspicion']:.1f}" if projection["suspicion"] else "-",
            str(projection["crashes"]),
            str(projection["anneal_strikes"]),
        ]

        color = QColor(theme.PHASE_COLORS[cs.phase])
        for column, text in enumerate(items):
            item = QTableWidgetItem(text)
            item.setForeground(color)
            if column == 0:
                item.setData(Qt.ItemDataRole.UserRole, projection)
            self._core_table.setItem(row, column, item)

    def _find_core_row(self, core_id: int) -> int:
        for row in range(self._core_table.rowCount()):
            item = self._core_table.item(row, 0)
            if item and item.text() == str(core_id):
                return row
        return -1

    def _add_log_entry(self, core_id: int, offset: int, passed: bool) -> None:
        if not self._db or not self._engine or not self._engine.session_id:
            return
        entries = self._db.get_tuner_test_log(self._engine.session_id, core_id=core_id, limit=1)
        if not entries:
            return
        if self._selected_core is not None and core_id != self._selected_core:
            return
        self._append_log_row(entries[-1])
        self._log_table.scrollToBottom()

    def _append_log_row(self, entry: dict) -> None:
        if self._log_table.rowCount() >= _MAX_LOG_ROWS:
            self._log_table.removeRow(0)
        passed = bool(entry["passed"])
        row = self._log_table.rowCount()
        self._log_table.insertRow(row)
        items = [
            format_local(entry.get("tested_at", "")),
            str(entry["core_id"]),
            str(entry["offset_tested"]),
            entry.get("phase", ""),
            entry.get("regime") or "-",
            workload_label(entry) or "-",
            "PASS" if passed else "FAIL",
            f"{entry.get('duration_seconds', 0):.1f}s" if entry.get("duration_seconds") else "-",
            entry.get("error_message", "") or "",
        ]
        color = QColor(theme.COLOR_PASS) if passed else QColor(theme.COLOR_FAIL)
        for column, text in enumerate(items):
            item = QTableWidgetItem(text)
            if column == _LOG_RESULT_COLUMN:
                item.setForeground(color)
            self._log_table.setItem(row, column, item)

    @Slot(int, int, int, int)
    def _on_core_selected(self, row: int, col: int, prev_row: int, prev_col: int) -> None:
        item = self._core_table.item(row, 0)
        if item:
            self._selected_core = int(item.text())
            self._log_filter_label.setText(f"(core {self._selected_core})")
            self._refresh_log_table()
        else:
            self._selected_core = None
            self._log_filter_label.setText("(all cores)")
            self._refresh_log_table()

    def _refresh_log_table(self) -> None:
        """Rebuild the log table based on the selected core filter."""
        self._log_table.setRowCount(0)
        if not self._db or not self._engine or not self._engine.session_id:
            return

        entries = self._db.get_tuner_test_log(
            self._engine.session_id,
            core_id=self._selected_core,
            limit=_MAX_LOG_ROWS,
        )
        for entry in entries:
            self._append_log_row(entry)
        self._log_table.scrollToBottom()

    # ------------------------------------------------------------------
    # Clipboard
    # ------------------------------------------------------------------

    def _install_copy_shortcut(self, table: QTableWidget) -> None:
        """Add Ctrl+C support to a QTableWidget by copying selected rows as TSV."""
        shortcut = QShortcut(QKeySequence.StandardKey.Copy, table)
        shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        shortcut.activated.connect(lambda: self._copy_table_selection(table))

    def _copy_table_selection(self, table: QTableWidget) -> None:
        """Copy selected rows (or all rows if none selected) as tab-separated text."""
        rows = sorted({idx.row() for idx in table.selectedIndexes()})
        if not rows:
            rows = list(range(table.rowCount()))
        if not rows:
            return

        # Header
        headers = []
        for col in range(table.columnCount()):
            h = table.horizontalHeaderItem(col)
            headers.append(h.text() if h else "")
        lines = ["\t".join(headers)]

        # Data
        for row in rows:
            cells = []
            for col in range(table.columnCount()):
                item = table.item(row, col)
                cells.append(item.text() if item else "")
            lines.append("\t".join(cells))

        clipboard = QApplication.clipboard()
        if clipboard:
            clipboard.setText("\n".join(lines))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _co_range(self) -> tuple[int, int] | None:
        return self._smu.commands.co_range if self._smu is not None else None

    def _set_running_state(self, running: bool) -> None:
        self._start_btn.setEnabled(not running)
        self._pause_btn.setEnabled(running)
        self._resume_btn.setEnabled(False)
        self._abort_btn.setEnabled(running)
        self._config_container.setEnabled(not running)
        if running:
            self._validate_btn.setEnabled(False)
            self._export_btn.setEnabled(False)
        self.tuner_running_changed.emit(running)

    def _get_backend(self, name: str | None = None) -> StressBackend | None:
        backend_name = name or self._backend_combo.currentText()
        if self._backend_factory:
            backend = self._backend_factory(backend_name)
        else:
            from corecycler.engine.backends import get_backend

            try:
                backend = get_backend(backend_name)
            except KeyError:
                QMessageBox.warning(self, "Error", f"Unknown backend: {backend_name}")
                return None

        if backend and not backend.is_available() and not ensure_tool(self, backend_name):
            return None
        return backend

    def _check_resume(self) -> None:
        """Check for active tuner sessions on startup."""
        if not self._db:
            return
        sessions = self._db.list_recoverable_tuner_sessions()
        if sessions:
            in_flight = [s for s in sessions if s.status in RESUMABLE_STATUSES]
            if len(in_flight) == 1:
                text = f"Status: RECOVERABLE SESSION #{in_flight[0].id} - click Resume to continue"
            elif in_flight:
                text = f"Status: {len(in_flight)} RECOVERABLE SESSIONS - click Resume to pick one"
            else:
                last = sessions[0]
                text = (
                    f"Status: LAST SESSION #{last.id} ENDED "
                    f"{status_label(last.status).upper()} \u2014 click Resume to re-open it"
                )
            self._status_label.setText(text)
            self._resume_btn.setEnabled(True)

    def set_test_running(self, running: bool) -> None:
        """Keep all tuner entry points unavailable during manual or memory stress."""
        self._external_test_running = running
        self.setEnabled(not running)
        if running:
            self._start_btn.setEnabled(False)
            self._start_btn.setToolTip("Manual test is running")
        else:
            has_active = self._engine is not None and (
                self._engine.status == "paused" or self._engine.status in ACTIVE_STATUSES
            )
            self._start_btn.setEnabled(not has_active)
            if has_active:
                self._start_btn.setToolTip("Tuner session is active - resume or abort first")
            else:
                self._start_btn.setToolTip("")

    def force_stop(self) -> None:
        """Abort the tuner engine and its worker after an unexpected error."""
        if self._engine:
            self._engine.abort()

    def shutdown(self) -> bool:
        """Stop the tuner for app exit, leaving its session paused and resumable."""
        if self._engine:
            return self._engine.shutdown()
        return True

    @property
    def is_running(self) -> bool:
        return self._engine is not None and (
            self._engine.status in ACTIVE_STATUSES or self._engine.status == "paused" or self._engine.test_in_flight
        )
