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
from corecycler.tuner import bisect, persistence as tp
from corecycler.tuner.config import TunerConfig
from corecycler.tuner.engine import TunerEngine, _pick_report
from corecycler.tuner.state import CoreState, TunerPhase

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
    eng._session_id = tp.create_session(db, cfg, "", "")
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
        tp.save_core_state(db, sid, cs)
        db.journal_co_intent(sid, core_id, offset, survived=True)
    db.update_tuner_session_status(sid, "validating")
    return tp.get_session(db, sid)


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
        tp.save_core_state(db, eng._session_id, cs)
        db.update_tuner_session_status(eng._session_id, "validating")
        db.set_hunting_core(eng._session_id, 0)
        session = tp.get_session(db, eng._session_id)
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
            tp.save_core_state(db, eng._session_id, cs)
        db.journal_co_intent(eng._session_id, 0, BEST[0], survived=True)
        db.journal_co_intent(eng._session_id, 1, 0, survived=True)
        db.update_tuner_session_status(eng._session_id, "validating")
        db.set_hunting_core(eng._session_id, 0)
        session = tp.get_session(db, eng._session_id)
        stock_cpu = topo_dual_ccd_x3d.cores[1].logical_cpus[0]
        eng._forensics = lambda since, timeout=15.0, **kwargs: ([_event(stock_cpu)], True)

        crashed, pending_hunt = eng._attribute_crash_after_reboot(session)

        assert crashed == []
        assert pending_hunt is False
        assert eng.status == "paused"
        after = {core: (cs.current_offset, cs.best_offset, cs.crash_count) for core, cs in eng._core_states.items()}
        assert after == {0: (BEST[0], BEST[0], 0), 1: (BEST[1], BEST[1], 0)}
        assert eng._smu.written == {}

    def test_loaded_core_is_not_blamed_when_other_live_offsets_were_resident(
        self, db, topo_dual_ccd_x3d, mock_backend
    ):
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
            tp.save_core_state(db, eng._session_id, cs)
            db.journal_co_intent(eng._session_id, cs.core_id, cs.current_offset, survived=True)
        session = tp.get_session(db, eng._session_id)
        eng._forensics = lambda since, timeout=15.0, **kwargs: ([], True)

        crashed, pending_hunt = eng._attribute_crash_after_reboot(session)

        assert crashed == []
        assert pending_hunt is True
        assert eng._pending_hunt_loaded == [2]
        assert all(cs.crash_count == 0 for cs in eng._core_states.values())

    def test_only_non_stock_resident_can_be_blamed_without_a_hunt(
        self, db, topo_dual_ccd_x3d, mock_backend
    ):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend, cores_to_test=[2, 3])
        tested = CoreState(core_id=2, phase=TunerPhase.COARSE_SEARCH, current_offset=-20, in_test=True)
        stock = CoreState(core_id=3, phase=TunerPhase.COARSE_SEARCH, current_offset=0)
        eng._core_states = {2: tested, 3: stock}
        for cs in eng._core_states.values():
            tp.save_core_state(db, eng._session_id, cs)
            db.journal_co_intent(eng._session_id, cs.core_id, cs.current_offset, survived=True)
        session = tp.get_session(db, eng._session_id)
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
        tp.set_hunt_state(db, eng._session_id, state.to_json())
        tp.update_session_status(db, eng._session_id, "hunting")
        for core_id in BEST:
            db.journal_co_intent(eng._session_id, core_id, 0, survived=True)
        session = tp.get_session(db, eng._session_id)
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
        tp.save_core_state(db, eng._session_id, cs)
        tp.set_resume_crash_streak(db, eng._session_id, 2)
        eng._run_next = lambda: None

        eng._on_test_finished(5, True, "", "", 60.0, 0.0)

        assert tp.get_resume_crash_streak(db, eng._session_id) == 0


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
        rows = tp.get_test_log(db, eng._session_id, core_id=5)
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
        rows = tp.get_test_log(db, eng._session_id, core_id=4)
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
            tp.save_core_state(db, eng._session_id, cs)
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
            tp.save_core_state(db, eng._session_id, cs)
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
        events = tp.get_events(db, eng._session_id)
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
        tp.save_core_state(db, sid, cs)
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
    def test_an_in_flight_isolated_slot_penalizes_its_proven_culprit(
        self, db, topo_dual_ccd_x3d, mock_backend
    ):
        engine = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        _seed_confirmed_validating(engine, db, BEST, BASELINES)
        for cs in engine._core_states.values():
            cs.in_test = False
            tp.save_core_state(db, engine._session_id, cs)
        tp.set_hunting_core(db, engine._session_id, 5)
        engine._forensics = lambda *_a, **_kw: ([], True)
        session = tp.get_session(db, engine._session_id)

        crashed, pending_hunt = engine._attribute_crash_after_reboot(session)

        restored = tp.load_core_states(db, engine._session_id)
        assert crashed == [5]
        assert pending_hunt is False
        assert restored[5].current_offset == BEST[5] + (
            engine._config.crash_penalty_steps * engine._config.fine_step
        )
        assert restored[5].crash_count == 1
        assert all(restored[cid].crash_count == 0 for cid in restored if cid != 5)
        assert tp.get_session(db, engine._session_id).hunting_core is None
    def test_unattributed_hunt_preserves_the_last_workload_breadcrumb(
        self, db, topo_dual_ccd_x3d, mock_backend
    ):
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

    def test_a_journal_suspect_already_attributed_is_not_penalized_twice(
        self, db, topo_single_ccd, mock_backend
    ):
        engine = _make_engine(db, topo_single_ccd, mock_backend)
        cs = CoreState(
            core_id=0,
            phase=TunerPhase.CONFIRMING,
            current_offset=-20,
            best_offset=-20,
            baseline_offset=0,
        )
        engine._core_states = {0: cs}
        tp.save_core_state(db, engine._session_id, cs)
        tp.journal_co_intent(db, engine._session_id, 0, -20, survived=False)

        assert engine._handle_journal_suspects({0}) == []
        restored = tp.load_core_states(db, engine._session_id)[0]
        assert restored.current_offset == -20
        assert restored.crash_count == 0
