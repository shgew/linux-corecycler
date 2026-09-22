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
from enum import StrEnum

from corecycler.history.context import SystemContext, compute_context_hash
from corecycler.tuner import engine as engine_mod
from corecycler.tuner.config import TunerConfig
from corecycler.tuner.engine import TunerEngine
from corecycler.tuner.state import CoreState, TunerPhase
from tests.silicon import FakeSilicon, Outcome, converged


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


class TerminalReason(StrEnum):
    CLEAN_CONVERGED = "clean_converged"
    STEP_CAP = "step_cap"
    PENDING_EXHAUSTED = "pending_exhausted"
    PAUSED = "paused"
    HUNTING = "hunting"
    INCOMPLETE = "incomplete"


@dataclass(slots=True)
class Run:
    """What a driven session ended up doing."""

    engine: TunerEngine
    session_id: int
    steps: int
    crashes: int
    status: str
    terminal_reason: TerminalReason
    final: dict[int, int] = field(default_factory=dict)
    culprits: list[int] = field(default_factory=list)
    context_id: int | None = None
    regime_banks: dict[int, dict[str, float]] = field(default_factory=dict)
    regime_weights: dict[str, float] = field(default_factory=dict)
    anneal_probes: int = 0
    annealed_cores: set[int] = field(default_factory=set)
    anneal_eligible: set[int] = field(default_factory=set)

    @property
    def clean_converged(self) -> bool:
        return self.terminal_reason is TerminalReason.CLEAN_CONVERGED

    @property
    def stalled(self) -> bool:
        return not self.clean_converged


def drive(
    db,
    topo,
    backend,
    silicon: FakeSilicon,
    *,
    cap: int = 4000,
    **cfg_kwargs,
) -> Run:
    """Run a whole tuning session against the silicon model."""
    cores = sorted(silicon.cores)
    defaults: dict[str, object] = {
        "coarse_step": 5,
        "fine_step": 1,
        "max_offset": -50,
        "search_duration_seconds": 10,
        "confirm_duration_seconds": 30,
        "validate_duration_seconds": 30,
        "probe_base_seconds": 60,
        "spectrum_slot_seconds": 30,
        "endurance_slot_seconds": 60,
        "anneal_bank_hours": 0.01,
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

    co = tuple(0 for _ in cores)
    context_values = dict(
        cpu_model="Silicon model CPU",
        physical_cores=len(cores),
        ccds=topo.ccds,
        co=co,
        pbo_scalar=smu.get_pbo_scalar(),
        boost_limit_mhz=smu.get_boost_limit(),
        ppt_limit_w=None,
        tdc_limit_a=None,
        edc_limit_a=None,
        bios_version="Silicon model BIOS",
    )
    context = SystemContext(
        **context_values,
        context_hash=compute_context_hash(**context_values),
        complete=True,
        missing=(),
    )
    context_id = db.get_or_create_context(context)
    engine_mod.capture_system_context = lambda *_, **__: context
    sid = db.create_tuner_session(cfg.to_json(), "Silicon model BIOS", "Silicon model CPU", context_id)
    pending: list[tuple[frozenset[int], int, float]] = []

    def solo(core_id: int, duration: int, **kw) -> None:
        e = holder["eng"]
        profile = "transient" if kw.get("duty_cycle") is not None else "spectrum" if kw.get("spectrum") else "sustained"
        e._worker_profile = profile
        snapshot = e._workload_snapshot(e._core_states[core_id], profile=profile)
        snapshot["duration_seconds"] = duration
        e._checkpoint_worker(snapshot, e._cores_under_stress or [core_id])
        pending.append((frozenset({core_id}), core_id, float(duration)))

    def multi(cores_arg, duration: int, **kw) -> None:
        e = holder["eng"]
        members = frozenset(cores_arg)
        workload = kw.get("workload")
        snapshot = dict(workload) if workload is not None else e._workload_snapshot(e._core_states[min(members)])
        snapshot["duration_seconds"] = duration
        e._checkpoint_worker(snapshot, sorted(members))
        pending.append((members, min(members), float(duration)))

    def fresh() -> TunerEngine:
        e = TunerEngine(
            db=db,
            topology=topo,
            smu=smu,
            backend=backend,
            config=TunerConfig(**defaults),
        )
        e._start_worker = solo
        e._start_multi_core_worker = multi
        e._session_id = sid
        return e

    eng = fresh()
    eng._core_states = {c: CoreState(core_id=c) for c in cores}
    for cs in eng._core_states.values():
        db.upsert_tuner_core_state(sid, cs)
    eng._set_status("running")
    holder = {"eng": eng}
    holder["eng"]._run_next()

    steps = crashes = 0
    anneal_probes = 0
    culprits: list[int] = []
    annealed_cores: set[int] = set()
    covered_regimes = {str(entry["regime"]) for entry in cfg.battery}

    def bank_snapshot(e: TunerEngine) -> dict[int, dict[str, float]]:
        context_id = e.context_id()
        if context_id is None:
            return {}
        result: dict[int, dict[str, float]] = {}
        for core_id, cs in e._core_states.items():
            offset = cs.best_offset if cs.best_offset is not None else cs.current_offset
            result[core_id] = db.get_regime_banks(context_id, core_id, offset)
        return result

    def anneal_eligible(e: TunerEngine) -> set[int]:
        """Cores the shipping annealer could still probe at this point."""
        if e._config.anneal_bank_hours <= 0:
            return set()
        eligible: set[int] = set()
        for core_id, cs in e._core_states.items():
            if cs.phase is not TunerPhase.CONFIRMED or cs.best_offset is None:
                continue
            if cs.anneal_strikes >= e._config.anneal_max_strikes:
                continue
            candidate = cs.best_offset + e._config.direction * e._config.fine_step
            if e._exceeds_max(candidate):
                continue
            if e._banked_hours(cs) >= e._anneal_bar(cs):
                eligible.add(core_id)
        return eligible

    def resume_engine_loop(e: TunerEngine) -> None:
        """Pump shipping continuations until they launch another worker.

        ``_on_test_finished`` normally posts its continuation to the Qt event
        loop. This driver has no event loop, so an empty worker queue is not a
        terminal state. Continue through the same engine entry points a real
        queued callback would invoke, including validation and hunt recovery.
        """
        while not pending:
            before = (
                e.status,
                e._hunting,
                e._validation_stage,
                e._validation_core_index,
                e._validation_half_index,
                e._in_requeue,
                tuple(e._validation_requeue),
                e._endurance_round,
                e._endurance_workload,
                e._endurance_index,
                tuple(
                    (core_id, cs.phase, cs.current_offset, cs.best_offset, cs.battery_index)
                    for core_id, cs in sorted(e._core_states.items())
                ),
            )
            if e._hunting or e.status == "hunting":
                e._run_next_hunt_slot()
            elif e._validation_stage > 0 or e.status == "validating":
                if e._in_requeue or e._validation_requeue:
                    e._run_validation_requeue()
                else:
                    e._run_validation_next()
            elif e.status == "running":
                e._run_next()
            else:
                return
            after = (
                e.status,
                e._hunting,
                e._validation_stage,
                e._validation_core_index,
                e._validation_half_index,
                e._in_requeue,
                tuple(e._validation_requeue),
                e._endurance_round,
                e._endurance_workload,
                e._endurance_index,
                tuple(
                    (core_id, cs.phase, cs.current_offset, cs.best_offset, cs.battery_index)
                    for core_id, cs in sorted(e._core_states.items())
                ),
            )
            if not pending and after == before:
                return

    def cleanly_converged(e: TunerEngine) -> bool:
        if e._hunting or e.status in ("hunting", "paused", "profile_quarantined", "aborted"):
            return False
        if e._validation_stage != 9 or e._validation_dirty:
            return False
        if not anneal_eligible(e) <= annealed_cores:
            return False
        if any(cs.phase is not TunerPhase.CONFIRMED for cs in e._core_states.values()):
            return False
        ok, _ = converged(silicon, e.live_vector())
        if not ok:
            return False
        banks = bank_snapshot(e)
        return all(
            covered_regimes <= {regime for regime, seconds in banks[core_id].items() if seconds > 0}
            for core_id in cores
        )

    clean = False
    while pending and steps < cap:
        steps += 1
        loaded, reporter, duration = pending.pop(0)
        e = holder["eng"]
        cs = e._core_states.get(reporter)
        if cs is None:
            continue
        if cs.phase is TunerPhase.ANNEALING:
            anneal_probes += 1
            annealed_cores.add(reporter)
        outcome, culprit = silicon.judge(loaded, dict(smu.applied))
        if outcome is Outcome.PASS:
            e._on_test_finished(reporter, True, "", "", duration, 0.0)
        elif outcome is Outcome.SOFT_FAIL:
            assert culprit is not None
            e._on_test_finished(culprit, False, "miscompare detected", "computation", duration, 0.0)
        else:
            assert culprit is not None
            crashes += 1
            culprits.append(culprit)
            pending.clear()
            smu.reboot()
            world["rebooted"] = True
            holder["eng"] = fresh()
            holder["eng"].resume(sid)
        clean = cleanly_converged(holder["eng"])
        if clean:
            break
        resume_engine_loop(holder["eng"])

    final = holder["eng"]
    if steps >= cap:
        terminal_reason = TerminalReason.STEP_CAP
    elif final._hunting or final.status == "hunting":
        terminal_reason = TerminalReason.HUNTING
    elif final.status in ("paused", "profile_quarantined", "aborted"):
        terminal_reason = TerminalReason.PAUSED
    elif clean:
        terminal_reason = TerminalReason.CLEAN_CONVERGED
    elif not pending:
        terminal_reason = TerminalReason.PENDING_EXHAUSTED
    else:
        terminal_reason = TerminalReason.INCOMPLETE
    banks = bank_snapshot(final)
    eligible = anneal_eligible(final)
    return Run(
        engine=final,
        session_id=sid,
        steps=steps,
        crashes=crashes,
        status=final.status,
        terminal_reason=terminal_reason,
        final=final.live_vector(),
        culprits=culprits,
        context_id=final.context_id(),
        regime_banks=banks,
        regime_weights=final._regime_weights(),
        anneal_probes=anneal_probes,
        annealed_cores=annealed_cores,
        anneal_eligible=eligible,
    )
