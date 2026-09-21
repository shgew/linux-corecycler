"""Comprehensive tests for PM table reader."""

from __future__ import annotations

import struct
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from corecycler.smu.pmtable import (
    PM_TABLE_OFFSETS,
    PMTableData,
    PMTableOffsets,
    PMTableReader,
    compute_fclk_uclk_ratio,
)

# ===========================================================================
# PMTableData tests
# ===========================================================================


class TestPMTableData:
    def test_defaults(self):
        data = PMTableData()
        assert data.package_power_w == 0.0
        assert data.soc_power_w == 0.0
        assert data.ppt_limit_w == 0.0
        assert data.tdc_limit_a == 0.0
        assert data.edc_limit_a == 0.0
        assert data.ppt_value_w == 0.0
        assert data.tdc_value_a == 0.0
        assert data.edc_value_a == 0.0
        assert data.tctl_c == 0.0
        assert data.tdie_c == 0.0
        assert data.raw_floats == []


# ===========================================================================
# PMTableReader tests
# ===========================================================================


class TestPMTableReader:
    def test_is_available_with_pm_table(self, tmp_path):
        smu_dir = tmp_path / "ryzen_smu_drv"
        smu_dir.mkdir()
        (smu_dir / "pm_table").write_bytes(b"\x00" * 100)
        reader = PMTableReader(sysfs_path=smu_dir)
        assert reader.is_available() is True

    def test_is_not_available(self, tmp_path):
        smu_dir = tmp_path / "ryzen_smu_drv"
        smu_dir.mkdir()
        reader = PMTableReader(sysfs_path=smu_dir)
        assert reader.is_available() is False

    def test_is_not_available_missing_dir(self, tmp_path):
        reader = PMTableReader(sysfs_path=tmp_path / "nonexistent")
        assert reader.is_available() is False

    def test_read_unavailable(self, tmp_path):
        reader = PMTableReader(sysfs_path=tmp_path / "nonexistent")
        assert reader.read() is None

    def test_read_missing_pm_table(self, tmp_path):
        smu_dir = tmp_path / "ryzen_smu_drv"
        smu_dir.mkdir()
        reader = PMTableReader(sysfs_path=smu_dir)
        assert reader.read() is None

    def test_read_empty_pm_table(self, tmp_path):
        smu_dir = tmp_path / "ryzen_smu_drv"
        smu_dir.mkdir()
        (smu_dir / "pm_table").write_bytes(b"")
        reader = PMTableReader(sysfs_path=smu_dir)
        assert reader.read() is None

    def test_read_too_short(self, tmp_path):
        smu_dir = tmp_path / "ryzen_smu_drv"
        smu_dir.mkdir()
        (smu_dir / "pm_table").write_bytes(b"\x00\x00")  # < 4 bytes
        reader = PMTableReader(sysfs_path=smu_dir)
        assert reader.read() is None

    def test_read_minimal_data(self, tmp_path):
        """4 bytes = 1 float, too short for parsing but should return PMTableData."""
        smu_dir = tmp_path / "ryzen_smu_drv"
        smu_dir.mkdir()
        data = struct.pack("<f", 42.0)
        (smu_dir / "pm_table").write_bytes(data)
        reader = PMTableReader(sysfs_path=smu_dir)
        result = reader.read()
        assert result is not None
        assert len(result.raw_floats) == 1
        assert result.raw_floats[0] == pytest.approx(42.0)

    def test_read_full_registered_pm_table(self, tmp_path):
        raw = bytearray(_build_versioned_pm_table(0x620205))
        struct.pack_into("<f", raw, 2 * 4, 225.0)
        struct.pack_into("<f", raw, 3 * 4, 142.0)
        struct.pack_into("<f", raw, 8 * 4, 190.0)
        struct.pack_into("<f", raw, 9 * 4, 95.0)
        struct.pack_into("<f", raw, 11 * 4, 70.0)
        struct.pack_into("<f", raw, 63 * 4, 230.0)
        smu_dir = _make_smu_dir(tmp_path, version_int=0x620205, raw_bytes=bytes(raw))

        result = PMTableReader(sysfs_path=smu_dir).read()

        assert result is not None
        assert result.ppt_limit_w == pytest.approx(225.0)
        assert result.ppt_value_w == pytest.approx(142.0)
        assert result.tdc_limit_a == pytest.approx(190.0)
        assert result.tdc_value_a == pytest.approx(95.0)
        assert result.edc_limit_a == pytest.approx(230.0)
        assert result.tctl_c == pytest.approx(70.0)
        assert result.package_power_w == pytest.approx(142.0)

    def test_raw_floats_always_available(self, tmp_path):
        """raw_floats should contain the full array regardless of parsing."""
        smu_dir = tmp_path / "ryzen_smu_drv"
        smu_dir.mkdir()

        floats = [float(i) for i in range(300)]
        raw = struct.pack(f"<{len(floats)}f", *floats)
        (smu_dir / "pm_table").write_bytes(raw)

        reader = PMTableReader(sysfs_path=smu_dir)
        result = reader.read()

        assert result is not None
        assert len(result.raw_floats) == 300
        assert result.raw_floats[0] == pytest.approx(0.0)
        assert result.raw_floats[299] == pytest.approx(299.0)

    def test_pm_table_read_error_preserves_readable_version(self, tmp_path):
        smu_dir = tmp_path / "ryzen_smu_drv"
        smu_dir.mkdir()
        (smu_dir / "pm_table").write_bytes(b"present")
        reader = PMTableReader(sysfs_path=smu_dir)
        with (
            patch.object(reader, "_read_pm_table_version", return_value=0x62FFFF),
            patch.object(Path, "read_bytes", side_effect=OSError("unsupported table")),
        ):
            result = reader.read()

        assert result is not None
        assert result.pm_table_version == 0x62FFFF
        assert result.is_calibrated is False
        assert result.raw_floats == []

    def test_non_aligned_data(self, tmp_path):
        """Data not aligned to 4 bytes should still parse what it can."""
        smu_dir = tmp_path / "ryzen_smu_drv"
        smu_dir.mkdir()

        # 201 floats + 2 extra bytes
        floats = [1.0] * 201
        raw = struct.pack(f"<{len(floats)}f", *floats) + b"\xaa\xbb"
        (smu_dir / "pm_table").write_bytes(raw)

        reader = PMTableReader(sysfs_path=smu_dir)
        result = reader.read()

        assert result is not None
        # Should parse 201 floats (ignoring trailing 2 bytes)
        assert len(result.raw_floats) == 201


# ===========================================================================
# Helper for version-aware sysfs mocking
# ===========================================================================


def _make_smu_dir(
    tmp_path: Path,
    *,
    version_int: int | None = None,
    raw_bytes: bytes | None = None,
    num_floats: int = 0,
) -> Path:
    """Create a mock sysfs smu directory with optional version and pm_table data.

    If raw_bytes is provided, it is written directly as pm_table.
    Otherwise, num_floats zero-floats are packed as pm_table.
    If version_int is provided, pm_table_version is written as 4-byte LE uint32.
    """
    smu_dir = tmp_path / "ryzen_smu_drv"
    smu_dir.mkdir(exist_ok=True)

    if raw_bytes is not None:
        (smu_dir / "pm_table").write_bytes(raw_bytes)
    elif num_floats > 0:
        (smu_dir / "pm_table").write_bytes(struct.pack(f"<{num_floats}f", *([0.0] * num_floats)))

    if version_int is not None:
        (smu_dir / "pm_table_version").write_bytes(struct.pack("<I", version_int))

    return smu_dir


CAPTURED_LAYOUTS = {
    0x620205: (0x994, 0x11C, 0x12C, 0x13C, 0x14C, 0x0A8),
    0x621102: (0x724, 0x11C, 0x12C, 0x13C, 0x14C, -1),
    0x621202: (0x994, 0x11C, 0x12C, 0x13C, 0x14C, 0x0A8),
    0x620105: (0x724, 0x11C, 0x12C, 0x13C, 0x14C, -1),
}


def _build_versioned_pm_table(
    version: int,
    *,
    fclk: float = 0.0,
    uclk: float = 0.0,
    mclk: float = 0.0,
    vddcr_soc: float = 0.0,
    vdd_mem: float = 0.0,
) -> bytes:
    """Build a raw table from independently captured layout offsets."""
    table_size, fclk_offset, uclk_offset, mclk_offset, soc_offset, vdd_mem_offset = CAPTURED_LAYOUTS[version]
    raw = bytearray(table_size)
    for offset, value in (
        (fclk_offset, fclk),
        (uclk_offset, uclk),
        (mclk_offset, mclk),
        (soc_offset, vddcr_soc),
        (vdd_mem_offset, vdd_mem),
    ):
        if offset >= 0 and value != 0.0:
            struct.pack_into("<f", raw, offset, value)
    return bytes(raw)


# ===========================================================================
# PMTableOffsets tests
# ===========================================================================


class TestPMTableOffsets:
    def test_frozen_dataclass_with_slots(self):
        """PMTableOffsets is a frozen dataclass with slots."""
        offsets = PMTableOffsets(
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
        )
        assert offsets.fclk == 0x11C
        assert offsets.uclk == 0x12C
        assert offsets.mclk == 0x13C
        # Verify frozen
        with pytest.raises(AttributeError):
            offsets.fclk = 0x200  # type: ignore[misc]
        # Verify slots
        assert hasattr(offsets, "__slots__")

    @pytest.mark.parametrize(("version", "layout"), CAPTURED_LAYOUTS.items())
    def test_registry_matches_captured_layout(self, version, layout):
        offsets = PM_TABLE_OFFSETS[version]
        assert (
            offsets.table_size,
            offsets.fclk,
            offsets.uclk,
            offsets.mclk,
            offsets.vddcr_soc,
            offsets.vdd_mem,
        ) == layout
        assert offsets.verified is True

    def test_registry_contains_only_captured_verified_layouts(self):
        assert set(PM_TABLE_OFFSETS) == set(CAPTURED_LAYOUTS)

    def test_verified_defaults_false(self):
        """An entry that omits ``verified`` is unverified (fail-closed default)."""
        offsets = PMTableOffsets(
            table_size=0x6A8,
            fclk=0x118,
            uclk=0x128,
            mclk=0x138,
            vddcr_soc=0xD0,
            cldo_vddp=0x430,
            cldo_vddg_iod=-1,
            cldo_vddg_ccd=-1,
            vdd_misc=0xE0,
            vdd_mem=-1,
        )
        assert offsets.verified is False


# ===========================================================================
# PMTableData new fields tests
# ===========================================================================


class TestPMTableDataNewFields:
    def test_new_fields_defaults(self):
        """PMTableData has new memory controller fields with correct defaults."""
        data = PMTableData()
        assert data.fclk_mhz == 0.0
        assert data.uclk_mhz == 0.0
        assert data.mclk_mhz == 0.0
        assert data.vddcr_soc_v == 0.0
        assert data.vdd_mem_v == 0.0
        assert data.pm_table_version == 0
        assert data.is_calibrated is False

    def test_is_verified_defaults_false(self):
        """PMTableData.is_verified is False until a verified version sets it."""
        assert PMTableData().is_verified is False


# ===========================================================================
# Version-dispatch tests
# ===========================================================================


class TestVersionDispatch:
    def test_known_version_dispatch(self, tmp_path):
        """read() with version 0x00620205 produces correct clock/voltage values."""
        raw = _build_versioned_pm_table(
            0x620205,
            fclk=2000.0,
            uclk=3000.0,
            mclk=3000.0,
            vddcr_soc=1.25,
            vdd_mem=1.395,
        )
        smu_dir = _make_smu_dir(tmp_path, version_int=0x00620205, raw_bytes=raw)
        reader = PMTableReader(sysfs_path=smu_dir)
        result = reader.read()

        assert result is not None
        assert result.is_calibrated is True
        assert result.pm_table_version == 0x00620205
        assert result.fclk_mhz == pytest.approx(2000.0)
        assert result.uclk_mhz == pytest.approx(3000.0)
        assert result.mclk_mhz == pytest.approx(3000.0)
        assert result.vddcr_soc_v == pytest.approx(1.25)
        assert result.vdd_mem_v == pytest.approx(1.395)
        assert result.is_verified is True

    def test_verified_version_also_gated(self, tmp_path):
        """The plausibility gate protects verified entries too (defense in depth)."""
        raw = _build_versioned_pm_table(0x620205, fclk=1e30, uclk=3000.0, mclk=3000.0, vddcr_soc=1.25)
        smu_dir = _make_smu_dir(tmp_path, version_int=0x00620205, raw_bytes=raw)
        result = PMTableReader(sysfs_path=smu_dir).read()
        assert result.is_calibrated is False

    def test_all_zero_registered_table_stays_calibrated(self, tmp_path):
        raw = _build_versioned_pm_table(0x620205)
        smu_dir = _make_smu_dir(tmp_path, version_int=0x620205, raw_bytes=raw)
        result = PMTableReader(sysfs_path=smu_dir).read()
        assert result is not None
        assert result.is_calibrated is True
        assert result.fclk_mhz == 0.0

    def test_unknown_version_uncalibrated(self, tmp_path):
        """read() with unknown version produces is_calibrated=False."""
        # Use a table big enough for legacy parsing
        floats = [0.0] * 300
        raw = struct.pack(f"<{len(floats)}f", *floats)
        smu_dir = _make_smu_dir(tmp_path, version_int=0x99999999, raw_bytes=raw)
        reader = PMTableReader(sysfs_path=smu_dir)
        result = reader.read()

        assert result is not None
        assert result.is_calibrated is False
        assert result.pm_table_version == 0x99999999
        assert len(result.raw_floats) > 0

    def test_no_version_file_keeps_interpreted_fields_unavailable(self, tmp_path):
        floats = [0.0] * 420
        floats[2] = 225.0
        raw = struct.pack(f"<{len(floats)}f", *floats)
        smu_dir = _make_smu_dir(tmp_path, raw_bytes=raw)
        result = PMTableReader(sysfs_path=smu_dir).read()

        assert result is not None
        assert result.ppt_limit_w == 0.0
        assert result.pm_table_version == 0
        assert result.is_calibrated is False

    def test_second_registered_zen5_layout(self, tmp_path):
        raw = _build_versioned_pm_table(
            0x621102,
            fclk=1800.0,
            uclk=3600.0,
            mclk=3600.0,
            vddcr_soc=1.15,
        )
        smu_dir = _make_smu_dir(tmp_path, version_int=0x00621102, raw_bytes=raw)
        reader = PMTableReader(sysfs_path=smu_dir)
        result = reader.read()

        assert result is not None
        assert result.is_calibrated is True
        assert result.fclk_mhz == pytest.approx(1800.0)
        assert result.uclk_mhz == pytest.approx(3600.0)
        assert result.mclk_mhz == pytest.approx(3600.0)

    def test_unknown_zen5_version_stays_uncalibrated(self, tmp_path):
        raw = bytearray(0x994)
        struct.pack_into("<f", raw, 2 * 4, 225.0)
        struct.pack_into("<f", raw, 8 * 4, 190.0)
        struct.pack_into("<f", raw, 63 * 4, 230.0)
        struct.pack_into("<f", raw, 0x11C, 1900.0)
        smu_dir = _make_smu_dir(tmp_path, version_int=0x62FFFF, raw_bytes=bytes(raw))
        result = PMTableReader(sysfs_path=smu_dir).read()

        assert result is not None
        assert result.pm_table_version == 0x62FFFF
        assert result.is_calibrated is False
        assert result.is_verified is False
        assert result.fclk_mhz == 0.0
        assert result.ppt_limit_w == 0.0
        assert result.tdc_limit_a == 0.0
        assert result.edc_limit_a == 0.0

    def test_vdd_mem_negative_offset_stays_zero(self, tmp_path):
        """offset -1 for vdd_mem means field stays at 0.0 (not read)."""
        # 0x621102 has vdd_mem=-1
        raw = _build_versioned_pm_table(
            0x621102,
            fclk=2000.0,
            uclk=3000.0,
            mclk=3000.0,
            vddcr_soc=1.25,
            vdd_mem=1.4,  # this should NOT be written since offset is -1
        )
        smu_dir = _make_smu_dir(tmp_path, version_int=0x00621102, raw_bytes=raw)
        reader = PMTableReader(sysfs_path=smu_dir)
        result = reader.read()

        assert result is not None
        assert result.is_calibrated is True
        assert result.vdd_mem_v == 0.0  # not read because offset is -1

    def test_truncated_registered_table_fails_closed(self, tmp_path):
        raw = bytearray(256)
        struct.pack_into("<f", raw, 2 * 4, 225.0)
        smu_dir = _make_smu_dir(tmp_path, version_int=0x620205, raw_bytes=bytes(raw))
        result = PMTableReader(sysfs_path=smu_dir).read()

        assert result is not None
        assert result.is_calibrated is False
        assert result.is_verified is False
        assert result.fclk_mhz == 0.0
        assert result.ppt_limit_w == 0.0


# ===========================================================================
# compute_fclk_uclk_ratio tests
# ===========================================================================


class TestComputeFclkUclkRatio:
    def test_ratio_1_1(self):
        """FCLK=UCLK produces (1, 1) ratio."""
        assert compute_fclk_uclk_ratio(2000.0, 2000.0) == (1, 1)

    def test_ratio_1_2(self):
        """UCLK=2*FCLK produces (1, 2) ratio."""
        assert compute_fclk_uclk_ratio(1000.0, 2000.0) == (1, 2)

    def test_zero_fclk_returns_none(self):
        assert compute_fclk_uclk_ratio(0.0, 2000.0) is None

    def test_zero_uclk_returns_none(self):
        assert compute_fclk_uclk_ratio(2000.0, 0.0) is None

    def test_negative_returns_none(self):
        assert compute_fclk_uclk_ratio(-100.0, 2000.0) is None

    def test_nonfinite_returns_none(self):
        assert compute_fclk_uclk_ratio(float("nan"), 2000.0) is None

    def test_sub_rounding_resolution_returns_none(self):
        assert compute_fclk_uclk_ratio(1.0, 2000.0) is None

    def test_ratio_2_3(self):
        """DDR5-6000 with FCLK capped: FCLK=2000, UCLK=3000 → 2:3."""
        assert compute_fclk_uclk_ratio(2000.0, 3000.0) == (2, 3)

    def test_ratio_1_3(self):
        """FCLK=1000, UCLK=3000 → 1:3."""
        assert compute_fclk_uclk_ratio(1000.0, 3000.0) == (1, 3)

    def test_near_1_1_ratio(self):
        """Slightly off ratio should still round to 1:1."""
        assert compute_fclk_uclk_ratio(2000.0, 2001.0) == (1, 1)

    def test_near_1_2_ratio(self):
        """Slightly off ratio should still round to 1:2."""
        assert compute_fclk_uclk_ratio(1000.0, 1999.0) == (1, 2)


# ===========================================================================
# read_power_limits: PBO limits for the tuning context
# ===========================================================================


class TestReadPowerLimits:
    def _tree(self, tmp_path, version: int | None, floats: list[float]):
        smu_dir = tmp_path / "ryzen_smu_drv"
        smu_dir.mkdir()
        if version is not None:
            (smu_dir / "pm_table_version").write_bytes(struct.pack("<I", version))
        table_size = CAPTURED_LAYOUTS.get(version, (len(floats) * 4,))[0]
        raw = bytearray(table_size)
        struct.pack_into(f"<{len(floats)}f", raw, 0, *floats)
        (smu_dir / "pm_table").write_bytes(raw)
        return smu_dir

    def _floats(self, ppt=225.0, tdc=190.0, edc=230.0) -> list[float]:
        floats = [0.0] * 420
        floats[2] = ppt
        floats[8] = tdc
        floats[63] = edc
        return floats

    def test_reads_zen5_limits(self, tmp_path):
        from corecycler.smu.pmtable import read_power_limits

        smu_dir = self._tree(tmp_path, 0x00620205, self._floats())
        assert read_power_limits(smu_dir) == (225.0, 190.0, 230.0)

    def test_unknown_generation_fails_closed(self, tmp_path):
        from corecycler.smu.pmtable import read_power_limits

        smu_dir = self._tree(tmp_path, 0x00540104, self._floats())
        assert read_power_limits(smu_dir) == (None, None, None)

    def test_implausible_values_fail_closed_per_field(self, tmp_path):
        from corecycler.smu.pmtable import read_power_limits

        smu_dir = self._tree(tmp_path, 0x00620205, self._floats(ppt=1e9, tdc=190.0, edc=5.0))
        assert read_power_limits(smu_dir) == (None, 190.0, None)

    def test_zero_reads_as_absent(self, tmp_path):
        from corecycler.smu.pmtable import read_power_limits

        smu_dir = self._tree(tmp_path, 0x00620205, self._floats(ppt=0.0, tdc=0.0, edc=0.0))
        assert read_power_limits(smu_dir) == (None, None, None)

    def test_missing_table_fails_closed(self, tmp_path):
        from corecycler.smu.pmtable import read_power_limits

        assert read_power_limits(tmp_path / "nope") == (None, None, None)
