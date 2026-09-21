"""Per-core CPU frequency monitoring."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from corecycler.monitor.files import read_int_optional, read_text_optional

log = logging.getLogger(__name__)

CPUFREQ_BASE = Path("/sys/devices/system/cpu")
PROC_CPUINFO = Path("/proc/cpuinfo")


def _cpu_directories() -> list[Path] | None:
    try:
        return sorted(CPUFREQ_BASE.iterdir())
    except OSError:
        log.debug("Unable to enumerate CPU frequency sysfs", exc_info=True)
        return None


def read_core_frequencies() -> dict[int, float]:
    """Read current frequency (MHz) for each logical CPU."""
    if not CPUFREQ_BASE.exists():
        return _read_from_proc()
    cpu_directories = _cpu_directories()
    if cpu_directories is None:
        return _read_from_proc()

    frequencies: dict[int, float] = {}
    for cpu_dir in cpu_directories:
        if not cpu_dir.name.startswith("cpu") or not cpu_dir.name[3:].isdigit():
            continue
        cpu_id = int(cpu_dir.name[3:])
        cpufreq = cpu_dir / "cpufreq"
        for name in ("cpuinfo_cur_freq", "scaling_cur_freq"):
            khz = read_int_optional(cpufreq / name)
            if khz is not None:
                frequencies[cpu_id] = khz / 1000.0
                break
    return frequencies if frequencies else _read_from_proc()


def _read_from_proc(path: Path | None = None) -> dict[int, float]:
    """Read fallback frequencies from procfs."""
    text = read_text_optional(PROC_CPUINFO if path is None else path)
    if text is None:
        return {}

    frequencies: dict[int, float] = {}
    current_cpu = -1
    for line in text.splitlines():
        if line.startswith("processor"):
            try:
                current_cpu = int(line.split(":", 1)[1].strip())
            except (ValueError, IndexError):
                current_cpu = -1
        elif line.startswith("cpu MHz") and current_cpu >= 0:
            try:
                frequencies[current_cpu] = float(line.split(":", 1)[1].strip())
            except (ValueError, IndexError):
                continue
    return frequencies


@dataclass(slots=True)
class CoreFreqReading:
    """A logical CPU's actual frequency and configured boost ceiling."""

    actual_mhz: float
    effective_max_mhz: float


def read_core_frequencies_dual() -> dict[int, CoreFreqReading]:
    """Read actual frequency and effective maximum for each logical CPU."""
    if not CPUFREQ_BASE.exists():
        return {}
    cpu_directories = _cpu_directories()
    if cpu_directories is None:
        return {}

    result: dict[int, CoreFreqReading] = {}
    for cpu_dir in cpu_directories:
        if not cpu_dir.name.startswith("cpu") or not cpu_dir.name[3:].isdigit():
            continue
        cpu_id = int(cpu_dir.name[3:])
        cpufreq = cpu_dir / "cpufreq"
        actual_khz = None
        for name in ("cpuinfo_cur_freq", "cpuinfo_avg_freq", "scaling_cur_freq"):
            actual_khz = read_int_optional(cpufreq / name)
            if actual_khz is not None:
                break
        maximum_khz = read_int_optional(cpufreq / "scaling_max_freq")
        if actual_khz is not None and maximum_khz is not None:
            result[cpu_id] = CoreFreqReading(
                actual_mhz=actual_khz / 1000.0,
                effective_max_mhz=maximum_khz / 1000.0,
            )
    return result


def read_max_frequency(cpu_id: int = 0) -> float | None:
    """Read the maximum boost frequency for a CPU (MHz)."""
    khz = read_int_optional(CPUFREQ_BASE / f"cpu{cpu_id}" / "cpufreq" / "cpuinfo_max_freq")
    return None if khz is None else khz / 1000.0


def read_min_frequency(cpu_id: int = 0) -> float | None:
    """Read the minimum frequency for a CPU (MHz)."""
    khz = read_int_optional(CPUFREQ_BASE / f"cpu{cpu_id}" / "cpufreq" / "cpuinfo_min_freq")
    return None if khz is None else khz / 1000.0
