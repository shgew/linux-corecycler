"""Crash-safe test history database using SQLite WAL mode.

Every write is an auto-commit transaction.  WAL + synchronous=NORMAL gives
process-crash safety with good performance - data survives kill -9 and OOM.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

from corecycler import __version__
from corecycler.config.paths import fix_sudo_ownership, user_home

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    from corecycler.history.context import SystemContext
    from corecycler.tuner.state import CoreState, TunerSession


DATA_DIR = user_home() / ".local" / "share" / "corecycler" / "history"
DEFAULT_DB_PATH = DATA_DIR / "history.db"

# Legacy location of root-owned history data from sudo runs.
LEGACY_ROOT_DB = Path("/root/.local/share/corecycler/history/history.db")

log = logging.getLogger(__name__)

RESUMABLE_STATUSES = ("running", "paused", "validating", "hunting")
RECOVERABLE_STATUSES = (*RESUMABLE_STATUSES, "profile_quarantined", "aborted")


class InFlightRecord(ValueError):
    pass


class LegacySession(ValueError):
    pass


def adopt_legacy_root_db(db: HistoryDB, root_db: Path = LEGACY_ROOT_DB) -> dict[str, int] | None:
    """One-time adoption of a root-owned history database.

    Sudo runs may have left history under /root; this merges that data into
    the user's (single) database and renames the source ``*.adopted`` so it
    can never be merged twice or silently diverge again. Only possible when
    running as root - the file is unreadable otherwise. Returns the merge
    counts, or None when there was nothing to adopt.
    """
    if os.geteuid() != 0:
        return None
    try:
        if not root_db.exists():
            return None
        if root_db.resolve() == db._db_path.resolve():
            return None  # HOME really is /root (no SUDO_USER) - same file
    except OSError:
        return None
    counts = db.merge_from(root_db)
    root_db.replace(root_db.with_name(root_db.name + ".adopted"))
    for sidecar in ("-wal", "-shm"):
        leftover = root_db.with_name(root_db.name + sidecar)
        if leftover.exists():
            leftover.unlink()
    log.info("Adopted legacy root history database %s: %s", root_db, counts)
    return counts


# ---------------------------------------------------------------------------
# Record dataclasses
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RunRecord:
    id: int | None = None
    started_at: str = ""  # ISO 8601 UTC
    finished_at: str | None = None
    status: str = "running"  # running, completed, stopped, crashed
    cpu_model: str = ""
    physical_cores: int = 0
    logical_cpus: int = 0
    ccds: int = 0
    is_x3d: bool = False
    # test settings snapshot (JSON blob)
    backend: str = ""
    stress_mode: str = ""
    fft_preset: str = ""
    seconds_per_core: int = 0
    cycle_count: int = 1
    stop_on_error: bool = False
    variable_load: bool = False
    idle_stability_test: float = 0.0
    max_temperature: float = 95.0
    settings_json: str = "{}"
    # tuning context (v2)
    context_id: int | None = None
    bios_version: str = ""
    # summary (filled on finish)
    total_cores: int = 0
    cores_passed: int = 0
    cores_failed: int = 0
    total_seconds: float = 0.0


@dataclass(slots=True)
class CoreResultRecord:
    id: int | None = None
    run_id: int = 0
    core_id: int = 0
    ccd: int | None = None
    cycle: int = 0
    started_at: str = ""
    finished_at: str | None = None
    passed: bool | None = None  # None while running
    error_message: str | None = None
    error_type: str | None = None
    elapsed_seconds: float = 0.0
    iterations_completed: int = 0
    peak_freq_mhz: float | None = None
    max_temp_c: float | None = None
    min_vcore_v: float | None = None
    max_vcore_v: float | None = None


@dataclass(slots=True)
class EventRecord:
    id: int | None = None
    run_id: int = 0
    timestamp: str = ""  # ISO 8601 UTC
    event_type: str = ""  # core_start, core_finish, error, phase_change, thermal, stall, cycle, info
    core_id: int | None = None
    message: str = ""
    details_json: str | None = None


@dataclass(slots=True)
class TuningContextRecord:
    id: int | None = None
    created_at: str = ""
    bios_version: str = ""
    cpu_model: str = ""
    physical_cores: int = 0
    ccds: int = 0
    co_offsets_json: str = "{}"
    context_hash: str = ""
    pbo_scalar: float | None = None
    boost_limit_mhz: int | None = None
    notes: str = ""
    ppt_limit_w: float | None = None
    tdc_limit_a: float | None = None
    edc_limit_a: float | None = None


@dataclass(slots=True)
class TelemetrySample:
    id: int | None = None
    run_id: int = 0
    core_id: int = 0
    timestamp: str = ""
    freq_mhz: float | None = None
    effective_max_mhz: float | None = None  # scaling_max_freq - boost ceiling for clock stretch detection
    temp_c: float | None = None
    vcore_v: float | None = None


@dataclass(frozen=True, slots=True)
class TableSpec:
    name: str
    columns: tuple[str, ...]
    record_type: type | None = None
    bool_columns: frozenset[str] = frozenset()
    decoders: tuple[tuple[str, Callable[[Any], Any]], ...] = ()

    @classmethod
    def for_record(
        cls,
        name: str,
        record_type: type,
        *,
        database_only: tuple[str, ...] = (),
        bool_columns: tuple[str, ...] = (),
        decoders: tuple[tuple[str, Callable[[Any], Any]], ...] = (),
    ) -> TableSpec:
        return cls(
            name,
            tuple(field.name for field in fields(record_type)) + database_only,
            record_type,
            frozenset(bool_columns),
            decoders,
        )

    @property
    def record_columns(self) -> tuple[str, ...]:
        if self.record_type is None:
            return ()
        return tuple(field.name for field in fields(self.record_type))

    @property
    def insert_columns(self) -> tuple[str, ...]:
        return tuple(column for column in self.columns if column != "id")

    @property
    def projection(self) -> str:
        return ", ".join(self.columns)

    def encode(self, record: object, columns: tuple[str, ...] | None = None) -> tuple[Any, ...]:
        selected = self.insert_columns if columns is None else columns
        values = []
        for column in selected:
            value = getattr(record, column)
            values.append(int(value) if column in self.bool_columns and value is not None else value)
        return tuple(values)

    def decode(self, row: sqlite3.Row) -> Any:
        if self.record_type is None:
            raise TypeError(f"{self.name} has no record type")
        decoder_map = dict(self.decoders)
        values = {}
        record_columns = {field.name for field in fields(self.record_type)}
        row_columns = set(row.keys())
        for column in self.columns:
            if column not in record_columns or column not in row_columns:
                continue
            value = row[column]
            if column in self.bool_columns and value is not None:
                value = bool(value)
            elif column in decoder_map:
                value = decoder_map[column](value)
            values[column] = value
        return self.record_type(**values)


RUNS = TableSpec.for_record("runs", RunRecord, bool_columns=("is_x3d", "stop_on_error", "variable_load"))
CORE_RESULTS = TableSpec.for_record("core_results", CoreResultRecord, bool_columns=("passed",))
EVENTS = TableSpec.for_record("events", EventRecord)
TUNING_CONTEXTS = TableSpec.for_record("tuning_contexts", TuningContextRecord)
TELEMETRY_SAMPLES = TableSpec.for_record("telemetry_samples", TelemetrySample)


@cache
def _tuner_core_states_spec() -> TableSpec:
    from corecycler.tuner.state import CoreState, TunerPhase

    return TableSpec.for_record(
        "tuner_core_states",
        CoreState,
        database_only=("id", "session_id", "updated_at"),
        bool_columns=("backoff_mode", "in_test"),
        decoders=(("phase", TunerPhase),),
    )


@cache
def _tuner_sessions_spec() -> TableSpec:
    from corecycler.tuner.state import TunerSession

    return TableSpec.for_record(
        "tuner_sessions",
        TunerSession,
        database_only=("contract_version",),
        bool_columns=("validation_dirty",),
    )


TUNER_EVENTS = TableSpec("tuner_events", ("id", "session_id", "timestamp", "boot_id", "severity", "message"))
TUNER_TEST_LOG = TableSpec(
    "tuner_test_log",
    (
        "id",
        "session_id",
        "core_id",
        "offset_tested",
        "phase",
        "passed",
        "error_message",
        "error_type",
        "duration_seconds",
        "run_id",
        "backend",
        "stress_mode",
        "fft_preset",
        "tested_at",
        "peak_stretch_pct",
        "threads",
        "profile",
        "regime",
    ),
)
TUNER_CO_JOURNAL = TableSpec("tuner_co_journal", ("session_id", "core_id", "value", "survived", "updated_at"))
TUNER_REGIME_BANKS = TableSpec(
    "tuner_regime_banks",
    ("context_id", "core_id", "regime", "offset_value", "clean_seconds", "updated_at"),
)
# ---------------------------------------------------------------------------
# HistoryDB
# ---------------------------------------------------------------------------


class HistoryDB:
    """Crash-safe SQLite database for test run history."""

    SCHEMA_VERSION = 21
    TUNER_CONTRACT_VERSION = 16

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self._db_path = Path(db_path)
        if str(self._db_path) != ":memory:":
            self._db_path.parent.mkdir(parents=True, exist_ok=True)

        self.__conn = sqlite3.connect(
            str(self._db_path),
            isolation_level=None,  # autocommit
        )
        self.__conn.row_factory = sqlite3.Row
        self.__conn.execute("PRAGMA journal_mode=WAL")
        self.__conn.execute("PRAGMA synchronous=NORMAL")
        self.__conn.execute("PRAGMA foreign_keys=ON")
        # Sudo and non-sudo runs share ONE database; a second writer must wait
        # for the WAL lock instead of failing with "database is locked".
        self.__conn.execute("PRAGMA busy_timeout=5000")
        # Fail closed on a corrupted file BEFORE migrations touch it.
        check = self.__conn.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            raise RuntimeError(
                f"History database failed integrity check ({check}): {self._db_path}. "
                f"Move the file aside and restart to rebuild."
            )
        self._create_schema()
        if str(self._db_path) != ":memory:":
            # A sudo run must not leave the shared DB (or its WAL sidecars)
            # root-owned, or the next non-sudo run cannot write it.
            fix_sudo_ownership(
                self._db_path.parent.parent,  # .../corecycler (mkdir -p may create it as root)
                self._db_path.parent,
                self._db_path,
                self._db_path.with_name(self._db_path.name + "-wal"),
                self._db_path.with_name(self._db_path.name + "-shm"),
            )

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    @staticmethod
    def _execute_script(conn: sqlite3.Connection, script: str) -> None:
        statement = ""
        for line in script.splitlines():
            statement += line + chr(10)
            if sqlite3.complete_statement(statement):
                conn.execute(statement)
                statement = ""
        if statement.strip():
            raise RuntimeError("Incomplete SQL migration statement")

    def _create_schema(self) -> None:
        marker_exists = self.__conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ).fetchone()
        if marker_exists is None:
            self.__conn.execute("BEGIN IMMEDIATE")
            try:
                self._execute_script(self.__conn, self._DDL_FRESH)
                self.__conn.execute("INSERT INTO schema_version(version) VALUES (?)", (self.SCHEMA_VERSION,))
                self.__conn.execute("COMMIT")
            except Exception:
                self.__conn.execute("ROLLBACK")
                raise
            return

        markers = self.__conn.execute("SELECT version FROM schema_version").fetchall()
        if len(markers) != 1:
            raise RuntimeError(f"History database must contain exactly one schema version marker; found {len(markers)}")
        version = markers[0][0]
        if version > self.SCHEMA_VERSION:
            raise RuntimeError(
                f"History database schema version {version} is newer than this application "
                f"supports {self.SCHEMA_VERSION}"
            )
        for target_version in range(version + 1, self.SCHEMA_VERSION + 1):
            migration = self._MIGRATIONS.get(target_version)
            if migration is None:
                raise RuntimeError(f"Missing migration for version {target_version}")
            self.__conn.execute("BEGIN IMMEDIATE")
            try:
                if callable(migration):
                    migration(self.__conn)
                else:
                    self._execute_script(self.__conn, migration)
                self.__conn.execute("UPDATE schema_version SET version=?", (target_version,))
                self.__conn.execute("COMMIT")
            except Exception:
                self.__conn.execute("ROLLBACK")
                raise

    # Full schema for fresh databases (current version)
    _DDL_FRESH = """\
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tuning_contexts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT    NOT NULL,
    bios_version    TEXT    NOT NULL DEFAULT '',
    cpu_model       TEXT    NOT NULL DEFAULT '',
    physical_cores  INTEGER NOT NULL DEFAULT 0,
    ccds            INTEGER NOT NULL DEFAULT 0,
    co_offsets_json TEXT    NOT NULL DEFAULT '{}',
    context_hash    TEXT    NOT NULL DEFAULT '',
    pbo_scalar      REAL,
    boost_limit_mhz INTEGER,
    notes           TEXT    NOT NULL DEFAULT '',
    ppt_limit_w     REAL,
    tdc_limit_a     REAL,
    edc_limit_a     REAL
);
CREATE INDEX IF NOT EXISTS idx_context_hash ON tuning_contexts(context_hash, bios_version);
CREATE UNIQUE INDEX IF NOT EXISTS idx_context_unique_hash ON tuning_contexts(context_hash, bios_version);

CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT    NOT NULL,
    finished_at     TEXT,
    status          TEXT    NOT NULL DEFAULT 'running',
    cpu_model       TEXT    NOT NULL DEFAULT '',
    physical_cores  INTEGER NOT NULL DEFAULT 0,
    logical_cpus    INTEGER NOT NULL DEFAULT 0,
    ccds            INTEGER NOT NULL DEFAULT 0,
    is_x3d          INTEGER NOT NULL DEFAULT 0,
    backend         TEXT    NOT NULL DEFAULT '',
    stress_mode     TEXT    NOT NULL DEFAULT '',
    fft_preset      TEXT    NOT NULL DEFAULT '',
    seconds_per_core INTEGER NOT NULL DEFAULT 0,
    cycle_count     INTEGER NOT NULL DEFAULT 1,
    stop_on_error   INTEGER NOT NULL DEFAULT 0,
    variable_load   INTEGER NOT NULL DEFAULT 0,
    idle_stability_test REAL NOT NULL DEFAULT 0.0,
    max_temperature REAL    NOT NULL DEFAULT 95.0,
    settings_json   TEXT    NOT NULL DEFAULT '{}',
    context_id      INTEGER REFERENCES tuning_contexts(id),
    bios_version    TEXT    NOT NULL DEFAULT '',
    total_cores     INTEGER NOT NULL DEFAULT 0,
    cores_passed    INTEGER NOT NULL DEFAULT 0,
    cores_failed    INTEGER NOT NULL DEFAULT 0,
    total_seconds   REAL    NOT NULL DEFAULT 0.0
);

CREATE INDEX IF NOT EXISTS idx_runs_started_at ON runs(started_at DESC);

CREATE TABLE IF NOT EXISTS core_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    core_id         INTEGER NOT NULL,
    ccd             INTEGER,
    cycle           INTEGER NOT NULL DEFAULT 0,
    started_at      TEXT    NOT NULL,
    finished_at     TEXT,
    passed          INTEGER,
    error_message   TEXT,
    error_type      TEXT,
    elapsed_seconds REAL    NOT NULL DEFAULT 0.0,
    iterations_completed INTEGER NOT NULL DEFAULT 0,
    peak_freq_mhz   REAL,
    max_temp_c       REAL,
    min_vcore_v      REAL,
    max_vcore_v      REAL
);
CREATE INDEX IF NOT EXISTS idx_core_results_run ON core_results(run_id);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    timestamp       TEXT    NOT NULL,
    event_type      TEXT    NOT NULL,
    core_id         INTEGER,
    message         TEXT    NOT NULL DEFAULT '',
    details_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id);

CREATE TABLE IF NOT EXISTS telemetry_samples (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    core_id         INTEGER NOT NULL,
    timestamp       TEXT    NOT NULL,
    freq_mhz       REAL,
    effective_max_mhz REAL,
    temp_c          REAL,
    vcore_v         REAL
);
CREATE INDEX IF NOT EXISTS idx_telemetry_run_core ON telemetry_samples(run_id, core_id);

CREATE TABLE IF NOT EXISTS tuner_sessions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    status              TEXT    NOT NULL DEFAULT 'running',
    bios_version        TEXT    NOT NULL DEFAULT '',
    cpu_model           TEXT    NOT NULL DEFAULT '',
    config_json         TEXT    NOT NULL DEFAULT '{}',
    context_id          INTEGER REFERENCES tuning_contexts(id),
    resume_crash_streak INTEGER NOT NULL DEFAULT 0,
    notes               TEXT    NOT NULL DEFAULT '',
    unattributed_crashes INTEGER NOT NULL DEFAULT 0,
    contract_version    INTEGER NOT NULL DEFAULT 16,
    validation_stage    INTEGER NOT NULL DEFAULT 0,
    validation_index    INTEGER NOT NULL DEFAULT 0,
    validation_half     INTEGER NOT NULL DEFAULT 0,
    validation_dirty    INTEGER NOT NULL DEFAULT 0,
    validation_requeue  TEXT    NOT NULL DEFAULT '[]',
    endurance_round     INTEGER NOT NULL DEFAULT 0,
    endurance_workload  INTEGER NOT NULL DEFAULT 0,
    endurance_index     INTEGER NOT NULL DEFAULT 0,
    boot_id             TEXT NOT NULL DEFAULT '',
    app_version         TEXT NOT NULL DEFAULT '',
    hunt_state          TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS tuner_core_states (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          INTEGER NOT NULL REFERENCES tuner_sessions(id) ON DELETE CASCADE,
    core_id             INTEGER NOT NULL,
    phase               TEXT    NOT NULL DEFAULT 'not_started',
    current_offset      INTEGER NOT NULL DEFAULT 0,
    best_offset         INTEGER,
    proven_offset       INTEGER,
    coarse_fail_offset  INTEGER,
    confirm_attempts    INTEGER NOT NULL DEFAULT 0,
    baseline_offset     INTEGER NOT NULL DEFAULT 0,
    backoff_mode        INTEGER NOT NULL DEFAULT 0,
    consecutive_backoff_fails INTEGER NOT NULL DEFAULT 0,
    backoff_fail_bound  INTEGER,
    backoff_pass_bound  INTEGER,
    in_test             INTEGER NOT NULL DEFAULT 0,
    crash_count         INTEGER NOT NULL DEFAULT 0,
    crash_cooldown      INTEGER NOT NULL DEFAULT 0,
    thermal_aborts      INTEGER NOT NULL DEFAULT 0,
    cumulative_test_time REAL   NOT NULL DEFAULT 0.0,
    battery_index       INTEGER NOT NULL DEFAULT 0,
    anneal_strikes      INTEGER NOT NULL DEFAULT 0,
    anneal_bar_hours    REAL    NOT NULL DEFAULT 0.0,
    suspicion           REAL    NOT NULL DEFAULT 0.0,
    updated_at          TEXT    NOT NULL,
    UNIQUE(session_id, core_id)
);

CREATE TABLE IF NOT EXISTS tuner_test_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          INTEGER NOT NULL REFERENCES tuner_sessions(id) ON DELETE CASCADE,
    core_id             INTEGER NOT NULL,
    offset_tested       INTEGER NOT NULL,
    phase               TEXT    NOT NULL,
    passed              INTEGER NOT NULL,
    error_message       TEXT,
    error_type          TEXT,
    duration_seconds    REAL,
    run_id              INTEGER REFERENCES runs(id),
    backend             TEXT,
    stress_mode         TEXT,
    fft_preset          TEXT,
    tested_at           TEXT    NOT NULL,
    peak_stretch_pct    REAL,
    threads             INTEGER,
    profile             TEXT,
    regime              TEXT
);
CREATE INDEX IF NOT EXISTS idx_tuner_log_session ON tuner_test_log(session_id, core_id);

CREATE TABLE IF NOT EXISTS tuner_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES tuner_sessions(id) ON DELETE CASCADE,
    timestamp   TEXT    NOT NULL,
    boot_id     TEXT    NOT NULL DEFAULT '',
    severity    TEXT    NOT NULL DEFAULT 'info',
    message     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tuner_events_session ON tuner_events(session_id);

CREATE TABLE IF NOT EXISTS tuner_co_journal (
    session_id  INTEGER NOT NULL REFERENCES tuner_sessions(id) ON DELETE CASCADE,
    core_id     INTEGER NOT NULL,
    value       INTEGER NOT NULL,
    survived    INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT    NOT NULL,
    UNIQUE(session_id, core_id)
);

CREATE TABLE IF NOT EXISTS tuner_regime_banks (
    context_id   INTEGER NOT NULL REFERENCES tuning_contexts(id) ON DELETE CASCADE,
    core_id      INTEGER NOT NULL,
    regime       TEXT    NOT NULL,
    offset_value INTEGER NOT NULL,
    clean_seconds REAL   NOT NULL DEFAULT 0.0,
    updated_at   TEXT    NOT NULL,
    UNIQUE(context_id, core_id, regime, offset_value)
);
CREATE INDEX IF NOT EXISTS idx_regime_bank_core ON tuner_regime_banks(context_id, core_id);
"""

    # Migration from v1 to v2
    _DDL_MIGRATE_V2_TABLES = """\
CREATE TABLE IF NOT EXISTS tuning_contexts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT    NOT NULL,
    bios_version    TEXT    NOT NULL DEFAULT '',
    co_offsets_json TEXT    NOT NULL DEFAULT '{}',
    co_hash         TEXT    NOT NULL DEFAULT '',
    pbo_scalar      REAL,
    boost_limit_mhz INTEGER,
    notes           TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_context_hash ON tuning_contexts(co_hash, bios_version);
CREATE INDEX IF NOT EXISTS idx_core_results_run ON core_results(run_id);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id);
CREATE INDEX IF NOT EXISTS idx_telemetry_run_core ON telemetry_samples(run_id, core_id);
"""

    @staticmethod
    def _migrate_v2(conn: sqlite3.Connection) -> None:
        HistoryDB._execute_script(conn, HistoryDB._DDL_MIGRATE_V2_TABLES)
        HistoryDB._add_columns(
            conn,
            "runs",
            [
                ("context_id", "INTEGER REFERENCES tuning_contexts(id)"),
                ("bios_version", "TEXT NOT NULL DEFAULT ''"),
            ],
        )

    # Migration from v2 to v3 - add tuner tables
    _DDL_MIGRATE_V3 = """\
CREATE TABLE IF NOT EXISTS tuner_sessions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    status              TEXT    NOT NULL DEFAULT 'running',
    bios_version        TEXT    NOT NULL DEFAULT '',
    cpu_model           TEXT    NOT NULL DEFAULT '',
    config_json         TEXT    NOT NULL DEFAULT '{}',
    context_id          INTEGER REFERENCES tuning_contexts(id),
    notes               TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS tuner_core_states (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          INTEGER NOT NULL REFERENCES tuner_sessions(id) ON DELETE CASCADE,
    core_id             INTEGER NOT NULL,
    phase               TEXT    NOT NULL DEFAULT 'not_started',
    current_offset      INTEGER NOT NULL DEFAULT 0,
    best_offset         INTEGER,
    coarse_fail_offset  INTEGER,
    confirm_attempts    INTEGER NOT NULL DEFAULT 0,
    updated_at          TEXT    NOT NULL,
    UNIQUE(session_id, core_id)
);

CREATE TABLE IF NOT EXISTS tuner_test_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          INTEGER NOT NULL REFERENCES tuner_sessions(id) ON DELETE CASCADE,
    core_id             INTEGER NOT NULL,
    offset_tested       INTEGER NOT NULL,
    phase               TEXT    NOT NULL,
    passed              INTEGER NOT NULL,
    error_message       TEXT,
    error_type          TEXT,
    duration_seconds    REAL,
    run_id              INTEGER REFERENCES runs(id),
    tested_at           TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tuner_log_session ON tuner_test_log(session_id, core_id);

-- Performance index for time-based queries on runs
CREATE INDEX IF NOT EXISTS idx_runs_started_at ON runs(started_at DESC);
"""

    # Migration from v3 to v4 - add effective_max_mhz for clock stretch detection
    @staticmethod
    def _migrate_v4(conn: sqlite3.Connection) -> None:
        HistoryDB._add_columns(conn, "telemetry_samples", [("effective_max_mhz", "REAL")])

    # Migration from v4 to v5 - deduplicate tuning contexts, add UNIQUE constraint
    _DDL_MIGRATE_V5 = """\
UPDATE runs
SET context_id = (
    SELECT MIN(survivor.id)
    FROM tuning_contexts survivor
    JOIN tuning_contexts duplicate
      ON survivor.co_hash = duplicate.co_hash AND survivor.bios_version = duplicate.bios_version
    WHERE duplicate.id = runs.context_id
)
WHERE context_id IS NOT NULL;
UPDATE tuner_sessions
SET context_id = (
    SELECT MIN(survivor.id)
    FROM tuning_contexts survivor
    JOIN tuning_contexts duplicate
      ON survivor.co_hash = duplicate.co_hash AND survivor.bios_version = duplicate.bios_version
    WHERE duplicate.id = tuner_sessions.context_id
)
WHERE context_id IS NOT NULL;
DELETE FROM tuning_contexts
WHERE id NOT IN (SELECT MIN(id) FROM tuning_contexts GROUP BY co_hash, bios_version);
CREATE UNIQUE INDEX IF NOT EXISTS idx_context_unique_hash ON tuning_contexts(co_hash, bios_version);
"""

    # Migration from v5 to v6 - add baseline_offset for CO isolation during tuning
    @staticmethod
    def _migrate_v6(conn: sqlite3.Connection) -> None:
        HistoryDB._add_columns(conn, "tuner_core_states", [("baseline_offset", "INTEGER NOT NULL DEFAULT 0")])

    # Migration from v6 to v7 - add backoff algorithm columns
    _DDL_MIGRATE_V7_COLUMNS = [
        ("backoff_mode", "INTEGER NOT NULL DEFAULT 0"),
        ("consecutive_backoff_fails", "INTEGER NOT NULL DEFAULT 0"),
        ("backoff_fail_bound", "INTEGER"),
        ("backoff_pass_bound", "INTEGER"),
    ]

    @staticmethod
    def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
        return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))

    @staticmethod
    def _add_columns(conn: sqlite3.Connection, table: str, columns: list[tuple[str, str]]) -> None:
        for name, coldef in columns:
            if not HistoryDB._column_exists(conn, table, name):
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {coldef}")

    @staticmethod
    def _migrate_v7(conn: sqlite3.Connection) -> None:
        for col_name, col_def in HistoryDB._DDL_MIGRATE_V7_COLUMNS:
            # Skip ONLY the known partial-migration case (column present);
            # any other ALTER failure must raise, not hide a broken schema.
            if not HistoryDB._column_exists(conn, "tuner_core_states", col_name):
                conn.execute(f"ALTER TABLE tuner_core_states ADD COLUMN {col_name} {col_def}")

    @staticmethod
    def _migrate_v8(conn: sqlite3.Connection) -> None:
        if not HistoryDB._column_exists(conn, "tuner_core_states", "in_test"):
            conn.execute("ALTER TABLE tuner_core_states ADD COLUMN in_test INTEGER NOT NULL DEFAULT 0")

    @staticmethod
    def _migrate_v9(conn: sqlite3.Connection) -> None:
        HistoryDB._add_columns(
            conn,
            "tuner_core_states",
            [
                ("crash_count", "INTEGER DEFAULT 0"),
                ("crash_cooldown", "INTEGER DEFAULT 0"),
                ("cumulative_test_time", "REAL DEFAULT 0.0"),
                ("hardening_tier_index", "INTEGER DEFAULT 0"),
            ],
        )
        HistoryDB._add_columns(
            conn,
            "tuner_test_log",
            [("backend", "TEXT"), ("stress_mode", "TEXT"), ("fft_preset", "TEXT")],
        )

    @staticmethod
    def _migrate_v10(conn: sqlite3.Connection) -> None:
        HistoryDB._add_columns(conn, "tuner_core_states", [("thermal_aborts", "INTEGER DEFAULT 0")])

    # v10 -> v11: CO write-ahead journal (crash-attributable SMU writes) +
    # resume-crash circuit-breaker counter on the session. The ADD COLUMN is
    # wrapped (like v7/v8) so a re-run or partial migration cannot fail.
    @staticmethod
    def _migrate_v11(conn: sqlite3.Connection) -> None:
        if not HistoryDB._column_exists(conn, "tuner_sessions", "resume_crash_streak"):
            conn.execute("ALTER TABLE tuner_sessions ADD COLUMN resume_crash_streak INTEGER NOT NULL DEFAULT 0")
        HistoryDB._execute_script(
            conn,
            """\
CREATE TABLE IF NOT EXISTS tuner_co_journal (
    session_id  INTEGER NOT NULL REFERENCES tuner_sessions(id) ON DELETE CASCADE,
    core_id     INTEGER NOT NULL,
    value       INTEGER NOT NULL,
    survived    INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT    NOT NULL,
    UNIQUE(session_id, core_id)
);
""",
        )

    # v11 -> v12: rebuild tuner_core_states into the canonical (fresh-DDL)
    # shape. The v9/v10 ALTERs added crash_count/crash_cooldown/thermal_aborts/
    # cumulative_test_time/hardening_tier_index as NULLABLE, so a migrated
    # database was structurally different from a fresh one (and could hold
    # NULLs the code papers over with `or 0`). One canonical schema everywhere;
    # tests/test_history_db.py::TestFreshEqualsMigrated enforces it stays that way.
    _DDL_MIGRATE_V12 = """\
CREATE TABLE tuner_core_states_v12 (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          INTEGER NOT NULL REFERENCES tuner_sessions(id) ON DELETE CASCADE,
    core_id             INTEGER NOT NULL,
    phase               TEXT    NOT NULL DEFAULT 'not_started',
    current_offset      INTEGER NOT NULL DEFAULT 0,
    best_offset         INTEGER,
    coarse_fail_offset  INTEGER,
    confirm_attempts    INTEGER NOT NULL DEFAULT 0,
    baseline_offset     INTEGER NOT NULL DEFAULT 0,
    backoff_mode        INTEGER NOT NULL DEFAULT 0,
    consecutive_backoff_fails INTEGER NOT NULL DEFAULT 0,
    backoff_fail_bound  INTEGER,
    backoff_pass_bound  INTEGER,
    in_test             INTEGER NOT NULL DEFAULT 0,
    crash_count         INTEGER NOT NULL DEFAULT 0,
    crash_cooldown      INTEGER NOT NULL DEFAULT 0,
    thermal_aborts      INTEGER NOT NULL DEFAULT 0,
    cumulative_test_time REAL   NOT NULL DEFAULT 0.0,
    hardening_tier_index INTEGER NOT NULL DEFAULT 0,
    updated_at          TEXT    NOT NULL,
    UNIQUE(session_id, core_id)
);
INSERT INTO tuner_core_states_v12 (
    id, session_id, core_id, phase, current_offset, best_offset,
    coarse_fail_offset, confirm_attempts, baseline_offset, backoff_mode,
    consecutive_backoff_fails, backoff_fail_bound, backoff_pass_bound,
    in_test, crash_count, crash_cooldown, thermal_aborts,
    cumulative_test_time, hardening_tier_index, updated_at
)
SELECT
    id, session_id, core_id, phase, current_offset, best_offset,
    coarse_fail_offset, confirm_attempts, baseline_offset,
    COALESCE(backoff_mode, 0), COALESCE(consecutive_backoff_fails, 0),
    backoff_fail_bound, backoff_pass_bound, COALESCE(in_test, 0),
    COALESCE(crash_count, 0), COALESCE(crash_cooldown, 0),
    COALESCE(thermal_aborts, 0), COALESCE(cumulative_test_time, 0.0),
    COALESCE(hardening_tier_index, 0), updated_at
FROM tuner_core_states;
DROP TABLE tuner_core_states;
ALTER TABLE tuner_core_states_v12 RENAME TO tuner_core_states;
"""

    # v12 -> v13: power-limit capture on tuning contexts (PPT/TDC/EDC are part
    # of the stability environment), crash-hunt bookkeeping on sessions, and
    # peak clock stretch preserved per test instead of only inside fail text.
    @staticmethod
    def _migrate_v13(conn: sqlite3.Connection) -> None:
        HistoryDB._add_columns(
            conn,
            "tuning_contexts",
            [("ppt_limit_w", "REAL"), ("tdc_limit_a", "REAL"), ("edc_limit_a", "REAL")],
        )
        HistoryDB._add_columns(
            conn,
            "tuner_sessions",
            [
                ("unattributed_crashes", "INTEGER NOT NULL DEFAULT 0"),
                ("hunting_core", "INTEGER"),
            ],
        )
        HistoryDB._add_columns(conn, "tuner_test_log", [("peak_stretch_pct", "REAL")])

    # v13 -> v14: validation progress survives reboots and app restarts, so a
    # back-off or crash never restarts the whole multi-core validation from
    # stage 1.
    @staticmethod
    def _migrate_v14(conn: sqlite3.Connection) -> None:
        HistoryDB._add_columns(
            conn,
            "tuner_sessions",
            [
                ("validation_stage", "INTEGER NOT NULL DEFAULT 0"),
                ("validation_index", "INTEGER NOT NULL DEFAULT 0"),
                ("validation_half", "INTEGER NOT NULL DEFAULT 0"),
                ("validation_dirty", "INTEGER NOT NULL DEFAULT 0"),
                ("validation_requeue", "TEXT NOT NULL DEFAULT '[]'"),
            ],
        )

    # v14 -> v15: the tuner narrative becomes durable - every log line the
    # engine emits lands in tuner_events, so a session's story survives the
    # terminal and can be replayed on resume.
    _DDL_MIGRATE_V15 = """\
CREATE TABLE IF NOT EXISTS tuner_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES tuner_sessions(id) ON DELETE CASCADE,
    timestamp   TEXT    NOT NULL,
    boot_id     TEXT    NOT NULL DEFAULT '',
    severity    TEXT    NOT NULL DEFAULT 'info',
    message     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tuner_events_session ON tuner_events(session_id);
"""

    # v15 -> v16: perpetual endurance validation. The session-level cursor
    # (round, workload, slot) survives a reboot, and every test row records
    # the thread count and load profile it actually ran so the per-core
    # evidence ledger can be read back per workload.
    @staticmethod
    def _migrate_v16(conn: sqlite3.Connection) -> None:
        HistoryDB._add_columns(
            conn,
            "tuner_sessions",
            [
                ("endurance_round", "INTEGER NOT NULL DEFAULT 0"),
                ("endurance_workload", "INTEGER NOT NULL DEFAULT 0"),
                ("endurance_index", "INTEGER NOT NULL DEFAULT 0"),
                ("contract_version", "INTEGER NOT NULL DEFAULT 16"),
            ],
        )
        conn.execute("UPDATE tuner_sessions SET contract_version=0")
        HistoryDB._add_columns(conn, "tuner_test_log", [("threads", "INTEGER"), ("profile", "TEXT")])

    @staticmethod
    def _migrate_v17(conn: sqlite3.Connection) -> None:
        HistoryDB._add_columns(conn, "tuner_sessions", [("boot_id", "TEXT NOT NULL DEFAULT ''")])
        conn.execute(
            "UPDATE tuner_sessions SET boot_id=COALESCE("
            "(SELECT boot_id FROM tuner_events WHERE session_id=tuner_sessions.id ORDER BY id DESC LIMIT 1), '') "
            "WHERE boot_id=''"
        )

    @staticmethod
    def _migrate_v18(conn: sqlite3.Connection) -> None:
        HistoryDB._add_columns(conn, "tuner_sessions", [("app_version", "TEXT NOT NULL DEFAULT ''")])

    # v18 -> v19: the search contract changed. Hardening tiers are gone (the
    # per-slot regime battery replaced them), a slot now carries a battery
    # cursor, and confidence is banked per (context, core, regime) so it
    # survives sessions instead of dying with one. Rebuild rather than ALTER:
    # hardening_tier_index has to disappear, and a fresh database must be
    # byte-identical to a migrated one.
    @staticmethod
    def _migrate_v19(conn: sqlite3.Connection) -> None:
        HistoryDB._add_columns(conn, "tuner_sessions", [("hunt_state", "TEXT NOT NULL DEFAULT ''")])
        HistoryDB._add_columns(conn, "tuner_test_log", [("regime", "TEXT")])
        HistoryDB._execute_script(conn, HistoryDB._DDL_MIGRATE_V19)

    _DDL_MIGRATE_V19 = """\
CREATE TABLE tuner_core_states_v19 (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          INTEGER NOT NULL REFERENCES tuner_sessions(id) ON DELETE CASCADE,
    core_id             INTEGER NOT NULL,
    phase               TEXT    NOT NULL DEFAULT 'not_started',
    current_offset      INTEGER NOT NULL DEFAULT 0,
    best_offset         INTEGER,
    coarse_fail_offset  INTEGER,
    confirm_attempts    INTEGER NOT NULL DEFAULT 0,
    baseline_offset     INTEGER NOT NULL DEFAULT 0,
    backoff_mode        INTEGER NOT NULL DEFAULT 0,
    consecutive_backoff_fails INTEGER NOT NULL DEFAULT 0,
    backoff_fail_bound  INTEGER,
    backoff_pass_bound  INTEGER,
    in_test             INTEGER NOT NULL DEFAULT 0,
    crash_count         INTEGER NOT NULL DEFAULT 0,
    crash_cooldown      INTEGER NOT NULL DEFAULT 0,
    thermal_aborts      INTEGER NOT NULL DEFAULT 0,
    cumulative_test_time REAL   NOT NULL DEFAULT 0.0,
    battery_index       INTEGER NOT NULL DEFAULT 0,
    anneal_strikes      INTEGER NOT NULL DEFAULT 0,
    anneal_bar_hours    REAL    NOT NULL DEFAULT 0.0,
    suspicion           REAL    NOT NULL DEFAULT 0.0,
    updated_at          TEXT    NOT NULL,
    UNIQUE(session_id, core_id)
);
INSERT INTO tuner_core_states_v19 (
    id, session_id, core_id, phase, current_offset, best_offset,
    coarse_fail_offset, confirm_attempts, baseline_offset, backoff_mode,
    consecutive_backoff_fails, backoff_fail_bound, backoff_pass_bound,
    in_test, crash_count, crash_cooldown, thermal_aborts,
    cumulative_test_time, updated_at
)
SELECT
    id, session_id, core_id,
    CASE phase WHEN 'hardening_t1' THEN 'confirmed'
               WHEN 'hardening_t2' THEN 'confirmed'
               WHEN 'hardened' THEN 'confirmed'
               ELSE phase END,
    current_offset, best_offset,
    coarse_fail_offset, confirm_attempts, baseline_offset, backoff_mode,
    consecutive_backoff_fails, backoff_fail_bound, backoff_pass_bound,
    in_test, crash_count, crash_cooldown, thermal_aborts,
    cumulative_test_time, updated_at
FROM tuner_core_states;
DROP TABLE tuner_core_states;
ALTER TABLE tuner_core_states_v19 RENAME TO tuner_core_states;
CREATE TABLE IF NOT EXISTS tuner_regime_banks (
    context_hash TEXT    NOT NULL,
    core_id      INTEGER NOT NULL,
    regime       TEXT    NOT NULL,
    offset_value INTEGER NOT NULL,
    clean_seconds REAL   NOT NULL DEFAULT 0.0,
    updated_at   TEXT    NOT NULL,
    UNIQUE(context_hash, core_id, regime, offset_value)
);
CREATE INDEX IF NOT EXISTS idx_regime_bank_core ON tuner_regime_banks(context_hash, core_id);
"""

    @staticmethod
    def _migrate_v20(conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(tuner_regime_banks)").fetchall()}
        current_columns = {
            "context_id",
            "core_id",
            "regime",
            "offset_value",
            "clean_seconds",
            "updated_at",
        }
        if columns == current_columns:
            foreign_keys = conn.execute("PRAGMA foreign_key_list(tuner_regime_banks)").fetchall()
            context_fk = any(
                row["from"] == "context_id"
                and row["table"] == "tuning_contexts"
                and row["to"] == "id"
                and row["on_delete"] == "CASCADE"
                for row in foreign_keys
            )
            unique_key = False
            for index in conn.execute("PRAGMA index_list(tuner_regime_banks)").fetchall():
                if not index["unique"]:
                    continue
                indexed = conn.execute(
                    "SELECT name FROM pragma_index_info(?) ORDER BY seqno", (index["name"],)
                ).fetchall()
                if [row["name"] for row in indexed] == [
                    "context_id",
                    "core_id",
                    "regime",
                    "offset_value",
                ]:
                    unique_key = True
                    break
            if not context_fk or not unique_key:
                raise RuntimeError("Invalid tuner_regime_banks schema: unsafe context identity")
            return
        if "context_hash" not in columns or "context_id" in columns:
            raise RuntimeError("Invalid tuner_regime_banks schema: unsafe context key")
        HistoryDB._execute_script(conn, HistoryDB._DDL_MIGRATE_V20)

    _DDL_MIGRATE_V20 = """\
CREATE TABLE tuner_regime_banks_v20 (
    context_id   INTEGER NOT NULL REFERENCES tuning_contexts(id) ON DELETE CASCADE,
    core_id      INTEGER NOT NULL,
    regime       TEXT    NOT NULL,
    offset_value INTEGER NOT NULL,
    clean_seconds REAL   NOT NULL DEFAULT 0.0,
    updated_at   TEXT    NOT NULL,
    UNIQUE(context_id, core_id, regime, offset_value)
);
INSERT INTO tuner_regime_banks_v20 (
    context_id, core_id, regime, offset_value, clean_seconds, updated_at
)
SELECT c.id, b.core_id, b.regime, b.offset_value, b.clean_seconds, b.updated_at
FROM tuner_regime_banks b
JOIN tuning_contexts c ON c.co_hash = b.context_hash
JOIN (
    SELECT co_hash
    FROM tuning_contexts
    GROUP BY co_hash
    HAVING COUNT(*) = 1
) unambiguous ON unambiguous.co_hash = b.context_hash;
DROP TABLE tuner_regime_banks;
ALTER TABLE tuner_regime_banks_v20 RENAME TO tuner_regime_banks;
CREATE INDEX idx_regime_bank_core ON tuner_regime_banks(context_id, core_id);
"""

    @staticmethod
    def _migrate_v21(conn: sqlite3.Connection) -> None:
        HistoryDB._add_columns(
            conn,
            "tuning_contexts",
            [
                ("cpu_model", "TEXT NOT NULL DEFAULT ''"),
                ("physical_cores", "INTEGER NOT NULL DEFAULT 0"),
                ("ccds", "INTEGER NOT NULL DEFAULT 0"),
            ],
        )
        if HistoryDB._column_exists(conn, "tuning_contexts", "co_hash"):
            conn.execute("ALTER TABLE tuning_contexts RENAME COLUMN co_hash TO context_hash")
        if not HistoryDB._column_exists(conn, "tuner_sessions", "contract_version"):
            conn.execute("ALTER TABLE tuner_sessions ADD COLUMN contract_version INTEGER NOT NULL DEFAULT 16")
            conn.execute("UPDATE tuner_sessions SET contract_version=0")
        if HistoryDB._column_exists(conn, "tuner_sessions", "hunting_core"):
            conn.execute("ALTER TABLE tuner_sessions DROP COLUMN hunting_core")
        HistoryDB._add_columns(conn, "tuner_core_states", [("proven_offset", "INTEGER")])

    _MIGRATIONS: dict[int, str | callable] = {
        2: _migrate_v2,
        3: _DDL_MIGRATE_V3,
        4: _migrate_v4,
        5: _DDL_MIGRATE_V5,
        6: _migrate_v6,
        7: _migrate_v7,
        8: _migrate_v8,
        9: _migrate_v9,
        10: _migrate_v10,
        11: _migrate_v11,
        12: _DDL_MIGRATE_V12,
        13: _migrate_v13,
        14: _migrate_v14,
        15: _DDL_MIGRATE_V15,
        16: _migrate_v16,
        17: _migrate_v17,
        18: _migrate_v18,
        19: _migrate_v19,
        20: _migrate_v20,
        21: _migrate_v21,
    }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(UTC).isoformat()

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------

    def create_run(self, run: RunRecord) -> int:
        """Insert a new run record. Returns the run id."""
        if not run.started_at:
            run.started_at = self._now_iso()
        columns = RUNS.insert_columns
        placeholders = ",".join("?" * len(columns))
        cur = self.__conn.execute(
            f"INSERT INTO {RUNS.name} ({', '.join(columns)}) VALUES ({placeholders})",
            RUNS.encode(run),
        )
        run.id = cur.lastrowid
        return cur.lastrowid

    def finish_run(
        self,
        run_id: int,
        *,
        status: str = "completed",
        total_cores: int = 0,
        cores_passed: int = 0,
        cores_failed: int = 0,
        total_seconds: float = 0.0,
    ) -> bool:
        cursor = self.__conn.execute(
            """\
            UPDATE runs SET finished_at=?, status=?,
                total_cores=?, cores_passed=?, cores_failed=?, total_seconds=?
            WHERE id=? AND status='running'
            """,
            (
                self._now_iso(),
                status,
                total_cores,
                cores_passed,
                cores_failed,
                total_seconds,
                run_id,
            ),
        )
        return cursor.rowcount == 1

    def get_run(self, run_id: int) -> RunRecord | None:
        row = self.__conn.execute(f"SELECT {RUNS.projection} FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_run(row)

    def list_runs(self, *, limit: int = 100, offset: int = 0) -> list[RunRecord]:
        rows = self.__conn.execute(
            f"SELECT {RUNS.projection} FROM runs ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [self._row_to_run(r) for r in rows]

    def delete_run(self, run_id: int) -> None:
        self.__conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.__conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is not None and row["status"] == "running":
                raise InFlightRecord(f"Cannot delete run {run_id} with status running")
            self.__conn.execute("DELETE FROM runs WHERE id=?", (run_id,))
            self.__conn.execute("COMMIT")
        except Exception:
            self.__conn.execute("ROLLBACK")
            raise

    def list_runs_for_context(self, context_id: int) -> list[RunRecord]:
        """Return all runs belonging to a specific tuning context."""
        rows = self.__conn.execute(
            f"SELECT {RUNS.projection} FROM runs WHERE context_id=? ORDER BY id DESC",
            (context_id,),
        ).fetchall()
        return [self._row_to_run(r) for r in rows]

    def count_runs(self) -> int:
        return self.__conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]

    def count_runs_for_context(self, context_id: int) -> int:
        return self.__conn.execute("SELECT COUNT(*) FROM runs WHERE context_id=?", (context_id,)).fetchone()[0]

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> RunRecord:
        return RUNS.decode(row)

    # ------------------------------------------------------------------
    # Core results
    # ------------------------------------------------------------------

    def insert_core_result(self, rec: CoreResultRecord) -> int:
        if not rec.started_at:
            rec.started_at = self._now_iso()
        columns = CORE_RESULTS.insert_columns
        placeholders = ",".join("?" * len(columns))
        cur = self.__conn.execute(
            f"INSERT INTO {CORE_RESULTS.name} ({', '.join(columns)}) VALUES ({placeholders})",
            CORE_RESULTS.encode(rec),
        )
        rec.id = cur.lastrowid
        return cur.lastrowid

    def update_core_result(
        self,
        result_id: int,
        *,
        finished_at: str | None = None,
        passed: bool | None = None,
        error_message: str | None = None,
        error_type: str | None = None,
        elapsed_seconds: float | None = None,
        iterations_completed: int | None = None,
        peak_freq_mhz: float | None = None,
        max_temp_c: float | None = None,
        min_vcore_v: float | None = None,
        max_vcore_v: float | None = None,
    ) -> None:
        sets: list[str] = []
        vals: list = []
        if finished_at is not None:
            sets.append("finished_at=?")
            vals.append(finished_at)
        if passed is not None:
            sets.append("passed=?")
            vals.append(int(passed))
        if error_message is not None:
            sets.append("error_message=?")
            vals.append(error_message)
        if error_type is not None:
            sets.append("error_type=?")
            vals.append(error_type)
        if elapsed_seconds is not None:
            sets.append("elapsed_seconds=?")
            vals.append(elapsed_seconds)
        if iterations_completed is not None:
            sets.append("iterations_completed=?")
            vals.append(iterations_completed)
        if peak_freq_mhz is not None:
            sets.append("peak_freq_mhz=?")
            vals.append(peak_freq_mhz)
        if max_temp_c is not None:
            sets.append("max_temp_c=?")
            vals.append(max_temp_c)
        if min_vcore_v is not None:
            sets.append("min_vcore_v=?")
            vals.append(min_vcore_v)
        if max_vcore_v is not None:
            sets.append("max_vcore_v=?")
            vals.append(max_vcore_v)
        if not sets:
            return
        vals.append(result_id)
        self.__conn.execute(
            f"UPDATE core_results SET {', '.join(sets)} WHERE id=?",
            vals,
        )

    def get_core_results(self, run_id: int) -> list[CoreResultRecord]:
        rows = self.__conn.execute(
            f"SELECT {CORE_RESULTS.projection} FROM core_results WHERE run_id=? ORDER BY cycle, core_id",
            (run_id,),
        ).fetchall()
        return [self._row_to_core_result(r) for r in rows]

    @staticmethod
    def _row_to_core_result(row: sqlite3.Row) -> CoreResultRecord:
        return CORE_RESULTS.decode(row)

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def insert_event(self, event: EventRecord) -> int:
        if not event.timestamp:
            event.timestamp = self._now_iso()
        columns = EVENTS.insert_columns
        placeholders = ",".join("?" * len(columns))
        cur = self.__conn.execute(
            f"INSERT INTO {EVENTS.name} ({', '.join(columns)}) VALUES ({placeholders})",
            EVENTS.encode(event),
        )
        event.id = cur.lastrowid
        return cur.lastrowid

    def get_events(self, run_id: int, *, event_type: str | None = None) -> list[EventRecord]:
        if event_type:
            rows = self.__conn.execute(
                f"SELECT {EVENTS.projection} FROM events WHERE run_id=? AND event_type=? ORDER BY id",
                (run_id, event_type),
            ).fetchall()
        else:
            rows = self.__conn.execute(
                f"SELECT {EVENTS.projection} FROM events WHERE run_id=? ORDER BY id",
                (run_id,),
            ).fetchall()
        return [self._row_to_event(r) for r in rows]

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> EventRecord:
        return EVENTS.decode(row)

    # ------------------------------------------------------------------
    # Telemetry
    # ------------------------------------------------------------------

    def insert_telemetry_batch(self, samples: list[TelemetrySample]) -> None:
        if not samples:
            return
        columns = TELEMETRY_SAMPLES.insert_columns
        placeholders = ",".join("?" * len(columns))
        for sample in samples:
            if not sample.timestamp:
                sample.timestamp = self._now_iso()
        self.__conn.executemany(
            f"INSERT INTO {TELEMETRY_SAMPLES.name} ({', '.join(columns)}) VALUES ({placeholders})",
            [TELEMETRY_SAMPLES.encode(sample) for sample in samples],
        )

    def get_telemetry(self, run_id: int, *, core_id: int | None = None) -> list[TelemetrySample]:
        if core_id is not None:
            rows = self.__conn.execute(
                f"SELECT {TELEMETRY_SAMPLES.projection} FROM telemetry_samples "
                "WHERE run_id=? AND core_id=? ORDER BY id",
                (run_id, core_id),
            ).fetchall()
        else:
            rows = self.__conn.execute(
                f"SELECT {TELEMETRY_SAMPLES.projection} FROM telemetry_samples WHERE run_id=? ORDER BY id",
                (run_id,),
            ).fetchall()
        return [TELEMETRY_SAMPLES.decode(row) for row in rows]

    # ------------------------------------------------------------------
    # Tuning contexts
    # ------------------------------------------------------------------

    def get_or_create_context(self, ctx: TuningContextRecord | SystemContext) -> int:
        """Return the matching context id, inserting the context when absent."""
        if not isinstance(ctx, TuningContextRecord):
            ctx = ctx.to_record()
        if not ctx.created_at:
            ctx.created_at = self._now_iso()
        columns = TUNING_CONTEXTS.insert_columns
        placeholders = ",".join("?" * len(columns))
        cur = self.__conn.execute(
            f"INSERT OR IGNORE INTO {TUNING_CONTEXTS.name} ({', '.join(columns)}) VALUES ({placeholders})",
            TUNING_CONTEXTS.encode(ctx),
        )
        if cur.lastrowid and cur.rowcount > 0:
            ctx.id = cur.lastrowid
            return ctx.id
        existing = self.get_context_by_hash(ctx.context_hash, ctx.bios_version)
        if existing:
            ctx.id = existing.id
            return existing.id
        raise RuntimeError(
            "tuning_contexts insert was ignored but no row matches "
            f"(context_hash={ctx.context_hash!r}, bios={ctx.bios_version!r}) - database inconsistent"
        )

    def get_context(self, context_id: int) -> TuningContextRecord | None:
        row = self.__conn.execute(
            f"SELECT {TUNING_CONTEXTS.projection} FROM tuning_contexts WHERE id=?", (context_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_context(row)

    def get_context_by_hash(self, context_hash: str, bios_version: str) -> TuningContextRecord | None:
        row = self.__conn.execute(
            f"SELECT {TUNING_CONTEXTS.projection} FROM tuning_contexts WHERE context_hash=? AND bios_version=? LIMIT 1",
            (context_hash, bios_version),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_context(row)

    def list_contexts(self, *, limit: int = 100, offset: int = 0) -> list[TuningContextRecord]:
        rows = self.__conn.execute(
            f"SELECT {TUNING_CONTEXTS.projection} FROM tuning_contexts ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [self._row_to_context(r) for r in rows]

    def update_context_notes(self, context_id: int, notes: str) -> None:
        self.__conn.execute("UPDATE tuning_contexts SET notes=? WHERE id=?", (notes, context_id))

    @staticmethod
    def _row_to_context(row: sqlite3.Row) -> TuningContextRecord:
        return TUNING_CONTEXTS.decode(row)

    # ------------------------------------------------------------------
    # Tuner sessions
    # ------------------------------------------------------------------

    def create_tuner_session(
        self,
        config_json: str,
        bios_version: str,
        cpu_model: str,
        context_id: int | None = None,
    ) -> int:
        """Create a new tuner session. Returns the session id."""
        now = self._now_iso()
        cur = self.__conn.execute(
            """\
            INSERT INTO tuner_sessions
                (created_at, updated_at, status, bios_version, cpu_model,
                 config_json, context_id, notes, app_version, contract_version)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                now,
                now,
                "running",
                bios_version,
                cpu_model,
                config_json,
                context_id,
                "",
                __version__,
                self.TUNER_CONTRACT_VERSION,
            ),
        )
        return cur.lastrowid

    def update_tuner_session_status(self, session_id: int, status: str) -> None:
        self.__conn.execute(
            "UPDATE tuner_sessions SET status=?, updated_at=? WHERE id=?",
            (status, self._now_iso(), session_id),
        )

    def get_tuner_session(self, session_id: int, *, resumable: bool = False) -> TunerSession | None:
        row = self.__conn.execute(
            f"SELECT {_tuner_sessions_spec().projection} FROM tuner_sessions WHERE id=?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        if resumable and row["contract_version"] < self.TUNER_CONTRACT_VERSION:
            raise LegacySession(
                f"Session {session_id} predates tuner contract v16; "
                f"start a new search with corecycler tune --seed-from {session_id}"
            )
        return self._row_to_tuner_session(row)

    def get_latest_tuner_session(self) -> TunerSession | None:
        row = self.__conn.execute(
            f"SELECT {_tuner_sessions_spec().projection} FROM tuner_sessions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return self._row_to_tuner_session(row)

    def get_active_tuner_session(self) -> TunerSession | None:
        row = self.__conn.execute(
            f"SELECT {_tuner_sessions_spec().projection} FROM tuner_sessions "
            "WHERE status IN ('running','paused','validating','hunting') AND contract_version>=? "
            "ORDER BY id DESC LIMIT 1",
            (self.TUNER_CONTRACT_VERSION,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_tuner_session(row)

    def list_resumable_tuner_sessions(self, *, limit: int = 50) -> list[TunerSession]:
        return self._sessions_with_status(RESUMABLE_STATUSES, limit)

    def list_recoverable_tuner_sessions(self, *, limit: int = 50) -> list[TunerSession]:
        return self._sessions_with_status(RECOVERABLE_STATUSES, limit)

    def _sessions_with_status(self, statuses: tuple[str, ...], limit: int) -> list[TunerSession]:
        placeholders = ",".join("?" * len(statuses))
        rows = self.__conn.execute(
            f"SELECT {_tuner_sessions_spec().projection} FROM tuner_sessions WHERE status IN ({placeholders}) "
            "AND contract_version>=? ORDER BY id DESC LIMIT ?",
            (*statuses, self.TUNER_CONTRACT_VERSION, limit),
        ).fetchall()
        return [self._row_to_tuner_session(r) for r in rows]

    def list_tuner_sessions(self, *, limit: int = 100, offset: int = 0) -> list[TunerSession]:
        rows = self.__conn.execute(
            f"SELECT {_tuner_sessions_spec().projection} FROM tuner_sessions ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [self._row_to_tuner_session(r) for r in rows]

    # ------------------------------------------------------------------
    # CO write-ahead journal + resume-crash circuit breaker
    # ------------------------------------------------------------------

    def journal_co_intent(self, session_id: int, core_id: int, value: int, survived: bool) -> None:
        """Record the CO value about to be made resident in the SMU, durably.

        Written BEFORE the hardware write so any hard crash (idle, baseline
        restore, post-test revert, validation, or search) is attributable to the
        exact (core, value) that was live at crash time. ``survived`` is True only
        when ``value`` is within the core's already-proven-safe envelope (0 is
        always safe); a value in new, more-aggressive territory is journaled
        un-survived until a test completes with it resident. A WAL checkpoint
        forces the record to durable storage before the caller touches hardware.
        """
        now = self._now_iso()
        self.__conn.execute(
            """\
            INSERT INTO tuner_co_journal (session_id, core_id, value, survived, updated_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(session_id, core_id) DO UPDATE SET
                value=excluded.value,
                survived=excluded.survived,
                updated_at=excluded.updated_at
            """,
            (session_id, core_id, value, int(survived), now),
        )
        # Force the intent to disk before the caller writes the value to hardware.
        self.__conn.execute("PRAGMA wal_checkpoint(FULL)")

    def checkpoint(self) -> None:
        """Flush the WAL to the main database file.

        For writes that must survive a hard crash moments later (the in-test
        marks before a validation worker starts): a committed WAL frame alone
        can still be lost to a freeze if the kernel never flushed it.
        """
        self.__conn.execute("PRAGMA wal_checkpoint(FULL)")

    def journal_mark_survived(self, session_id: int, exclude_cores: tuple[int, ...] | list[int] = ()) -> None:
        """Mark every resident CO value for the session as survived.

        Called after a test completes without a hard crash: the machine
        demonstrably ran with the whole resident offset vector and lived.
        ``exclude_cores`` keeps cores with fresh contrary evidence (a corrected
        MCE named them during this very test) un-survived - surviving the test
        does not clear an error the hardware just reported.
        """
        if exclude_cores:
            marks = ",".join("?" * len(exclude_cores))
            self.__conn.execute(
                f"UPDATE tuner_co_journal SET survived=1, updated_at=? WHERE session_id=? AND core_id NOT IN ({marks})",
                (self._now_iso(), session_id, *exclude_cores),
            )
            return
        self.__conn.execute(
            "UPDATE tuner_co_journal SET survived=1, updated_at=? WHERE session_id=?",
            (self._now_iso(), session_id),
        )

    def journal_suspects(self, session_id: int) -> list[tuple[int, int]]:
        """Return ``[(core_id, value)]`` for non-zero offsets that were resident
        but never proven survivable - i.e. live when the machine died."""
        rows = self.__conn.execute(
            "SELECT core_id, value FROM tuner_co_journal "
            "WHERE session_id=? AND survived=0 AND value<>0 ORDER BY core_id",
            (session_id,),
        ).fetchall()
        return [(r["core_id"], r["value"]) for r in rows]

    def journal_survived_values(self, session_id: int) -> dict[int, int]:
        """Return ``{core_id: value}`` for offsets proven survivable this session."""
        rows = self.__conn.execute(
            "SELECT core_id, value FROM tuner_co_journal WHERE session_id=? AND survived=1",
            (session_id,),
        ).fetchall()
        return {r["core_id"]: r["value"] for r in rows}

    def journal_values(self, session_id: int) -> dict[int, int]:
        """Return {core_id: value} - the last CO value the tuner wrote per
        core, survived or not. This is what the SMU is EXPECTED to hold; drift
        detection compares live hardware against it (not against baselines,
        which validation deliberately leaves behind)."""
        rows = self.__conn.execute(
            "SELECT core_id, value FROM tuner_co_journal WHERE session_id=?",
            (session_id,),
        ).fetchall()
        return {r["core_id"]: r["value"] for r in rows}

    def latest_session_activity(self, session_id: int) -> str | None:
        """Latest execution checkpoint, excluding configuration and status edits."""
        row = self.__conn.execute(
            """\
            SELECT MAX(ts) FROM (
                SELECT created_at AS ts FROM tuner_sessions WHERE id=?
                UNION ALL SELECT updated_at FROM tuner_core_states WHERE session_id=?
                UNION ALL SELECT updated_at FROM tuner_co_journal WHERE session_id=?
                UNION ALL SELECT tested_at FROM tuner_test_log WHERE session_id=?
            )
            """,
            (session_id, session_id, session_id, session_id),
        ).fetchone()
        return row[0] if row else None

    def get_unattributed_crashes(self, session_id: int) -> int:
        row = self.__conn.execute(
            "SELECT unattributed_crashes FROM tuner_sessions WHERE id=?", (session_id,)
        ).fetchone()
        if row is None:
            return 0
        return row["unattributed_crashes"] or 0

    def set_unattributed_crashes(self, session_id: int, value: int) -> None:
        self.__conn.execute(
            "UPDATE tuner_sessions SET unattributed_crashes=?, updated_at=? WHERE id=?",
            (value, self._now_iso(), session_id),
        )

    def set_validation_position(
        self,
        session_id: int,
        stage: int,
        index: int,
        half: int,
        dirty: bool,
        requeue_json: str,
    ) -> None:
        """Persist the multi-core validation cursor after every transition,
        so a reboot or app restart continues exactly where validation was
        instead of restarting stage 1 for every core."""
        self.__conn.execute(
            "UPDATE tuner_sessions SET validation_stage=?, validation_index=?, "
            "validation_half=?, validation_dirty=?, validation_requeue=?, "
            "updated_at=? WHERE id=?",
            (stage, index, half, int(dirty), requeue_json, self._now_iso(), session_id),
        )

    def set_endurance_position(self, session_id: int, round_: int, workload: int, index: int) -> None:
        """Persist the endurance cursor before every slot, so a reboot mid-slot
        resumes the same round and workload instead of restarting endurance."""
        self.__conn.execute(
            "UPDATE tuner_sessions SET endurance_round=?, endurance_workload=?, "
            "endurance_index=?, updated_at=? WHERE id=?",
            (round_, workload, index, self._now_iso(), session_id),
        )

    def set_session_boot(self, session_id: int, boot_id: str) -> None:
        self.__conn.execute("UPDATE tuner_sessions SET boot_id=? WHERE id=?", (boot_id, session_id))
        self.checkpoint()

    def update_tuner_session_config(self, session_id: int, config_json: str) -> None:
        self.__conn.execute(
            "UPDATE tuner_sessions SET config_json=?, updated_at=? WHERE id=?",
            (config_json, self._now_iso(), session_id),
        )

    def insert_tuner_event(self, session_id: int, message: str, boot_id: str = "", severity: str = "info") -> None:
        self.__conn.execute(
            "INSERT INTO tuner_events (session_id, timestamp, boot_id, severity, message) VALUES (?,?,?,?,?)",
            (session_id, self._now_iso(), boot_id, severity, message),
        )

    def get_tuner_events(self, session_id: int, limit: int = 200) -> list[dict]:
        """Newest-last narrative lines for a session (the replayable story)."""
        rows = self.__conn.execute(
            "SELECT * FROM (SELECT * FROM tuner_events WHERE session_id=? ORDER BY id DESC LIMIT ?) ORDER BY id",
            (session_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def set_hunt_state(self, session_id: int, blob: str) -> None:
        """Persist bisection progress before the probe that may end the process."""
        self.__conn.execute("UPDATE tuner_sessions SET hunt_state=? WHERE id=?", (blob, session_id))

    def get_resume_crash_streak(self, session_id: int) -> int:
        row = self.__conn.execute("SELECT resume_crash_streak FROM tuner_sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            return 0
        return row["resume_crash_streak"] or 0

    def set_resume_crash_streak(self, session_id: int, value: int) -> None:
        self.__conn.execute(
            "UPDATE tuner_sessions SET resume_crash_streak=?, updated_at=? WHERE id=?",
            (value, self._now_iso(), session_id),
        )

    # Hard silicon bounds for any Curve Optimizer offset (Zen generations use
    # at most [-60, +30]; anything outside is corruption, not a tuning value).
    _CO_SANE_RANGE = (-100, 100)

    @classmethod
    def _check_core_state_sane(cls, cs: CoreState) -> None:
        """Guard condition on the persistence boundary, both directions.

        Insane values (bit corruption, a hand-edited row, an arithmetic bug
        upstream) must RAISE at the boundary - once written they become
        indistinguishable from truth and every later decision trusts them.
        """
        lo, hi = cls._CO_SANE_RANGE
        for name in (
            "current_offset",
            "best_offset",
            "coarse_fail_offset",
            "baseline_offset",
            "backoff_fail_bound",
            "backoff_pass_bound",
        ):
            v = getattr(cs, name)
            if v is not None and not lo <= v <= hi:
                raise ValueError(
                    f"core {cs.core_id}: {name}={v} outside sane CO range "
                    f"[{lo}, {hi}] - refusing to persist/load corrupted state"
                )
        for name in (
            "confirm_attempts",
            "consecutive_backoff_fails",
            "crash_count",
            "crash_cooldown",
            "thermal_aborts",
            "battery_index",
            "anneal_strikes",
        ):
            v = getattr(cs, name)
            if v < 0:
                raise ValueError(f"core {cs.core_id}: {name}={v} negative - refusing to persist/load corrupted state")
        if cs.cumulative_test_time < 0:
            raise ValueError(
                f"core {cs.core_id}: cumulative_test_time={cs.cumulative_test_time} "
                f"negative - refusing to persist/load corrupted state"
            )

    def upsert_tuner_core_state(self, session_id: int, cs: CoreState) -> None:
        self._check_core_state_sane(cs)
        now = self._now_iso()
        columns = ("session_id", *_tuner_core_states_spec().record_columns, "updated_at")
        placeholders = ",".join("?" * len(columns))
        updates = ",".join(
            f"{column}=excluded.{column}" for column in columns if column not in {"session_id", "core_id"}
        )
        self.__conn.execute(
            f"INSERT INTO tuner_core_states ({', '.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT(session_id, core_id) DO UPDATE SET {updates}",
            (session_id, *_tuner_core_states_spec().encode(cs, _tuner_core_states_spec().record_columns), now),
        )

    def get_tuner_core_states(self, session_id: int) -> dict[int, CoreState]:
        rows = self.__conn.execute(
            f"SELECT {_tuner_core_states_spec().projection} FROM tuner_core_states WHERE session_id=? ORDER BY core_id",
            (session_id,),
        ).fetchall()
        result = {}
        for row in rows:
            loaded = _tuner_core_states_spec().decode(row)
            self._check_core_state_sane(loaded)
            result[loaded.core_id] = loaded
        return result

    # ------------------------------------------------------------------
    # Per-regime confidence banks
    # ------------------------------------------------------------------

    def bank_regime_time(
        self,
        context_id: int,
        core_id: int,
        regime: str,
        offset_value: int,
        seconds: float,
    ) -> None:
        """Credit clean time to one (context, core, regime, offset) bucket.

        The complete context identity keeps evidence isolated by BIOS while it
        accumulates across reboots and sessions at one operating point.
        """
        self.__conn.execute(
            """\
            INSERT INTO tuner_regime_banks
                (context_id, core_id, regime, offset_value, clean_seconds, updated_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(context_id, core_id, regime, offset_value) DO UPDATE SET
                clean_seconds = clean_seconds + excluded.clean_seconds,
                updated_at = excluded.updated_at
            """,
            (context_id, core_id, regime, offset_value, float(seconds), self._now_iso()),
        )

    def get_regime_banks(self, context_id: int, core_id: int, offset_value: int) -> dict[str, float]:
        rows = self.__conn.execute(
            "SELECT regime, clean_seconds FROM tuner_regime_banks WHERE context_id=? AND core_id=? AND offset_value=?",
            (context_id, core_id, offset_value),
        ).fetchall()
        return {r["regime"]: r["clean_seconds"] for r in rows}

    def clear_regime_banks(self, context_id: int, core_id: int) -> None:
        """Drop banked confidence for one core at one operating point."""
        self.__conn.execute(
            "DELETE FROM tuner_regime_banks WHERE context_id=? AND core_id=?",
            (context_id, core_id),
        )

    def regime_bank_summary(self, context_id: int) -> list[dict[str, object]]:
        rows = self.__conn.execute(
            "SELECT core_id, regime, offset_value, clean_seconds FROM tuner_regime_banks "
            "WHERE context_id=? ORDER BY core_id, regime",
            (context_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def regime_yield(self, context_id: int) -> dict[str, tuple[int, float]]:
        """Return (failures, seconds) per regime for one complete context."""
        rows = self.__conn.execute(
            """\
            SELECT l.regime AS regime,
                   SUM(CASE WHEN l.passed = 0 THEN 1 ELSE 0 END) AS failures,
                   COALESCE(SUM(l.duration_seconds), 0.0) AS seconds
            FROM tuner_test_log l
            JOIN tuner_sessions s ON s.id = l.session_id
            WHERE s.context_id = ? AND l.regime IS NOT NULL
            GROUP BY l.regime
            """,
            (context_id,),
        ).fetchall()
        return {r["regime"]: (int(r["failures"]), float(r["seconds"])) for r in rows}

    def insert_tuner_test_log(
        self,
        session_id: int,
        core_id: int,
        offset: int,
        phase: str,
        passed: bool,
        error_msg: str | None = None,
        error_type: str | None = None,
        duration: float | None = None,
        run_id: int | None = None,
        backend: str | None = None,
        stress_mode: str | None = None,
        fft_preset: str | None = None,
        peak_stretch_pct: float | None = None,
        threads: int | None = None,
        profile: str | None = None,
        regime: str | None = None,
    ) -> int:
        cur = self.__conn.execute(
            """\
            INSERT INTO tuner_test_log
                (session_id, core_id, offset_tested, phase, passed,
                 error_message, error_type, duration_seconds, run_id,
                 backend, stress_mode, fft_preset, tested_at, peak_stretch_pct,
                 threads, profile, regime)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                session_id,
                core_id,
                offset,
                phase,
                int(passed),
                error_msg,
                error_type,
                duration,
                run_id,
                backend,
                stress_mode,
                fft_preset,
                self._now_iso(),
                peak_stretch_pct,
                threads,
                profile,
                regime,
            ),
        )
        return cur.lastrowid

    def get_tuner_test_log(self, session_id: int, core_id: int | None = None, limit: int | None = None) -> list[dict]:
        clauses = ["session_id=?"]
        params: list[int] = [session_id]
        if core_id is not None:
            clauses.append("core_id=?")
            params.append(core_id)
        where = " AND ".join(clauses)
        if limit is None:
            rows = self.__conn.execute(f"SELECT * FROM tuner_test_log WHERE {where} ORDER BY id", params).fetchall()
        else:
            rows = self.__conn.execute(
                f"SELECT * FROM tuner_test_log WHERE {where} ORDER BY id DESC LIMIT ?", (*params, limit)
            ).fetchall()
            rows.reverse()
        return [dict(r) for r in rows]

    def get_tuner_session_offsets(self, session_id: int) -> dict[int, int]:
        rows = self.__conn.execute(
            "SELECT core_id, best_offset FROM tuner_core_states WHERE session_id=? AND best_offset IS NOT NULL",
            (session_id,),
        ).fetchall()
        return {r["core_id"]: r["best_offset"] for r in rows}

    def get_tuner_best_profile(self, session_id: int) -> dict[int, int]:
        rows = self.__conn.execute(
            "SELECT core_id, best_offset FROM tuner_core_states "
            "WHERE session_id=? AND phase='confirmed' "
            "AND best_offset IS NOT NULL",
            (session_id,),
        ).fetchall()
        return {r["core_id"]: r["best_offset"] for r in rows}

    def delete_context_cascade(self, context_id: int) -> None:
        self.__conn.execute("BEGIN IMMEDIATE")
        try:
            run = self.__conn.execute(
                "SELECT id, status FROM runs WHERE context_id=? AND status='running' ORDER BY id LIMIT 1",
                (context_id,),
            ).fetchone()
            if run is not None:
                raise InFlightRecord(f"Cannot delete run {run['id']} with status {run['status']}")
            session = self.__conn.execute(
                "SELECT id, status FROM tuner_sessions WHERE context_id=? "
                "AND status IN ('running','paused','validating','hunting') ORDER BY id LIMIT 1",
                (context_id,),
            ).fetchone()
            if session is not None:
                raise InFlightRecord(f"Cannot delete tuner session {session['id']} with status {session['status']}")
            self.__conn.execute("DELETE FROM runs WHERE context_id=?", (context_id,))
            self.__conn.execute("DELETE FROM tuner_sessions WHERE context_id=?", (context_id,))
            self.__conn.execute("DELETE FROM tuning_contexts WHERE id=?", (context_id,))
            self.__conn.execute("COMMIT")
        except Exception:
            self.__conn.execute("ROLLBACK")
            raise

    def count_contexts(self) -> int:
        return self.__conn.execute("SELECT COUNT(*) FROM tuning_contexts").fetchone()[0]

    def count_tuner_sessions_for_context(self, context_id: int) -> int:
        return self.__conn.execute("SELECT COUNT(*) FROM tuner_sessions WHERE context_id=?", (context_id,)).fetchone()[
            0
        ]

    def get_status_counts(self) -> dict[str, int]:
        rows = self.__conn.execute("SELECT status, COUNT(*) as cnt FROM runs GROUP BY status").fetchall()
        return {r["status"]: r["cnt"] for r in rows}

    @staticmethod
    def _row_to_tuner_session(row: sqlite3.Row) -> TunerSession:
        return _tuner_sessions_spec().decode(row)

    def _execute_raw(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Internal: raw SQL access for testing. Not for application code."""
        return self.__conn.execute(sql, params)

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Merging another history database (one-database guarantee)
    # ------------------------------------------------------------------

    @staticmethod
    def _merge_tables() -> tuple[tuple[str, tuple[str, ...], dict[str, str]], ...]:
        core_states = _tuner_core_states_spec()
        return (
            (CORE_RESULTS.name, CORE_RESULTS.insert_columns, {"run_id": "runs"}),
            (EVENTS.name, EVENTS.insert_columns, {"run_id": "runs"}),
            (TELEMETRY_SAMPLES.name, TELEMETRY_SAMPLES.insert_columns, {"run_id": "runs"}),
            (core_states.name, core_states.insert_columns, {"session_id": "tuner_sessions"}),
            (TUNER_EVENTS.name, TUNER_EVENTS.insert_columns, {"session_id": "tuner_sessions"}),
            (
                TUNER_TEST_LOG.name,
                TUNER_TEST_LOG.insert_columns,
                {"session_id": "tuner_sessions", "run_id": "runs"},
            ),
        )

    def merge_from(self, other_path: str | Path) -> dict[str, int]:
        """Adopt every record from another corecycler history database.

        Merges a database left elsewhere (e.g. under /root by sudo runs):
        the source is first opened through HistoryDB (migrating it to the
        current schema, however old it is), then every run, tuning context and
        tuner session is copied in with fresh ids and remapped references.
        Tuning contexts deduplicate by (context_hash, bios_version). The source
        file is not modified beyond its schema migration. All-or-nothing:
        one transaction, rolled back on any error.
        """
        other_path = Path(other_path)
        HistoryDB(other_path).close()  # migrate source to the current schema
        conn = self.__conn
        conn.execute("ATTACH DATABASE ? AS src", (str(other_path),))
        counts = {"contexts": 0, "runs": 0, "tuner_sessions": 0}
        try:
            conn.execute("BEGIN")
            maps: dict[str, dict[int, int]] = {}

            ctx_map: dict[int, int] = {}
            for row in conn.execute("SELECT * FROM src.tuning_contexts ORDER BY id").fetchall():
                columns = TUNING_CONTEXTS.insert_columns
                placeholders = ",".join("?" * len(columns))
                cur = conn.execute(
                    f"INSERT OR IGNORE INTO {TUNING_CONTEXTS.name} ({','.join(columns)}) VALUES ({placeholders})",
                    tuple(row[column] for column in columns),
                )
                if cur.rowcount > 0:
                    ctx_map[row["id"]] = cur.lastrowid
                    counts["contexts"] += 1
                else:  # already present - dedup to the existing context
                    existing = conn.execute(
                        "SELECT id FROM tuning_contexts WHERE context_hash=? AND bios_version=?",
                        (row["context_hash"], row["bios_version"]),
                    ).fetchone()
                    ctx_map[row["id"]] = existing["id"]
            maps["tuning_contexts"] = ctx_map

            def copy_parent(table: str, cols: tuple[str, ...]) -> dict[int, int]:
                id_map: dict[int, int] = {}
                for row in conn.execute(f"SELECT * FROM src.{table} ORDER BY id").fetchall():
                    vals = []
                    for c in cols:
                        v = row[c]
                        if c == "context_id" and v is not None:
                            v = ctx_map.get(v)  # orphan context -> ungrouped
                        vals.append(v)
                    placeholders = ",".join("?" * len(cols))
                    cur = conn.execute(
                        f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})",
                        vals,
                    )
                    id_map[row["id"]] = cur.lastrowid
                return id_map

            maps["runs"] = copy_parent(RUNS.name, RUNS.insert_columns)
            counts["runs"] = len(maps["runs"])

            maps["tuner_sessions"] = copy_parent(_tuner_sessions_spec().name, _tuner_sessions_spec().insert_columns)
            counts["tuner_sessions"] = len(maps["tuner_sessions"])

            for table, cols, remaps in self._merge_tables():
                for row in conn.execute(f"SELECT * FROM src.{table} ORDER BY id").fetchall():
                    vals = []
                    skip = False
                    for c in cols:
                        v = row[c]
                        if c in remaps and v is not None:
                            v = maps[remaps[c]].get(v)
                            if v is None and c != "run_id":
                                skip = True  # orphaned child of a missing parent
                                break
                        vals.append(v)
                    if skip:
                        continue
                    placeholders = ",".join("?" * len(cols))
                    conn.execute(
                        f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})",
                        vals,
                    )

            # journal has no id column - copy keyed rows directly
            for row in conn.execute("SELECT * FROM src.tuner_co_journal ORDER BY session_id, core_id").fetchall():
                new_sid = maps["tuner_sessions"].get(row["session_id"])
                if new_sid is None:
                    continue
                columns = TUNER_CO_JOURNAL.columns
                conn.execute(
                    f"INSERT INTO {TUNER_CO_JOURNAL.name} ({','.join(columns)}) "
                    f"VALUES ({','.join('?' * len(columns))})",
                    tuple(new_sid if column == "session_id" else row[column] for column in columns),
                )

            for row in conn.execute(
                "SELECT * FROM src.tuner_regime_banks ORDER BY context_id, core_id, regime, offset_value"
            ).fetchall():
                context_id = ctx_map.get(row["context_id"])
                if context_id is None:
                    continue
                columns = TUNER_REGIME_BANKS.columns
                conn.execute(
                    f"INSERT INTO {TUNER_REGIME_BANKS.name} ({','.join(columns)}) "
                    f"VALUES ({','.join('?' * len(columns))}) "
                    "ON CONFLICT(context_id, core_id, regime, offset_value) DO UPDATE SET "
                    "clean_seconds = tuner_regime_banks.clean_seconds + excluded.clean_seconds, "
                    "updated_at = MAX(tuner_regime_banks.updated_at, excluded.updated_at)",
                    tuple(context_id if column == "context_id" else row[column] for column in columns),
                )

            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.execute("DETACH DATABASE src")
        return counts

    def delete_orphaned_contexts(self) -> int:
        """Delete tuning contexts that have no associated runs or tuner sessions."""
        cursor = self.__conn.execute(
            "DELETE FROM tuning_contexts WHERE id NOT IN "
            "(SELECT DISTINCT context_id FROM runs WHERE context_id IS NOT NULL) "
            "AND id NOT IN "
            "(SELECT DISTINCT context_id FROM tuner_sessions WHERE context_id IS NOT NULL)"
        )
        return cursor.rowcount

    def delete_tuner_session(self, session_id: int) -> None:
        self.__conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.__conn.execute("SELECT status FROM tuner_sessions WHERE id=?", (session_id,)).fetchone()
            if row is not None and row["status"] in {"running", "paused", "validating", "hunting"}:
                raise InFlightRecord(f"Cannot delete tuner session {session_id} with status {row['status']}")
            self.__conn.execute("DELETE FROM tuner_sessions WHERE id=?", (session_id,))
            self.__conn.execute("COMMIT")
        except Exception:
            self.__conn.execute("ROLLBACK")
            raise

    def count_tuner_sessions(self) -> int:
        return self.__conn.execute("SELECT COUNT(*) FROM tuner_sessions").fetchone()[0]

    def recover_incomplete_runs(self) -> list[tuple[int, str]]:
        """Mark any 'running' runs as 'crashed'. Returns list of (id, started_at) recovered."""
        stale = self.__conn.execute("SELECT id, started_at FROM runs WHERE status='running'").fetchall()
        if stale:
            self.__conn.execute(
                "UPDATE runs SET status='crashed', finished_at=? WHERE status='running'",
                (self._now_iso(),),
            )
        return [(r["id"], r["started_at"]) for r in stale]

    def purge_before(self, iso_date: str) -> int:
        """Delete all runs started before the given ISO date. Returns count deleted."""
        cur = self.__conn.execute(
            "DELETE FROM runs WHERE started_at < ?",
            (iso_date,),
        )
        return cur.rowcount

    def vacuum(self) -> None:
        """Reclaim space after bulk deletes."""
        self.__conn.execute("VACUUM")

    def close(self) -> None:
        self.__conn.close()
        if str(self._db_path) != ":memory:":
            # WAL sidecars may have been recreated (root-owned) during the run.
            fix_sudo_ownership(
                self._db_path,
                self._db_path.with_name(self._db_path.name + "-wal"),
                self._db_path.with_name(self._db_path.name + "-shm"),
            )
