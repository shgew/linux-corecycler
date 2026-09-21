"""stressapptest stress backend, Google's memory stress testing tool.

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

from .base import StressBackend, StressConfig, StressMode

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
    """Return a safe per-lane share of the batch memory budget."""
    lanes = max(1, lanes)
    available = available_memory_mb()
    total = int(available * MEMORY_SHARE) if available else FALLBACK_MEMORY_MB
    if total < MIN_MEMORY_MB * lanes:
        raise RuntimeError(
            f"cannot fit {lanes} concurrent stressapptest lanes: "
            f"{total} MiB budget is below the {MIN_MEMORY_MB} MiB per-lane minimum"
        )
    return total // lanes


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
        lowered = (stdout + "\n" + stderr).lower()
        for signature in ("miscompare", "hardware error", "hardware incident", "status: fail"):
            if signature in lowered:
                return False, f"stressapptest: '{signature}' - memory errors detected"
        return self.indefinite_exit_verdict(returncode)

    def get_supported_modes(self) -> list[StressMode]:
        return [StressMode.SSE]

    def workload(self, config: StressConfig) -> tuple[str, ...]:
        return ("memory",)

    def default_memory_mb(self, lanes: int = 1) -> int:
        return default_memory_mb(lanes)

    def prepare(self, work_dir: Path, config: StressConfig) -> None:
        work_dir.mkdir(parents=True, exist_ok=True)
