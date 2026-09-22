"""Comprehensive tests for all stress test backends."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from corecycler.engine.backends import available_backends
from corecycler.engine.backends.base import (
    KILLED_BY_US_CODES,
    FFTPreset,
    StressBackend,
    StressConfig,
    StressMode,
    StressResult,
)
from corecycler.engine.backends.mprime import FFT_RANGES, MODE_TO_CPU_FLAGS, MprimeBackend
from corecycler.engine.backends.stress_ng import StressNgBackend, _mode_to_method
from corecycler.engine.backends.ycruncher import MODE_TO_ALGORITHMS, VALID_COMPONENT_TESTS, YCruncherBackend
from corecycler.engine.execution import classify_error


def test_backend_registry_is_available_without_explicit_loading():
    assert set(available_backends()) == {"mprime", "stress-ng", "stressapptest", "y-cruncher"}


# ===========================================================================
# Base class tests
# ===========================================================================


class TestStressConfig:
    def test_defaults(self):
        cfg = StressConfig()
        assert cfg.mode == StressMode.SSE
        assert cfg.fft_preset == FFTPreset.SMALL
        assert cfg.threads == 1
        assert cfg.fft_min is None
        assert cfg.fft_max is None
        assert cfg.memory_mb is None

    def test_custom_config(self):
        cfg = StressConfig(
            mode=StressMode.AVX512,
            fft_preset=FFTPreset.CUSTOM,
            fft_min=100,
            fft_max=500,
            threads=4,
            memory_mb=2048,
        )
        assert cfg.mode == StressMode.AVX512
        assert cfg.fft_min == 100
        assert cfg.fft_max == 500


class TestStressResult:
    def test_defaults(self):
        r = StressResult(core_id=0, passed=True, duration_seconds=60.0)
        assert r.error_message is None
        assert r.error_type is None
        assert r.iterations_completed == 0
        assert r.last_fft_size is None


class TestStressMode:
    def test_all_modes(self):
        assert StressMode.SSE
        assert StressMode.AVX
        assert StressMode.AVX2
        assert StressMode.AVX512
        assert StressMode.CUSTOM


class TestFFTPreset:
    def test_all_presets(self):
        assert FFTPreset.SMALLEST.value == "smallest"
        assert FFTPreset.SMALL.value == "small"
        assert FFTPreset.LARGE.value == "large"
        assert FFTPreset.HUGE.value == "huge"
        assert FFTPreset.ALL.value == "all"
        assert FFTPreset.MODERATE.value == "moderate"
        assert FFTPreset.HEAVY.value == "heavy"
        assert FFTPreset.HEAVY_SHORT.value == "heavy_short"
        assert FFTPreset.CUSTOM.value == "custom"


class TestBaseBackendBinaryResolution:
    def test_is_available_caches_the_resolved_path(self, on_path):
        on_path({"mprime": "/usr/bin/mprime"})
        backend = MprimeBackend()
        assert backend.is_available() is True
        assert backend._binary == "/usr/bin/mprime"

    def test_resolution_reports_why_a_backend_is_absent(self, on_path):
        on_path({})
        resolution = MprimeBackend().resolution()
        assert resolution.path is None
        assert resolution.problem == "not found on PATH"

    def test_require_binary_resolves_lazily(self, on_path):
        on_path({"mprime": "/usr/bin/mprime"})
        backend = MprimeBackend()
        assert backend.require_binary() == "/usr/bin/mprime"

    def test_default_get_supported_fft_presets(self):
        """Base class returns empty list by default."""

        class DummyBackend(StressBackend):
            name = "dummy"

            def is_available(self):
                return True

            def get_command(self, config, work_dir):
                return []

            def parse_output(self, stdout, stderr, returncode):
                return True, None

            def get_supported_modes(self):
                return []

        backend = DummyBackend()
        assert backend.get_supported_fft_presets() == []
        assert backend.instruction_set(StressConfig()) is None
        assert backend.workload(StressConfig(tests=("custom",))) == ("custom",)
        assert backend.default_memory_mb(4) is None

    def test_default_prepare_and_cleanup(self, tmp_path):
        """Base class prepare/cleanup are no-ops."""

        class DummyBackend(StressBackend):
            name = "dummy"

            def is_available(self):
                return True

            def get_command(self, config, work_dir):
                return []

            def parse_output(self, stdout, stderr, returncode):
                return True, None

            def get_supported_modes(self):
                return []

        backend = DummyBackend()
        cfg = StressConfig()
        # should not raise
        backend.prepare(tmp_path, cfg)
        backend.cleanup(tmp_path)


# ===========================================================================
# mprime backend tests
# ===========================================================================


class TestMprimeBackend:
    def test_name(self):
        assert MprimeBackend.name == "mprime"

    def test_is_available_found(self, on_path):
        on_path({"mprime": "/usr/bin/mprime"})
        backend = MprimeBackend()
        assert backend.is_available() is True
        assert backend._binary == "/usr/bin/mprime"

    def test_is_available_not_found(self, on_path):
        on_path({})
        assert MprimeBackend().is_available() is False

    def test_get_command(self, tmp_path):
        backend = MprimeBackend()
        backend._binary = "/usr/bin/mprime"
        cfg = StressConfig()
        cmd = backend.get_command(cfg, tmp_path)
        assert cmd == ["/usr/bin/mprime", "-t", f"-W{tmp_path}"]

    def test_get_command_no_binary_triggers_search(self, tmp_path, on_path):
        on_path({})
        backend = MprimeBackend()
        with pytest.raises(RuntimeError, match="mprime binary not found"):
            backend.get_command(StressConfig(), tmp_path)

    def test_get_supported_modes(self):
        backend = MprimeBackend()
        modes = backend.get_supported_modes()
        assert StressMode.SSE in modes
        assert StressMode.AVX in modes
        assert StressMode.AVX2 in modes
        assert StressMode.AVX512 in modes

    def test_instruction_set_and_workload_are_independent(self):
        config = StressConfig(mode=StressMode.AVX2, fft_preset=FFTPreset.LARGE, threads=2)
        backend = MprimeBackend()
        assert backend.instruction_set(config) is StressMode.AVX2
        assert backend.workload(config) == ("torture", "large", "2T")

    def test_get_supported_fft_presets(self):
        backend = MprimeBackend()
        presets = backend.get_supported_fft_presets()
        assert FFTPreset.SMALL in presets
        assert FFTPreset.LARGE in presets
        assert FFTPreset.CUSTOM in presets

    # --- prepare tests ---

    @pytest.mark.parametrize(
        "preset,expected_min,expected_max",
        [
            (FFTPreset.SMALLEST, 4, 21),
            (FFTPreset.SMALL, 36, 248),
            (FFTPreset.LARGE, 426, 8192),
            (FFTPreset.HUGE, 8960, 65536),
            (FFTPreset.ALL, 4, 65536),
            (FFTPreset.MODERATE, 1344, 4096),
            (FFTPreset.HEAVY, 4, 1344),
            (FFTPreset.HEAVY_SHORT, 4, 160),
        ],
    )
    def test_prepare_fft_ranges(self, tmp_path, preset, expected_min, expected_max):
        backend = MprimeBackend()
        cfg = StressConfig(fft_preset=preset)
        backend.prepare(tmp_path, cfg)

        content = (tmp_path / "local.txt").read_text()
        assert f"MinTortureFFT={expected_min}" in content
        assert f"MaxTortureFFT={expected_max}" in content

    def test_prepare_custom_fft(self, tmp_path):
        backend = MprimeBackend()
        cfg = StressConfig(fft_preset=FFTPreset.CUSTOM, fft_min=100, fft_max=500)
        backend.prepare(tmp_path, cfg)

        content = (tmp_path / "local.txt").read_text()
        assert "MinTortureFFT=100" in content
        assert "MaxTortureFFT=500" in content

    def test_prepare_custom_fft_no_range_uses_default(self, tmp_path):
        """CUSTOM preset without fft_min/fft_max should fall back to default."""
        backend = MprimeBackend()
        cfg = StressConfig(fft_preset=FFTPreset.CUSTOM, fft_min=None, fft_max=None)
        backend.prepare(tmp_path, cfg)
        content = (tmp_path / "local.txt").read_text()
        # should use fallback (4, 8192)
        assert "MinTortureFFT=4" in content
        assert "MaxTortureFFT=8192" in content

    @pytest.mark.parametrize(
        "mode,expected_flags",
        [
            (
                StressMode.SSE,
                {"CpuSupportsAVX": 0, "CpuSupportsFMA3": 0, "CpuSupportsAVX2": 0, "CpuSupportsAVX512F": 0},
            ),
            (
                StressMode.AVX,
                {"CpuSupportsAVX": 1, "CpuSupportsFMA3": 0, "CpuSupportsAVX2": 0, "CpuSupportsAVX512F": 0},
            ),
            (
                StressMode.AVX2,
                {"CpuSupportsAVX": 1, "CpuSupportsFMA3": 1, "CpuSupportsAVX2": 1, "CpuSupportsAVX512F": 0},
            ),
            (
                StressMode.AVX512,
                {"CpuSupportsAVX": 1, "CpuSupportsFMA3": 1, "CpuSupportsAVX2": 1, "CpuSupportsAVX512F": 1},
            ),
        ],
    )
    def test_prepare_selects_the_instruction_set(self, tmp_path, mode, expected_flags):
        backend = MprimeBackend()
        cfg = StressConfig(mode=mode)
        backend.prepare(tmp_path, cfg)

        for name in ("local.txt", "prime.txt"):
            content = (tmp_path / name).read_text()
            for key, value in expected_flags.items():
                assert f"{key}={value}" in content
            assert "TortureWeak" not in content
        assert "EnableSetAffinity=0" in (tmp_path / "prime.txt").read_text()

    def test_prepare_thread_count(self, tmp_path):
        backend = MprimeBackend()
        cfg = StressConfig(threads=4)
        backend.prepare(tmp_path, cfg)

        content = (tmp_path / "local.txt").read_text()
        assert "TortureThreads=4" in content

    def test_prepare_creates_both_files(self, tmp_path):
        backend = MprimeBackend()
        backend.prepare(tmp_path, StressConfig())
        assert (tmp_path / "local.txt").exists()
        assert (tmp_path / "prime.txt").exists()

    def test_prepare_prime_txt_content(self, tmp_path):
        backend = MprimeBackend()
        cfg = StressConfig(fft_preset=FFTPreset.SMALL, mode=StressMode.AVX2, threads=2)
        backend.prepare(tmp_path, cfg)

        content = (tmp_path / "prime.txt").read_text()
        assert "UsePrimenet=0" in content
        assert "StressTester=1" in content
        assert "MinTortureFFT=36" in content
        assert "TortureThreads=2" in content
        assert "CpuSupportsAVX2=1" in content
        assert "CpuSupportsAVX512F=0" in content
        assert "EnableSetAffinity=0" in content
        # ResultsFile=/LogFile= were never real Prime95 keys, so output stays at
        # the defaults (results.txt / prime.log in the work dir)
        assert "ResultsFile" not in content
        assert "LogFile" not in content

    def test_prepare_creates_work_dir(self, tmp_path):
        backend = MprimeBackend()
        work = tmp_path / "sub" / "dir"
        backend.prepare(work, StressConfig())
        assert work.exists()

    # --- parse_output tests ---

    @pytest.mark.parametrize(
        "output",
        [
            "FATAL ERROR: Rounding was 0.5, expected less than 0.4",
            "FATAL ERROR: Final result was 0000ABCD, expected: 0000EF01.",
            "ERROR: ILLEGAL SUMOUT",
            "Possible hardware failure, consult readme.txt file, restarting test.",
            "Hardware failure detected running 288K FFT size, consult stress.txt file.",
            "Maximum number of warnings exceeded.",
            "TORTURE TEST FAILED on worker #2.",
            "Torture Test completed 20 tests in 2 hours, 15 minutes - 1 errors, 0 warnings.",
            "ERROR: SUM(INPUTS) != SUM(OUTPUTS), 1.5 != 1.6",
            "ERROR: Jacobi error check failed!",
            "Warning: SUMOUT MISMATCH",
        ],
    )
    def test_parse_output_fatal_errors(self, output):
        backend = MprimeBackend()
        passed, msg = backend.parse_output(output, "", 1)
        assert not passed
        assert msg is not None
        assert "mprime error" in msg

    def test_parse_output_error_in_stderr(self):
        backend = MprimeBackend()
        passed, msg = backend.parse_output("", "FATAL ERROR: test", 1)
        assert not passed

    @pytest.mark.parametrize(
        "line",
        [
            "[Worker #1] Self-test 240K passed!",  # K-suffixed FFT (usual)
            "Self-test 42 passed!",  # sub-1K FFT
            "Self-test 4K (thread 2 of 2) passed!",  # hyperthreaded variant
        ],
    )
    def test_parse_output_self_test_passed(self, line):
        backend = MprimeBackend()
        passed, msg = backend.parse_output(line + "\n", "", -15)
        assert passed
        assert msg is None

    def test_parse_output_torture_summary_clean(self):
        backend = MprimeBackend()
        passed, msg = backend.parse_output(
            "Torture Test completed 20 tests in 15 minutes - 0 errors, 0 warnings.", "", -15
        )
        assert passed
        assert msg is None

    def test_parse_output_benign_worker_stop_is_not_an_error(self):
        """ "Worker stopped." is Prime95's graceful-stop line (commonb.c:3143),
        not a fatal error."""
        backend = MprimeBackend()
        passed, msg = backend.parse_output("[Worker #1] Self-test 240K passed!\n[Worker #1] Worker stopped.\n", "", -15)
        assert passed
        assert msg is None

    @pytest.mark.parametrize("code", sorted(KILLED_BY_US_CODES))
    def test_parse_output_killed_signals(self, code):
        backend = MprimeBackend()
        passed, msg = backend.parse_output("", "", code)
        assert passed

    def test_parse_output_unknown_error_code(self):
        backend = MprimeBackend()
        passed, msg = backend.parse_output("", "", 42)
        assert not passed
        assert "exited with code 42" in msg

    def test_parse_output_clean_exit_no_output(self):
        backend = MprimeBackend()
        passed, msg = backend.parse_output("", "", 0)
        assert not passed
        assert msg is not None and "verdict unavailable" in msg

    # --- cleanup tests ---

    def test_cleanup_removes_files(self, tmp_path):
        backend = MprimeBackend()
        for f in ("prime.txt", "local.txt", "prime.log", "results.txt", "prime.spl"):
            (tmp_path / f).write_text("data")
        backend.cleanup(tmp_path)
        for f in ("prime.txt", "local.txt", "prime.log", "results.txt", "prime.spl"):
            assert not (tmp_path / f).exists()

    def test_cleanup_ignores_missing_files(self, tmp_path):
        backend = MprimeBackend()
        # should not raise
        backend.cleanup(tmp_path)

    def test_cleanup_preserves_other_files(self, tmp_path):
        backend = MprimeBackend()
        (tmp_path / "important.dat").write_text("keep me")
        backend.cleanup(tmp_path)
        assert (tmp_path / "important.dat").exists()

    def test_cleanup_on_error_renames_postmortem_files(self, tmp_path):
        """A preserved results.txt must be RENAMED, never left in place: mprime
        appends to results.txt, so a stale FATAL ERROR would be re-parsed by
        every later run in this work dir as its own failure."""
        backend = MprimeBackend()
        (tmp_path / "results.txt").write_text("FATAL ERROR: Rounding was 0.5")
        (tmp_path / "prime.log").write_text("log")
        backend.cleanup(tmp_path, preserve_on_error=True)
        assert not (tmp_path / "results.txt").exists()
        assert not (tmp_path / "prime.log").exists()
        assert "FATAL ERROR" in (tmp_path / "failed-results.txt").read_text()
        assert (tmp_path / "failed-prime.log").read_text() == "log"

    def test_prepare_removes_stale_run_files(self, tmp_path):
        """prepare() must clean leftovers (abort/hard crash skips cleanup) so a
        new run never inherits the previous run's errors or savefile."""
        backend = MprimeBackend()
        for f in ("results.txt", "prime.log", "prime.spl"):
            (tmp_path / f).write_text("stale")
        backend.prepare(tmp_path, StressConfig())
        for f in ("results.txt", "prime.log", "prime.spl"):
            assert not (tmp_path / f).exists()
        # the failed-* post-mortem copies are kept
        (tmp_path / "failed-results.txt").write_text("post-mortem")
        backend.prepare(tmp_path, StressConfig())
        assert (tmp_path / "failed-results.txt").exists()

    # --- live error polling ---

    def test_poll_errors_detects_fatal(self, tmp_path):
        backend = MprimeBackend()
        (tmp_path / "results.txt").write_text(
            "[Worker #1] Self-test 240K passed!\nFATAL ERROR: Rounding was 0.4999, expected less than 0.4\n"
        )
        msg = backend.poll_errors(tmp_path)
        assert msg is not None and "FATAL ERROR" in msg

    def test_poll_errors_clean_run(self, tmp_path):
        backend = MprimeBackend()
        (tmp_path / "results.txt").write_text("[Worker #1] Self-test 240K passed!\n")
        assert backend.poll_errors(tmp_path) is None

    def test_poll_errors_no_file(self, tmp_path):
        assert MprimeBackend().poll_errors(tmp_path) is None

    def test_poll_errors_invalid_utf8_is_verdict_unavailable(self, tmp_path):
        (tmp_path / "results.txt").write_bytes(b"\xff")
        msg = MprimeBackend().poll_errors(tmp_path)
        assert msg is not None and "verdict unavailable" in msg


# ===========================================================================
# stress-ng backend tests
# ===========================================================================


class TestStressNgBackend:
    def test_name(self):
        assert StressNgBackend.name == "stress-ng"

    def test_is_available_found(self, on_path):
        on_path({"stress-ng": "/usr/bin/stress-ng"})
        assert StressNgBackend().is_available() is True

    def test_is_available_not_found(self, on_path):
        on_path({})
        assert StressNgBackend().is_available() is False

    def test_get_command(self, tmp_path):
        backend = StressNgBackend()
        backend._binary = "/usr/bin/stress-ng"
        cfg = StressConfig(mode=StressMode.SSE, threads=2)
        cmd = backend.get_command(cfg, tmp_path)
        assert cmd[0] == "/usr/bin/stress-ng"
        assert "--cpu" in cmd
        assert "2" in cmd
        assert "--cpu-method" in cmd
        assert "matrixprod" in cmd
        assert "--verify" in cmd
        assert "--metrics-brief" in cmd
        assert "--temp-path" in cmd
        assert str(tmp_path) in cmd

    def test_get_command_avx_method(self, tmp_path):
        backend = StressNgBackend()
        backend._binary = "/usr/bin/stress-ng"
        cfg = StressConfig(mode=StressMode.AVX)
        cmd = backend.get_command(cfg, tmp_path)
        idx = cmd.index("--cpu-method")
        assert cmd[idx + 1] == "fft"

    def test_get_command_no_binary_raises(self, tmp_path, on_path):
        on_path({})
        backend = StressNgBackend()
        with pytest.raises(RuntimeError, match="stress-ng binary not found"):
            backend.get_command(StressConfig(), tmp_path)

    def test_get_supported_modes(self):
        backend = StressNgBackend()
        modes = backend.get_supported_modes()
        assert StressMode.SSE in modes
        assert StressMode.AVX in modes
        assert StressMode.AVX2 in modes
        assert StressMode.AVX512 not in modes

    def test_workload_does_not_claim_an_instruction_set(self):
        config = StressConfig(mode=StressMode.AVX2)
        backend = StressNgBackend()
        assert backend.instruction_set(config) is None
        assert backend.workload(config) == ("fft",)

    def test_prepare(self, tmp_path):
        backend = StressNgBackend()
        work = tmp_path / "work"
        backend.prepare(work, StressConfig())
        assert work.exists()

    # --- parse_output ---

    @pytest.mark.parametrize(
        "output",
        [
            "3 FAILED during stress test",
            "verification error on cpu 0",
            "computation mismatch detected",
            "error: incorrect result",
        ],
    )
    def test_parse_output_errors(self, output):
        backend = StressNgBackend()
        passed, msg = backend.parse_output(output, "", 1)
        assert not passed
        assert "stress-ng error" in msg

    def test_parse_output_error_in_stderr(self):
        backend = StressNgBackend()
        passed, msg = backend.parse_output("", "FAILED test", 1)
        assert not passed

    @pytest.mark.parametrize("code", sorted(KILLED_BY_US_CODES))
    def test_parse_output_success_codes(self, code):
        backend = StressNgBackend()
        passed, msg = backend.parse_output("completed", "", code)
        assert passed
        assert msg is None

    def test_parse_output_clean_exit_has_no_verdict(self):
        passed, msg = StressNgBackend().parse_output("completed", "", 0)
        assert not passed
        assert msg is not None and "verdict unavailable" in msg

    def test_parse_output_unknown_exit_code(self):
        backend = StressNgBackend()
        passed, msg = backend.parse_output("", "", 99)
        assert not passed
        assert "exited with code 99" in msg

    def test_cleanup_noop(self, tmp_path):
        backend = StressNgBackend()
        (tmp_path / "test.dat").write_text("data")
        backend.cleanup(tmp_path)
        assert (tmp_path / "test.dat").exists()


class TestModeToMethod:
    @pytest.mark.parametrize(
        "mode,expected",
        [
            (StressMode.SSE, "matrixprod"),
            (StressMode.AVX, "fft"),
            (StressMode.AVX2, "fft"),
            (StressMode.AVX512, "matrixprod"),
            (StressMode.CUSTOM, "matrixprod"),
        ],
    )
    def test_mode_mapping(self, mode, expected):
        assert _mode_to_method(mode) == expected


# ===========================================================================
# y-cruncher backend tests
# ===========================================================================


_CAPTURED_PASS_OUTPUT = """\
Auto-Selecting: 11-SNB ~ Hina

Component Stress Tester

  1   Logical Cores:      4
  2   Memory:              200 MiB  ( 50.0 MiB per thread )
  6   Stop on Error:      Enabled

  #  Tag   Test Name                   Mem/Thread  Component
 11  BKT   Basecase + Karatsuba          27.8 KiB  Scalar Integer
 16  FFTv4 Fast Fourier Transform (v4)   246 MiB   AVX Float

  0   Start Stress-Testing!

Allocating Memory...
  Core   0:  27.8 KiB

Iteration: 0  Total Elapsed Time: 0.001 seconds  ( 0.000 minutes )
Running BKT: Passed  Test Speed:  5.25 * 10^08  bits / sec

Iteration: 1  Total Elapsed Time: 3.054 seconds  ( 0.051 minutes )
Running BKT: Passed  Test Speed:  5.2 * 10^08  bits / sec
"""

_CAPTURED_INVALID_PARAM_OUTPUT = """\
Reading Hardware Topology...

Logical Cores:
    0 1 2 3

Invalid Parameter: SSE
Press ENTER to continue . . .
"""


class TestYCruncherBackend:
    def test_name(self):
        assert YCruncherBackend.name == "y-cruncher"

    def test_is_available_first_name(self, on_path):
        on_path({"y-cruncher": "/bin/y-cruncher"})
        backend = YCruncherBackend()
        assert backend.is_available() is True
        assert backend._binary == "/bin/y-cruncher"

    def test_is_available_second_name(self, on_path):
        on_path({"y_cruncher": "/bin/y_cruncher"})
        backend = YCruncherBackend()
        assert backend.is_available() is True
        assert backend._binary == "/bin/y_cruncher"

    def test_is_available_not_found(self, on_path):
        on_path({})
        assert YCruncherBackend().is_available() is False

    def _render(self, work_dir: Path, **overrides) -> str:
        config = StressConfig(**{"cpus": (15, 31), "threads": 2, **overrides})
        YCruncherBackend().prepare(work_dir, config)
        return (work_dir / "stress.cfg").read_text()

    @staticmethod
    def _field(text: str, key: str) -> str:
        match = re.search(rf"^\s*{key} : (.+)$", text, re.MULTILINE)
        assert match, f"{key} missing from:\n{text}"
        return match.group(1)

    @staticmethod
    def _tests(text: str) -> list[str]:
        return re.findall(r'"(\w+)"', text[text.index("Tests :") :])

    def test_command_runs_the_prepared_config_headless(self, tmp_path):
        backend = YCruncherBackend()
        backend._binary = "/bin/y-cruncher"
        cmd = backend.get_command(StressConfig(cpus=(0,)), tmp_path)
        assert cmd == [
            "/bin/y-cruncher",
            "skip-warnings",
            "pause:-2",
            "status:none",
            "config",
            str(tmp_path / "stress.cfg"),
        ]

    def test_config_runs_one_thread_on_each_lane_cpu(self, tmp_path):
        """The stress command sizes its pool from the machine topology and
        ignores the cpuset, so only LogicalCores keeps it on the lane."""
        assert self._field(self._render(tmp_path), "LogicalCores") == "[15 31]"

    def test_a_config_without_lane_cpus_is_refused(self, tmp_path):
        with pytest.raises(RuntimeError, match="lane CPUs"):
            YCruncherBackend().prepare(tmp_path, StressConfig())
        assert not (tmp_path / "stress.cfg").exists()

    @pytest.mark.parametrize(
        ("mode", "expected"),
        [(StressMode.SSE, ["BKT"]), (StressMode.AVX2, ["BKT", "FFTv4", "N63", "VT3"])],
    )
    def test_mode_selects_default_component_tests(self, tmp_path, mode, expected):
        assert self._tests(self._render(tmp_path, mode=mode)) == expected

    def test_explicit_component_tests_win(self, tmp_path):
        assert self._tests(self._render(tmp_path, mode=StressMode.AVX2, tests=("BKT", "VT3"))) == ["BKT", "VT3"]

    def test_unknown_component_tests_are_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError) as exc_info:
            YCruncherBackend().prepare(tmp_path, StressConfig(cpus=(0,), tests=("UNKNOWN", "BKT", "BAD")))
        assert str(exc_info.value) == "Unknown y-cruncher component test(s): BAD, UNKNOWN"

    def test_a_mode_with_no_component_tests_is_refused(self, tmp_path):
        with pytest.raises(RuntimeError, match="no y-cruncher component tests"):
            YCruncherBackend().prepare(tmp_path, StressConfig(cpus=(0,), mode=StressMode.CUSTOM))

    @pytest.mark.parametrize(("test_seconds", "expected"), [(None, "30"), (45, "45"), (0, "1")])
    def test_each_test_runs_for_the_clamped_slot_share(self, tmp_path, test_seconds, expected):
        text = self._render(tmp_path, test_seconds=test_seconds)
        assert self._field(text, "SecondsPerTest") == expected
        assert self._field(text, "SecondsTotal") == "0"
        assert self._field(text, "StopOnError") == '"true"'

    def test_memory_stays_cache_sized_per_thread(self, tmp_path):
        assert self._field(self._render(tmp_path), "TotalMemory") == str(2 * 32 * 1024 * 1024)

    def test_a_memory_coupled_workload_spills_past_the_cache(self, tmp_path):
        text = self._render(tmp_path, memory_coupled=True)
        assert self._field(text, "TotalMemory") == str(2 * 256 * 1024 * 1024)

    def test_an_explicit_memory_budget_wins(self, tmp_path):
        text = self._render(tmp_path, memory_mb=2048, memory_coupled=True)
        assert self._field(text, "TotalMemory") == str(2048 * 1024 * 1024)

    def test_a_launch_without_its_config_is_refused(self, tmp_path):
        with pytest.raises(OSError, match="stress.cfg"):
            YCruncherBackend().assert_prepared(tmp_path)

    def test_no_binary_raises(self, tmp_path, on_path):
        on_path({})
        backend = YCruncherBackend()
        with pytest.raises(RuntimeError, match="y-cruncher binary not found"):
            backend.get_command(StressConfig(cpus=(0,)), tmp_path)

    def test_get_supported_modes(self):
        backend = YCruncherBackend()
        modes = backend.get_supported_modes()
        assert StressMode.SSE in modes
        assert StressMode.AVX in modes
        assert StressMode.AVX2 in modes
        assert StressMode.AVX512 in modes

    def test_component_tests_are_the_workload_not_an_instruction_set(self):
        config = StressConfig(mode=StressMode.AVX512, tests=("N63",))
        backend = YCruncherBackend()
        assert backend.instruction_set(config) is None
        assert backend.workload(config) == ("N63",)

    def test_parse_real_pass_output_not_false_flagged(self):
        backend = YCruncherBackend()
        passed, msg = backend.parse_output(_CAPTURED_PASS_OUTPUT, "", -15)
        assert passed, msg
        assert msg is None

    @pytest.mark.parametrize("code", sorted(KILLED_BY_US_CODES))
    def test_parse_killed_is_pass(self, code):
        backend = YCruncherBackend()
        passed, _msg = backend.parse_output(_CAPTURED_PASS_OUTPUT, "", code)
        assert passed

    def test_parse_self_exit_zero_is_anomaly_not_silent_pass(self):
        backend = YCruncherBackend()
        passed, msg = backend.parse_output(_CAPTURED_PASS_OUTPUT, "", 0)
        assert not passed
        assert "verdict unavailable" in msg

    def test_parse_unpinnable_thread_is_a_harness_fault_not_instability(self):
        """A thread y-cruncher cannot pin prints 'Failed ...' and still passes
        every test; that is a lane/config mismatch, never the core's verdict."""
        out = _CAPTURED_PASS_OUTPUT.replace(
            "Allocating Memory...", "Failed to set core affinity to core: 14\nAllocating Memory..."
        )
        passed, msg = YCruncherBackend().parse_output(out, "", -15)
        assert not passed
        assert classify_error(msg) == "startup"

    @pytest.mark.parametrize(("stdout", "stderr"), [("Checksum mismatch", ""), ("", "Checksum mismatch")])
    def test_parse_checksum_mismatch_after_scheduler_kill(self, stdout, stderr):
        passed, msg = YCruncherBackend().parse_output(stdout, stderr, -15)
        assert not passed
        assert msg is not None and "Checksum mismatch" in msg

    def test_parse_error_encountered(self):
        backend = YCruncherBackend()
        passed, msg = backend.parse_output("Iteration: 5\nError(s) encountered on logical core 3.\n", "", 0)
        assert not passed
        assert "y-cruncher error" in msg

    def test_parse_coefficient_too_large(self):
        backend = YCruncherBackend()
        passed, msg = backend.parse_output("Coefficient is too large\n", "", 0)
        assert not passed
        assert "Coefficient is too large" in msg

    def test_parse_invalid_parameter(self):
        backend = YCruncherBackend()
        passed, msg = backend.parse_output(_CAPTURED_INVALID_PARAM_OUTPUT, "", 0)
        assert not passed
        assert "Invalid Parameter" in msg

    def test_parse_ansi_codes_stripped(self):
        backend = YCruncherBackend()
        passed, _msg = backend.parse_output("\x1b[01;31mCoefficient is too large\x1b[0m\n", "", 0)
        assert not passed

    @pytest.mark.parametrize("code", [-11, -6, -4])
    def test_parse_crash_signal_fails(self, code):
        backend = YCruncherBackend()
        passed, msg = backend.parse_output("", "", code)
        assert not passed
        assert "crashed" in msg

    def test_parse_unknown_exit_code_is_apparatus_fault(self):
        backend = YCruncherBackend()
        passed, msg = backend.parse_output("", "", 7)
        assert not passed
        assert "exited with code 7" in msg
        assert "verdict unavailable" in msg

    def test_prepare_creates_the_work_dir(self, tmp_path):
        work = tmp_path / "ycruncher_work"
        YCruncherBackend().prepare(work, StressConfig(cpus=(0,)))
        assert (work / "stress.cfg").is_file()

    def test_cleanup_noop(self, tmp_path):
        backend = YCruncherBackend()
        backend.cleanup(tmp_path)


class TestYCruncherModeMapping:
    @pytest.mark.parametrize("mode", list(StressMode))
    def test_every_mode_maps(self, mode):
        assert mode in MODE_TO_ALGORITHMS

    def test_sse_is_scalar_only(self):
        assert MODE_TO_ALGORITHMS[StressMode.SSE] == ("BKT",)

    def test_avx2_uses_curve_optimizer_algorithms_by_default(self):
        assert MODE_TO_ALGORITHMS[StressMode.AVX2] == ("BKT", "FFTv4", "N63", "VT3")

    def test_avx512_uses_curve_optimizer_algorithms_by_default(self):
        assert MODE_TO_ALGORITHMS[StressMode.AVX512] == ("BKT", "FFTv4", "N63", "VT3")

    def test_algorithms_are_valid_ycruncher_names(self):
        for algos in MODE_TO_ALGORITHMS.values():
            assert set(algos) <= VALID_COMPONENT_TESTS


# ===========================================================================
# FFT_RANGES and MODE_TO_CPU_FLAGS constants tests
# ===========================================================================


class TestMprimeConstants:
    def test_fft_ranges_completeness(self):
        """All non-CUSTOM presets should be in FFT_RANGES."""
        for preset in FFTPreset:
            if preset != FFTPreset.CUSTOM:
                assert preset in FFT_RANGES

    def test_fft_ranges_valid(self):
        for preset, (lo, hi) in FFT_RANGES.items():
            assert lo < hi, f"{preset}: {lo} >= {hi}"
            assert lo > 0

    def test_every_mode_has_a_cpu_flag_set(self):
        for mode in [StressMode.SSE, StressMode.AVX, StressMode.AVX2, StressMode.AVX512]:
            assert mode in MODE_TO_CPU_FLAGS

    def test_each_mode_disables_everything_above_it(self):
        order = [StressMode.SSE, StressMode.AVX, StressMode.AVX2, StressMode.AVX512]
        gates = ["CpuSupportsAVX", "CpuSupportsAVX2", "CpuSupportsAVX512F"]
        enabled = [[gate for gate in gates if MODE_TO_CPU_FLAGS[mode][gate]] for mode in order]
        assert enabled == [[], gates[:1], gates[:2], gates]
        for mode in order:
            assert MODE_TO_CPU_FLAGS[mode]["CpuSupportsFMA4"] == 0

    def test_version_parse_reads_the_live_format(self):
        line = "Mersenne Prime Test Program: Linux64,Untrusted Prime95,v31.4,build 2"
        assert MprimeBackend.parse_version(line) == "31.4-build-2"
        assert MprimeBackend.parse_version("Prime95,v31.4,build 99") == "31.4-build-99"
        assert MprimeBackend.parse_version("no version here") is None

    def test_installed_version_reads_the_binary(self, monkeypatch, tmp_path):
        import subprocess
        from types import SimpleNamespace

        backend = MprimeBackend()
        backend._binary = "/usr/bin/mprime"
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: SimpleNamespace(stdout="Prime95,v31.4,build 2", stderr=""),
        )
        assert backend.installed_version() == "31.4-build-2"

    def test_installed_version_survives_a_broken_binary(self, monkeypatch):
        import subprocess

        backend = MprimeBackend()
        backend._binary = "/usr/bin/mprime"

        def boom(*a, **k):
            raise OSError("exec failed")

        monkeypatch.setattr(subprocess, "run", boom)
        assert backend.installed_version() is None


class TestFailClosedResultsRead:
    def test_unreadable_results_txt_is_not_a_pass(self, tmp_path):
        """Without results.txt a real error could pass unseen. An unreadable
        file must produce an apparatus-fault verdict (engine pauses on it),
        never a silent pass."""
        import os

        backend = MprimeBackend()

        results = tmp_path / "results.txt"
        results.write_text("FATAL ERROR: Rounding was 0.5, expected less than 0.4")
        os.chmod(results, 0o000)
        try:
            msg = backend.poll_errors(tmp_path)
            assert "verdict unavailable" in msg
        finally:
            os.chmod(results, 0o644)  # let tmp_path cleanup succeed
