"""The attribution state machine, exercised as a search rather than as plumbing."""

from __future__ import annotations

import json
import re

import pytest

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

    def test_exonerated_singleton_does_not_convict_first_core_of_next_set(self):
        state = bisect.begin(list(range(8)), [0])
        responses = {
            (0, 1, 2, 3): True,
            (4, 5, 6, 7): True,
            (0, 1): True,
            (2, 3): False,
            (0,): True,
            (1,): False,
            (4, 5): True,
            (6, 7): False,
            (4,): False,
            (5,): True,
        }

        while state.stage not in (Stage.CULPRIT, Stage.PLATFORM, Stage.EXHAUSTED):
            live = bisect.next_live_set(state)
            assert live is not None
            reproduced = False if state.stage is Stage.CONTROL else responses[tuple(live)]
            if state.stage is Stage.CONFIRM and live == [0]:
                reproduced = False
            bisect.record(state, reproduced=reproduced, control_confirmations=2, max_no_reproduce=3)
            if state.exonerated == [0] and state.stage is Stage.PROBE:
                assert state.found == []

        assert state.stage is Stage.CULPRIT
        assert state.found == [5]
        assert state.exonerated == [0]


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
        ("changes", "message"),
        [
            ({"version": 2}, "unsupported version"),
            ({"level": float("nan")}, "non-finite number NaN"),
            ({"control_fails": True}, "control_fails must be an integer from 0"),
            ({"control_fails": -1}, "control_fails must be an integer from 0"),
            ({"control_fails": 2**31}, "control_fails must be an integer from 0"),
            ({"no_reproduce": float("inf")}, "non-finite number Infinity"),
            ({"pending": {}}, "pending must be a list"),
            ({"in_flight": {}}, "in_flight must be a list"),
            ({"pending": [0]}, "pending[0] must be a list"),
            ({"pending": [[]]}, "pending[0] must not be empty"),
            ({"pending": [[1, 0]]}, "pending[0] must be a sorted set of core IDs"),
            ({"pending": [[0, 0]]}, "pending[0] must be a sorted set of core IDs"),
            ({"pending": [[0], [0]]}, "pending must not contain duplicate sets"),
            ({"loaded": [-1]}, "loaded core must be an integer from 0"),
            ({"loaded": [True]}, "loaded core must be an integer from 0"),
            ({"loaded": [0, 0]}, "loaded must be a sorted set of core IDs"),
            ({"loaded": [1, 0]}, "loaded must be a sorted set of core IDs"),
            ({"vector": []}, "vector must be an object"),
            ({"vector": {"core": -1}}, "vector contains a non-integer core ID"),
            ({"vector": {"00": -1}}, "vector contains an invalid core ID"),
            ({"vector": {str(2**31): -1}}, "vector contains an invalid core ID"),
            ({"vector": {"0": 2**31}}, "vector offsets must be bounded integers"),
            ({"vector": {"0": True}}, "vector offsets must be bounded integers"),
            ({"armed": 1}, "armed must be a boolean"),
            ({"armed": True}, "an armed probe requires its exact vector"),
            ({"workload": []}, "workload[0] must be a dict"),
            ({"pending": [[0, 1], [1, 2]]}, "pending sets must be disjoint"),
            ({"queue": [[0, 1], [1, 2]]}, "queued sets must be disjoint"),
            ({"guilty_halves": [[0, 1], [1, 2]]}, "guilty sets must be disjoint"),
            ({"found": [0], "exonerated": [0]}, "found and exonerated sets must be disjoint"),
            ({"level": 1}, "control stage has impossible search progress"),
        ],
    )
    def test_corrupt_fields_are_rejected_with_the_reason(self, changes, message):
        raw = json.loads(bisect.begin([0, 1], [0, 1]).to_json())
        raw.update(changes)

        with pytest.raises(InvalidHuntState, match=re.escape(message)):
            HuntState.from_json(json.dumps(raw))

    @pytest.mark.parametrize(
        ("changes", "message"),
        [
            ({"found": [3]}, "probe stage cannot already have a culprit"),
            (
                {"pending": [], "queue": [[0], [1], [2]], "parent": [0, 1, 2], "level": 1},
                "probe stage has too many split sets",
            ),
            (
                {"pending": [], "queue": [[0], [1]], "parent": [0, 1], "level": 0},
                "probe parent and level are inconsistent",
            ),
            ({"pending": [], "queue": [[0]]}, "probe progress requires a parent set"),
            (
                {"pending": [], "queue": [[2]], "parent": [0, 1], "level": 1},
                "split sets must be subsets of their parent",
            ),
            (
                {"pending": [], "in_flight": [2], "parent": [0, 1], "level": 1},
                "in-flight set must be a subset of its parent",
            ),
            (
                {"pending": [[0]], "queue": [[0], [1]], "parent": [0, 1], "level": 1},
                "pending and active split sets must be disjoint",
            ),
            (
                {"pending": [], "queue": [[0]], "guilty_halves": [[0]], "parent": [0, 1], "level": 1},
                "queued and guilty halves must be disjoint",
            ),
            (
                {"pending": [], "queue": [[0], [1]], "parent": [0, 1, 2], "level": 1},
                "a fully requeued split must partition its parent",
            ),
            (
                {"pending": [], "queue": [[0]], "in_flight": [2], "parent": [0, 1, 2], "level": 1},
                "queued and in-flight halves must partition their parent",
            ),
            ({"pending": []}, "probe stage has no remaining work"),
        ],
    )
    def test_impossible_probe_progress_is_rejected(self, changes, message):
        raw = json.loads(bisect.begin([0, 1], [0, 1]).to_json())
        raw.update(stage="probe")
        raw.update(changes)

        with pytest.raises(InvalidHuntState, match=re.escape(message)):
            HuntState.from_json(json.dumps(raw))

    @pytest.mark.parametrize(
        ("changes", "message"),
        [
            ({"in_flight": []}, "confirm stage requires exactly one in-flight suspect"),
            ({"pending": [[0]]}, "confirm suspect cannot also be pending"),
            ({"parent": [1]}, "confirm suspect must belong to its parent"),
        ],
    )
    def test_impossible_confirm_progress_is_rejected(self, changes, message):
        raw = json.loads(bisect.begin([0, 1], [0, 1]).to_json())
        raw.update(stage="confirm", pending=[], in_flight=[0])
        raw.update(changes)

        with pytest.raises(InvalidHuntState, match=re.escape(message)):
            HuntState.from_json(json.dumps(raw))

    @pytest.mark.parametrize(
        ("changes", "message"),
        [
            (
                {"stage": "culprit", "pending": [], "in_flight": [0]},
                "culprit stage requires the confirmed in-flight suspect",
            ),
            ({"stage": "platform", "control_fails": 0}, "platform stage must be a completed stock control"),
            (
                {"stage": "exhausted", "pending": [], "no_reproduce": 0},
                "exhausted stage has unresolved work or a verdict",
            ),
        ],
    )
    def test_impossible_terminal_progress_is_rejected(self, changes, message):
        raw = json.loads(bisect.begin([0, 1], [0, 1]).to_json())
        raw.update(changes)

        with pytest.raises(InvalidHuntState, match=re.escape(message)):
            HuntState.from_json(json.dumps(raw))

    @pytest.mark.parametrize(
        "changes",
        [
            {"stage": "platform", "control_fails": 1},
            {"stage": "exhausted", "pending": [], "no_reproduce": 1},
        ],
    )
    def test_terminal_state_survives_a_round_trip(self, changes):
        raw = json.loads(bisect.begin([0, 1], [0, 1]).to_json())
        raw.update(changes)

        restored = HuntState.from_json(json.dumps(raw))

        assert restored is not None
        assert restored.stage == Stage(changes["stage"])

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


def test_split_puts_the_larger_half_first():
    assert bisect.split([0, 1, 2, 3, 4]) == ([0, 1, 2], [3, 4])
    assert bisect.split([0, 1]) == ([0], [1])


def test_the_original_load_is_replayed_by_every_probe():
    """Varying the load instead of the mask would silence idle-caused faults."""
    state = bisect.begin([0, 1, 2, 3], [2])
    assert state.loaded == [2]
    restored = HuntState.from_json(state.to_json())
    assert restored.loaded == [2]
