"""PM (Power Monitoring) table reader for live telemetry."""

from __future__ import annotations

import logging
import math
import struct
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

SYSFS_BASE = Path("/sys/kernel/ryzen_smu_drv")


# ===========================================================================
# Version-aware PM table offset registry
# ===========================================================================


@dataclass(frozen=True, slots=True)
class PMTableOffsets:
    """Named byte offsets for a specific PM table version.

    Offsets are in bytes (not float indices). A value of -1 means the
    field is not available for this version. ``verified`` is True only for
    offsets confirmed against real silicon; it defaults to False so an
    unconfirmed entry fails closed (surfaced as community-sourced, never as
    Verified).
    """

    table_size: int
    fclk: int  # byte offset
    uclk: int
    mclk: int
    vddcr_soc: int
    cldo_vddp: int
    cldo_vddg_iod: int  # -1 if not available
    cldo_vddg_ccd: int  # -1 if not available
    vdd_misc: int
    vdd_mem: int  # -1 if not calibrated
    verified: bool = False  # True only if confirmed on real hardware


# Exact version match only. No Zen 4 prefix fallback: version 0x540208 uses a
# +4-byte-shifted layout, so guessing by the 0x54 family would misread every
# field. Source: ZenStates-Core PowerTable.cs. ``verified`` marks offsets
# confirmed on real silicon; an unverified entry still parses but is gated by
# the runtime plausibility check in PMTableReader.read() and never labelled
# "Verified".
# VDD_MEM at 0x0A8; VDDQ at 0x0E8 (per-channel pair at 0x0E8/0x0EC).
PM_TABLE_OFFSETS: dict[int, PMTableOffsets] = {
    0x620205: PMTableOffsets(
        table_size=0x994,
        fclk=0x11C,
        uclk=0x12C,
        mclk=0x13C,
        vddcr_soc=0x14C,
        cldo_vddp=0x434,
        cldo_vddg_iod=0x40C,
        cldo_vddg_ccd=0x414,
        vdd_misc=0xE8,
        vdd_mem=0x0A8,
        verified=True,
    ),
    0x621102: PMTableOffsets(
        table_size=0x724,
        fclk=0x11C,
        uclk=0x12C,
        mclk=0x13C,
        vddcr_soc=0x14C,
        cldo_vddp=0x434,
        cldo_vddg_iod=0x40C,
        cldo_vddg_ccd=0x414,
        vdd_misc=0xE8,
        vdd_mem=-1,
        verified=True,
    ),
    0x621202: PMTableOffsets(
        table_size=0x994,
        fclk=0x11C,
        uclk=0x12C,
        mclk=0x13C,
        vddcr_soc=0x14C,
        cldo_vddp=0x434,
        cldo_vddg_iod=0x40C,
        cldo_vddg_ccd=0x414,
        vdd_misc=0xE8,
        vdd_mem=0x0A8,
        verified=True,
    ),
    0x620105: PMTableOffsets(
        table_size=0x724,
        fclk=0x11C,
        uclk=0x12C,
        mclk=0x13C,
        vddcr_soc=0x14C,
        cldo_vddp=0x434,
        cldo_vddg_iod=0x40C,
        cldo_vddg_ccd=0x414,
        vdd_misc=0xE8,
        vdd_mem=-1,
        verified=True,
    ),
}
# Generous physical bounds for the runtime plausibility gate. A field is
# accepted when it is zero (absent for this version/state) or finite and within
# range; anything else means the offset map decoded non-telemetry bytes.
_CLOCK_MIN_MHZ = 200.0
_CLOCK_MAX_MHZ = 6000.0
_VOLT_MIN_V = 0.3
_VOLT_MAX_V = 2.0


def _plausible(value: float, lo: float, hi: float) -> bool:
    """True if value is exactly 0.0 (absent) or finite and within [lo, hi]."""
    if value == 0.0:
        return True
    return math.isfinite(value) and lo <= value <= hi


def _read_float(raw: bytes, byte_offset: int, table_size: int) -> float:
    """Read one little-endian float within the registered table boundary."""
    if byte_offset < 0 or byte_offset + 4 > table_size or byte_offset + 4 > len(raw):
        return 0.0
    return struct.unpack_from("<f", raw, byte_offset)[0]


# ===========================================================================
# PM table data
# ===========================================================================


@dataclass(slots=True)
class PMTableData:
    """Parsed PM table telemetry values."""

    # package-level
    package_power_w: float = 0.0
    soc_power_w: float = 0.0
    ppt_limit_w: float = 0.0
    tdc_limit_a: float = 0.0
    edc_limit_a: float = 0.0
    ppt_value_w: float = 0.0
    tdc_value_a: float = 0.0
    edc_value_a: float = 0.0
    tctl_c: float = 0.0
    tdie_c: float = 0.0

    # memory controller clocks and voltages (version-aware parsing)
    fclk_mhz: float = 0.0
    uclk_mhz: float = 0.0
    mclk_mhz: float = 0.0
    vddcr_soc_v: float = 0.0
    vdd_mem_v: float = 0.0
    vddq_v: float = 0.0
    pm_table_version: int = 0
    is_calibrated: bool = False
    is_verified: bool = False  # offsets confirmed on real silicon (not just sourced)

    raw_floats: list[float] = field(default_factory=list)


# Physical bounds for PBO limit plausibility (fail closed to None on garbage).
_PPT_MIN_W, _PPT_MAX_W = 30.0, 1000.0
_AMP_MIN_A, _AMP_MAX_A = 30.0, 2000.0


def read_power_limits(sysfs_path: Path | None = None) -> tuple[float | None, float | None, float | None]:
    """Read live PBO power limits, failing closed for unknown layouts."""
    reader = PMTableReader(sysfs_path) if sysfs_path is not None else PMTableReader()
    data = reader.read()
    if data is None or not data.is_calibrated:
        return None, None, None

    def _gate(value: float, lower: float, upper: float) -> float | None:
        return value if math.isfinite(value) and lower <= value <= upper else None

    return (
        _gate(data.ppt_limit_w, _PPT_MIN_W, _PPT_MAX_W),
        _gate(data.tdc_limit_a, _AMP_MIN_A, _AMP_MAX_A),
        _gate(data.edc_limit_a, _AMP_MIN_A, _AMP_MAX_A),
    )


# ===========================================================================
# FCLK:UCLK ratio computation
# ===========================================================================


def compute_fclk_uclk_ratio(fclk_mhz: float, uclk_mhz: float) -> tuple[int, int] | None:
    """Compute FCLK:UCLK ratio as a simplified integer pair.

    Common AMD DDR5 ratios:
    - 1:1 - FCLK=UCLK (coupled, optimal latency)
    - 2:3 - FCLK=2000, UCLK=3000 (DDR5-6000 with FCLK capped at ~2000)
    - 1:2 - FCLK=UCLK/2 (decoupled)
    Returns None if values are zero/negative or non-finite (NaN/inf would crash
    round()).
    """
    if not (math.isfinite(fclk_mhz) and math.isfinite(uclk_mhz)):
        return None
    if fclk_mhz <= 0 or uclk_mhz <= 0:
        return None
    from math import gcd

    # Round to nearest 100 MHz to handle measurement noise
    f = round(fclk_mhz / 100)
    u = round(uclk_mhz / 100)
    if f <= 0 or u <= 0:
        return None
    g = gcd(f, u)
    return (f // g, u // g)


# ===========================================================================
# PM table reader
# ===========================================================================


class PMTableReader:
    """Read exact, registered SMU PM table layouts."""

    def __init__(self, sysfs_path: Path = SYSFS_BASE) -> None:
        self.sysfs = sysfs_path

    def is_available(self) -> bool:
        return (self.sysfs / "pm_table").exists()

    def read(self) -> PMTableData | None:
        version = self._read_pm_table_version()
        data = PMTableData(pm_table_version=version or 0)
        pm_path = self.sysfs / "pm_table"
        if not pm_path.exists():
            return None
        try:
            raw = pm_path.read_bytes()
        except OSError:
            return data
        if len(raw) < 4:
            return None

        num_floats = len(raw) // 4
        data.raw_floats = list(struct.unpack(f"<{num_floats}f", raw[: num_floats * 4]))
        if version is None:
            return data
        offsets = PM_TABLE_OFFSETS.get(version)
        if offsets is None:
            return data
        if len(raw) != offsets.table_size:
            log.warning(
                "PM table v%#010x has size %#x, expected %#x; treating as uncalibrated",
                version,
                len(raw),
                offsets.table_size,
            )
            return data

        self._parse_versioned(data, raw, offsets)
        if not self._memory_values_plausible(data):
            log.warning("PM table v%#010x decoded implausible values; treating as uncalibrated", version)
            self._blank_memory_values(data)
            return data

        data.is_calibrated = True
        data.is_verified = offsets.verified
        self._parse_granite_ridge(data, raw, offsets)
        return data

    def _read_pm_table_version(self) -> int | None:
        try:
            raw = (self.sysfs / "pm_table_version").read_bytes()
        except OSError:
            return None
        return struct.unpack("<I", raw[:4])[0] if len(raw) >= 4 else None

    @staticmethod
    def _parse_versioned(data: PMTableData, raw: bytes, offsets: PMTableOffsets) -> None:
        data.fclk_mhz = _read_float(raw, offsets.fclk, offsets.table_size)
        data.uclk_mhz = _read_float(raw, offsets.uclk, offsets.table_size)
        data.mclk_mhz = _read_float(raw, offsets.mclk, offsets.table_size)
        data.vddcr_soc_v = _read_float(raw, offsets.vddcr_soc, offsets.table_size)
        data.vdd_mem_v = _read_float(raw, offsets.vdd_mem, offsets.table_size)
        data.vddq_v = _read_float(raw, offsets.vdd_misc, offsets.table_size)

    @staticmethod
    def _memory_values_plausible(data: PMTableData) -> bool:
        return all(
            _plausible(value, _CLOCK_MIN_MHZ, _CLOCK_MAX_MHZ) for value in (data.fclk_mhz, data.uclk_mhz, data.mclk_mhz)
        ) and all(
            _plausible(value, _VOLT_MIN_V, _VOLT_MAX_V) for value in (data.vddcr_soc_v, data.vdd_mem_v, data.vddq_v)
        )

    @staticmethod
    def _blank_memory_values(data: PMTableData) -> None:
        data.fclk_mhz = 0.0
        data.uclk_mhz = 0.0
        data.mclk_mhz = 0.0
        data.vddcr_soc_v = 0.0
        data.vdd_mem_v = 0.0
        data.vddq_v = 0.0
        data.is_calibrated = False
        data.is_verified = False

    @staticmethod
    def _parse_granite_ridge(data: PMTableData, raw: bytes, offsets: PMTableOffsets) -> None:
        def read(index: int) -> float:
            return _read_float(raw, index * 4, offsets.table_size)

        data.ppt_limit_w = read(2)
        data.ppt_value_w = read(3)
        data.tdc_limit_a = read(8)
        data.tdc_value_a = read(9)
        data.edc_limit_a = read(63)
        data.edc_value_a = 0.0
        data.tctl_c = read(11)
        data.tdie_c = 0.0
        data.package_power_w = data.ppt_value_w
        data.soc_power_w = 0.0
