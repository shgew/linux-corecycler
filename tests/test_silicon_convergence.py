"""Convergence properties for the autonomous search.

These are the acceptance criteria for the tuner as a search algorithm, stated
against a silicon model that has both a load limit and an idle limit per core
(see :mod:`tests.silicon`). They deliberately say nothing about phases, hunts or
internal bookkeeping: an autonomous tuner is correct when it lands every core on
its true limit without help, and incorrect when it stops and asks.
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.silicon import CoreSilicon, FakeSilicon, converged
from tests.silicon_driver import drive


@pytest.fixture(autouse=True)
def _synchronous_qtimer():
    """The driver relies on the engine's QTimer continuations firing inline.
    Real PySide6 would queue them on an idle loop and the driver would stall."""
    from unittest.mock import patch

    with patch("corecycler.tuner.engine.QTimer.singleShot", new=lambda _ms, fn: fn()):
        yield


@pytest.fixture
def db():
    from corecycler.history.db import HistoryDB

    d = HistoryDB(":memory:")
    yield d
    d.close()


def _topo(n_cores: int):
    from corecycler.engine.topology import CPUTopology, PhysicalCore

    topo = CPUTopology()
    for i in range(n_cores):
        topo.cores[i] = PhysicalCore(core_id=i, ccd=0, ccx=None, logical_cpus=(i,))
    topo.ccds = 1
    return topo


def _uniform(n_cores: int, load: int, idle: int) -> FakeSilicon:
    return FakeSilicon(cores={i: CoreSilicon(load_limit=load, idle_limit=idle) for i in range(n_cores)})


class TestLoadOnlyInstability:
    """The regime the current engine was built for: every fault is attributable."""

    def test_converges_when_idle_never_bites(self, db, mock_backend):
        silicon = FakeSilicon(
            cores={
                0: CoreSilicon(load_limit=-32, idle_limit=-60),
                1: CoreSilicon(load_limit=-27, idle_limit=-60),
                2: CoreSilicon(load_limit=-38, idle_limit=-60),
                3: CoreSilicon(load_limit=-30, idle_limit=-60),
            }
        )
        run = drive(db, _topo(4), mock_backend, silicon)
        ok, problems = converged(silicon, run.final)
        assert not run.stalled, f"engine handed back control: {run.status}"
        assert ok, problems


class TestIdleInstability:
    """The regime that produced the real dead end.

    One core is stable under load far past the point where it survives sitting
    idle with its offset live. Every per-core test passes; the machine dies when
    the whole vector is resident. A search that isolates by loading one core and
    parking the rest at stock can never see this, and must not conclude the
    vector is good.
    """

    def test_idle_limited_core_is_found(self, db, mock_backend):
        silicon = FakeSilicon(
            cores={
                0: CoreSilicon(load_limit=-40, idle_limit=-60),
                1: CoreSilicon(load_limit=-40, idle_limit=-28),
                2: CoreSilicon(load_limit=-40, idle_limit=-60),
                3: CoreSilicon(load_limit=-40, idle_limit=-60),
            }
        )
        run = drive(db, _topo(4), mock_backend, silicon)
        ok, problems = converged(silicon, run.final)
        assert not run.stalled, f"engine handed back control: {run.status}"
        assert ok, problems

    def test_innocent_cores_keep_their_offsets(self, db, mock_backend):
        """Attribution matters: only the idle-limited core may lose depth."""
        silicon = FakeSilicon(
            cores={
                0: CoreSilicon(load_limit=-40, idle_limit=-60),
                1: CoreSilicon(load_limit=-40, idle_limit=-25),
                2: CoreSilicon(load_limit=-40, idle_limit=-60),
                3: CoreSilicon(load_limit=-40, idle_limit=-60),
            }
        )
        run = drive(db, _topo(4), mock_backend, silicon)
        assert not run.stalled, f"engine handed back control: {run.status}"
        for core_id in (0, 2, 3):
            assert run.final[core_id] <= -39, f"innocent core {core_id} punished to {run.final[core_id]}"

    def test_two_idle_limited_cores_are_both_found(self, db, mock_backend):
        """Bisection must recurse rather than converge on one of two culprits."""
        silicon = FakeSilicon(
            cores={
                0: CoreSilicon(load_limit=-40, idle_limit=-60),
                1: CoreSilicon(load_limit=-40, idle_limit=-30),
                2: CoreSilicon(load_limit=-40, idle_limit=-60),
                3: CoreSilicon(load_limit=-40, idle_limit=-26),
            }
        )
        run = drive(db, _topo(4), mock_backend, silicon)
        ok, problems = converged(silicon, run.final)
        assert not run.stalled, f"engine handed back control: {run.status}"
        assert ok, problems


class TestNeverHandsBack:
    """Instability is never a reason to stop."""

    def test_unattributable_crashes_do_not_pause(self, db, mock_backend):
        silicon = _uniform(4, load=-40, idle=-24)
        run = drive(db, _topo(4), mock_backend, silicon)
        assert not run.stalled, f"engine handed back control: {run.status}"

    def test_flaky_instability_still_converges(self, db, mock_backend):
        """A marginal offset that passes twice before biting must not be trusted."""
        silicon = FakeSilicon(
            cores={
                0: CoreSilicon(load_limit=-33, idle_limit=-60, flaky_visits=3),
                1: CoreSilicon(load_limit=-40, idle_limit=-29, flaky_visits=2),
                2: CoreSilicon(load_limit=-36, idle_limit=-60, flaky_visits=3),
                3: CoreSilicon(load_limit=-31, idle_limit=-60, flaky_visits=2),
            }
        )
        run = drive(db, _topo(4), mock_backend, silicon)
        ok, problems = converged(silicon, run.final)
        assert not run.stalled, f"engine handed back control: {run.status}"
        assert ok, problems


class TestConvergenceProperty:
    @settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        load=st.lists(st.integers(min_value=-45, max_value=-20), min_size=4, max_size=4),
        idle=st.lists(st.integers(min_value=-45, max_value=-20), min_size=4, max_size=4),
    )
    def test_converges_from_any_silicon(self, db, mock_backend, load, idle):
        silicon = FakeSilicon(cores={i: CoreSilicon(load_limit=load[i], idle_limit=idle[i]) for i in range(4)})
        run = drive(db, _topo(4), mock_backend, silicon)
        ok, problems = converged(silicon, run.final)
        assert not run.stalled, f"engine handed back control: {run.status}"
        assert ok, problems


@pytest.mark.parametrize("n_cores", [2, 8])
def test_scales_with_core_count(db, mock_backend, n_cores):
    silicon = FakeSilicon(
        cores={i: CoreSilicon(load_limit=-38, idle_limit=-27 if i == n_cores - 1 else -60) for i in range(n_cores)}
    )
    run = drive(db, _topo(n_cores), mock_backend, silicon)
    ok, problems = converged(silicon, run.final)
    assert not run.stalled, f"engine handed back control: {run.status}"
    assert ok, problems
