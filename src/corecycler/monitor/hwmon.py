"""Hardware monitoring via hwmon/k10temp sysfs for temperature and voltage."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from corecycler.monitor.files import read_int_optional, read_text_optional

log = logging.getLogger(__name__)

HWMON_BASE = Path("/sys/class/hwmon")

# Super I/O chips that can provide Vcore via analog input.
# Nuvoton NCT66xx: common on modern MSI boards (B550, B650, X570, X670)
# Nuvoton NCT67xx: common on ASUS, MSI, ASRock boards
# ITE IT868x/IT866x/IT871x: common on Gigabyte boards
_SUPERIO_CHIPS = (
    "nct6687",
    "nct6686",
    "nct6683",
    "nct6799",
    "nct6798",
    "nct6797",
    "nct6796",
    "nct6795",
    "nct6793",
    "nct6792",
    "nct6791",
    "nct6779",
    "nct6776",
    "nct6775",
    "it8689",
    "it8688",
    "it8686",
    "it8665",
    "it8628",
    "it8625",
    "it8720",
    "it8728",
    "it8771",
    "it8772",
)


def _normalized_label(label: str) -> str:
    return re.sub(r"[^a-z0-9]", "", label.lower())


@dataclass(slots=True)
class HWMonData:
    tctl_c: float | None = None
    tdie_c: float | None = None
    ccd_temperatures_c: dict[int, float] = field(default_factory=dict)
    vcore_v: float | None = None
    vsoc_v: float | None = None


class HWMonReader:
    """Read CPU temperatures and voltages from hwmon."""

    _PREFERRED = ("zenpower", "zenpower3", "zenpower5", "k10temp")
    _FALLBACK = ("coretemp",)
    _VCORE_LABELS = {"vcore", "cpuvcore", "svi2vdd", "svi3vdd", "vddcrcpu"}
    _VSOC_LABELS = {"vsoc", "svi2vddnb", "svi3vddnb", "vddcrsoc"}

    def __init__(self) -> None:
        self._hwmon_path: Path | None = None
        self._superio_path: Path | None = None
        self._find_device()

    @staticmethod
    def _glob(path: Path, pattern: str) -> list[Path]:
        try:
            return sorted(path.glob(pattern))
        except OSError:
            log.debug("Unable to enumerate hwmon files below %s", path, exc_info=True)
            return []

    def _find_device(self) -> None:
        if not HWMON_BASE.exists():
            return
        try:
            devices = sorted(HWMON_BASE.iterdir())
        except OSError:
            log.debug("Unable to enumerate hwmon devices", exc_info=True)
            return

        fallback: Path | None = None
        for hwmon_dir in devices:
            name = read_text_optional(hwmon_dir / "name")
            if name is None:
                continue
            if name in self._PREFERRED:
                self._hwmon_path = hwmon_dir
            elif name in self._FALLBACK and fallback is None:
                fallback = hwmon_dir
            elif any(name.startswith(chip) for chip in _SUPERIO_CHIPS):
                self._superio_path = hwmon_dir
        if self._hwmon_path is None:
            self._hwmon_path = fallback

    def is_available(self) -> bool:
        return self._hwmon_path is not None

    def read(self) -> HWMonData:
        data = HWMonData()
        if self._hwmon_path is None:
            return data

        for temp_file in self._glob(self._hwmon_path, "temp*_input"):
            raw = read_int_optional(temp_file)
            if raw is None:
                continue
            temperature = raw / 1000.0
            label = read_text_optional(temp_file.with_name(temp_file.name.replace("_input", "_label"))) or ""
            normalized = _normalized_label(label)
            if "tctl" in normalized:
                data.tctl_c = temperature
            elif "tdie" in normalized:
                data.tdie_c = temperature
            elif match := re.search(r"tccd(\d+)", normalized):
                ccd_index = int(match.group(1)) - 1
                if ccd_index >= 0:
                    data.ccd_temperatures_c[ccd_index] = temperature
            elif data.tctl_c is None:
                data.tctl_c = temperature

        for input_file in self._glob(self._hwmon_path, "in*_input"):
            raw = read_int_optional(input_file)
            if raw is None:
                continue
            label = read_text_optional(input_file.with_name(input_file.name.replace("_input", "_label"))) or ""
            normalized = _normalized_label(label)
            if normalized in self._VSOC_LABELS:
                data.vsoc_v = raw / 1000.0
            elif normalized in self._VCORE_LABELS:
                data.vcore_v = raw / 1000.0

        if data.vcore_v is None and self._superio_path is not None:
            for label_file in self._glob(self._superio_path, "in*_label"):
                label = read_text_optional(label_file)
                if label is None or _normalized_label(label) not in self._VCORE_LABELS:
                    continue
                raw = read_int_optional(label_file.with_name(label_file.name.replace("_label", "_input")))
                if raw is not None:
                    data.vcore_v = raw / 1000.0
                break

        return data

    def max_cpu_temp(self) -> float | None:
        """Return the hottest readable CPU temperature, including coretemp cores."""
        if self._hwmon_path is None:
            return None
        temperatures = [
            raw / 1000.0
            for temp_file in self._glob(self._hwmon_path, "temp*_input")
            if (raw := read_int_optional(temp_file)) is not None
        ]
        return max(temperatures) if temperatures else None
