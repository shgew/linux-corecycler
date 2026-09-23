"""TunerTab action coverage: start/pause/resume/abort/validate/export and slots.

The engine is a stand-in throughout. A real TunerEngine would write Curve
Optimizer offsets through the SMU. What is exercised here is the tab's own
decision logic: every refusal, every dialog branch and every engine signal
handler.
"""

from __future__ import annotations

import json
import os
import sys as _sys
from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest

if not hasattr(_sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("GUI tests require real PySide6", allow_module_level=True)

from corecycler.engine.topology import CPUTopology, PhysicalCore
from corecycler.gui import tuner_tab as tt
from corecycler.history.db import HistoryDB
from corecycler.tuner.config import TunerConfig
from corecycler.tuner.state import CoreState, TunerPhase


def _qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _topo(cores: int = 2) -> CPUTopology:
    topo = CPUTopology(model_name="Test 8C", family=26, model=0x44, physical_cores=cores, ccds=1)
    for cid in range(cores):
        topo.cores[cid] = PhysicalCore(core_id=cid, ccd=0, logical_cpus=(cid,))
    return topo


def _smu(available: bool = True):
    smu = MagicMock()
    smu.is_available.return_value = available
    smu.commands.co_range = (-50, 10)
    return smu


def _backend(available: bool = True):
    backend = MagicMock()
    backend.is_available.return_value = available
    return backend


def _engine(status="running", session_id=1, cores=(0, 1)):
    eng = MagicMock()
    eng.status = status
    eng.session_id = session_id
    eng.core_states = {cid: CoreState(core_id=cid, phase=TunerPhase.COARSE_SEARCH, current_offset=-10) for cid in cores}
    return eng


@pytest.fixture
def db():
    d = HistoryDB(":memory:")
    yield d
    d.close()


@pytest.fixture(autouse=True)
def no_modal(monkeypatch):
    monkeypatch.setattr(tt, "QMessageBox", MagicMock())
    monkeypatch.setattr(TunerConfig, "backend_availability_errors", lambda self: [])
    # A missing backend prompts for its path; default to the user declining.
    monkeypatch.setattr(tt, "ensure_tool", lambda parent, key: False)
    return tt.QMessageBox


def _tab(db=None, topology=None, smu=None, backend_factory=None):
    _qapp()
    return tt.TunerTab(db=db, topology=topology, smu=smu, backend_factory=backend_factory)


@pytest.fixture
def tab(db):
    return _tab(db=db, topology=_topo(), smu=_smu(), backend_factory=lambda _n: _backend())


def _seed_session(db, status="paused"):
    sid = db.create_tuner_session(TunerConfig().to_json(), bios_version="2402", cpu_model="Test 8C")
    db.update_tuner_session_status(sid, status)
    return sid


def _persist_engine(db, engine) -> None:
    if db.get_tuner_session(engine.session_id) is None:
        session_id = db.create_tuner_session(TunerConfig().to_json(), "2402", "Test 8C")
        assert session_id == engine.session_id
    for state in engine.core_states.values():
        db.upsert_tuner_core_state(engine.session_id, state)


class TestMsrProbe:
    def test_a_readable_msr_leaves_the_stretch_spin_enabled(self, monkeypatch):
        real_open = os.open

        def fake_open(path, *args, **kwargs):
            if path == "/dev/cpu/0/msr":
                return real_open(os.devnull, os.O_RDONLY)
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(os, "open", fake_open)
        tab = _tab()
        assert tab._stretch_threshold_spin.isEnabled()

    def test_an_unreadable_msr_disables_the_stretch_spin(self, monkeypatch):
        real_open = os.open

        def fake_open(path, *args, **kwargs):
            if path == "/dev/cpu/0/msr":
                raise PermissionError(13, "denied")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(os, "open", fake_open)
        tab = _tab()
        assert not tab._stretch_threshold_spin.isEnabled()


class TestConfigPanel:
    def test_load_defaults_restores_the_configuration(self, tab):
        tab._start_offset_spin.setValue(-7)
        tab._coarse_step_spin.setValue(4)
        tab._order_combo.setCurrentText("round_robin")
        tab._auto_validate_check.setChecked(False)
        tab._load_defaults()
        defaults = TunerConfig()
        restored = tab._get_config()
        assert replace(restored, backend=defaults.backend) == defaults


class TestStart:
    def test_refuses_without_db_or_topology(self, no_modal):
        tab = _tab(db=None, topology=None, smu=_smu())
        tab._on_start()
        assert tab._engine is None
        assert no_modal.warning.called

    def test_refuses_while_a_session_is_active(self, tab, no_modal):
        tab._engine = _engine(status="running")
        tab._on_start()
        assert no_modal.warning.called

    def test_refuses_while_a_session_is_paused(self, tab, no_modal):
        tab._engine = _engine(status="paused")
        tab._on_start()
        assert no_modal.warning.called

    def test_refuses_without_smu(self, db, no_modal):
        tab = _tab(db=db, topology=_topo(), smu=_smu(available=False))
        tab._on_start()
        assert no_modal.warning.called

    def test_declining_the_hazard_prompt_starts_nothing(self, tab, no_modal, monkeypatch):
        engine_cls = MagicMock()
        monkeypatch.setattr(tt, "TunerEngine", engine_cls)
        no_modal.warning.return_value = no_modal.StandardButton.No
        tab._on_start()
        assert not engine_cls.called

    def test_refuses_when_the_backend_is_missing(self, db, no_modal, monkeypatch):
        tab = _tab(db=db, topology=_topo(), smu=_smu(), backend_factory=lambda _n: _backend(False))
        engine_cls = MagicMock()
        monkeypatch.setattr(tt, "TunerEngine", engine_cls)
        no_modal.warning.return_value = no_modal.StandardButton.Yes
        tab._on_start()
        assert not engine_cls.called

    @pytest.mark.parametrize(
        "config",
        [TunerConfig(coarse_step=0), TunerConfig(start_offset=11, max_offset=-50)],
    )
    def test_refuses_an_invalid_configuration(self, tab, no_modal, monkeypatch, config):
        engine_cls = MagicMock()
        monkeypatch.setattr(tt, "TunerEngine", engine_cls)
        monkeypatch.setattr(tab, "_get_config", lambda: config)
        no_modal.warning.return_value = no_modal.StandardButton.Yes

        tab._on_start()

        assert not engine_cls.called
        assert no_modal.warning.called

    def test_refuses_when_a_battery_backend_is_unavailable(self, tab, no_modal, monkeypatch):
        engine_cls = MagicMock()
        monkeypatch.setattr(tt, "TunerEngine", engine_cls)
        monkeypatch.setattr(TunerConfig, "backend_availability_errors", lambda self: ["missing battery backend"])
        no_modal.warning.return_value = no_modal.StandardButton.Yes

        tab._on_start()

        assert not engine_cls.called
        assert "missing battery backend" in no_modal.warning.call_args.args[2]

    def test_an_engine_that_refuses_to_start_leaves_the_ui_idle(self, tab, no_modal, monkeypatch):
        eng = _engine(status="idle")
        monkeypatch.setattr(tt, "TunerEngine", MagicMock(return_value=eng))
        no_modal.warning.return_value = no_modal.StandardButton.Yes
        tab._on_start()
        assert eng.start.called
        assert tab._start_btn.isEnabled()
        assert no_modal.warning.called

    def test_a_started_engine_locks_the_ui_and_fills_the_table(self, tab, no_modal, monkeypatch):
        eng = _engine(status="running")
        _persist_engine(tab._db, eng)
        monkeypatch.setattr(tt, "TunerEngine", MagicMock(return_value=eng))
        no_modal.warning.return_value = no_modal.StandardButton.Yes
        tab._on_start()
        assert eng.start.called
        assert not tab._start_btn.isEnabled()
        assert tab._core_table.rowCount() == 2
        assert eng.core_state_changed.connect.called


class TestPause:
    def test_startup_self_pause_acquires_tuner_ownership(self, tab):
        states = []
        tab.tuner_running_changed.connect(states.append)
        tab._engine = _engine(status="paused")

        tab._on_status_changed("paused")

        assert states == [True]
        assert not tab._config_container.isEnabled()

    def test_pause_hands_control_to_resume(self, tab):
        eng = _engine()
        tab._engine = eng
        tab._set_running_state(True)
        tab._on_pause()
        assert eng.pause.called
        assert not tab._pause_btn.isEnabled()
        assert tab._resume_btn.isEnabled()

    def test_pause_without_an_engine_is_a_noop(self, tab):
        tab._on_pause()
        assert not tab._resume_btn.isEnabled()

    def test_a_self_pause_with_no_test_running_releases_the_tested_core(self, tab):
        eng = _engine(status="paused")
        eng.test_in_flight = False
        tab._engine = eng
        tab._on_worker_started(0)
        states = []
        tab.tuner_core_testing.connect(lambda c, s: states.append((c, s)))
        tab._on_status_changed("paused")
        assert tab._active_test_core is None
        assert not tab._tuner_timer.isActive()
        assert len(states) == 1
        assert states[0][0] == 0
        assert states[0][1] != "testing"

    def test_a_pause_requested_mid_test_keeps_the_running_core(self, tab):
        eng = _engine(status="paused")
        eng.test_in_flight = True
        tab._engine = eng
        tab._on_worker_started(0)
        tab._on_status_changed("paused")
        assert tab._active_test_core == 0
        assert tab._tuner_timer.isActive()
        tab._tuner_timer.stop()


def _accepting_dialog(monkeypatch, *, accept=True, clear_selection=False):
    real = tt.QDialog

    class _Dialog(real):
        def exec(self):
            if clear_selection:
                from PySide6.QtWidgets import QListWidget

                widget = self.findChild(QListWidget)
                widget.setCurrentRow(-1)
            return real.DialogCode.Accepted if accept else real.DialogCode.Rejected

    monkeypatch.setattr(tt, "QDialog", _Dialog)


class TestResume:
    def test_a_paused_engine_resumes_directly(self, tab, monkeypatch):
        sid = _seed_session(tab._db, "paused")
        eng = _engine(status="paused", session_id=sid)
        _persist_engine(tab._db, eng)
        tab._engine = eng
        eng.status = "paused"

        def _resumed(_session_id):
            eng.status = "running"

        eng.resume.side_effect = _resumed
        tab._on_resume()
        assert eng.resume.call_args.args == (sid,)

    def test_without_a_db_nothing_is_resumed(self):
        tab = _tab(db=None, topology=_topo(), smu=_smu())
        tab._on_resume()
        assert tab._engine is None

    def test_no_resumable_sessions_informs_the_user(self, tab, no_modal):
        tab._on_resume()
        assert no_modal.information.called

    def test_a_single_session_resumes_without_a_picker(self, tab, monkeypatch):
        sid = _seed_session(tab._db, "paused")
        eng = _engine(status="running", session_id=sid)
        _persist_engine(tab._db, eng)
        monkeypatch.setattr(tt, "TunerEngine", MagicMock(return_value=eng))
        tab._on_resume()
        assert eng.resume.call_args.args == (sid,)

    def test_resume_refuses_when_a_saved_battery_backend_is_unavailable(self, tab, no_modal, monkeypatch):
        _seed_session(tab._db, "paused")
        engine_cls = MagicMock()
        monkeypatch.setattr(tt, "TunerEngine", engine_cls)
        monkeypatch.setattr(TunerConfig, "backend_availability_errors", lambda self: ["missing battery backend"])

        tab._on_resume()

        assert not engine_cls.called
        assert "missing battery backend" in no_modal.warning.call_args.args[2]

    def test_the_picker_resumes_the_chosen_session(self, tab, monkeypatch):
        first = _seed_session(tab._db, "paused")
        second = _seed_session(tab._db, "paused")
        tab._db.upsert_tuner_core_state(
            second, CoreState(core_id=0, phase=TunerPhase.CONFIRMED, current_offset=-30, best_offset=-30)
        )
        eng = _engine(status="running", session_id=second)
        _persist_engine(tab._db, eng)
        monkeypatch.setattr(tt, "TunerEngine", MagicMock(return_value=eng))
        _accepting_dialog(monkeypatch, accept=True)
        tab._on_resume()
        assert eng.resume.call_args.args[0] in (first, second)

    def test_a_cancelled_picker_resumes_nothing(self, tab, monkeypatch):
        _seed_session(tab._db, "paused")
        _seed_session(tab._db, "paused")
        eng = _engine(status="running")
        monkeypatch.setattr(tt, "TunerEngine", MagicMock(return_value=eng))
        _accepting_dialog(monkeypatch, accept=False)
        tab._on_resume()
        assert not eng.resume.called

    def test_a_lone_aborted_session_resumes_without_a_warning(self, tab, monkeypatch, no_modal):
        """Stopping a run is a human choice, not a hazard: no question to answer."""
        sid = _seed_session(tab._db, "aborted")
        eng = _engine(status="running", session_id=sid)
        _persist_engine(tab._db, eng)
        monkeypatch.setattr(tt, "TunerEngine", MagicMock(return_value=eng))
        tab._on_resume()
        assert eng.resume.call_args.args[0] == sid
        assert not no_modal.question.called

    def test_a_lone_quarantined_session_is_offered_not_auto_resumed(self, tab, monkeypatch):
        """One stopped session must still reach the picker, never a silent resume."""
        _seed_session(tab._db, "profile_quarantined")
        eng = _engine(status="running")
        monkeypatch.setattr(tt, "TunerEngine", MagicMock(return_value=eng))
        _accepting_dialog(monkeypatch, accept=False)
        tab._on_resume()
        assert not eng.resume.called

    def test_a_quarantined_pick_asks_first(self, tab, monkeypatch, no_modal):
        _seed_session(tab._db, "paused")
        sid = _seed_session(tab._db, "profile_quarantined")
        eng = _engine(status="running")
        monkeypatch.setattr(tt, "TunerEngine", MagicMock(return_value=eng))
        _accepting_dialog(monkeypatch, accept=True)
        no_modal.question.return_value = no_modal.StandardButton.No
        tab._on_resume()
        assert not eng.resume.called, "declining the warning must resume nothing"
        assert str(sid) in no_modal.question.call_args.args[2]

    def test_a_confirmed_quarantined_pick_resumes(self, tab, monkeypatch, no_modal):
        _seed_session(tab._db, "paused")
        sid = _seed_session(tab._db, "profile_quarantined")
        eng = _engine(status="running", session_id=sid)
        _persist_engine(tab._db, eng)
        monkeypatch.setattr(tt, "TunerEngine", MagicMock(return_value=eng))
        _accepting_dialog(monkeypatch, accept=True)
        no_modal.question.return_value = no_modal.StandardButton.Yes
        tab._on_resume()
        assert eng.resume.call_args.args[0] == sid

    def test_an_empty_picker_selection_resumes_nothing(self, tab, monkeypatch):
        _seed_session(tab._db, "paused")
        _seed_session(tab._db, "paused")
        eng = _engine(status="running")
        monkeypatch.setattr(tt, "TunerEngine", MagicMock(return_value=eng))
        _accepting_dialog(monkeypatch, accept=True, clear_selection=True)
        tab._on_resume()
        assert not eng.resume.called


class TestResumeSession:
    def test_refuses_without_smu(self, db, no_modal):
        tab = _tab(db=db, topology=_topo(), smu=_smu(available=False))
        tab._resume_session(1)
        assert no_modal.warning.called

    def test_refuses_a_cold_start_without_db_or_topology(self, no_modal):
        tab = _tab(db=None, topology=None, smu=_smu())
        tab._resume_session(1)
        assert no_modal.warning.called

    def test_refuses_a_cold_start_without_a_backend(self, db, no_modal, monkeypatch):
        tab = _tab(db=db, topology=_topo(), smu=_smu(), backend_factory=lambda _n: _backend(False))
        engine_cls = MagicMock()
        monkeypatch.setattr(tt, "TunerEngine", engine_cls)
        sid = _seed_session(db)
        tab._resume_session(sid)
        assert not engine_cls.called

    def test_the_saved_config_is_mirrored_into_the_panel(self, tab, monkeypatch):
        cfg = TunerConfig(coarse_step=3, fine_step=2, max_offset=-42, test_order="round_robin")
        sid = tab._db.create_tuner_session(cfg.to_json(), bios_version="2402", cpu_model="Test 8C")
        tab._db.insert_tuner_event(sid, "story line", severity="info")
        eng = _engine(status="running", session_id=sid)
        _persist_engine(tab._db, eng)
        monkeypatch.setattr(tt, "TunerEngine", MagicMock(return_value=eng))
        tab._resume_session(sid)
        assert tab._coarse_step_spin.value() == 3
        assert tab._max_offset_spin.value() == -42
        assert tab._order_combo.currentText() == "round_robin"
        assert tab._core_table.rowCount() == 2

    def test_an_engine_that_will_not_resume_leaves_the_ui_idle(self, tab, no_modal, monkeypatch):
        sid = _seed_session(tab._db, "paused")
        eng = _engine(status="paused", session_id=sid)
        monkeypatch.setattr(tt, "TunerEngine", MagicMock(return_value=eng))
        tab._resume_session(sid)
        assert tab._start_btn.isEnabled()
        assert no_modal.warning.called


class TestAbort:
    def test_abort_releases_the_ui_and_repaints_every_core(self, tab):
        eng = _engine()
        eng.core_states[1].phase = TunerPhase.CONFIRMED
        tab._engine = eng
        tab._set_running_state(True)
        tab._active_test_core = 0
        tab._tuner_timer.start(1000)
        states, infos = [], []
        tab.tuner_core_testing.connect(lambda c, s: states.append((c, s)))
        tab.tuner_core_info.connect(lambda c, o, p: infos.append((c, o, p)))
        tab._on_abort()
        assert eng.abort.called
        assert tab._start_btn.isEnabled()
        assert tab._active_test_core is None
        assert not tab._tuner_timer.isActive()
        assert len(states) == 2
        assert len(infos) == 2

    def test_abort_without_an_engine_is_a_noop(self, tab):
        tab._on_abort()
        assert tab._active_test_core is None


class TestValidate:
    def test_refuses_without_a_session(self, tab):
        tab._on_validate()
        assert tab._start_btn.isEnabled()

    def test_validation_uses_the_saved_session_backend(self, db):
        tab = _tab(db=db, topology=_topo(), smu=_smu(), backend_factory=lambda _n: _backend(False))
        eng = _engine(status="validating")
        tab._engine = eng
        tab._backend_combo.setCurrentText("stress-ng")

        tab._on_validate()

        eng.validate_profile.assert_called_once_with(eng.session_id)

    def test_an_engine_that_refuses_leaves_the_ui_idle(self, tab, no_modal):
        eng = _engine(status="idle")
        tab._engine = eng
        tab._on_validate()
        assert eng.validate_profile.called
        assert tab._start_btn.isEnabled()
        assert no_modal.warning.called

    def test_a_started_validation_locks_the_ui(self, tab):
        eng = _engine(status="validating")
        tab._engine = eng
        tab._on_validate()
        assert not tab._start_btn.isEnabled()


class TestExport:
    def test_refuses_without_a_session(self, tab):
        with patch("corecycler.gui.tuner_tab.QFileDialog.getSaveFileName") as dlg:
            tab._on_export()
        assert not dlg.called

    def test_an_empty_profile_informs_the_user(self, tab, no_modal):
        sid = _seed_session(tab._db)
        tab._engine = _engine(session_id=sid)
        with patch("corecycler.gui.tuner_tab.QFileDialog.getSaveFileName") as dlg:
            tab._on_export()
        assert not dlg.called
        assert no_modal.information.called

    def test_a_cancelled_dialog_writes_nothing(self, tab, tmp_path):
        sid = _seed_session(tab._db)
        tab._db.upsert_tuner_core_state(
            sid, CoreState(core_id=0, phase=TunerPhase.CONFIRMED, current_offset=-30, best_offset=-30)
        )
        tab._engine = _engine(session_id=sid)
        with patch("corecycler.gui.tuner_tab.QFileDialog.getSaveFileName", return_value=("", "")):
            tab._on_export()
        assert list(tmp_path.iterdir()) == []

    def test_a_confirmed_profile_is_written(self, tab, tmp_path, no_modal):
        sid = _seed_session(tab._db)
        tab._db.upsert_tuner_core_state(
            sid, CoreState(core_id=0, phase=TunerPhase.CONFIRMED, current_offset=-30, best_offset=-30)
        )
        tab._engine = _engine(session_id=sid)
        out = tmp_path / "profile.json"
        with patch("corecycler.gui.tuner_tab.QFileDialog.getSaveFileName", return_value=(str(out), "")):
            tab._on_export()
        assert json.loads(out.read_text())["offsets"] == {"0": -30}
        assert no_modal.information.called

    def test_a_failed_write_is_surfaced(self, tab, tmp_path, no_modal, monkeypatch):
        sid = _seed_session(tab._db)
        tab._db.upsert_tuner_core_state(
            sid, CoreState(core_id=0, phase=TunerPhase.CONFIRMED, current_offset=-30, best_offset=-30)
        )
        tab._engine = _engine(session_id=sid)
        import corecycler.config.settings as settings

        def _boom(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(settings, "save_co_profile", _boom)
        with patch(
            "corecycler.gui.tuner_tab.QFileDialog.getSaveFileName",
            return_value=(str(tmp_path / "p.json"), ""),
        ):
            tab._on_export()
        assert "disk full" in no_modal.warning.call_args.args[2]


class TestEngineSignals:
    def test_wiring_without_an_engine_is_a_noop(self, tab):
        tab._wire_engine()
        assert tab._engine is None

    def test_display_failure_is_queued_and_never_reaches_fatal_handling(self, tab, monkeypatch, caplog):
        from PySide6.QtCore import QObject, Signal

        class SignalEngine(QObject):
            core_state_changed = Signal(int, str, int)
            worker_started = Signal(int)
            test_completed = Signal(int, int, bool)
            session_completed = Signal(str)
            status_changed = Signal(str)
            progress_updated = Signal(int, int)
            log_message = Signal(str)
            slot_started = Signal(str)
            platform_fault = Signal(str)
            co_drift_detected = Signal(str)
            validation_progress = Signal(int, int, int)

        rendered = []

        def broken_renderer(core_id):
            rendered.append(core_id)
            raise RuntimeError("display failed")

        fatal = MagicMock()
        force_stop = MagicMock()
        monkeypatch.setattr(tab, "_on_worker_started", broken_renderer)
        monkeypatch.setattr(tab, "force_stop", force_stop)
        monkeypatch.setattr(_sys, "excepthook", fatal)
        tab._engine = SignalEngine()
        tab._wire_engine()

        with caplog.at_level("ERROR", logger="corecycler.gui.tuner_tab"):
            tab._engine.worker_started.emit(3)
            assert rendered == []
            _qapp().processEvents()

        assert rendered == [3]
        assert "display failed" in caplog.text
        assert not fatal.called
        assert not force_stop.called

    def test_co_drift_is_reported_per_core(self, tab, no_modal):
        tab._on_co_drift(json.dumps({"0": {"expected": -30, "actual": -10}}))
        body = no_modal.warning.call_args.args[2]
        assert "Core 0: tuner last wrote -30, found -10" in body

    def test_the_active_core_stays_highlighted_on_a_state_change(self, tab):
        engine = _engine()
        _persist_engine(tab._db, engine)
        tab._engine = engine
        tab._active_test_core = 0
        states = []
        tab.tuner_core_testing.connect(lambda c, s: states.append((c, s)))
        tab._on_core_state_changed(0, TunerPhase.COARSE_SEARCH, -12)
        assert states == [(0, "testing")]

    def test_a_new_worker_releases_the_previous_core(self, tab):
        tab._engine = _engine()
        tab._engine.core_states[0].phase = TunerPhase.CONFIRMED
        tab._active_test_core = 0
        states = []
        tab.tuner_core_testing.connect(lambda c, s: states.append((c, s)))
        tab._on_worker_started(1)
        assert states[0][0] == 0
        assert states[0][1] != "testing"
        assert states[1] == (1, "testing")
        assert tab._active_test_core == 1
        assert tab._tuner_timer.isActive()
        tab._tuner_timer.stop()

    def test_a_completed_test_clears_the_active_core_and_logs(self, tab):
        sid = _seed_session(tab._db)
        tab._db.insert_tuner_test_log(sid, 0, -30, "coarse", True, duration=60.0)
        engine = _engine(session_id=sid)
        _persist_engine(tab._db, engine)
        tab._engine = engine
        tab._active_test_core = 0
        tab._tuner_timer.start(1000)
        tab._on_test_completed(0, -30, True)
        assert tab._active_test_core is None
        assert not tab._tuner_timer.isActive()
        assert tab._log_table.rowCount() == 1
        assert tab._log_table.item(0, 6).text() == "PASS"

    def test_session_completion_releases_the_ui(self, tab, monkeypatch):
        _mute_notify(monkeypatch)
        tab._set_running_state(True)
        tab._on_session_completed(json.dumps({"0": -30}))
        assert tab._start_btn.isEnabled()
        assert tab._validate_btn.isEnabled()
        assert tab._export_btn.isEnabled()

    def test_an_empty_profile_leaves_validate_disabled(self, tab, monkeypatch):
        _mute_notify(monkeypatch)
        tab._on_session_completed("")
        assert not tab._validate_btn.isEnabled()

    def test_progress_handlers_change_presentation_state(self, tab):
        initial_progress = tab._progress_label.text()
        tab._on_progress_updated(3, 8)
        core_progress = tab._progress_label.text()
        assert core_progress != initial_progress
        tab._on_validation_progress(6, 2, 4)
        assert tab._status_label.text()
        assert tab._progress_label.text() != core_progress

    def test_log_messages_reach_the_logger(self, tab, caplog):
        with caplog.at_level("INFO", logger="corecycler.gui.tuner_tab"):
            tab._on_log_message("core 0 settled")
        assert "core 0 settled" in caplog.text

    def test_idle_status_repaints_every_core(self, tab, monkeypatch):
        _mute_notify(monkeypatch)
        tab._engine = _engine()
        tab._set_running_state(True)
        infos = []
        tab.tuner_core_info.connect(lambda c, o, p: infos.append(c))
        tab._on_status_changed("idle")
        assert infos == [0, 1]
        assert tab._start_btn.isEnabled()

    def test_profile_quarantine_notifies_with_critical_urgency(self, tab, monkeypatch):
        notify = _mute_notify(monkeypatch)
        tab._engine = _engine()
        tab._on_status_changed("profile_quarantined")
        assert notify.call_args.kwargs["urgency"] == "critical"

    def test_platform_fault_reports_evidence_without_profile_quarantine(self, tab, monkeypatch):
        notify = _mute_notify(monkeypatch)
        tab._engine = _engine()

        tab._on_status_changed("platform_fault")
        assert not notify.called
        tab._on_platform_fault("no core held a live offset at the time of the failure")

        assert notify.call_count == 1
        assert "no core held a live offset at the time of the failure" in notify.call_args.args[1]


def _mute_notify(monkeypatch, *, enabled=True):
    import corecycler.config.settings as settings
    import corecycler.notify as notify_mod

    monkeypatch.setattr(settings, "load_settings", lambda: MagicMock(notify_on_completion=enabled))
    sent = MagicMock()
    monkeypatch.setattr(notify_mod, "desktop_notify", sent)
    return sent


class TestNotify:
    def test_a_disabled_setting_sends_nothing(self, tab, monkeypatch):
        sent = _mute_notify(monkeypatch, enabled=False)
        tab._notify("done", "body")
        assert not sent.called

    def test_an_enabled_setting_sends_the_notification(self, tab, monkeypatch):
        sent = _mute_notify(monkeypatch)
        tab._notify("done", "body")
        assert sent.called

    def test_a_broken_notifier_never_reaches_the_caller(self, tab, monkeypatch):
        import corecycler.config.settings as settings

        def _boom():
            raise RuntimeError("no dbus")

        monkeypatch.setattr(settings, "load_settings", _boom)
        tab._notify("done", "body")


class TestTicker:
    def test_an_active_core_emits_elapsed_time(self, tab):
        tab._active_test_core = 1
        tab._test_start_time = 0.0
        seen = []
        tab.tuner_core_elapsed.connect(lambda c, e: seen.append((c, e)))
        tab._tick_tuner()
        assert seen[0][0] == 1
        assert seen[0][1] > 0

    def test_an_idle_tab_stops_the_timer(self, tab):
        tab._tuner_timer.start(1000)
        tab._active_test_core = None
        tab._engine = None
        tab._tick_tuner()
        assert not tab._tuner_timer.isActive()


class TestLogTable:
    def test_core_without_an_active_session_has_no_evidence_row(self, tab):
        tab._engine = _engine(session_id=None, cores=(0,))

        tab._update_core_row(0)

        assert tab._core_table.rowCount() == 0

    def test_an_entry_without_a_session_is_dropped(self, tab):
        tab._add_log_entry(0, -30, True)
        assert tab._log_table.rowCount() == 0

    def test_an_entry_without_a_test_log_row_is_dropped(self, tab):
        sid = _seed_session(tab._db)
        tab._engine = _engine(session_id=sid)
        tab._add_log_entry(0, -30, True)
        assert tab._log_table.rowCount() == 0

    def test_an_entry_for_another_core_is_filtered_out(self, tab):
        sid = _seed_session(tab._db)
        tab._db.insert_tuner_test_log(sid, 0, -30, "coarse", True, duration=60.0)
        tab._engine = _engine(session_id=sid)
        tab._selected_core = 1
        tab._add_log_entry(0, -30, True)
        assert tab._log_table.rowCount() == 0

    def test_the_oldest_row_is_dropped_at_the_cap(self, tab):
        from PySide6.QtWidgets import QTableWidgetItem

        sid = _seed_session(tab._db)
        tab._db.insert_tuner_test_log(sid, 0, -30, "coarse", False, duration=60.0)
        tab._engine = _engine(session_id=sid)
        for _ in range(2000):
            tab._log_table.insertRow(tab._log_table.rowCount())
        tab._log_table.setItem(0, 0, QTableWidgetItem("oldest"))

        tab._add_log_entry(0, -30, False)

        assert tab._log_table.rowCount() == 2000
        assert tab._log_table.item(0, 0) is None
        assert tab._log_table.item(1999, 6).text() == "FAIL"

    def test_selecting_a_core_filters_the_log(self, tab):
        sid = _seed_session(tab._db)
        tab._db.insert_tuner_test_log(sid, 0, -30, "coarse", True, duration=60.0)
        tab._db.insert_tuner_test_log(sid, 1, -25, "coarse", False, duration=12.0)
        tab._engine = _engine(session_id=sid)
        tab._core_table.insertRow(0)
        from PySide6.QtWidgets import QTableWidgetItem

        tab._core_table.setItem(0, 0, QTableWidgetItem("1"))
        tab._on_core_selected(0, 0, -1, -1)
        assert tab._selected_core == 1
        assert tab._log_table.rowCount() == 1
        assert tab._log_table.item(0, 6).text() == "FAIL"

    def test_selecting_an_empty_row_shows_every_core(self, tab):
        sid = _seed_session(tab._db)
        tab._db.insert_tuner_test_log(sid, 0, -30, "coarse", True, duration=60.0)
        tab._db.insert_tuner_test_log(sid, 1, -25, "coarse", False, duration=None)
        tab._engine = _engine(session_id=sid)
        tab._selected_core = 1
        tab._on_core_selected(5, 0, -1, -1)
        assert tab._selected_core is None
        assert tab._log_table.rowCount() == 2
        assert tab._log_table.item(1, 7).text() == "-"

    def test_refresh_keeps_only_the_newest_rows(self, tab):
        sid = _seed_session(tab._db)
        for offset in range(2001):
            tab._db.insert_tuner_test_log(sid, 0, offset, "coarse", True, duration=1.0)
        tab._engine = _engine(session_id=sid)

        tab._refresh_log_table()

        assert tab._log_table.rowCount() == 2000
        assert tab._log_table.item(0, 2).text() == "1"
        assert tab._log_table.item(1999, 2).text() == "2000"

    def test_refreshing_without_a_session_empties_the_log(self, tab):
        tab._log_table.insertRow(0)
        tab._refresh_log_table()
        assert tab._log_table.rowCount() == 0


class TestClipboard:
    def test_an_empty_table_copies_nothing(self, tab):
        from PySide6.QtWidgets import QApplication

        clipboard = QApplication.clipboard()
        clipboard.setText("untouched")
        tab._copy_table_selection(tab._log_table)
        assert clipboard.text() == "untouched"

    def test_every_row_is_copied_when_nothing_is_selected(self, tab):
        from PySide6.QtWidgets import QApplication, QTableWidgetItem

        tab._log_table.insertRow(0)
        tab._log_table.setItem(0, 1, QTableWidgetItem("7"))
        tab._copy_table_selection(tab._log_table)
        text = QApplication.clipboard().text()
        assert "\t7\t" in text.splitlines()[1]

    def test_only_the_selected_rows_are_copied(self, tab):
        from PySide6.QtWidgets import QApplication, QTableWidgetItem

        for row in range(2):
            tab._log_table.insertRow(row)
            tab._log_table.setItem(row, 1, QTableWidgetItem(str(row)))
        tab._log_table.selectRow(1)
        tab._copy_table_selection(tab._log_table)
        lines = QApplication.clipboard().text().splitlines()
        assert len(lines) == 2
        assert "\t1\t" in lines[1]


class TestBackendResolution:
    def test_an_unknown_backend_name_is_refused(self, db, no_modal):
        tab = _tab(db=db, topology=_topo(), smu=_smu())
        tab._backend_combo.addItem("nonexistent")
        tab._backend_combo.setCurrentText("nonexistent")
        assert tab._get_backend() is None
        assert "Unknown backend" in no_modal.warning.call_args.args[2]

    def test_a_known_backend_is_returned(self, db, monkeypatch):
        import corecycler.engine.backends as backends

        chosen = _backend()
        monkeypatch.setattr(backends, "get_backend", lambda _n: chosen)
        tab = _tab(db=db, topology=_topo(), smu=_smu())
        assert tab._get_backend() is chosen

    def test_an_uninstalled_backend_is_refused(self, db, no_modal):
        tab = _tab(db=db, topology=_topo(), smu=_smu(), backend_factory=lambda _n: _backend(False))
        assert tab._get_backend() is None

    def test_an_uninstalled_backend_is_kept_once_the_user_supplies_a_path(self, db, no_modal, monkeypatch):
        monkeypatch.setattr(tt, "ensure_tool", lambda parent, key: True)
        chosen = _backend(False)
        tab = _tab(db=db, topology=_topo(), smu=_smu(), backend_factory=lambda _n: chosen)
        assert tab._get_backend() is chosen


class TestRecoveryBanner:
    def test_an_in_flight_session_enables_recovery(self, db):
        sid = _seed_session(db, "paused")
        tab = _tab(db=db, topology=_topo(), smu=_smu())
        assert tab._resume_btn.isEnabled()
        assert [session.id for session in db.list_recoverable_tuner_sessions()] == [sid]

    def test_an_ended_session_does_not_claim_engine_ownership(self, db):
        _seed_session(db, "profile_quarantined")
        tab = _tab(db=db, topology=_topo(), smu=_smu())
        assert tab._engine is None
        assert tab._start_btn.isEnabled()

    def test_live_work_remains_recoverable_with_an_older_stopped_session(self, db):
        _seed_session(db, "profile_quarantined")
        sid = _seed_session(db, "paused")
        tab = _tab(db=db, topology=_topo(), smu=_smu())
        statuses = {session.id: session.status for session in db.list_recoverable_tuner_sessions()}
        assert statuses[sid] == "paused"
        assert tab._resume_btn.isEnabled()


class TestStartupRecovery:
    def test_a_single_recoverable_session_is_selected(self, db):
        sid = _seed_session(db, "paused")
        tab = _tab(db=db, topology=_topo(), smu=_smu())
        tab._resume_session = MagicMock()

        tab._on_resume()

        tab._resume_session.assert_called_once_with(sid)

    def test_several_recoverable_sessions_enable_the_picker(self, db):
        _seed_session(db, "paused")
        _seed_session(db, "paused")
        tab = _tab(db=db, topology=_topo(), smu=_smu())
        assert tab._resume_btn.isEnabled()
        assert len(db.list_recoverable_tuner_sessions()) == 2


class TestForceStop:
    def test_force_stop_aborts_a_live_engine(self, tab):
        eng = _engine()
        tab._engine = eng
        tab.force_stop()
        assert eng.abort.called

    def test_force_stop_without_an_engine_is_a_noop(self, tab):
        tab.force_stop()
        assert tab._engine is None

    def test_exit_shutdown_pauses_instead_of_aborting(self, tab):
        eng = _engine()
        eng.shutdown.return_value = True
        tab._engine = eng
        assert tab.shutdown() is True
        assert eng.shutdown.called
        assert not eng.abort.called

    def test_exit_shutdown_without_an_engine_is_a_noop(self, tab):
        assert tab.shutdown() is True
        assert tab._engine is None


class TestExternalOwnership:
    def test_external_stress_blocks_all_tuner_actions(self, tab, monkeypatch):
        eng = _engine(status="paused")
        eng.test_in_flight = False
        tab._engine = eng
        tab._resume_btn.setEnabled(True)
        tab._validate_btn.setEnabled(True)
        tab.set_test_running(True)
        for button in (tab._start_btn, tab._resume_btn, tab._validate_btn):
            assert not button.isEnabled()
        tab._on_start()
        tab._resume_session(1)
        tab._on_validate()
        eng.start.assert_not_called()
        eng.resume.assert_not_called()
        eng.validate_profile.assert_not_called()
        tab.set_test_running(False)
        assert tab._resume_btn.isEnabled()
        assert tab._validate_btn.isEnabled()

    def test_cold_resume_selects_the_saved_backend(self, tab, monkeypatch):
        config = TunerConfig(backend="stress-ng")
        sid = tab._db.create_tuner_session(config.to_json(), "", "")
        tab._db.upsert_tuner_core_state(sid, CoreState(core_id=0))
        requested = []
        tab._backend_factory = lambda name: requested.append(name) or _backend()
        eng = _engine(session_id=sid, cores=(0,))
        monkeypatch.setattr(tt, "TunerEngine", lambda **kw: eng)
        tab._backend_combo.clear()
        tab._backend_combo.addItem("nonexistent")
        tab._resume_session(sid)
        assert requested == ["stress-ng"]

    def test_resume_refuses_corrupt_saved_config(self, tab, monkeypatch):
        sid = _seed_session(tab._db)
        tab._db._execute_raw("UPDATE tuner_sessions SET config_json = ? WHERE id = ?", ("{broken", sid))
        constructor = MagicMock()
        monkeypatch.setattr(tt, "TunerEngine", constructor)
        tab._resume_session(sid)
        assert tab._engine is None
        constructor.assert_not_called()

    def test_resume_refuses_missing_topology(self, db):
        sid = _seed_session(db)
        tab = _tab(db=db, topology=None, smu=_smu())
        tab._resume_session(sid)
        assert tab._engine is None


class TestResidualResumeAndTableEdges:
    def test_invalid_saved_configuration_is_refused(self, tab, no_modal):
        config = TunerConfig(start_offset=20)
        sid = tab._db.create_tuner_session(config.to_json(), bios_version="2402", cpu_model="Test 8C")
        tab._db.update_tuner_session_status(sid, "paused")

        tab._resume_session(sid)

        assert tab._engine is None
        assert no_modal.warning.call_args.args[1] == "Invalid Configuration"
        assert "start_offset" in no_modal.warning.call_args.args[2]

    def test_refreshing_existing_core_reuses_its_table_row(self, tab):
        engine = _engine()
        _persist_engine(tab._db, engine)
        tab._engine = engine

        tab._update_core_row(0)
        rows = tab._core_table.rowCount()
        tab._update_core_row(0)

        assert rows == 1
        assert tab._core_table.rowCount() == rows
        assert tab._core_table.item(0, 0).text() == "0"
