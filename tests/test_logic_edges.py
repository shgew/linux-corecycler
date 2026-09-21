"""Edge coverage for backend helpers pending move to their owning modules."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from corecycler.engine.backends.base import StressBackend, StressConfig
from corecycler.engine.backends.stressapptest import StressapptestBackend


class TestClassifyExitCode:
    def test_killed_by_us(self):
        assert StressBackend.classify_exit_code(-15) == "killed_by_us"

    def test_crash_signal(self):
        assert StressBackend.classify_exit_code(-11) == "crash:SIGSEGV"

    def test_normal_exit_is_none(self):
        assert StressBackend.classify_exit_code(0) is None


class TestStressapptestPrepareCleanup:
    def test_prepare_creates_work_dir(self, tmp_path):
        work = tmp_path / "sat"
        StressapptestBackend().prepare(work, StressConfig())
        assert work.exists()
