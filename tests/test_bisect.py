"""The attribution state machine, exercised as a search rather than as plumbing."""

from __future__ import annotations

import json
import re

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from corecycler.tuner import bisect
from corecycler.tuner.bisect import HuntState, InvalidHuntState, Stage


def run_hunt(
    candidates: list[int],
    culprits: set[int],
    *,
    together: set[int] | None = None,
    max_no_reproduce: int = 3,
    cap: int = 200,
) -> tuple[HuntState, int]:
    """Drive a hunt against an oracle where any live culprit reproduces.

    ``together`` is a set that fails only when every member is live at once.
    """
    state = bisect.begin(candidates, [candidates[0]])
    probes = 0
    while probes < cap:
        live = bisect.next_live_set(state)
        if live is None:
            break
        probes += 1
        live_set = set(live)
        reproduced = bool(culprits & live_set) or (together is not None and together <= live_set)
        bisect.record(
            state,
            reproduced=reproduced,
            max_no_reproduce=max_no_reproduce,
        )
    return state, probes


def _state_at(stage: Stage) -> HuntState:
    state = bisect.begin([0, 1, 2, 3], [0])
    if stage is Stage.CULPRIT:
        state.pending = [[0]]
        bisect.next_live_set(state)
    elif stage is Stage.EXHAUSTED:
        state.pending = []
        bisect.next_live_set(state)
    return state


class TestCrashContext:
    def test_the_first_probe_is_the_larger_half_of_the_live_set(self):
        state = bisect.begin([0, 1, 2, 3, 4], [4])
        assert bisect.next_live_set(state) == [0, 1, 2]

    def test_a_hunt_that_has_asked_nothing_is_only_crash_context(self):
        state = bisect.begin([0, 1, 2, 3], [3])
        assert state.started is False
        bisect.next_live_set(state)
        assert state.started is True

    def test_a_hunt_back_on_its_whole_set_after_clean_halves_has_started(self):
        state = bisect.begin([0, 1], [0])
        for _ in range(2):
            bisect.next_live_set(state)
            bisect.record(state, reproduced=False, max_no_reproduce=5)
        assert state.pending == [[0, 1]]
        assert state.started is True


class TestIsolation:
    @pytest.mark.parametrize("culprit", [0, 1, 2, 3, 4, 5, 6, 7])
    def test_single_culprit_is_found(self, culprit):
        state, _ = run_hunt(list(range(8)), {culprit})
        assert state.stage is Stage.CULPRIT
        assert state.found == [culprit]

    def test_isolation_is_logarithmic(self):
        _, probes = run_hunt(list(range(8)), {5})
        # At most two probes per halving of 8.
        assert probes <= 2 * 3

    def test_two_culprits_are_both_found(self):
        state, _ = run_hunt(list(range(8)), {1, 7}, cap=400)
        assert state.stage is Stage.CULPRIT
        assert state.found == [1, 7]
        assert state.pending == []

    def test_exhaustion_preserves_an_already_confirmed_culprit(self):
        state = bisect.begin([0, 1, 2], [0])
        state.stage = Stage.PROBE
        state.found = [0]
        state.pending = [[1, 2]]

        assert bisect.next_live_set(state) == [1]
        bisect.record(state, reproduced=False, max_no_reproduce=1)
        assert bisect.next_live_set(state) == [2]
        bisect.record(state, reproduced=False, max_no_reproduce=1)
        assert bisect.next_live_set(state) == [1, 2]
        bisect.record(state, reproduced=False, max_no_reproduce=1)

        assert state.stage is Stage.CULPRIT
        assert state.found == [0]

    def test_a_lone_live_candidate_is_the_culprit_without_a_probe(self):
        state = bisect.begin([3], [3])

        assert bisect.next_live_set(state) is None
        assert state.stage is Stage.CULPRIT
        assert state.found == [3]

    def test_a_core_that_reproduced_alone_is_convicted_without_a_leave_one_out_probe(self):
        """Session 12: core 3 killed the machine with every other core at stock,
        then the hunt queued 131 launches of the other three without it."""
        state = bisect.begin([0, 1, 2, 3], [3])
        probes = []
        while (live := bisect.next_live_set(state)) is not None and len(probes) < 10:
            probes.append(live)
            bisect.record(state, reproduced=3 in live, max_no_reproduce=1)

        assert probes == [[0, 1], [2, 3], [2], [3]]
        assert state.stage is Stage.CULPRIT
        assert state.found == [3]

    def test_an_unanswered_probe_is_replayed_before_the_queue_moves_on(self):
        """A shutdown mid-probe must not skip to the other half, as the 09:26 pause skipped [0, 1]."""
        state = bisect.begin([0, 1, 2, 3], [3])
        first = bisect.next_live_set(state)

        restored = HuntState.from_json(state.to_json())

        assert restored is not None
        assert bisect.next_live_set(restored) == first

    @given(st.integers(min_value=1, max_value=32), st.integers(min_value=0, max_value=31))
    @settings(max_examples=64, deadline=None)
    def test_every_single_culprit_hunt_terminates_with_that_culprit(self, count, raw_culprit):
        culprit = raw_culprit % count
        state, probes = run_hunt(list(range(count)), {culprit}, cap=500)
        assert state.stage is Stage.CULPRIT
        assert state.found == [culprit]
        assert probes <= 4 * count + 2

    @given(st.integers(min_value=2, max_value=32))
    @settings(max_examples=32, deadline=None)
    def test_every_clean_hunt_terminates(self, count):
        state, probes = run_hunt(list(range(count)), set(), max_no_reproduce=2, cap=500)
        assert state.stage is Stage.EXHAUSTED
        assert probes <= 4 * count + 2


class TestNonReproduction:
    def test_a_failure_that_never_recurs_exhausts(self):
        state, _ = run_hunt([0, 1, 2, 3], set(), max_no_reproduce=2)
        assert state.stage is Stage.EXHAUSTED
        assert state.found == []

    def test_a_retried_set_is_not_split_further(self):
        """Neither half reproducing means splitting again would invent facts."""
        state = bisect.begin([0, 1, 2, 3], [0])
        bisect.next_live_set(state)
        bisect.record(state, reproduced=False, max_no_reproduce=5)
        bisect.next_live_set(state)
        bisect.record(state, reproduced=False, max_no_reproduce=5)
        assert state.pending == [[0, 1, 2, 3]]


class TestConjunction:
    def test_a_pair_that_fails_only_together_is_found(self):
        """Session 12: [2, 3] froze 5/5, [2] and [3] alone never did."""
        state, _ = run_hunt([0, 1, 2, 3], set(), together={2, 3}, max_no_reproduce=2)
        assert state.stage is Stage.CULPRIT
        assert state.found == [2, 3]

    def test_a_conjunction_spanning_the_root_halves_names_the_whole_set(self):
        state, _ = run_hunt([0, 1, 2, 3], set(), together={1, 2}, max_no_reproduce=2)
        assert state.stage is Stage.CULPRIT
        assert state.found == [0, 1, 2, 3]

    def test_the_whole_set_is_rechecked_only_once_halves_are_out_of_retries(self):
        state = bisect.begin([0, 1], [0])
        seen = []
        while (live := bisect.next_live_set(state)) is not None:
            seen.append(live)
            bisect.record(state, reproduced=live == [0, 1], max_no_reproduce=2)
        assert seen == [[0], [1], [0], [1], [0, 1]]
        assert state.found == [0, 1]

    def test_a_whole_set_that_no_longer_reproduces_exhausts(self):
        state = bisect.begin([0, 1], [0])
        assert bisect.next_live_set(state) == [0]
        bisect.record(state, reproduced=False, max_no_reproduce=1)
        assert bisect.next_live_set(state) == [1]
        bisect.record(state, reproduced=False, max_no_reproduce=1)
        assert bisect.next_live_set(state) == [0, 1]
        restored = HuntState.from_json(state.to_json())
        assert restored is not None and restored.in_flight == [0, 1]
        bisect.record(state, reproduced=False, max_no_reproduce=1)
        assert state.stage is Stage.EXHAUSTED
        assert state.found == []

    def test_a_conjunction_does_not_end_a_hunt_with_other_guilty_sets_pending(self):
        state, _ = run_hunt(list(range(8)), {7}, together={0, 1}, max_no_reproduce=2, cap=400)
        assert state.stage is Stage.CULPRIT
        assert state.found == [0, 1, 7]


class TestBudget:
    def test_budget_grows_with_depth(self):
        shallow = HuntState(stage=Stage.PROBE, level=1, observed_failure_time=0.0)
        deep = HuntState(stage=Stage.PROBE, level=3, observed_failure_time=0.0)
        kw = {
            "base": 1800,
            "mttf_multiplier": 4.0,
            "level_multiplier": 1.5,
        }
        assert bisect.probe_seconds(deep, **kw) > bisect.probe_seconds(shallow, **kw)

    def test_a_fast_failure_still_gets_the_floor(self):
        state = HuntState(stage=Stage.PROBE, observed_failure_time=5.0)
        seconds = bisect.probe_seconds(state, base=1800, mttf_multiplier=4.0, level_multiplier=1.5)
        assert seconds == 1800

    def test_a_slow_failure_stretches_the_budget(self):
        state = HuntState(stage=Stage.PROBE, observed_failure_time=900.0)
        seconds = bisect.probe_seconds(state, base=1800, mttf_multiplier=4.0, level_multiplier=1.5)
        assert seconds == 3600


class TestOnsetLaunches:
    """A failure that lands seconds after load starts is reproduced by load
    starts, not by wall time: the budget is spent as many short launches."""

    KW = {"onset_seconds": 60, "min_launch": 30, "mttf_multiplier": 4.0}

    @pytest.mark.parametrize("observed", [0.0, 61.0, 900.0])
    def test_an_untimed_or_slow_failure_keeps_one_long_launch(self, observed):
        state = HuntState(stage=Stage.PROBE, observed_failure_time=observed)
        assert bisect.onset_launches(state, budget=556, **self.KW) == (556, 1)

    def test_an_onset_failure_spends_the_budget_as_launches(self):
        state = HuntState(stage=Stage.PROBE, observed_failure_time=8.0)
        launch, count = bisect.onset_launches(state, budget=556, **self.KW)
        assert launch == 32
        assert count == 18
        assert launch * count >= 556

    def test_a_very_fast_failure_still_gets_the_minimum_launch(self):
        state = HuntState(stage=Stage.PROBE, observed_failure_time=1.0)
        assert bisect.onset_launches(state, budget=300, **self.KW) == (30, 10)

    def test_a_launch_never_outlasts_the_budget(self):
        state = HuntState(stage=Stage.PROBE, observed_failure_time=8.0)
        assert bisect.onset_launches(state, budget=20, **self.KW) == (20, 1)

    def test_a_zero_threshold_disables_onset_probes(self):
        state = HuntState(stage=Stage.PROBE, observed_failure_time=8.0)
        kw = {**self.KW, "onset_seconds": 0}
        assert bisect.onset_launches(state, budget=556, **kw) == (556, 1)


class TestPersistence:
    def test_state_survives_a_round_trip(self):
        state, _ = run_hunt(list(range(8)), {3}, cap=6)
        restored = HuntState.from_json(state.to_json())
        assert restored == state

    def test_armed_probe_evidence_survives_a_round_trip(self):
        state = bisect.begin([0, 1], [0])
        bisect.next_live_set(state)
        state.armed = True
        state.vector = {0: -20, 1: 0}
        state.workload = {
            "regime": "current",
            "backend": "mprime",
            "stress_mode": "AVX2",
            "fft_preset": "SMALL",
            "threads": 2,
        }

        restored = HuntState.from_json(state.to_json())

        assert restored == state
        assert restored.observed_failure_time == 0.0

    def test_observed_failure_time_survives_a_round_trip(self):
        state = bisect.begin([0, 1], [0], observed_failure_time=123.5)

        restored = HuntState.from_json(state.to_json())

        assert restored is not None
        assert restored.observed_failure_time == 123.5

    def test_empty_state_is_absent(self):
        assert HuntState.from_json("") is None

    def test_state_must_be_a_string(self):
        with pytest.raises(InvalidHuntState, match="state must be a string"):
            HuntState.from_json(None)

    @pytest.mark.parametrize("blob", [" ", "not json", "{", '{"stage":"nonsense"}', "[]"])
    def test_nonempty_unusable_state_is_explicitly_invalid(self, blob):
        with pytest.raises(InvalidHuntState):
            HuntState.from_json(blob)

    @pytest.mark.parametrize(
        "changes",
        [
            {"version": 1},
            {"level": float("nan")},
            {"no_reproduce": float("inf")},
            {"observed_failure_time": -1},
            {"observed_failure_time": True},
            {"observed_failure_time": "slow"},
            {"pending": {}},
            {"in_flight": {}},
            {"pending": [0]},
            {"pending": [[]]},
            {"pending": [[1, 0]]},
            {"pending": [[0, 0]]},
            {"pending": [[0], [0]]},
            {"loaded": [-1]},
            {"loaded": [True]},
            {"loaded": [0, 0]},
            {"loaded": [1, 0]},
            {"vector": []},
            {"vector": {"core": -1}},
            {"vector": {"00": -1}},
            {"vector": {str(2**31): -1}},
            {"vector": {"0": 2**31}},
            {"vector": {"0": True}},
            {"armed": 1},
            {"armed": True},
            {"workload": []},
            {"pending": [[0, 1], [1]]},
            {"launches_done": -1},
            {"level": 1},
        ],
    )
    def test_corrupt_fields_are_rejected(self, changes):
        raw = json.loads(bisect.begin([0, 1], [0, 1]).to_json())
        raw.update(changes)

        with pytest.raises(InvalidHuntState):
            HuntState.from_json(json.dumps(raw))

    @pytest.mark.parametrize(
        ("stage", "changes", "message"),
        [
            (Stage.PROBE, {"pending": [], "queue": [[0, 1], [1, 2]]}, "queued sets must be disjoint"),
            (Stage.PROBE, {"pending": [], "guilty_halves": [[0, 1], [1, 2]]}, "guilty sets must be disjoint"),
            (Stage.PROBE, {"pending": [[0, 4]]}, "search sets must be subsets of candidates"),
            (Stage.PROBE, {"found": [4]}, "resolved search sets must be subsets of candidates"),
            (
                Stage.PROBE,
                {"armed": True, "vector": {"0": -1}},
                "an armed probe requires its exact vector and live set",
            ),
            (Stage.PROBE, {"pending": [], "queue": [[0], [1], [2]]}, "probe stage has too many split sets"),
            (Stage.PROBE, {"pending": [], "parent": [0], "in_flight": [0]}, "probe parent and level are inconsistent"),
            (
                Stage.PROBE,
                {"pending": [], "parent": [0, 1], "in_flight": [2], "level": 1},
                "split sets must be subsets of their parent",
            ),
            (
                Stage.PROBE,
                {"pending": [], "parent": [0, 1, 2, 3], "in_flight": [0, 1], "queue": [[1, 2]], "level": 1},
                "active split sets must be disjoint",
            ),
            (
                Stage.PROBE,
                {"pending": [[0]], "parent": [0, 1], "level": 1},
                "pending sets must be disjoint from the active parent",
            ),
            (
                Stage.PROBE,
                {"pending": [], "parent": [0, 1, 2, 3], "in_flight": [], "queue": [[0], [2, 3]], "level": 1},
                "a fully queued split must partition its parent",
            ),
            (Stage.PROBE, {"pending": [], "in_flight": [0]}, "probe progress requires a parent set"),
            (Stage.PROBE, {"pending": []}, "probe stage has no remaining work"),
            (Stage.CULPRIT, {"found": []}, "culprit stage requires only confirmed culprits"),
            (Stage.CULPRIT, {"launches_done": 2}, "launch progress requires an unanswered probe"),
            (Stage.EXHAUSTED, {"pending": [[0]]}, "exhausted stage has unresolved work"),
        ],
    )
    def test_each_persisted_state_invariant_reports_its_rejection(self, stage, changes, message):
        raw = json.loads(_state_at(stage).to_json())
        raw.update(changes)

        with pytest.raises(InvalidHuntState, match=re.escape(f"invalid persisted hunt state: {message}")):
            HuntState.from_json(json.dumps(raw))

    def test_negative_observed_failure_time_cannot_be_serialized(self):
        state = bisect.begin([0], [0])
        state.observed_failure_time = -1

        with pytest.raises(InvalidHuntState, match="observed_failure_time"):
            state.to_json()

    def test_active_and_resolved_partition_cannot_overlap(self):
        state = bisect.begin([0, 1, 2, 3], [0])
        assert bisect.next_live_set(state) == [0, 1]
        raw = json.loads(state.to_json())
        raw["found"] = [0]

        with pytest.raises(InvalidHuntState, match="resolved and active"):
            HuntState.from_json(json.dumps(raw))

    def test_split_level_is_bounded_by_candidate_universe(self):
        raw = json.loads(bisect.begin([0, 1], [0]).to_json())
        raw.update(stage="probe", level=3)

        with pytest.raises(InvalidHuntState, match="split depth"):
            HuntState.from_json(json.dumps(raw))

    def test_terminal_states_survive_round_trip(self):
        culprit, _ = run_hunt([0, 1], {1})
        exhausted, _ = run_hunt([0, 1], set(), max_no_reproduce=1)

        for state in (culprit, exhausted):
            assert HuntState.from_json(state.to_json()) == state

    @pytest.mark.parametrize(
        ("changes", "message"),
        [
            ({"armed": 1}, "armed must be a boolean"),
            ({"vector": {2**31: -1}}, "vector must map bounded integer core IDs"),
            ({"workload": []}, "workload[0] must be a dict"),
        ],
    )
    def test_in_memory_corruption_cannot_be_serialised(self, changes, message):
        state = bisect.begin([0, 1], [0, 1])
        for field, value in changes.items():
            setattr(state, field, value)

        with pytest.raises(InvalidHuntState, match=re.escape(message)):
            state.to_json()

    def test_non_string_vector_keys_are_rejected(self):
        with pytest.raises(InvalidHuntState, match="vector core IDs must be strings"):
            bisect._vector({0: -1})

    def test_unknown_in_memory_stage_is_rejected(self):
        state = bisect.begin([0], [0])
        state.stage = "unknown"

        with pytest.raises(InvalidHuntState, match="unknown stage"):
            state.to_json()

    def test_probe_without_pending_work_exhausts(self):
        state = HuntState(stage=Stage.PROBE, loaded=[0])

        assert bisect.next_live_set(state) is None
        assert state.stage is Stage.EXHAUSTED

    def test_fresh_process_resume_matches_uninterrupted_search(self):
        uninterrupted, _ = run_hunt(list(range(8)), {6})
        state = bisect.begin(list(range(8)), [0])
        stages = set()
        requeued = False

        while state.stage not in (Stage.CULPRIT, Stage.EXHAUSTED):
            restored = HuntState.from_json(state.to_json())
            assert restored is not state
            state = restored
            if bisect.next_live_set(state) is None:
                break
            stages.add(state.stage)
            state = HuntState.from_json(state.to_json())

            if state.stage is Stage.PROBE and state.queue and not requeued:
                interrupted = list(state.in_flight)
                state = HuntState.from_json(state.to_json())
                assert bisect.next_live_set(state) == interrupted
                state = HuntState.from_json(state.to_json())
                requeued = True

            reproduced = 6 in state.in_flight
            bisect.record(state, reproduced=reproduced, max_no_reproduce=3)
            state = HuntState.from_json(state.to_json())

        assert requeued
        assert stages == {Stage.PROBE}
        assert state == uninterrupted


def test_split_puts_the_larger_half_first():
    assert bisect.split([0, 1, 2, 3, 4]) == ([0, 1, 2], [3, 4])
    assert bisect.split([0, 1]) == ([0], [1])


def test_the_original_load_is_replayed_by_every_probe():
    """Varying the load instead of the mask would silence idle-caused faults."""
    state = bisect.begin([0, 1, 2, 3], [2])
    assert state.loaded == [2]
    restored = HuntState.from_json(state.to_json())
    assert restored.loaded == [2]
