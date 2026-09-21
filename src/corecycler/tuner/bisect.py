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

    def to_json(self) -> str:
        return json.dumps(
            {
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
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, data: str) -> HuntState | None:
        """Parse persisted hunt state, or None when there is nothing usable.

        Fail closed: a truncated or foreign blob restarts the hunt from the
        control probe rather than resuming a half-understood bisection.
        """
        if not data:
            return None
        try:
            raw = json.loads(data)
            return cls(
                stage=Stage(raw["stage"]),
                pending=[list(map(int, s)) for s in raw["pending"]],
                queue=[list(map(int, s)) for s in raw["queue"]],
                in_flight=list(map(int, raw["in_flight"])),
                parent=list(map(int, raw["parent"])),
                guilty_halves=[list(map(int, s)) for s in raw["guilty_halves"]],
                control_fails=int(raw["control_fails"]),
                level=int(raw["level"]),
                found=list(map(int, raw["found"])),
                exonerated=list(map(int, raw["exonerated"])),
                no_reproduce=int(raw["no_reproduce"]),
                loaded=list(map(int, raw["loaded"])),
            )
        except (ValueError, TypeError, KeyError):
            return None


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
            if state.no_reproduce >= max_no_reproduce and not state.pending:
                state.stage = Stage.EXHAUSTED
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
