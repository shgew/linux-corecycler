"""TunerTab table/display methods: core rows, test counts, verdicts, log entries.

Driven with a mock engine (core_states + session_id) and a seeded test log --
the display layer, not the engine-construction handlers.
"""

from __future__ import annotations

import json
import sys as _sys
import time
from unittest.mock import MagicMock

import pytest

if not hasattr(_sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("GUI tests require real PySide6", allow_module_level=True)

from PySide6.QtCore import Qt

from corecycler.engine.topology import CPUTopology, PhysicalCore
from corecycler.history.db import HistoryDB, TuningContextRecord
from corecycler.tuner import report
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
        topo.cores[c] = PhysicalCore(core_id=c, ccd=0 if c < 4 else 1, logical_cpus=(c, c + 8))
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
    return db.create_tuner_session(
        TunerConfig().to_json(), bios_version="2402", cpu_model="Test 8C", context_id=context_id
    )


class TestCoreRow:
    def test_update_core_row_matches_canonical_report(self, db):
        selected_context = db.get_or_create_context(TuningContextRecord(bios_version="2402", context_hash="selected"))
        other_context = db.get_or_create_context(TuningContextRecord(bios_version="2403", context_hash="other"))
        sid = _sid(db, selected_context)
        state = CoreState(
            core_id=0,
            phase=TunerPhase.CONFIRMED,
            current_offset=-31,
            best_offset=-30,
            proven_offset=-30,
            suspicion=1.5,
        )
        db.upsert_tuner_core_state(sid, state)
        db.insert_tuner_test_log(sid, 0, -30, "confirm", True, duration=60.0)
        for regime in ("boost", "current", "transient"):
            db.bank_regime_time(selected_context, 0, regime, -30, 7200.0)
            db.bank_regime_time(other_context, 0, regime, -30, 18000.0)
        db.bank_regime_time(selected_context, 0, "current", -30, 285.0)
        tab = _tab(db)
        tab._engine = _engine(sid, {0: state})

        tab._update_core_row(0)

        expected = report.core_row(db, sid, 0)
        assert tab._core_table.item(0, 0).data(Qt.ItemDataRole.UserRole) == expected
        headers = [tab._core_table.horizontalHeaderItem(c).text() for c in range(tab._core_table.columnCount())]
        assert tab._core_table.item(0, headers.index("Candidate")).text() == "-31"
        assert tab._core_table.item(0, headers.index("Accepted")).text() == "-30"
        assert tab._core_table.item(0, headers.index("Confidence")).text() == "-"
        assert tab._core_table.item(0, headers.index("Boost")).text() == "2h 00m"
        assert tab._core_table.item(0, headers.index("Current")).text() == "2h 04m"
        assert tab._core_table.item(0, headers.index("Coupled")).text() == "-"

    def test_sub_hour_banks_stay_distinguishable(self, db):
        context = db.get_or_create_context(TuningContextRecord(bios_version="2402", context_hash="selected"))
        sid = _sid(db, context)
        state = CoreState(core_id=0, phase=TunerPhase.CONFIRMED, current_offset=-50, best_offset=-50)
        db.upsert_tuner_core_state(sid, state)
        for regime, seconds in (("boost", 285.0), ("current", 454.0), ("transient", 307.0), ("coupled", 219.0)):
            db.bank_regime_time(context, 0, regime, -50, seconds)
        tab = _tab(db)
        tab._engine = _engine(sid, {0: state})

        tab._update_core_row(0)

        headers = [tab._core_table.horizontalHeaderItem(c).text() for c in range(tab._core_table.columnCount())]
        cells = {name: tab._core_table.item(0, headers.index(name)).text() for name in headers}
        assert cells["Boost"] == "4m 45s"
        assert cells["Current"] == "7m 34s"
        assert cells["Coupled"] == "3m 39s"
        assert cells["Confidence"] == "3m 39s"
        assert tab._core_table.verticalHeader().isHidden()

    def test_profile_quarantine_suppresses_the_accepted_offset(self, db):
        sid = _sid(db)
        state = CoreState(core_id=0, current_offset=-31, best_offset=-30, proven_offset=-30)
        db.upsert_tuner_core_state(sid, state)
        db.update_tuner_session_status(sid, "profile_quarantined")
        tab = _tab(db)
        tab._engine = _engine(sid, {0: state})

        tab._update_core_row(0)

        expected = report.core_row(db, sid, 0)
        assert expected["accepted_offset"] is None
        assert tab._core_table.item(0, 0).data(Qt.ItemDataRole.UserRole) == expected
        headers = [tab._core_table.horizontalHeaderItem(c).text() for c in range(tab._core_table.columnCount())]
        assert tab._core_table.item(0, headers.index("Accepted")).text() == "-"

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

    def test_core_row_shows_bios_recommendation_crashes_strikes_and_vcache(self, db):
        sid = _sid(db)
        state = CoreState(
            core_id=4,
            phase=TunerPhase.CONFIRMED,
            current_offset=-20,
            best_offset=-20,
            proven_offset=-20,
            crash_count=2,
            anneal_strikes=1,
        )
        db.upsert_tuner_core_state(sid, state)
        tab = _tab(db)
        tab._topology.cores[4] = PhysicalCore(core_id=4, ccd=1, logical_cpus=(4, 12), has_vcache=True)
        tab._engine = _engine(sid, {4: state})

        tab._update_core_row(4)

        headers = [tab._core_table.horizontalHeaderItem(c).text() for c in range(tab._core_table.columnCount())]
        expected = report.core_row(db, sid, 4)
        assert tab._core_table.item(0, headers.index("BIOS")).text() == str(expected["bios_offset"])
        assert tab._core_table.item(0, headers.index("Crashes")).text() == "2"
        assert tab._core_table.item(0, headers.index("Strikes")).text() == "1"
        assert "V-Cache" in tab._core_table.item(0, headers.index("CCD")).text()


class TestLogEntry:
    def test_add_log_entry_appends_row(self, db):
        sid = _sid(db)
        db.insert_tuner_test_log(sid, 0, -30, "confirm", True, duration=60.0)
        tab = _tab(db)
        tab._engine = _engine(sid, {})
        before = tab._log_table.rowCount()
        tab._add_log_entry(0, -30, True)
        assert tab._log_table.rowCount() == before + 1

    def test_add_log_entry_respects_selected_core(self, db):
        sid = _sid(db)
        db.insert_tuner_test_log(sid, 1, -30, "confirm", True, duration=60.0)
        tab = _tab(db)
        tab._engine = _engine(sid, {})
        tab._selected_core = 0
        before = tab._log_table.rowCount()
        tab._add_log_entry(1, -30, True)
        assert tab._log_table.rowCount() == before

    def test_log_row_names_the_regime_and_workload_that_ran(self, db):
        sid = _sid(db)
        db.insert_tuner_test_log(
            sid,
            0,
            -5,
            "coarse",
            True,
            duration=60.0,
            backend="mprime",
            stress_mode="AVX2",
            fft_preset="SMALL",
            threads=2,
            profile="transient",
            regime="transient",
        )
        tab = _tab(db)
        tab._engine = _engine(sid, {})

        tab._add_log_entry(0, -5, True)

        headers = [tab._log_table.horizontalHeaderItem(c).text() for c in range(tab._log_table.columnCount())]
        row = tab._log_table.rowCount() - 1
        assert tab._log_table.item(row, headers.index("Regime")).text() == "transient"
        assert tab._log_table.item(row, headers.index("Workload")).text() == "mprime AVX2 SMALL 2T transient"
        assert tab._log_table.item(row, headers.index("Result")).text() == "PASS"


_COARSE_SLOT = {
    "kind": "solo",
    "core": 0,
    "offset": -10,
    "phase": "coarse_search",
    "cores": [0],
    "regime": "transient",
    "battery_regimes": ["current", "transient"],
    "battery_position": 2,
    "backend": "mprime",
    "stress_mode": "AVX2",
    "fft_preset": "SMALL",
    "threads": 2,
    "profile": "transient",
    "duration_seconds": 60,
}


class TestSlotLine:
    def test_search_slot_shows_its_place_in_the_offset_battery(self, db):
        tab = _tab(db)
        tab._on_slot_started(json.dumps(_COARSE_SLOT))
        text = tab._slot_label.text()
        assert "core 0 at -10 (Coarse search)" in text
        assert "regime 2 of 2: transient (current, transient)" in text
        assert "mprime AVX2 SMALL 2T transient" in text
        assert "60 s" in text

    def test_running_slot_counts_elapsed_against_its_duration(self, db):
        tab = _tab(db)
        tab._on_slot_started(json.dumps(_COARSE_SLOT))
        tab._slot_started_at = time.monotonic() - 12.4
        tab._tick_tuner()
        assert "12/60 s" in tab._slot_label.text()

    def test_hunt_probe_shows_the_live_set(self, db):
        tab = _tab(db)
        slot = {
            "kind": "parallel",
            "cores": [0, 1],
            "regime": "current",
            "backend": "mprime",
            "stress_mode": "avx2",
            "fft_preset": "small",
            "duration_seconds": 1800,
            "hunt": {"stage": "probe", "level": 1, "live": [0, 1]},
        }
        tab._on_slot_started(json.dumps(slot))
        text = tab._slot_label.text()
        assert "Hunt probe (level 1): cores [0, 1] live, the rest at stock" in text
        assert "1800 s" in text

    def test_hunt_lead_probe_names_the_core_running_alone(self, db):
        tab = _tab(db)
        slot = {"cores": [3], "duration_seconds": 328, "hunt": {"stage": "lead", "level": 0, "live": [3]}}
        tab._on_slot_started(json.dumps(slot))
        assert "Hunt lead probe: core 3 alone, the rest at stock" in tab._slot_label.text()

    def test_validation_slot_names_its_stage_and_cores(self, db):
        tab = _tab(db)
        slot = {"kind": "parallel", "cores": [0, 1, 2], "validation_stage": 2, "duration_seconds": 300}
        tab._on_slot_started(json.dumps(slot))
        text = tab._slot_label.text()
        assert "Validation S2 (all-core)" in text
        assert "cores [0, 1, 2]" in text

    def test_finished_test_clears_the_slot(self, db):
        sid = _sid(db)
        tab = _tab(db)
        tab._engine = _engine(sid, {})
        tab._on_slot_started(json.dumps(_COARSE_SLOT))
        tab._on_test_completed(0, -10, True)
        assert tab._slot_label.text() == ""


class TestBatterySummary:
    def test_coarse_and_fine_show_regimes_and_time_per_offset(self, db):
        tab = _tab(db)
        tab._search_dur_spin.setValue(60)
        tab._confirm_dur_spin.setValue(300)
        text = tab._battery_label.text()
        assert "Coarse: current, transient - 2 x 60 s = 2 min per offset" in text
        assert "Fine: boost, current, transient, coupled - 4 x 60 s = 4 min per offset" in text
        assert "Confirm, anneal: 4 x 300 s = 20 min per offset" in text

    def test_a_resumed_session_shows_its_own_coarse_regimes(self, db):
        tab = _tab(db)
        tab._apply_config_to_ui(TunerConfig(coarse_regimes=["current"], search_duration_seconds=30))
        assert "Coarse: current - 1 x 30 s = 30 s per offset" in tab._battery_label.text()


class TestEventsPane:
    def test_tuner_narrative_is_shown_live(self, db):
        tab = _tab(db)
        tab._on_log_message("Hunt probe (level 1): live [0, 1], every other core at stock, for 600s")
        assert "Hunt probe (level 1)" in tab._events_view.toPlainText()

    def test_resume_replays_the_stored_story(self, db):
        sid = _sid(db)
        db.insert_tuner_event(sid, "Attribution hunt over [0, 1]")
        tab = _tab(db)
        tab._show_session_events(sid)
        assert "Attribution hunt over [0, 1]" in tab._events_view.toPlainText()


class TestSlots:
    def test_core_state_changed_updates_row(self, db):
        sid = _sid(db)
        state = CoreState(core_id=2, phase=TunerPhase.COARSE_SEARCH, current_offset=-20)
        db.upsert_tuner_core_state(sid, state)
        tab = _tab(db)
        tab._engine = _engine(sid, {2: state})
        tab._on_core_state_changed(2, "coarse_search", -20)
        assert tab._core_table.item(0, 0).data(Qt.ItemDataRole.UserRole)["core"] == 2

    def test_validation_progress_sets_label(self, db):
        tab = _tab(db)
        tab._on_validation_progress(2, 3, 8)
        assert tab._progress_label.text()

    @pytest.mark.parametrize(
        ("status", "label"),
        [("hunting", "Hunting"), ("platform_fault", "Platform fault"), ("profile_quarantined", "Quarantined")],
    )
    def test_new_session_statuses_have_readable_labels(self, db, status, label):
        tab = _tab(db)
        tab._on_status_changed(status)
        assert tab._status_label.text() == f"Status: {label}"

    def test_progress_updated_sets_label(self, db):
        tab = _tab(db)
        tab._on_progress_updated(4, 8)
        assert tab._progress_label.text()
