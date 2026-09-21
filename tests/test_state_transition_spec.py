"""Executable contract for docs/tuner-state-spec.md."""

from __future__ import annotations

from itertools import product
from unittest.mock import MagicMock

from corecycler.history.db import HistoryDB
from corecycler.tuner.config import TunerConfig
from corecycler.tuner.engine import TunerEngine
from corecycler.tuner.state import CoreState, TunerPhase

P = TunerPhase

# This is the verdict table in docs/tuner-state-spec.md.
ADVANCE_RELATION: dict[tuple[TunerPhase, bool], set[TunerPhase]] = {
    (P.NOT_STARTED, True): {P.COARSE_SEARCH},
    (P.NOT_STARTED, False): {P.COARSE_SEARCH},
    (P.COARSE_SEARCH, True): {P.COARSE_SEARCH, P.SETTLED},
    (P.COARSE_SEARCH, False): {P.FINE_SEARCH, P.SETTLED, P.BACKOFF_PRECONFIRM},
    (P.FINE_SEARCH, True): {P.FINE_SEARCH, P.SETTLED},
    (P.FINE_SEARCH, False): {P.SETTLED},
    (P.SETTLED, True): {P.CONFIRMING},
    (P.SETTLED, False): {P.CONFIRMING},
    (P.CONFIRMING, True): {P.CONFIRMED},
    (P.CONFIRMING, False): {P.CONFIRMING, P.FAILED_CONFIRM},
    (P.CONFIRMED, True): {P.CONFIRMED},
    (P.CONFIRMED, False): {P.CONFIRMED},
    (P.FAILED_CONFIRM, True): {P.BACKOFF_PRECONFIRM},
    (P.FAILED_CONFIRM, False): {P.BACKOFF_PRECONFIRM},
    (P.BACKOFF_PRECONFIRM, True): {P.BACKOFF_PRECONFIRM, P.BACKOFF_CONFIRMING},
    (P.BACKOFF_PRECONFIRM, False): {P.BACKOFF_PRECONFIRM, P.BACKOFF_CONFIRMING},
    (P.BACKOFF_CONFIRMING, True): {P.CONFIRMED, P.BACKOFF_PRECONFIRM},
    (P.BACKOFF_CONFIRMING, False): {P.BACKOFF_PRECONFIRM, P.BACKOFF_CONFIRMING},
    (P.ANNEALING, True): {P.CONFIRMED},
    (P.ANNEALING, False): {P.CONFIRMED},
}

# This is the hard-crash table in docs/tuner-state-spec.md.
CRASH_RELATION: dict[TunerPhase, TunerPhase] = {
    P.COARSE_SEARCH: P.BACKOFF_PRECONFIRM,
    P.FINE_SEARCH: P.BACKOFF_PRECONFIRM,
    P.CONFIRMING: P.BACKOFF_PRECONFIRM,
    P.CONFIRMED: P.BACKOFF_PRECONFIRM,
    P.BACKOFF_PRECONFIRM: P.BACKOFF_PRECONFIRM,
    P.NOT_STARTED: P.NOT_STARTED,
    P.SETTLED: P.SETTLED,
    P.FAILED_CONFIRM: P.FAILED_CONFIRM,
    P.BACKOFF_CONFIRMING: P.BACKOFF_CONFIRMING,
    P.ANNEALING: P.ANNEALING,
}

MAX_OFFSET = -30
BASELINES = (0, -10)
CURRENTS = (MAX_OFFSET, -20, -5, 0)
BESTS = (None, -18, -4)
FAIL_BOUNDS = (None, -22)
PASS_BOUNDS = (None, -4)


def make_engine(db: HistoryDB, **config_overrides) -> TunerEngine:
    from corecycler.engine.topology import CPUTopology, PhysicalCore

    topo = CPUTopology()
    topo.cores[0] = PhysicalCore(core_id=0, ccd=0, ccx=None, logical_cpus=(0,))
    topo.ccds = 1
    cfg = TunerConfig(
        cores_to_test=[0],
        coarse_step=5,
        fine_step=1,
        max_offset=MAX_OFFSET,
        max_confirm_retries=2,
        midpoint_jump_threshold=3,
        **config_overrides,
    )
    return TunerEngine(db=db, topology=topo, smu=None, backend=MagicMock(), config=cfg)


def crash_scenarios():
    yield from product(list(TunerPhase), (True, False), BASELINES, CURRENTS, BESTS, FAIL_BOUNDS)


def advance_scenarios():
    """Include every branch-driving counter and bound used by _advance_core."""
    yield from product(
        list(TunerPhase),
        (True, False),
        BASELINES,
        CURRENTS,
        BESTS,
        FAIL_BOUNDS,
        PASS_BOUNDS,
        (0, 1),
        (0, 3),
    )


def check_invariants(eng: TunerEngine, cs: CoreState, label: str) -> None:
    assert not eng._exceeds_max(cs.current_offset), f"{label}: current beyond max"
    if cs.best_offset is not None:
        assert not eng._exceeds_max(cs.best_offset), f"{label}: best beyond max"
    if cs.backoff_pass_bound is not None and cs.backoff_fail_bound is not None:
        assert not eng._is_more_aggressive(cs.backoff_pass_bound, cs.backoff_fail_bound), (
            f"{label}: pass bound more aggressive than fail bound"
        )
    assert cs.confirm_attempts >= 0, label
    assert cs.crash_count >= 0, label
    assert cs.battery_index >= 0, label
    assert cs.anneal_strikes >= 0, label
    assert cs.anneal_bar_hours >= 0, label
    HistoryDB._check_core_state_sane(cs)


def test_documented_relation_covers_every_current_phase_and_outcome():
    assert {phase for phase, _passed in ADVANCE_RELATION} == set(TunerPhase)
    assert set(ADVANCE_RELATION) == set(product(TunerPhase, (True, False)))
    assert set(CRASH_RELATION) == set(TunerPhase)


def test_every_verdict_transition_is_declared():
    """Drive the real transition function over every phase/outcome scenario."""
    db = HistoryDB(":memory:")
    try:
        eng = make_engine(db)
        observed: dict[tuple[TunerPhase, bool], set[TunerPhase]] = {key: set() for key in ADVANCE_RELATION}
        for (
            phase,
            passed,
            baseline,
            current,
            best,
            fail_bound,
            pass_bound,
            confirm_attempts,
            consecutive_backoff_fails,
        ) in advance_scenarios():
            cs = CoreState(
                core_id=0,
                phase=phase,
                current_offset=current,
                best_offset=best,
                baseline_offset=baseline,
                backoff_fail_bound=fail_bound,
                backoff_pass_bound=(
                    pass_bound
                    if phase in (P.BACKOFF_PRECONFIRM, P.BACKOFF_CONFIRMING) and fail_bound is not None
                    else None
                ),
                backoff_mode=phase in (P.BACKOFF_PRECONFIRM, P.BACKOFF_CONFIRMING),
                confirm_attempts=confirm_attempts,
                consecutive_backoff_fails=consecutive_backoff_fails,
                anneal_bar_hours=6.0,
            )
            eng._core_states = {0: cs}
            label = (
                f"{phase}/{'pass' if passed else 'fail'} "
                f"baseline={baseline} current={current} best={best} fail_bound={fail_bound}"
            )

            eng._advance_core(0, passed)

            assert cs.phase in ADVANCE_RELATION[(phase, passed)], (
                f"UNDECLARED TRANSITION {label}: {phase} -> {cs.phase}; "
                f"allowed={sorted(p.value for p in ADVANCE_RELATION[(phase, passed)])}"
            )
            check_invariants(eng, cs, label)
            observed[(phase, passed)].add(cs.phase)
        assert observed == ADVANCE_RELATION
    finally:
        db.close()


def test_regime_sets_are_coarse_subset_then_full_battery():
    db = HistoryDB(":memory:")
    try:
        eng = make_engine(db)
        cs = CoreState(core_id=0, phase=P.COARSE_SEARCH)
        coarse = eng._slot_regimes(cs)
        cs.phase = P.FINE_SEARCH
        full = eng._slot_regimes(cs)

        configured_coarse = set(eng._config.coarse_regimes)
        configured_battery = {str(entry["regime"]) for entry in eng._config.battery}
        assert set(coarse) == configured_coarse & configured_battery
        assert set(full) == configured_battery
        assert set(coarse) < set(full)
    finally:
        db.close()


def test_battery_passes_retest_same_offset_until_final_regime(monkeypatch):
    db = HistoryDB(":memory:")
    try:
        eng = make_engine(db)
        cs = CoreState(
            core_id=0,
            phase=P.FINE_SEARCH,
            current_offset=-5,
            best_offset=-4,
            coarse_fail_offset=-20,
            in_test=True,
        )
        eng._core_states = {0: cs}
        queued = []
        monkeypatch.setattr("corecycler.tuner.engine.QTimer.singleShot", lambda _ms, fn: queued.append(fn))
        monkeypatch.setattr(eng, "_revert_core_to_baseline", lambda _core_id: True)

        regimes = eng._slot_regimes(cs)
        assert len(regimes) > 1
        first_entry = eng._battery_entry(cs)
        original_offset = cs.current_offset
        eng._on_test_finished(0, True, "", "", 1.0, 0.0)

        assert str(first_entry["regime"]) == regimes[0]
        assert cs.phase is P.FINE_SEARCH
        assert cs.current_offset == original_offset
        assert cs.battery_index == 1
        assert str(eng._battery_entry(cs)["regime"]) == regimes[1]
        assert len(queued) == 1

        cs.battery_index = len(regimes) - 1
        cs.in_test = True
        eng._on_test_finished(0, True, "", "", 1.0, 0.0)

        assert cs.battery_index == 0
        assert cs.best_offset == original_offset
        assert cs.current_offset == original_offset - eng._config.fine_step
    finally:
        db.close()


def test_battery_failure_ends_slot_immediately(monkeypatch):
    db = HistoryDB(":memory:")
    try:
        eng = make_engine(db)
        cs = CoreState(
            core_id=0,
            phase=P.FINE_SEARCH,
            current_offset=-5,
            best_offset=-4,
            coarse_fail_offset=-20,
            battery_index=1,
            in_test=True,
        )
        eng._core_states = {0: cs}
        queued = []
        monkeypatch.setattr("corecycler.tuner.engine.QTimer.singleShot", lambda _ms, fn: queued.append(fn))
        monkeypatch.setattr(eng, "_revert_core_to_baseline", lambda _core_id: True)

        eng._on_test_finished(0, False, "unstable", "mce", 1.0, 0.0)

        assert cs.battery_index == 0
        assert cs.phase is P.SETTLED
        assert len(queued) == 1
    finally:
        db.close()


def test_annealing_pass_promotes_and_resets_bar():
    db = HistoryDB(":memory:")
    try:
        eng = make_engine(db, anneal_bank_hours=6.0)
        cs = CoreState(
            core_id=0,
            phase=P.ANNEALING,
            current_offset=-11,
            best_offset=-10,
            anneal_strikes=2,
            anneal_bar_hours=24.0,
        )
        eng._core_states = {0: cs}

        eng._advance_core(0, True)

        assert cs.phase is P.CONFIRMED
        assert cs.current_offset == cs.best_offset == -11
        assert cs.anneal_strikes == 0
        assert cs.anneal_bar_hours == 6.0
    finally:
        db.close()


def test_annealing_failure_restores_best_and_doubles_bar():
    db = HistoryDB(":memory:")
    try:
        eng = make_engine(db, anneal_bank_hours=6.0)
        cs = CoreState(
            core_id=0,
            phase=P.ANNEALING,
            current_offset=-11,
            best_offset=-10,
            anneal_strikes=1,
            anneal_bar_hours=12.0,
        )
        eng._core_states = {0: cs}

        eng._advance_core(0, False)

        assert cs.phase is P.CONFIRMED
        assert cs.current_offset == cs.best_offset == -10
        assert cs.anneal_strikes == 2
        assert cs.anneal_bar_hours == 24.0
    finally:
        db.close()


def test_every_crash_penalty_transition_is_declared():
    db = HistoryDB(":memory:")
    try:
        eng = make_engine(db)
        covered: set[TunerPhase] = set()
        for phase, _passed, baseline, current, best, fail_bound in crash_scenarios():
            cs = CoreState(
                core_id=0,
                phase=phase,
                current_offset=current,
                best_offset=best,
                baseline_offset=baseline,
                backoff_fail_bound=fail_bound,
            )
            eng._core_states = {0: cs}
            label = f"crash@{phase} baseline={baseline} current={current} best={best} fail_bound={fail_bound}"

            eng._apply_crash_penalty(cs)

            if current == 0:
                assert cs.phase is phase, label
                assert eng.status == "paused", label
                assert cs.best_offset == best, label
                assert cs.backoff_fail_bound == fail_bound, label
                assert cs.crash_count == 1 and cs.crash_cooldown == 0, label
                assert cs.current_offset == 0, label
                check_invariants(eng, cs, label)
                continue
            assert cs.phase is CRASH_RELATION[phase], f"UNDECLARED CRASH TRANSITION {label}: {phase} -> {cs.phase}"
            assert cs.best_offset is not None, f"{label}: best is None after crash"
            assert cs.crash_count >= 1 and cs.crash_cooldown >= 1, label
            assert cs.backoff_fail_bound is not None, label
            assert cs.current_offset <= 0, f"{label}: penalty crossed stock"
            assert not eng._is_more_aggressive(cs.best_offset, cs.current_offset), (
                f"{label}: best={cs.best_offset} more aggressive than current={cs.current_offset}"
            )
            check_invariants(eng, cs, label)
            covered.add(phase)
        assert covered == set(CRASH_RELATION)
    finally:
        db.close()
