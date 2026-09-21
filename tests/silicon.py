"""A silicon model whose stability depends on the whole live offset vector.

The existing closed-loop drivers in ``test_tuner_faults`` key their verdict on
the tested core's offset alone. Real Curve Optimizer instability does not work
that way: a core sitting *idle* at an aggressive offset drops to a low-current
operating point where its Vmin margin is thinnest, and takes the machine down
while some entirely different core is the one under load. That failure names no
core, and any search that tests one core with every other core at stock cannot
observe it at all.

This module models both conditions:

``load_limit``
    The most negative offset the core survives **while loaded**.
``idle_limit``
    The most negative offset the core survives **while idle** with its offset
    live. On real silicon this is usually the shallower of the two, which is
    what produces the "passes every per-core test, dies overnight" pattern.

A core is genuinely stable only at ``max(load_limit, idle_limit)`` or above.
That value is the answer an autonomous tuner has to converge on, and
``true_limit`` returns it.

The oracle reads the offsets that were actually written to the fake SMU rather
than any engine-internal bookkeeping, so a search that forgets to apply the rest
of the vector is judged on what it really put on the hardware.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto

from corecycler.history.context import SystemContext, compute_context_hash

# Verdicts a stress slot can produce in this model.


class Outcome(Enum):
    PASS = auto()
    #: The loaded core miscomputed. The engine learns which core failed.
    SOFT_FAIL = auto()
    #: The machine died. No core is named; recovery goes through the journal.
    HARD_CRASH = auto()


def complete_context(db, topology, smu=None) -> int:
    cores = len(topology.cores)
    co = tuple(smu.get_co_offset(core_id) if smu is not None else 0 for core_id in range(cores))
    values = {
        "cpu_model": topology.model_name,
        "physical_cores": cores,
        "ccds": topology.ccds,
        "co": co,
        "pbo_scalar": smu.get_pbo_scalar() if smu is not None else 1.0,
        "boost_limit_mhz": smu.get_boost_limit() if smu is not None else 5500,
        "ppt_limit_w": None,
        "tdc_limit_a": None,
        "edc_limit_a": None,
        "bios_version": "Test BIOS",
    }
    return db.get_or_create_context(
        SystemContext(
            **values,
            context_hash=compute_context_hash(**values),
            complete=True,
            missing=(),
        )
    )


@dataclass(frozen=True, slots=True)
class CoreSilicon:
    """One core's true limits. Offsets are negative; larger is safer."""

    load_limit: int
    idle_limit: int
    #: Visits at a marginal offset before instability actually bites. 1 = every
    #: visit fails, 3 = the first two visits look stable. Models the fact that a
    #: short pass proves nothing.
    flaky_visits: int = 1

    @property
    def true_limit(self) -> int:
        """The most negative offset stable under both load and idle."""
        return max(self.load_limit, self.idle_limit)


@dataclass(slots=True)
class FakeSilicon:
    """Whole-CPU model. ``cores`` maps core id to its true limits."""

    cores: dict[int, CoreSilicon]
    #: Every (core, offset, condition) visit counter, for flaky_visits.
    _visits: dict[tuple[int, int, str], int] = field(default_factory=dict)
    #: Appended for every judged slot: (loaded, applied, outcome, culprit).
    history: list[tuple[frozenset[int], dict[int, int], Outcome, int | None]] = field(default_factory=list)

    def true_vector(self) -> dict[int, int]:
        return {core_id: si.true_limit for core_id, si in self.cores.items()}

    def _bites(self, core_id: int, offset: int, condition: str) -> bool:
        """Whether an over-aggressive offset actually manifests on this visit."""
        key = (core_id, offset, condition)
        self._visits[key] = self._visits.get(key, 0) + 1
        return self._visits[key] >= self.cores[core_id].flaky_visits

    def judge(self, loaded: set[int] | frozenset[int], applied: dict[int, int]) -> tuple[Outcome, int | None]:
        """Judge one stress slot.

        ``loaded`` is the set of cores running the payload; ``applied`` is the
        offset actually resident on every core. Returns the outcome and, for a
        hard crash, the core that really caused it (which the engine is never
        told -- it exists so a test can assert the search found the right one).

        A loaded core below its load limit miscomputes, which is evidence the
        engine can attribute. An idle core below its idle limit takes the whole
        machine down, which is evidence it cannot. Load failures are checked
        first because a detected computation error stops the slot before the
        machine has a chance to die.
        """
        loaded = frozenset(loaded)
        for core_id in sorted(loaded):
            si = self.cores.get(core_id)
            if (
                si is not None
                and applied.get(core_id, 0) < si.load_limit
                and self._bites(core_id, applied[core_id], "load")
            ):
                self.history.append((loaded, dict(applied), Outcome.SOFT_FAIL, core_id))
                return Outcome.SOFT_FAIL, core_id
        for core_id in sorted(self.cores):
            if core_id in loaded:
                continue
            si = self.cores[core_id]
            offset = applied.get(core_id, 0)
            if offset < si.idle_limit and self._bites(core_id, offset, "idle"):
                self.history.append((loaded, dict(applied), Outcome.HARD_CRASH, core_id))
                return Outcome.HARD_CRASH, core_id
        self.history.append((loaded, dict(applied), Outcome.PASS, None))
        return Outcome.PASS, None


def converged(silicon: FakeSilicon, final: dict[int, int], *, tolerance: int = 1) -> tuple[bool, list[str]]:
    """Check a final offset vector against the silicon's true limits.

    Two-sided on purpose. Leaving a core below its true limit means the tuner
    shipped an unstable answer; leaving it needlessly far above means the search
    punished an innocent core, which is the failure mode that blanket back-off
    and mis-attribution produce.
    """
    problems: list[str] = []
    for core_id, si in silicon.cores.items():
        offset = final.get(core_id, 0)
        if offset < si.true_limit:
            problems.append(f"core {core_id} left unstable at {offset} (true limit {si.true_limit})")
        elif offset > si.true_limit + tolerance:
            problems.append(f"core {core_id} over-backed-off to {offset} (true limit {si.true_limit})")
    return not problems, problems
