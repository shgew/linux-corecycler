"""smu_tab CO action coverage: read/apply/reset/backup/restore branch matrix."""

from __future__ import annotations

import sys as _sys
from unittest.mock import MagicMock

import pytest

if not hasattr(_sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("GUI tests require real PySide6", allow_module_level=True)

from corecycler.engine.topology import CPUTopology, PhysicalCore


def _qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _topo(cores: int = 4) -> CPUTopology:
    topo = CPUTopology(model_name="Test", family=26, model=0x44, physical_cores=cores, ccds=1)
    for cid in range(cores):
        topo.cores[cid] = PhysicalCore(core_id=cid, ccd=0, logical_cpus=(cid,))
    return topo


@pytest.fixture
def tab(monkeypatch):
    import corecycler.gui.smu_tab as st

    monkeypatch.setattr(st, "QMessageBox", MagicMock())
    _qapp()
    t = st.SMUTab(topology=_topo())
    t._tuner_active = False
    monkeypatch.setattr(t, "_confirm_co_write", lambda _detail: True)
    return t


def _smu(**kw):
    smu = MagicMock()
    smu.get_co_offset.return_value = kw.get("get", -10)
    smu.dry_run = kw.get("dry_run", False)
    smu.set_co_offset.return_value = kw.get("set", True)
    smu.reset_all_co.return_value = kw.get("reset", True)
    smu.has_backup.return_value = kw.get("has_backup", True)
    smu.backup_co_offsets.return_value = kw.get("backup", {cid: -10 for cid in range(4)})
    smu.restore_co_offsets.return_value = kw.get("restore", (True, []))
    return smu


class TestReadAllCo:
    def test_no_smu_is_a_noop(self, tab):
        tab._smu = None
        tab._read_all_co()

    def test_reads_and_fills_spinboxes(self, tab):
        tab._smu = _smu(get=-20)
        tab._read_all_co()
        assert tab._spinboxes[0].value() == -20

    def test_retries_then_succeeds(self, tab):
        smu = _smu()
        smu.get_co_offset.side_effect = [None, -7] + [-7] * 32
        tab._smu = smu
        tab._read_all_co()
        assert tab._spinboxes[0].value() == -7

    def test_persistent_failure_marks_err(self, tab):
        smu = _smu()
        smu.get_co_offset.return_value = None
        tab._smu = smu
        tab._read_all_co()
        assert tab._table.item(0, 2).text() == "ERR"


class TestApplySingle:
    def test_without_driver_warns(self, tab):
        tab._smu = None
        tab._apply_single(0)

    def test_blocked_while_tuner_active(self, tab):
        tab._smu = _smu()
        tab._tuner_active = True
        tab._apply_single(0)
        tab._smu.set_co_offset.assert_not_called()

    def test_unknown_core_is_a_noop(self, tab):
        tab._smu = _smu()
        tab._apply_single(999)
        tab._smu.set_co_offset.assert_not_called()

    def test_declined_confirmation_does_not_write(self, tab, monkeypatch):
        tab._smu = _smu()
        monkeypatch.setattr(tab, "_confirm_co_write", lambda _d: False)
        tab._apply_single(0)
        tab._smu.set_co_offset.assert_not_called()

    def test_successful_write_rereads_hardware_value(self, tab):
        tab._smu = _smu(set=True, get=-14)
        tab._spinboxes[0].setValue(-15)
        tab._apply_single(0)
        assert tab._table.item(0, 2).text() == "-14"

    def test_failed_write_warns(self, tab):
        tab._smu = _smu(set=False)
        tab._apply_single(0)
        tab._smu.set_co_offset.assert_called_once()


class TestApplyAllAndReset:
    def test_apply_all_without_driver(self, tab):
        tab._smu = None
        tab._apply_all_co()

    def test_apply_all_blocked_while_tuner_active(self, tab):
        tab._smu = _smu()
        tab._tuner_active = True
        tab._apply_all_co()
        tab._smu.set_co_offset.assert_not_called()

    def test_apply_all_declined(self, tab, monkeypatch):
        tab._smu = _smu()
        monkeypatch.setattr(tab, "_confirm_co_write", lambda _d: False)
        tab._apply_all_co()
        tab._smu.set_co_offset.assert_not_called()

    def test_apply_all_success_hides_banner_and_reenables(self, tab):
        tab._smu = _smu(set=True)
        tab._apply_all_co()
        assert tab._profile_banner.isHidden()
        assert tab._apply_all_btn.isEnabled()

    def test_apply_all_partial_failure_reenables(self, tab):
        tab._smu = _smu(set=False)
        tab._apply_all_co()
        assert tab._apply_all_btn.isEnabled()
        assert tab._reset_btn.isEnabled()

    def test_reset_without_driver(self, tab):
        tab._smu = None
        tab._reset_all_co()

    def test_reset_blocked_while_tuner_active(self, tab):
        tab._smu = _smu()
        tab._tuner_active = True
        tab._reset_all_co()
        tab._smu.set_co_offset.assert_not_called()

    def test_reset_declined(self, tab, monkeypatch):
        tab._smu = _smu()
        monkeypatch.setattr(tab, "_confirm_co_write", lambda _d: False)
        tab._reset_all_co()
        tab._smu.set_co_offset.assert_not_called()

    def test_reset_writes_zero_through_the_transaction(self, tab):
        tab._smu = _smu(set=True)
        tab._reset_all_co()
        assert [call.args for call in tab._smu.set_co_offset.call_args_list] == [
            (core_id, 0) for core_id in tab._spinboxes
        ]


class TestBackupRestore:
    def test_backup_without_driver(self, tab):
        tab._smu = None
        tab._backup_co()

    def test_backup_enables_restore(self, tab):
        tab._smu = _smu()
        tab._backup_co()
        assert tab._restore_btn.isEnabled()

    def test_restore_without_backup_warns(self, tab):
        tab._smu = _smu(has_backup=False)
        tab._restore_co()
        tab._smu.restore_co_offsets.assert_not_called()

    def test_restore_declined(self, tab, monkeypatch):
        tab._smu = _smu()
        tab._co_backup = {core_id: -10 for core_id in tab._spinboxes}
        monkeypatch.setattr(tab, "_confirm_co_write", lambda _d: False)
        tab._restore_co()
        tab._smu.set_co_offset.assert_not_called()

    def test_restore_partial_failure_still_rereads(self, tab):
        tab._smu = _smu(set=False)
        tab._co_backup = {core_id: -10 for core_id in tab._spinboxes}
        tab._restore_co()
        tab._smu.get_co_offset.assert_called()


class _OrderedSMU:
    def __init__(self, backup: dict[int, int], *, dry_run: bool = False) -> None:
        self.backup = backup
        self.dry_run = dry_run
        self.events: list[str] = []
        self.writes: list[tuple[int, int]] = []

    def backup_co_offsets(self, num_cores: int) -> dict[int, int]:
        self.events.append("backup")
        return dict(self.backup)

    def set_co_offset(self, core_id: int, offset: int) -> bool:
        self.events.append(f"write:{core_id}")
        self.writes.append((core_id, offset))
        return True

    def is_available(self) -> bool:
        return True

    def get_co_offset(self, core_id: int) -> int:
        return self.backup[core_id]

    def has_backup(self) -> bool:
        return True


class TestCompleteWriteTransaction:
    def test_apply_all_backs_up_all_16_cores_before_exact_plan(self, tab, monkeypatch):
        tab.set_topology(_topo(16))
        smu = _OrderedSMU({cid: -20 + cid for cid in range(16)})
        tab._smu = smu
        plans: list[str] = []
        monkeypatch.setattr(tab, "_confirm_co_write", lambda plan: plans.append(plan) or True)
        for cid, spin in tab._spinboxes.items():
            spin.setValue(-cid)

        tab._apply_all_co()

        expected = [(cid, -cid) for cid in range(16)]
        assert smu.events == ["backup", *(f"write:{cid}" for cid in range(16))]
        assert smu.writes == expected
        assert plans == ["Apply CO offsets to all cores:\n" + ", ".join(f"C{cid}={offset}" for cid, offset in expected)]

    def test_incomplete_backup_aborts_without_replacing_complete_snapshot(self, tab, monkeypatch):
        tab.set_topology(_topo(16))
        complete = {cid: -10 for cid in range(16)}
        smu = _OrderedSMU(complete)
        tab._smu = smu
        monkeypatch.setattr(tab, "_confirm_co_write", lambda _plan: True)
        tab._apply_single(0)
        assert tab._co_backup == complete

        smu.backup = {cid: -5 for cid in range(15)}
        smu.events.clear()
        smu.writes.clear()
        tab._apply_single(1)

        assert smu.events == ["backup"]
        assert smu.writes == []
        assert tab._co_backup == complete

    def test_dry_run_leaves_current_value_and_does_not_call_driver_write(self, tab, monkeypatch):
        smu = _OrderedSMU({cid: -10 for cid in range(4)}, dry_run=True)
        tab._smu = smu
        monkeypatch.setattr(tab, "_confirm_co_write", lambda _plan: True)
        tab._table.item(0, 2).setText("-7")
        tab._spinboxes[0].setValue(-20)

        tab._apply_single(0)

        assert smu.events == []
        assert tab._table.item(0, 2).text() == "-7"

    def test_restore_confirmation_contains_exact_saved_plan(self, tab, monkeypatch):
        backup = {cid: -10 - cid for cid in range(4)}
        smu = _OrderedSMU(backup)
        tab._smu = smu
        tab._co_backup = backup
        plans: list[str] = []
        monkeypatch.setattr(tab, "_confirm_co_write", lambda plan: plans.append(plan) or True)

        tab._restore_co()

        assert plans == [
            "Restore CO offsets from backup:\n" + ", ".join(f"C{cid}={value}" for cid, value in backup.items())
        ]
        assert smu.writes == list(backup.items())


class TestTransactionOwnershipEdges:
    def test_backup_helper_refuses_without_a_driver_or_topology(self, tab):
        tab._smu = None
        assert tab._take_complete_backup() is False

    def test_tuner_takeover_during_backup_aborts_before_first_write(self, tab, monkeypatch):
        tab.set_topology(_topo(16))
        smu = _OrderedSMU({cid: -10 for cid in range(16)})
        tab._smu = smu
        monkeypatch.setattr(tab, "_confirm_co_write", lambda _plan: True)
        backup = smu.backup_co_offsets

        def take_over(num_cores):
            snapshot = backup(num_cores)
            tab.set_tuner_running(True)
            return snapshot

        smu.backup_co_offsets = take_over
        tab._apply_single(0)

        assert tab._tuner_active is True
        assert smu.events == ["backup"]
        assert smu.writes == []

    def test_restore_refuses_while_tuner_owns_driver(self, tab):
        backup = {cid: -10 for cid in range(4)}
        smu = _OrderedSMU(backup)
        tab._smu = smu
        tab._co_backup = backup
        tab.set_tuner_running(True)

        tab._restore_co()

        assert smu.events == []
        assert smu.writes == []
