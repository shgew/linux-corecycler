"""Per-logical-CPU usage % from /proc/stat."""

from __future__ import annotations

from pathlib import Path

from corecycler.monitor.files import read_text_optional

PROC_STAT = Path("/proc/stat")


def read_cpu_times(path: Path = PROC_STAT) -> dict[int, tuple[int, int]]:
    """Return each logical CPU's cumulative (idle, total) scheduler ticks."""
    text = read_text_optional(path)
    if text is None:
        return {}

    times: dict[int, tuple[int, int]] = {}
    for line in text.splitlines():
        if not line.startswith("cpu") or line.startswith("cpu "):
            continue
        parts = line.split()
        try:
            cpu_id = int(parts[0][3:])
            values = [int(value) for value in parts[1:9]]
            idle = values[3] + values[4]
            total = sum(values)
        except (ValueError, IndexError):
            continue
        times[cpu_id] = (idle, total)
    return times


class CPUUsageReader:
    """Reads /proc/stat to compute per-CPU usage % between successive calls."""

    def __init__(self) -> None:
        self._prev: dict[int, tuple[int, int]] = {}

    def read(self) -> dict[int, float]:
        """Return per-logical-CPU usage % (0-100). Empty on first call."""
        results: dict[int, float] = {}
        for cpu_id, (idle, total) in read_cpu_times().items():
            previous = self._prev.get(cpu_id)
            if previous is not None:
                idle_delta = idle - previous[0]
                total_delta = total - previous[1]
                if total_delta > 0:
                    results[cpu_id] = ((total_delta - idle_delta) / total_delta) * 100.0
            self._prev[cpu_id] = (idle, total)
        return results
