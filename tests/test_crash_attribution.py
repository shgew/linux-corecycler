"""Evidence-based crash attribution for the live-offset search mask."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from corecycler.engine.detector import MCEEvent
from corecycler.history.db import HistoryDB
from corecycler.tuner import bisect
from corecycler.tuner import persistence as tp
from corecycler.tuner.config import TunerConfig
from corecycler.tuner.engine import TunerEngine, _pick_report
from corecycler.tuner.state import CoreState, TunerPhase
from tests.silicon import complete_context

# Real kernel MCE line shape (kernel: prefix stripped).
CPU5_CORRECTED = (
    "[Hardware Error]: CPU:{cpu} (1a:44:0) MC0_STATUS[Over|CE|MiscV|AddrV|-|-|SyndV|CECC|-|-|-]: 0xdc204000000d0175"
)


@pytest.fixture
def db(tmp_path):
    d = HistoryDB(tmp_path / "test.db")
    yield d
    d.close()


class FakeSMU:
    def __init__(self):
        self.written: dict[int, int] = {}
        self.commands = SimpleNamespace(co_range=(-60, 30))

    def set_co_offset(self, core_id: int, value: int) -> bool:
        self.written[core_id] = value
        return True

    def get_co_offset(self, core_id: int) -> int:
        return self.written.get(core_id, 0)

    def get_all_co_offsets(self, num_cores: int) -> dict[int, int]:
        return {core_id: self.get_co_offset(core_id) for core_id in range(num_cores)}

    def get_pbo_scalar(self) -> float:
        return 1.0

    def get_boost_limit(self) -> int:
        return 5500


def _event(cpu: int, corrected: bool = True) -> MCEEvent:
    return MCEEvent(
        timestamp=0.0,
        cpu=cpu,
        bank=0,
        message=CPU5_CORRECTED.format(cpu=cpu),
        corrected=corrected,
        raw_ts=float(cpu),
    )


def _make_engine(db, topo, mock_backend, **cfg_kwargs):
    defaults = dict(
        coarse_step=5,
        fine_step=1,
        crash_penalty_steps=3,
        cores_to_test=sorted(topo.cores),
    )
    defaults.update(cfg_kwargs)
    cfg = TunerConfig(**defaults)
    eng = TunerEngine(
        db=db,
        topology=topo,
        smu=FakeSMU(),
        backend=mock_backend,
        config=cfg,
    )
    context_id = complete_context(db, topo, eng._smu)
    eng._session_id = db.create_tuner_session(cfg.to_json(), "Test BIOS", topo.model_name, context_id)
    return eng


def _seed_confirmed_validating(eng, db, best: dict[int, int], baselines: dict[int, int]):
    """Seed a validation freeze's persisted shape: every core CONFIRMED and
    in_test (a validation stage was stressing the whole set when the box
    froze), every journal row survived (validation re-applies proven values).
    """
    sid = eng._session_id
    eng._core_states = {}
    for core_id, offset in best.items():
        cs = CoreState(
            core_id=core_id,
            phase=TunerPhase.CONFIRMED,
            current_offset=offset,
            best_offset=offset,
            baseline_offset=baselines[core_id],
            in_test=True,
        )
        eng._core_states[core_id] = cs
        db.upsert_tuner_core_state(sid, cs)
        db.journal_co_intent(sid, core_id, offset, survived=True)
    db.update_tuner_session_status(sid, "validating")
    return db.get_tuner_session(sid)


BEST = {0: -41, 1: -37, 2: -36, 3: -43, 4: -42, 5: -30, 6: -41, 7: -50}
BASELINES = {c: (-15 if c < 4 else -6) for c in BEST}


class TestForensicAttribution:
    """Kernel-journal evidence names the culprits; policy guesses never run."""

    def test_replay_field_incident_penalizes_kernel_named_cores(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        session = _seed_confirmed_validating(eng, db, BEST, BASELINES)
        # The kernel named cores 5 and 6 (corrected LS MCEs) before the freeze.
        eng._forensics = lambda since, timeout=15.0, **kwargs: (
            [_event(5), _event(5), _event(6)],
            True,
        )

        crashed, pending_hunt = eng._attribute_crash_after_reboot(session)

        assert crashed == [5, 6]
        assert pending_hunt is False
        # The named cores took the penalty, anchored at their resident values.
        assert eng._core_states[5].backoff_fail_bound == -30
        assert eng._core_states[6].backoff_fail_bound == -41
        assert eng._core_states[5].phase == TunerPhase.BACKOFF_PRECONFIRM
        # The deepest-undervolt core is untouched.
        assert eng._core_states[7].crash_count == 0
        assert eng._core_states[7].best_offset == -50
        assert eng._core_states[7].phase == TunerPhase.CONFIRMED
        assert all(not cs.in_test for cs in eng._core_states.values())

    def test_sibling_cpu_maps_to_its_physical_core(self, db, topo_dual_ccd_x3d, mock_backend):
        """An MCE on the second SMT sibling is evidence about the same core."""
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        session = _seed_confirmed_validating(eng, db, BEST, BASELINES)
        sibling = topo_dual_ccd_x3d.cores[3].logical_cpus[1]
        eng._forensics = lambda since, timeout=15.0, **kwargs: ([_event(sibling)], True)

        crashed, pending_hunt = eng._attribute_crash_after_reboot(session)

        assert crashed == [3]
        assert pending_hunt is False

    def test_unattributable_events_do_not_penalize(self, db, topo_dual_ccd_x3d, mock_backend):
        """A kernel panic line with no CPU proves a crash, names no core."""
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        session = _seed_confirmed_validating(eng, db, BEST, BASELINES)
        eng._forensics = lambda since, timeout=15.0, **kwargs: ([_event(-1)], True)

        crashed, pending_hunt = eng._attribute_crash_after_reboot(session)

        assert crashed == []
        assert pending_hunt is True  # fall through to the hunt

    def test_no_forensics_multi_core_requests_hunt_not_guess(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        session = _seed_confirmed_validating(eng, db, BEST, BASELINES)
        eng._forensics = lambda since, timeout=15.0, **kwargs: ([], True)

        crashed, pending_hunt = eng._attribute_crash_after_reboot(session)

        assert crashed == []
        assert pending_hunt is True
        assert all(cs.crash_count == 0 for cs in eng._core_states.values())
        assert all(not cs.in_test for cs in eng._core_states.values())

    def test_forensics_unavailable_pauses_without_guessing(self, db, topo_dual_ccd_x3d, mock_backend):
        """journalctl missing cannot prove an isolated core caused the reboot."""
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        session = _seed_confirmed_validating(eng, db, BEST, BASELINES)
        before = {core: (cs.current_offset, cs.best_offset, cs.crash_count) for core, cs in eng._core_states.items()}
        eng._forensics = lambda since, timeout=15.0, **kwargs: ([], False)

        crashed, pending_hunt = eng._attribute_crash_after_reboot(session)

        assert crashed == []
        assert pending_hunt is False
        assert eng.status == "paused"
        after = {core: (cs.current_offset, cs.best_offset, cs.crash_count) for core, cs in eng._core_states.items()}
        assert after == before
        assert eng._smu.written == {}

    def test_mce_on_an_unselected_core_pauses_without_blame(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend, cores_to_test=[0])
        cs = CoreState(
            core_id=0,
            phase=TunerPhase.CONFIRMED,
            current_offset=-41,
            best_offset=-41,
            baseline_offset=-15,
            in_test=True,
        )
        eng._core_states = {0: cs}
        db.upsert_tuner_core_state(eng._session_id, cs)
        db.update_tuner_session_status(eng._session_id, "validating")
        session = db.get_tuner_session(eng._session_id)
        outside_cpu = topo_dual_ccd_x3d.cores[1].logical_cpus[0]
        eng._forensics = lambda since, timeout=15.0, **kwargs: ([_event(outside_cpu)], True)

        crashed, pending_hunt = eng._attribute_crash_after_reboot(session)

        assert crashed == []
        assert pending_hunt is False
        assert eng.status == "paused"
        assert (cs.current_offset, cs.best_offset, cs.crash_count) == (-41, -41, 0)
        assert eng._smu.written == {}

    def test_mce_on_a_stock_core_pauses_without_blame(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend, cores_to_test=[0, 1])
        eng._core_states = {
            core: CoreState(
                core_id=core,
                phase=TunerPhase.CONFIRMED,
                current_offset=BEST[core],
                best_offset=BEST[core],
                baseline_offset=BASELINES[core],
                in_test=core == 0,
            )
            for core in (0, 1)
        }
        for cs in eng._core_states.values():
            db.upsert_tuner_core_state(eng._session_id, cs)
        db.journal_co_intent(eng._session_id, 0, BEST[0], survived=True)
        db.journal_co_intent(eng._session_id, 1, 0, survived=True)
        db.update_tuner_session_status(eng._session_id, "validating")
        session = db.get_tuner_session(eng._session_id)
        stock_cpu = topo_dual_ccd_x3d.cores[1].logical_cpus[0]
        eng._forensics = lambda since, timeout=15.0, **kwargs: ([_event(stock_cpu)], True)

        crashed, pending_hunt = eng._attribute_crash_after_reboot(session)

        assert crashed == []
        assert pending_hunt is False
        assert eng.status == "paused"
        after = {core: (cs.current_offset, cs.best_offset, cs.crash_count) for core, cs in eng._core_states.items()}
        assert after == {0: (BEST[0], BEST[0], 0), 1: (BEST[1], BEST[1], 0)}
        assert eng._smu.written == {}

    def test_loaded_core_is_not_blamed_when_other_live_offsets_were_resident(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend, cores_to_test=[2, 3])
        eng._core_states = {
            2: CoreState(core_id=2, phase=TunerPhase.COARSE_SEARCH, current_offset=-20, in_test=True),
            3: CoreState(
                core_id=3,
                phase=TunerPhase.CONFIRMED,
                current_offset=-15,
                best_offset=-15,
            ),
        }
        for cs in eng._core_states.values():
            db.upsert_tuner_core_state(eng._session_id, cs)
            db.journal_co_intent(eng._session_id, cs.core_id, cs.current_offset, survived=True)
        session = db.get_tuner_session(eng._session_id)
        eng._forensics = lambda since, timeout=15.0, **kwargs: ([], True)

        crashed, pending_hunt = eng._attribute_crash_after_reboot(session)

        assert crashed == []
        assert pending_hunt is True
        assert eng._pending_hunt_loaded == [2]
        assert all(cs.crash_count == 0 for cs in eng._core_states.values())

    def test_only_non_stock_resident_can_be_blamed_without_a_hunt(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend, cores_to_test=[2, 3])
        tested = CoreState(core_id=2, phase=TunerPhase.COARSE_SEARCH, current_offset=-20, in_test=True)
        stock = CoreState(core_id=3, phase=TunerPhase.COARSE_SEARCH, current_offset=0)
        eng._core_states = {2: tested, 3: stock}
        for cs in eng._core_states.values():
            db.upsert_tuner_core_state(eng._session_id, cs)
            db.journal_co_intent(eng._session_id, cs.core_id, cs.current_offset, survived=True)
        session = db.get_tuner_session(eng._session_id)
        eng._forensics = lambda since, timeout=15.0, **kwargs: ([], True)

        crashed, pending_hunt = eng._attribute_crash_after_reboot(session)

        assert crashed == [2]
        assert pending_hunt is False
        assert tested.crash_count == 1
        assert stock.crash_count == 0


class TestCrashHunt:
    def test_in_flight_control_probe_owns_attribution(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        _seed_confirmed_validating(eng, db, BEST, BASELINES)
        state = bisect.begin(sorted(BEST), loaded=[5])
        state.vector = dict(BEST)
        state.workload = eng._workload_snapshot(eng._core_states[5])
        state.armed = True
        db.set_hunt_state(eng._session_id, state.to_json())
        db.update_tuner_session_status(eng._session_id, "hunting")
        for core_id in BEST:
            db.journal_co_intent(eng._session_id, core_id, 0, survived=True)
        session = db.get_tuner_session(eng._session_id)
        eng._forensics = lambda *args, **kwargs: pytest.fail("hunt probes own their crash verdict")

        crashed, pending_hunt = eng._attribute_crash_after_reboot(session)

        assert crashed == []
        assert pending_hunt is True
        assert all(cs.crash_count == 0 for cs in eng._core_states.values())
        assert all(not cs.in_test for cs in eng._core_states.values())

    def test_hunt_opens_with_a_stock_control_probe(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        _seed_confirmed_validating(eng, db, BEST, BASELINES)
        eng._smu.written = dict(BEST)
        probes = []
        eng._start_worker = lambda core_id, duration, **kwargs: probes.append((core_id, duration))

        eng._start_hunt(loaded=[5])

        assert eng.status == "hunting"
        assert eng._hunt is not None
        assert eng._hunt.stage is bisect.Stage.CONTROL
        assert eng._smu.written == dict.fromkeys(BEST, 0)
        assert probes and probes[0][0] == 5
        assert eng._core_states[5].in_test is True

    def test_non_hunt_pass_clears_the_resume_crash_streak(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        cs = CoreState(core_id=5, phase=TunerPhase.COARSE_SEARCH, current_offset=-20, baseline_offset=0)
        eng._core_states = {5: cs}
        db.upsert_tuner_core_state(eng._session_id, cs)
        db.set_resume_crash_streak(eng._session_id, 2)
        eng._run_next = lambda: None

        eng._on_test_finished(5, True, "", "", 60.0, 0.0)

        assert db.get_resume_crash_streak(eng._session_id) == 0

    @pytest.mark.parametrize(
        ("error_type", "message", "events"),
        [
            ("thermal", "temperature limit", ""),
            ("stall", "worker stalled", ""),
            ("killed", "worker killed externally", ""),
            (
                "mce_unattributed",
                "machine check without a CPU",
                json.dumps([{"cpu": -1, "corrected": False, "message": "uncore MCE"}]),
            ),
        ],
    )
    def test_hunt_non_verdict_preserves_crash_streak_and_learned_offsets(
        self,
        db,
        topo_dual_ccd_x3d,
        mock_backend,
        monkeypatch,
        error_type,
        message,
        events,
    ):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        _seed_confirmed_validating(eng, db, BEST, BASELINES)
        db.set_resume_crash_streak(eng._session_id, 3)
        learned = {c: (cs.current_offset, cs.best_offset) for c, cs in eng._core_states.items()}
        monkeypatch.setattr("corecycler.tuner.engine.QTimer.singleShot", lambda *_: None)
        eng._start_worker = lambda *a, **k: None
        eng._start_multi_core_worker = lambda *a, **k: None

        eng._start_hunt(loaded=[5])
        eng._on_test_finished(5, False, message, error_type, 60.0, 0.0, events)

        assert db.get_resume_crash_streak(eng._session_id) == 3
        assert {c: (cs.current_offset, cs.best_offset) for c, cs in eng._core_states.items()} == learned

    def test_passing_probes_and_exhausted_hunt_preserve_crash_streak_and_learned_offsets(
        self, db, topo_dual_ccd_x3d, mock_backend, monkeypatch
    ):
        eng = _make_engine(
            db,
            topo_dual_ccd_x3d,
            mock_backend,
            max_unattributed_crash_hunts=1,
            suspicion_min_failures=99,
        )
        _seed_confirmed_validating(eng, db, BEST, BASELINES)
        db.set_resume_crash_streak(eng._session_id, 3)
        learned = {c: (cs.current_offset, cs.best_offset) for c, cs in eng._core_states.items()}
        monkeypatch.setattr("corecycler.tuner.engine.QTimer.singleShot", lambda *_: None)
        eng._start_worker = lambda *a, **k: None
        eng._start_multi_core_worker = lambda *a, **k: None

        eng._start_hunt(loaded=[5])
        eng._on_test_finished(5, True, "", "", 60.0, 0.0)

        assert db.get_resume_crash_streak(eng._session_id) == 3
        assert {c: (cs.current_offset, cs.best_offset) for c, cs in eng._core_states.items()} == learned

        while eng._hunt is not None:
            eng._run_next_hunt_slot()
            if eng._hunt is not None:
                eng._on_test_finished(5, True, "", "", 60.0, 0.0)

        assert db.get_resume_crash_streak(eng._session_id) == 3
        assert {c: (cs.current_offset, cs.best_offset) for c, cs in eng._core_states.items()} == learned

    def test_completed_hunt_probe_then_reboot_is_not_another_reproduction(
        self, db, topo_dual_ccd_x3d, mock_backend, monkeypatch
    ):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend, max_unattributed_crash_hunts=3)
        _seed_confirmed_validating(eng, db, BEST, BASELINES)
        monkeypatch.setattr("corecycler.tuner.engine.QTimer.singleShot", lambda *_: None)
        eng._start_worker = lambda *a, **k: None
        eng._start_multi_core_worker = lambda *a, **k: None
        eng._start_hunt(loaded=[5])
        eng._on_test_finished(5, True, "", "", 60.0, 0.0)
        eng._run_next_hunt_slot()

        eng._on_test_finished(5, True, "", "", 60.0, 0.0)

        session = db.get_tuner_session(eng._session_id)
        completed = bisect.HuntState.from_json(session.hunt_state)
        assert completed is not None
        assert completed.armed is False
        eng._forensics = lambda *a, **k: ([], True)
        eng._run_next_hunt_slot = lambda: None

        crashed, pending_hunt = eng._attribute_crash_after_reboot(session)
        eng._hunt = bisect.HuntState.from_json(session.hunt_state)

        assert crashed == []
        assert pending_hunt is True
        assert eng._hunt is not None
        assert eng._hunt.to_json() == completed.to_json()

    def test_a_pair_that_fails_only_together_is_backed_off_as_a_pair(
        self, db, topo_dual_ccd_x3d, mock_backend, monkeypatch
    ):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend, max_unattributed_crash_hunts=2)
        _seed_confirmed_validating(eng, db, BEST, BASELINES)
        monkeypatch.setattr("corecycler.tuner.engine.QTimer.singleShot", lambda *_: None)
        eng._start_worker = lambda *a, **k: None
        eng._start_multi_core_worker = lambda *a, **k: None
        messages: list[str] = []
        eng.log_message.connect(messages.append)

        eng._start_hunt(loaded=[5])
        while eng._hunt is not None:
            both_live = {4, 5} <= set(eng._hunt.in_flight)
            eng._on_test_finished(5, not both_live, "mprime error: FATAL ERROR" if both_live else "", "", 60.0, 0.0)
            if eng._hunt is not None:
                eng._run_next_hunt_slot()

        moved = {c for c, cs in eng._core_states.items() if cs.current_offset != BEST[c]}
        assert moved == {4, 5}
        assert all(eng._core_states[c].current_offset > BEST[c] for c in moved)
        assert any("[4, 5]" in m and "together" in m for m in messages)

    @staticmethod
    def _exhaust_hunt(eng, loaded: list[int]) -> None:
        eng._start_hunt(loaded=loaded)
        while eng._hunt is not None:
            eng._on_test_finished(loaded[0], True, "", "", 60.0, 0.0)
            if eng._hunt is not None:
                eng._run_next_hunt_slot()

    def test_an_unexplained_crash_fails_the_step_that_was_running(
        self, db, topo_dual_ccd_x3d, mock_backend, monkeypatch
    ):
        """Session 12: core 3 advanced from -40 to -41 after 3 freezes and 1 pass,
        because an exhausted hunt left the step unanswered."""
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend, max_unattributed_crash_hunts=1, suspicion_min_failures=99)
        _seed_confirmed_validating(eng, db, BEST, BASELINES)
        searching = eng._core_states[3]
        searching.phase = TunerPhase.COARSE_SEARCH
        searching.best_offset = -38
        db.upsert_tuner_core_state(eng._session_id, searching)
        db.update_tuner_session_status(eng._session_id, "running")
        monkeypatch.setattr("corecycler.tuner.engine.QTimer.singleShot", lambda *_: None)
        eng._start_worker = lambda *a, **k: None
        eng._start_multi_core_worker = lambda *a, **k: None

        self._exhaust_hunt(eng, [3])

        assert searching.current_offset > BEST[3]
        assert searching.phase is not TunerPhase.COARSE_SEARCH
        assert all(cs.current_offset == BEST[c] for c, cs in eng._core_states.items() if c != 3)

    def test_exhausted_hunts_feed_the_suspicion_fallback(self, db, topo_dual_ccd_x3d, mock_backend, monkeypatch):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend, max_unattributed_crash_hunts=1, suspicion_min_failures=1)
        _seed_confirmed_validating(eng, db, BEST, BASELINES)
        eng._core_states[7].suspicion = 1000.0
        monkeypatch.setattr("corecycler.tuner.engine.QTimer.singleShot", lambda *_: None)
        eng._start_worker = lambda *a, **k: None
        eng._start_multi_core_worker = lambda *a, **k: None

        self._exhaust_hunt(eng, [5])

        assert eng._core_states[7].current_offset > BEST[7]
        assert all(cs.current_offset == BEST[c] for c, cs in eng._core_states.items() if c != 7)

    def test_search_incidents_do_not_pre_trip_the_validation_breaker(
        self, db, topo_dual_ccd_x3d, mock_backend, monkeypatch
    ):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend, max_unattributed_crash_hunts=1, suspicion_min_failures=99)
        _seed_confirmed_validating(eng, db, BEST, BASELINES)
        monkeypatch.setattr("corecycler.tuner.engine.QTimer.singleShot", lambda *_: None)
        eng._start_worker = lambda *a, **k: None
        eng._start_multi_core_worker = lambda *a, **k: None
        self._exhaust_hunt(eng, [5])
        assert db.get_unattributed_crashes(eng._session_id) == 1
        eng._run_validation_next = lambda: None

        eng._enter_auto_validation(dict(BEST))

        assert db.get_unattributed_crashes(eng._session_id) == 0


class TestForeignMceEvidence:
    def test_parse_groups_by_core_and_severity(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        _seed_confirmed_validating(eng, db, BEST, BASELINES)
        payload = json.dumps(
            [
                {"cpu": 5, "bank": 0, "corrected": True, "message": "a", "raw_ts": 1.0},
                {"cpu": 5, "bank": 0, "corrected": False, "message": "b", "raw_ts": 2.0},
                {"cpu": 6, "bank": 0, "corrected": True, "message": "c", "raw_ts": 3.0},
                {"cpu": 2, "bank": 0, "corrected": True, "message": "own", "raw_ts": 4.0},
                {"cpu": -1, "bank": -1, "corrected": False, "message": "panic", "raw_ts": 5.0},
            ]
        )

        foreign = eng._foreign_mce_by_core(tested_core=2, mce_json=payload)

        assert sorted(foreign) == [5, 6]
        assert foreign[5]["corrected"] is False  # any uncorrected wins
        assert foreign[6]["corrected"] is True

    def test_malformed_payload_is_no_evidence(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        assert eng._foreign_mce_by_core(0, "not json") == {}
        assert eng._foreign_mce_by_core(0, "") == {}
        assert eng._foreign_mce_by_core(0, json.dumps({"cpu": 1})) == {}

    def test_corrected_evidence_backs_off_one_step_and_reearns(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend, fine_step=1)
        _seed_confirmed_validating(eng, db, BEST, BASELINES)
        eng._co_applied[5] = -30

        eng._apply_foreign_evidence({5: {"corrected": True, "messages": ["m"]}})

        cs = eng._core_states[5]
        assert cs.backoff_fail_bound == -30
        assert cs.current_offset == -29  # exactly one fine step
        assert cs.best_offset == -29
        assert cs.phase == TunerPhase.BACKOFF_PRECONFIRM  # must re-earn
        assert cs.crash_count == 0  # a warning, not a crash
        rows = db.get_tuner_test_log(eng._session_id, core_id=5)
        assert any(r["phase"] == "mce_evidence" and r["error_type"] == "mce" for r in rows)

    def test_uncorrected_evidence_gets_crash_grade_penalty(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _make_engine(
            db,
            topo_dual_ccd_x3d,
            mock_backend,
            fine_step=1,
            crash_penalty_steps=3,
        )
        _seed_confirmed_validating(eng, db, BEST, BASELINES)
        eng._co_applied[6] = -41

        eng._apply_foreign_evidence({6: {"corrected": False, "messages": ["m"]}})

        cs = eng._core_states[6]
        assert cs.backoff_fail_bound == -41
        assert cs.current_offset == -38  # three steps
        assert cs.crash_count == 1

    def test_error_at_stock_changes_no_state(self, db, topo_dual_ccd_x3d, mock_backend):
        """An MCE at CO=0 is not a Curve Optimizer problem — never walk zero."""
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        _seed_confirmed_validating(eng, db, BEST, BASELINES)
        eng._co_applied[4] = 0
        before = eng._core_states[4].best_offset

        eng._apply_foreign_evidence({4: {"corrected": True, "messages": ["m"]}})

        cs = eng._core_states[4]
        assert cs.best_offset == before
        assert cs.phase == TunerPhase.CONFIRMED
        rows = db.get_tuner_test_log(eng._session_id, core_id=4)
        assert any(r["phase"] == "mce_evidence" for r in rows)  # recorded, loudly

    def test_survival_marking_excludes_named_cores(self, db):
        sid = db.create_tuner_session("{}", "1.0", "TestCPU")
        db.journal_co_intent(sid, 0, -20, survived=False)
        db.journal_co_intent(sid, 5, -30, survived=False)

        db.journal_mark_survived(sid, exclude_cores=[5])

        survived = db.journal_survived_values(sid)
        assert 0 in survived
        assert 5 not in survived
        assert (5, -30) in db.journal_suspects(sid)


class TestPickReport:
    def _result(self, core_id: int, passed: bool):
        return SimpleNamespace(core_id=core_id, passed=passed)

    def test_primary_pass_with_no_other_failures(self):
        results = {0: [self._result(0, True)], 1: [self._result(1, True)]}
        core, report = _pick_report(results, primary=0)
        assert core == 0
        assert report.passed is True

    def test_failure_on_any_core_outranks_primary_pass(self):
        """A failure on any core in the batch must outrank the primary's pass."""
        results = {
            0: [self._result(0, True)],
            5: [self._result(5, False)],
            7: [self._result(7, True)],
        }
        core, report = _pick_report(results, primary=0)
        assert core == 5
        assert report.passed is False

    def test_primary_failure_is_reported_directly(self):
        results = {0: [self._result(0, False)], 5: [self._result(5, False)]}
        core, report = _pick_report(results, primary=0)
        assert core == 0

    def test_no_results_returns_none(self):
        core, report = _pick_report({}, primary=3)
        assert core == 3
        assert report is None


class TestDriftAgainstJournal:
    def test_no_false_drift_when_smu_holds_tuner_written_values(self, db, topo_dual_ccd_x3d, mock_backend, monkeypatch):
        """Reopening mid-validation: the SMU holding exactly what the tuner
        wrote (the confirmed offsets) is not drift."""
        import corecycler.tuner.engine as engine_mod

        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        _seed_confirmed_validating(eng, db, BEST, BASELINES)
        for cs in eng._core_states.values():
            cs.in_test = False
            db.upsert_tuner_core_state(eng._session_id, cs)
        for core_id, offset in BEST.items():
            eng._smu.written[core_id] = offset  # SMU == tuner's last write
        monkeypatch.setattr(engine_mod, "_rebooted_since", lambda *a, **k: False)

        eng._start_worker = lambda *a, **k: None  # no real worker threads
        drift_reports: list[str] = []
        eng.co_drift_detected.connect(drift_reports.append)
        eng.resume(eng._session_id)

        assert drift_reports == []

    def test_third_party_change_is_reported_against_last_write(self, db, topo_dual_ccd_x3d, mock_backend, monkeypatch):
        import corecycler.tuner.engine as engine_mod

        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        _seed_confirmed_validating(eng, db, BEST, BASELINES)
        for cs in eng._core_states.values():
            cs.in_test = False
            db.upsert_tuner_core_state(eng._session_id, cs)
        for core_id, offset in BEST.items():
            eng._smu.written[core_id] = offset
        eng._smu.written[3] = -10  # someone changed core 3 behind our back
        monkeypatch.setattr(engine_mod, "_rebooted_since", lambda *a, **k: False)

        eng._start_worker = lambda *a, **k: None  # no real worker threads
        drift_reports: list[str] = []
        eng.co_drift_detected.connect(drift_reports.append)
        eng.resume(eng._session_id)

        assert len(drift_reports) == 1
        drift = json.loads(drift_reports[0])
        assert drift == {"3": {"expected": BEST[3], "actual": -10}}


class TestNarrativePersistence:
    def test_log_messages_become_durable_events(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        eng.log_message.emit("the story survives the terminal")
        events = db.get_tuner_events(eng._session_id)
        assert [e["message"] for e in events] == ["the story survives the terminal"]

    def test_narrative_without_session_is_dropped(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        eng._session_id = None
        eng.log_message.emit("nowhere to persist")  # must not raise


def _mce_payload(cpu: int) -> str:
    return json.dumps(
        [{"cpu": cpu, "bank": 0, "corrected": True, "message": CPU5_CORRECTED.format(cpu=cpu), "raw_ts": 1.0}]
    )


class TestUnattributedMcePayload:
    """A machine check that names NO core taints every resident value.

    Surviving a stress test does not clear an error the hardware just reported,
    and with no CPU named there is no core to exclude -- so the whole resident
    set must stay unproven. Journalling it survived would record a value the
    hardware complained about as proven, and the search would then treat it as
    a safe floor.
    """

    def test_a_payload_naming_no_cpu_is_detected(self):
        from corecycler.tuner.engine import _has_unattributed_mce

        assert _has_unattributed_mce(_mce_payload(-1)) is True

    def test_an_attributed_or_absent_payload_is_not(self):
        from corecycler.tuner.engine import _has_unattributed_mce

        assert _has_unattributed_mce(_mce_payload(5)) is False
        assert _has_unattributed_mce("") is False
        assert _has_unattributed_mce("not json") is False
        assert _has_unattributed_mce(json.dumps({"cpu": -1})) is False

    def _mark_survived_calls(self, db, topo_single_ccd, mock_backend, mce_json, monkeypatch):
        eng = _make_engine(db, topo_single_ccd, mock_backend)
        sid = eng._session_id
        cs = CoreState(core_id=0, phase=TunerPhase.CONFIRMING, current_offset=-20, best_offset=-20, baseline_offset=0)
        eng._core_states = {0: cs}
        db.upsert_tuner_core_state(sid, cs)
        eng._co_applied[0] = -20
        db.journal_co_intent(sid, 0, -20, survived=False)
        calls = []
        monkeypatch.setattr(tp, "journal_mark_survived", lambda *a, **kw: calls.append(kw))
        eng._on_test_finished(0, True, "", "", 1.0, 0.0, mce_json, "")
        return calls

    def test_a_clean_pass_marks_the_resident_set_survived(self, db, topo_single_ccd, mock_backend, monkeypatch):
        assert self._mark_survived_calls(db, topo_single_ccd, mock_backend, "", monkeypatch)

    def test_a_pass_carrying_an_unattributed_mce_marks_nothing_survived(
        self, db, topo_single_ccd, mock_backend, monkeypatch
    ):
        calls = self._mark_survived_calls(db, topo_single_ccd, mock_backend, _mce_payload(-1), monkeypatch)
        assert calls == []


class TestResumeHuntAttribution:
    def test_unattributed_hunt_preserves_the_last_workload_breadcrumb(self, db, topo_dual_ccd_x3d, mock_backend):
        engine = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        session = _seed_confirmed_validating(engine, db, BEST, BASELINES)
        engine._forensics = lambda *_a, **_kw: ([], True)
        engine._read_breadcrumb = lambda: "mprime AVX2 large"
        messages = []
        engine.log_message.connect(messages.append)

        crashed, pending_hunt = engine._attribute_crash_after_reboot(session)

        assert crashed == []
        assert pending_hunt is True
        assert any("mprime AVX2 large" in message for message in messages)

    def test_a_journal_suspect_already_attributed_is_not_penalized_twice(self, db, topo_single_ccd, mock_backend):
        engine = _make_engine(db, topo_single_ccd, mock_backend)
        cs = CoreState(
            core_id=0,
            phase=TunerPhase.CONFIRMING,
            current_offset=-20,
            best_offset=-20,
            baseline_offset=0,
        )
        engine._core_states = {0: cs}
        db.upsert_tuner_core_state(engine._session_id, cs)
        tp.journal_co_intent(db, engine._session_id, 0, -20, survived=False)

        assert engine._handle_journal_suspects({0}) == []
        restored = db.get_tuner_core_states(engine._session_id)[0]
        assert restored.current_offset == -20
        assert restored.crash_count == 0


def _armed_probe_state(engine):
    state = bisect.begin(sorted(BEST), [5, 6])
    bisect.next_live_set(state)
    bisect.record(
        state,
        reproduced=False,
        control_confirmations=engine._config.control_run_confirmations,
        max_no_reproduce=engine._config.max_unattributed_crash_hunts,
    )
    bisect.next_live_set(state)
    state.vector = dict(BEST)
    state.workload = engine._workload_snapshot(engine._core_states[5])
    state.armed = True
    return state


class TestPersistedHuntResume:
    @pytest.mark.parametrize("rebooted", [True, False])
    def test_corrupt_hunting_state_pauses_before_writing_co(
        self, db, topo_dual_ccd_x3d, mock_backend, monkeypatch, rebooted
    ):
        import corecycler.tuner.engine as engine_mod

        engine = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        _seed_confirmed_validating(engine, db, BEST, BASELINES)
        db.set_hunt_state(engine._session_id, "{corrupt")
        db.update_tuner_session_status(engine._session_id, "hunting")
        before = {
            core_id: (cs.phase, cs.current_offset, cs.best_offset)
            for core_id, cs in db.get_tuner_core_states(engine._session_id).items()
        }
        messages = []
        engine.log_message.connect(messages.append)
        monkeypatch.setattr(engine_mod, "_rebooted_since", lambda *_a, **_kw: rebooted)

        engine.resume(engine._session_id)

        after = {
            core_id: (cs.phase, cs.current_offset, cs.best_offset)
            for core_id, cs in db.get_tuner_core_states(engine._session_id).items()
        }
        assert engine._smu.written == {}
        assert engine.status == "paused"
        assert db.get_tuner_session(engine._session_id).status == "paused"
        assert after == before
        assert any("Persisted hunt state is invalid:" in message for message in messages)

    @pytest.mark.parametrize("rebooted", [True, False])
    def test_armed_hunting_state_requeues_the_exact_probe(
        self, db, topo_dual_ccd_x3d, mock_backend, monkeypatch, rebooted
    ):
        import corecycler.tuner.engine as engine_mod

        engine = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        _seed_confirmed_validating(engine, db, BEST, BASELINES)
        state = _armed_probe_state(engine)
        crashed_live = list(state.in_flight)
        workload = dict(state.workload)
        db.set_hunt_state(engine._session_id, state.to_json())
        db.update_tuner_session_status(engine._session_id, "hunting")
        launches = []
        monkeypatch.setattr(engine_mod, "_rebooted_since", lambda *_a, **_kw: rebooted)
        monkeypatch.setattr(engine_mod.QTimer, "singleShot", lambda *_a: None)
        engine._start_multi_core_worker = lambda cores, duration, **kwargs: launches.append(
            (list(cores), duration, kwargs["workload"])
        )

        engine.resume(engine._session_id)

        expected_residents = {core_id: BEST[core_id] if core_id in crashed_live else 0 for core_id in BEST}
        assert engine.status == "hunting"
        assert engine._hunting is True
        assert engine._hunt is not None
        assert engine._hunt.in_flight == crashed_live
        assert engine._hunt.vector == BEST
        assert engine._hunt_workload == workload
        assert launches and launches[0][0] == [5, 6]
        assert launches[0][2] == workload
        assert engine._smu.written == expected_residents

    @pytest.mark.parametrize("rebooted", [True, False])
    def test_paused_session_restores_its_persisted_hunt(
        self, db, topo_dual_ccd_x3d, mock_backend, monkeypatch, rebooted
    ):
        import corecycler.tuner.engine as engine_mod

        engine = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        _seed_confirmed_validating(engine, db, BEST, BASELINES)
        state = _armed_probe_state(engine)
        state.armed = False
        db.set_hunt_state(engine._session_id, state.to_json())
        db.update_tuner_session_status(engine._session_id, "paused")
        monkeypatch.setattr(engine_mod, "_rebooted_since", lambda *_a, **_kw: rebooted)
        monkeypatch.setattr(engine_mod.QTimer, "singleShot", lambda *_a: None)
        engine._run_next_hunt_slot = lambda: None

        engine.resume(engine._session_id)

        assert engine._hunt is not None
        assert engine._hunt.to_json() == state.to_json()
        assert engine.status == "hunting"
        assert db.get_tuner_session(engine._session_id).status == "hunting"

    def test_resume_discards_another_sessions_in_memory_hunt(self, db, topo_dual_ccd_x3d, mock_backend, monkeypatch):
        import corecycler.tuner.engine as engine_mod

        first = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        _seed_confirmed_validating(first, db, BEST, BASELINES)
        first_state = _armed_probe_state(first)
        first._hunt = first_state

        second = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        _seed_confirmed_validating(second, db, BEST, BASELINES)
        second_state = _armed_probe_state(second)
        second_state.in_flight = [0, 1]
        second_state.armed = False
        db.set_hunt_state(second._session_id, second_state.to_json())
        db.update_tuner_session_status(second._session_id, "paused")
        monkeypatch.setattr(engine_mod, "_rebooted_since", lambda *_a, **_kw: False)
        first._run_next_hunt_slot = lambda: None

        first.resume(second._session_id)

        assert first._hunt is not first_state
        assert first._hunt is not None
        assert first._hunt.to_json() == second_state.to_json()

    def test_second_resume_site_rejects_corrupt_hunt_state_before_writing_co(
        self, db, topo_dual_ccd_x3d, mock_backend, monkeypatch
    ):
        import corecycler.tuner.engine as engine_mod

        engine = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        _seed_confirmed_validating(engine, db, BEST, BASELINES)
        state = _armed_probe_state(engine)
        state.armed = False
        db.set_hunt_state(engine._session_id, state.to_json())
        db.update_tuner_session_status(engine._session_id, "validating")
        smu = engine._smu
        messages = []
        engine.log_message.connect(messages.append)
        monkeypatch.setattr(engine_mod, "_rebooted_since", lambda *_a, **_kw: True)
        monkeypatch.setattr(engine_mod, "last_boot_ended_cleanly", lambda **_kw: False)

        def corrupt_pending_hunt(session, *_args):
            session.hunt_state = "not-json"
            engine._smu = None
            return [], True

        engine._attribute_crash_after_reboot = corrupt_pending_hunt

        engine.resume(engine._session_id)

        assert smu.written == {}
        assert engine.status == "paused"
        assert db.get_tuner_session(engine._session_id).status == "paused"
        assert any("Persisted hunt state is invalid:" in message for message in messages)


class TestPersistedHuntSafety:
    def test_crash_attribution_rejects_corrupt_hunt_state(self, db, topo_dual_ccd_x3d, mock_backend):
        engine = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        db.set_hunt_state(engine._session_id, "[]")
        messages = []
        engine.log_message.connect(messages.append)
        session = db.get_tuner_session(engine._session_id)

        crashed, pending_hunt = engine._attribute_crash_after_reboot(session)

        assert crashed == []
        assert pending_hunt is False
        assert engine._smu.written == {}
        assert engine.status == "paused"
        assert any("Persisted hunt state is invalid:" in message for message in messages)

    def test_hunt_candidates_use_live_journal_residents(self, db, topo_dual_ccd_x3d, mock_backend):
        engine = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        _seed_confirmed_validating(engine, db, BEST, BASELINES)
        for core_id in BEST:
            tp.journal_co_intent(db, engine._session_id, core_id, 0, survived=False)
        tp.journal_co_intent(db, engine._session_id, 2, -31, survived=False)
        tp.journal_co_intent(db, engine._session_id, 5, -27, survived=False)
        tp.journal_co_intent(db, engine._session_id, 99, -40, survived=False)

        assert engine._hunt_candidates() == [2, 5]

    def test_disarming_confirm_probe_requeues_suspect_without_a_verdict(self, db, topo_dual_ccd_x3d, mock_backend):
        engine = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        _seed_confirmed_validating(engine, db, BEST, BASELINES)
        state = bisect.HuntState(
            candidates=sorted(BEST),
            stage=bisect.Stage.CONFIRM,
            pending=[[6, 7]],
            suspect=5,
            in_flight=[0, 1, 2, 3, 4, 6, 7],
            loaded=[5, 6],
            armed=True,
            vector=dict(BEST),
            workload=engine._workload_snapshot(engine._core_states[5]),
        )
        engine._hunt = state
        engine._hunting = True
        engine._core_states[5].in_test = True
        db.upsert_tuner_core_state(engine._session_id, engine._core_states[5])
        db.set_hunt_state(engine._session_id, state.to_json())

        engine._requeue_hunt_probe()

        restored = bisect.HuntState.from_json(db.get_tuner_session(engine._session_id).hunt_state)
        assert restored is not None
        assert restored.armed is False
        assert restored.stage is bisect.Stage.PROBE
        assert restored.pending == [[5], [6, 7]]
        assert restored.in_flight == []
        assert restored.found == []
        assert restored.exonerated == []
        assert db.get_tuner_core_states(engine._session_id)[5].in_test is False


class TestHuntExecution:
    def test_start_persists_hunting_before_the_first_probe(self, db, topo_dual_ccd_x3d, mock_backend):
        engine = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        _seed_confirmed_validating(engine, db, BEST, BASELINES)
        engine._pending_hunt_vector = dict(BEST)
        statuses = []
        engine._run_next_hunt_slot = lambda: statuses.append(db.get_tuner_session(engine._session_id).status)

        engine._start_hunt(loaded=[5, 6])

        assert statuses == ["hunting"]

    @pytest.mark.parametrize(
        ("kind", "loaded", "expected"),
        [
            ("rapid_transition", [5, 6], "rapid_transition"),
            ("soak", [5, 6], "soak"),
            ("parallel", [5, 6], "parallel"),
            ("solo", [5], "solo"),
        ],
    )
    def test_hunt_replays_the_checkpointed_worker_kind(
        self, db, topo_dual_ccd_x3d, mock_backend, monkeypatch, kind, loaded, expected
    ):
        engine = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        _seed_confirmed_validating(engine, db, BEST, BASELINES)
        state = bisect.begin(sorted(BEST), loaded)
        bisect.next_live_set(state)
        bisect.record(
            state,
            reproduced=False,
            control_confirmations=engine._config.control_run_confirmations,
            max_no_reproduce=engine._config.max_unattributed_crash_hunts,
        )
        state.vector = dict(BEST)
        state.workload = {**engine._workload_snapshot(engine._core_states[loaded[0]]), "kind": kind}
        engine._hunt = state
        engine._hunt_workload = state.workload
        engine._hunting = True
        engine._apply_hunt_mask = lambda _live: True
        launched = []
        monkeypatch.setattr(
            engine,
            "_start_rapid_transition_worker",
            lambda cores, duration, workload: launched.append(("rapid_transition", cores, workload)),
            raising=False,
        )
        monkeypatch.setattr(
            engine,
            "_start_soak_worker",
            lambda cores, duration, workload: launched.append(("soak", cores, workload)),
            raising=False,
        )
        engine._start_multi_core_worker = lambda cores, duration, **kwargs: launched.append(
            ("parallel", cores, kwargs["workload"])
        )
        engine._start_worker = lambda core, duration, **kwargs: launched.append(("solo", [core], state.workload))

        engine._run_next_hunt_slot()

        assert launched == [(expected, loaded, state.workload)]

    def test_immediate_stage4_hunt_keeps_the_rapid_transition_workload(
        self, db, topo_dual_ccd_x3d, mock_backend, monkeypatch
    ):
        import corecycler.tuner.engine as engine_mod

        engine = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        _seed_confirmed_validating(engine, db, BEST, BASELINES)
        engine._validation_stage = 4
        engine._validation_core_order = [5, 6]
        engine._apply_validation_offsets = lambda *_a: True
        launched = []
        monkeypatch.setattr(engine_mod, "CoreScheduler", lambda **_kw: SimpleNamespace())
        monkeypatch.setattr(engine_mod, "_RapidTransitionWorker", lambda *_a, **_kw: SimpleNamespace())
        engine._launch_worker = lambda _worker, workload, _cores, **_kw: launched.append(workload)

        engine._run_validation_stage4()
        engine._run_next_hunt_slot = lambda: None
        engine._on_validation_test_finished(5, False, 17.0)

        assert launched[0]["kind"] == "rapid_transition"
        assert engine._hunt is not None
        assert engine._hunt.workload == launched[0]

    @pytest.mark.parametrize("failure_source", ["outside_live_set", "zero_offset"])
    def test_failure_reported_by_a_stock_core_requeues_without_a_verdict(
        self, db, topo_dual_ccd_x3d, mock_backend, failure_source
    ):
        engine = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        state = bisect.begin(sorted(BEST), [5, 6])
        bisect.next_live_set(state)
        bisect.record(
            state,
            reproduced=False,
            control_confirmations=engine._config.control_run_confirmations,
            max_no_reproduce=engine._config.max_unattributed_crash_hunts,
        )
        in_flight = bisect.next_live_set(state)
        assert in_flight is not None
        vector = dict(BEST)
        if failure_source == "outside_live_set":
            reported = next(core for core in BEST if core not in in_flight)
        else:
            reported = in_flight[0]
            vector[reported] = 0
        state.vector = vector
        engine._hunt = state
        engine._hunting = True
        requeued = []
        engine._requeue_hunt_probe = lambda: requeued.append(True)
        engine.pause = lambda: None

        engine._on_hunt_slot_finished(reported, False, "computation", {})

        assert requeued == [True]
        assert state.in_flight == in_flight
        assert state.no_reproduce == 0

    def test_platform_fault_does_not_overwrite_failed_stock_restoration(self, db, topo_dual_ccd_x3d, mock_backend):
        engine = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        state = bisect.begin(sorted(BEST), [5, 6])
        engine._hunt = state
        engine._hunting = True
        engine._restore_hunt_stock = lambda: False
        db.update_tuner_session_status(engine._session_id, "profile_quarantined")
        engine._set_status("profile_quarantined")
        faults = []
        engine.platform_fault.connect(faults.append)

        engine._platform_fault("stock probe failed")

        assert engine.status == "profile_quarantined"
        assert db.get_tuner_session(engine._session_id).status == "profile_quarantined"
        assert engine._hunt is state
        assert faults == []
