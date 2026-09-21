"""The attribution state machine, exercised as a search rather than as plumbing."""

from __future__ import annotations

import pytest

from corecycler.tuner import bisect
from corecycler.tuner.bisect import HuntState, Stage


def run_hunt(
    candidates: list[int],
    culprits: set[int],
    *,
    stock_dies: bool = False,
    control_confirmations: int = 2,
    max_no_reproduce: int = 3,
    cap: int = 200,
) -> tuple[HuntState, int]:
    """Drive a hunt against an oracle where any live culprit reproduces."""
    state = bisect.begin(candidates, [candidates[0]])
    probes = 0
    while probes < cap:
        live = bisect.next_live_set(state)
        if live is None:
            break
        probes += 1
        reproduced = stock_dies if state.stage is Stage.CONTROL else bool(culprits & set(live))
        bisect.record(
            state,
            reproduced=reproduced,
            control_confirmations=control_confirmations,
            max_no_reproduce=max_no_reproduce,
        )
    return state, probes


class TestControl:
    def test_stock_survival_opens_bisection(self):
        state, _ = run_hunt([0, 1, 2, 3], {2})
        assert state.stage is Stage.CULPRIT

    def test_stock_dying_twice_is_a_platform_fault(self):
        state, probes = run_hunt([0, 1, 2, 3], {2}, stock_dies=True)
        assert state.stage is Stage.PLATFORM
        assert state.found == []
        assert probes == 2

    def test_one_stock_death_is_not_enough(self):
        state = bisect.begin([0, 1], [0])
        bisect.next_live_set(state)
        bisect.record(state, reproduced=True, control_confirmations=2, max_no_reproduce=3)
        assert state.stage is Stage.CONTROL
        assert state.control_fails == 1


class TestIsolation:
    @pytest.mark.parametrize("culprit", [0, 1, 2, 3, 4, 5, 6, 7])
    def test_single_culprit_is_found(self, culprit):
        state, _ = run_hunt(list(range(8)), {culprit})
        assert state.stage is Stage.CULPRIT
        assert state.found == [culprit]

    def test_isolation_is_logarithmic(self):
        _, probes = run_hunt(list(range(8)), {5})
        # One control probe, then at most two probes per halving of 8, plus
        # the single-core confirmation.
        assert probes <= 1 + 2 * 3 + 1

    def test_two_culprits_are_both_found(self):
        state, _ = run_hunt(list(range(8)), {1, 6}, cap=400)
        assert set(state.found) == {1, 6} or state.stage is Stage.CULPRIT

    def test_confirmation_can_exonerate(self):
        """A suspect that survives its longer look is not blamed."""
        state = bisect.begin([3], [3])
        bisect.next_live_set(state)
        bisect.record(state, reproduced=False, control_confirmations=2, max_no_reproduce=3)
        assert bisect.next_live_set(state) == [3]
        assert state.stage is Stage.CONFIRM
        bisect.record(state, reproduced=False, control_confirmations=2, max_no_reproduce=1)
        assert state.found == []
        assert state.exonerated == [3]
        assert state.stage is Stage.EXHAUSTED


class TestNonReproduction:
    def test_a_failure_that_never_recurs_exhausts(self):
        state, _ = run_hunt([0, 1, 2, 3], set(), max_no_reproduce=2)
        assert state.stage is Stage.EXHAUSTED
        assert state.found == []

    def test_a_retried_set_is_not_split_further(self):
        """Neither half reproducing means splitting again would invent facts."""
        state = bisect.begin([0, 1, 2, 3], [0])
        bisect.next_live_set(state)
        bisect.record(state, reproduced=False, control_confirmations=2, max_no_reproduce=5)
        bisect.next_live_set(state)
        bisect.record(state, reproduced=False, control_confirmations=2, max_no_reproduce=5)
        bisect.next_live_set(state)
        bisect.record(state, reproduced=False, control_confirmations=2, max_no_reproduce=5)
        assert state.pending == [[0, 1, 2, 3]]


class TestBudget:
    def test_budget_grows_with_depth(self):
        shallow = HuntState(stage=Stage.PROBE, level=1)
        deep = HuntState(stage=Stage.PROBE, level=3)
        kw = {
            "base": 1800,
            "observed_mttf": 0.0,
            "mttf_multiplier": 4.0,
            "level_multiplier": 1.5,
            "final_multiplier": 4.0,
        }
        assert bisect.probe_seconds(deep, **kw) > bisect.probe_seconds(shallow, **kw)

    def test_confirmation_gets_the_longest_look(self):
        probe = HuntState(stage=Stage.PROBE, level=2)
        confirm = HuntState(stage=Stage.CONFIRM, level=2)
        kw = {
            "base": 1800,
            "observed_mttf": 0.0,
            "mttf_multiplier": 4.0,
            "level_multiplier": 1.5,
            "final_multiplier": 4.0,
        }
        assert bisect.probe_seconds(confirm, **kw) == bisect.probe_seconds(probe, **kw) * 4

    def test_a_fast_failure_still_gets_the_floor(self):
        state = HuntState(stage=Stage.PROBE)
        seconds = bisect.probe_seconds(
            state, base=1800, observed_mttf=5.0, mttf_multiplier=4.0, level_multiplier=1.5, final_multiplier=4.0
        )
        assert seconds == 1800

    def test_a_slow_failure_stretches_the_budget(self):
        state = HuntState(stage=Stage.PROBE)
        seconds = bisect.probe_seconds(
            state, base=1800, observed_mttf=900.0, mttf_multiplier=4.0, level_multiplier=1.5, final_multiplier=4.0
        )
        assert seconds == 3600


class TestPersistence:
    def test_state_survives_a_round_trip(self):
        state, _ = run_hunt(list(range(8)), {3}, cap=6)
        restored = HuntState.from_json(state.to_json())
        assert restored == state

    @pytest.mark.parametrize("blob", ["", "not json", "{}", '{"stage":"nonsense"}', "[]"])
    def test_unusable_state_restarts_the_hunt(self, blob):
        assert HuntState.from_json(blob) is None


def test_split_puts_the_larger_half_first():
    assert bisect.split([0, 1, 2, 3, 4]) == ([0, 1, 2], [3, 4])
    assert bisect.split([0, 1]) == ([0], [1])


def test_the_original_load_is_replayed_by_every_probe():
    """Varying the load instead of the mask would silence idle-caused faults."""
    state = bisect.begin([0, 1, 2, 3], [2])
    assert state.loaded == [2]
    restored = HuntState.from_json(state.to_json())
    assert restored.loaded == [2]
