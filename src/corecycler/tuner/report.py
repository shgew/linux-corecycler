"""The answer, and exactly how much it is worth.

A per-core offset with no provenance is the state this tuner exists to get
out of. Every number here is paired with the evidence behind it: hours banked
per regime, what failed and how, and a confidence taken from the weakest
regime rather than the total, because a vector is only as proven as its
least-tested load class.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from corecycler.tuner.regime import Regime

if TYPE_CHECKING:
    from corecycler.history.db import HistoryDB


def _core_rows(db: HistoryDB, session_id: int, context_hash: str) -> list[dict]:
    from corecycler.tuner import persistence as tp

    states = tp.load_core_states(db, session_id)
    regimes = [str(r) for r in Regime]
    rows = []
    for core_id in sorted(states):
        cs = states[core_id]
        offset = cs.best_offset if cs.best_offset is not None else cs.current_offset
        banks = db.get_regime_banks(context_hash, core_id, offset) if context_hash else {}
        hours = {r: banks.get(r, 0.0) / 3600.0 for r in regimes}
        failures: dict[str, int] = {}
        for entry in db.get_tuner_test_log(session_id, core_id):
            if entry["passed"]:
                continue
            kind = entry["error_type"] or "unknown"
            failures[kind] = failures.get(kind, 0) + 1
        rows.append(
            {
                "core": core_id,
                "offset": offset,
                "current": cs.current_offset,
                "phase": str(cs.phase),
                "hours": hours,
                # The weakest regime is the confidence, not the total: one
                # cheap regime must not buy trust the others never earned.
                "confidence_hours": min(hours.values()) if hours else 0.0,
                "anneal_strikes": cs.anneal_strikes,
                "suspicion": cs.suspicion,
                "stress_hours": cs.cumulative_test_time / 3600.0,
                "crashes": cs.crash_count,
                "failures": failures,
            }
        )
    return rows


def build(db: HistoryDB, session_id: int) -> dict:
    """The machine-readable report for one session."""
    from corecycler.tuner import persistence as tp

    session = tp.get_session(db, session_id)
    if session is None:
        raise ValueError(f"no tuner session {session_id}")
    context = db.get_context(session.context_id) if session.context_id else None
    context_hash = context.co_hash if context else ""
    cores = _core_rows(db, session_id, context_hash)
    yields = db.regime_yield(context_hash) if context_hash else {}
    return {
        "session": session_id,
        "status": session.status,
        "cpu": session.cpu_model,
        "bios": session.bios_version,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "context": {
            "hash": context_hash,
            "ppt_limit_w": context.ppt_limit_w if context else None,
            "tdc_limit_a": context.tdc_limit_a if context else None,
            "edc_limit_a": context.edc_limit_a if context else None,
            "pbo_scalar": context.pbo_scalar if context else None,
        },
        "unattributed_crashes": session.unattributed_crashes,
        "endurance_round": session.endurance_round,
        "cores": cores,
        "regime_yield": {
            regime: {"failures": failures, "hours": seconds / 3600.0}
            for regime, (failures, seconds) in sorted(yields.items())
        },
        "total_stress_hours": sum(c["stress_hours"] for c in cores),
    }


def render(report: dict) -> list[str]:
    """The same report as text, ordered most-aggressive core first."""
    regimes = [str(r) for r in Regime]
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
    header = f"{'core':>4}  {'offset':>6}  {'proven':>6}  " + "  ".join(f"{r:>9}" for r in regimes)
    lines.append(header)
    # Deepest offset first: the silicon-quality ordering is the thing a human
    # actually reads this table for.
    for row in sorted(report["cores"], key=lambda r: r["offset"]):
        banked = "  ".join(f"{row['hours'].get(r, 0.0):>8.1f}h" for r in regimes)
        lines.append(f"{row['core']:>4}  {row['offset']:>6}  {row['confidence_hours']:>5.1f}h  {banked}")
    lines.append("")
    for row in sorted(report["cores"], key=lambda r: r["core"]):
        detail = ", ".join(f"{k}x{v}" for k, v in sorted(row["failures"].items())) or "no failures"
        lines.append(
            f"core {row['core']}: {row['phase']}, {row['crashes']} crash(es), "
            f"{row['anneal_strikes']} anneal strike(s); {detail}"
        )
    lines.append("")
    lines.append("regime yield (what each load class has actually caught here)")
    for regime, stats in report["regime_yield"].items():
        lines.append(f"  {regime:<10} {stats['failures']:>4} failure(s) in {stats['hours']:.1f}h")
    lines.append("")
    lines.append("Offsets are volatile SMU overlays; enter them in BIOS to keep them across a reboot.")
    return lines


def to_json(report: dict) -> str:
    return json.dumps(report, indent=2, sort_keys=True)
