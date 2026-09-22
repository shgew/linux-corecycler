"""HistoryTab with a seeded DB: view modes, delete guards, run/session deletion.

Deletion is destructive, so the guards (a running/validating tuner session must
refuse deletion) and the view-mode wiring get first-class tests here.
"""

from __future__ import annotations

import csv
import json
import sys as _sys
from unittest.mock import MagicMock, patch

import pytest

if not hasattr(_sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("GUI tests require real PySide6", allow_module_level=True)

from corecycler.history.db import HistoryDB, RunRecord, TuningContextRecord
from corecycler.tuner.config import TunerConfig


def _qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def db():
    d = HistoryDB(":memory:")
    yield d
    d.close()


def _seed_run(db, started_at, status="completed", cores_failed=0):
    return db.create_run(
        RunRecord(
            started_at=started_at,
            status=status,
            backend="mprime",
            stress_mode="SSE",
            cpu_model="Test 8C",
            total_cores=4,
            cores_passed=4 - cores_failed,
            cores_failed=cores_failed,
        )
    )


def _seed_session(db, status="completed"):
    sid = db.create_tuner_session(TunerConfig().to_json(), bios_version="2402", cpu_model="Test 8C")
    db.update_tuner_session_status(sid, status)
    return sid


def _seed_context_run(db, context_hash, notes, backend):
    context_id = db.get_or_create_context(
        TuningContextRecord(
            bios_version="2402",
            cpu_model="Test 8C",
            physical_cores=4,
            context_hash=context_hash,
            notes=notes,
        )
    )
    db.create_run(
        RunRecord(
            context_id=context_id,
            backend=backend,
            stress_mode="SSE",
            cpu_model="Test 8C",
            status="completed",
            total_cores=4,
            cores_passed=4,
        )
    )
    return context_id


def _tab(db):
    _qapp()
    from corecycler.gui.history_tab import HistoryTab

    return HistoryTab(db)


def _yes():
    from PySide6.QtWidgets import QMessageBox

    return QMessageBox.StandardButton.Yes


class TestViews:
    def test_sorted_context_selection_refresh_and_delete_keep_the_same_context(self, db):
        from PySide6.QtCore import Qt

        selected_id = _seed_context_run(db, "first", "a-note", "selected-backend")
        other_id = _seed_context_run(db, "second", "z-note", "other-backend")
        tab = _tab(db)
        tab._view_mode = tab.VIEW_GROUPED
        tab.refresh()
        tab._context_table.sortItems(5, Qt.SortOrder.AscendingOrder)

        tab._context_table.selectRow(0)
        assert tab._displayed_runs[0].backend == "selected-backend"

        tab._refresh_preserve_context()
        selected_row = tab._context_table.currentRow()
        assert tab._context_table.item(selected_row, 0).data(Qt.ItemDataRole.UserRole) == selected_id

        with patch("corecycler.gui.history_tab.QMessageBox.question", return_value=_yes()):
            tab._delete_contexts([selected_row])

        assert db.get_context(selected_id) is None
        assert db.get_context(other_id) is not None

    def test_sorted_context_menu_updates_the_selected_context(self, db, monkeypatch):
        from PySide6.QtCore import Qt

        from corecycler.gui import history_tab as history_module

        selected_id = _seed_context_run(db, "first", "a-note", "selected-backend")
        other_id = _seed_context_run(db, "second", "z-note", "other-backend")
        tab = _tab(db)
        tab._view_mode = tab.VIEW_GROUPED
        tab.refresh()
        tab._context_table.sortItems(5, Qt.SortOrder.AscendingOrder)
        tab._context_table.selectRow(0)
        actions = []
        menu = MagicMock()
        menu.addAction.side_effect = lambda _label, callback: actions.append(callback)
        monkeypatch.setattr(history_module, "QMenu", lambda _parent: menu)
        monkeypatch.setattr(history_module.QInputDialog, "getText", lambda *args, **kwargs: ("updated", True))

        tab._show_context_table_menu(tab._context_table.rect().center())
        actions[0]()

        assert db.get_context(selected_id).notes == "updated"
        assert db.get_context(other_id).notes == "z-note"

    def test_all_view_shows_seeded_runs(self, db):
        _seed_run(db, "2026-07-20T10:00:00+00:00")
        _seed_run(db, "2026-07-21T10:00:00+00:00")
        tab = _tab(db)
        tab._view_mode = tab.VIEW_ALL
        tab.refresh()
        assert tab._runs_table.rowCount() == 2
        assert len(tab._displayed_runs) == 2

    def test_tuner_view_shows_sessions(self, db):
        _seed_session(db, "completed")
        tab = _tab(db)
        tab._view_mode = tab.VIEW_TUNER
        tab.refresh()
        assert len(tab._tuner_sessions) == 1

    def test_tuner_summary_counts_quarantined_sessions(self, db):
        _seed_session(db, "profile_quarantined")
        _seed_session(db, "completed")
        tab = _tab(db)
        tab._view_mode = tab.VIEW_TUNER
        tab.refresh()
        assert tab._crashed_label.text() == "Quarantined: 1"

    def test_tuner_view_marks_a_fully_confirmed_profile(self, db):
        from corecycler.tuner.state import CoreState, TunerPhase

        sid = _seed_session(db, "completed")
        db.upsert_tuner_core_state(
            sid, CoreState(core_id=0, phase=TunerPhase.CONFIRMED, current_offset=-25, best_offset=-25)
        )
        tab = _tab(db)
        tab._view_mode = tab.VIEW_TUNER
        tab.refresh()

        assert tab._runs_table.item(0, 4).text() == "1/1"

    def test_load_more_reaches_every_tuner_session(self, db):
        for _ in range(3):
            _seed_session(db, "completed")
        tab = _tab(db)
        tab.PAGE_SIZE = 2
        tab._view_mode = tab.VIEW_TUNER
        tab.refresh()

        assert tab._runs_table.rowCount() == 2
        tab._load_more_btn.click()
        assert tab._runs_table.rowCount() == 3

    def test_load_more_reaches_every_run(self, db):
        for day in range(3):
            _seed_run(db, f"2026-07-2{day}T10:00:00+00:00")
        tab = _tab(db)
        tab.PAGE_SIZE = 2
        tab._view_mode = tab.VIEW_ALL
        tab.refresh()

        assert tab._runs_table.rowCount() == 2
        assert tab._load_more_btn.isEnabled()
        assert tab._total_label.text() == "Runs: 3"

        tab._load_more_btn.click()

        assert tab._runs_table.rowCount() == 3
        assert not tab._load_more_btn.isEnabled()

    def test_numeric_duration_sort_crosses_unit_boundary(self, db):
        short = _seed_run(db, "2026-07-20T10:00:00+00:00", status="running")
        long = _seed_run(db, "2026-07-21T10:00:00+00:00", status="running")
        db.finish_run(short, status="completed", cores_passed=4, cores_failed=0, total_seconds=120.0)
        db.finish_run(long, status="completed", cores_passed=4, cores_failed=0, total_seconds=600.0)
        tab = _tab(db)
        tab._view_mode = tab.VIEW_ALL
        tab.refresh()
        tab._runs_table.sortItems(4)

        assert tab._runs_table.item(0, 4).text().startswith("2m")

    def test_empty_db_does_not_crash(self, db):
        tab = _tab(db)
        tab.refresh()
        assert tab._runs_table.rowCount() == 0


class TestDeleteGuards:
    @pytest.mark.parametrize("status", ["running", "paused", "validating", "hunting"])
    def test_in_flight_session_refuses_deletion_with_database_message(self, db, status):
        _seed_session(db, status)
        tab = _tab(db)
        tab._view_mode = tab.VIEW_TUNER
        tab.refresh()
        with (
            patch("corecycler.gui.history_tab.QMessageBox.question", return_value=_yes()),
            patch("corecycler.gui.history_tab.QMessageBox.warning") as warning,
        ):
            tab._delete_tuner_sessions([0])

        assert status in warning.call_args.args[2]
        assert len(db.list_tuner_sessions()) == 1

    def test_running_run_refuses_deletion_with_database_message(self, db):
        _seed_run(db, "2026-07-20T10:00:00+00:00", status="running")
        tab = _tab(db)
        tab._view_mode = tab.VIEW_ALL
        tab.refresh()
        with (
            patch("corecycler.gui.history_tab.QMessageBox.question", return_value=_yes()),
            patch("corecycler.gui.history_tab.QMessageBox.warning") as warning,
        ):
            tab._delete_runs([0])

        assert "running" in warning.call_args.args[2]
        assert len(db.list_runs()) == 1

    def test_completed_session_deletes(self, db):
        _seed_session(db, "completed")
        tab = _tab(db)
        tab._view_mode = tab.VIEW_TUNER
        tab.refresh()
        with patch("corecycler.gui.history_tab.QMessageBox.question", return_value=_yes()):
            tab._delete_tuner_sessions([0])
        assert db.list_tuner_sessions() == []

    def test_delete_run_removes_it(self, db):
        _seed_run(db, "2026-07-20T10:00:00+00:00")
        tab = _tab(db)
        tab._view_mode = tab.VIEW_ALL
        tab.refresh()
        with patch("corecycler.gui.history_tab.QMessageBox.question", return_value=_yes()):
            tab._delete_runs([0])
        assert db.list_runs() == []

    def test_delete_run_cancelled_keeps_it(self, db):
        from PySide6.QtWidgets import QMessageBox

        _seed_run(db, "2026-07-20T10:00:00+00:00")
        tab = _tab(db)
        tab._view_mode = tab.VIEW_ALL
        tab.refresh()
        with patch(
            "corecycler.gui.history_tab.QMessageBox.question",
            return_value=QMessageBox.StandardButton.No,
        ):
            tab._delete_runs([0])
        assert len(db.list_runs()) == 1


class TestBiosWarning:
    def test_set_bios_warning_is_recorded(self, db):
        tab = _tab(db)
        tab.set_bios_warning("2401", "2402")
        assert tab._bios_warning


def _seed_run_with_results(db, cores=2, failed=0):
    from corecycler.history.db import CoreResultRecord

    rid = db.create_run(
        RunRecord(
            started_at="2026-07-20T10:00:00+00:00",
            finished_at="2026-07-20T11:00:00+00:00",
            status="completed",
            backend="mprime",
            stress_mode="SSE",
            fft_preset="SMALL",
            cpu_model="Test 8C",
            seconds_per_core=600,
            cycle_count=1,
            total_cores=cores,
            cores_passed=cores - failed,
            cores_failed=failed,
            bios_version="2402",
        )
    )
    for c in range(cores):
        db.insert_core_result(
            CoreResultRecord(
                run_id=rid,
                core_id=c,
                ccd=0,
                cycle=0,
                started_at="2026-07-20T10:00:00+00:00",
                passed=(c >= failed),
                elapsed_seconds=600.0,
                iterations_completed=5,
                peak_freq_mhz=5200.0,
                max_temp_c=78.0,
                error_message=None if c >= failed else "rounding error",
                error_type=None if c >= failed else "computation",
            )
        )
    return rid


def _run_by_id(db, rid):
    return next(r for r in db.list_runs() if r.id == rid)


class TestRunDetail:
    def test_selecting_row_shows_the_run_results(self, db):
        _seed_run_with_results(db, cores=2, failed=1)
        tab = _tab(db)
        tab._view_mode = tab.VIEW_ALL
        tab.refresh()
        tab._runs_table.selectRow(0)
        tab._on_run_selection_changed()

        assert tab._core_results_table.rowCount() == 2
        assert tab._core_results_table.item(0, 3).text() == "FAIL"


class TestExport:
    def test_export_json(self, db, tmp_path):
        from PySide6.QtWidgets import QDialog

        _seed_run_with_results(db, cores=2)
        tab = _tab(db)
        tab._view_mode = tab.VIEW_ALL
        tab.refresh()
        out = tmp_path / "run.json"
        with (
            patch("corecycler.gui.history_tab.user_home", return_value=tmp_path),
            patch("corecycler.gui.history_tab.QFileDialog.getSaveFileName", return_value=(str(out), "")) as dialog,
            patch("corecycler.gui.history_tab._ExportOptionsDialog.exec", return_value=QDialog.DialogCode.Accepted),
        ):
            tab._export_json(0)
        payload = json.loads(out.read_text())
        assert payload["run"]["id"] == tab._displayed_runs[0].id
        assert payload["core_results"][0]["core_id"] == 0
        assert dialog.call_args.args[2] == str(tmp_path / f"run_{tab._displayed_runs[0].id}.json")

    def test_export_csv(self, db, tmp_path):
        _seed_run_with_results(db, cores=2)
        tab = _tab(db)
        tab._view_mode = tab.VIEW_ALL
        tab.refresh()
        out = tmp_path / "run.csv"
        with (
            patch("corecycler.gui.history_tab.user_home", return_value=tmp_path),
            patch("corecycler.gui.history_tab.QFileDialog.getSaveFileName", return_value=(str(out), "")) as dialog,
        ):
            tab._export_csv(0)
        rows = list(csv.DictReader(out.read_text().splitlines()))
        assert {int(row["core_id"]) for row in rows} == {0, 1}
        assert dialog.call_args.args[2] == str(tmp_path / f"run_{tab._displayed_runs[0].id}.csv")

    def test_export_write_failure_is_reported(self, db, tmp_path):
        _seed_run_with_results(db, cores=1)
        tab = _tab(db)
        tab._view_mode = tab.VIEW_ALL
        tab.refresh()
        with (
            patch("corecycler.gui.history_tab.QFileDialog.getSaveFileName", return_value=(str(tmp_path / "x.csv"), "")),
            patch("corecycler.history.export.export_run_csv_file", side_effect=OSError("disk full")),
            patch("corecycler.gui.history_tab.QMessageBox.critical") as critical,
        ):
            tab._export_csv(0)
        assert "disk full" in critical.call_args.args[2]

    def test_export_bulk_csv(self, db, tmp_path):
        _seed_run_with_results(db, cores=1)
        _seed_run_with_results(db, cores=1)
        tab = _tab(db)
        tab._view_mode = tab.VIEW_ALL
        tab.refresh()
        out = tmp_path / "bulk.csv"
        with (
            patch("corecycler.gui.history_tab.user_home", return_value=tmp_path),
            patch("corecycler.gui.history_tab.QFileDialog.getSaveFileName", return_value=(str(out), "")) as dialog,
        ):
            tab._export_bulk_csv([0, 1])
        rows = list(csv.DictReader(out.read_text().splitlines()))
        assert {int(row["run_id"]) for row in rows} == {run.id for run in tab._displayed_runs}
        assert dialog.call_args.args[2] == str(tmp_path / "runs_comparison.csv")

    def test_json_export_cancelled_before_destination_does_not_write(self, db):
        _seed_run_with_results(db, cores=1)
        tab = _tab(db)
        tab._view_mode = tab.VIEW_ALL
        tab.refresh()
        with (
            patch("corecycler.gui.history_tab.QFileDialog.getSaveFileName", return_value=("", "")),
            patch("corecycler.history.export.export_run_json_file") as export,
        ):
            tab._export_json(0)

        assert not export.called

    def test_json_export_cancelled_at_options_does_not_write(self, db, tmp_path):
        from PySide6.QtWidgets import QDialog

        _seed_run_with_results(db, cores=1)
        tab = _tab(db)
        tab._view_mode = tab.VIEW_ALL
        tab.refresh()
        with (
            patch(
                "corecycler.gui.history_tab.QFileDialog.getSaveFileName", return_value=(str(tmp_path / "x.json"), "")
            ),
            patch("corecycler.gui.history_tab._ExportOptionsDialog.exec", return_value=QDialog.DialogCode.Rejected),
            patch("corecycler.history.export.export_run_json_file") as export,
        ):
            tab._export_json(0)

        assert not export.called

    def test_json_write_failure_is_reported(self, db, tmp_path):
        from PySide6.QtWidgets import QDialog

        _seed_run_with_results(db, cores=1)
        tab = _tab(db)
        tab._view_mode = tab.VIEW_ALL
        tab.refresh()
        with (
            patch(
                "corecycler.gui.history_tab.QFileDialog.getSaveFileName", return_value=(str(tmp_path / "x.json"), "")
            ),
            patch("corecycler.gui.history_tab._ExportOptionsDialog.exec", return_value=QDialog.DialogCode.Accepted),
            patch("corecycler.history.export.export_run_json_file", side_effect=OSError("read-only filesystem")),
            patch("corecycler.gui.history_tab.QMessageBox.critical") as critical,
        ):
            tab._export_json(0)
        assert "read-only filesystem" in critical.call_args.args[2]

    def test_bulk_csv_write_failure_is_reported(self, db, tmp_path):
        _seed_run_with_results(db, cores=1)
        tab = _tab(db)
        tab._view_mode = tab.VIEW_ALL
        tab.refresh()
        with (
            patch("corecycler.gui.history_tab.QFileDialog.getSaveFileName", return_value=(str(tmp_path / "x.csv"), "")),
            patch("corecycler.history.export.export_runs_bulk_csv_file", side_effect=OSError("permission denied")),
            patch("corecycler.gui.history_tab.QMessageBox.critical") as critical,
        ):
            tab._export_bulk_csv([0])
        assert "permission denied" in critical.call_args.args[2]
