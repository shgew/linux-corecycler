"""CoreGridWidget + CoreCell: CCD layout, state transitions, telemetry injection."""

from __future__ import annotations

import sys as _sys

import pytest

if not hasattr(_sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("GUI tests require real PySide6", allow_module_level=True)

from corecycler.engine.scheduler import CoreTestStatus
from corecycler.engine.topology import CPUTopology, PhysicalCore


def _qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _topo(ccds=2, vcache_ccd=None):
    topo = CPUTopology(model_name="Test", family=26, model=0x44, physical_cores=8, ccds=ccds)
    per = 8 // ccds
    for cid in range(8):
        ccd = cid // per
        topo.cores[cid] = PhysicalCore(
            core_id=cid,
            ccd=ccd,
            logical_cpus=(cid, cid + 8),
            has_vcache=(ccd == vcache_ccd),
        )
    return topo


def _grid(topo=None):
    _qapp()
    from corecycler.gui.widgets.core_grid import CoreGridWidget

    return CoreGridWidget(topo)


class TestGrid:
    def test_dual_ccd_builds_a_cell_per_core(self):
        grid = _grid(_topo(ccds=2))
        assert sorted(grid._cells) == list(range(8))

    def test_empty_topology_builds_no_cells(self):
        grid = _grid(CPUTopology())
        assert grid._cells == {}

    def test_rebuild_replaces_cells_cleanly(self):
        grid = _grid(_topo(ccds=2))
        old_cells = dict(grid._cells)
        replacement = CPUTopology(model_name="Replacement", physical_cores=2, ccds=1)
        replacement.cores = {
            core_id: PhysicalCore(core_id=core_id, ccd=0, logical_cpus=(core_id,)) for core_id in (20, 21)
        }

        grid.set_topology(replacement)

        assert sorted(grid._cells) == [20, 21]
        assert not set(old_cells.values()) & set(grid._cells.values())
        assert grid._layout.count() == 4
        grid.update_core_status(0, CoreTestStatus(core_id=0, state="failed"))
        assert grid._cells[20]._state == "pending"

    def test_9950x3d2_builds_both_vcache_ccds(self, topo_9950x3d2):
        from PySide6.QtWidgets import QLabel

        grid = _grid(topo_9950x3d2)

        assert sorted(grid._cells) == list(range(16))
        assert all(cell.has_vcache and cell._header_label.text().endswith("V") for cell in grid._cells.values())
        headers = [
            grid._layout.itemAt(index).widget().text()
            for index in range(grid._layout.count())
            if isinstance(grid._layout.itemAt(index).widget(), QLabel)
        ]
        assert headers == ["CCD 0 (V-Cache)", "CCD 1 (V-Cache)"]

    def test_vcache_flag_carried_to_cell(self):
        grid = _grid(_topo(ccds=2, vcache_ccd=0))
        assert grid._cells[0].has_vcache is True
        assert grid._cells[7].has_vcache is False

    @pytest.mark.parametrize("state", ["pending", "testing", "passed", "failed", "skipped"])
    def test_update_status_all_states(self, state):
        grid = _grid(_topo())
        grid.update_core_status(0, CoreTestStatus(core_id=0, state=state))
        cell = grid._cells[0]
        assert cell._state == state
        assert cell.height() == (38 if state == "testing" else 22)
        assert cell._telemetry_label.isHidden() is (state != "testing")

    def test_update_unknown_core_is_noop(self):
        grid = _grid(_topo())
        states = {core_id: cell._state for core_id, cell in grid._cells.items()}
        grid.update_core_status(999, CoreTestStatus(core_id=999, state="failed"))
        assert {core_id: cell._state for core_id, cell in grid._cells.items()} == states

    def test_passed_with_errors_becomes_warned(self):
        grid = _grid(_topo())
        grid.update_core_status(0, CoreTestStatus(core_id=0, state="passed", errors=2))
        assert grid._cells[0]._state == "warned"

    def test_testing_expands_then_collapses(self):
        from corecycler.gui.widgets.core_grid import CELL_HEIGHT_ACTIVE, CELL_HEIGHT_NORMAL

        grid = _grid(_topo())
        cell = grid._cells[0]
        grid.update_core_status(0, CoreTestStatus(core_id=0, state="testing"))
        assert cell.height() == CELL_HEIGHT_ACTIVE
        grid.update_core_status(0, CoreTestStatus(core_id=0, state="passed"))
        assert cell.height() == CELL_HEIGHT_NORMAL

    def test_telemetry_injection_while_testing(self):
        grid = _grid(_topo())
        grid.update_core_status(0, CoreTestStatus(core_id=0, state="testing"))
        grid.update_core_telemetry(
            0, freq_mhz=5200, temp_c=78, vcore_v=1.234, stretch_pct=2.5, co_offset=-30, tuner_phase="confirm"
        )
        assert "CO:-30" in grid._cells[0]._detail_label.text()
        assert "5200MHz" in grid._cells[0]._telemetry_label.text()

    def test_telemetry_unknown_core_is_noop(self):
        grid = _grid(_topo())
        grid.update_core_telemetry(999, freq_mhz=5000)

    def test_missing_temperature_is_not_rendered_as_zero(self):
        grid = _grid(_topo())
        grid.update_core_status(0, CoreTestStatus(core_id=0, state="testing"))
        grid.update_core_telemetry(0, freq_mhz=5200, temp_c=None)
        assert "5200MHz" in grid._cells[0]._telemetry_label.text()
        assert "0C" not in grid._cells[0]._telemetry_label.text()
