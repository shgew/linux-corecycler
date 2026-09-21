"""Live monitoring tab with package and per-core telemetry."""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass

from PySide6.QtCore import Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from corecycler.config.settings import load_settings
from corecycler.gui.style import format_mhz, format_temperature, format_volts, format_watts, theme
from corecycler.gui.widgets.charts import LiveChart
from corecycler.monitor.cpu_usage import CPUUsageReader
from corecycler.monitor.frequency import (
    read_core_frequencies,
    read_core_frequencies_dual,
    read_max_frequency,
)
from corecycler.monitor.hwmon import HWMonReader
from corecycler.monitor.msr import MSRReader
from corecycler.monitor.power import PowerMonitor
from corecycler.smu.pmtable import PMTableReader

MAX_FREQ_HISTORY = 60  # 1 minute at 1s

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MonitorSnapshot:
    frequencies: tuple[tuple[int, float, float], ...]
    tctl_c: float | None
    ccd_temperatures_c: tuple[tuple[int, float], ...]
    vcore_v: float | None
    package_watts: float | None
    usage: tuple[tuple[int, float], ...]
    stretch: tuple[tuple[int, float], ...]
    core_watts: tuple[tuple[int, float], ...]
    max_frequency_mhz: float | None
    power_limits: tuple[tuple[str, float | None, float | None, str], ...]


class _MonitorWorker(QThread):
    snapshot_ready = Signal(object)

    def __init__(self, topology, hwmon, power, msr, cpu_usage, pmtable, parent=None) -> None:
        super().__init__(parent)
        self.topology = topology
        self.hwmon = hwmon
        self.power = power
        self.msr = msr
        self.cpu_usage = cpu_usage
        self.pmtable = pmtable

    def _read(self, source: str, operation, default):
        try:
            return operation()
        except (OSError, PermissionError, RuntimeError, TypeError, ValueError) as exc:
            log.warning("%s telemetry read failed: %s", source, exc)
            return default

    def run(self) -> None:
        dual = self._read("frequency", read_core_frequencies_dual, {})
        if dual:
            frequencies = tuple(
                sorted((cpu_id, reading.actual_mhz, reading.effective_max_mhz) for cpu_id, reading in dual.items())
            )
        else:
            simple = self._read("frequency fallback", read_core_frequencies, {})
            frequencies = tuple(sorted((cpu_id, mhz, 0.0) for cpu_id, mhz in simple.items()))

        hwmon_data = self._read("hwmon", self.hwmon.read, None)
        tctl = hwmon_data.tctl_c if hwmon_data is not None else None
        ccd_temperatures = tuple(sorted(hwmon_data.ccd_temperatures_c.items())) if hwmon_data is not None else ()
        vcore = hwmon_data.vcore_v if hwmon_data is not None else None

        package_watts = self._read("power", self.power.read_power_watts, None)
        msr_available = self._read("MSR availability", self.msr.is_available, False)
        if package_watts is None and msr_available:
            package_watts = self._read("MSR package power", self.msr.read_package_power, None)

        usage = tuple(sorted(self._read("CPU usage", self.cpu_usage.read, {}).items()))
        stretch: tuple[tuple[int, float], ...] = ()
        core_watts: tuple[tuple[int, float], ...] = ()
        if msr_available and self.topology:
            cpus = tuple(core.logical_cpus[0] for core in self.topology.cores.values() if core.logical_cpus)
            stretch_readings = self._read("MSR clock stretch", lambda: self.msr.read_clock_stretch(list(cpus)), {})
            power_readings = self._read("MSR core power", lambda: self.msr.read_core_power(list(cpus)), {})
            stretch = tuple(sorted((cpu_id, reading.stretch_pct) for cpu_id, reading in stretch_readings.items()))
            core_watts = tuple(sorted((cpu_id, reading.watts) for cpu_id, reading in power_readings.items()))

        power_limits: tuple[tuple[str, float | None, float | None, str], ...] = ()
        if self._read("PM table availability", self.pmtable.is_available, False):
            pm = self._read("PM table", self.pmtable.read, None)
            if pm is not None:
                power_limits = (
                    ("PPT", pm.ppt_value_w, pm.ppt_limit_w, "W"),
                    ("TDC", pm.tdc_value_a, pm.tdc_limit_a, "A"),
                    ("EDC", pm.edc_value_a, pm.edc_limit_a, "A"),
                )

        snapshot = MonitorSnapshot(
            frequencies=frequencies,
            tctl_c=tctl,
            ccd_temperatures_c=ccd_temperatures,
            vcore_v=vcore,
            package_watts=package_watts,
            usage=usage,
            stretch=stretch,
            core_watts=core_watts,
            max_frequency_mhz=self._read("maximum frequency", read_max_frequency, None),
            power_limits=power_limits,
        )
        self.snapshot_ready.emit(snapshot)


class CoreFreqBar(QWidget):
    """Compact per-core frequency bar with sparkline history."""

    def __init__(self, core_id: int, label: str, max_freq: float = 6000) -> None:
        super().__init__()
        self.core_id = core_id
        self._label = label
        self._max_freq = max_freq
        self._freq: float = 0
        self._eff_max: float = 0  # per-core boost ceiling (scaling_max_freq)
        self._temp: float | None = None
        self._usage_pct: float = 0
        self._stretch_pct: float | None = None
        self._core_watts: float | None = None
        self._is_active: bool = False  # currently being tested
        self._history: deque[float] = deque(maxlen=MAX_FREQ_HISTORY)
        self.setFixedHeight(24)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_active(self, active: bool) -> None:
        if self._is_active != active:
            self._is_active = active
            self.update()

    def update_data(
        self,
        freq: float,
        temp: float | None = None,
        usage_pct: float = 0,
        stretch_pct: float | None = None,
        core_watts: float | None = None,
        eff_max_mhz: float = 0,
    ) -> None:
        self._freq = freq
        self._temp = temp
        self._usage_pct = usage_pct
        self._stretch_pct = stretch_pct
        self._core_watts = core_watts
        if eff_max_mhz > 0:
            self._eff_max = eff_max_mhz
        self._history.append(freq)
        self.update()

    @staticmethod
    def _freq_text(freq: float, eff_max: float) -> str:
        """Per-core frequency readout, always unit-labelled.

        An idle core (freq <= 0) shows "-- MHz" with no ceiling, because
        "--/<max>" reads as a false live maximum. A live core shows its current
        MHz, with the boost ceiling appended only when it is known.
        """
        if freq <= 0:
            return "  -- MHz"
        if eff_max > 0:
            return f"{format_mhz(freq).removesuffix(' MHz')}/{format_mhz(eff_max)}"
        return format_mhz(freq)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        # Background is highlighted if this core is being tested.
        bg = QColor(theme.BG_ACTIVE_TINT) if self._is_active else QColor(theme.BG_PANEL_DARK)
        painter.fillRect(0, 0, w, h, bg)

        # label area
        label_w = 40
        label_color = QColor(theme.COLOR_ACTIVE) if self._is_active else QColor(theme.COLOR_TEXT_DIM)
        painter.setPen(label_color)
        painter.setFont(QFont("monospace", 7, QFont.Weight.Bold))
        painter.drawText(4, 0, label_w - 4, h, Qt.AlignmentFlag.AlignVCenter, self._label)

        # frequency bar
        bar_x = label_w
        text_area_w = 260  # usage + actual/expected + stretch + watts + temp
        bar_w = w - label_w - text_area_w
        if bar_w > 0 and self._max_freq > 0:
            fill_ratio = min(self._freq / self._max_freq, 1.0)

            # color: blue at low, cyan at mid, green at high
            if fill_ratio < 0.5:
                color = QColor(theme.COLOR_BLUE_DEEP)
            elif fill_ratio < 0.8:
                color = QColor(theme.COLOR_ACTIVE)
            else:
                color = QColor(theme.COLOR_PASS)

            # bar background
            painter.fillRect(bar_x, 3, bar_w, h - 6, QColor(theme.BG_PANEL))
            # filled portion
            fill_w = int(bar_w * fill_ratio)
            if fill_w > 0:
                painter.fillRect(bar_x, 3, fill_w, h - 6, color)

            # Draw the per-core boost ceiling as a yellow dashed line.
            if self._eff_max > 0:
                eff_ratio = min(self._eff_max / self._max_freq, 1.0)
                marker_x = int(bar_x + bar_w * eff_ratio)
                pen = QPen(QColor(theme.COLOR_WARN_SOFT), 1, Qt.PenStyle.DashLine)
                painter.setPen(pen)
                painter.drawLine(marker_x, 3, marker_x, h - 3)

            # sparkline overlay
            if len(self._history) > 1:
                data = list(self._history)
                pen = QPen(QColor(255, 255, 255, 80), 1)
                painter.setPen(pen)
                n = len(data)
                for i in range(1, n):
                    x0 = bar_x + ((i - 1) / max(n - 1, 1)) * bar_w
                    x1 = bar_x + (i / max(n - 1, 1)) * bar_w
                    y0 = 3 + (1.0 - min(data[i - 1] / self._max_freq, 1.0)) * (h - 6)
                    y1 = 3 + (1.0 - min(data[i] / self._max_freq, 1.0)) * (h - 6)
                    painter.drawLine(int(x0), int(y0), int(x1), int(y1))

        # Keep fixed-width value columns aligned on the right.
        text_x = bar_x + bar_w + 4 if bar_w > 0 else label_w
        mono = QFont("monospace", 7)
        painter.setFont(mono)
        fm = painter.fontMetrics()
        col_gap = fm.horizontalAdvance(" ")

        # Column definitions: (text, color, fixed_chars)
        # Usage
        usage_str = f"{self._usage_pct:3.0f}%"
        usage_color = QColor(theme.COLOR_PASS) if self._usage_pct > 50 else QColor(theme.COLOR_MUTED)

        # The live boost ceiling also has a dashed bar marker.
        freq_str = self._freq_text(self._freq, self._eff_max)
        freq_color = QColor(theme.COLOR_ACTIVE)

        # Reserve a fixed stretch slot to keep columns aligned while idle.
        if self._stretch_pct is not None and self._usage_pct > 5:
            stretch_str = f"N:{self._stretch_pct:4.1f}%"
            if self._stretch_pct > 3.0:
                stretch_color = QColor(theme.CHART_TEMP)
            elif self._stretch_pct > 1.0:
                stretch_color = QColor(theme.COLOR_WARN_SOFT)
            else:
                stretch_color = QColor(theme.COLOR_MUTED_DARK)
        else:
            stretch_str = "       "  # 7 chars placeholder
            stretch_color = QColor(theme.COLOR_MUTED_DARK)

        # Power
        if self._core_watts is not None:
            watts_str = f"{self._core_watts:5.1f}W"
            watts_color = QColor(theme.COLOR_WARN_SOFT)
        else:
            watts_str = "      "  # 6 chars placeholder
            watts_color = QColor(theme.COLOR_MUTED_DARK)

        # Temperature
        if self._temp is not None:
            temp_str = f"{self._temp:3.0f}C"
            if self._temp >= 85:
                temp_color = QColor(theme.CHART_TEMP)
            elif self._temp >= 70:
                temp_color = QColor(theme.COLOR_WARN_SOFT)
            else:
                temp_color = QColor(theme.COLOR_MUTED)
        else:
            temp_str = "    "  # 4 chars placeholder
            temp_color = QColor(theme.COLOR_MUTED)

        # Draw each column at fixed positions
        cols = [
            (usage_str, usage_color),
            (freq_str, freq_color),
            (stretch_str, stretch_color),
            (watts_str, watts_color),
            (temp_str, temp_color),
        ]
        cx = text_x
        for text, color in cols:
            tw = fm.horizontalAdvance(text)
            painter.setPen(color)
            painter.drawText(cx, 0, tw + col_gap, h, Qt.AlignmentFlag.AlignVCenter, text)
            cx += tw + col_gap

        # Highlight the border while this core is active.
        border_color = QColor(theme.COLOR_ACTIVE) if self._is_active else QColor(theme.BORDER_DARKER)
        border_width = 2 if self._is_active else 1
        painter.setPen(QPen(border_color, border_width))
        painter.drawRect(0, 0, w - 1, h - 1)

        painter.end()


class MonitorTab(QWidget):
    """Live system monitoring with package charts + per-core view toggle."""

    _NORMAL_STYLE = "font: bold 11px monospace; padding: 2px;"

    def __init__(self, topology=None) -> None:
        super().__init__()
        self._topology = topology
        self._hwmon = HWMonReader()
        self._power = PowerMonitor()
        self._msr = MSRReader()
        self._cpu_usage = CPUUsageReader()
        self._pmtable = PMTableReader()
        self._per_core_bars: dict[int, CoreFreqBar] = {}
        self._per_core_visible = False
        self._max_core_freq = 6000.0
        self._setup_ui()
        self._worker = _MonitorWorker(
            topology,
            self._hwmon,
            self._power,
            self._msr,
            self._cpu_usage,
            self._pmtable,
            parent=self,
        )
        self._worker.snapshot_ready.connect(self._apply_snapshot)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._update)
        settings = load_settings()
        self._timer.start(int(settings.poll_interval * 1000))

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # top bar: current values + view toggle
        top_bar = QHBoxLayout()
        top_bar.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        values_group = QGroupBox("Current Values")
        values_layout = QGridLayout(values_group)
        values_layout.setContentsMargins(4, 4, 4, 4)

        self._tctl_label = QLabel("Tctl: --°C")
        self._ccd_temp_labels: dict[int, QLabel] = {}
        self._vcore_label = QLabel("Vcore: --V")
        self._power_label = QLabel("Package: --W")
        self._max_freq_label = QLabel("Max Boost: --MHz")

        self._ppt_label = QLabel("PPT: --/--W")
        self._tdc_label = QLabel("TDC: --/--A")
        self._edc_label = QLabel("EDC: --/--A")

        row0_labels = [
            self._tctl_label,
            self._vcore_label,
            self._power_label,
            self._max_freq_label,
        ]
        for i, label in enumerate(row0_labels):
            label.setStyleSheet("font: bold 11px monospace; padding: 2px;")
            values_layout.addWidget(label, 0, i)
        for i, label in enumerate((self._ppt_label, self._tdc_label, self._edc_label)):
            label.setStyleSheet("font: bold 11px monospace; padding: 2px;")
            values_layout.addWidget(label, 2, i)

        top_bar.addWidget(values_group, 1)

        self._toggle_btn = QPushButton("Per-Core View")
        self._toggle_btn.setCheckable(True)
        self._toggle_btn.setFixedSize(110, 36)
        self._toggle_btn.setStyleSheet(
            "QPushButton { padding: 6px; } "
            f"QPushButton:checked {{ background: {theme.BG_SELECTED}; "
            f"color: {theme.COLOR_ON_SELECTED}; border: 1px solid {theme.COLOR_ACTIVE}; }}"
        )
        self._toggle_btn.toggled.connect(self._toggle_view)
        top_bar.addWidget(self._toggle_btn)

        layout.addLayout(top_bar)

        # package charts view
        self._charts_widget = QWidget()
        charts_layout = QGridLayout(self._charts_widget)
        charts_layout.setContentsMargins(0, 0, 0, 0)
        charts_layout.setSpacing(4)

        self._freq_chart = LiveChart("Frequency", "MHz", 0, 6000, "CHART_FREQ")
        self._temp_chart = LiveChart("Temperature", "°C", 0, 100, "CHART_TEMP")
        self._power_chart = LiveChart("Package Power", "W", 0, 250, "CHART_POWER")
        self._voltage_chart = LiveChart("Vcore", "V", 0.5, 1.6, "CHART_VOLT")

        charts_layout.addWidget(self._freq_chart, 0, 0)
        charts_layout.addWidget(self._temp_chart, 0, 1)
        charts_layout.addWidget(self._power_chart, 1, 0)
        charts_layout.addWidget(self._voltage_chart, 1, 1)

        layout.addWidget(self._charts_widget)

        # per-core view (scrollable, hidden by default)
        self._per_core_scroll = QScrollArea()
        self._per_core_scroll.setWidgetResizable(True)
        self._per_core_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._per_core_scroll.setStyleSheet("QScrollArea { border: none; }")

        per_core_container = QWidget()
        self._per_core_layout = QVBoxLayout(per_core_container)
        self._per_core_layout.setContentsMargins(0, 0, 0, 0)
        self._per_core_layout.setSpacing(2)

        self._build_per_core_bars()

        self._per_core_layout.addStretch()
        self._per_core_scroll.setWidget(per_core_container)
        self._per_core_scroll.setVisible(False)
        layout.addWidget(self._per_core_scroll)

    def _build_per_core_bars(self) -> None:
        """Create per-core frequency bars from topology or by scanning sysfs."""
        self._per_core_bars.clear()

        if self._topology:
            # group by CCD
            ccd_groups: dict[int, list] = {}
            for core in sorted(self._topology.cores.values(), key=lambda c: c.core_id):
                ccd = core.ccd if core.ccd is not None else 0
                ccd_groups.setdefault(ccd, []).append(core)

            for ccd_idx in sorted(ccd_groups.keys()):
                cores = ccd_groups[ccd_idx]
                vcache_str = " (V-Cache)" if any(c.has_vcache for c in cores) else ""
                header = QLabel(f"CCD {ccd_idx}{vcache_str}")
                header.setFont(QFont("monospace", 8, QFont.Weight.Bold))
                header.setStyleSheet(f"color: {theme.COLOR_TEXT_DIM}; padding: 1px 4px;")
                header.setFixedHeight(16)
                self._per_core_layout.addWidget(header)

                for core in cores:
                    label = f"C{core.core_id}"
                    if core.has_vcache:
                        label += "V"
                    bar = CoreFreqBar(core.core_id, label, max_freq=6000)
                    self._per_core_bars[core.core_id] = bar
                    self._per_core_layout.addWidget(bar)
        else:
            # fallback: create bars from current frequency readings
            # Label as "CPU N" (logical CPU IDs, not physical core IDs)
            freqs = read_core_frequencies()
            for cpu_id in sorted(freqs.keys()):
                bar = CoreFreqBar(cpu_id, f"CPU{cpu_id}", max_freq=6000)
                self._per_core_bars[cpu_id] = bar
                self._per_core_layout.addWidget(bar)

    def _toggle_view(self, checked: bool) -> None:
        self._per_core_visible = checked
        self._charts_widget.setVisible(not checked)
        self._per_core_scroll.setVisible(checked)
        self._toggle_btn.setText("Package View" if checked else "Per-Core View")

    def _update(self) -> None:
        if not self._worker.isRunning():
            self._worker.start()

    def _ccd_label(self, ccd_id: int) -> QLabel:
        label = self._ccd_temp_labels.get(ccd_id)
        if label is not None:
            return label
        label = QLabel()
        label.setStyleSheet(self._NORMAL_STYLE)
        values_group = self._tctl_label.parent()
        if values_group and values_group.layout():
            values_group.layout().addWidget(label, 1, len(self._ccd_temp_labels))
        self._ccd_temp_labels[ccd_id] = label
        return label

    @Slot(object)
    def _apply_snapshot(self, snapshot: MonitorSnapshot) -> None:
        freqs = {cpu_id: actual for cpu_id, actual, _ceiling in snapshot.frequencies}
        eff_max = {cpu_id: ceiling for cpu_id, _actual, ceiling in snapshot.frequencies}
        usage = dict(snapshot.usage)
        stretch = dict(snapshot.stretch)
        core_watts = dict(snapshot.core_watts)
        ccd_temperatures = dict(snapshot.ccd_temperatures_c)

        sample_max = max(freqs.values(), default=None)
        advertised_max = snapshot.max_frequency_mhz
        observed_max = max((value for value in (sample_max, advertised_max) if value is not None), default=None)
        if observed_max is not None and observed_max > self._max_core_freq:
            self._max_core_freq = observed_max
            self._freq_chart.max_val = observed_max * 1.1
        self._max_freq_label.setText(f"Max Boost: {format_mhz(observed_max)}")
        if sample_max is not None:
            self._freq_chart.add_value(sample_max)

        self._tctl_label.setText(f"Tctl: {format_temperature(snapshot.tctl_c)}")
        if snapshot.tctl_c is not None:
            self._temp_chart.add_value(snapshot.tctl_c)
        self._vcore_label.setText(f"Vcore: {format_volts(snapshot.vcore_v)}")
        if snapshot.vcore_v is not None:
            self._voltage_chart.add_value(snapshot.vcore_v)
        self._power_label.setText(f"Package: {format_watts(snapshot.package_watts)}")
        if snapshot.package_watts is not None:
            self._power_chart.add_value(snapshot.package_watts)

        expected_ccds = set(ccd_temperatures)
        if self._topology:
            expected_ccds.update(core.ccd for core in self._topology.cores.values() if core.ccd is not None)
        for ccd_id in sorted(expected_ccds):
            vcache = bool(
                self._topology and any(core.ccd == ccd_id and core.has_vcache for core in self._topology.cores.values())
            )
            tag = " VC" if vcache else ""
            self._ccd_label(ccd_id).setText(f"CCD{ccd_id}{tag}: {format_temperature(ccd_temperatures.get(ccd_id))}")

        labels = {"PPT": self._ppt_label, "TDC": self._tdc_label, "EDC": self._edc_label}
        values = {name: (value, limit, unit) for name, value, limit, unit in snapshot.power_limits}
        for name, label in labels.items():
            value, limit, unit = values.get(name, (None, None, ""))
            if value is None or limit is None or limit <= 0:
                label.setText(f"{name}: N/A")
            else:
                label.setText(f"{name}: {value:.0f}/{limit:.0f}{unit} ({value / limit * 100:.0f}%)")

        if self._topology:
            for core_id, bar in self._per_core_bars.items():
                core = self._topology.cores.get(core_id)
                if core is None or not core.logical_cpus:
                    continue
                logical_cpu = core.logical_cpus[0]
                ccd_id = core.ccd if core.ccd is not None else 0
                core_temp = ccd_temperatures.get(ccd_id, snapshot.tctl_c)
                bar.update_data(
                    freqs.get(logical_cpu, 0.0),
                    core_temp,
                    usage_pct=min(sum(usage.get(cpu, 0.0) for cpu in core.logical_cpus), 100.0),
                    stretch_pct=stretch.get(logical_cpu),
                    core_watts=core_watts.get(logical_cpu),
                    eff_max_mhz=eff_max.get(logical_cpu, 0.0),
                )
                bar._max_freq = self._max_core_freq * 1.05
        else:
            for cpu_id, bar in self._per_core_bars.items():
                bar.update_data(freqs.get(cpu_id, 0.0), snapshot.tctl_c, usage_pct=usage.get(cpu_id, 0.0))

    def set_topology(self, topology) -> None:
        """Update topology and rebuild per-core bars."""
        self._topology = topology
        self._worker.topology = topology
        # clear existing bars
        for bar in self._per_core_bars.values():
            bar.deleteLater()
        self._per_core_bars.clear()
        # clear layout (skip stretch)
        while self._per_core_layout.count():
            item = self._per_core_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._build_per_core_bars()
        self._per_core_layout.addStretch()

    def set_active_core(self, core_id: int | None) -> None:
        """Highlight the core currently being tested (None to clear)."""
        for cid, bar in self._per_core_bars.items():
            bar.set_active(cid == core_id)

    def stop_monitoring(self) -> None:
        self._timer.stop()
        if self._worker.isRunning():
            self._worker.wait()
        self._msr.close()
