"""Attributing a failure that names no core.

A hard freeze with no machine check and no lane error tells you the machine
died, not which core killed it. The old answer was to load each core in turn
with every other core at stock, which cannot work: parking the others at stock
removes the very condition (a whole live offset vector) that caused the
failure, so every slot passes and the search learns nothing.

What actually varies is the offset MASK. Cores in the live set carry their
learned offsets; everyone else sits at stock. Halving the live set and
reproducing the failure isolates a culprit in log2(n) probes, and the machine
is in a real operating point the whole time.

A control probe at full stock comes first. If the machine dies with every core
at CO=0, the offsets are not the problem and no amount of searching will fix
it. That is a platform fault (current limits, memory, power) and the only
honest outcome is to say so.

A single core reaches the end of bisection only by reproducing the failure
with every other core at stock. That is the whole question the hunt asks, so
the core is convicted there; re-running everyone else without it would only
spend hours re-proving the answer.

The state machine is pure and serialisable because a probe's answer arrives by
way of a reboot: the process that asked the question is gone by the time the
answer is known.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from enum import StrEnum

from corecycler.tuner import regime


class Stage(StrEnum):
    #: Probing at full stock to rule out a platform fault.
    CONTROL = "control"
    #: Halving the live set to find which half carries the culprit.
    PROBE = "probe"
    #: A culprit was confirmed.
    CULPRIT = "culprit"
    #: The machine dies at stock too.
    PLATFORM = "platform"
    #: Nothing reproduced. The caller falls back to accumulated suspicion.
    EXHAUSTED = "exhausted"


def split(candidates: list[int]) -> tuple[list[int], list[int]]:
    """Halve a candidate set, larger half first so odd sets shrink fastest."""
    mid = (len(candidates) + 1) // 2
    return candidates[:mid], candidates[mid:]


_STATE_VERSION = 4
_MAX_INTEGER = 2**31 - 1
_MIN_INTEGER = -(2**31)


class InvalidHuntState(ValueError):
    """Persisted hunt state is present but cannot be trusted."""


def _invalid(message: str) -> InvalidHuntState:
    return InvalidHuntState(f"invalid persisted hunt state: {message}")


def _reject_constant(value: str) -> None:
    raise _invalid(f"non-finite number {value}")


def _counter(value: object, name: str) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_INTEGER:
        raise _invalid(f"{name} must be an integer from 0 to {_MAX_INTEGER}")
    return value


def _duration(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise _invalid(f"{name} must be a finite non-negative number")
    return float(value)


def _core_set(value: object, name: str, *, empty: bool = True) -> list[int]:
    if not isinstance(value, list):
        raise _invalid(f"{name} must be a list")
    result = [_counter(core, f"{name} core") for core in value]
    if not empty and not result:
        raise _invalid(f"{name} must not be empty")
    if result != sorted(set(result)):
        raise _invalid(f"{name} must be a sorted set of core IDs")
    return result


def _core_sets(value: object, name: str) -> list[list[int]]:
    if not isinstance(value, list):
        raise _invalid(f"{name} must be a list")
    result = [_core_set(item, f"{name}[{index}]", empty=False) for index, item in enumerate(value)]
    encoded = [tuple(item) for item in result]
    if len(encoded) != len(set(encoded)):
        raise _invalid(f"{name} must not contain duplicate sets")
    return result


def _disjoint(groups: list[list[int]]) -> bool:
    seen: set[int] = set()
    for group in groups:
        if seen.intersection(group):
            return False
        seen.update(group)
    return True


def _vector(value: object) -> dict[int, int]:
    if not isinstance(value, dict):
        raise _invalid("vector must be an object")
    result: dict[int, int] = {}
    for encoded_core, offset in value.items():
        if not isinstance(encoded_core, str):
            raise _invalid("vector core IDs must be strings")
        try:
            core = int(encoded_core)
        except ValueError as error:
            raise _invalid("vector contains a non-integer core ID") from error
        if encoded_core != str(core) or not 0 <= core <= _MAX_INTEGER:
            raise _invalid("vector contains an invalid core ID")
        if type(offset) is not int or not _MIN_INTEGER <= offset <= _MAX_INTEGER:
            raise _invalid("vector offsets must be bounded integers")
        result[core] = offset
    return result


@dataclass(slots=True)
class HuntState:
    """Serialisable progress of one attribution hunt."""

    stage: Stage = Stage.CONTROL
    candidates: list[int] = field(default_factory=list)
    pending: list[list[int]] = field(default_factory=list)
    queue: list[list[int]] = field(default_factory=list)
    in_flight: list[int] = field(default_factory=list)
    parent: list[int] = field(default_factory=list)
    guilty_halves: list[list[int]] = field(default_factory=list)
    control_fails: int = 0
    level: int = 0
    found: list[int] = field(default_factory=list)
    no_reproduce: int = 0
    launches_done: int = 0
    loaded: list[int] = field(default_factory=list)
    observed_failure_time: float = 0.0
    armed: bool = False
    vector: dict[int, int] = field(default_factory=dict)
    workload: dict | None = None

    def to_json(self) -> str:
        _validate_state(self)
        return json.dumps(
            {
                "version": _STATE_VERSION,
                "stage": str(self.stage),
                "candidates": self.candidates,
                "pending": self.pending,
                "queue": self.queue,
                "in_flight": self.in_flight,
                "parent": self.parent,
                "guilty_halves": self.guilty_halves,
                "control_fails": self.control_fails,
                "level": self.level,
                "found": self.found,
                "no_reproduce": self.no_reproduce,
                "launches_done": self.launches_done,
                "observed_failure_time": self.observed_failure_time,
                "loaded": self.loaded,
                "armed": self.armed,
                "vector": self.vector,
                "workload": self.workload,
            },
            separators=(",", ":"),
            allow_nan=False,
        )

    @classmethod
    def from_json(cls, data: str) -> HuntState | None:
        """Return absent state for an empty value and reject every corrupt blob."""
        if data == "":
            return None
        if not isinstance(data, str):
            raise _invalid("state must be a string")
        try:
            raw = json.loads(data, parse_constant=_reject_constant)
            if not isinstance(raw, dict):
                raise _invalid("state must be an object")
            expected = {
                "version",
                "stage",
                "candidates",
                "pending",
                "queue",
                "in_flight",
                "parent",
                "guilty_halves",
                "control_fails",
                "level",
                "found",
                "no_reproduce",
                "launches_done",
                "observed_failure_time",
                "loaded",
                "armed",
                "vector",
                "workload",
            }
            if set(raw) != expected:
                raise _invalid("state fields do not match the supported version")
            if raw["version"] != _STATE_VERSION or type(raw["version"]) is not int:
                raise _invalid("unsupported version")
            if type(raw["armed"]) is not bool:
                raise _invalid("armed must be a boolean")
            workload = raw["workload"]
            if workload is not None:
                errors = regime.workload_errors("workload", 0, workload)
                if errors:
                    raise _invalid("; ".join(errors))
            state = cls(
                stage=Stage(raw["stage"]),
                candidates=_core_set(raw["candidates"], "candidates", empty=False),
                pending=_core_sets(raw["pending"], "pending"),
                queue=_core_sets(raw["queue"], "queue"),
                in_flight=_core_set(raw["in_flight"], "in_flight"),
                parent=_core_set(raw["parent"], "parent"),
                guilty_halves=_core_sets(raw["guilty_halves"], "guilty_halves"),
                control_fails=_counter(raw["control_fails"], "control_fails"),
                level=_counter(raw["level"], "level"),
                found=_core_set(raw["found"], "found"),
                no_reproduce=_counter(raw["no_reproduce"], "no_reproduce"),
                launches_done=_counter(raw["launches_done"], "launches_done"),
                observed_failure_time=_duration(raw["observed_failure_time"], "observed_failure_time"),
                loaded=_core_set(raw["loaded"], "loaded", empty=False),
                armed=raw["armed"],
                vector=_vector(raw["vector"]),
                workload=workload,
            )
            _validate_state(state)
            return state
        except InvalidHuntState:
            raise
        except (KeyError, TypeError, ValueError, OverflowError, RecursionError) as error:
            raise _invalid(str(error)) from error


def _validate_state(state: HuntState) -> None:
    candidates = _core_set(state.candidates, "candidates", empty=False)
    pending = [_core_set(group, "pending set", empty=False) for group in state.pending]
    queue = [_core_set(group, "queue set", empty=False) for group in state.queue]
    guilty = [_core_set(group, "guilty set", empty=False) for group in state.guilty_halves]
    in_flight = _core_set(state.in_flight, "in_flight")
    parent = _core_set(state.parent, "parent")
    found = _core_set(state.found, "found")
    _core_set(state.loaded, "loaded", empty=False)
    _counter(state.control_fails, "control_fails")
    _counter(state.level, "level")
    _counter(state.no_reproduce, "no_reproduce")
    _counter(state.launches_done, "launches_done")
    _duration(state.observed_failure_time, "observed_failure_time")
    if type(state.armed) is not bool:
        raise _invalid("armed must be a boolean")
    if not isinstance(state.vector, dict) or any(
        type(core) is not int
        or not 0 <= core <= _MAX_INTEGER
        or type(offset) is not int
        or not _MIN_INTEGER <= offset <= _MAX_INTEGER
        for core, offset in state.vector.items()
    ):
        raise _invalid("vector must map bounded integer core IDs to bounded integer offsets")
    if state.armed and not state.vector:
        raise _invalid("an armed probe requires its exact vector")
    if state.workload is not None:
        errors = regime.workload_errors("workload", 0, state.workload)
        if errors:
            raise _invalid("; ".join(errors))

    if not _disjoint(pending):
        raise _invalid("pending sets must be disjoint")
    if not _disjoint(queue):
        raise _invalid("queued sets must be disjoint")
    if not _disjoint(guilty):
        raise _invalid("guilty sets must be disjoint")
    candidate_set = set(candidates)
    grouped = pending + queue + guilty
    if any(not set(group) <= candidate_set for group in grouped) or not set(in_flight + parent) <= candidate_set:
        raise _invalid("search sets must be subsets of candidates")
    if set(found) & (set(in_flight + parent) | {core for group in grouped for core in group}):
        raise _invalid("resolved and active search sets must be disjoint")
    if not set(found) <= candidate_set:
        raise _invalid("resolved search sets must be subsets of candidates")
    max_level = (len(candidates) - 1).bit_length()
    if state.level > max_level:
        raise _invalid("level exceeds the candidate split depth")
    if state.launches_done and state.stage is not Stage.CONTROL and not in_flight:
        raise _invalid("launch progress requires an unanswered probe")

    if state.stage is Stage.CONTROL:
        if (
            pending != [candidates]
            or queue
            or in_flight
            or parent
            or guilty
            or found
            or state.level != 0
            or state.no_reproduce != 0
        ):
            raise _invalid("control stage has impossible search progress")
        return

    if state.stage is Stage.PROBE:
        if len(queue) > 2 or len(guilty) > 2:
            raise _invalid("probe stage has too many split sets")
        if parent:
            if len(parent) < 2 or state.level < 1:
                raise _invalid("probe parent and level are inconsistent")
            parent_set = set(parent)
            active_halves = queue + guilty + ([in_flight] if in_flight else [])
            if any(not set(group) <= parent_set for group in active_halves):
                raise _invalid("split sets must be subsets of their parent")
            if not _disjoint(active_halves):
                raise _invalid("active split sets must be disjoint")
            if any(set(group) & parent_set for group in pending):
                raise _invalid("pending sets must be disjoint from the active parent")
            if len(queue) == 2 and set(queue[0] + queue[1]) != parent_set:
                raise _invalid("a fully queued split must partition its parent")
        elif queue or guilty or in_flight or state.level != 0:
            raise _invalid("probe progress requires a parent set")
        if not (pending or queue or in_flight):
            raise _invalid("probe stage has no remaining work")
        return

    if state.stage is Stage.CULPRIT:
        if not found or pending or queue or in_flight or parent or guilty:
            raise _invalid("culprit stage requires only confirmed culprits")
        return

    if state.stage is Stage.PLATFORM:
        if state.control_fails < 1 or pending != [candidates] or queue or in_flight or parent or guilty or found:
            raise _invalid("platform stage must be a completed stock control")
        return

    if state.stage is Stage.EXHAUSTED:
        if pending or queue or in_flight or parent or guilty:
            raise _invalid("exhausted stage has unresolved work")
        return

    raise _invalid("unknown stage")


def begin(candidates: list[int], loaded: list[int], *, observed_failure_time: float = 0.0) -> HuntState:
    """Open a hunt over the cores that were carrying a live offset."""
    universe = _core_set(sorted(candidates), "candidates", empty=False)
    loaded_set = _core_set(sorted(loaded), "loaded", empty=False)
    state = HuntState(
        stage=Stage.CONTROL,
        candidates=universe,
        pending=[list(universe)],
        observed_failure_time=_duration(observed_failure_time, "observed_failure_time"),
        loaded=loaded_set,
    )
    _validate_state(state)
    return state


def next_live_set(state: HuntState) -> list[int] | None:
    """Return and persist the exact live mask for the next probe.

    A mask already in flight has not been answered, so it is replayed rather
    than skipped.
    """
    if state.stage in (Stage.CULPRIT, Stage.PLATFORM, Stage.EXHAUSTED):
        return None
    if state.stage is Stage.CONTROL:
        state.in_flight = []
        return []
    if state.in_flight:
        return list(state.in_flight)
    if state.queue:
        state.in_flight = state.queue.pop(0)
        return list(state.in_flight)
    # A lone core is pending only after reproducing the failure with every
    # other core at stock, or as the whole live set of the crash.
    while state.pending and len(state.pending[0]) == 1:
        state.found = sorted(state.found + state.pending.pop(0))
    if not state.pending:
        state.stage = Stage.CULPRIT if state.found else Stage.EXHAUSTED
        return None
    target = state.pending.pop(0)
    left, right = split(target)
    state.parent = list(target)
    state.guilty_halves = []
    max_depth = (len(state.candidates) - 1).bit_length()
    state.level = min(max_depth, max(1, max_depth - (len(target) - 1).bit_length() + 1))
    state.queue = [left, right]
    state.in_flight = state.queue.pop(0)
    return list(state.in_flight)


def record(state: HuntState, *, reproduced: bool, control_confirmations: int, max_no_reproduce: int) -> HuntState:
    """Fold one probe's answer into the hunt."""
    state.launches_done = 0
    if state.stage is Stage.CONTROL:
        state.in_flight = []
        if reproduced:
            state.control_fails += 1
            if state.control_fails >= control_confirmations:
                state.stage = Stage.PLATFORM
            return state
        state.stage = Stage.PROBE
        return state

    probe = list(state.in_flight)
    state.in_flight = []
    if probe == state.parent:
        return _record_whole_set(state, reproduced=reproduced)
    if reproduced:
        state.guilty_halves.append(probe)
        state.no_reproduce = 0
    if state.queue:
        return state

    parent = list(state.parent)
    level = state.level
    state.parent = []
    state.level = 0
    if state.guilty_halves:
        state.pending = state.guilty_halves + state.pending
        state.guilty_halves = []
        return state

    state.no_reproduce += 1
    if state.no_reproduce >= max_no_reproduce:
        # Both halves stayed clean through every retry. Before calling that a
        # non-reproduction, ask the whole set once more: a set that fails while
        # neither half fails alone needs cores from both halves at once.
        state.parent = parent
        state.level = level
        state.queue = [parent]
    else:
        state.pending.insert(0, parent)
    return state


def _record_whole_set(state: HuntState, *, reproduced: bool) -> HuntState:
    whole = list(state.parent)
    state.parent = []
    state.level = 0
    if reproduced:
        state.found = sorted(state.found + whole)
        state.no_reproduce = 0
        if not state.pending:
            state.stage = Stage.CULPRIT
        return state
    state.pending = []
    state.stage = Stage.CULPRIT if state.found else Stage.EXHAUSTED
    return state


def probe_seconds(
    state: HuntState,
    *,
    base: int,
    mttf_multiplier: float,
    level_multiplier: float,
) -> int:
    """How long this probe runs before a survival counts as clean.

    A false clean near the leaves throws away the whole answer, so the budget
    grows with depth.
    """
    budget = max(float(base), state.observed_failure_time * mttf_multiplier)
    budget *= level_multiplier**state.level
    return max(1, int(budget))


def onset_launches(
    state: HuntState,
    *,
    budget: int,
    onset_seconds: int,
    min_launch: int,
    mttf_multiplier: float,
) -> tuple[int, int]:
    """Split a probe budget into ``(launch_seconds, launches)``.

    A failure that lands within ``onset_seconds`` of load starting is
    reproduced by load starts, not by wall time, so its budget is spent as
    many launches each long enough to outlast the observed failure time. A
    slow or untimed failure keeps one launch for the whole budget.
    """
    observed = state.observed_failure_time
    if not 0 < observed <= onset_seconds:
        return budget, 1
    launch = min(budget, max(min_launch, math.ceil(observed * mttf_multiplier)))
    return launch, math.ceil(budget / launch)
