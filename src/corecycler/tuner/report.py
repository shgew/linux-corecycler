"""Machine-readable and human-readable tuner evidence reports."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from corecycler.tuner.config import TunerConfig
from corecycler.tuner.regime import Regime

_UNREPORTABLE_STATUSES = frozenset({"platform_fault", "profile_quarantined"})

if TYPE_CHECKING:
    from corecycler.history.db import HistoryDB
    from corecycler.tuner.state import CoreState, TunerSession


def bios_offset(
    proven_offset: int,
    *,
    baseline_offset: int,
    direction: int,
    fine_step: int,
    guard_band_steps: int,
) -> int:
    """Move a proven runtime offset toward its stock baseline for BIOS use."""
    guarded = proven_offset - direction * fine_step * guard_band_steps
    if direction * (guarded - baseline_offset) <= 0:
        return baseline_offset
    return guarded


def _project_core_row(
    db: HistoryDB,
    session: TunerSession,
    config: TunerConfig,
    core_state: CoreState,
) -> dict:
    regimes = [str(item) for item in Regime]
    quarantined = session.status in _UNREPORTABLE_STATUSES
    proven = core_state.proven_offset
    accepted = None if quarantined else proven
    recommendation = (
        None
        if accepted is None
        else bios_offset(
            accepted,
            baseline_offset=core_state.baseline_offset,
            direction=config.direction,
            fine_step=config.fine_step,
            guard_band_steps=config.bios_guard_band_steps,
        )
    )
    evidence_offset = proven
    if evidence_offset is None:
        evidence_offset = core_state.best_offset if core_state.best_offset is not None else core_state.current_offset
    banks = (
        db.get_regime_banks(session.context_id, core_state.core_id, evidence_offset)
        if session.context_id is not None
        else {}
    )
    hours = {item: banks.get(item, 0.0) / 3600.0 for item in regimes}
    failures: dict[str, int] = {}
    entries = db.get_tuner_test_log(session.id, core_state.core_id)
    for entry in entries:
        if entry["passed"]:
            continue
        kind = entry["error_type"] or "unknown"
        failures[kind] = failures.get(kind, 0) + 1
    return {
        "core": core_state.core_id,
        "accepted_offset": accepted,
        "proven_offset": proven,
        "bios_offset": recommendation,
        "candidate_offset": core_state.current_offset,
        "phase": str(core_state.phase),
        "hours": hours,
        "confidence_hours": min(hours.values()) if hours else 0.0,
        "anneal_strikes": core_state.anneal_strikes,
        "suspicion": core_state.suspicion,
        "stress_hours": sum(float(entry["duration_seconds"] or 0.0) for entry in entries) / 3600.0,
        "crashes": core_state.crash_count,
        "failures": failures,
    }


def core_row(db: HistoryDB, session_id: int, core_id: int) -> dict:
    """Project one persisted core through the canonical report semantics."""
    session = db.get_tuner_session(session_id)
    if session is None:
        raise ValueError(f"no tuner session {session_id}")
    core_state = db.get_tuner_core_states(session_id).get(core_id)
    if core_state is None:
        raise ValueError(f"no core {core_id} in tuner session {session_id}")
    return _project_core_row(db, session, TunerConfig.from_json(session.config_json), core_state)


def build(db: HistoryDB, session_id: int) -> dict:
    """Build the canonical machine-readable report for one session."""
    session = db.get_tuner_session(session_id)
    if session is None:
        raise ValueError(f"no tuner session {session_id}")
    config = TunerConfig.from_json(session.config_json)
    context_id = session.context_id
    context = db.get_context(context_id) if context_id is not None else None
    states = db.get_tuner_core_states(session_id)
    cores = [_project_core_row(db, session, config, states[core_id]) for core_id in sorted(states)]
    yields = db.regime_yield(context_id) if context_id is not None else {}
    return {
        "session": session_id,
        "status": session.status,
        "cpu": session.cpu_model,
        "bios": session.bios_version,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "context": {
            "hash": context.context_hash if context else "",
            "ppt_limit_w": context.ppt_limit_w if context else None,
            "tdc_limit_a": context.tdc_limit_a if context else None,
            "edc_limit_a": context.edc_limit_a if context else None,
            "pbo_scalar": context.pbo_scalar if context else None,
        },
        "bios_guard_band": {
            "steps": config.bios_guard_band_steps,
            "fine_step": config.fine_step,
            "direction": config.direction,
        },
        "unattributed_crashes": session.unattributed_crashes,
        "endurance_round": session.endurance_round,
        "cores": cores,
        "regime_yield": {
            item: {"failures": failures, "hours": seconds / 3600.0}
            for item, (failures, seconds) in sorted(yields.items())
        },
        "total_stress_hours": sum(core["stress_hours"] for core in cores),
    }


def render(report: dict) -> list[str]:
    """Render the machine-readable report as stable, scannable text."""
    regimes = [str(item) for item in Regime]
    lines = [
        f"session #{report['session']}  {report['status']}  {report['cpu'] or 'unknown CPU'}",
        f"BIOS {report['bios'] or 'unknown'}  context {report['context']['hash'][:12] or 'none'}",
    ]
    limits = report["context"]
    shown = [
        f"{label} {limits[key]:g}{unit}"
        for key, label, unit in (
            ("ppt_limit_w", "PPT", "W"),
            ("tdc_limit_a", "TDC", "A"),
            ("edc_limit_a", "EDC", "A"),
        )
        if limits[key] is not None
    ]
    if shown:
        lines.append("limits  " + "  ".join(shown))
    lines.append(
        f"{report['total_stress_hours']:.1f} stress-hours total, "
        f"endurance round {report['endurance_round']}, "
        f"{report['unattributed_crashes']} unattributed crash(es)"
    )
    lines.append("")
    if report["status"] in _UNREPORTABLE_STATUSES:
        lines.extend(("Historical offsets are unsafe. Remain at stock CO=0.", ""))
    header = f"{'core':>4}  {'candidate':>9}  {'proven':>6}  {'BIOS':>6}  {'confidence':>10}  " + "  ".join(
        f"{item:>9}" for item in regimes
    )
    lines.append(header)
    for row in sorted(
        report["cores"],
        key=lambda item: (
            item["accepted_offset"] is None,
            item["accepted_offset"] if item["accepted_offset"] is not None else item["candidate_offset"],
        ),
    ):
        banked = "  ".join(f"{row['hours'].get(item, 0.0):>8.1f}h" for item in regimes)
        proven = "-" if row["accepted_offset"] is None else str(row["accepted_offset"])
        recommendation = "-" if row["bios_offset"] is None else str(row["bios_offset"])
        lines.append(
            f"{row['core']:>4}  {row['candidate_offset']:>9}  {proven:>6}  {recommendation:>6}  "
            f"{row['confidence_hours']:>9.1f}h  {banked}"
        )
    lines.append("")
    for row in sorted(report["cores"], key=lambda item: item["core"]):
        detail = ", ".join(f"{kind}x{count}" for kind, count in sorted(row["failures"].items())) or "no failures"
        lines.append(
            f"core {row['core']}: {row['phase']}, {row['crashes']} crash(es), "
            f"{row['anneal_strikes']} anneal strike(s); {detail}"
        )
    lines.append("")
    lines.append("regime yield (what each load class has actually caught here)")
    for item, stats in report["regime_yield"].items():
        lines.append(f"  {item:<10} {stats['failures']:>4} failure(s) in {stats['hours']:.1f}h")
    if (
        report["status"] not in _UNREPORTABLE_STATUSES
        and report["cores"]
        and all(row["bios_offset"] is not None for row in report["cores"])
    ):
        guard = report["bios_guard_band"]
        lines.extend(
            (
                "",
                f"BIOS recommendations include a {guard['steps']}-step guard band "
                f"({guard['fine_step']} CO unit(s) per step) toward stock.",
                "Proven offsets are volatile SMU overlays; use the guarded BIOS values for persistent settings.",
            )
        )
    return lines


def to_json(report: dict) -> str:
    return json.dumps(report, indent=2, sort_keys=True)
