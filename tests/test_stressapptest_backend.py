"""Tests for the stressapptest stress backend."""

from __future__ import annotations

from unittest.mock import mock_open, patch

import pytest

import corecycler.engine.backends.stressapptest as sat
from corecycler.engine.backends.base import StressConfig, StressMode
from corecycler.engine.backends.stressapptest import StressapptestBackend, available_memory_mb, default_memory_mb


def _flag(cmd: list[str], flag: str) -> str:
    return cmd[cmd.index(flag) + 1]


class TestStressapptestBackend:
    def test_command_generation(self, tmp_path, on_path, monkeypatch):
        on_path({"stressapptest": "/usr/bin/stressapptest"})
        monkeypatch.setattr(sat, "available_memory_mb", lambda: 8192)
        backend = StressapptestBackend()
        config = StressConfig(mode=StressMode.SSE)
        cmd = backend.get_command(config, tmp_path)
        assert cmd[0] == "/usr/bin/stressapptest"
        assert "-W" in cmd
        assert _flag(cmd, "-s") == "86400"
        assert _flag(cmd, "-M") == str(int(8192 * 0.75))

    def test_explicit_memory_mb_wins_over_the_default(self, tmp_path, on_path, monkeypatch):
        """A caller running several lanes at once hands each process its share;
        the backend must pass that through untouched, never re-derive a
        whole-machine size that the lanes together would exceed."""
        on_path({"stressapptest": "/usr/bin/stressapptest"})
        monkeypatch.setattr(sat, "available_memory_mb", lambda: 8192)
        cmd = StressapptestBackend().get_command(StressConfig(memory_mb=512), tmp_path)
        assert _flag(cmd, "-M") == "512"

    @pytest.mark.parametrize("memory_mb", [0, -1])
    def test_non_positive_memory_mb_falls_back_to_the_default(self, tmp_path, on_path, monkeypatch, memory_mb):
        on_path({"stressapptest": "/usr/bin/stressapptest"})
        monkeypatch.setattr(sat, "available_memory_mb", lambda: 8192)
        cmd = StressapptestBackend().get_command(StressConfig(memory_mb=memory_mb), tmp_path)
        assert _flag(cmd, "-M") == str(int(8192 * 0.75))

    def test_unrequested_clean_exit_has_no_verdict(self):
        backend = StressapptestBackend()
        stdout = "Status: PASS - please pass all stress tests."
        passed, err = backend.parse_output(stdout, "", 0)
        assert passed is False
        assert err is not None and "verdict unavailable" in err

    def test_parse_fail(self):
        backend = StressapptestBackend()
        stdout = "Status: FAIL - memory errors detected."
        passed, err = backend.parse_output(stdout, "", 1)
        assert passed is False
        assert "fail" in err.lower()

    def test_parse_killed_by_scheduler(self):
        backend = StressapptestBackend()
        passed, err = backend.parse_output("", "", -15)
        assert passed is True

    def test_supported_mode_is_a_workload_label_not_an_enforced_isa(self):
        backend = StressapptestBackend()
        assert StressMode.SSE in backend.get_supported_modes()
        assert backend.instruction_set(StressConfig(mode=StressMode.SSE)) is None
        assert backend.workload(StressConfig()) == ("memory",)


class TestAvailableMemoryMb:
    def test_reads_mem_available(self):
        data = "MemTotal:       16000000 kB\nMemAvailable:    2097152 kB\n"
        with patch("builtins.open", mock_open(read_data=data)):
            assert available_memory_mb() == 2048

    def test_absent_field_returns_none(self):
        with patch("builtins.open", mock_open(read_data="MemTotal: 1 kB\n")):
            assert available_memory_mb() is None

    def test_unreadable_meminfo_returns_none(self):
        with patch("builtins.open", side_effect=OSError):
            assert available_memory_mb() is None


class TestDefaultMemoryMb:
    """The whole batch of concurrent processes must fit in 75% of what is
    available: sizing a lane is sizing the machine divided by the lanes."""

    def test_single_lane_takes_the_share(self, monkeypatch):
        monkeypatch.setattr(sat, "available_memory_mb", lambda: 29000)
        assert default_memory_mb() == int(29000 * 0.75)

    def test_lanes_split_the_share_so_the_batch_fits(self, monkeypatch):
        monkeypatch.setattr(sat, "available_memory_mb", lambda: 29000)
        per_lane = default_memory_mb(8)
        assert per_lane == int(29000 * 0.75) // 8
        assert per_lane * 8 <= 29000 * 0.75

    def test_batch_refuses_when_the_per_lane_minimum_would_overcommit(self, monkeypatch):
        monkeypatch.setattr(sat, "available_memory_mb", lambda: 1000)
        with pytest.raises(RuntimeError, match="64 concurrent stressapptest lanes"):
            StressapptestBackend().default_memory_mb(64)

    def test_unknown_memory_falls_back_to_a_fixed_total(self, monkeypatch):
        monkeypatch.setattr(sat, "available_memory_mb", lambda: None)
        assert default_memory_mb() == sat.FALLBACK_MEMORY_MB
        assert default_memory_mb(4) == sat.FALLBACK_MEMORY_MB // 4

    def test_backend_exposes_batch_aware_default(self, monkeypatch):
        monkeypatch.setattr(sat, "available_memory_mb", lambda: 8192)
        assert StressapptestBackend().default_memory_mb(4) == int(8192 * sat.MEMORY_SHARE) // 4
