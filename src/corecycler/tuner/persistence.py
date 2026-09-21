"""Database operations for the auto-tuner — sessions, core states, test log.

All functions delegate to public HistoryDB methods.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from corecycler.history.db import HistoryDB

    from .config import TunerConfig
    from .state import CoreState, TunerSession


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def create_session(
    db: HistoryDB,
    config: TunerConfig,
    bios_version: str,
    cpu_model: str,
    context_id: int | None = None,
) -> int:
    """Create a new tuner session. Returns the session id."""
    return db.create_tuner_session(config.to_json(), bios_version, cpu_model, context_id)


def update_session_status(db: HistoryDB, session_id: int, status: str) -> None:
    db.update_tuner_session_status(session_id, status)


def get_session(db: HistoryDB, session_id: int) -> TunerSession | None:
    return db.get_tuner_session(session_id)


def get_latest_session(db: HistoryDB) -> TunerSession | None:
    return db.get_latest_tuner_session()


def get_active_session(db: HistoryDB) -> TunerSession | None:
    """Return session with status 'running' or 'paused', if any."""
    return db.get_active_tuner_session()


# ---------------------------------------------------------------------------
# Core states
# ---------------------------------------------------------------------------


def save_core_state(db: HistoryDB, session_id: int, cs: CoreState) -> None:
    """Upsert a core state row."""
    db.upsert_tuner_core_state(session_id, cs)


def checkpoint(db: HistoryDB) -> None:
    """Flush the WAL to disk — for state that must survive a hard crash."""
    db.checkpoint()


def load_core_states(db: HistoryDB, session_id: int) -> dict[int, CoreState]:
    return db.get_tuner_core_states(session_id)


# ---------------------------------------------------------------------------
# CO write-ahead journal + resume-crash circuit breaker
# ---------------------------------------------------------------------------


def journal_co_intent(db: HistoryDB, session_id: int, core_id: int, value: int, survived: bool) -> None:
    """Durably record a CO value before it is written to the SMU."""
    db.journal_co_intent(session_id, core_id, value, survived)


def journal_mark_survived(db: HistoryDB, session_id: int, exclude_cores: tuple[int, ...] | list[int] = ()) -> None:
    """Mark all resident CO values survived after a test completes without a crash.

    Cores in ``exclude_cores`` stay un-survived — fresh contrary evidence (a
    corrected MCE named them during this very test) outranks the survival.
    """
    db.journal_mark_survived(session_id, exclude_cores)


def journal_suspects(db: HistoryDB, session_id: int) -> list[tuple[int, int]]:
    """Return (core_id, value) offsets that were resident when the machine died."""
    return db.journal_suspects(session_id)


def journal_survived_values(db: HistoryDB, session_id: int) -> dict[int, int]:
    """Return {core_id: value} offsets proven survivable this session."""
    return db.journal_survived_values(session_id)


def get_resume_crash_streak(db: HistoryDB, session_id: int) -> int:
    return db.get_resume_crash_streak(session_id)


def set_resume_crash_streak(db: HistoryDB, session_id: int, value: int) -> None:
    db.set_resume_crash_streak(session_id, value)


def journal_values(db: HistoryDB, session_id: int) -> dict[int, int]:
    """Return {core_id: value} — the last CO value the tuner wrote per core."""
    return db.journal_values(session_id)


def get_unattributed_crashes(db: HistoryDB, session_id: int) -> int:
    return db.get_unattributed_crashes(session_id)


def set_unattributed_crashes(db: HistoryDB, session_id: int, value: int) -> None:
    db.set_unattributed_crashes(session_id, value)


def set_hunt_state(db: HistoryDB, session_id: int, blob: str) -> None:
    """Durably record bisection progress before the probe that may reboot us."""
    db.set_hunt_state(session_id, blob)


def set_hunting_core(db: HistoryDB, session_id: int, core_id: int | None) -> None:
    """Durably record which core an isolated hunt slot stresses, before it runs."""
    db.set_hunting_core(session_id, core_id)


def log_event(db: HistoryDB, session_id: int, message: str, boot_id: str = "", severity: str = "info") -> None:
    db.insert_tuner_event(session_id, message, boot_id, severity)


def get_events(db: HistoryDB, session_id: int, limit: int = 200) -> list[dict]:
    return db.get_tuner_events(session_id, limit)


def pick_auto_resume_session(db: HistoryDB):
    """The session login-autostart may resume: mid-run (running/validating)
    only. A paused session is a deliberate human choice; quarantined and
    completed ones are excluded by the active query."""
    session = db.get_active_tuner_session()
    if session is not None and session.status in ("running", "validating"):
        return session
    return None


def set_validation_position(
    db: HistoryDB,
    session_id: int,
    stage: int,
    index: int,
    half: int,
    dirty: bool,
    requeue_json: str,
) -> None:
    """Persist the validation cursor so progress survives reboots/restarts."""
    db.set_validation_position(session_id, stage, index, half, dirty, requeue_json)


def set_endurance_position(db: HistoryDB, session_id: int, round_: int, workload: int, index: int) -> None:
    """Persist the endurance cursor so a reboot mid-round resumes in place."""
    db.set_endurance_position(session_id, round_, workload, index)


def set_session_boot(db: HistoryDB, session_id: int, boot_id: str) -> None:
    db.set_session_boot(session_id, boot_id)


def update_session_config(db: HistoryDB, session_id: int, config_json: str) -> None:
    db.update_tuner_session_config(session_id, config_json)


# ---------------------------------------------------------------------------
# Test log
# ---------------------------------------------------------------------------


def log_test_result(
    db: HistoryDB,
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
    return db.insert_tuner_test_log(
        session_id,
        core_id,
        offset,
        phase,
        passed,
        error_msg,
        error_type,
        duration,
        run_id,
        backend=backend,
        stress_mode=stress_mode,
        fft_preset=fft_preset,
        peak_stretch_pct=peak_stretch_pct,
        threads=threads,
        profile=profile,
        regime=regime,
    )


def get_test_log(db: HistoryDB, session_id: int, core_id: int | None = None) -> list[dict]:
    return db.get_tuner_test_log(session_id, core_id)


def get_best_profile(db: HistoryDB, session_id: int) -> dict[int, int]:
    """Return {core_id: confirmed_offset} for all CONFIRMED cores."""
    return db.get_tuner_best_profile(session_id)


def get_session_offsets(db: HistoryDB, session_id: int) -> dict[int, int]:
    """Return {core_id: best_offset} for every core with a value, any phase."""
    return db.get_tuner_session_offsets(session_id)


# ---------------------------------------------------------------------------
# Live-evidence ledger
# ---------------------------------------------------------------------------

# Only all-offsets-live phases count as evidence. A hunt probe deliberately
# parks cores at stock, which holds the shared VDDCR rail high and overstates
# what the profile survives in use, so hunt slots never bank as evidence.
# Ordinary search does run the live mask and does count.
LIVE_EVIDENCE_PHASES = frozenset(
    {
        "coarse",
        "fine",
        "confirm",
        "backoff_preconfirm",
        "backoff_confirm",
        "validate_s1",
        "validate_s2",
        "validate_s3",
        "validate_s5",
        "validate_s6",
        "endurance",
    }
)


def workload_label(
    backend: str | None,
    stress_mode: str | None,
    fft_preset: str | None,
    threads: int | None = None,
    profile: str | None = None,
) -> str:
    label = f"{backend} {stress_mode} {fft_preset}"
    if threads:
        label += f" {threads}T"
    if profile == "spectrum":
        label += " spectrum"
    elif profile == "transitions":
        label += " transitions"
    return label


def evidence_summary(
    db: HistoryDB,
    session_id: int,
    core_states: dict[int, CoreState],
    direction: int,
) -> dict[int, dict[str, float]]:
    """Per-core, per-workload seconds of passing all-offsets-live evidence at
    an offset at least as aggressive as the core's current best."""
    summary: dict[int, dict[str, float]] = {}
    for row in get_test_log(db, session_id):
        cs = core_states.get(row["core_id"])
        duration = row["duration_seconds"]
        if cs is None or not row["passed"] or not isinstance(duration, (int, float)):
            continue
        if row["phase"] not in LIVE_EVIDENCE_PHASES:
            continue
        best = cs.best_offset if cs.best_offset is not None else cs.baseline_offset
        if direction * row["offset_tested"] < direction * best:
            continue
        label = workload_label(row["backend"], row["stress_mode"], row["fft_preset"], row["threads"], row["profile"])
        per_label = summary.setdefault(cs.core_id, {})
        per_label[label] = per_label.get(label, 0.0) + float(duration)
    return summary


def format_evidence_line(core_id: int, offset: int | None, per_label: dict[str, float]) -> str:
    total = sum(per_label.values()) / 3600
    line = f"core {core_id} @ {'n/a' if offset is None else offset}: {total:.1f}h live evidence"
    if not per_label:
        return line + " (none yet)"
    ranked = sorted(per_label.items(), key=lambda kv: -kv[1])
    parts = ", ".join(f"{label} {seconds / 3600:.1f}h" for label, seconds in ranked)
    return f"{line} ({parts})"
