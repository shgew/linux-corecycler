"""Test configuration tab for backend, mode, timing, core selection, and presets."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from corecycler.config.settings import TestProfile
from corecycler.engine.backends import available_backends, get_backend
from corecycler.gui.style import set_semantic_style, theme

if TYPE_CHECKING:
    from corecycler.engine.topology import CPUTopology


@dataclass(frozen=True, slots=True)
class TestPreset:
    summary: str
    seconds_per_core: int | None = None
    cycle_count: int | None = None
    variable_load: bool | None = None
    idle_stability_test: int | None = None
    idle_between_cores: int | None = None

    @property
    def description(self) -> str:
        if self.seconds_per_core is None or self.cycle_count is None:
            return self.summary
        minutes = self.seconds_per_core // 60
        cycles = "cycle" if self.cycle_count == 1 else "cycles"
        return f"{minutes} min/core, {self.cycle_count} {cycles} - {self.summary}"


TEST_PRESETS: dict[str, TestPreset] = {
    "CUSTOM": TestPreset("Configure all settings manually"),
    "QUICK": TestPreset("fast screening, lower sensitivity", 120, 1, False, 0, 0),
    "STANDARD": TestPreset("good starting point for CO tuning", 600, 1, False, 0, 0),
    "THOROUGH": TestPreset("catches intermittent errors", 1800, 2, False, 0, 5),
    "FULL_SPECTRUM": TestPreset("most comprehensive, tests all real-world scenarios", 1200, 3, True, 120, 10),
}


class ConfigTab(QWidget):
    """Configuration panel for stress test settings."""

    def __init__(self, topology: CPUTopology | None = None) -> None:
        super().__init__()
        self._topology = topology
        self._building = True
        self._defaults = TestProfile()
        self._setup_ui()
        self.set_profile(self._defaults)

    def _setup_ui(self) -> None:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setSpacing(12)

        # test mode preset
        mode_group = QGroupBox("Test Mode")
        mode_layout = QVBoxLayout(mode_group)

        mode_row = QHBoxLayout()
        self._mode_combo = QComboBox()
        for preset_name in TEST_PRESETS:
            self._mode_combo.addItem(preset_name)
        self._mode_combo.setCurrentText(self._defaults.test_mode)
        self._mode_combo.currentTextChanged.connect(self._on_mode_change)
        mode_row.addWidget(QLabel("Preset:"))
        mode_row.addWidget(self._mode_combo, 1)
        mode_layout.addLayout(mode_row)

        self._mode_desc = QLabel(TEST_PRESETS[self._defaults.test_mode].description)
        self._mode_desc.setWordWrap(True)
        set_semantic_style(self._mode_desc, lambda: f"color: {theme.COLOR_MUTED_DARKER}; padding: 4px")
        mode_layout.addWidget(self._mode_desc)

        layout.addWidget(mode_group)

        # stress test backend
        backend_group = QGroupBox("Stress Test Backend")
        backend_layout = QFormLayout(backend_group)

        self._backend_combo = QComboBox()
        self._backend_combo.addItems(available_backends())
        self._backend_combo.currentTextChanged.connect(self._on_backend_change)
        backend_layout.addRow("Backend:", self._backend_combo)

        self._stress_mode_combo = QComboBox()
        self._stress_mode_combo.currentTextChanged.connect(self._on_change)
        backend_layout.addRow("Stress Mode:", self._stress_mode_combo)

        self._fft_combo = QComboBox()
        self._fft_combo.currentTextChanged.connect(self._on_fft_change)
        backend_layout.addRow("FFT Preset:", self._fft_combo)

        # custom FFT range
        fft_range_widget = QWidget()
        fft_range_layout = QHBoxLayout(fft_range_widget)
        fft_range_layout.setContentsMargins(0, 0, 0, 0)
        self._fft_min_spin = QSpinBox()
        self._fft_min_spin.setRange(4, 65536)
        self._fft_min_spin.setValue(self._defaults.fft_min or 4)
        self._fft_min_spin.setSuffix("K")
        self._fft_min_spin.valueChanged.connect(self._on_fft_range_change)
        self._fft_max_spin = QSpinBox()
        self._fft_max_spin.setRange(4, 65536)
        self._fft_max_spin.setValue(self._defaults.fft_max or 8192)
        self._fft_max_spin.setSuffix("K")
        self._fft_max_spin.valueChanged.connect(self._on_fft_range_change)
        fft_range_layout.addWidget(QLabel("Min:"))
        fft_range_layout.addWidget(self._fft_min_spin)
        fft_range_layout.addWidget(QLabel("Max:"))
        fft_range_layout.addWidget(self._fft_max_spin)
        self._fft_range_widget = fft_range_widget
        self._fft_range_widget.setVisible(False)
        backend_layout.addRow("Custom Range:", self._fft_range_widget)

        self._threads_spin = QSpinBox()
        self._threads_spin.setRange(1, 2)
        self._threads_spin.setValue(self._defaults.threads)
        self._threads_spin.valueChanged.connect(self._on_change)
        backend_layout.addRow("Threads:", self._threads_spin)

        layout.addWidget(backend_group)

        # timing
        timing_group = QGroupBox("Timing")
        timing_layout = QFormLayout(timing_group)

        self._time_spin = QSpinBox()
        self._time_spin.setRange(10, 86400)
        self._time_spin.setValue(self._defaults.seconds_per_core)
        self._time_spin.setSuffix(" seconds")
        self._time_spin.valueChanged.connect(self._on_change)
        timing_layout.addRow("Time per core:", self._time_spin)

        self._cycles_spin = QSpinBox()
        self._cycles_spin.setRange(1, 100)
        self._cycles_spin.setValue(self._defaults.cycle_count)
        self._cycles_spin.valueChanged.connect(self._on_change)
        timing_layout.addRow("Cycles:", self._cycles_spin)

        layout.addWidget(timing_group)

        # safety
        safety_group = QGroupBox("Safety")
        safety_layout = QFormLayout(safety_group)

        self._max_temp_spin = QDoubleSpinBox()
        self._max_temp_spin.setRange(50.0, 115.0)
        self._max_temp_spin.setValue(self._defaults.max_temperature)
        self._max_temp_spin.setSuffix(" °C")
        self._max_temp_spin.setDecimals(1)
        self._max_temp_spin.valueChanged.connect(self._on_change)
        safety_layout.addRow("Max temperature:", self._max_temp_spin)

        layout.addWidget(safety_group)

        # advanced testing options
        advanced_group = QGroupBox("Advanced Testing")
        advanced_layout = QFormLayout(advanced_group)

        self._variable_load = QCheckBox("Variable load testing (stop/start stress periodically)")
        self._variable_load.setToolTip(
            "Simulates real-world load transitions. CO instability often manifests "
            "during frequency/voltage transitions, not under steady load."
        )
        self._variable_load.stateChanged.connect(self._on_change)
        advanced_layout.addRow(self._variable_load)

        self._idle_stability_spin = QSpinBox()
        self._idle_stability_spin.setRange(0, 300)
        self._idle_stability_spin.setValue(0)
        self._idle_stability_spin.setSuffix(" seconds")
        self._idle_stability_spin.setToolTip(
            "Time to monitor each core at idle after stress. Catches errors during "
            "C-state transitions, the primary cause of CO-related crashes in daily use."
        )
        self._idle_stability_spin.valueChanged.connect(self._on_change)
        advanced_layout.addRow("Idle stability test:", self._idle_stability_spin)

        self._idle_between_spin = QSpinBox()
        self._idle_between_spin.setRange(0, 60)
        self._idle_between_spin.setValue(0)
        self._idle_between_spin.setSuffix(" seconds")
        self._idle_between_spin.setToolTip(
            "Idle pause between testing each core. Allows the CPU to cool and "
            "return to idle voltages before testing the next core."
        )
        self._idle_between_spin.valueChanged.connect(self._on_change)
        advanced_layout.addRow("Idle between cores:", self._idle_between_spin)

        layout.addWidget(advanced_group)

        # behavior
        behavior_group = QGroupBox("Behavior")
        behavior_layout = QFormLayout(behavior_group)

        self._stop_on_error = QCheckBox("Stop testing when first error occurs")
        self._stop_on_error.stateChanged.connect(self._on_change)
        behavior_layout.addRow(self._stop_on_error)

        layout.addWidget(behavior_group)

        # core selection
        cores_group = QGroupBox("Core Selection")
        cores_layout = QVBoxLayout(cores_group)

        cores_layout.addWidget(QLabel("Leave empty to test all cores. Comma-separated core IDs:"))
        self._cores_input = QLineEdit()
        self._cores_input.setPlaceholderText("e.g., 0,1,4,5 (physical core IDs)")
        self._cores_input.textChanged.connect(self._on_cores_changed)
        cores_layout.addWidget(self._cores_input)

        self._cores_error_label = QLabel("")
        set_semantic_style(
            self._cores_error_label,
            lambda: f"color: {theme.COLOR_FAIL}; font-size: 10px; padding: 2px",
        )
        self._cores_error_label.setVisible(False)
        cores_layout.addWidget(self._cores_error_label)

        self._retest_failed_btn = QPushButton("Retest Failed Cores Only")
        self._retest_failed_btn.setToolTip(
            "After a test run, populate the core selection with only the cores that failed and skip stable cores."
        )
        self._retest_failed_btn.setEnabled(False)
        self._retest_failed_btn.clicked.connect(self._on_retest_failed)
        cores_layout.addWidget(self._retest_failed_btn)

        layout.addWidget(cores_group)
        layout.addStretch()

        scroll.setWidget(content)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

    def _on_mode_change(self, mode_name: str) -> None:
        preset = TEST_PRESETS[mode_name]
        self._mode_desc.setText(preset.description)
        if preset.seconds_per_core is None:
            self._on_change()
            return

        self._building = True
        self._time_spin.setValue(preset.seconds_per_core)
        self._cycles_spin.setValue(preset.cycle_count)
        self._variable_load.setChecked(preset.variable_load)
        self._idle_stability_spin.setValue(preset.idle_stability_test)
        self._idle_between_spin.setValue(preset.idle_between_cores)
        self._building = False
        self._on_change()

    def _on_backend_change(self, backend_name: str) -> None:
        try:
            backend = get_backend(backend_name)
        except KeyError:
            return
        selected_mode = self._stress_mode_combo.currentText()
        selected_fft = self._fft_combo.currentText()
        modes = [mode.name for mode in backend.get_supported_modes()]
        presets = [preset.name for preset in backend.get_supported_fft_presets()]
        self._stress_mode_combo.blockSignals(True)
        self._stress_mode_combo.clear()
        self._stress_mode_combo.addItems(modes)
        self._stress_mode_combo.setCurrentText(selected_mode if selected_mode in modes else modes[0])
        self._stress_mode_combo.blockSignals(False)
        self._fft_combo.blockSignals(True)
        self._fft_combo.clear()
        self._fft_combo.addItems(presets)
        if selected_fft in presets:
            self._fft_combo.setCurrentText(selected_fft)
        self._fft_combo.blockSignals(False)
        fft_available = bool(presets)
        self._fft_combo.setVisible(fft_available)
        label = self._fft_combo.parentWidget().layout().labelForField(self._fft_combo)
        if label is not None:
            label.setVisible(fft_available)
        self._on_fft_change()

    def _on_fft_change(self) -> None:
        is_custom = self._fft_combo.currentText() == "CUSTOM"
        self._fft_range_widget.setVisible(is_custom)
        self._on_change()

    def _on_fft_range_change(self) -> None:
        """Enforce fft_min <= fft_max."""
        if self._fft_min_spin.value() > self._fft_max_spin.value():
            self._building = True
            self._fft_max_spin.setValue(self._fft_min_spin.value())
            self._building = False
        self._on_change()

    def _on_cores_changed(self) -> None:
        cores_text = self._cores_input.text().strip()
        if not cores_text or not cores_text.rstrip(",").strip():
            self._cores_error_label.setVisible(False)
            self._on_change()
            return

        values = [part.strip() for part in cores_text.rstrip(",").split(",") if part.strip()]
        try:
            cores = [int(value) for value in values]
        except ValueError:
            self._cores_error_label.setText("Invalid core list: use comma-separated integers")
            self._cores_error_label.setVisible(True)
            self._on_change()
            return
        if len(cores) != len(set(cores)):
            self._cores_error_label.setText("Duplicate core IDs are not allowed")
            self._cores_error_label.setVisible(True)
            self._on_change()
            return
        valid_ids = set(self._topology.cores) if self._topology else set(cores)
        invalid = [core for core in cores if core not in valid_ids]
        if invalid:
            max_id = max(valid_ids) if valid_ids else 0
            self._cores_error_label.setText(
                f"Core(s) {', '.join(str(core) for core in invalid)} out of range (valid: 0-{max_id})"
            )
            self._cores_error_label.setVisible(True)
            self._on_change()
            return
        self._cores_error_label.setVisible(False)
        self._on_change()

    def _on_change(self) -> None:
        if self._building:
            return
        # Auto-switch to CUSTOM when user changes a preset-controlled parameter
        if self._mode_combo.currentText() != "CUSTOM":
            self._building = True
            self._mode_combo.setCurrentText("CUSTOM")
            self._mode_desc.setText(TEST_PRESETS["CUSTOM"].description)
            self._building = False

    def _validation_errors(self, profile: TestProfile) -> list[str]:
        try:
            backend = get_backend(profile.backend)
        except KeyError:
            return [f"Unknown backend: {profile.backend}"]
        errors: list[str] = []
        modes = {mode.name for mode in backend.get_supported_modes()}
        presets = {preset.name for preset in backend.get_supported_fft_presets()}
        if profile.stress_mode not in modes:
            errors.append(f"Unsupported stress mode {profile.stress_mode} for backend {profile.backend}")
        if presets and profile.fft_preset not in presets:
            errors.append(f"Unsupported FFT preset {profile.fft_preset} for backend {profile.backend}")
        bounds = {
            "threads": (profile.threads, 1, 2),
            "seconds_per_core": (profile.seconds_per_core, 10, 86400),
            "cycle_count": (profile.cycle_count, 1, 100),
            "max_temperature": (profile.max_temperature, 50.0, 115.0),
            "idle_stability_test": (profile.idle_stability_test, 0.0, 300.0),
            "idle_between_cores": (profile.idle_between_cores, 0.0, 60.0),
        }
        for name, (value, minimum, maximum) in bounds.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not minimum <= value <= maximum:
                errors.append(f"{name} must be between {minimum} and {maximum}")
        if profile.test_mode not in TEST_PRESETS:
            errors.append(f"Unknown test preset: {profile.test_mode}")
        if profile.cores_to_test is not None:
            if any(isinstance(core, bool) or not isinstance(core, int) for core in profile.cores_to_test):
                errors.append("Core IDs must be integers")
            elif len(profile.cores_to_test) != len(set(profile.cores_to_test)):
                errors.append("Duplicate core IDs are not allowed")
            elif self._topology and any(core not in self._topology.cores for core in profile.cores_to_test):
                errors.append("Core IDs must exist in the detected topology")
        if profile.fft_preset == "CUSTOM" and (
            profile.fft_min is None or profile.fft_max is None or not 4 <= profile.fft_min <= profile.fft_max <= 65536
        ):
            errors.append("Custom FFT range must be ordered between 4K and 65536K")
        return errors

    def get_profile(self) -> TestProfile:
        if not self._cores_error_label.isHidden():
            raise ValueError(self._cores_error_label.text())
        cores_text = self._cores_input.text().strip().rstrip(",").strip()
        cores = [int(part.strip()) for part in cores_text.split(",") if part.strip()] if cores_text else None
        profile = TestProfile(
            backend=self._backend_combo.currentText(),
            stress_mode=self._stress_mode_combo.currentText(),
            fft_preset=self._fft_combo.currentText() or self._defaults.fft_preset,
            fft_min=self._fft_min_spin.value() if self._fft_combo.currentText() == "CUSTOM" else None,
            fft_max=self._fft_max_spin.value() if self._fft_combo.currentText() == "CUSTOM" else None,
            threads=self._threads_spin.value(),
            seconds_per_core=self._time_spin.value(),
            cycle_count=self._cycles_spin.value(),
            stop_on_error=self._stop_on_error.isChecked(),
            cores_to_test=cores,
            max_temperature=self._max_temp_spin.value(),
            test_mode=self._mode_combo.currentText(),
            variable_load=self._variable_load.isChecked(),
            idle_stability_test=self._idle_stability_spin.value(),
            idle_between_cores=self._idle_between_spin.value(),
        )
        errors = self._validation_errors(profile)
        if errors:
            raise ValueError("; ".join(errors))
        return profile

    def set_profile(self, profile: TestProfile) -> None:
        errors = self._validation_errors(profile)
        if errors:
            raise ValueError("; ".join(errors))
        self._building = True
        self._backend_combo.setCurrentText(profile.backend)
        self._on_backend_change(profile.backend)
        self._stress_mode_combo.setCurrentText(profile.stress_mode)
        if self._fft_combo.count():
            self._fft_combo.setCurrentText(profile.fft_preset)
        if profile.fft_min is not None:
            self._fft_min_spin.setValue(profile.fft_min)
        if profile.fft_max is not None:
            self._fft_max_spin.setValue(profile.fft_max)
        self._threads_spin.setValue(profile.threads)
        self._time_spin.setValue(profile.seconds_per_core)
        self._cycles_spin.setValue(profile.cycle_count)
        self._stop_on_error.setChecked(profile.stop_on_error)
        self._cores_input.setText(",".join(str(core) for core in profile.cores_to_test or []))
        self._max_temp_spin.setValue(profile.max_temperature)
        self._mode_combo.setCurrentText(profile.test_mode)
        self._mode_desc.setText(TEST_PRESETS[profile.test_mode].description)
        self._variable_load.setChecked(profile.variable_load)
        self._idle_stability_spin.setValue(int(profile.idle_stability_test))
        self._idle_between_spin.setValue(int(profile.idle_between_cores))
        self._on_fft_change()
        self._building = False

    def set_failed_cores(self, failed_cores: list[int]) -> None:
        """Store failed cores from a test run and enable the retest button."""
        self._last_failed_cores = failed_cores
        self._retest_failed_btn.setEnabled(bool(failed_cores))
        if failed_cores:
            n = len(failed_cores)
            self._retest_failed_btn.setText(f"Retest {n} Failed Core{'s' if n != 1 else ''} Only")
        else:
            self._retest_failed_btn.setText("Retest Failed Cores Only")

    def _on_retest_failed(self) -> None:
        """Populate core selection with only the failed cores."""
        cores = getattr(self, "_last_failed_cores", [])
        if cores:
            self._cores_input.setText(",".join(str(c) for c in sorted(cores)))
