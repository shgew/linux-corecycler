"""Machine-readable and human-readable tuner evidence reports."""

from __future__ import annotations

import json

import pytest

from corecycler.history.db import HistoryDB, TuningContextRecord
from corecycler.tuner import persistence as tp
from corecycler.tuner.config import TunerConfig
from corecycler.tuner.report import build, render, to_json
from corecycler.tuner.state import CoreState, TunerPhase


@pytest.fixture
def db():
    history = HistoryDB(":memory:")
    yield history
    history.close()


def test_missing_session_is_reported_by_id(db):
    with pytest.raises(ValueError, match=r"^no tuner session 404$"):
        build(db, 404)


def test_empty_session_reports_zero_evidence_without_inventing_limits(db):
    session_id = tp.create_session(db, TunerConfig(), "Empty BIOS", "Empty CPU")

    report = build(db, session_id)
    lines = render(report)

    assert report["cores"] == []
    assert report["regime_yield"] == {}
    assert report["total_stress_hours"] == 0
    assert report["context"] == {
        "hash": "",
        "ppt_limit_w": None,
        "tdc_limit_a": None,
        "edc_limit_a": None,
        "pbo_scalar": None,
    }
    assert lines[:3] == [
        f"session #{session_id}  running  Empty CPU",
        "BIOS Empty BIOS  context none",
        "0.0 stress-hours total, endurance round 0, 0 unattributed crash(es)",
    ]
    assert not any(line.startswith("limits ") for line in lines)
    assert "regime yield (what each load class has actually caught here)" in lines
    assert lines[-1] == "Offsets are volatile SMU overlays; enter them in BIOS to keep them across a reboot."


def _seed_evidence_report(db: HistoryDB) -> int:
    context_hash = "0123456789abcdef"
    context_id = db.create_context(
        TuningContextRecord(
            bios_version="Test BIOS",
            co_offsets_json='{"0": -32, "1": -20}',
            co_hash=context_hash,
            pbo_scalar=2.0,
            ppt_limit_w=120.0,
            tdc_limit_a=75.0,
            edc_limit_a=110.0,
        )
    )
    session_id = tp.create_session(
        db,
        TunerConfig(cores_to_test=[0, 1]),
        "Test BIOS",
        "Ryzen Test",
        context_id=context_id,
    )
    tp.update_session_status(db, session_id, "completed")
    db.set_unattributed_crashes(session_id, 4)
    db.set_endurance_position(session_id, 7, 1, 2)

    tp.save_core_state(
        db,
        session_id,
        CoreState(
            core_id=0,
            phase=TunerPhase.ANNEALING,
            current_offset=-30,
            best_offset=-32,
            crash_count=3,
            cumulative_test_time=9000.0,
            anneal_strikes=2,
            suspicion=1.75,
        ),
    )
    tp.save_core_state(
        db,
        session_id,
        CoreState(
            core_id=1,
            phase=TunerPhase.CONFIRMED,
            current_offset=-20,
            cumulative_test_time=1800.0,
        ),
    )

    for regime, seconds in {
        "boost": 7200.0,
        "current": 3600.0,
        "transient": 1800.0,
        "coupled": 10800.0,
    }.items():
        db.bank_regime_time(context_hash, 0, regime, -32, seconds)

    tp.log_test_result(
        db,
        session_id,
        0,
        -32,
        "anneal",
        True,
        duration=3600.0,
        regime="boost",
    )
    tp.log_test_result(
        db,
        session_id,
        0,
        -33,
        "anneal",
        False,
        error_type="worker_exit",
        duration=1800.0,
        regime="boost",
    )
    tp.log_test_result(
        db,
        session_id,
        0,
        -33,
        "anneal",
        False,
        error_type="worker_exit",
        duration=900.0,
        regime="boost",
    )
    tp.log_test_result(
        db,
        session_id,
        0,
        -32,
        "confirm",
        False,
        error_type="mce",
        duration=3600.0,
        regime="current",
    )
    tp.log_test_result(
        db,
        session_id,
        0,
        -32,
        "confirm",
        False,
        duration=1800.0,
        regime="transient",
    )
    return session_id


def test_report_exposes_banked_confidence_failures_and_session_counters(db):
    session_id = _seed_evidence_report(db)

    report = build(db, session_id)

    assert report["session"] == session_id
    assert report["status"] == "completed"
    assert report["cpu"] == "Ryzen Test"
    assert report["bios"] == "Test BIOS"
    assert report["context"] == {
        "hash": "0123456789abcdef",
        "ppt_limit_w": 120.0,
        "tdc_limit_a": 75.0,
        "edc_limit_a": 110.0,
        "pbo_scalar": 2.0,
    }
    assert report["unattributed_crashes"] == 4
    assert report["endurance_round"] == 7
    assert report["total_stress_hours"] == 3.0
    assert report["cores"] == [
        {
            "core": 0,
            "offset": -32,
            "current": -30,
            "phase": "annealing",
            "hours": {"boost": 2.0, "current": 1.0, "transient": 0.5, "coupled": 3.0},
            "confidence_hours": 0.5,
            "anneal_strikes": 2,
            "suspicion": 1.75,
            "stress_hours": 2.5,
            "crashes": 3,
            "failures": {"worker_exit": 2, "mce": 1, "unknown": 1},
        },
        {
            "core": 1,
            "offset": -20,
            "current": -20,
            "phase": "confirmed",
            "hours": {"boost": 0.0, "current": 0.0, "transient": 0.0, "coupled": 0.0},
            "confidence_hours": 0.0,
            "anneal_strikes": 0,
            "suspicion": 0.0,
            "stress_hours": 0.5,
            "crashes": 0,
            "failures": {},
        },
    ]
    assert report["regime_yield"] == {
        "boost": {"failures": 2, "hours": 1.75},
        "current": {"failures": 1, "hours": 1.0},
        "transient": {"failures": 1, "hours": 0.5},
    }


def test_text_report_orders_offsets_and_spells_out_the_evidence(db):
    report = build(db, _seed_evidence_report(db))

    lines = render(report)

    assert lines[:4] == [
        f"session #{report['session']}  completed  Ryzen Test",
        "BIOS Test BIOS  context 0123456789ab",
        "limits  PPT 120W  TDC 75A  EDC 110A",
        "3.0 stress-hours total, endurance round 7, 4 unattributed crash(es)",
    ]
    core_rows = [line for line in lines if line.strip().startswith(("0 ", "1 "))]
    assert core_rows[0].split() == ["0", "-32", "0.5h", "2.0h", "1.0h", "0.5h", "3.0h"]
    assert core_rows[1].split() == ["1", "-20", "0.0h", "0.0h", "0.0h", "0.0h", "0.0h"]
    assert "core 0: annealing, 3 crash(es), 2 anneal strike(s); mcex1, unknownx1, worker_exitx2" in lines
    assert "core 1: confirmed, 0 crash(es), 0 anneal strike(s); no failures" in lines
    assert "  boost         2 failure(s) in 1.8h" in lines
    assert "  current       1 failure(s) in 1.0h" in lines
    assert "  transient     1 failure(s) in 0.5h" in lines


def test_json_report_keeps_the_public_nested_shape(db):
    report = build(db, _seed_evidence_report(db))

    decoded = json.loads(to_json(report))

    assert decoded == report
    assert set(decoded) == {
        "session",
        "status",
        "cpu",
        "bios",
        "created_at",
        "updated_at",
        "context",
        "unattributed_crashes",
        "endurance_round",
        "cores",
        "regime_yield",
        "total_stress_hours",
    }
    assert set(decoded["cores"][0]) == {
        "core",
        "offset",
        "current",
        "phase",
        "hours",
        "confidence_hours",
        "anneal_strikes",
        "suspicion",
        "stress_hours",
        "crashes",
        "failures",
    }
