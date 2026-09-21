"""Tests for TunerTab GUI widget."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from corecycler.history.db import HistoryDB
from corecycler.tuner.config import FIELD_BOUNDS, TEST_ORDERS, TunerConfig


@pytest.fixture
def db():
    d = HistoryDB(":memory:")
    yield d
    d.close()


def _tab(co_range=(-50, 10)):
    from PySide6.QtWidgets import QApplication

    from corecycler.gui.tuner_tab import TunerTab

    QApplication.instance() or QApplication([])
    smu = SimpleNamespace(commands=SimpleNamespace(co_range=co_range))
    return TunerTab(db=None, topology=None, smu=smu)


class TestTunerTabCreation:
    """Basic construction tests that don't require a running Qt app."""

    def test_config_defaults(self):
        """TunerConfig defaults are usable for GUI initialization."""
        cfg = TunerConfig()
        assert cfg.coarse_step > 0
        assert cfg.fine_step > 0
        assert cfg.search_duration_seconds > 0
        assert cfg.confirm_duration_seconds > 0

    @pytest.mark.parametrize("edge", [0, 1])
    def test_numeric_widget_boundaries_round_trip(self, edge):
        tab = _tab()
        fields = {
            "start_offset": tab._start_offset_spin,
            "coarse_step": tab._coarse_step_spin,
            "fine_step": tab._fine_step_spin,
            "max_offset": tab._max_offset_spin,
            "search_duration_seconds": tab._search_dur_spin,
            "confirm_duration_seconds": tab._confirm_dur_spin,
            "validate_duration_seconds": tab._validate_dur_spin,
            "max_confirm_retries": tab._max_retries_spin,
            "stretch_threshold_pct": tab._stretch_threshold_spin,
        }
        values = {
            field: ((-50, 10) if field in {"start_offset", "max_offset"} else FIELD_BOUNDS[field])[edge]
            for field in fields
        }
        for field, spin in fields.items():
            expected_bounds = (-50, 10) if field in {"start_offset", "max_offset"} else FIELD_BOUNDS[field]
            assert (spin.minimum(), spin.maximum()) == expected_bounds

        tab._apply_config_to_ui(TunerConfig(**values))
        round_trip = tab._get_config()

        assert {field: getattr(round_trip, field) for field in fields} == values
        assert tuple(tab._order_combo.itemText(index) for index in range(tab._order_combo.count())) == TEST_ORDERS

    def test_db_schema_has_tuner_tables(self, db):
        """The DB fixture should have tuner tables from v3 schema."""
        tables = db._execute_raw("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        names = [t["name"] for t in tables]
        assert "tuner_sessions" in names
        assert "tuner_core_states" in names
        assert "tuner_test_log" in names


class TestSelfPauseRecoverable:
    """Every engine self-pause says 'fix the cause, then Resume'. The buttons
    must follow the engine status or the GUI is a dead end (Resume greyed out
    after an apparatus/SMU/startup pause)."""

    def test_status_paused_enables_resume(self):
        from PySide6.QtWidgets import QApplication

        from corecycler.gui.tuner_tab import TunerTab

        app = QApplication.instance() or QApplication([])
        assert app is not None
        tab = TunerTab(db=None, topology=None, smu=None)
        tab._set_running_state(True)  # engine started: Resume disabled
        assert not tab._resume_btn.isEnabled()

        tab._on_status_changed("paused")  # engine paused ITSELF
        assert tab._resume_btn.isEnabled()
        assert not tab._pause_btn.isEnabled()

        tab._on_status_changed("running")  # resumed again
        assert not tab._resume_btn.isEnabled()
        assert tab._pause_btn.isEnabled()
