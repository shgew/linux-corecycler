"""MemoryTab: tool detection, stress-run guards, external-test lock."""

from __future__ import annotations

import sys as _sys
from unittest.mock import MagicMock, patch

import pytest

if not hasattr(_sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("GUI tests require real PySide6", allow_module_level=True)


def _tab():
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from corecycler.gui.memory_tab import MemoryTab

    return MemoryTab()


class TestToolDetection:
    def test_no_tools_reports_none_installed(self):
        with patch("corecycler.config.tools.shutil.which", return_value=None):
            tab = _tab()
            assert tab._detect_available_tools() == ["(none installed)"]

    def test_both_tools_detected(self):
        with patch("corecycler.config.tools.shutil.which", lambda name: "/usr/bin/" + name):
            tab = _tab()
            tools = tab._detect_available_tools()
        assert "stressapptest" in tools
        assert "stress-ng --vm" in tools


class TestStressGuards:
    def test_external_test_running_disables_run(self):
        tab = _tab()
        tab.set_test_running(True)
        assert not tab._stress_btn.isEnabled()
        tab.set_test_running(False)
        assert tab._stress_btn.isEnabled()

    def test_run_with_no_tool_warns_and_starts_nothing(self):
        tab = _tab()
        tab._stress_tool.clear()
        tab._stress_tool.addItem("(none installed)")
        with patch("corecycler.gui.memory_tab.QMessageBox.warning") as warn:
            tab._run_memory_stress()
        assert warn.called
        assert tab._stress_worker is None

    def test_run_is_reentrancy_guarded(self):
        tab = _tab()
        tab._stress_worker = MagicMock()
        tab._stress_worker.isRunning.return_value = True
        before = tab._stress_worker
        tab._run_memory_stress()
        assert tab._stress_worker is before

    def test_on_stress_done_reenables_and_signals(self, qtbot=None):
        tab = _tab()
        seen = []
        tab.memory_stress_done.connect(lambda ok: seen.append(ok))
        with patch("corecycler.gui.memory_tab.QMessageBox.information"):
            tab._on_stress_done(True, "Status: PASS")
        assert seen == [True]
        assert tab._stress_btn.isEnabled()
        assert not tab._stop_btn.isEnabled()


class TestMemoryTelemetryWorker:
    def test_inventory_spd_and_temperatures_are_sampled_by_worker(self, monkeypatch):
        import corecycler.gui.memory_tab as mt
        from corecycler.monitor.memory import DIMMInfo

        tab = _tab()
        dimm = DIMMInfo(locator="DIMM_A1", size_gb=32, mem_type="DDR5", speed_mt=6000)
        monkeypatch.setattr(mt, "read_dimm_info", lambda: [dimm])
        tab._memory_worker.spd_reader = MagicMock()
        tab._memory_worker.spd_reader.spd_timings = None
        tab._memory_worker.spd_reader.read_temperatures.return_value = [51.5]
        tab._memory_worker.pm_reader = MagicMock()
        tab._memory_worker.pm_reader.is_available.return_value = False
        tab._memory_worker.run()

        assert tab._dimms == [dimm]
        assert "32" in tab._summary_label.text()
        assert "51.5" in tab._temp_labels[0].text()

    def test_source_failure_is_logged_without_dropping_inventory(self, monkeypatch, caplog):
        import corecycler.gui.memory_tab as mt
        from corecycler.monitor.memory import DIMMInfo

        tab = _tab()
        dimm = DIMMInfo(locator="DIMM_A1", size_gb=16, mem_type="DDR5")
        monkeypatch.setattr(mt, "read_dimm_info", lambda: [dimm])
        tab._memory_worker.spd_reader = MagicMock()
        tab._memory_worker.spd_reader.spd_timings = None
        tab._memory_worker.spd_reader.read_temperatures.side_effect = OSError("sensor gone")
        tab._memory_worker.pm_reader = MagicMock()
        tab._memory_worker.pm_reader.is_available.return_value = False

        tab._memory_worker.run()

        assert tab._dimms == [dimm]
        assert not tab._temp_labels
        assert "sensor gone" in caplog.text

    def test_refresh_schedules_inventory_without_overlapping_worker(self):
        tab = _tab()
        worker = MagicMock()
        worker.isRunning.return_value = False
        tab._memory_worker = worker

        tab._refresh_memory_info()

        assert worker.refresh_inventory is True
        worker.start.assert_called_once_with()

        worker.reset_mock()
        worker.isRunning.return_value = True
        tab._request_update()
        worker.start.assert_not_called()

    def test_empty_inventory_is_rendered_as_unavailable(self):
        tab = _tab()
        tab._apply_inventory(())
        assert "No DIMM info available" in tab._summary_label.text()
