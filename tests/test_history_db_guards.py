"""HistoryDB boundary guards: corruption refusal, missing rows, merge skips.

Every one of these is a fail-closed path - a corrupt file, an absent session,
an insane persisted offset, or an orphaned row in a merged database. They must
raise or return a safe default, never quietly hand back nonsense.
"""

from __future__ import annotations

import sqlite3

import pytest

from corecycler.history.db import (
    CoreResultRecord,
    HistoryDB,
    InFlightRecord,
    LegacySession,
    RunRecord,
    TableSpec,
    TuningContextRecord,
)
from corecycler.tuner import persistence as tp
from corecycler.tuner.config import TunerConfig
from corecycler.tuner.state import CoreState, TunerPhase


@pytest.fixture
def db():
    d = HistoryDB(":memory:")
    yield d
    d.close()


class TestOpenGuards:
    def test_untyped_table_specs_have_no_record_projection(self, db):
        spec = TableSpec("raw", ("value",))

        assert spec.record_columns == ()
        with pytest.raises(TypeError, match="raw has no record type"):
            spec.decode(db._execute_raw("SELECT 1 AS value").fetchone())

    def test_incomplete_migration_script_is_refused(self):
        conn = sqlite3.connect(":memory:")
        try:
            with pytest.raises(RuntimeError, match="Incomplete SQL migration statement"):
                HistoryDB._execute_script(conn, "CREATE TABLE unfinished (")
        finally:
            conn.close()

    def test_fresh_schema_failure_is_rolled_back(self, tmp_path, monkeypatch):
        path = tmp_path / "history.db"

        def fail_schema(_conn, _script):
            raise RuntimeError("injected schema failure")

        monkeypatch.setattr(HistoryDB, "_execute_script", staticmethod(fail_schema))
        with pytest.raises(RuntimeError, match="injected schema failure"):
            HistoryDB(path)

        conn = sqlite3.connect(path)
        try:
            assert conn.execute("SELECT name FROM sqlite_master WHERE name='schema_version'").fetchone() is None
        finally:
            conn.close()

    def test_a_corrupt_file_is_refused(self, tmp_path):
        path = tmp_path / "history.db"
        path.write_bytes(b"SQLite format 3\x00" + b"\x00" * 512)
        with pytest.raises((RuntimeError, sqlite3.DatabaseError)):
            HistoryDB(path)

    def test_a_future_schema_version_is_refused(self, tmp_path):
        path = tmp_path / "history.db"
        HistoryDB(path).close()
        conn = sqlite3.connect(path)
        conn.execute("UPDATE schema_version SET version=?", (HistoryDB.SCHEMA_VERSION + 1,))
        conn.commit()
        conn.close()
        future, current = HistoryDB.SCHEMA_VERSION + 1, HistoryDB.SCHEMA_VERSION
        with pytest.raises(RuntimeError, match=rf"schema version {future}.*supports {current}"):
            HistoryDB(path)

        conn = sqlite3.connect(path)
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == HistoryDB.SCHEMA_VERSION + 1
        conn.close()

    def test_schema_version_requires_exactly_one_marker(self, tmp_path):
        path = tmp_path / "history.db"
        HistoryDB(path).close()
        conn = sqlite3.connect(path)
        conn.execute("INSERT INTO schema_version VALUES (?)", (HistoryDB.SCHEMA_VERSION,))
        conn.commit()
        conn.close()
        with pytest.raises(RuntimeError, match="exactly one"):
            HistoryDB(path)

    def test_missing_migration_is_refused(self, tmp_path, monkeypatch):
        path = tmp_path / "history.db"
        HistoryDB(path).close()
        conn = sqlite3.connect(path)
        conn.execute("UPDATE schema_version SET version=?", (HistoryDB.SCHEMA_VERSION - 1,))
        conn.commit()
        conn.close()
        monkeypatch.delitem(HistoryDB._MIGRATIONS, HistoryDB.SCHEMA_VERSION)

        with pytest.raises(RuntimeError, match=f"Missing migration for version {HistoryDB.SCHEMA_VERSION}"):
            HistoryDB(path)

    def test_failed_migration_is_rolled_back(self, tmp_path, monkeypatch):
        path = tmp_path / "history.db"
        HistoryDB(path).close()
        conn = sqlite3.connect(path)
        conn.execute("UPDATE schema_version SET version=?", (HistoryDB.SCHEMA_VERSION - 1,))
        conn.commit()
        conn.close()

        def fail_migration(conn):
            conn.execute("CREATE TABLE migration_canary (value INTEGER)")
            raise RuntimeError("injected migration failure")

        monkeypatch.setitem(HistoryDB._MIGRATIONS, HistoryDB.SCHEMA_VERSION, fail_migration)
        with pytest.raises(RuntimeError, match="injected migration failure"):
            HistoryDB(path)

        conn = sqlite3.connect(path)
        try:
            assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == HistoryDB.SCHEMA_VERSION - 1
            assert conn.execute("SELECT name FROM sqlite_master WHERE name='migration_canary'").fetchone() is None
        finally:
            conn.close()

    def test_v21_migration_marks_sessions_without_contract_version_as_legacy(self, tmp_path):
        path = tmp_path / "history.db"
        db = HistoryDB(path)
        session_id = db.create_tuner_session("{}", "", "")
        db.close()
        conn = sqlite3.connect(path)
        conn.execute("ALTER TABLE tuner_sessions DROP COLUMN contract_version")
        conn.execute("UPDATE schema_version SET version=20")
        conn.commit()
        conn.close()

        migrated = HistoryDB(path)
        try:
            columns = {row[1] for row in migrated._execute_raw("PRAGMA table_info(tuner_sessions)").fetchall()}
            assert "contract_version" in columns
            with pytest.raises(LegacySession, match=rf"Session {session_id} predates tuner contract v16"):
                migrated.get_tuner_session(session_id, resumable=True)
        finally:
            migrated.close()

    def test_v22_migration_renames_legacy_quarantined_sessions(self, tmp_path):
        path = tmp_path / "history.db"
        db = HistoryDB(path)
        legacy = db.create_tuner_session("{}", "", "")
        paused = db.create_tuner_session("{}", "", "")
        db.update_tuner_session_status(legacy, "quarantined")
        db.update_tuner_session_status(paused, "paused")
        db.close()
        conn = sqlite3.connect(path)
        conn.execute("UPDATE schema_version SET version=21")
        conn.commit()
        conn.close()

        migrated = HistoryDB(path)
        try:
            assert migrated.get_tuner_session(legacy).status == "profile_quarantined"
            assert migrated.get_tuner_session(paused).status == "paused"
        finally:
            migrated.close()


class TestMissingRows:
    def test_an_absent_session_has_no_latest(self, db):
        assert db.get_latest_tuner_session() is None

    def test_an_absent_session_reports_no_unattributed_crashes(self, db):
        assert db.get_unattributed_crashes(999) == 0

    def test_an_absent_session_reports_no_crash_streak(self, db):
        assert db.get_resume_crash_streak(999) == 0


class TestDeletionGuards:
    def test_running_run_cannot_be_deleted(self, db):
        run_id = db.create_run(RunRecord())
        with pytest.raises(InFlightRecord, match=rf"run {run_id}.*running"):
            db.delete_run(run_id)
        assert db.get_run(run_id) is not None

    @pytest.mark.parametrize("status", ["running", "paused", "validating", "hunting"])
    def test_in_flight_tuner_session_cannot_be_deleted(self, db, status):
        session_id = db.create_tuner_session("{}", "", "")
        db.update_tuner_session_status(session_id, status)
        with pytest.raises(InFlightRecord, match=rf"tuner session {session_id}.*{status}"):
            db.delete_tuner_session(session_id)

    def test_context_delete_is_atomic_when_a_child_is_in_flight(self, db):
        context_id = db.get_or_create_context(TuningContextRecord(context_hash="ctx"))
        finished = db.create_run(RunRecord(context_id=context_id, status="completed"))
        running = db.create_run(RunRecord(context_id=context_id))
        with pytest.raises(InFlightRecord, match=rf"run {running}.*running"):
            db.delete_context_cascade(context_id)
        assert db.get_run(finished) is not None
        assert db.get_run(running) is not None
        assert db.get_context(context_id) is not None

    def test_context_delete_is_atomic_when_a_tuner_session_is_in_flight(self, db):
        context_id = db.get_or_create_context(TuningContextRecord(context_hash="ctx"))
        session_id = db.create_tuner_session("{}", "", "", context_id=context_id)

        with pytest.raises(InFlightRecord, match=rf"tuner session {session_id}.*running"):
            db.delete_context_cascade(context_id)

        assert db.get_tuner_session(session_id) is not None
        assert db.get_context(context_id) is not None


class TestLegacySessions:
    def test_explicit_resumable_load_refuses_legacy_contract(self, db):
        session_id = db.create_tuner_session("{}", "", "")
        db._execute_raw("UPDATE tuner_sessions SET contract_version=0 WHERE id=?", (session_id,))
        with pytest.raises(
            LegacySession,
            match=(
                rf"Session {session_id} predates tuner contract v16; start a new search "
                rf"with corecycler tune --seed-from {session_id}"
            ),
        ):
            db.get_tuner_session(session_id, resumable=True)
        assert db.get_tuner_session(session_id) is not None

    def test_automatic_resume_excludes_legacy_contract(self, db):
        session_id = db.create_tuner_session("{}", "", "")
        db._execute_raw("UPDATE tuner_sessions SET contract_version=0 WHERE id=?", (session_id,))
        assert db.get_active_tuner_session() is None
        assert db.list_resumable_tuner_sessions() == []


class TestCoreStateSanity:
    def _session(self, db):
        return db.create_tuner_session(TunerConfig().to_json(), "2402", "Test")

    def test_an_offset_outside_the_sane_range_is_refused(self, db):
        sid = self._session(db)
        with pytest.raises(ValueError, match="outside sane CO range"):
            db.upsert_tuner_core_state(sid, CoreState(core_id=0, current_offset=-9999))

    def test_a_negative_counter_is_refused(self, db):
        sid = self._session(db)
        with pytest.raises(ValueError, match="negative"):
            db.upsert_tuner_core_state(sid, CoreState(core_id=0, crash_count=-1))

    def test_negative_accumulated_time_is_refused(self, db):
        sid = self._session(db)
        with pytest.raises(ValueError, match="cumulative_test_time"):
            db.upsert_tuner_core_state(sid, CoreState(core_id=0, cumulative_test_time=-1.0))


def _seed(path, *, runs=1, sessions=1):
    db = HistoryDB(path)
    for i in range(runs):
        rid = db.create_run(
            RunRecord(
                started_at=f"2026-07-2{i}T10:00:00+00:00",
                status="completed",
                backend="mprime",
                total_cores=1,
                cores_passed=1,
            )
        )
        db.insert_core_result(
            CoreResultRecord(run_id=rid, core_id=0, started_at="2026-07-20T10:00:00+00:00", passed=True)
        )
    for _ in range(sessions):
        sid = db.create_tuner_session(TunerConfig().to_json(), "2402", "Test")
        db.get_or_create_context(
            TuningContextRecord(bios_version="2402", co_offsets_json="{}", context_hash=f"seed{sid}")
        )
        db.upsert_tuner_core_state(sid, CoreState(core_id=0, phase=TunerPhase.CONFIRMED, best_offset=-20))
        tp.journal_co_intent(db, sid, 0, -20, False)
        db.insert_tuner_test_log(sid, 0, -20, "confirm", True, duration=300.0)
    db.close()
    return db


class TestMerge:
    def test_rows_from_another_database_are_adopted(self, tmp_path):
        other = tmp_path / "other.db"
        _seed(other)
        target = HistoryDB(tmp_path / "target.db")
        counts = target.merge_from(other)
        assert counts["runs"] == 1
        assert counts["tuner_sessions"] == 1
        assert counts["contexts"] == 1
        assert len(target.list_runs()) == 1
        assert len(target.list_tuner_sessions()) == 1
        target.close()

    def test_a_duplicate_context_is_deduplicated(self, tmp_path):
        other_path = tmp_path / "other.db"
        other = HistoryDB(other_path)
        ctx = TuningContextRecord(bios_version="2402", co_offsets_json="{}", context_hash="same")
        cid = other.get_or_create_context(ctx)
        other.create_run(RunRecord(started_at="2026-07-20T10:00:00+00:00", status="completed", context_id=cid))
        other.close()

        target = HistoryDB(tmp_path / "target.db")
        target.get_or_create_context(
            TuningContextRecord(bios_version="2402", co_offsets_json="{}", context_hash="same")
        )
        counts = target.merge_from(other_path)
        assert counts["contexts"] == 0
        assert len(target.list_contexts()) == 1
        assert len(target.list_runs()) == 1
        target.close()

    def test_an_orphaned_journal_row_is_skipped(self, tmp_path):
        other_path = tmp_path / "other.db"
        _seed(other_path, runs=0)
        raw = sqlite3.connect(other_path)
        raw.execute("PRAGMA foreign_keys=OFF")
        raw.execute("DELETE FROM tuner_sessions")
        raw.commit()
        raw.close()

        target = HistoryDB(tmp_path / "target.db")
        counts = target.merge_from(other_path)
        assert counts["tuner_sessions"] == 0
        assert target.list_tuner_sessions() == []
        target.close()


class TestContextIdentity:
    def test_the_same_profile_reuses_its_context(self, db):
        first = db.get_or_create_context(
            TuningContextRecord(bios_version="2402", co_offsets_json="{}", context_hash="abc")
        )
        second = db.get_or_create_context(
            TuningContextRecord(bios_version="2402", co_offsets_json="{}", context_hash="abc")
        )
        assert first == second
        assert len(db.list_contexts()) == 1


class TestFailClosedOnBadInput:
    def test_a_context_that_cannot_be_stored_is_refused_loudly(self, db):
        with pytest.raises(RuntimeError, match="database inconsistent"):
            db.get_or_create_context(TuningContextRecord(bios_version="2402", co_offsets_json="{}", context_hash=None))

    def test_a_merge_that_cannot_complete_leaves_nothing_behind(self, tmp_path):
        """All-or-nothing: a source holding a child row whose parent is gone
        must roll the whole merge back, not half-import it."""
        source = tmp_path / "orphaned.db"
        _seed(source)
        raw = sqlite3.connect(source)
        raw.execute("PRAGMA foreign_keys=OFF")
        raw.execute("DELETE FROM runs")
        raw.commit()
        raw.close()

        target = HistoryDB(tmp_path / "target.db")
        _seed_run_only(target)
        before = len(target.list_runs())
        with pytest.raises(sqlite3.Error):
            target.merge_from(source)
        assert len(target.list_runs()) == before
        assert target.list_tuner_sessions() == []
        target.close()

    def test_a_corrupted_page_is_refused_at_open(self, tmp_path):
        path = tmp_path / "history.db"
        seeded = HistoryDB(path)
        for i in range(400):
            seeded.create_run(RunRecord(started_at=f"2026-07-20T10:00:{i % 60:02d}+00:00", status="completed"))
        seeded.close()

        raw = bytearray(path.read_bytes())
        assert len(raw) > 16384
        raw[-4096:] = b"\xde\xad\xbe\xef" * 1024
        path.write_bytes(bytes(raw))

        with pytest.raises(RuntimeError, match="failed integrity check"):
            HistoryDB(path)


def test_proven_offset_round_trips_through_core_state(db):
    sid = db.create_tuner_session(TunerConfig().to_json(), "2402", "Test")
    db.upsert_tuner_core_state(sid, CoreState(core_id=0, proven_offset=-31))
    assert db.get_tuner_core_states(sid)[0].proven_offset == -31

    db.upsert_tuner_core_state(sid, CoreState(core_id=0, proven_offset=-30))
    assert db.get_tuner_core_states(sid)[0].proven_offset == -30


def _seed_run_only(db):
    return db.create_run(RunRecord(started_at="2026-07-20T10:00:00+00:00", status="completed"))
