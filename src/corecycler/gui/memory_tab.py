"""Memory information tab - DIMM details and DDR5 temperature monitoring."""

from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6.QtCore import QThread, QTimer, Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from corecycler.config import tools
from corecycler.config.paths import ensure_work_dir
from corecycler.config.settings import load_settings
from corecycler.engine import execution
from corecycler.engine.backends.base import StressBackend, StressConfig, StressMode
from corecycler.engine.backends.stressapptest import default_memory_mb
from corecycler.engine.detector import ErrorDetector
from corecycler.gui.style import format_mhz, format_temperature, format_volts, theme
from corecycler.monitor.hwmon import HWMonReader
from corecycler.monitor.memory import DIMMInfo, SPD5118Reader, read_dimm_info
from corecycler.smu.pmtable import PMTableReader, compute_fclk_uclk_ratio

if TYPE_CHECKING:
    from collections.abc import Callable

log = logging.getLogger(__name__)

PART_NUMBER_COL = 6


class _MemoryStressBackend(StressBackend):
    def __init__(self, tool: str) -> None:
        super().__init__()
        self.tool = tool
        self.name = "stressapptest" if tool == "stressapptest" else "stress-ng"

    def get_command(self, config: StressConfig, work_dir) -> list[str]:
        seconds = config.test_seconds or 60
        binary = self.require_binary()
        if self.tool == "stressapptest":
            return [binary, "-W", "-M", str(config.memory_mb or default_memory_mb()), "-s", str(seconds)]
        return [binary, "--vm", "1", "--vm-bytes", "75%", "--verify", "--timeout", f"{seconds}s"]

    def parse_output(self, stdout: str, stderr: str, returncode: int) -> tuple[bool, str | None]:
        combined = f"{stdout}\n{stderr}"
        lowered = combined.lower()
        for signature in ("miscompare", "hardware error", "status: fail", "verification error", "failed"):
            if signature in lowered:
                return False, f"Memory stress reported {signature}"
        if returncode != 0:
            return False, f"Memory stress exited with code {returncode}"
        if self.tool == "stressapptest" and "status: pass" not in lowered:
            return False, "stressapptest exited without a PASS verdict"
        return True, None

    def get_supported_modes(self) -> list[StressMode]:
        return [StressMode.SSE]

    def prepare(self, work_dir, config: StressConfig) -> None:
        work_dir.mkdir(parents=True, exist_ok=True)


class _StressWorker(QThread):
    """Run one all-CPU memory lane through the shared Supervisor."""

    done = Signal(bool, str)

    def __init__(
        self,
        tool: str,
        duration_minutes: int,
        parent=None,
        *,
        supervisor_factory: Callable[..., execution.Supervisor] | None = None,
        detector_factory: Callable[[], ErrorDetector] = ErrorDetector,
    ) -> None:
        super().__init__(parent)
        self._tool = tool
        self._duration = duration_minutes
        self._supervisor_factory = supervisor_factory or execution.Supervisor
        self._detector_factory = detector_factory
        self._stop_event = threading.Event()
        self._cancelled = False

    def run(self) -> None:
        if self._tool not in {"stressapptest", "stress-ng --vm"}:
            self.done.emit(False, f"Unknown tool: {self._tool}")
            return
        try:
            seconds = self._duration * 60
            backend = _MemoryStressBackend(self._tool)
            detector = self._detector_factory()
            detector.reset()
            settings = load_settings()
            thermal = execution.ThermalWatch(
                max_temperature=settings.active_profile.max_temperature,
                grace_seconds=5.0,
                hard_margin=5.0,
                require_sensor=True,
                read=HWMonReader().max_cpu_temp,
            )
            supervisor = self._supervisor_factory(
                backend=backend,
                detector=detector,
                thermal=thermal,
                stop_event=self._stop_event,
                observed=[],
                phase="memory stress",
            )
            cpus = tuple(sorted(os.sched_getaffinity(0)))
            if not cpus:
                self.done.emit(False, "No online CPUs available")
                return
            lane = execution.Lane(core_id=0, cpus=cpus, work_dir=ensure_work_dir(settings.work_dir) / "memory-gui")
            config = StressConfig(
                mode=StressMode.SSE,
                threads=len(cpus),
                memory_mb=default_memory_mb(),
                test_seconds=seconds,
            )
            result = supervisor.run([lane], lambda _lane: config, seconds + 60.0).get(0)
            if self._cancelled:
                self.done.emit(False, "Memory stress stopped")
            elif result is None:
                self.done.emit(False, "Memory stress stopped before a verdict")
            elif result.passed:
                self.done.emit(True, "Memory stress completed")
            else:
                self.done.emit(False, result.error_message or result.error_type or "Memory stress failed")
        except (OSError, RuntimeError, ValueError) as exc:
            self.done.emit(False, str(exc))

    def stop(self) -> None:
        self._cancelled = True
        self._stop_event.set()


@dataclass(frozen=True, slots=True)
class MemorySnapshot:
    dimms: tuple[DIMMInfo, ...] | None
    spd_timings: object | None
    pm_data: object | None
    temperatures_c: tuple[float, ...]
    spd_available: bool
    pm_available: bool


class _MemoryWorker(QThread):
    snapshot_ready = Signal(object)

    def __init__(self, spd_reader: SPD5118Reader, pm_reader: PMTableReader, parent=None) -> None:
        super().__init__(parent)
        self.spd_reader = spd_reader
        self.pm_reader = pm_reader
        self.refresh_inventory = True

    @staticmethod
    def _read(source: str, operation, default):
        try:
            return operation()
        except (OSError, PermissionError, RuntimeError, TypeError, ValueError) as exc:
            log.warning("%s read failed: %s", source, exc)
            return default

    def run(self) -> None:
        dimms = None
        spd_timings = None
        if self.refresh_inventory:
            dimms = tuple(self._read("DIMM inventory", read_dimm_info, []))
            spd_timings = self._read("SPD EEPROM", lambda: self.spd_reader.spd_timings, None)
            self.refresh_inventory = False
        temperatures = tuple(self._read("SPD temperature", self.spd_reader.read_temperatures, []))
        spd_available = self._read("SPD availability", self.spd_reader.is_available, False)
        pm_available = self._read("PM table availability", self.pm_reader.is_available, False)
        pm_data = self._read("PM table", self.pm_reader.read, None) if pm_available else None
        self.snapshot_ready.emit(MemorySnapshot(dimms, spd_timings, pm_data, temperatures, spd_available, pm_available))


class MemoryTab(QWidget):
    """Memory information tab showing DIMM details and live temperatures."""

    memory_stress_started = Signal()
    memory_stress_done = Signal(bool)  # passed

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._dimms: list[DIMMInfo] = []
        self._spd_reader = SPD5118Reader()
        self._pm_reader = PMTableReader()
        self._stress_worker: _StressWorker | None = None
        self._external_test_running: bool = False
        self._setup_ui()
        self._memory_worker = _MemoryWorker(self._spd_reader, self._pm_reader, self)
        self._memory_worker.snapshot_ready.connect(self._apply_memory_snapshot)
        self._update_timer = QTimer(self)
        self._update_timer.timeout.connect(self._request_update)
        settings = load_settings()
        self._update_timer.start(int(settings.poll_interval * 1000))

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        # Memory Controller group box (PM Table data)
        self._mc_group = QGroupBox("Memory Controller (PM Table)")
        mc_layout = QVBoxLayout(self._mc_group)

        # Clock row: FCLK, UCLK, MCLK, ratio
        clk_row = QHBoxLayout()
        self._fclk_label = QLabel("FCLK: --")
        self._fclk_label.setFont(QFont("monospace", 10))
        self._uclk_label = QLabel("UCLK: --")
        self._uclk_label.setFont(QFont("monospace", 10))
        self._mclk_label = QLabel("MCLK: --")
        self._mclk_label.setFont(QFont("monospace", 10))
        self._ratio_label = QLabel("FCLK:UCLK --")
        self._ratio_label.setFont(QFont("monospace", 10, QFont.Weight.Bold))
        clk_row.addWidget(self._fclk_label)
        clk_row.addWidget(self._uclk_label)
        clk_row.addWidget(self._mclk_label)
        clk_row.addWidget(self._ratio_label)
        clk_row.addStretch()
        mc_layout.addLayout(clk_row)

        # Voltage row: VDD, VDDQ
        volt_row = QHBoxLayout()
        self._vdd_label = QLabel("VDD: --")
        self._vdd_label.setFont(QFont("monospace", 10))
        self._vddq_label = QLabel("VDDQ: --")
        self._vddq_label.setFont(QFont("monospace", 10))
        volt_row.addWidget(self._vdd_label)
        volt_row.addWidget(self._vddq_label)
        volt_row.addStretch()
        mc_layout.addLayout(volt_row)

        # Calibration status
        self._cal_label = QLabel("")
        self._cal_label.setStyleSheet(f"color: {theme.COLOR_MUTED}; font: 9px monospace;")
        mc_layout.addWidget(self._cal_label)

        # Driver-missing message (hidden by default)
        self._mc_missing_label = QLabel("Requires ryzen_smu driver")
        self._mc_missing_label.setStyleSheet(f"color: {theme.COLOR_MUTED}; font: 10px monospace; padding: 8px;")
        self._mc_missing_label.setVisible(False)
        mc_layout.addWidget(self._mc_missing_label)

        if not self._pm_reader.is_available():
            # Hide clock/voltage rows, show driver-missing message
            self._fclk_label.setVisible(False)
            self._uclk_label.setVisible(False)
            self._mclk_label.setVisible(False)
            self._ratio_label.setVisible(False)
            self._vdd_label.setVisible(False)
            self._vddq_label.setVisible(False)
            self._cal_label.setVisible(False)
            self._mc_missing_label.setVisible(True)

        layout.addWidget(self._mc_group)

        # SPD Timings group box (DDR5 EEPROM data, cached at startup)
        self._spd_group = QGroupBox("SPD Timings - JEDEC Base Profile (DDR5)")
        self._spd_group.setToolTip(
            "JEDEC base profile timings from SPD EEPROM. XMP/EXPO overclocking profiles are not stored in SPD."
        )
        spd_layout = QVBoxLayout(self._spd_group)
        self._primary_label = QLabel("Primary: --")
        self._primary_label.setFont(QFont("monospace", 10))
        self._secondary_label = QLabel("Secondary: --")
        self._secondary_label.setFont(QFont("monospace", 10))
        self._spd_unavailable_label = QLabel("")
        self._spd_unavailable_label.setStyleSheet(f"color: {theme.COLOR_MUTED}; font: 10px monospace; padding: 4px;")
        self._spd_unavailable_label.setVisible(False)
        spd_layout.addWidget(self._primary_label)
        spd_layout.addWidget(self._secondary_label)
        spd_layout.addWidget(self._spd_unavailable_label)
        layout.addWidget(self._spd_group)

        self._summary_label = QLabel("Loading DIMM information...")
        self._summary_label.setFont(QFont("monospace", 11, QFont.Weight.Bold))
        layout.addWidget(self._summary_label)

        # Dependency status
        self._deps_label = QLabel("")
        self._deps_label.setStyleSheet(f"color: {theme.COLOR_MUTED}; font: 9px monospace;")
        layout.addWidget(self._deps_label)

        self._temp_group = QGroupBox("DIMM Temperatures (SPD5118)")
        QHBoxLayout(self._temp_group)
        self._temp_labels: list[QLabel] = []
        self._temp_group.setVisible(False)
        layout.addWidget(self._temp_group)

        self._dimm_table = QTableWidget()
        self._dimm_table.setColumnCount(12)
        self._dimm_table.setHorizontalHeaderLabels(
            [
                "Slot",
                "Size",
                "Type",
                "SPD Speed",
                "Running",
                "Manufacturer",
                "Part Number",
                "Serial",
                "Rank",
                "Form",
                "SPD Rated V",
                "Width",
            ]
        )
        header = self._dimm_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(PART_NUMBER_COL, QHeaderView.ResizeMode.Stretch)
        self._dimm_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self._dimm_table)

        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._refresh_memory_info)
        layout.addWidget(refresh_btn)

        # Stress test controls
        stress_group = QGroupBox("Memory Stress Test")
        stress_layout = QHBoxLayout(stress_group)

        stress_layout.addWidget(QLabel("Duration:"))
        self._stress_duration = QSpinBox()
        self._stress_duration.setRange(1, 60)
        self._stress_duration.setValue(5)
        self._stress_duration.setSuffix(" min")
        stress_layout.addWidget(self._stress_duration)

        stress_layout.addWidget(QLabel("Tool:"))
        self._stress_tool = QComboBox()
        self._stress_tool.addItems(self._detect_available_tools())
        stress_layout.addWidget(self._stress_tool)

        self._stress_btn = QPushButton("Run")
        self._stress_btn.clicked.connect(self._run_memory_stress)
        stress_layout.addWidget(self._stress_btn)

        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setEnabled(False)
        self._stop_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.BTN_RED}; color: white; padding: 4px 10px; "
            f"border-radius: 3px; }} QPushButton:disabled {{ background: {theme.COLOR_MUTED_DARKER}; "
            f"color: {theme.COLOR_MUTED}; }}"
        )
        self._stop_btn.clicked.connect(self._stop_memory_stress)
        stress_layout.addWidget(self._stop_btn)

        self._stress_status = QLabel("")
        stress_layout.addWidget(self._stress_status)
        stress_layout.addStretch()

        layout.addWidget(stress_group)

    def _detect_available_tools(self) -> list[str]:
        """Detect which memory stress tools are installed."""
        available = []
        if tools.resolve("stressapptest").path:
            available.append("stressapptest")
        if tools.resolve("stress-ng").path:
            available.append("stress-ng --vm")
        if not available:
            available.append("(none installed)")
        return available

    def _refresh_memory_info(self) -> None:
        self._memory_worker.refresh_inventory = True
        self._request_update()

    def _request_update(self) -> None:
        if not self._memory_worker.isRunning():
            self._memory_worker.start()

    def _apply_inventory(self, dimms: tuple[DIMMInfo, ...]) -> None:
        self._dimms = list(dimms)
        self._populate_table()
        if not self._dimms:
            self._summary_label.setText("No DIMM info available (dmidecode requires root)")
            return
        total_gb = sum(d.size_gb for d in self._dimms)
        types = {d.mem_type for d in self._dimms}
        speeds = {d.configured_speed_mt for d in self._dimms if d.configured_speed_mt}
        type_str = "/".join(sorted(types)) if types else "Unknown"
        speed_str = "/".join(f"{s} MT/s" for s in sorted(speeds)) if speeds else ""
        has_ecc = any(d.total_width > d.data_width for d in self._dimms if d.total_width and d.data_width)
        ecc_str = "ECC" if has_ecc else "Non-ECC"
        ranks = {d.rank for d in self._dimms if d.rank}
        rank_str = f"{max(ranks)}R" if ranks else ""
        self._summary_label.setText(
            f"{len(self._dimms)} DIMMs | {total_gb} GB {type_str} {speed_str} {ecc_str} {rank_str}".rstrip()
        )

    def _update_dependencies(self, snapshot: MemorySnapshot) -> None:
        deps = []
        for key in ("dmidecode", "stressapptest", "stress-ng"):
            deps.append(f"{key}: " + ("found" if tools.resolve(key).path else "missing"))
        deps.append("spd5118: " + ("available" if snapshot.spd_available else "not loaded"))
        deps.append("ryzen_smu: " + ("available" if snapshot.pm_available else "not loaded"))
        self._deps_label.setText("  |  ".join(deps))

    def _update_spd_labels(self, spd) -> None:
        """Render SPD timing labels from a worker snapshot."""
        if spd is None:
            self._primary_label.setVisible(False)
            self._secondary_label.setVisible(False)
            self._spd_unavailable_label.setText("SPD Timings unavailable - spd5118 eeprom not exposed")
            self._spd_unavailable_label.setVisible(True)
            self._spd_group.setTitle("SPD Timings - JEDEC Base Profile (DDR5)")
            return

        self._primary_label.setVisible(True)
        self._secondary_label.setVisible(True)
        self._spd_unavailable_label.setVisible(False)

        dimm_num = spd.dimm_index + 1
        self._spd_group.setTitle(f"SPD Timings - JEDEC Base Profile (DDR5) (DIMM {dimm_num})")

        self._primary_label.setText(f"Primary: {spd.tCL}-{spd.tRCD}-{spd.tRP}-{spd.tRAS}-{spd.tRC}")

        parts = []
        parts.append(f"tRFC1: {spd.tRFC1_ns}ns")
        parts.append(f"tRFCsb: {spd.tRFCsb_ns}ns")
        parts.append(f"tWR: {spd.tWR_ns:.0f}ns")
        self._secondary_label.setText("Secondary: " + "  ".join(parts))

    def _populate_table(self) -> None:
        self._dimm_table.setRowCount(len(self._dimms))
        for row, d in enumerate(self._dimms):
            items = [
                f"{d.locator} ({d.bank_locator})" if d.bank_locator else d.locator,
                f"{d.size_gb} GB",
                d.mem_type,
                f"{d.speed_mt} MT/s" if d.speed_mt else "-",
                f"{d.configured_speed_mt} MT/s" if d.configured_speed_mt else "-",
                d.manufacturer,
                d.part_number,
                d.serial_number or "-",
                str(d.rank) if d.rank else "-",
                d.form_factor or "-",
                f"{d.configured_voltage:.2f}V" if d.configured_voltage else "-",
                f"{d.data_width}/{d.total_width} bit" if d.data_width else "-",
            ]
            for col, text in enumerate(items):
                self._dimm_table.setItem(row, col, QTableWidgetItem(text))

    def _update_temperatures(self, temperatures: tuple[float, ...]) -> None:
        for label in self._temp_labels:
            label.deleteLater()
        self._temp_labels.clear()
        self._temp_group.setVisible(bool(temperatures))
        layout = self._temp_group.layout()
        for index, temperature in enumerate(temperatures, start=1):
            label = QLabel(f"DIMM {index}: {format_temperature(temperature)}")
            label.setFont(QFont("monospace", 10))
            label.setStyleSheet("padding: 4px;")
            layout.addWidget(label)
            self._temp_labels.append(label)

    @Slot(object)
    def _apply_memory_snapshot(self, snapshot: MemorySnapshot) -> None:
        if snapshot.dimms is not None:
            self._apply_inventory(snapshot.dimms)
            self._update_spd_labels(snapshot.spd_timings)
        self._update_temperatures(snapshot.temperatures_c)
        self._update_dependencies(snapshot)
        pm_data = snapshot.pm_data
        if pm_data is not None and pm_data.is_calibrated:
            self._update_clock_labels(pm_data)
            self._update_voltage_labels(pm_data)
            state = "Verified" if pm_data.is_verified else "Calibrated (community-sourced, unverified)"
            self._cal_label.setText(f"PM Table v{pm_data.pm_table_version:#010x} - {state}")
        elif pm_data is not None:
            self._show_uncalibrated(pm_data)
        else:
            self._set_clocks_unavailable()

    def _update_clock_labels(self, pm_data) -> None:
        self._fclk_label.setText(f"FCLK: {format_mhz(pm_data.fclk_mhz)}")
        self._uclk_label.setText(f"UCLK: {format_mhz(pm_data.uclk_mhz)}")
        self._mclk_label.setText(f"MCLK: {format_mhz(pm_data.mclk_mhz)}")
        self._fclk_label.setStyleSheet("")
        self._uclk_label.setStyleSheet("")
        self._mclk_label.setStyleSheet("")
        ratio = compute_fclk_uclk_ratio(pm_data.fclk_mhz, pm_data.uclk_mhz)
        if ratio is not None:
            self._ratio_label.setText(f"FCLK:UCLK {ratio[0]}:{ratio[1]}")
            if ratio == (1, 1):
                self._ratio_label.setStyleSheet(f"color: {theme.COLOR_PASS};")
            else:
                self._ratio_label.setStyleSheet(f"color: {theme.COLOR_WARN_SOFT};")
        else:
            self._ratio_label.setText("FCLK:UCLK --")
            self._ratio_label.setStyleSheet("")

    def _update_voltage_labels(self, pm_data) -> None:
        if pm_data.vdd_mem_v > 0:
            self._vdd_label.setText(f"VDD: {format_volts(pm_data.vdd_mem_v)}")
            self._vdd_label.setStyleSheet("")
        else:
            self._vdd_label.setText("VDD: --")
            self._vdd_label.setStyleSheet(f"color: {theme.COLOR_MUTED};")
        if pm_data.vddq_v > 0:
            self._vddq_label.setText(f"VDDQ: {format_volts(pm_data.vddq_v)}")
            self._vddq_label.setStyleSheet("")
        else:
            self._vddq_label.setText("VDDQ: --")
            self._vddq_label.setStyleSheet(f"color: {theme.COLOR_MUTED};")

    def _show_uncalibrated(self, pm_data) -> None:
        self._fclk_label.setText("FCLK: --")
        self._uclk_label.setText("UCLK: --")
        self._mclk_label.setText("MCLK: --")
        self._ratio_label.setText("FCLK:UCLK --")
        self._ratio_label.setStyleSheet("")
        self._vdd_label.setText("VDD: --")
        self._vddq_label.setText("VDDQ: --")
        for lbl in (self._fclk_label, self._uclk_label, self._mclk_label, self._vdd_label, self._vddq_label):
            lbl.setStyleSheet(f"color: {theme.COLOR_MUTED};")
        self._cal_label.setText(
            f"PM Table v{pm_data.pm_table_version:#010x} - Uncalibrated ({len(pm_data.raw_floats)} floats)"
        )

    def _set_clocks_unavailable(self) -> None:
        for lbl in (self._fclk_label, self._uclk_label, self._mclk_label, self._vdd_label, self._vddq_label):
            lbl.setText(lbl.text().split(":")[0] + ": --")
            lbl.setStyleSheet(f"color: {theme.COLOR_MUTED};")
        self._ratio_label.setText("FCLK:UCLK --")
        self._ratio_label.setStyleSheet(f"color: {theme.COLOR_MUTED};")
        self._cal_label.setText("")

    def set_test_running(self, running: bool) -> None:
        """Disable memory stress when another test is active."""
        self._external_test_running = running
        self._stress_btn.setEnabled(not running and not (self._stress_worker and self._stress_worker.isRunning()))

    def _run_memory_stress(self) -> None:
        if self._stress_worker is not None and self._stress_worker.isRunning():
            return
        tool = self._stress_tool.currentText()
        if tool == "(none installed)":
            QMessageBox.warning(
                self,
                "Not Found",
                "No memory stress tools installed.\nInstall stressapptest or stress-ng.",
            )
            return
        duration = self._stress_duration.value()
        self._stress_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._stress_duration.setEnabled(False)
        self._stress_tool.setEnabled(False)
        self._stress_status.setText(f"Running {tool} for {duration}min...")
        self._stress_worker = _StressWorker(tool, duration, parent=self)
        self._stress_worker.done.connect(self._on_stress_done)
        self.memory_stress_started.emit()
        self._stress_worker.start()

    def _stop_memory_stress(self) -> None:
        self._stop_btn.setEnabled(False)
        """Stop the running memory stress test."""
        if self._stress_worker and self._stress_worker.isRunning():
            self._stress_status.setText("Stopping...")
            self._stress_worker.stop()

    def force_stop(self) -> None:
        """Stop any running memory stress test on app exit."""
        if self._stress_worker and self._stress_worker.isRunning():
            self._stress_worker.stop()
            self._stress_worker.wait(3000)

    @Slot(bool, str)
    def _on_stress_done(self, passed: bool, output: str) -> None:
        self._stress_btn.setEnabled(not self._external_test_running)
        self._stop_btn.setEnabled(False)
        self._stress_duration.setEnabled(True)
        self._stress_tool.setEnabled(True)
        status = "PASS" if passed else "FAIL"
        self._stress_status.setText(f"Result: {status}")
        self.memory_stress_done.emit(passed)
        # Strip ANSI escape sequences before displaying
        clean_output = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", output[-500:])
        QMessageBox.information(self, f"Memory Stress: {status}", clean_output)
