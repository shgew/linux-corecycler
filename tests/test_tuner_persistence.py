"""Tests for tuner persistence layer — sessions, core states, test log."""

from __future__ import annotations

import pytest

from corecycler import __version__
from corecycler.history.db import HistoryDB, TuningContextRecord
from corecycler.tuner import bisect
from corecycler.tuner.config import TunerConfig
from corecycler.tuner.persistence import (
    create_session,
    get_active_session,
    get_best_profile,
    get_latest_session,
    get_session,
    get_test_log,
    load_core_states,
    log_test_result,
    save_core_state,
    set_hunt_state,
    update_session_status,
)
from corecycler.tuner.state import CoreState, TunerPhase


@pytest.fixture
def db():
    """In-memory database with the current schema."""
    d = HistoryDB(":memory:")
    yield d
    d.close()


class TestTunerSessions:
    def test_v16_recovery_boot_is_taken_from_last_event_not_wall_clock(self, tmp_path):
        path = tmp_path / "legacy.db"
        db = HistoryDB(path)
        sid = create_session(db, TunerConfig(), "", "")
        empty = create_session(db, TunerConfig(), "", "")
        db.insert_tuner_event(sid, "older boot", "old")
        db.insert_tuner_event(sid, "last execution", "latest")
        db._execute_raw("UPDATE tuner_events SET timestamp='2099-01-01' WHERE boot_id='old'")
        db._execute_raw("ALTER TABLE tuner_sessions DROP COLUMN boot_id")
        db._execute_raw("ALTER TABLE tuner_sessions DROP COLUMN hunt_state")
        db._execute_raw("ALTER TABLE tuner_test_log DROP COLUMN regime")
        db._execute_raw("DROP TABLE tuner_regime_banks")
        db._execute_raw("UPDATE schema_version SET version=16")
        db.close()

        reopened = HistoryDB(path)
        assert reopened.get_tuner_session(sid).boot_id == "latest"
        assert reopened.get_tuner_session(empty).boot_id == ""
        reopened.set_session_boot(sid, "resumed")
        reopened.close()
        reopened = HistoryDB(path)
        assert reopened.get_tuner_session(sid).boot_id == "resumed"
        reopened.close()

    def test_new_sessions_are_stamped_with_the_running_build(self, db):
        sid = create_session(db, TunerConfig(), "", "")
        assert get_session(db, sid).app_version == __version__

    def test_v17_sessions_gain_an_empty_app_version(self, tmp_path):
        path = tmp_path / "legacy.db"
        db = HistoryDB(path)
        sid = create_session(db, TunerConfig(), "", "")
        db._execute_raw("ALTER TABLE tuner_sessions DROP COLUMN app_version")
        db._execute_raw("ALTER TABLE tuner_sessions DROP COLUMN hunt_state")
        db._execute_raw("ALTER TABLE tuner_test_log DROP COLUMN regime")
        db._execute_raw("DROP TABLE tuner_regime_banks")
        db._execute_raw("UPDATE schema_version SET version=17")
        db.close()

        reopened = HistoryDB(path)
        assert reopened.get_tuner_session(sid).app_version == ""
        fresh = create_session(reopened, TunerConfig(), "", "")
        assert reopened.get_tuner_session(fresh).app_version == __version__
        reopened.close()

    def test_create_and_get_session(self, db):
        cfg = TunerConfig(coarse_step=10)
        sid = create_session(db, cfg, "BIOS-1.0", "Ryzen 9 9950X3D")
        assert sid > 0

        s = get_session(db, sid)
        assert s is not None
        assert s.id == sid
        assert s.status == "running"
        assert s.bios_version == "BIOS-1.0"
        assert s.cpu_model == "Ryzen 9 9950X3D"
        assert "coarse_step" in s.config_json

    def test_update_session_status(self, db):
        cfg = TunerConfig()
        sid = create_session(db, cfg, "", "")
        update_session_status(db, sid, "paused")
        s = get_session(db, sid)
        assert s.status == "paused"

    def test_get_latest_session(self, db):
        cfg = TunerConfig()
        create_session(db, cfg, "", "CPU1")
        sid2 = create_session(db, cfg, "", "CPU2")
        latest = get_latest_session(db)
        assert latest.id == sid2

    def test_get_active_session_running(self, db):
        cfg = TunerConfig()
        sid = create_session(db, cfg, "", "")
        active = get_active_session(db)
        assert active is not None
        assert active.id == sid

    def test_get_active_session_paused(self, db):
        cfg = TunerConfig()
        sid = create_session(db, cfg, "", "")
        update_session_status(db, sid, "paused")
        active = get_active_session(db)
        assert active is not None
        assert active.id == sid

    def test_get_active_session_none_when_completed(self, db):
        cfg = TunerConfig()
        sid = create_session(db, cfg, "", "")
        update_session_status(db, sid, "completed")
        active = get_active_session(db)
        assert active is None

    def test_get_session_not_found(self, db):
        assert get_session(db, 999) is None

    def test_hunt_state_round_trips_as_a_serialized_state_machine(self, db):
        sid = create_session(db, TunerConfig(), "", "")
        hunt = bisect.begin([0, 1, 2, 3], loaded=[1, 3])
        hunt.control_fails = 1
        hunt.level = 2
        tp_blob = hunt.to_json()

        set_hunt_state(db, sid, tp_blob)

        restored = bisect.HuntState.from_json(get_session(db, sid).hunt_state)
        assert restored == hunt


class TestCoreStates:
    def test_save_and_load(self, db):
        sid = create_session(db, TunerConfig(), "", "")
        cs0 = CoreState(
            core_id=0,
            phase=TunerPhase.COARSE_SEARCH,
            current_offset=-5,
            best_offset=0,
            battery_index=2,
            anneal_strikes=1,
            anneal_bar_hours=12.5,
            suspicion=3.75,
        )
        save_core_state(db, sid, cs0)
        save_core_state(db, sid, CoreState(core_id=1, phase=TunerPhase.NOT_STARTED))

        loaded = load_core_states(db, sid)
        assert len(loaded) == 2
        assert loaded[0].phase == TunerPhase.COARSE_SEARCH
        assert loaded[0].current_offset == -5
        assert loaded[0].best_offset == 0
        assert loaded[0].battery_index == 2
        assert loaded[0].anneal_strikes == 1
        assert loaded[0].anneal_bar_hours == pytest.approx(12.5)
        assert loaded[0].suspicion == pytest.approx(3.75)
        assert loaded[1].phase == TunerPhase.NOT_STARTED

    def test_upsert_updates_existing(self, db):
        cfg = TunerConfig()
        sid = create_session(db, cfg, "", "")

        cs = CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH, current_offset=-5)
        save_core_state(db, sid, cs)

        cs.phase = TunerPhase.FINE_SEARCH
        cs.current_offset = -8
        cs.best_offset = -5
        save_core_state(db, sid, cs)

        loaded = load_core_states(db, sid)
        assert len(loaded) == 1
        assert loaded[0].phase == TunerPhase.FINE_SEARCH
        assert loaded[0].current_offset == -8
        assert loaded[0].best_offset == -5

    def test_thermal_aborts_round_trips(self, db):
        # The thermal retry cap must survive pause/resume/reboot, otherwise the
        # "cooling cannot sustain testing" abort silently never fires.
        cfg = TunerConfig()
        sid = create_session(db, cfg, "", "")
        cs = CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH, current_offset=-10, thermal_aborts=2)
        save_core_state(db, sid, cs)
        loaded = load_core_states(db, sid)
        assert loaded[0].thermal_aborts == 2

    def test_load_empty(self, db):
        cfg = TunerConfig()
        sid = create_session(db, cfg, "", "")
        loaded = load_core_states(db, sid)
        assert loaded == {}


class TestTestLog:
    def test_log_and_query(self, db):
        cfg = TunerConfig()
        sid = create_session(db, cfg, "", "")

        log_test_result(db, sid, 0, -5, "coarse", True, duration=60.0)
        log_test_result(db, sid, 0, -10, "coarse", True, duration=60.0)
        log_test_result(db, sid, 0, -15, "coarse", False, error_msg="MCE", duration=30.0)

        log_entries = get_test_log(db, sid, core_id=0)
        assert len(log_entries) == 3
        assert log_entries[0]["offset_tested"] == -5
        assert log_entries[0]["passed"] == 1
        assert log_entries[2]["passed"] == 0
        assert log_entries[2]["error_message"] == "MCE"

    def test_log_filter_by_core(self, db):
        cfg = TunerConfig()
        sid = create_session(db, cfg, "", "")

        log_test_result(db, sid, 0, -5, "coarse", True)
        log_test_result(db, sid, 1, -5, "coarse", True)

        log0 = get_test_log(db, sid, core_id=0)
        log1 = get_test_log(db, sid, core_id=1)
        log_all = get_test_log(db, sid)

        assert len(log0) == 1
        assert len(log1) == 1
        assert len(log_all) == 2

    def test_log_all_fields(self, db):
        cfg = TunerConfig()
        sid = create_session(db, cfg, "", "")

        lid = log_test_result(
            db,
            sid,
            3,
            -20,
            "fine",
            False,
            error_msg="computation error",
            error_type="computation",
            duration=45.5,
            run_id=None,
        )
        assert lid > 0

        entries = get_test_log(db, sid, core_id=3)
        assert len(entries) == 1
        e = entries[0]
        assert e["core_id"] == 3
        assert e["offset_tested"] == -20
        assert e["phase"] == "fine"
        assert e["error_type"] == "computation"
        assert e["duration_seconds"] == pytest.approx(45.5)


class TestRegimeBanks:
    def test_clean_time_accumulates_per_regime_and_clear_is_core_scoped(self, db):
        db.bank_regime_time("ctx", 0, "boost", -20, 30.0)
        db.bank_regime_time("ctx", 0, "boost", -20, 45.0)
        db.bank_regime_time("ctx", 0, "current", -20, 60.0)
        db.bank_regime_time("ctx", 1, "boost", -18, 90.0)

        assert db.get_regime_banks("ctx", 0, -20) == {"boost": 75.0, "current": 60.0}
        db.clear_regime_banks("ctx", 0)
        assert db.get_regime_banks("ctx", 0, -20) == {}
        assert db.get_regime_banks("ctx", 1, -18) == {"boost": 90.0}

    def test_regime_yield_groups_failures_and_time_by_context(self, db):
        context_id = db.create_context(TuningContextRecord(bios_version="1.0", co_hash="ctx"))
        sid = create_session(db, TunerConfig(), "1.0", "CPU", context_id=context_id)
        log_test_result(db, sid, 0, -20, "fine", True, duration=60.0, regime="boost")
        log_test_result(db, sid, 0, -20, "fine", False, duration=30.0, regime="boost")
        log_test_result(db, sid, 1, -18, "fine", False, duration=40.0, regime="current")

        other_id = db.create_context(TuningContextRecord(bios_version="1.0", co_hash="other"))
        other_sid = create_session(db, TunerConfig(), "1.0", "CPU", context_id=other_id)
        log_test_result(db, other_sid, 0, -20, "fine", False, duration=999.0, regime="boost")

        assert db.regime_yield("ctx") == {"boost": (1, 90.0), "current": (1, 40.0)}


class TestBestProfile:
    def test_confirmed_cores_only(self, db):
        cfg = TunerConfig()
        sid = create_session(db, cfg, "", "")

        save_core_state(
            db,
            sid,
            CoreState(
                core_id=0,
                phase=TunerPhase.CONFIRMED,
                current_offset=-30,
                best_offset=-30,
            ),
        )
        save_core_state(
            db,
            sid,
            CoreState(
                core_id=1,
                phase=TunerPhase.CONFIRMED,
                current_offset=-25,
                best_offset=-25,
            ),
        )
        save_core_state(
            db,
            sid,
            CoreState(
                core_id=2,
                phase=TunerPhase.FINE_SEARCH,
                current_offset=-20,
                best_offset=-15,
            ),
        )

        profile = get_best_profile(db, sid)
        assert profile == {0: -30, 1: -25}

    def test_empty_when_no_confirmed(self, db):
        cfg = TunerConfig()
        sid = create_session(db, cfg, "", "")
        save_core_state(db, sid, CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH))
        profile = get_best_profile(db, sid)
        assert profile == {}


class TestSchemaMigration:
    def test_fresh_db_has_v19_tuner_schema(self, db):
        tables = db._execute_raw("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        table_names = [t["name"] for t in tables]
        assert {"tuner_sessions", "tuner_core_states", "tuner_test_log", "tuner_regime_banks"} <= set(table_names)
        log_columns = {row["name"] for row in db._execute_raw("PRAGMA table_info(tuner_test_log)").fetchall()}
        assert "regime" in log_columns

    def test_schema_version_is_current(self, db):
        version = db._execute_raw("SELECT version FROM schema_version").fetchone()[0]
        assert version == HistoryDB.SCHEMA_VERSION


class TestSchemaV11:
    def test_schema_version_is_current(self, tmp_path):
        db = HistoryDB(tmp_path / "test.db")
        try:
            row = db._execute_raw("SELECT version FROM schema_version").fetchone()
            assert row[0] == HistoryDB.SCHEMA_VERSION
        finally:
            db.close()

    def test_co_journal_and_breaker_round_trip(self, tmp_path):
        """The CO write-ahead journal and resume-crash-streak column work on a
        fresh database (exercises _DDL_FRESH + the new accessors)."""
        db = HistoryDB(tmp_path / "test.db")
        try:
            sid = db.create_tuner_session("{}", "1.0", "TestCPU")
            # New sessions start with a zeroed circuit breaker.
            assert db.get_resume_crash_streak(sid) == 0
            # Aggressive value -> suspect; 0 -> survived.
            db.journal_co_intent(sid, 0, -30, survived=False)
            db.journal_co_intent(sid, 1, 0, survived=True)
            assert db.journal_suspects(sid) == [(0, -30)]
            # Marking survived clears suspects.
            db.journal_mark_survived(sid)
            assert db.journal_suspects(sid) == []
            assert db.journal_survived_values(sid) == {0: -30, 1: 0}
            # Breaker is read/write.
            db.set_resume_crash_streak(sid, 3)
            assert db.get_resume_crash_streak(sid) == 3
        finally:
            db.close()

    def test_core_state_crash_fields_persist(self, tmp_path):
        db = HistoryDB(tmp_path / "test.db")
        try:
            sid = db.create_tuner_session("{}", "1.0", "TestCPU")
            cs = CoreState(
                core_id=0,
                crash_count=2,
                crash_cooldown=1,
                thermal_aborts=3,
                cumulative_test_time=3600.5,
            )
            db.upsert_tuner_core_state(sid, cs)
            states = db.get_tuner_core_states(sid)
            assert states[0].crash_count == 2
            assert states[0].crash_cooldown == 1
            assert states[0].thermal_aborts == 3
            assert abs(states[0].cumulative_test_time - 3600.5) < 0.01
        finally:
            db.close()

    def test_test_log_has_workload_and_regime_fields(self, tmp_path):
        db = HistoryDB(tmp_path / "test.db")
        try:
            sid = db.create_tuner_session("{}", "1.0", "TestCPU")
            db.insert_tuner_test_log(
                sid,
                core_id=0,
                offset=-30,
                phase="fine",
                passed=True,
                error_msg=None,
                error_type=None,
                duration=300.0,
                run_id=None,
                backend="mprime",
                stress_mode="AVX2",
                fft_preset="SMALL",
                regime="current",
            )
            log = db.get_tuner_test_log(sid)[0]
            assert (log["backend"], log["stress_mode"], log["fft_preset"]) == ("mprime", "AVX2", "SMALL")
            assert log["regime"] == "current"
        finally:
            db.close()


class TestSchemaV13:
    def test_peak_stretch_round_trips(self, tmp_path):
        db = HistoryDB(tmp_path / "test.db")
        try:
            sid = db.create_tuner_session("{}", "1.0", "TestCPU")
            db.insert_tuner_test_log(
                sid,
                0,
                -20,
                "confirm",
                True,
                peak_stretch_pct=1.7,
            )
            db.insert_tuner_test_log(sid, 1, -20, "confirm", True)
            logs = db.get_tuner_test_log(sid)
            assert logs[0]["peak_stretch_pct"] == pytest.approx(1.7)
            assert logs[1]["peak_stretch_pct"] is None
        finally:
            db.close()

    def test_unattributed_crash_counter_round_trips(self, tmp_path):
        db = HistoryDB(tmp_path / "test.db")
        try:
            sid = db.create_tuner_session("{}", "1.0", "TestCPU")
            assert db.get_unattributed_crashes(sid) == 0
            db.set_unattributed_crashes(sid, 2)
            assert db.get_unattributed_crashes(sid) == 2
            assert db.get_tuner_session(sid).unattributed_crashes == 2
        finally:
            db.close()

    def test_hunting_core_round_trips_and_clears(self, tmp_path):
        db = HistoryDB(tmp_path / "test.db")
        try:
            sid = db.create_tuner_session("{}", "1.0", "TestCPU")
            assert db.get_tuner_session(sid).hunting_core is None
            db.set_hunting_core(sid, 5)
            assert db.get_tuner_session(sid).hunting_core == 5
            db.set_hunting_core(sid, None)
            assert db.get_tuner_session(sid).hunting_core is None
        finally:
            db.close()

    def test_journal_values_returns_last_write_survived_or_not(self, tmp_path):
        db = HistoryDB(tmp_path / "test.db")
        try:
            sid = db.create_tuner_session("{}", "1.0", "TestCPU")
            db.journal_co_intent(sid, 0, -41, survived=True)
            db.journal_co_intent(sid, 1, -30, survived=False)
            assert db.journal_values(sid) == {0: -41, 1: -30}
        finally:
            db.close()

    def test_context_power_limits_round_trip(self, tmp_path):
        db = HistoryDB(tmp_path / "test.db")
        try:
            ctx = TuningContextRecord(
                bios_version="2101",
                co_hash="abc",
                ppt_limit_w=225.0,
                tdc_limit_a=190.0,
                edc_limit_a=None,
            )
            cid = db.create_context(ctx)
            loaded = db.get_context(cid)
            assert loaded.ppt_limit_w == pytest.approx(225.0)
            assert loaded.tdc_limit_a == pytest.approx(190.0)
            assert loaded.edc_limit_a is None
        finally:
            db.close()


class TestSchemaV15Narrative:
    def test_events_round_trip_newest_last(self, tmp_path):
        db = HistoryDB(tmp_path / "test.db")
        try:
            sid = db.create_tuner_session("{}", "1.0", "TestCPU")
            for i in range(5):
                db.insert_tuner_event(sid, f"line {i}", boot_id="boot-a")
            events = db.get_tuner_events(sid, limit=3)
            assert [e["message"] for e in events] == ["line 2", "line 3", "line 4"]
            assert events[0]["boot_id"] == "boot-a"
            assert events[0]["severity"] == "info"
        finally:
            db.close()

    def test_pick_auto_resume_session_statuses(self, tmp_path):
        from corecycler.tuner import persistence as tp

        db = HistoryDB(tmp_path / "test.db")
        try:
            assert tp.pick_auto_resume_session(db) is None
            sid = db.create_tuner_session("{}", "1.0", "TestCPU")
            assert tp.pick_auto_resume_session(db).id == sid  # running
            db.update_tuner_session_status(sid, "validating")
            assert tp.pick_auto_resume_session(db).id == sid
            db.update_tuner_session_status(sid, "paused")
            assert tp.pick_auto_resume_session(db) is None  # human choice
            db.update_tuner_session_status(sid, "quarantined")
            assert tp.pick_auto_resume_session(db) is None
        finally:
            db.close()
