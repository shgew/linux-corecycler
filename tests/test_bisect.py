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


def _state_at(stage: Stage) -> HuntState:
    if stage is Stage.CONTROL:
        return bisect.begin([0, 1, 2, 3], [0])

    state = bisect.begin([0, 1, 2, 3], [0])
    bisect.next_live_set(state)
    bisect.record(state, reproduced=False, control_confirmations=1, max_no_reproduce=1)
    if stage is Stage.PROBE:
        return state
    if stage is Stage.CONFIRM:
        state.pending = [[0]]
        bisect.next_live_set(state)
        return state
    if stage is Stage.CULPRIT:
        state.pending = [[0]]
        bisect.next_live_set(state)
        bisect.record(state, reproduced=False, control_confirmations=1, max_no_reproduce=1)
        return state
    if stage is Stage.EXHAUSTED:
        state.pending = [[0]]
        bisect.next_live_set(state)
        bisect.record(state, reproduced=True, control_confirmations=1, max_no_reproduce=1)
        return state

    state = bisect.begin([0, 1, 2, 3], [0])
    bisect.next_live_set(state)
    bisect.record(state, reproduced=True, control_confirmations=1, max_no_reproduce=1)
    return state


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
        assert state.stage is Stage.CULPRIT
        assert set(state.found) == {1, 6}
        assert state.pending == []

    def test_confirmation_uses_persisted_leave_one_out_mask(self):
        state = bisect.begin([3], [3])
        bisect.next_live_set(state)
        bisect.record(state, reproduced=False, control_confirmations=2, max_no_reproduce=3)
        assert bisect.next_live_set(state) == []
        assert state.stage is Stage.CONFIRM
        assert state.candidates == [3]
        assert state.suspect == 3
        restored = HuntState.from_json(state.to_json())
        assert restored is not None
        assert restored.in_flight == []
        bisect.record(state, reproduced=True, control_confirmations=2, max_no_reproduce=1)
        assert state.found == []
        assert state.exonerated == [3]
        assert state.stage is Stage.EXHAUSTED

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
        bisect.record(state, reproduced=False, control_confirmations=2, max_no_reproduce=5)
        bisect.next_live_set(state)
        bisect.record(state, reproduced=False, control_confirmations=2, max_no_reproduce=5)
        bisect.next_live_set(state)
        bisect.record(state, reproduced=False, control_confirmations=2, max_no_reproduce=5)
        assert state.pending == [[0, 1, 2, 3]]


class TestBudget:
    def test_budget_grows_with_depth(self):
        shallow = HuntState(stage=Stage.PROBE, level=1, observed_failure_time=0.0)
        deep = HuntState(stage=Stage.PROBE, level=3, observed_failure_time=0.0)
        kw = {
            "base": 1800,
            "mttf_multiplier": 4.0,
            "level_multiplier": 1.5,
            "final_multiplier": 4.0,
        }
        assert bisect.probe_seconds(deep, **kw) > bisect.probe_seconds(shallow, **kw)

    def test_confirmation_gets_the_longest_look(self):
        probe = HuntState(stage=Stage.PROBE, level=2, observed_failure_time=900.0)
        confirm = HuntState(stage=Stage.CONFIRM, level=2, observed_failure_time=900.0)
        kw = {
            "base": 1800,
            "mttf_multiplier": 4.0,
            "level_multiplier": 1.5,
            "final_multiplier": 4.0,
        }
        assert bisect.probe_seconds(confirm, **kw) == bisect.probe_seconds(probe, **kw) * 4

    def test_a_fast_failure_still_gets_the_floor(self):
        state = HuntState(stage=Stage.PROBE, observed_failure_time=5.0)
        seconds = bisect.probe_seconds(
            state, base=1800, mttf_multiplier=4.0, level_multiplier=1.5, final_multiplier=4.0
        )
        assert seconds == 1800

    def test_a_slow_failure_stretches_the_budget(self):
        state = HuntState(stage=Stage.PROBE, observed_failure_time=900.0)
        seconds = bisect.probe_seconds(
            state, base=1800, mttf_multiplier=4.0, level_multiplier=1.5, final_multiplier=4.0
        )
        assert seconds == 3600


class TestPersistence:
    def test_state_survives_a_round_trip(self):
        state, _ = run_hunt(list(range(8)), {3}, cap=6)
        restored = HuntState.from_json(state.to_json())
        assert restored == state

    def test_armed_probe_evidence_survives_a_round_trip(self):
        state = bisect.begin([0, 1], [0])
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
            {"control_fails": True},
            {"control_fails": -1},
            {"control_fails": 2**31},
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
            {"found": [0], "exonerated": [0]},
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
            (Stage.CONTROL, {"suspect": -1}, "suspect must be a core ID or null"),
            (Stage.PROBE, {"pending": [], "queue": [[0, 1], [1, 2]]}, "queued sets must be disjoint"),
            (Stage.PROBE, {"pending": [], "guilty_halves": [[0, 1], [1, 2]]}, "guilty sets must be disjoint"),
            (Stage.CONTROL, {"pending": [[0, 4]]}, "search sets must be subsets of candidates"),
            (Stage.CONTROL, {"suspect": 4}, "suspect must belong to candidates"),
            (Stage.CONTROL, {"found": [4]}, "resolved search sets must be subsets of candidates"),
            (Stage.PROBE, {"suspect": 0}, "probe stage cannot have a suspect"),
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
            (Stage.CONFIRM, {"suspect": None}, "confirm stage requires exactly one suspect and its mask"),
            (Stage.CONFIRM, {"pending": [[0]]}, "confirm suspect cannot also be pending or deferred"),
            (Stage.CULPRIT, {"found": []}, "culprit stage requires only confirmed culprits"),
            (Stage.PLATFORM, {"pending": []}, "platform stage must be a completed stock control"),
            (Stage.EXHAUSTED, {"exonerated": [], "pending": [[0]]}, "exhausted stage has unresolved work"),
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
        bisect.next_live_set(state)
        bisect.record(state, reproduced=False, control_confirmations=2, max_no_reproduce=3)
        assert bisect.next_live_set(state) == [0, 1]
        raw = json.loads(state.to_json())
        raw["exonerated"] = [0]

        with pytest.raises(InvalidHuntState, match="resolved and active"):
            HuntState.from_json(json.dumps(raw))

    def test_split_level_is_bounded_by_candidate_universe(self):
        raw = json.loads(bisect.begin([0, 1], [0]).to_json())
        raw.update(stage="probe", level=3)

        with pytest.raises(InvalidHuntState, match="split depth"):
            HuntState.from_json(json.dumps(raw))

    def test_confirmation_mask_must_match_serialized_universe(self):
        state = bisect.begin([0, 1], [0])
        bisect.next_live_set(state)
        bisect.record(state, reproduced=False, control_confirmations=2, max_no_reproduce=3)
        bisect.next_live_set(state)
        bisect.record(state, reproduced=True, control_confirmations=2, max_no_reproduce=3)
        bisect.next_live_set(state)
        bisect.record(state, reproduced=False, control_confirmations=2, max_no_reproduce=3)
        assert bisect.next_live_set(state) == [1]
        raw = json.loads(state.to_json())
        raw["in_flight"] = []

        with pytest.raises(InvalidHuntState, match="leave-one-out mask"):
            HuntState.from_json(json.dumps(raw))

    def test_terminal_states_survive_round_trip(self):
        culprit, _ = run_hunt([0, 1], {1})
        platform, _ = run_hunt([0, 1], {1}, stock_dies=True)
        exhausted, _ = run_hunt([0, 1], set(), max_no_reproduce=1)

        for state in (culprit, platform, exhausted):
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

        while state.stage not in (Stage.CULPRIT, Stage.PLATFORM, Stage.EXHAUSTED):
            restored = HuntState.from_json(state.to_json())
            assert restored is not state
            state = restored
            live = bisect.next_live_set(state)
            assert live is not None
            stages.add(state.stage)
            state = HuntState.from_json(state.to_json())

            if state.stage is Stage.PROBE and state.queue and not requeued:
                interrupted = list(state.in_flight)
                state.queue.insert(0, interrupted)
                state.in_flight = []
                state = HuntState.from_json(state.to_json())
                assert bisect.next_live_set(state) == interrupted
                state = HuntState.from_json(state.to_json())
                requeued = True

            reproduced = state.stage is not Stage.CONTROL and 6 in state.in_flight
            bisect.record(state, reproduced=reproduced, control_confirmations=2, max_no_reproduce=3)
            state = HuntState.from_json(state.to_json())

        assert requeued
        assert stages == {Stage.CONTROL, Stage.PROBE, Stage.CONFIRM}
        assert state == uninterrupted


def test_a_resumed_confirmation_replays_its_persisted_mask():
    state = _state_at(Stage.CONFIRM)

    assert bisect.next_live_set(state) == [1, 2, 3]


def test_a_confirmation_without_a_suspect_is_rejected():
    state = HuntState(stage=Stage.CONFIRM)

    with pytest.raises(ValueError, match="confirmation has no suspect"):
        bisect.record(state, reproduced=True, control_confirmations=1, max_no_reproduce=1)


def test_split_puts_the_larger_half_first():
    assert bisect.split([0, 1, 2, 3, 4]) == ([0, 1, 2], [3, 4])
    assert bisect.split([0, 1]) == ([0], [1])


def test_the_original_load_is_replayed_by_every_probe():
    """Varying the load instead of the mask would silence idle-caused faults."""
    state = bisect.begin([0, 1, 2, 3], [2])
    assert state.loaded == [2]
    restored = HuntState.from_json(state.to_json())
    assert restored.loaded == [2]
