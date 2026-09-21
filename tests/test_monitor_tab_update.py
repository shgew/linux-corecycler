"""Monitor worker sampling and missing-value behavior."""

from __future__ import annotations

import sys as _sys
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

if not hasattr(_sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("GUI tests require real PySide6", allow_module_level=True)

from corecycler.engine.topology import CPUTopology, PhysicalCore


def _qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _topo() -> CPUTopology:
    topo = CPUTopology(model_name="Test", family=26, model=0x44, physical_cores=4, ccds=2)
    for cid in range(4):
        ccd = 0 if cid < 2 else 1
        topo.cores[cid] = PhysicalCore(core_id=cid, ccd=ccd, logical_cpus=(cid,), has_vcache=ccd == 0)
    return topo


def _tab(topology=None):
    _qapp()
    from corecycler.gui.monitor_tab import MonitorTab

    return MonitorTab(topology=topology or _topo())


def _hwmon_data(tctl=60.0, ccd=None, vcore=1.2):
    return SimpleNamespace(
        tctl_c=tctl,
        ccd_temperatures_c=ccd if ccd is not None else {0: 61.0, 1: 59.0},
        vcore_v=vcore,
    )


def _dual(count=4, actual=5000.0, ceiling=5200.0):
    return {cid: SimpleNamespace(actual_mhz=actual, effective_max_mhz=ceiling) for cid in range(count)}


def _sample(tab, monkeypatch, *, dual=None, simple=None, watts=None, msr_ok=False, pkg=None, hwmon=None, pm=None):
    import corecycler.gui.monitor_tab as mt

    monkeypatch.setattr(mt, "read_core_frequencies_dual", lambda: dual if dual is not None else {})
    monkeypatch.setattr(mt, "read_core_frequencies", lambda: simple if simple is not None else {})
    monkeypatch.setattr(mt, "read_max_frequency", lambda: 6000.0)
    worker = tab._worker
    worker.hwmon = MagicMock()
    worker.hwmon.read.return_value = hwmon if hwmon is not None else _hwmon_data()
    worker.power = MagicMock()
    worker.power.read_power_watts.return_value = watts
    worker.msr = MagicMock()
    worker.msr.is_available.return_value = msr_ok
    worker.msr.read_package_power.return_value = pkg
    worker.msr.read_clock_stretch.return_value = {0: SimpleNamespace(stretch_pct=2.5)}
    worker.msr.read_core_power.return_value = {0: SimpleNamespace(watts=12.0)}
    worker.cpu_usage = MagicMock()
    worker.cpu_usage.read.return_value = {cid: 50.0 for cid in range(32)}
    worker.pmtable = MagicMock()
    worker.pmtable.is_available.return_value = pm is not None
    worker.pmtable.read.return_value = pm
    worker.run()


class TestMonitorWorker:
    def test_emits_one_immutable_snapshot_and_updates_widgets(self, monkeypatch):
        tab = _tab()
        seen = []
        tab._worker.snapshot_ready.connect(seen.append)
        _sample(tab, monkeypatch, dual=_dual(), watts=95.0)

        assert len(seen) == 1
        with pytest.raises(FrozenInstanceError):
            seen[0].package_watts = 1.0
        assert "95.0" in tab._power_label.text()
        assert "VC" in tab._ccd_temp_labels[0].text()
        assert "VC" not in tab._ccd_temp_labels[1].text()

    def test_disappeared_temperature_and_voltage_render_missing(self, monkeypatch):
        tab = _tab()
        _sample(tab, monkeypatch, dual=_dual(), watts=50.0)
        _sample(tab, monkeypatch, dual=_dual(), watts=50.0, hwmon=_hwmon_data(tctl=None, ccd={}, vcore=None))

        assert "N/A" in tab._tctl_label.text()
        assert "N/A" in tab._vcore_label.text()
        assert "N/A" in tab._ccd_temp_labels[0].text()
        assert "N/A" in tab._ccd_temp_labels[1].text()

    def test_missing_power_is_not_rendered_as_zero(self, monkeypatch):
        tab = _tab()
        _sample(tab, monkeypatch, dual=_dual(), watts=None, msr_ok=False)
        assert "N/A" in tab._power_label.text()
        assert "0.0" not in tab._power_label.text()

    def test_source_failure_is_logged_without_dropping_other_sources(self, monkeypatch, caplog):
        import corecycler.gui.monitor_tab as mt

        tab = _tab()
        monkeypatch.setattr(mt, "read_core_frequencies_dual", MagicMock(side_effect=OSError("frequency gone")))
        monkeypatch.setattr(mt, "read_core_frequencies", lambda: {})
        monkeypatch.setattr(mt, "read_max_frequency", lambda: 6000.0)
        tab._worker.hwmon = MagicMock()
        tab._worker.hwmon.read.return_value = _hwmon_data(tctl=72.0)
        tab._worker.power = MagicMock()
        tab._worker.power.read_power_watts.return_value = 42.0
        tab._worker.cpu_usage = MagicMock()
        tab._worker.cpu_usage.read.return_value = {}
        tab._worker.msr = MagicMock()
        tab._worker.msr.is_available.return_value = False
        tab._worker.pmtable = MagicMock()
        tab._worker.pmtable.is_available.return_value = False

        tab._worker.run()

        assert "72.0" in tab._tctl_label.text()
        assert "42.0" in tab._power_label.text()
        assert "frequency gone" in caplog.text

    def test_msr_and_pm_table_fallbacks_are_included_in_snapshot(self, monkeypatch):
        tab = _tab()
        pm = SimpleNamespace(
            ppt_value_w=120.0,
            ppt_limit_w=200.0,
            tdc_value_a=90.0,
            tdc_limit_a=180.0,
            edc_value_a=110.0,
            edc_limit_a=230.0,
        )
        _sample(tab, monkeypatch, dual=_dual(), watts=None, msr_ok=True, pkg=88.0, pm=pm)

        assert "88.0" in tab._power_label.text()
        assert "120" in tab._ppt_label.text()
        assert "200" in tab._ppt_label.text()

    def test_9950x3d2_updates_both_vcache_ccds_and_16_bars(self, topo_9950x3d2, monkeypatch):
        tab = _tab(topo_9950x3d2)
        _sample(
            tab,
            monkeypatch,
            dual=_dual(count=32),
            watts=125.0,
            hwmon=_hwmon_data(ccd={0: 70.0, 1: 68.0}),
        )

        assert len(tab._per_core_bars) == 16
        assert "VC" in tab._ccd_temp_labels[0].text()
        assert "70.0" in tab._ccd_temp_labels[0].text()
        assert "VC" in tab._ccd_temp_labels[1].text()
        assert "68.0" in tab._ccd_temp_labels[1].text()


class TestMonitorResidualEdges:
    def test_update_never_overlaps_an_inflight_sample(self):
        tab = _tab()
        worker = MagicMock()
        tab._worker = worker
        worker.isRunning.return_value = True

        tab._update()
        worker.start.assert_not_called()

        worker.isRunning.return_value = False
        tab._update()
        worker.start.assert_called_once()

    def test_new_frequency_ceiling_rescales_chart_and_skips_core_without_cpu(self):
        from corecycler.gui.monitor_tab import MonitorSnapshot

        topology = _topo()
        topology.cores[4] = PhysicalCore(core_id=4, ccd=1, logical_cpus=())
        tab = _tab(topology)
        empty_core_bar = tab._per_core_bars[4]
        empty_core_bar.update_data = MagicMock()
        snapshot = MonitorSnapshot(
            frequencies=((0, 7000.0, 7100.0),),
            tctl_c=None,
            ccd_temperatures_c=(),
            vcore_v=None,
            package_watts=None,
            usage=(),
            stretch=(),
            core_watts=(),
            max_frequency_mhz=6800.0,
            power_limits=(),
        )

        tab._apply_snapshot(snapshot)

        assert tab._max_core_freq == 7000.0
        assert tab._freq_chart.max_val == pytest.approx(7700.0)
        assert "7000" in tab._max_freq_label.text()
        empty_core_bar.update_data.assert_not_called()
