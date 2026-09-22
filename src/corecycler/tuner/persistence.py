"""Tuner-specific persistence policy and Curve Optimizer journal operations."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from corecycler.history.db import HistoryDB
    from corecycler.tuner.state import CoreState, TunerSession


def journal_co_intent(db: HistoryDB, session_id: int, core_id: int, value: int, survived: bool) -> None:
    """Durably record a CO value before it is written to the SMU."""
    db.journal_co_intent(session_id, core_id, value, survived)


def journal_mark_survived(db: HistoryDB, session_id: int, exclude_cores: tuple[int, ...] | list[int] = ()) -> None:
    """Mark resident CO values survived, except cores with contrary evidence."""
    db.journal_mark_survived(session_id, exclude_cores)


def journal_suspects(db: HistoryDB, session_id: int) -> list[tuple[int, int]]:
    """Return offsets that were resident when the machine died."""
    return db.journal_suspects(session_id)


def journal_survived_values(db: HistoryDB, session_id: int) -> dict[int, int]:
    """Return offsets proven survivable in this session."""
    return db.journal_survived_values(session_id)


def journal_values(db: HistoryDB, session_id: int) -> dict[int, int]:
    """Return the last CO value the tuner wrote for each core."""
    return db.journal_values(session_id)


def pick_auto_resume_session(db: HistoryDB) -> TunerSession | None:
    """Return the active session only when unattended resume is appropriate."""
    session = db.get_active_tuner_session()
    if session is not None and session.status in ("running", "validating", "hunting"):
        return session
    return None


SEARCH_EVIDENCE_PHASES = frozenset(
    {
        "coarse",
        "fine",
        "confirm",
        "backoff_preconfirm",
        "backoff_confirm",
        "annealing",
    }
)
VALIDATION_EVIDENCE_PHASES = frozenset(
    {
        "validate_s1",
        "validate_s2",
        "validate_s3",
        "validate_s4",
        "validate_s5",
        "validate_s6",
        "validate_s7",
        "endurance",
    }
)
LIVE_EVIDENCE_PHASES = SEARCH_EVIDENCE_PHASES | VALIDATION_EVIDENCE_PHASES


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
    """Return passing live-mask seconds grouped by core and workload."""
    summary: dict[int, dict[str, float]] = {}
    for row in db.get_tuner_test_log(session_id):
        core_state = core_states.get(row["core_id"])
        duration = row["duration_seconds"]
        if core_state is None or not row["passed"] or not isinstance(duration, (int, float)):
            continue
        phase = row["phase"]
        if phase not in LIVE_EVIDENCE_PHASES:
            continue
        if phase in SEARCH_EVIDENCE_PHASES and row["regime"] is None:
            continue
        best = core_state.best_offset if core_state.best_offset is not None else core_state.baseline_offset
        if direction * row["offset_tested"] < direction * best:
            continue
        label = workload_label(
            row["backend"],
            row["stress_mode"],
            row["fft_preset"],
            row["threads"],
            row["profile"],
        )
        per_label = summary.setdefault(core_state.core_id, {})
        per_label[label] = per_label.get(label, 0.0) + float(duration)
    return summary


def format_evidence_line(core_id: int, offset: int | None, per_label: dict[str, float]) -> str:
    total = sum(per_label.values()) / 3600
    line = f"core {core_id} @ {'n/a' if offset is None else offset}: {total:.1f}h live evidence"
    if not per_label:
        return line + " (none yet)"
    ranked = sorted(per_label.items(), key=lambda item: -item[1])
    parts = ", ".join(f"{label} {seconds / 3600:.1f}h" for label, seconds in ranked)
    return f"{line} ({parts})"
