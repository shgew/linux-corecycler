"""Package power monitoring via RAPL sysfs with hwmon fallback."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from corecycler.monitor.files import read_int_optional, read_text_optional

log = logging.getLogger(__name__)

RAPL_BASE = Path("/sys/class/powercap/intel-rapl")
HWMON_BASE = Path("/sys/class/hwmon")

_POWER_HWMON_DRIVERS = ("zenpower", "zenpower3", "zenpower5", "k10temp")


class PowerMonitor:
    """Read package power from RAPL or hwmon."""

    def __init__(self) -> None:
        self._package_path: Path | None = None
        self._max_energy_range_uj: int | None = None
        self._hwmon_power_path: Path | None = None
        self._last_energy_uj: int | None = None
        self._last_time: float | None = None
        self._find_package()

    @staticmethod
    def _directories(path: Path) -> list[Path]:
        try:
            return sorted(path.iterdir())
        except OSError:
            log.debug("Unable to enumerate %s", path, exc_info=True)
            return []

    @staticmethod
    def _glob(path: Path, pattern: str) -> list[Path]:
        try:
            return sorted(path.glob(pattern))
        except OSError:
            log.debug("Unable to enumerate %s", path, exc_info=True)
            return []

    def _select_rapl(self, energy_path: Path) -> None:
        self._package_path = energy_path
        maximum = read_int_optional(energy_path.with_name("max_energy_range_uj"))
        self._max_energy_range_uj = maximum if maximum is not None and maximum > 0 else None

    def _find_package(self) -> None:
        package_zero = RAPL_BASE / "intel-rapl:0" / "energy_uj"
        if self._try_read(package_zero):
            self._select_rapl(package_zero)
            return

        if RAPL_BASE.exists():
            for rapl_dir in self._glob(RAPL_BASE.parent, "intel-rapl*"):
                energy = rapl_dir / "energy_uj"
                name = read_text_optional(rapl_dir / "name") or ""
                if "package" in name.lower() and self._try_read(energy):
                    self._select_rapl(energy)
                    return

        if HWMON_BASE.exists():
            for hwmon_dir in self._directories(HWMON_BASE):
                name = read_text_optional(hwmon_dir / "name")
                if name not in _POWER_HWMON_DRIVERS:
                    continue
                for power_file in self._glob(hwmon_dir, "power*_input"):
                    label = read_text_optional(power_file.with_name(power_file.name.replace("_input", "_label"))) or ""
                    if ("rapl" in label.lower() or "package" in label.lower() or not label) and self._try_read(
                        power_file
                    ):
                        self._hwmon_power_path = power_file
                        log.info("Using hwmon %s for package power", name)
                        return

        if package_zero.exists():
            log.info("RAPL energy_uj exists but is not readable")
        else:
            log.debug("RAPL sysfs not found")

    @staticmethod
    def _try_read(path: Path) -> bool:
        return read_int_optional(path) is not None

    def is_available(self) -> bool:
        return self._package_path is not None or self._hwmon_power_path is not None

    def read_power_watts(self) -> float | None:
        if self._package_path is not None:
            return self._read_rapl()
        if self._hwmon_power_path is not None:
            return self._read_hwmon_power()
        return None

    def _read_rapl(self) -> float | None:
        if self._package_path is None:
            return None
        energy_uj = read_int_optional(self._package_path)
        if energy_uj is None:
            return None

        now = time.monotonic()
        if self._max_energy_range_uj is None:
            self._last_energy_uj = energy_uj
            self._last_time = now
            return None

        if self._last_energy_uj is not None and self._last_time is not None:
            elapsed = now - self._last_time
            if elapsed > 0:
                delta_uj = (energy_uj - self._last_energy_uj) % self._max_energy_range_uj
                self._last_energy_uj = energy_uj
                self._last_time = now
                return delta_uj / 1_000_000 / elapsed

        self._last_energy_uj = energy_uj
        self._last_time = now
        return None

    def _read_hwmon_power(self) -> float | None:
        if self._hwmon_power_path is None:
            return None
        microwatts = read_int_optional(self._hwmon_power_path)
        return None if microwatts is None else microwatts / 1_000_000
