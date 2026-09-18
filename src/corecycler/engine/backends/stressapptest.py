"""stressapptest stress backend — Google's memory stress testing tool.

Note: Like all backends, stressapptest runs indefinitely and the
CoreScheduler handles timing by killing the process after
seconds_per_core. We pass -s 86400 (24h) so stressapptest doesn't
self-terminate before the scheduler stops it.

Memory is always sized explicitly. Without -M stressapptest targets 95% of
PHYSICAL RAM minus 192 MB per process, which is fine for the single-instance
DIMM test it was written for but fatal for the tuner's memory stage, where one
process per core launched together tried to claim all of RAM eight times over
and the OOM killer tore the batch down before any core got a verdict.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from corecycler.engine.backends import register_backend

from .base import CRASH_SIGNALS, KILLED_BY_US_CODES, StressBackend, StressConfig, StressMode

if TYPE_CHECKING:
    from pathlib import Path

MEMORY_SHARE = 0.75
MIN_MEMORY_MB = 256
FALLBACK_MEMORY_MB = 1024


def available_memory_mb() -> int | None:
    """MemAvailable from /proc/meminfo in MB, None when it cannot be read."""
    with contextlib.suppress(OSError, ValueError), open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    return None


def default_memory_mb(lanes: int = 1) -> int:
    """Per-process -M size: 75% of MemAvailable shared equally across ``lanes``
    concurrent processes (never below MIN_MEMORY_MB), so a batch of them
    together stays clear of the OOM killer. Falls back to a fixed total when
    /proc/meminfo is unreadable."""
    available = available_memory_mb()
    total = int(available * MEMORY_SHARE) if available else FALLBACK_MEMORY_MB
    return max(MIN_MEMORY_MB, total // max(1, lanes))


@register_backend("stressapptest")
class StressapptestBackend(StressBackend):
    name = "stressapptest"

    def get_command(self, config: StressConfig, work_dir: Path) -> list[str]:
        # stressapptest sizes its worker pool from its affinity mask, which the
        # engine's cgroup cpuset already clamps to the lane's CPUs.
        memory_mb = config.memory_mb if config.memory_mb and config.memory_mb > 0 else default_memory_mb()
        return [
            self.require_binary(),
            "-W",
            "-M",
            str(memory_mb),
            "-s",
            "86400",
        ]

    def parse_output(self, stdout: str, stderr: str, returncode: int) -> tuple[bool, str | None]:
        # The scheduler kills stressapptest (a 24h run) before its final
        # "Status: PASS/FAIL" summary line, so detect the memory-error signatures it
        # logs DURING the run. Checking only the final summary meant a killed run
        # that had already found memory errors was reported as passed (false stable).
        lowered = (stdout + "\n" + stderr).lower()
        for signature in ("miscompare", "hardware error", "hardware incident", "status: fail"):
            if signature in lowered:
                return False, f"stressapptest: '{signature}' — memory errors detected"
        # A crash signal always wins, even over a final "Status: PASS": a clean run
        # exits 0 or is killed by the scheduler, never with a crash code, so a crash
        # exit is unambiguous instability and must never be masked by a printed PASS.
        if returncode in CRASH_SIGNALS:
            return False, f"stressapptest crashed with {CRASH_SIGNALS[returncode]} (exit {returncode})"
        if "Status: PASS" in stdout:
            return True, None
        if returncode in KILLED_BY_US_CODES:
            return True, None
        if returncode != 0:
            return False, f"stressapptest exited with code {returncode}"
        return True, None

    def get_supported_modes(self) -> list[StressMode]:
        return [StressMode.SSE]

    def prepare(self, work_dir: Path, config: StressConfig) -> None:
        work_dir.mkdir(parents=True, exist_ok=True)

    def cleanup(self, work_dir: Path, *, preserve_on_error: bool = False) -> None:
        pass
