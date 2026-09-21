"""ConfigTab behaviour + adversarial input injection.

The config tab produces the TestProfile that drives every stress run, so its
core-id parser, preset logic, and FFT-range handling must never crash or emit a
malformed profile on hostile input.
"""

from __future__ import annotations

import sys as _sys

import pytest

if not hasattr(_sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("GUI tests require real PySide6", allow_module_level=True)

from corecycler.config.settings import TestProfile
from corecycler.engine.backends import load_all
from corecycler.engine.topology import CPUTopology, PhysicalCore


def _qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _topo() -> CPUTopology:
    topo = CPUTopology(model_name="Test 8C/16T", family=26, model=0x44, physical_cores=8, ccds=2)
    for cid in range(8):
        topo.cores[cid] = PhysicalCore(core_id=cid, ccd=0 if cid < 4 else 1, logical_cpus=(cid, cid + 8))
    return topo


def _tab():
    _qapp()
    load_all()
    from corecycler.gui.config_tab import ConfigTab

    return ConfigTab(_topo())


class TestPresets:
    def test_default_profile_is_valid_and_standard(self):
        tab = _tab()
        p = tab.get_profile()
        assert p.test_mode == "STANDARD"
        assert p.seconds_per_core == 600
        assert p.cycle_count == 1
        assert p.max_temperature == 95.0

    @pytest.mark.parametrize(
        "preset,seconds,cycles,variable",
        [
            ("QUICK", 120, 1, False),
            ("STANDARD", 600, 1, False),
            ("THOROUGH", 1800, 2, False),
            ("FULL_SPECTRUM", 1200, 3, True),
        ],
    )
    def test_preset_applies_documented_values(self, preset, seconds, cycles, variable):
        tab = _tab()
        tab._mode_combo.setCurrentText(preset)
        p = tab.get_profile()
        assert p.seconds_per_core == seconds
        assert p.cycle_count == cycles
        assert p.variable_load is variable

    def test_changing_a_preset_param_switches_to_custom(self):
        tab = _tab()
        assert tab.get_profile().test_mode == "STANDARD"
        tab._time_spin.setValue(999)
        assert tab.get_profile().test_mode == "CUSTOM"


class TestFftRange:
    def test_custom_fft_exposes_range_and_reports_it(self):
        tab = _tab()
        tab._fft_combo.setCurrentText("CUSTOM")
        assert not tab._fft_range_widget.isHidden()
        tab._fft_min_spin.setValue(64)
        tab._fft_max_spin.setValue(512)
        p = tab.get_profile()
        assert p.fft_min == 64
        assert p.fft_max == 512

    def test_non_custom_fft_reports_none_range(self):
        tab = _tab()
        tab._fft_combo.setCurrentText("SMALL")
        p = tab.get_profile()
        assert p.fft_min is None
        assert p.fft_max is None

    def test_min_above_max_is_clamped(self):
        tab = _tab()
        tab._fft_combo.setCurrentText("CUSTOM")
        tab._fft_max_spin.setValue(256)
        tab._fft_min_spin.setValue(4096)
        assert tab._fft_max_spin.value() >= tab._fft_min_spin.value()


class TestCoreInputInjection:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("0,1,4,5", [0, 1, 4, 5]),
            ("", None),
            ("   ", None),
            (" 0 , 1 ", [0, 1]),
            ("1,2,", [1, 2]),
            ("1,,2", [1, 2]),
            (",,,", None),
            ("7", [7]),
        ],
    )
    def test_valid_core_input_is_preserved(self, text, expected):
        tab = _tab()
        tab._cores_input.setText(text)
        assert tab.get_profile().cores_to_test == expected

    @pytest.mark.parametrize("text", ["abc", "1,abc,2", "999", "-1", "0,999", "0,0"])
    def test_invalid_core_input_refuses_a_profile(self, text):
        tab = _tab()
        tab._cores_input.setText(text)
        with pytest.raises(ValueError, match="[Cc]ore"):
            tab.get_profile()
        assert not tab._cores_error_label.isHidden()

    def test_out_of_range_shows_error_label(self):
        tab = _tab()
        tab._cores_input.setText("999")
        assert not tab._cores_error_label.isHidden()

    def test_valid_input_hides_error_label(self):
        tab = _tab()
        tab._cores_input.setText("999")
        tab._cores_input.setText("0,1")
        assert tab._cores_error_label.isHidden()


class TestRoundTrip:
    def test_set_then_get_profile_round_trips(self):
        from corecycler.config.settings import TestProfile

        tab = _tab()
        src = TestProfile(
            seconds_per_core=42,
            cycle_count=3,
            stop_on_error=True,
            max_temperature=88.0,
            cores_to_test=[2, 3],
            variable_load=True,
            idle_stability_test=15.0,
            idle_between_cores=7.0,
            test_mode="CUSTOM",
        )
        tab.set_profile(src)
        out = tab.get_profile()
        assert out.seconds_per_core == 42
        assert out.cycle_count == 3
        assert out.stop_on_error is True
        assert out.max_temperature == 88.0
        assert out.cores_to_test == [2, 3]
        assert out.variable_load is True
        assert int(out.idle_stability_test) == 15
        assert int(out.idle_between_cores) == 7

    def test_an_invalid_import_does_not_mutate_the_widgets(self):
        from corecycler.config.settings import TestProfile

        tab = _tab()
        before = tab.get_profile()
        with pytest.raises(ValueError, match="backend"):
            tab.set_profile(TestProfile(backend="missing"))
        assert tab.get_profile() == before


class TestBackendWorkloads:
    def test_backend_change_exposes_only_supported_workloads(self):
        from corecycler.engine.backends import get_backend

        tab = _tab()
        tab._backend_combo.setCurrentText("stress-ng")
        backend = get_backend("stress-ng")
        assert {tab._stress_mode_combo.itemText(i) for i in range(tab._stress_mode_combo.count())} == {
            mode.name for mode in backend.get_supported_modes()
        }
        assert {tab._fft_combo.itemText(i) for i in range(tab._fft_combo.count())} == {
            preset.name for preset in backend.get_supported_fft_presets()
        }


class TestRetestFailed:
    def test_failed_cores_enable_button_and_populate_input(self):
        tab = _tab()
        tab.set_failed_cores([3, 1])
        assert tab._retest_failed_btn.isEnabled()
        tab._on_retest_failed()
        assert tab._cores_input.text() == "1,3"

    def test_no_failed_cores_disables_button(self):
        tab = _tab()
        tab.set_failed_cores([])
        assert not tab._retest_failed_btn.isEnabled()


class TestProfileValidation:
    @pytest.mark.parametrize(
        "changes,field",
        [
            ({"stress_mode": "missing"}, "stress mode"),
            ({"fft_preset": "missing"}, "FFT preset"),
            ({"threads": 0}, "threads"),
            ({"seconds_per_core": 0}, "seconds_per_core"),
            ({"cycle_count": 0}, "cycle_count"),
            ({"max_temperature": 0.0}, "max_temperature"),
            ({"idle_stability_test": -1.0}, "idle_stability_test"),
            ({"idle_between_cores": -1.0}, "idle_between_cores"),
            ({"test_mode": "missing"}, "test preset"),
            ({"cores_to_test": [True]}, "Core IDs must be integers"),
            ({"cores_to_test": [0, 0]}, "Duplicate core IDs"),
            ({"cores_to_test": [999]}, "detected topology"),
            ({"fft_preset": "CUSTOM", "fft_min": None, "fft_max": None}, "Custom FFT range"),
        ],
    )
    def test_each_invalid_field_is_refused_without_mutating_widgets(self, changes, field):
        tab = _tab()
        before = tab.get_profile()
        profile = TestProfile()
        for name, value in changes.items():
            setattr(profile, name, value)

        with pytest.raises(ValueError, match=field):
            tab.set_profile(profile)

        assert tab.get_profile() == before

    def test_widget_profile_validation_refuses_an_out_of_contract_value(self):
        tab = _tab()
        tab._time_spin.setMinimum(0)
        tab._time_spin.setValue(0)

        with pytest.raises(ValueError, match="seconds_per_core"):
            tab.get_profile()

    def test_unknown_backend_change_preserves_workload_choices(self):
        tab = _tab()
        before = (tab._stress_mode_combo.currentText(), tab._fft_combo.currentText())

        tab._on_backend_change("missing")

        assert (tab._stress_mode_combo.currentText(), tab._fft_combo.currentText()) == before
