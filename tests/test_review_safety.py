from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock

import pytest

from corecycler.engine import execution
from corecycler.engine.backends.base import StressResult
from corecycler.engine.detector import ErrorDetector
from corecycler.engine.execution import Lane, _LaneRun
from corecycler.history.db import HistoryDB
from corecycler.tuner import engine as engine_mod
from corecycler.tuner import persistence as tp
from corecycler.tuner.config import TunerConfig
from corecycler.tuner.regime import Mask
from corecycler.tuner.state import CoreState, TunerPhase
from tests.test_tuner_faults import FaultSMU, make_engine


@pytest.fixture
def db():
    database = HistoryDB(":memory:")
    yield database
    database.close()


@pytest.fixture
def tuning(db, topo_single_ccd, mock_backend, monkeypatch):
    eng = make_engine(db, topo_single_ccd, FaultSMU(), mock_backend, cores_to_test=[0, 1])
    eng._session_id = db.create_tuner_session(eng._config.to_json(), "", "")
    eng._status = "running"
    eng._core_states = {
        c: CoreState(core_id=c, phase=TunerPhase.CONFIRMED, current_offset=-20, best_offset=-20) for c in (0, 1)
    }
    for state in eng._core_states.values():
        db.upsert_tuner_core_state(eng.session_id, state)
    eng._co_applied = {0: 0, 1: 0}
    monkeypatch.setattr(engine_mod.QTimer, "singleShot", lambda *_: None)
    monkeypatch.setattr(eng, "_run_next", lambda: None)
    monkeypatch.setattr(eng, "_run_validation_next", lambda: None)
    monkeypatch.setattr(eng, "_start_worker", lambda *_: None)
    return eng


def test_failed_readback_cannot_skip_abort_restoration(tuning, monkeypatch):
    smu = tuning._smu

    def partially_verified(core, value):
        smu.applied[core] = value
        smu.writes.append((core, value))
        return value == 0

    monkeypatch.setattr(smu, "set_co_offset", partially_verified)

    assert not tuning._apply_co_mask(0, -10, Mask.ISOLATED)
    assert smu.applied[0] == 0
    assert (0, 0) in smu.writes

    # An abort must not trust the write cache: hardware can diverge after the
    # failed operation and still needs an unconditional baseline write.
    assert tuning._co_applied[0] == 0
    smu.applied[0] = -10
    writes_before_abort = len(smu.writes)

    tuning.abort()

    assert smu.applied[0] == 0
    assert (0, 0) in smu.writes[writes_before_abort:]


@pytest.mark.parametrize("action", ["start", "resume", "validate_profile"])
def test_dry_run_never_starts_a_tuning_session(tuning, action):
    tuning._smu.dry_run = True
    tuning._status = "idle"
    method = getattr(tuning, action)
    method() if action == "start" else method(tuning.session_id)
    assert tuning.status == "idle"
    assert tuning._smu.writes == []


def test_dry_run_enabled_after_preflight_cannot_journal_a_fake_write(tuning):
    tuning._smu.dry_run = True
    with pytest.raises(RuntimeError, match="Dry Run"):
        tuning._write_co_verified(0, -30)
    assert tp.journal_values(tuning._db, tuning.session_id) == {}


def test_failure_at_time_limit_is_not_confirmation(tuning):
    cs = tuning._core_states[0]
    cs.phase = TunerPhase.CONFIRMING
    cs.cumulative_test_time = 1799
    tuning._config.max_core_time_seconds = 1800
    tuning._config.max_confirm_retries = 0
    tuning._on_test_finished(0, False, "rounding error", "computation", 2.0, 0.0)
    assert tuning.status == "paused"
    assert cs.phase == TunerPhase.FAILED_CONFIRM
    assert tuning._db.get_tuner_test_log(tuning.session_id, core_id=0)[-1]["passed"] == 0


def test_crash_invalidates_a_contradicted_pass_bound(tuning):
    cs = tuning._core_states[0]
    cs.backoff_pass_bound = -20
    tuning._apply_crash_penalty(cs)
    assert cs.backoff_pass_bound is None
    assert cs.backoff_fail_bound == -20
    assert cs.current_offset > -20


def test_paused_validation_crash_does_not_blame_the_loaded_core(tuning):
    db, sid = tuning._db, tuning.session_id
    tuning._core_states[0].in_test = True
    db.upsert_tuner_core_state(sid, tuning._core_states[0])
    db.set_validation_position(sid, 5, 0, 0, False, "[]")
    db.update_tuner_session_status(sid, "paused")
    crashed, hunt = tuning._attribute_crash_after_reboot(db.get_tuner_session(sid))
    assert crashed == []
    assert hunt
    assert all(cs.crash_count == 0 for cs in tuning.core_states.values())


def test_thermal_stop_cannot_discard_foreign_mce_or_clean_pass_debt(tuning):
    db, sid = tuning._db, tuning.session_id
    tuning._status = "validating"
    tuning._validation_stage = 5
    tuning._co_applied[1] = -20
    db.set_validation_position(sid, 5, 0, 0, False, "[]")
    cpu = tuning._topology.cores[1].logical_cpus[0]
    events = json.dumps([{"cpu": cpu, "corrected": True, "message": "hardware error"}])
    tuning._on_test_finished(0, False, "temperature limit", "thermal", 2.0, 0.0, events)
    assert tuning.core_states[1].current_offset == -19
    assert tuning.core_states[1].phase == TunerPhase.BACKOFF_PRECONFIRM
    assert db.get_tuner_session(sid).validation_dirty
    assert tuning._validation_stage == 0


def test_recovery_penalty_preserves_the_need_for_a_full_clean_pass(tuning):
    db, sid = tuning._db, tuning.session_id
    db.set_validation_position(sid, 5, 0, 0, False, "[]")
    stale_session = db.get_tuner_session(sid)
    tuning._apply_crash_penalty(tuning.core_states[0])
    tuning._enter_auto_validation({0: -17, 1: -20}, resume_from=stale_session)
    assert tuning._validation_dirty
    assert db.get_tuner_session(sid).validation_dirty


def test_explicit_profile_validation_includes_staged_validation(tuning):
    tuning.validate_profile(tuning.session_id)
    for core_id in tuning.core_states:
        tuning._advance_core(core_id, True)
    tuning._complete_session()
    assert tuning.status == "validating"
    assert tuning._validation_stage == 1
    assert tuning._db.get_tuner_session(tuning.session_id).status != "completed"


def test_resume_resolves_the_saved_backend(tuning, monkeypatch):
    config = TunerConfig(backend="stress-ng", cores_to_test=[0])
    sid = tuning._db.create_tuner_session(config.to_json(), "", "")
    tuning._db.upsert_tuner_core_state(sid, CoreState(core_id=0))
    backend = MagicMock()
    backend.name = "stress-ng"
    backend.is_available.return_value = True
    monkeypatch.setattr(engine_mod, "get_backend", lambda name: backend if name == "stress-ng" else None)
    tuning.resume(sid)
    assert tuning._get_backend_for_name("stress-ng") is backend
    assert tuning._config.backend == "stress-ng"


def test_transition_stage_refuses_missing_sensor_and_honors_temperature_limit(tuning, monkeypatch):
    schedulers = []

    def worker(core, cpu, scheduler, *args, **kwargs):
        schedulers.append(scheduler)
        return MagicMock()

    tuning._validation_core_order = [0, 1]
    tuning._config.max_temperature_c = 80
    tuning._config.over_temp_grace_seconds = 0
    tuning._config.over_temp_hard_margin_c = 0
    monkeypatch.setattr(engine_mod, "_RapidTransitionWorker", worker)
    tuning._run_validation_stage4()
    monkeypatch.setattr(execution, "read_cpu_temperature", lambda: None)
    assert not schedulers[0]._new_thermal().safe()
    monkeypatch.setattr(execution, "read_cpu_temperature", lambda: 81)
    assert not schedulers[0]._new_thermal().safe()


def test_soak_cannot_pass_without_readable_forensics(monkeypatch):
    monkeypatch.setattr("corecycler.engine.detector.subprocess.run", MagicMock(side_effect=PermissionError("denied")))
    worker = engine_mod._SoakWorker(0, 0)
    results = []
    worker.finished.connect(lambda *args: results.append(args))
    worker.run()
    assert results[0][1] is False
    assert results[0][3] == "startup"


@pytest.mark.parametrize("unreadable", [False, True])
def test_final_detector_drain_overrides_a_pending_pass(tmp_path, monkeypatch, unreadable):
    detector = ErrorDetector()
    detector._dmesg_baseline_ts = 100.0
    detector._last_dmesg_time = float("inf")
    result = MagicMock(returncode=1 if unreadable else 0, stderr="denied", stdout="101.0 mce: CPU 0 Bank 1: error\n")
    monkeypatch.setattr("corecycler.engine.detector.subprocess.run", lambda *a, **kw: result)
    supervisor = execution.Supervisor(
        backend=MagicMock(), detector=detector, thermal=MagicMock(), stop_event=threading.Event(), observed=[]
    )
    run = _LaneRun(lane=Lane(0, (0,), tmp_path), verdict=StressResult(0, True, 10.0))
    supervisor._finish([run], 0.0, 10.0)
    assert not run.verdict.passed
    assert run.verdict.error_type == ("startup" if unreadable else "mce")


@pytest.mark.parametrize("error_type", ["startup", "thermal"])
def test_environment_fault_does_not_erase_same_core_hardware_evidence(tuning, error_type):
    tuning._co_applied[0] = -20
    cpu = tuning._topology.cores[0].logical_cpus[0]
    events = json.dumps([{"cpu": cpu, "corrected": True, "message": "hardware error"}])
    tuning._on_test_finished(0, False, "environment fault", error_type, 2.0, 0.0, events)
    assert tuning.core_states[0].current_offset == -19
    assert tuning.core_states[0].phase == TunerPhase.BACKOFF_PRECONFIRM


@pytest.mark.parametrize("phase", [TunerPhase.CONFIRMING, TunerPhase.BACKOFF_PRECONFIRM, TunerPhase.BACKOFF_CONFIRMING])
def test_baseline_failure_never_becomes_a_confirmed_profile(tuning, phase):
    cs = tuning.core_states[0]
    cs.phase = phase
    cs.current_offset = cs.best_offset = cs.baseline_offset = -20
    tuning._advance_core(0, False)
    assert tuning.status == "paused"
    assert cs.phase is not TunerPhase.CONFIRMED


def test_only_live_mask_phases_count_as_stability_evidence():
    search_phases = {"coarse", "fine", "confirm", "backoff_preconfirm", "backoff_confirm"}
    assert search_phases <= tp.LIVE_EVIDENCE_PHASES
    assert "hunt" not in tp.LIVE_EVIDENCE_PHASES


@pytest.mark.parametrize("missing", ["unknown", "uninstalled"])
def test_resume_refuses_unavailable_saved_backend(tuning, monkeypatch, missing):
    config = TunerConfig(backend="stress-ng", cores_to_test=[0])
    sid = tuning._db.create_tuner_session(config.to_json(), "", "")
    tuning._db.upsert_tuner_core_state(sid, CoreState(core_id=0))
    tuning._status = "idle"
    backend = MagicMock()
    backend.is_available.return_value = False

    def resolve(name):
        if missing == "unknown":
            raise KeyError(name)
        return backend

    monkeypatch.setattr(engine_mod, "get_backend", resolve)
    tuning.resume(sid)
    assert tuning.status == "idle"
    assert tuning._smu.writes == []


@pytest.mark.parametrize("phase", [TunerPhase.BACKOFF_PRECONFIRM, TunerPhase.BACKOFF_CONFIRMING])
def test_backoff_floor_requires_confirmation_after_failed_probe(tuning, phase):
    tuning._config.fine_step = 3
    tuning._config.midpoint_jump_threshold = 1
    cs = tuning.core_states[0]
    cs.phase = phase
    cs.current_offset = cs.best_offset = -22
    cs.backoff_pass_bound = -20
    cs.backoff_fail_bound = -25
    tuning._advance_core(0, False)
    assert cs.current_offset == -20
    assert cs.phase == TunerPhase.BACKOFF_CONFIRMING
    tuning._advance_core(0, True)
    assert cs.phase == TunerPhase.CONFIRMED


def test_unparsable_kernel_timestamp_refuses_an_observation_window(monkeypatch):
    result = MagicMock(returncode=0, stdout="[..] invalid timestamp", stderr="")
    monkeypatch.setattr("corecycler.engine.detector.subprocess.run", lambda *a, **kw: result)
    with pytest.raises(RuntimeError, match="baseline"):
        ErrorDetector().reset()


def test_idle_observation_failure_is_not_a_stability_verdict():
    detector = ErrorDetector()
    message = execution.watch_idle(
        cpus=(0,),
        duration=0,
        thermal=MagicMock(),
        detector=detector,
        stop_event=threading.Event(),
        observed=[],
        phase="idle",
    )
    assert execution.classify_error(message) == "startup"


def test_missing_transition_verdict_refuses_confirmation(tmp_path, monkeypatch):
    from tests.test_scheduler import ScriptedSupervisor, make_scheduler

    scheduler = make_scheduler(tmp_path)
    monkeypatch.setattr(scheduler, "_supervisor", lambda _: ScriptedSupervisor())
    ScriptedSupervisor.script = [lambda sup, lanes, config_for, duration: {lane.core_id: None for lane in lanes}]
    result = scheduler.run_rapid_transitions([0], total_duration=1)
    assert not result.passed
    assert result.error_type == "startup"


def test_idle_thermal_stop_collects_concurrent_hardware_errors(monkeypatch):
    detector = ErrorDetector()
    detector._dmesg_baseline_ts = 100.0
    detector._last_dmesg_time = float("inf")
    result = MagicMock(returncode=0, stderr="", stdout="101.0 mce: CPU 1 Bank 1: error\n")
    monkeypatch.setattr("corecycler.engine.detector.subprocess.run", lambda *a, **kw: result)
    thermal = MagicMock(max_temperature=80)
    thermal.safe.return_value = False
    observed = []
    message = execution.watch_idle(
        cpus=(0,),
        duration=1,
        thermal=thermal,
        detector=detector,
        stop_event=threading.Event(),
        observed=observed,
        phase="idle",
    )
    assert execution.classify_error(message) == "thermal"
    assert [event.cpu for event in observed] == [1]
