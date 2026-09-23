"""Endurance (validation stage 9): the perpetual confirmation loop.

Stage 9 never completes a session. It runs rounds of per-core and all-core
slots over the configured workload matrix with every offset live, grows the
slot length each round, backs a core off one fine step whenever a slot fails,
and keeps a per-core evidence ledger that survives a reboot.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from unittest.mock import MagicMock

from corecycler.engine.backends.base import DutyCycle, FFTPreset, StressMode
from corecycler.history.db import HistoryDB
from corecycler.tuner import bisect
from corecycler.tuner import engine as engine_module
from corecycler.tuner import persistence as tp
from corecycler.tuner.config import TunerConfig
from corecycler.tuner.state import CoreState, TunerPhase
from tests.test_crash_attribution import (
    BASELINES,
    BEST,
    _make_engine,
    _seed_confirmed_validating,
)

ORDER = sorted(BEST)


@pytest.fixture
def db(tmp_path):
    d = HistoryDB(tmp_path / "test.db")
    yield d
    d.close()


def _seed(db, topo, backend, **cfg):
    """A session that already passed staged validation, poised at stage 9."""
    eng = _make_engine(db, topo, backend, endurance=True, **cfg)
    _seed_confirmed_validating(eng, db, BEST, BASELINES)
    for cs in eng._core_states.values():
        cs.in_test = False
        db.upsert_tuner_core_state(eng._session_id, cs)
    eng._set_status("validating")
    eng._validation_core_order = list(ORDER)
    eng.solo = []
    eng.multi = []
    eng._start_worker = lambda core, duration, *, spectrum=False, duty_cycle=None: eng.solo.append(
        (core, duration, spectrum)
    )
    eng._start_multi_core_worker = lambda cores, duration, **kw: eng.multi.append((list(cores), duration, kw))
    eng._run_validation_stage4 = lambda *a, **k: None
    return eng


def _reject_nonstock_but_restore(smu):
    original_write = smu.set_co_offset

    def write(core_id, offset):
        if offset != 0:
            return False
        return original_write(core_id, offset)

    smu.set_co_offset = write


def _at(eng, round_=0, workload=0, index=0):
    eng._validation_stage = 9
    eng._endurance_round = round_
    eng._endurance_workload = workload
    eng._endurance_index = index
    return eng


class TestEnteringEndurance:
    def test_a_clean_pass_enters_endurance_instead_of_completing(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _seed(db, topo_dual_ccd_x3d, mock_backend)
        completed: list[str] = []
        eng.session_completed.connect(completed.append)
        eng._validation_stage = 8
        eng._validation_dirty = False

        eng._run_validation_next()

        assert eng._validation_stage == 9
        assert eng.status == "validating"
        assert completed == []
        session = db.get_tuner_session(eng._session_id)
        assert session.status != "completed"
        assert session.validation_stage == 9
        assert (session.endurance_round, session.endurance_workload, session.endurance_index) == (0, 0, 0)

    def test_entering_endurance_clears_stale_unattributed_crashes(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _seed(db, topo_dual_ccd_x3d, mock_backend)
        db.set_unattributed_crashes(eng._session_id, 2)
        eng._validation_stage = 8

        eng._run_validation_next()

        assert db.get_unattributed_crashes(eng._session_id) == 0


class TestSlotDispatch:
    def test_a_solo_slot_stresses_one_core_with_every_offset_live(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _at(_seed(db, topo_dual_ccd_x3d, mock_backend), index=3)

        eng._run_validation_next()

        assert eng.solo == [(ORDER[3], 600, False)]
        assert eng.multi == []
        assert eng._cores_under_stress == [ORDER[3]]
        assert [c for c, cs in eng._core_states.items() if cs.in_test] == [ORDER[3]]
        # all offsets stay live: nobody was written back to baseline
        assert eng._smu.written == {c: BEST[c] for c in ORDER}

    def test_spectrum_solo_dispatch_uses_the_selected_workload(
        self, db, topo_dual_ccd_x3d, mock_backend, monkeypatch, tmp_path
    ):
        eng = _at(_seed(db, topo_dual_ccd_x3d, mock_backend), index=1)
        workload = {
            **eng._config.endurance_workloads[0],
            "stress_mode": "AVX2",
            "fft_preset": "LARGE",
            "threads": 1,
            "profile": "spectrum",
        }
        eng._config.endurance_workloads[0] = workload
        eng._work_dir = tmp_path
        eng._start_worker = engine_module.TunerEngine._start_worker.__get__(eng)

        class ParkedWorker:
            def __init__(self, _core_id, _logical_cpu, scheduler, **_kwargs):
                self.scheduler = scheduler
                self.finished = MagicMock()
                self.started = False

            def start(self):
                self.started = True

        monkeypatch.setattr(engine_module, "_TunerWorker", ParkedWorker)
        monkeypatch.setattr(eng, "_start_freeze_monitor", lambda: None)

        eng._run_validation_next()

        worker = eng._worker
        assert worker.started is True
        assert worker.scheduler.stress_config.mode is StressMode.AVX2
        assert worker.scheduler.stress_config.fft_preset is FFTPreset.LARGE
        assert worker.scheduler.stress_config.threads == 1
        assert worker.scheduler.config.variable_load is True
        assert worker.scheduler.config.idle_stability_test > 0
        assert eng._worker_profile == "spectrum"

    def test_the_all_core_slot_follows_the_solo_slots(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _at(_seed(db, topo_dual_ccd_x3d, mock_backend), index=len(ORDER))

        eng._run_validation_next()

        cores, duration, kwargs = eng.multi[0]
        assert cores == ORDER
        assert duration == 600
        assert kwargs["workload"] == eng._config.endurance_workloads[0]
        assert eng._cores_under_stress == ORDER

    def test_a_refused_offset_write_launches_nothing(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _at(_seed(db, topo_dual_ccd_x3d, mock_backend), index=2)
        _reject_nonstock_but_restore(eng._smu)

        eng._run_validation_next()

        assert eng.solo == []
        assert eng.status == "paused"

    def test_a_refused_offset_write_before_the_all_core_slot_launches_nothing(
        self, db, topo_dual_ccd_x3d, mock_backend
    ):
        eng = _at(_seed(db, topo_dual_ccd_x3d, mock_backend), index=len(ORDER))
        _reject_nonstock_but_restore(eng._smu)

        eng._run_validation_next()

        assert eng.multi == []
        assert eng.status == "paused"


class TestAllCoreWorkloadLaunch:
    """The all-core slot builds its stress config from the workload itself, so
    a 2-thread AVX2 slot is what actually runs, not the session's base load."""

    def _engine(self, db, topo, backend):
        eng = _seed(db, topo, backend)
        del eng._start_multi_core_worker  # the real one, not the recorder
        return eng

    def test_the_workload_shapes_the_stress_config(self, db, topo_dual_ccd_x3d, mock_backend, monkeypatch):
        eng = self._engine(db, topo_dual_ccd_x3d, mock_backend)
        worker = MagicMock()
        monkeypatch.setattr(engine_module, "_ParallelWorker", MagicMock(return_value=worker))
        runners = []
        monkeypatch.setattr(engine_module, "ParallelStress", lambda **kw: runners.append(kw) or MagicMock())

        eng._start_multi_core_worker(
            ORDER,
            600,
            workload={
                "regime": "current",
                "backend": "mprime",
                "stress_mode": "AVX2",
                "fft_preset": "LARGE",
                "threads": 1,
                "profile": "sustained",
            },
        )

        stress_config = runners[0]["stress_config"]
        assert stress_config.mode is StressMode.AVX2
        assert stress_config.fft_preset is FFTPreset.LARGE
        assert stress_config.threads == 1
        assert runners[0]["backend"] is eng._backend
        assert worker.start.called

    def test_a_memory_coupled_workload_reaches_every_lane(self, db, topo_dual_ccd_x3d, mock_backend, monkeypatch):
        eng = self._engine(db, topo_dual_ccd_x3d, mock_backend)
        monkeypatch.setattr(engine_module, "_ParallelWorker", MagicMock(return_value=MagicMock()))
        runners = []
        monkeypatch.setattr(engine_module, "ParallelStress", lambda **kw: runners.append(kw) or MagicMock())

        eng._start_multi_core_worker(
            ORDER,
            600,
            workload={
                "regime": "coupled",
                "backend": "mprime",
                "stress_mode": "AVX2",
                "fft_preset": "LARGE",
                "threads": 2,
                "profile": "sustained",
                "memory_coupled": True,
            },
        )

        assert runners[0]["stress_config"].memory_coupled is True

    def test_an_unknown_workload_mode_is_an_apparatus_fault(self, db, topo_dual_ccd_x3d, mock_backend, monkeypatch):
        eng = self._engine(db, topo_dual_ccd_x3d, mock_backend)
        failed = []
        monkeypatch.setattr(eng, "_fail_test_async", lambda cid, msg: failed.append((cid, msg)))

        eng._start_multi_core_worker(
            ORDER, 600, workload={"backend": "mprime", "stress_mode": "AVX9", "fft_preset": "SMALL"}
        )

        assert failed[0][0] == ORDER[0]
        assert eng._worker is None


class TestWorkloadSelection:
    def test_stage9_reports_the_current_workload(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _at(_seed(db, topo_dual_ccd_x3d, mock_backend))
        workload = eng._config.endurance_workloads[0]
        assert eng._get_active_stress_config(eng._core_states[0]) == (
            workload["backend"],
            workload["stress_mode"],
            workload["fft_preset"],
            workload.get("threads"),
        )

    def test_default_endurance_matrix_is_the_regime_battery(self):
        config = TunerConfig()
        assert config.endurance_workloads == config.battery
        assert all(workload.get("regime") for workload in config.endurance_workloads)

    def test_a_requeued_solo_retest_uses_the_base_workload(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _at(_seed(db, topo_dual_ccd_x3d, mock_backend))
        eng._in_requeue = True
        cfg = eng._config
        assert eng._get_active_stress_config(eng._core_states[0]) == (
            cfg.backend,
            cfg.stress_mode,
            cfg.fft_preset,
            None,
        )

    def test_threads_are_clamped_to_the_cores_smt_width(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _seed(db, topo_dual_ccd_x3d, mock_backend)
        width = len(topo_dual_ccd_x3d.cores[0].logical_cpus)
        assert eng._threads_for(0, None) == width
        assert eng._threads_for(0, 1) == 1
        assert eng._threads_for(0, 8) == width
        assert eng._threads_for(999, 4) == 1  # unknown core never asks for SMT it has not got

    def test_hunt_replays_the_persisted_endurance_workload_and_duration(
        self, db, topo_dual_ccd_x3d, mock_backend, monkeypatch, tmp_path
    ):
        workloads = [dict(workload) for workload in TunerConfig().endurance_workloads]
        workloads[1] = {
            **workloads[1],
            "backend": "mprime",
            "stress_mode": "AVX2",
            "fft_preset": "LARGE",
            "threads": 1,
            "profile": "spectrum",
        }
        eng = _seed(
            db,
            topo_dual_ccd_x3d,
            mock_backend,
            endurance_workloads=workloads,
        )
        eng._work_dir = tmp_path
        eng._start_worker = engine_module.TunerEngine._start_worker.__get__(eng)
        eng._config.backend = "mock"
        db.set_validation_position(eng._session_id, 9, 0, 0, False, "[]")
        db.set_endurance_position(eng._session_id, 1, 1, 2)
        eng._validation_stage = 0
        eng._endurance_round = 0
        eng._endurance_workload = 0
        eng._endurance_index = 0
        monkeypatch.setattr(eng, "_has_stage1_pass_at_current_best", lambda _core: True)

        class ParkedWorker:
            def __init__(self, _core_id, _logical_cpu, scheduler, **_kwargs):
                self.scheduler = scheduler
                self.finished = MagicMock()
                self.started = False

            def start(self):
                self.started = True

        monkeypatch.setattr(engine_module, "_TunerWorker", ParkedWorker)
        monkeypatch.setattr(eng, "_start_freeze_monitor", lambda: None)

        session = db.get_tuner_session(eng._session_id)
        eng._enter_auto_validation(dict(BEST), resume_from=session)

        assert (eng._endurance_round, eng._endurance_workload, eng._endurance_index) == (1, 1, 2)
        checkpoint = bisect.HuntState.from_json(db.get_tuner_session(eng._session_id).hunt_state)
        assert checkpoint is not None
        assert checkpoint.workload is not None
        assert {
            key: checkpoint.workload[key] for key in ("backend", "stress_mode", "fft_preset", "threads", "profile")
        } == {
            "backend": "mprime",
            "stress_mode": "AVX2",
            "fft_preset": "LARGE",
            "threads": 1,
            "profile": "spectrum",
        }
        assert checkpoint.workload["duration_seconds"] == 1200

        eng._worker = None
        eng._hunt = checkpoint
        eng._hunting = True
        eng._set_status("hunting")
        eng._run_next_hunt_slot()

        worker = eng._worker
        assert worker.started is True
        assert worker.scheduler.backend.name == "mprime"
        assert worker.scheduler.stress_config.mode is StressMode.AVX2
        assert worker.scheduler.stress_config.fft_preset is FFTPreset.LARGE
        assert worker.scheduler.stress_config.threads == 1
        assert worker.scheduler.config.variable_load is True
        assert worker.scheduler.config.seconds_per_core == max(1200, eng._config.probe_base_seconds)
        assert eng._worker_profile == "spectrum"

    def test_transient_slot_reaches_the_scheduler_as_a_duty_cycled_workload(
        self, db, topo_dual_ccd_x3d, mock_backend, monkeypatch, tmp_path
    ):
        eng = _at(_seed(db, topo_dual_ccd_x3d, mock_backend))
        eng._work_dir = tmp_path
        eng._start_worker = engine_module.TunerEngine._start_worker.__get__(eng)
        transient = next(workload for workload in eng._config.battery if workload["regime"] == "transient")
        eng._config.endurance_workloads[0] = dict(transient)

        class ParkedWorker:
            def __init__(self, _core_id, _logical_cpu, scheduler, **_kwargs):
                self.scheduler = scheduler
                self.finished = MagicMock()
                self.started = False

            def start(self):
                self.started = True

        monkeypatch.setattr(engine_module, "_TunerWorker", ParkedWorker)
        monkeypatch.setattr(eng, "_start_freeze_monitor", lambda: None)
        duty_cycle = DutyCycle(random_phases=True)

        eng._start_worker(0, 30, duty_cycle=duty_cycle)

        workload = eng._config.endurance_workloads[0]
        worker = eng._worker
        assert worker.started is True
        assert worker.scheduler.stress_config.mode is StressMode[workload["stress_mode"]]
        assert worker.scheduler.stress_config.fft_preset is FFTPreset[workload["fft_preset"]]
        assert worker.scheduler.stress_config.duty_cycle == duty_cycle
        assert worker.scheduler.config.duty_cycle == duty_cycle

    def test_unknown_persisted_workload_values_fall_back_to_safe_stress_defaults(
        self, db, topo_dual_ccd_x3d, mock_backend, monkeypatch, tmp_path
    ):
        eng = _at(_seed(db, topo_dual_ccd_x3d, mock_backend))
        eng._work_dir = tmp_path
        eng._start_worker = engine_module.TunerEngine._start_worker.__get__(eng)
        eng._hunt = bisect.begin(ORDER, [0])
        eng._hunt.vector = dict(BEST)
        eng._hunt.workload = {
            "regime": "current",
            "backend": "mprime",
            "stress_mode": "unknown-mode",
            "fft_preset": "unknown-preset",
            "threads": 1,
            "profile": "sustained",
            "duration_seconds": 30,
        }
        eng._hunt.armed = False
        eng._hunting = True
        eng._set_status("hunting")

        class ParkedWorker:
            def __init__(self, _core_id, _logical_cpu, scheduler, **_kwargs):
                self.scheduler = scheduler
                self.finished = MagicMock()

            def start(self):
                pass

        monkeypatch.setattr(engine_module, "_TunerWorker", ParkedWorker)
        monkeypatch.setattr(eng, "_start_freeze_monitor", lambda: None)

        eng._run_next_hunt_slot()

        assert eng._worker.scheduler.stress_config.mode is StressMode.SSE
        assert eng._worker.scheduler.stress_config.fft_preset is FFTPreset.SMALL


class TestVerdicts:
    def test_a_failed_solo_slot_backs_the_core_off_and_retries_the_slot(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _at(_seed(db, topo_dual_ccd_x3d, mock_backend), index=5)
        core = ORDER[5]
        before = eng._core_states[core].best_offset

        eng._on_validation_test_finished(core, passed=False)

        assert eng._core_states[core].best_offset == before + 1  # exactly one fine step
        assert eng._endurance_index == 5  # same slot retries
        assert eng._validation_stage == 9
        assert eng._validation_dirty is True
        assert eng._validation_requeue == []

    def test_a_failed_all_core_slot_requeues_the_reported_lane(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _at(_seed(db, topo_dual_ccd_x3d, mock_backend), index=len(ORDER))
        before = eng._core_states[5].best_offset

        eng._on_validation_test_finished(5, passed=False)

        assert eng._core_states[5].best_offset == before + 1
        assert eng._validation_requeue == [5]
        assert eng._endurance_index == len(ORDER)  # the slot reruns, unchanged
        assert eng._validation_stage == 9

    def test_the_requeued_retest_returns_to_endurance(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _at(_seed(db, topo_dual_ccd_x3d, mock_backend), index=len(ORDER))
        eng._validation_requeue = [5]
        eng._in_requeue = True

        eng._on_validation_test_finished(5, passed=True)

        assert eng._validation_requeue == []
        assert eng._validation_stage == 9
        assert eng._endurance_index == len(ORDER)

    def test_a_passed_solo_slot_advances_and_persists_the_cursor(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _at(_seed(db, topo_dual_ccd_x3d, mock_backend), round_=1, workload=2, index=4)

        eng._on_validation_test_finished(ORDER[4], passed=True)

        assert eng._endurance_index == 5
        session = db.get_tuner_session(eng._session_id)
        assert (session.endurance_round, session.endurance_workload, session.endurance_index) == (1, 2, 5)


class TestRounds:
    def test_slot_length_doubles_each_round_up_to_the_cap(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _seed(db, topo_dual_ccd_x3d, mock_backend)
        lengths = []
        for round_ in range(5):
            eng._endurance_round = round_
            lengths.append(eng._endurance_duration())
        assert lengths == [600, 1200, 2400, 3600, 3600]
        eng._endurance_round = 4000  # a very long-lived session must not overflow
        assert eng._endurance_duration() == 3600

    def test_a_finished_round_restarts_the_matrix_with_longer_slots(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _seed(db, topo_dual_ccd_x3d, mock_backend)
        _at(eng, workload=len(eng._config.endurance_workloads))

        eng._run_validation_next()

        assert (eng._endurance_round, eng._endurance_workload, eng._endurance_index) == (1, 0, 0)
        assert eng._validation_dirty is False
        assert eng._endurance_duration() == 1200
        session = db.get_tuner_session(eng._session_id)
        assert (session.endurance_round, session.endurance_workload) == (1, 0)

    def test_a_round_boundary_hands_a_fully_banked_core_to_annealing(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _seed(db, topo_dual_ccd_x3d, mock_backend)
        _at(eng, workload=len(eng._config.endurance_workloads))
        session = db.get_tuner_session(eng._session_id)
        assert session is not None
        assert session.context_id is not None
        eng._config.anneal_bank_hours = 1.0
        candidate = ORDER[0]
        for regime in {workload["regime"] for workload in eng._config.battery}:
            db.bank_regime_time(session.context_id, candidate, regime, BEST[candidate], 3600.0)

        eng._run_validation_next()

        cs = eng._core_states[candidate]
        assert cs.phase is TunerPhase.ANNEALING
        assert cs.current_offset == BEST[candidate] + eng._config.direction
        assert eng._validation_stage == 0
        assert eng.status == "running"
        assert db.get_tuner_session(eng._session_id).status == "running"

    def test_a_clean_round_clears_unattributed_crashes(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _seed(db, topo_dual_ccd_x3d, mock_backend)
        _at(eng, workload=len(eng._config.endurance_workloads))
        db.set_unattributed_crashes(eng._session_id, 2)
        eng._validation_dirty = False

        eng._run_validation_next()

        assert db.get_unattributed_crashes(eng._session_id) == 0

    def test_a_round_with_backoffs_keeps_the_unexplained_incident_count(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _seed(db, topo_dual_ccd_x3d, mock_backend)
        _at(eng, workload=len(eng._config.endurance_workloads))
        db.set_unattributed_crashes(eng._session_id, 2)
        eng._validation_dirty = True

        eng._run_validation_next()

        assert db.get_unattributed_crashes(eng._session_id) == 2

    def test_a_finished_round_reports_each_cores_evidence(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _seed(db, topo_dual_ccd_x3d, mock_backend)
        _at(eng, workload=len(eng._config.endurance_workloads))
        lines: list[str] = []
        eng.log_message.connect(lines.append)
        db.insert_tuner_test_log(
            eng._session_id,
            0,
            BEST[0],
            "endurance",
            True,
            duration=3600.0,
            backend="mprime",
            stress_mode="AVX2",
            fft_preset="SMALL",
            threads=2,
        )

        eng._run_validation_next()

        evidence = [line for line in lines if "live evidence" in line]
        assert len(evidence) == len(BEST)
        assert f"core 0 @ {BEST[0]}: 1.0h live evidence (mprime AVX2 SMALL 2T 1.0h)" in evidence
        assert f"core 1 @ {BEST[1]}: 0.0h live evidence (none yet)" in evidence


class TestTestLogRows:
    def test_an_endurance_result_records_its_regime_workload(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _at(_seed(db, topo_dual_ccd_x3d, mock_backend), index=0)
        workload = eng._config.endurance_workloads[0]

        eng._on_test_finished(ORDER[0], True, "", "", 600.0, 0.0, "", "")

        row = db.get_tuner_test_log(eng._session_id, core_id=ORDER[0])[-1]
        assert row["phase"] == "endurance"
        assert (row["backend"], row["stress_mode"], row["fft_preset"]) == (
            workload["backend"],
            workload["stress_mode"],
            workload["fft_preset"],
        )
        assert row["threads"] == workload["threads"]
        assert row["profile"] == workload["profile"]
        assert row["regime"] == workload["regime"]
        assert row["duration_seconds"] == 600.0

    def test_a_clean_all_core_slot_banks_every_live_lane(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _at(_seed(db, topo_dual_ccd_x3d, mock_backend), index=len(ORDER))
        session = db.get_tuner_session(eng._session_id)
        assert session is not None
        assert session.context_id is not None
        eng._cores_under_stress = list(ORDER)
        eng._co_applied = dict(BEST)
        regime = eng._config.endurance_workloads[0]["regime"]

        lane_results = json.dumps([{"core": core_id, "passed": True, "duration": 600.0} for core_id in ORDER])
        eng._on_test_finished(ORDER[0], True, "", "", 600.0, 0.0, "", lane_results)

        for core_id in ORDER:
            assert db.get_regime_banks(session.context_id, core_id, BEST[core_id]) == {regime: 600.0}


class TestResume:
    def _persist(self, eng, db, round_, workload, index):
        db.set_validation_position(eng._session_id, 9, 0, 0, False, "[]")
        db.set_endurance_position(eng._session_id, round_, workload, index)
        return db.get_tuner_session(eng._session_id)

    def _log_solo_passes(self, eng, db, skip=()):
        for core, offset in BEST.items():
            if core in skip:
                continue
            db.insert_tuner_test_log(eng._session_id, core, offset, "validate_s1", True, duration=300.0)

    def test_a_reboot_resumes_the_persisted_endurance_cursor(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _seed(db, topo_dual_ccd_x3d, mock_backend)
        session = self._persist(eng, db, 2, 1, 4)
        self._log_solo_passes(eng, db)

        eng._enter_auto_validation(dict(BEST), resume_from=session)

        assert eng._validation_stage == 9
        assert (eng._endurance_round, eng._endurance_workload, eng._endurance_index) == (2, 1, 4)
        assert eng._validation_requeue == []

    def test_a_core_without_a_live_solo_pass_is_retested_first(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _seed(db, topo_dual_ccd_x3d, mock_backend)
        session = self._persist(eng, db, 2, 1, 4)
        self._log_solo_passes(eng, db, skip=(3,))

        eng._enter_auto_validation(dict(BEST), resume_from=session)

        assert eng._validation_requeue == [3]
        assert eng._validation_stage == 9

    def test_an_endurance_pass_counts_as_that_live_solo_evidence(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _seed(db, topo_dual_ccd_x3d, mock_backend)
        session = self._persist(eng, db, 2, 1, 4)
        self._log_solo_passes(eng, db, skip=(3,))
        db.insert_tuner_test_log(eng._session_id, 3, BEST[3], "endurance", True, duration=600.0)

        eng._enter_auto_validation(dict(BEST), resume_from=session)

        assert eng._validation_requeue == []

    def test_a_corrupt_cursor_is_clamped_to_the_current_matrix(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _seed(db, topo_dual_ccd_x3d, mock_backend)
        session = self._persist(eng, db, -1, 99, 99)
        self._log_solo_passes(eng, db)
        eng._run_validation_next = lambda: None  # restoration only, no dispatch

        eng._enter_auto_validation(dict(BEST), resume_from=session)

        assert eng._endurance_round == 0
        assert eng._endurance_workload == len(eng._config.endurance_workloads)
        assert eng._endurance_index == len(ORDER)

    def test_a_fresh_validation_entry_zeroes_the_cursor(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _seed(db, topo_dual_ccd_x3d, mock_backend)
        _at(eng, round_=3, workload=2, index=4)

        eng._enter_auto_validation(dict(BEST))

        assert (eng._endurance_round, eng._endurance_workload, eng._endurance_index) == (0, 0, 0)
        session = db.get_tuner_session(eng._session_id)
        assert (session.endurance_round, session.endurance_workload, session.endurance_index) == (0, 0, 0)


class TestEvidenceLedger:
    """Only live-mask passes at least as aggressive as the current best count;
    hunt probes remain attribution evidence, not profile confidence."""

    def _rows(self, db, sid):
        common = dict(backend="mprime", stress_mode="AVX2", fft_preset="SMALL")
        db.insert_tuner_test_log(sid, 0, -41, "validate_s1", True, duration=600.0, threads=2, **common)
        db.insert_tuner_test_log(sid, 0, -40, "endurance", True, duration=600.0, threads=1, **common)
        db.insert_tuner_test_log(
            sid,
            0,
            -41,
            "coarse",
            True,
            duration=300.0,
            threads=2,
            regime="current",
            **common,
        )
        db.insert_tuner_test_log(sid, 0, -41, "hunt", True, duration=300.0, threads=2, **common)
        db.insert_tuner_test_log(sid, 0, -40, "endurance", False, duration=300.0, threads=2, **common)
        db.insert_tuner_test_log(sid, 0, -38, "validate_s1", True, duration=600.0, threads=2, **common)
        db.insert_tuner_test_log(sid, 0, -40, "endurance", True, duration=None, threads=2, **common)

    def test_only_live_passes_at_the_current_offset_count(self, db):
        sid = db.create_tuner_session(TunerConfig().to_json(), "", "")
        self._rows(db, sid)
        states = {0: CoreState(core_id=0, best_offset=-40)}

        summary = tp.evidence_summary(db, sid, states, direction=-1)

        two_thread = tp.workload_label("mprime", "AVX2", "SMALL", 2, None)
        one_thread = tp.workload_label("mprime", "AVX2", "SMALL", 1, None)
        assert summary == {0: {two_thread: 900.0, one_thread: 600.0}}

    def test_a_core_without_a_best_offset_falls_back_to_its_baseline(self, db):
        sid = db.create_tuner_session(TunerConfig().to_json(), "", "")
        db.insert_tuner_test_log(
            sid, 1, -5, "endurance", True, duration=1800.0, backend="mprime", stress_mode="SSE", fft_preset="SMALL"
        )
        states = {1: CoreState(core_id=1, best_offset=None, baseline_offset=-5)}

        summary = tp.evidence_summary(db, sid, states, direction=-1)

        line = tp.format_evidence_line(1, None, summary[1])
        assert line == "core 1 @ n/a: 0.5h live evidence (mprime SSE SMALL 0.5h)"

    def test_rows_for_unknown_cores_are_ignored(self, db):
        sid = db.create_tuner_session(TunerConfig().to_json(), "", "")
        db.insert_tuner_test_log(sid, 9, -5, "endurance", True, duration=60.0, backend="mprime")

        assert tp.evidence_summary(db, sid, {}, direction=-1) == {}
        db.close()

    def test_a_spectrum_slot_is_labelled_apart_from_sustained_stress(self):
        assert tp.workload_label("mprime", "SSE", "SMALL", None, "spectrum") == "mprime SSE SMALL spectrum"
        assert tp.workload_label("mprime", "SSE", "SMALL", 2, "transitions") == "mprime SSE SMALL 2T transitions"
        assert tp.workload_label("mprime", "SSE", "SMALL", None, "sustained") == "mprime SSE SMALL"
