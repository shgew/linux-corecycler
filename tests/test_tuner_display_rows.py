"""TunerTab table/display methods: core rows, test counts, verdicts, log entries.

Driven with a mock engine (core_states + session_id) and a seeded test log --
the display layer, not the engine-construction handlers.
"""

from __future__ import annotations

import sys as _sys
from unittest.mock import MagicMock

import pytest

if not hasattr(_sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("GUI tests require real PySide6", allow_module_level=True)

from corecycler.engine.topology import CPUTopology, PhysicalCore
from corecycler.history.db import HistoryDB, TuningContextRecord
from corecycler.tuner import persistence as tp
from corecycler.tuner.config import TunerConfig
from corecycler.tuner.state import CoreState, TunerPhase


@pytest.fixture
def db():
    d = HistoryDB(":memory:")
    yield d
    d.close()


def _topo():
    topo = CPUTopology(model_name="Test", family=26, model=0x44, physical_cores=8, ccds=2)
    for c in range(8):
        topo.cores[c] = PhysicalCore(core_id=c, ccd=0 if c < 4 else 1, ccx=None, logical_cpus=(c, c + 8))
    return topo


def _tab(db):
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from corecycler.gui.tuner_tab import TunerTab

    return TunerTab(db=db, topology=_topo(), smu=None)


def _engine(sid, states):
    eng = MagicMock()
    eng.session_id = sid
    eng.status = "validating"
    eng.core_states = states
    return eng


def _sid(db, context_id=None):
    return tp.create_session(db, TunerConfig(), bios_version="2402", cpu_model="Test 8C", context_id=context_id)


class TestCoreRow:
    def test_update_core_row_shows_only_selected_context_banked_evidence(self, db):
        """The row reports evidence from the selected session's complete context.

        The two BIOS contexts intentionally share a CO hash. The weaker regime
        is still the headline number, so a missing fourth regime is unproven.
        """
        selected_context = db.create_context(TuningContextRecord(bios_version="2402", co_hash="same"))
        other_context = db.create_context(TuningContextRecord(bios_version="2403", co_hash="same"))
        sid = _sid(db, selected_context)
        tp.log_test_result(db, sid, 0, -30, "confirm", True, duration=60.0)
        for regime in ("boost", "current", "transient"):
            db.bank_regime_time(selected_context, 0, regime, -30, 7200.0)
            db.bank_regime_time(other_context, 0, regime, -30, 18000.0)
        tab = _tab(db)
        tab._engine = _engine(
            sid,
            {0: CoreState(core_id=0, phase=TunerPhase.CONFIRMED, current_offset=-30, best_offset=-30)},
        )
        tab._update_core_row(0)
        assert tab._find_core_row(0) == 0
        headers = [tab._core_table.horizontalHeaderItem(c).text() for c in range(tab._core_table.columnCount())]
        assert tab._core_table.item(0, headers.index("Boost")).text() == "2.0h"
        assert tab._core_table.item(0, headers.index("Coupled")).text() == "0.0h"
        assert tab._core_table.item(0, headers.index("Proven")).text() == "0.0h"
        assert tab._core_table.item(0, headers.index("Last Result")).text() == "PASS"

    def test_core_row_reports_no_hours_without_a_context(self, db):
        """Confidence is keyed to the operating point; with no context there
        is nothing to key it to, and the row must not imply otherwise."""
        sid = _sid(db)
        tab = _tab(db)
        tab._engine = _engine(
            sid, {0: CoreState(core_id=0, phase=TunerPhase.CONFIRMED, current_offset=-30, best_offset=-30)}
        )
        tab._update_core_row(0)
        headers = [tab._core_table.horizontalHeaderItem(c).text() for c in range(tab._core_table.columnCount())]
        assert tab._core_table.item(0, headers.index("Proven")).text() == "0.0h"

    def test_update_core_row_no_engine_is_noop(self, db):
        tab = _tab(db)
        tab._engine = None
        tab._update_core_row(0)
        assert tab._find_core_row(0) == -1

    def test_update_core_row_unknown_core_is_noop(self, db):
        tab = _tab(db)
        tab._engine = _engine(_sid(db), {})
        tab._update_core_row(9)
        assert tab._find_core_row(9) == -1


class TestLogEntry:
    def test_add_log_entry_appends_row(self, db):
        sid = _sid(db)
        tp.log_test_result(db, sid, 0, -30, "confirm", True, duration=60.0)
        tab = _tab(db)
        tab._engine = _engine(sid, {})
        before = tab._log_table.rowCount()
        tab._add_log_entry(0, -30, True)
        assert tab._log_table.rowCount() == before + 1

    def test_add_log_entry_respects_selected_core(self, db):
        sid = _sid(db)
        tp.log_test_result(db, sid, 1, -30, "confirm", True, duration=60.0)
        tab = _tab(db)
        tab._engine = _engine(sid, {})
        tab._selected_core = 0
        before = tab._log_table.rowCount()
        tab._add_log_entry(1, -30, True)
        assert tab._log_table.rowCount() == before


class TestSlots:
    def test_core_state_changed_updates_row(self, db):
        sid = _sid(db)
        tab = _tab(db)
        tab._engine = _engine(sid, {2: CoreState(core_id=2, phase=TunerPhase.COARSE_SEARCH, current_offset=-20)})
        tab._on_core_state_changed(2, "coarse_search", -20)
        assert tab._find_core_row(2) >= 0

    def test_validation_progress_sets_label(self, db):
        tab = _tab(db)
        tab._on_validation_progress(2, 3, 8)
        assert tab._progress_label.text()

    def test_progress_updated_sets_label(self, db):
        tab = _tab(db)
        tab._on_progress_updated(4, 8)
        assert tab._progress_label.text()
