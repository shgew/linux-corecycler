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

Two things have to happen before any of that is trustworthy:

* A control probe at full stock. If the machine dies with every core at CO=0,
  the offsets are not the problem and no amount of searching will fix it. That
  is a platform fault (current limits, memory, power) and the only honest
  outcome is to say so.
* A single-core confirmation at the end. Bisection narrows to one core, but the
  narrowing steps are short and probabilistic, so the last step gets a longer
  budget before a core is blamed and demoted.

The state machine is pure and serialisable because a probe's answer arrives by
way of a reboot: the process that asked the question is gone by the time the
answer is known.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum

from corecycler.tuner import regime


class Stage(StrEnum):
    #: Probing at full stock to rule out a platform fault.
    CONTROL = "control"
    #: Halving the live set to find which half carries the culprit.
    PROBE = "probe"
    #: Re-proving a lone suspect with a longer budget before blaming it.
    CONFIRM = "confirm"
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


_STATE_VERSION = 1
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
    #: Sets known to contain at least one culprit, awaiting a split.
    pending: list[list[int]] = field(default_factory=list)
    #: Probes queued for the current split, in order.
    queue: list[list[int]] = field(default_factory=list)
    #: The set made live by the probe currently in flight.
    in_flight: list[int] = field(default_factory=list)
    #: The parent set the in-flight probe was split from.
    parent: list[int] = field(default_factory=list)
    #: Halves of the current parent that reproduced the failure.
    guilty_halves: list[list[int]] = field(default_factory=list)
    control_fails: int = 0
    #: Bisection depth, which grows the probe budget as the answer nears.
    level: int = 0
    found: list[int] = field(default_factory=list)
    exonerated: list[int] = field(default_factory=list)
    #: Consecutive probes that failed to reproduce anything.
    no_reproduce: int = 0
    #: The cores that were under load when the failure happened. Every probe
    #: replays that load; only the offset mask varies. Loading the live set
    #: instead would silence the whole class of faults an IDLE core causes,
    #: which is the class this hunt exists to find.
    loaded: list[int] = field(default_factory=list)
    #: True only after the exact probe vector is durably checkpointed.
    armed: bool = False
    #: The exact per-core offsets checkpointed for the armed probe.
    vector: dict[int, int] = field(default_factory=dict)
    #: The exact workload checkpointed for the armed probe.
    workload: dict | None = None

    def to_json(self) -> str:
        _validate_state(self)
        return json.dumps(
            {
                "version": _STATE_VERSION,
                "stage": str(self.stage),
                "pending": self.pending,
                "queue": self.queue,
                "in_flight": self.in_flight,
                "parent": self.parent,
                "guilty_halves": self.guilty_halves,
                "control_fails": self.control_fails,
                "level": self.level,
                "found": self.found,
                "exonerated": self.exonerated,
                "no_reproduce": self.no_reproduce,
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
                "pending",
                "queue",
                "in_flight",
                "parent",
                "guilty_halves",
                "control_fails",
                "level",
                "found",
                "exonerated",
                "no_reproduce",
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
                pending=_core_sets(raw["pending"], "pending"),
                queue=_core_sets(raw["queue"], "queue"),
                in_flight=_core_set(raw["in_flight"], "in_flight"),
                parent=_core_set(raw["parent"], "parent"),
                guilty_halves=_core_sets(raw["guilty_halves"], "guilty_halves"),
                control_fails=_counter(raw["control_fails"], "control_fails"),
                level=_counter(raw["level"], "level"),
                found=_core_set(raw["found"], "found"),
                exonerated=_core_set(raw["exonerated"], "exonerated"),
                no_reproduce=_counter(raw["no_reproduce"], "no_reproduce"),
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
    pending = [_core_set(group, "pending set", empty=False) for group in state.pending]
    queue = [_core_set(group, "queue set", empty=False) for group in state.queue]
    guilty = [_core_set(group, "guilty set", empty=False) for group in state.guilty_halves]
    in_flight = _core_set(state.in_flight, "in_flight")
    parent = _core_set(state.parent, "parent")
    found = _core_set(state.found, "found")
    exonerated = _core_set(state.exonerated, "exonerated")
    _core_set(state.loaded, "loaded", empty=False)
    _counter(state.control_fails, "control_fails")
    _counter(state.level, "level")
    _counter(state.no_reproduce, "no_reproduce")
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
    if set(found).intersection(exonerated):
        raise _invalid("found and exonerated sets must be disjoint")

    if state.stage is Stage.CONTROL:
        if (
            len(pending) != 1
            or queue
            or in_flight
            or parent
            or guilty
            or found
            or exonerated
            or state.level != 0
            or state.no_reproduce != 0
        ):
            raise _invalid("control stage has impossible search progress")
        return

    if state.stage is Stage.PROBE:
        if found:
            raise _invalid("probe stage cannot already have a culprit")
        if len(queue) > 2 or len(guilty) > 1:
            raise _invalid("probe stage has too many split sets")
        if parent:
            if len(parent) < 2 or state.level < 1:
                raise _invalid("probe parent and level are inconsistent")
        elif queue or guilty or in_flight or state.level != 0:
            raise _invalid("probe progress requires a parent set")
        parent_cores = set(parent)
        if any(not set(group) <= parent_cores for group in queue + guilty):
            raise _invalid("split sets must be subsets of their parent")
        if in_flight and not set(in_flight) <= parent_cores:
            raise _invalid("in-flight set must be a subset of its parent")
        if queue and guilty and not _disjoint(queue + guilty):
            raise _invalid("queued and guilty halves must be disjoint")
        if not _disjoint(pending + queue + guilty):
            raise _invalid("pending and active split sets must be disjoint")
        if len(queue) == 2 and (in_flight or guilty or set(queue[0] + queue[1]) != parent_cores):
            raise _invalid("a fully requeued split must partition its parent")
        if queue and in_flight and (not _disjoint(queue + [in_flight]) or set(queue[0] + in_flight) != parent_cores):
            raise _invalid("queued and in-flight halves must partition their parent")
        if not (pending or queue or in_flight):
            raise _invalid("probe stage has no remaining work")
        return

    if state.stage is Stage.CONFIRM:
        if len(in_flight) != 1 or queue or guilty or found:
            raise _invalid("confirm stage requires exactly one in-flight suspect")
        if any(in_flight[0] in group for group in pending):
            raise _invalid("confirm suspect cannot also be pending")
        if parent and in_flight[0] not in parent:
            raise _invalid("confirm suspect must belong to its parent")
        return

    if state.stage is Stage.CULPRIT:
        if len(in_flight) != 1 or found != in_flight or queue or guilty:
            raise _invalid("culprit stage requires the confirmed in-flight suspect")
        return

    if state.stage is Stage.PLATFORM:
        if (
            state.control_fails < 1
            or len(pending) != 1
            or queue
            or in_flight
            or parent
            or guilty
            or found
            or exonerated
            or state.level != 0
            or state.no_reproduce != 0
        ):
            raise _invalid("platform stage must be a completed stock control")
        return

    if state.stage is Stage.EXHAUSTED:
        if pending or queue or guilty or found or state.no_reproduce < 1:
            raise _invalid("exhausted stage has unresolved work or a verdict")
        return

    raise _invalid("unknown stage")


def begin(candidates: list[int], loaded: list[int]) -> HuntState:
    """Open a hunt over the cores that were carrying a live offset.

    ``loaded`` is replayed by every probe so the question each one answers is
    always "was it these offsets?" and never "was it this workload?".
    """
    return HuntState(stage=Stage.CONTROL, pending=[sorted(candidates)], loaded=sorted(loaded))


def next_live_set(state: HuntState) -> list[int] | None:
    """The live set for the next probe, or None when the hunt has an answer.

    The control probe makes nothing live; an empty list is a real answer and
    is distinct from None.
    """
    if state.stage in (Stage.CULPRIT, Stage.PLATFORM, Stage.EXHAUSTED):
        return None
    if state.stage is Stage.CONTROL:
        state.in_flight = []
        return []
    if state.queue:
        state.in_flight = state.queue.pop(0)
        return list(state.in_flight)
    if not state.pending:
        state.stage = Stage.EXHAUSTED
        return None
    target = state.pending.pop(0)
    if len(target) == 1:
        state.stage = Stage.CONFIRM
        state.in_flight = list(target)
        return list(target)
    left, right = split(target)
    state.parent = list(target)
    state.guilty_halves = []
    state.level += 1
    state.queue = [left, right]
    state.in_flight = state.queue.pop(0)
    return list(state.in_flight)


def record(state: HuntState, *, reproduced: bool, control_confirmations: int, max_no_reproduce: int) -> HuntState:
    """Fold one probe's answer into the hunt.

    ``reproduced`` means the machine failed again under the probe's live set.
    """
    if state.stage is Stage.CONTROL:
        if reproduced:
            state.control_fails += 1
            if state.control_fails >= control_confirmations:
                state.stage = Stage.PLATFORM
            return state
        state.stage = Stage.PROBE
        return state

    if state.stage is Stage.CONFIRM:
        suspect = state.in_flight[0]
        if reproduced:
            state.found.append(suspect)
            state.stage = Stage.CULPRIT
        else:
            # The narrowing said this core, a longer look says otherwise. Do
            # not blame it: an unproven demotion costs real offset depth.
            state.exonerated.append(suspect)
            state.no_reproduce += 1
            if not state.pending:
                state.stage = Stage.EXHAUSTED
            else:
                state.stage = Stage.PROBE
        return state

    if reproduced:
        state.guilty_halves.append(list(state.in_flight))
        state.no_reproduce = 0
    if state.queue:
        return state

    if state.guilty_halves:
        # Both halves reproducing means at least two culprits, so both become
        # independent sub-problems rather than one of them being guessed at.
        state.pending = state.guilty_halves + state.pending
        state.guilty_halves = []
        return state

    # Neither half reproduced on its own. Either the failure needs cores from
    # both halves at once, or it simply did not recur. Splitting further would
    # be inventing information.
    state.no_reproduce += 1
    if state.no_reproduce >= max_no_reproduce:
        state.stage = Stage.EXHAUSTED
    else:
        state.pending.insert(0, list(state.parent))
    return state


def probe_seconds(
    state: HuntState,
    *,
    base: int,
    observed_mttf: float,
    mttf_multiplier: float,
    level_multiplier: float,
    final_multiplier: float,
) -> int:
    """How long this probe runs before a survival counts as clean.

    A false clean near the leaves throws away the whole answer, so the budget
    grows with depth and again for the single-core confirmation.
    """
    budget = max(float(base), observed_mttf * mttf_multiplier)
    budget *= level_multiplier**state.level
    if state.stage is Stage.CONFIRM:
        budget *= final_multiplier
    return max(1, int(budget))
