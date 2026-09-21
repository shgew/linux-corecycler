"""Executable contract for docs/test-order-spec.md."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from corecycler.history.db import HistoryDB
from corecycler.tuner import persistence as tp
from corecycler.tuner.config import TunerConfig
from corecycler.tuner.engine import TunerEngine
from corecycler.tuner.state import CoreState, TunerPhase

ORDERS = ["sequential", "round_robin", "weakest_first", "ccd_alternating", "ccd_round_robin"]
ACTIVE = TunerPhase.COARSE_SEARCH
TERMINAL = TunerPhase.CONFIRMED

# Exact table from docs/test-order-spec.md. CONFIRMED is unavailable, not scored.
PHASE_SCORES = {
    TunerPhase.FINE_SEARCH: 0,
    TunerPhase.FAILED_CONFIRM: 0,
    TunerPhase.BACKOFF_PRECONFIRM: 0,
    TunerPhase.BACKOFF_CONFIRMING: 1,
    TunerPhase.CONFIRMING: 1,
    TunerPhase.COARSE_SEARCH: 2,
    TunerPhase.SETTLED: 3,
    TunerPhase.NOT_STARTED: 4,
    TunerPhase.ANNEALING: 5,
}


@pytest.fixture
def db():
    database = HistoryDB(":memory:")
    yield database
    database.close()


def make_engine(db, topo, order: str, **config_overrides) -> TunerEngine:
    cfg = TunerConfig(
        test_order=order,
        cores_to_test=sorted(topo.cores),
        **config_overrides,
    )
    return TunerEngine(db=db, topology=topo, smu=None, backend=MagicMock(), config=cfg)


def seed(
    eng,
    phases: dict[int, TunerPhase],
    cooldowns: dict[int, int] | None = None,
    crash_counts: dict[int, int] | None = None,
    best_offsets: dict[int, int] | None = None,
) -> None:
    eng._core_states = {
        core: CoreState(
            core_id=core,
            phase=phase,
            crash_cooldown=(cooldowns or {}).get(core, 0),
            crash_count=(crash_counts or {}).get(core, 0),
            best_offset=(best_offsets or {}).get(core),
            current_offset=(best_offsets or {}).get(core, 0),
        )
        for core, phase in phases.items()
    }


def step(eng) -> int | None:
    """Pick once and apply the cursor/cooldown updates performed by _run_next."""
    core = eng._pick_next_core()
    if core is None:
        return None
    eng._decrement_cooldowns(core)
    eng._last_tested_core = core
    info = eng._topology.cores.get(core)
    if info and info.ccd is not None:
        eng._ccd_last_tested[info.ccd] = core
    return core


def ccd_of(eng, core: int) -> int:
    info = eng._topology.cores.get(core)
    return info.ccd if info and info.ccd is not None else 0


@pytest.mark.parametrize("order", ORDERS)
class TestGlobalInvariants:
    @pytest.mark.parametrize("phase", [phase for phase in TunerPhase if phase is not TunerPhase.CONFIRMED])
    def test_every_nonconfirmed_phase_is_available(self, db, topo_dual_ccd_x3d, order, phase):
        eng = make_engine(db, topo_dual_ccd_x3d, order)
        phases = dict.fromkeys(range(8), TERMINAL)
        phases[0] = phase
        seed(eng, phases)
        assert eng._pick_next_core() == 0

    def test_never_picks_confirmed_or_cooling(self, db, topo_dual_ccd_x3d, order):
        eng = make_engine(db, topo_dual_ccd_x3d, order)
        seed(
            eng,
            {0: TERMINAL, 1: TERMINAL, 2: ACTIVE, 3: ACTIVE, 4: ACTIVE, 5: TERMINAL, 6: ACTIVE, 7: ACTIVE},
            cooldowns={2: 2, 6: 1},
        )
        for _ in range(20):
            core = step(eng)
            assert core is not None
            cs = eng._core_states[core]
            assert cs.phase is not TunerPhase.CONFIRMED
            assert cs.crash_cooldown == 0

    def test_all_confirmed_without_annealing_credit_returns_none(self, db, topo_dual_ccd_x3d, order):
        eng = make_engine(db, topo_dual_ccd_x3d, order)
        seed(eng, dict.fromkeys(range(8), TERMINAL))
        assert eng._pick_next_core() is None

    def test_liveness_when_all_ordinary_work_is_cooling(self, db, topo_dual_ccd_x3d, order):
        eng = make_engine(db, topo_dual_ccd_x3d, order)
        seed(
            eng,
            {0: ACTIVE, 1: TERMINAL, 2: ACTIVE, 3: TERMINAL, 4: TERMINAL, 5: TERMINAL, 6: TERMINAL, 7: TERMINAL},
            cooldowns={0: 3, 2: 2},
        )
        assert eng._pick_next_core() is None
        for _ in range(3):
            if eng._pick_next_core() is not None:
                break
            for cs in eng._core_states.values():
                if cs.crash_cooldown > 0:
                    cs.crash_cooldown -= 1
        assert eng._pick_next_core() is not None

    def test_pick_decrements_other_cooldowns(self, db, topo_dual_ccd_x3d, order):
        eng = make_engine(db, topo_dual_ccd_x3d, order)
        seed(eng, dict.fromkeys(range(8), ACTIVE), cooldowns={5: 2})
        picked = step(eng)
        assert picked != 5
        assert eng._core_states[5].crash_cooldown == 1


class TestSequentialSpec:
    def test_lowest_available_and_stays_until_confirmed(self, db, topo_dual_ccd_x3d):
        eng = make_engine(db, topo_dual_ccd_x3d, "sequential")
        seed(eng, dict.fromkeys(range(8), ACTIVE))
        assert step(eng) == 0
        assert step(eng) == 0
        eng._core_states[0].phase = TERMINAL
        assert step(eng) == 1


class TestRoundRobinSpec:
    def test_full_round_visits_each_core_once_cyclically(self, db, topo_dual_ccd_x3d):
        eng = make_engine(db, topo_dual_ccd_x3d, "round_robin")
        seed(eng, dict.fromkeys(range(8), ACTIVE))
        assert [step(eng) for _ in range(8)] == list(range(8))
        assert step(eng) == 0

    def test_terminal_cursor_continues_at_next_position(self, db, topo_dual_ccd_x3d):
        eng = make_engine(db, topo_dual_ccd_x3d, "round_robin")
        seed(eng, dict.fromkeys(range(8), ACTIVE))
        eng._last_tested_core = 3
        eng._core_states[3].phase = TERMINAL
        assert step(eng) == 4


class TestWeakestFirstSpec:
    @pytest.mark.parametrize("phase,expected_score", PHASE_SCORES.items())
    def test_exact_phase_score(self, db, topo_dual_ccd_x3d, phase, expected_score):
        """Two opposite tie-break layouts prove equality with a known score."""
        reference_phase = TunerPhase.FINE_SEARCH if expected_score % 2 == 0 else TunerPhase.CONFIRMING
        reference_base = PHASE_SCORES[reference_phase]
        reference_crashes = (expected_score - reference_base) // 2

        eng = make_engine(db, topo_dual_ccd_x3d, "weakest_first")
        phases = dict.fromkeys(range(8), TERMINAL)
        phases.update({0: phase, 1: reference_phase})
        seed(eng, phases, crash_counts={1: reference_crashes})
        assert eng._pick_next_core() == 0

        eng = make_engine(db, topo_dual_ccd_x3d, "weakest_first")
        phases = dict.fromkeys(range(8), TERMINAL)
        phases.update({0: reference_phase, 1: phase})
        seed(eng, phases, crash_counts={0: reference_crashes})
        assert eng._pick_next_core() == 0

    def test_crash_count_adds_two_points_each(self, db, topo_dual_ccd_x3d):
        eng = make_engine(db, topo_dual_ccd_x3d, "weakest_first")
        phases = dict.fromkeys(range(8), TERMINAL)
        phases.update({0: TunerPhase.FINE_SEARCH, 1: TunerPhase.COARSE_SEARCH})
        seed(eng, phases, crash_counts={0: 2})
        assert eng._pick_next_core() == 1


class TestCcdAlternatingSpec:
    def test_alternates_while_both_ccds_have_work(self, db, topo_dual_ccd_x3d):
        eng = make_engine(db, topo_dual_ccd_x3d, "ccd_alternating")
        seed(eng, dict.fromkeys(range(8), ACTIVE))
        picks = [step(eng) for _ in range(6)]
        ccds = [ccd_of(eng, core) for core in picks]
        assert all(left != right for left, right in zip(ccds, ccds[1:], strict=False))

    def test_without_cursor_prefers_ccd_with_fewer_confirmed_cores(self, db, topo_dual_ccd_x3d):
        eng = make_engine(db, topo_dual_ccd_x3d, "ccd_alternating")
        seed(
            eng,
            {
                0: ACTIVE,
                1: TERMINAL,
                2: TERMINAL,
                3: TERMINAL,
                4: ACTIVE,
                5: ACTIVE,
                6: ACTIVE,
                7: ACTIVE,
            },
        )
        assert step(eng) == 4

    def test_drains_remaining_ccd_when_other_is_done(self, db, topo_dual_ccd_x3d):
        eng = make_engine(db, topo_dual_ccd_x3d, "ccd_alternating")
        seed(
            eng,
            {0: ACTIVE, 1: ACTIVE, 2: TERMINAL, 3: TERMINAL, 4: TERMINAL, 5: TERMINAL, 6: TERMINAL, 7: TERMINAL},
        )
        eng._last_tested_core = 0
        assert ccd_of(eng, step(eng)) == 0


class TestCcdRoundRobinSpec:
    def test_alternates_ccds_and_rotates_within_each(self, db, topo_dual_ccd_x3d):
        eng = make_engine(db, topo_dual_ccd_x3d, "ccd_round_robin")
        seed(eng, dict.fromkeys(range(8), ACTIVE))
        picks = [step(eng) for _ in range(8)]
        ccds = [ccd_of(eng, core) for core in picks]
        assert all(left != right for left, right in zip(ccds, ccds[1:], strict=False))
        assert sorted(picks) == list(range(8))

    def test_single_ccd_degrades_to_round_robin(self, db, topo_single_ccd):
        eng = make_engine(db, topo_single_ccd, "ccd_round_robin")
        cores = sorted(topo_single_ccd.cores)
        seed(eng, dict.fromkeys(cores, ACTIVE))
        assert [step(eng) for _ in cores] == cores


class TestAnnealingFallThrough:
    def test_ordinary_work_precedes_eligible_annealing(self, db, topo_dual_ccd_x3d, monkeypatch):
        eng = make_engine(db, topo_dual_ccd_x3d, "sequential", anneal_bank_hours=1.0)
        phases = dict.fromkeys(range(8), TERMINAL)
        phases[0] = ACTIVE
        seed(eng, phases, best_offsets={3: -10})
        monkeypatch.setattr(eng, "_banked_hours", lambda cs: 10.0 if cs.core_id == 3 else 0.0)

        assert eng._pick_next_core() == 0
        assert eng._core_states[3].phase is TERMINAL

    def test_no_ordinary_work_promotes_eligible_core_to_annealing(self, db, topo_dual_ccd_x3d, monkeypatch):
        eng = make_engine(db, topo_dual_ccd_x3d, "round_robin", anneal_bank_hours=2.0)
        seed(eng, dict.fromkeys(range(8), TERMINAL), best_offsets={3: -10})
        monkeypatch.setattr(eng, "_banked_hours", lambda cs: 2.0 if cs.core_id == 3 else 0.0)

        assert eng._pick_next_core() == 3
        cs = eng._core_states[3]
        assert cs.phase is TunerPhase.ANNEALING
        assert cs.current_offset == -11
        assert cs.battery_index == 0

    def test_strike_limit_stops_further_annealing(self, db, topo_dual_ccd_x3d, monkeypatch):
        eng = make_engine(
            db,
            topo_dual_ccd_x3d,
            "sequential",
            anneal_bank_hours=1.0,
            anneal_max_strikes=3,
        )
        seed(eng, dict.fromkeys(range(8), TERMINAL), best_offsets={3: -10})
        eng._core_states[3].anneal_strikes = 3
        monkeypatch.setattr(eng, "_banked_hours", lambda _cs: 100.0)

        assert eng._pick_next_core() is None
    @pytest.mark.parametrize(
        ("best_offset", "banked_hours"),
        [(-10, 0.5), (-50, 100.0)],
    )
    def test_bank_bar_and_offset_limit_gate_annealing(
        self, db, topo_dual_ccd_x3d, monkeypatch, best_offset, banked_hours
    ):
        eng = make_engine(
            db,
            topo_dual_ccd_x3d,
            "sequential",
            anneal_bank_hours=1.0,
            max_offset=-50,
        )
        seed(eng, dict.fromkeys(range(8), TERMINAL), best_offsets={3: best_offset})
        monkeypatch.setattr(eng, "_banked_hours", lambda _cs: banked_hours)

        assert eng._pick_next_core() is None
class TestInterruptionContract:
    @staticmethod
    def _log_real_test(db, session_id, core, offset=-10):
        tp.log_test_result(db, session_id, core, offset, "coarse", True, duration=60.0)

    def test_cursors_rebuilt_from_real_test_log_only(self, db, topo_dual_ccd_x3d):
        session_id = tp.create_session(db, TunerConfig(), "", "")
        for core in (0, 4, 1):
            self._log_real_test(db, session_id, core)
        tp.log_test_result(db, session_id, 6, -20, "coarse", False, error_type="crash", duration=None)

        eng = make_engine(db, topo_dual_ccd_x3d, "ccd_round_robin")
        seed(eng, dict.fromkeys(range(8), ACTIVE))
        eng._session_id = session_id
        eng._reconstruct_scheduling_position()

        assert eng._last_tested_core == 1
        assert eng._ccd_last_tested == {0: 1, 1: 4}

    def test_round_robin_continues_after_resume(self, db, topo_dual_ccd_x3d):
        session_id = tp.create_session(db, TunerConfig(), "", "")
        for core in (0, 1, 2):
            self._log_real_test(db, session_id, core)
        eng = make_engine(db, topo_dual_ccd_x3d, "round_robin")
        seed(eng, dict.fromkeys(range(8), ACTIVE))
        eng._session_id = session_id
        eng._reconstruct_scheduling_position()
        assert step(eng) == 3

    def test_no_test_log_leaves_cursors_unset(self, db, topo_dual_ccd_x3d):
        session_id = tp.create_session(db, TunerConfig(), "", "")
        eng = make_engine(db, topo_dual_ccd_x3d, "round_robin")
        seed(eng, dict.fromkeys(range(8), ACTIVE))
        eng._session_id = session_id
        eng._reconstruct_scheduling_position()
        assert eng._last_tested_core is None
        assert step(eng) == 0
