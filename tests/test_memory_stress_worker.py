"""Memory stress Supervisor coverage."""

from __future__ import annotations

import sys as _sys
from unittest.mock import MagicMock

import pytest

if not hasattr(_sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("GUI tests require real PySide6", allow_module_level=True)

from corecycler.engine.backends.base import StressResult


def _qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


class _Supervisor:
    def __init__(self, result: StressResult | None) -> None:
        self.result = result
        self.calls = []

    def run(self, lanes, config_for, duration):
        self.calls.append((lanes, config_for(lanes[0]), duration))
        return {lanes[0].core_id: self.result}


def _run(result: StressResult | None, *, stopped: bool = False):
    from corecycler.gui.memory_tab import _StressWorker

    _qapp()
    supervisor = _Supervisor(result)
    worker = _StressWorker(
        "stressapptest", 1, supervisor_factory=lambda **_kwargs: supervisor, detector_factory=MagicMock
    )
    seen = []
    worker.done.connect(lambda passed, output: seen.append((passed, output)))
    if stopped:
        worker.stop()
    worker.run()
    return seen, supervisor


class TestStressWorker:
    def test_clean_supervisor_result_passes(self):
        result = StressResult(core_id=0, passed=True, duration_seconds=60.0)
        seen, supervisor = _run(result)
        assert seen == [(True, "Memory stress completed")]
        lanes, config, deadline = supervisor.calls[0]
        assert lanes[0].cpus
        assert config.test_seconds == 60
        assert deadline > config.test_seconds

    @pytest.mark.parametrize("error_type", ["timeout", "startup", "thermal", "mce"])
    def test_supervisor_failures_never_pass(self, error_type):
        result = StressResult(
            core_id=0,
            passed=False,
            duration_seconds=1.0,
            error_message=f"{error_type} failure",
            error_type=error_type,
        )
        seen, _supervisor = _run(result)
        assert seen == [(False, f"{error_type} failure")]

    def test_user_stop_never_passes_even_if_runner_returns_pass(self):
        result = StressResult(core_id=0, passed=True, duration_seconds=1.0)
        seen, _supervisor = _run(result, stopped=True)
        assert seen == [(False, "Memory stress stopped")]

    def test_missing_verdict_is_failure(self):
        seen, _supervisor = _run(None)
        assert seen == [(False, "Memory stress stopped before a verdict")]

    def test_unknown_tool_fails_without_running_supervisor(self):
        from corecycler.gui.memory_tab import _StressWorker

        _qapp()
        factory = MagicMock()
        worker = _StressWorker("bogus-tool", 1, supervisor_factory=factory, detector_factory=MagicMock)
        seen = []
        worker.done.connect(lambda passed, output: seen.append((passed, output)))
        worker.run()
        assert seen == [(False, "Unknown tool: bogus-tool")]
        factory.assert_not_called()

    def test_force_teardown_stops_and_delegates_to_the_active_supervisor(self):
        from corecycler.gui.memory_tab import _StressWorker

        _qapp()
        worker = _StressWorker("stressapptest", 1, detector_factory=MagicMock)
        supervisor = MagicMock()
        supervisor.force_teardown.return_value = False
        worker._supervisor = supervisor

        assert worker.force_teardown() is False
        assert worker._stop_event.is_set()
        supervisor.force_teardown.assert_called_once_with()

        worker._supervisor = None
        assert worker.force_teardown() is True


class TestMemoryStressBackend:
    def test_builds_supervised_payload_commands(self, tmp_path):
        from corecycler.engine.backends.base import StressConfig
        from corecycler.gui.memory_tab import _MemoryStressBackend

        sat = _MemoryStressBackend("stressapptest")
        sat._binary = "/tools/stressapptest"
        sat_cmd = sat.get_command(StressConfig(test_seconds=90, memory_mb=2048), tmp_path)
        assert sat_cmd == ["/tools/stressapptest", "-W", "-M", "2048", "-s", "90"]

        stress_ng = _MemoryStressBackend("stress-ng --vm")
        stress_ng._binary = "/tools/stress-ng"
        ng_cmd = stress_ng.get_command(StressConfig(test_seconds=45), tmp_path)
        assert ng_cmd == [
            "/tools/stress-ng",
            "--vm",
            "1",
            "--vm-bytes",
            "75%",
            "--verify",
            "--timeout",
            "45s",
        ]

    def test_parser_requires_a_real_stressapptest_pass(self):
        from corecycler.gui.memory_tab import _MemoryStressBackend

        backend = _MemoryStressBackend("stressapptest")
        assert backend.parse_output("Status: PASS", "", 0) == (True, None)
        assert backend.parse_output("finished", "", 0)[0] is False
        assert backend.parse_output("Status: PASS", "", 2)[0] is False
        assert backend.parse_output("Status: PASS", "miscompare", 0)[0] is False

    def test_stress_ng_clean_exit_is_a_verdict_and_prepare_creates_directory(self, tmp_path):
        from corecycler.engine.backends.base import StressConfig, StressMode
        from corecycler.gui.memory_tab import _MemoryStressBackend

        backend = _MemoryStressBackend("stress-ng --vm")
        assert backend.parse_output("", "", 0) == (True, None)
        assert backend.get_supported_modes() == [StressMode.SSE]
        work_dir = tmp_path / "memory"
        backend.prepare(work_dir, StressConfig())
        assert work_dir.is_dir()


class TestStressWorkerFailures:
    def test_no_affinity_fails_without_running_supervisor(self, monkeypatch):
        import corecycler.gui.memory_tab as mt

        _qapp()
        supervisor = _Supervisor(None)
        monkeypatch.setattr(mt.os, "sched_getaffinity", lambda _pid: set())
        worker = mt._StressWorker(
            "stressapptest", 1, supervisor_factory=lambda **_kwargs: supervisor, detector_factory=MagicMock
        )
        seen = []
        worker.done.connect(lambda passed, output: seen.append((passed, output)))
        worker.run()
        assert seen == [(False, "No online CPUs available")]
        assert supervisor.calls == []

    def test_supervisor_exception_is_a_failure(self):
        from corecycler.gui.memory_tab import _StressWorker

        _qapp()
        factory = MagicMock()
        factory.return_value.run.side_effect = RuntimeError("containment failed")
        worker = _StressWorker("stressapptest", 1, supervisor_factory=factory, detector_factory=MagicMock)
        seen = []
        worker.done.connect(lambda passed, output: seen.append((passed, output)))
        worker.run()
        assert seen == [(False, "containment failed")]
