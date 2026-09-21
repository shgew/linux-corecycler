"""Drive the real tuner loop against :mod:`tests.silicon`.

Only the worker thread is replaced. Everything else is the shipping engine:
the picker, the state machine, ``_apply_co`` and its journal, the resume path,
crash attribution. A hard crash is injected the way the machine really dies --
the offset is already journaled, the process is thrown away, a fresh engine
resumes the same session, and the SMU comes back at stock because Curve
Optimizer lives in volatile SRAM.

The oracle is asked about the offsets that reached the SMU, not about engine
state, so an engine that tests one core while leaving the rest of the vector at
stock is judged on exactly that.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from corecycler.tuner import engine as engine_mod
from corecycler.tuner import persistence as tp
from corecycler.tuner.config import TunerConfig
from corecycler.tuner.engine import TunerEngine
from corecycler.tuner.state import CoreState

from tests.silicon import FakeSilicon, Outcome


class RebootingSMU:
    """Fake SMU that loses every offset on reboot, like the real mailbox."""

    def __init__(self, co_range: tuple[int, int] = (-60, 10)) -> None:
        from types import SimpleNamespace

        self.commands = SimpleNamespace(co_range=co_range)
        self.applied: dict[int, int] = {}
        self.writes: list[tuple[int, int]] = []

    def set_co_offset(self, core_id: int, value: int) -> bool:
        self.writes.append((core_id, value))
        self.applied[core_id] = value
        return True

    def get_co_offset(self, core_id: int) -> int:
        return self.applied.get(core_id, 0)

    def get_all_co_offsets(self, num_cores: int) -> dict[int, int]:
        return {c: self.applied.get(c, 0) for c in range(num_cores)}

    def get_pbo_scalar(self) -> float:
        return 1.0

    def get_boost_limit(self) -> int:
        return 5500

    def reboot(self) -> None:
        self.applied.clear()


@dataclass(slots=True)
class Run:
    """What a driven session ended up doing."""

    engine: TunerEngine
    session_id: int
    steps: int
    crashes: int
    status: str
    final: dict[int, int] = field(default_factory=dict)
    #: True culprit of every hard crash, in order. The engine never sees these.
    culprits: list[int] = field(default_factory=list)

    @property
    def stalled(self) -> bool:
        """The engine stopped making progress and handed control back."""
        return self.status in ("paused", "quarantined")


def drive(
    db,
    topo,
    backend,
    silicon: FakeSilicon,
    *,
    cap: int = 4000,
    settle_steps: int = 400,
    **cfg_kwargs,
) -> Run:
    """Run a whole tuning session against ``silicon`` and report the outcome."""
    cores = sorted(silicon.cores)
    defaults: dict[str, object] = {
        "coarse_step": 5,
        "fine_step": 1,
        "max_offset": -50,
        "search_duration_seconds": 1,
        "confirm_duration_seconds": 1,
        "validate_duration_seconds": 1,
        "hunt_slot_seconds": 30,
        "probe_base_seconds": 60,
        "spectrum_slot_seconds": 30,
        "endurance_slot_seconds": 60,
        # Stages that do not route through the two patched worker entry points
        # (rapid transitions, the memory backend, the no-load soak) would start
        # real threads, so the model cannot judge them.
        "validate_transitions": False,
        "validate_memory": False,
        "validate_soak": False,
        "cores_to_test": cores,
    }
    defaults.update(cfg_kwargs)
    cfg = TunerConfig(**defaults)

    smu = RebootingSMU()
    world = {"rebooted": True}
    engine_mod._rebooted_since = lambda *a, **k: world["rebooted"]

    sid = tp.create_session(db, cfg, "", "")
    pending: list[tuple[frozenset[int], int, float]] = []

    def solo(core_id: int, duration: int, **_kw) -> None:
        pending.append((frozenset({core_id}), core_id, float(duration)))

    def multi(cores_arg, duration: int, **_kw) -> None:
        members = frozenset(cores_arg)
        pending.append((members, min(members), float(duration)))

    def fresh() -> TunerEngine:
        e = TunerEngine(db=db, topology=topo, smu=smu, backend=backend, config=TunerConfig(**defaults))
        e._start_worker = solo
        e._start_multi_core_worker = multi
        e._session_id = sid
        return e

    eng = fresh()
    eng._core_states = {c: CoreState(core_id=c) for c in cores}
    for cs in eng._core_states.values():
        tp.save_core_state(db, sid, cs)
    eng._set_status("running")
    holder = {"eng": eng}
    holder["eng"]._run_next()

    steps = crashes = 0
    culprits: list[int] = []
    # The search never "finishes" by design, so stop once the vector has held
    # still through a full sweep: that is the fixed point the annealing loop
    # keeps re-proving.
    settled_for = 0
    last_vector: dict[int, int] | None = None
    while pending and steps < cap and settled_for < settle_steps:
        steps += 1
        loaded, reporter, duration = pending.pop(0)
        e = holder["eng"]
        if e._core_states.get(reporter) is None:
            continue
        outcome, culprit = silicon.judge(loaded, dict(smu.applied))
        if outcome is Outcome.PASS:
            e._on_test_finished(reporter, True, "", "", duration, 0.0)
        elif outcome is Outcome.SOFT_FAIL:
            e._on_test_finished(culprit, False, "miscompare detected", "computation", duration, 0.0)
        else:
            crashes += 1
            culprits.append(culprit)
            pending.clear()
            smu.reboot()
            world["rebooted"] = True
            holder["eng"] = fresh()
            holder["eng"].resume(sid)
        vector = holder["eng"].live_vector()
        settled_for = settled_for + 1 if vector == last_vector else 0
        last_vector = vector

    final = holder["eng"]
    return Run(
        engine=final,
        session_id=sid,
        steps=steps,
        crashes=crashes,
        status=final.status,
        final=final.live_vector(),
        culprits=culprits,
    )
