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
    with pytest.raises(ValueError):
        build(db, 404)


def test_empty_session_reports_zero_evidence_without_inventing_limits(db):
    session_id = tp.create_session(db, TunerConfig(), "Empty BIOS", "Empty CPU")

    report = build(db, session_id)

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
        db.bank_regime_time(context_id, 0, regime, -32, seconds)

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
    tp.log_test_result(
        db,
        session_id,
        1,
        -20,
        "validation",
        True,
        duration=1800.0,
        regime="current",
    )
    tp.log_test_result(
        db,
        session_id,
        1,
        -20,
        "endurance",
        True,
        duration=7200.0,
        regime="coupled",
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
    assert report["total_stress_hours"] == 5.75
    assert report["cores"] == [
        {
            "core": 0,
            "accepted_offset": -32,
            "candidate_offset": -30,
            "phase": "annealing",
            "hours": {"boost": 2.0, "current": 1.0, "transient": 0.5, "coupled": 3.0},
            "confidence_hours": 0.5,
            "anneal_strikes": 2,
            "suspicion": 1.75,
            "stress_hours": 3.25,
            "crashes": 3,
            "failures": {"worker_exit": 2, "mce": 1, "unknown": 1},
        },
        {
            "core": 1,
            "accepted_offset": None,
            "candidate_offset": -20,
            "phase": "confirmed",
            "hours": {"boost": 0.0, "current": 0.0, "transient": 0.0, "coupled": 0.0},
            "confidence_hours": 0.0,
            "anneal_strikes": 0,
            "suspicion": 0.0,
            "stress_hours": 2.5,
            "crashes": 0,
            "failures": {},
        },
    ]
    assert report["regime_yield"] == {
        "boost": {"failures": 2, "hours": 1.75},
        "coupled": {"failures": 0, "hours": 2.0},
        "current": {"failures": 1, "hours": 1.5},
        "transient": {"failures": 1, "hours": 0.5},
    }


def test_accepted_offsets_are_recommended_for_bios_application(db):
    session_id = tp.create_session(db, TunerConfig(cores_to_test=[0]), "Test BIOS", "Test CPU")
    tp.save_core_state(
        db,
        session_id,
        CoreState(
            core_id=0,
            phase=TunerPhase.CONFIRMED,
            current_offset=-30,
            best_offset=-30,
        ),
    )
    tp.update_session_status(db, session_id, "completed")

    assert "Accepted offsets are volatile SMU overlays; enter them in BIOS to keep them across a reboot." in render(
        build(db, session_id)
    )


def test_unproven_candidate_is_not_recommended_for_bios_application(db):
    report = build(db, _seed_evidence_report(db))

    assert report["cores"][1]["candidate_offset"] == -20
    assert report["cores"][1]["accepted_offset"] is None
    assert not any("enter" in line.lower() and "bios" in line.lower() for line in render(report))


def test_quarantine_marks_every_historical_offset_unsafe(db):
    session_id = _seed_evidence_report(db)
    tp.update_session_status(db, session_id, "quarantined")

    report = build(db, session_id)
    lines = render(report)

    assert [row["candidate_offset"] for row in report["cores"]] == [-30, -20]
    assert all(row["accepted_offset"] is None for row in report["cores"])
    assert any("unsafe" in line.lower() for line in lines)
    assert not any("enter" in line.lower() and "bios" in line.lower() for line in lines)


def test_json_report_keeps_the_public_nested_shape(db):
    report = build(db, _seed_evidence_report(db))

    decoded = json.loads(to_json(report))

    assert decoded == report
    assert [(row["candidate_offset"], row["accepted_offset"]) for row in decoded["cores"]] == [
        (-30, -32),
        (-20, None),
    ]
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
        "accepted_offset",
        "candidate_offset",
        "phase",
        "hours",
        "confidence_hours",
        "anneal_strikes",
        "suspicion",
        "stress_hours",
        "crashes",
        "failures",
    }
