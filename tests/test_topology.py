"""Comprehensive tests for CPU topology detection."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from corecycler.engine.topology import (
    CPUTopology,
    LogicalCPU,
    PhysicalCore,
    _detect_ccd_layout,
    _detect_x3d,
    _l3_cache_dir,
    _l3_id_sort_key,
    _l3_size_kib,
    _parse_cpuinfo,
    _parse_sysfs,
    detect_topology,
    get_first_logical_cpu,
    get_physical_core_list,
)
from tests.conftest import (
    CPUINFO_DUAL_CCD_SMT,
    CPUINFO_INTEL_10CORE_SMT,
    CPUINFO_SINGLE_CCD_NO_SMT,
    CPUINFO_X3D_SINGLE_CCD,
    CPUINFO_ZEN5_9950X3D2,
)

if TYPE_CHECKING:
    from collections.abc import Callable

# ---------------------------------------------------------------------------
# Helper to run _parse_cpuinfo with fake data
# ---------------------------------------------------------------------------


def parse_cpuinfo_from_text(text: str) -> CPUTopology:
    topo = CPUTopology()
    mock_path = MagicMock()
    mock_path.exists.return_value = True
    mock_path.read_text.return_value = text
    with patch("corecycler.engine.topology.CPUINFO", mock_path):
        _parse_cpuinfo(topo)
    return topo


# ---------------------------------------------------------------------------
# _parse_cpuinfo tests
# ---------------------------------------------------------------------------


class TestParseCpuinfo:
    def test_dual_ccd_smt_core_count(self):
        topo = parse_cpuinfo_from_text(CPUINFO_DUAL_CCD_SMT)
        assert topo.physical_cores == 8
        assert topo.logical_cpus_count == 16
        assert topo.smt_enabled is True

    def test_dual_ccd_smt_model_name(self):
        topo = parse_cpuinfo_from_text(CPUINFO_DUAL_CCD_SMT)
        assert "9950X3D" in topo.model_name
        assert topo.vendor == "AuthenticAMD"
        assert topo.family == 26
        assert topo.model == 68
        assert topo.stepping == 2

    def test_dual_ccd_smt_logical_map(self):
        topo = parse_cpuinfo_from_text(CPUINFO_DUAL_CCD_SMT)
        assert len(topo.logical_map) == 16
        for i in range(16):
            assert i in topo.logical_map

    def test_dual_ccd_smt_siblings(self):
        """Each physical core should have exactly 2 SMT siblings."""
        topo = parse_cpuinfo_from_text(CPUINFO_DUAL_CCD_SMT)
        for lcpu in topo.logical_map.values():
            assert len(lcpu.core_cpus) == 2
            assert lcpu.logical_id in lcpu.core_cpus

    def test_single_ccd_no_smt(self):
        topo = parse_cpuinfo_from_text(CPUINFO_SINGLE_CCD_NO_SMT)
        assert topo.physical_cores == 4
        assert topo.logical_cpus_count == 4
        assert topo.smt_enabled is False
        assert topo.vendor == "AuthenticAMD"
        assert topo.family == 25

    def test_single_ccd_no_smt_siblings(self):
        topo = parse_cpuinfo_from_text(CPUINFO_SINGLE_CCD_NO_SMT)
        for lcpu in topo.logical_map.values():
            assert len(lcpu.core_cpus) == 1

    def test_intel_with_smt(self):
        topo = parse_cpuinfo_from_text(CPUINFO_INTEL_10CORE_SMT)
        assert topo.physical_cores == 2
        assert topo.logical_cpus_count == 4
        assert topo.smt_enabled is True
        assert topo.vendor == "GenuineIntel"
        assert topo.family == 6

    def test_x3d_single_ccd(self):
        topo = parse_cpuinfo_from_text(CPUINFO_X3D_SINGLE_CCD)
        assert "7800X3D" in topo.model_name
        assert topo.physical_cores == 2
        assert topo.smt_enabled is True

    def test_empty_cpuinfo(self):
        topo = parse_cpuinfo_from_text("")
        assert topo.physical_cores == 0
        assert topo.logical_cpus_count == 0
        assert topo.smt_enabled is False

    def test_missing_cpuinfo(self):
        topo = CPUTopology()
        mock_path = MagicMock()
        mock_path.exists.return_value = False
        with patch("corecycler.engine.topology.CPUINFO", mock_path):
            _parse_cpuinfo(topo)
        assert topo.physical_cores == 0

    def test_no_trailing_blank_line(self):
        """Last entry without trailing blank should still be parsed."""
        text = (
            "processor\t: 0\nvendor_id\t: AuthenticAMD\ncpu family\t: 25\n"
            "model\t\t: 33\nmodel name\t: Test\nstepping\t: 1\n"
            "core id\t\t: 0\nphysical id\t: 0"
        )
        topo = parse_cpuinfo_from_text(text)
        assert topo.physical_cores == 1
        assert 0 in topo.logical_map

    def test_package_id_preserved(self):
        topo = parse_cpuinfo_from_text(CPUINFO_DUAL_CCD_SMT)
        for lcpu in topo.logical_map.values():
            assert lcpu.package_id == 0

    def test_only_first_model_name_used(self):
        """Model name/vendor/family should only be set from the first processor entry."""
        text = CPUINFO_SINGLE_CCD_NO_SMT
        topo = parse_cpuinfo_from_text(text)
        assert "5800X" in topo.model_name
        assert topo.family == 25


# ---------------------------------------------------------------------------
# _parse_sysfs tests
# ---------------------------------------------------------------------------


class TestParseSysfs:
    def test_missing_sysfs_leaves_online_proof_unknown(self):
        topo = CPUTopology()
        mock_path = MagicMock()
        mock_path.exists.return_value = False
        with patch("corecycler.engine.topology.SYSFS_CPU", mock_path):
            _parse_sysfs(topo)
        assert topo.cpus_all_online is None

    def test_online_range_simple(self, tmp_path):
        cpu_dir = tmp_path / "cpu"
        cpu_dir.mkdir()
        (cpu_dir / "online").write_text("0-7\n")
        topo = CPUTopology()
        with patch("corecycler.engine.topology.SYSFS_CPU", cpu_dir):
            _parse_sysfs(topo)
        assert topo.logical_cpus_count == 8
        assert topo.cpus_all_online is None

    def test_online_range_multi(self, tmp_path):
        cpu_dir = tmp_path / "cpu"
        cpu_dir.mkdir()
        (cpu_dir / "online").write_text("0-15,32-47\n")
        topo = CPUTopology()
        with patch("corecycler.engine.topology.SYSFS_CPU", cpu_dir):
            _parse_sysfs(topo)
        assert topo.logical_cpus_count == 32

    def test_online_single_cpus(self, tmp_path):
        cpu_dir = tmp_path / "cpu"
        cpu_dir.mkdir()
        (cpu_dir / "online").write_text("0,1,2,3\n")
        topo = CPUTopology()
        with patch("corecycler.engine.topology.SYSFS_CPU", cpu_dir):
            _parse_sysfs(topo)
        assert topo.logical_cpus_count == 4

    def test_does_not_overwrite_existing_count(self, tmp_path):
        cpu_dir = tmp_path / "cpu"
        cpu_dir.mkdir()
        (cpu_dir / "online").write_text("0-3\n")
        topo = CPUTopology(logical_cpus_count=99)
        with patch("corecycler.engine.topology.SYSFS_CPU", cpu_dir):
            _parse_sysfs(topo)
        assert topo.logical_cpus_count == 99

    def test_matching_nonempty_online_and_present_proves_all_online(self, tmp_path):
        cpu_dir = tmp_path / "cpu"
        cpu_dir.mkdir()
        (cpu_dir / "online").write_text("0-7\n")
        (cpu_dir / "present").write_text("0-7\n")
        topo = CPUTopology()
        with patch("corecycler.engine.topology.SYSFS_CPU", cpu_dir):
            _parse_sysfs(topo)
        assert topo.cpus_all_online is True

    @pytest.mark.parametrize(("online", "present"), [("0-3", "0-7"), ("", "0-7"), ("0-7", "")])
    def test_online_proof_distinguishes_mismatch_from_unknown(self, tmp_path, online, present):
        cpu_dir = tmp_path / "cpu"
        cpu_dir.mkdir()
        (cpu_dir / "online").write_text(online)
        (cpu_dir / "present").write_text(present)
        topo = CPUTopology()
        with patch("corecycler.engine.topology.SYSFS_CPU", cpu_dir):
            _parse_sysfs(topo)
        assert topo.cpus_all_online is (False if online and present else None)


# ---------------------------------------------------------------------------
# _detect_ccd_layout tests
# ---------------------------------------------------------------------------


class TestDetectCCDLayout:
    def test_l3_cache_lookup_without_a_level_three_entry_returns_none(self, tmp_path):
        cache_dir = tmp_path / "cpu0" / "cache"
        cache_dir.mkdir(parents=True)

        with patch("corecycler.engine.topology.SYSFS_CPU", tmp_path):
            assert _l3_cache_dir(0) is None

    def test_l3_size_without_a_size_file_returns_none(self, tmp_path):
        assert _l3_size_kib(tmp_path) is None

    def test_l3_size_with_an_unparseable_value_returns_none(self, tmp_path):
        (tmp_path / "size").write_text("unknown")

        assert _l3_size_kib(tmp_path) is None

    def test_single_l3_group(self, tmp_path):
        """All cores sharing one L3 = 1 CCD."""
        topo = parse_cpuinfo_from_text(CPUINFO_SINGLE_CCD_NO_SMT)

        cpu_dir = tmp_path / "cpu"
        for i in range(4):
            cache_dir = cpu_dir / f"cpu{i}" / "cache" / "index3"
            cache_dir.mkdir(parents=True)
            (cache_dir / "level").write_text("3")
            (cache_dir / "id").write_text("0")

        with patch("corecycler.engine.topology.SYSFS_CPU", cpu_dir):
            _detect_ccd_layout(topo)

        assert topo.ccds == 1
        assert topo.ccd_layout_known is True
        assert len(topo.cores) == 4
        for pc in topo.cores.values():
            assert pc.ccd == 0

    def test_two_l3_groups(self, tmp_path):
        """Cores split across two L3 caches = 2 CCDs."""
        topo = parse_cpuinfo_from_text(CPUINFO_DUAL_CCD_SMT)

        cpu_dir = tmp_path / "cpu"
        for i in range(16):
            phys_core = topo.logical_map[i].physical_core
            cache_dir = cpu_dir / f"cpu{i}" / "cache" / "index3"
            cache_dir.mkdir(parents=True)
            (cache_dir / "level").write_text("3")
            # cores 0-3 -> L3 id=0, cores 4-7 -> L3 id=1
            l3_id = "0" if phys_core < 4 else "1"
            (cache_dir / "id").write_text(l3_id)

        with patch("corecycler.engine.topology.SYSFS_CPU", cpu_dir):
            _detect_ccd_layout(topo)

        assert topo.ccds == 2
        assert topo.ccd_layout_known is True
        for pc in topo.cores.values():
            expected_ccd = 0 if pc.core_id < 4 else 1
            assert pc.ccd == expected_ccd

    def test_no_cache_dirs(self, tmp_path):
        """If no cache sysfs exists, default to 1 CCD."""
        topo = parse_cpuinfo_from_text(CPUINFO_SINGLE_CCD_NO_SMT)
        cpu_dir = tmp_path / "cpu"
        cpu_dir.mkdir()
        with patch("corecycler.engine.topology.SYSFS_CPU", cpu_dir):
            _detect_ccd_layout(topo)
        assert topo.ccds == 1
        assert topo.ccd_layout_known is False

    def test_cache_without_id_leaves_layout_unknown(self, tmp_path):
        topo = parse_cpuinfo_from_text(CPUINFO_SINGLE_CCD_NO_SMT)
        cpu_dir = tmp_path / "cpu"
        for i in range(4):
            cache_dir = cpu_dir / f"cpu{i}" / "cache" / "index3"
            cache_dir.mkdir(parents=True)
            (cache_dir / "level").write_text("3")
        with patch("corecycler.engine.topology.SYSFS_CPU", cpu_dir):
            _detect_ccd_layout(topo)
        assert topo.ccd_layout_known is False

    @pytest.mark.parametrize("bad_id", ["0x0", "", "  ", "abc"])
    def test_malformed_l3_id_does_not_crash(self, tmp_path, bad_id):
        """A non-decimal sysfs L3 cache `id` (transient zero-byte read, a hex id)
        must not crash detection with ValueError on int()."""
        topo = parse_cpuinfo_from_text(CPUINFO_SINGLE_CCD_NO_SMT)
        cpu_dir = tmp_path / "cpu"
        for i in range(4):
            cache_dir = cpu_dir / f"cpu{i}" / "cache" / "index3"
            cache_dir.mkdir(parents=True)
            (cache_dir / "level").write_text("3")
            (cache_dir / "id").write_text(bad_id)
        with patch("corecycler.engine.topology.SYSFS_CPU", cpu_dir):
            _detect_ccd_layout(topo)  # must not raise
        assert topo.ccd_layout_known is False
        assert all(core.ccd is None for core in topo.cores.values())

    def test_l3_id_sort_key_orders_numeric_then_malformed(self):
        """Numeric ids sort by value; malformed ids sort last, deterministically."""
        groups = {"2": [0], "0x0": [1], "0": [2], "": [3], "10": [4]}
        ordered = [k for k, _ in sorted(groups.items(), key=_l3_id_sort_key)]
        assert ordered[:3] == ["0", "2", "10"]  # numerics by value, not lexically
        assert set(ordered[3:]) == {"0x0", ""}  # malformed pushed to the end


# ---------------------------------------------------------------------------
# _detect_x3d tests
# ---------------------------------------------------------------------------


def _x3d_topology(
    tmp_path: Path,
    cpuinfo: str,
    ccd_of: Callable[[int], int],
    l3_of_ccd: dict[int, str] | None,
) -> CPUTopology:
    """Cores built from cpuinfo with CCDs assigned by ``ccd_of``; ``l3_of_ccd``
    materialises a sysfs L3 ``size`` per CCD, None leaves sysfs absent."""
    topo = parse_cpuinfo_from_text(cpuinfo)
    topo.ccds = len({ccd_of(lcpu.physical_core) for lcpu in topo.logical_map.values()})
    for lcpu in topo.logical_map.values():
        pc = lcpu.physical_core
        if pc not in topo.cores:
            topo.cores[pc] = PhysicalCore(core_id=pc, ccd=ccd_of(pc), logical_cpus=lcpu.core_cpus)
    sysfs = tmp_path / "cpu"
    if l3_of_ccd is not None:
        for core in topo.cores.values():
            cache_dir = sysfs / f"cpu{core.logical_cpus[0]}" / "cache" / "index3"
            cache_dir.mkdir(parents=True)
            (cache_dir / "level").write_text("3")
            (cache_dir / "size").write_text(l3_of_ccd[core.ccd])
    with patch("corecycler.engine.topology.SYSFS_CPU", sysfs):
        _detect_x3d(topo)
    return topo


def _vcache_ccds(topo: CPUTopology) -> set[int]:
    return {pc.ccd for pc in topo.cores.values() if pc.has_vcache}


class TestDetectX3D:
    @pytest.mark.parametrize(
        "model_name,expected",
        [
            ("AMD Ryzen 9 9950X3D", True),
            ("AMD Ryzen 9 9950X3D2", True),
            ("AMD Ryzen 7 7800X3D", True),
            ("AMD Ryzen 7 5800X3D", True),
            ("AMD Ryzen 9 9900X3D", True),
            ("AMD Ryzen 9 7950X3D", True),
            ("AMD Ryzen 9 9950X", False),
            ("AMD Ryzen 7 5800X", False),
            ("Intel Core i9-10900K", False),
        ],
    )
    def test_x3d_detection_by_name(self, model_name, expected):
        topo = CPUTopology(model_name=model_name)
        with patch("corecycler.engine.topology.SYSFS_CPU", MagicMock()):
            _detect_x3d(topo)
        assert topo.is_x3d == expected

    def test_single_ccd_x3d_without_sysfs_is_vcache(self, tmp_path):
        """No cache sysfs at all: the CCD pass leaves ccd=None and ccds=1, and
        the name alone still marks the whole part."""
        topo = parse_cpuinfo_from_text(CPUINFO_X3D_SINGLE_CCD)
        with patch("corecycler.engine.topology.SYSFS_CPU", tmp_path / "cpu"):
            _detect_ccd_layout(topo)
            _detect_x3d(topo)
        assert topo.is_x3d is True
        assert topo.ccds == 1
        assert all(pc.ccd is None and pc.has_vcache for pc in topo.cores.values())

    @pytest.mark.parametrize(
        "l3_of_ccd,expected",
        [
            ({0: "96M", 1: "32M"}, {0}),
            ({0: "32M", 1: "96M"}, {1}),
            ({0: "98304K", 1: "32768K"}, {0}),
            ({0: "32M", 1: "32M"}, set()),
            ({0: "96M", 1: "96M"}, {0, 1}),
        ],
        ids=["ccd0", "ccd1", "kib-units", "no-vcache", "dual-vcache"],
    )
    def test_dual_ccd_vcache_follows_l3_size(self, tmp_path, l3_of_ccd, expected):
        topo = _x3d_topology(tmp_path, CPUINFO_DUAL_CCD_SMT, lambda pc: 0 if pc < 4 else 1, l3_of_ccd)
        assert topo.is_x3d is True
        assert _vcache_ccds(topo) == expected

    def test_9950x3d2_every_core_has_vcache(self, tmp_path):
        topo = _x3d_topology(tmp_path, CPUINFO_ZEN5_9950X3D2, lambda pc: pc // 8, {0: "96M", 1: "96M"})
        assert topo.physical_cores == 16
        assert topo.logical_cpus_count == 32
        assert topo.ccds == 2
        assert all(pc.has_vcache for pc in topo.cores.values())

    def test_dual_ccd_x3d_without_l3_sizes_marks_nothing(self, tmp_path):
        topo = _x3d_topology(tmp_path, CPUINFO_DUAL_CCD_SMT, lambda pc: 0 if pc < 4 else 1, None)
        assert topo.is_x3d is True
        assert _vcache_ccds(topo) == set()

    def test_non_x3d_skips_vcache_detection(self, tmp_path):
        topo = _x3d_topology(tmp_path, CPUINFO_SINGLE_CCD_NO_SMT, lambda _pc: 0, {0: "96M"})
        assert topo.is_x3d is False
        assert _vcache_ccds(topo) == set()


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_get_first_logical_cpu(self, topo_dual_ccd_x3d):
        topo = topo_dual_ccd_x3d
        for core_id, core in topo.cores.items():
            result = get_first_logical_cpu(topo, core_id)
            assert result == core.logical_cpus[0]

    def test_get_first_logical_cpu_missing_core(self):
        topo = CPUTopology()
        assert get_first_logical_cpu(topo, 999) == 999

    def test_get_physical_core_list(self, topo_dual_ccd_x3d):
        cores = get_physical_core_list(topo_dual_ccd_x3d)
        assert cores == sorted(cores)
        assert cores == sorted(set(cores))
        assert len(cores) == 8

    def test_get_physical_core_list_empty(self):
        topo = CPUTopology()
        assert get_physical_core_list(topo) == []


# ---------------------------------------------------------------------------
# Integration-style test using detect_topology with full mocking
# ---------------------------------------------------------------------------


class TestDetectTopologyIntegration:
    def test_with_mocked_cpuinfo_and_sysfs(self, tmp_path):
        """Full detect_topology with mocked /proc/cpuinfo and /sys."""
        mock_cpuinfo = MagicMock()
        mock_cpuinfo.exists.return_value = True
        mock_cpuinfo.read_text.return_value = CPUINFO_SINGLE_CCD_NO_SMT

        cpu_dir = tmp_path / "cpu"
        cpu_dir.mkdir()
        (cpu_dir / "online").write_text("0-3")
        for i in range(4):
            cache_dir = cpu_dir / f"cpu{i}" / "cache" / "index3"
            cache_dir.mkdir(parents=True)
            (cache_dir / "level").write_text("3")
            (cache_dir / "id").write_text("0")

        with (
            patch("corecycler.engine.topology.CPUINFO", mock_cpuinfo),
            patch("corecycler.engine.topology.SYSFS_CPU", cpu_dir),
        ):
            topo = detect_topology()

        assert topo.physical_cores == 4
        assert topo.logical_cpus_count == 4
        assert topo.smt_enabled is False
        assert topo.ccds == 1
        assert topo.is_x3d is False

    def test_completely_missing_everything(self):
        """Should not crash even if /proc/cpuinfo and /sys are missing."""
        mock_cpuinfo = MagicMock()
        mock_cpuinfo.exists.return_value = False
        mock_sysfs = MagicMock()
        mock_sysfs.exists.return_value = False

        with (
            patch("corecycler.engine.topology.CPUINFO", mock_cpuinfo),
            patch("corecycler.engine.topology.SYSFS_CPU", mock_sysfs),
        ):
            topo = detect_topology()

        assert topo.physical_cores == 0
        assert topo.logical_cpus_count == 0


# ---------------------------------------------------------------------------
# Dataclass tests
# ---------------------------------------------------------------------------


class TestDataclasses:
    def test_logical_cpu_frozen(self):
        lcpu = LogicalCPU(logical_id=0, physical_core=0, package_id=0, core_cpus=(0, 8))
        with pytest.raises(AttributeError):
            lcpu.logical_id = 1  # type: ignore[misc]

    def test_physical_core_frozen(self):
        pc = PhysicalCore(core_id=0, ccd=0, logical_cpus=(0, 8))
        with pytest.raises(AttributeError):
            pc.core_id = 1  # type: ignore[misc]

    def test_physical_core_vcache_default(self):
        pc = PhysicalCore(core_id=0, ccd=0, logical_cpus=(0,))
        assert pc.has_vcache is False

    def test_cpu_topology_defaults(self):
        topo = CPUTopology()
        assert topo.model_name == ""
        assert topo.vendor == ""
        assert topo.ccds == 0
        assert topo.is_x3d is False
        assert topo.cores == {}
        assert topo.logical_map == {}


# ---------------------------------------------------------------------------
# Edge case tests for hardware variations and fallback scenarios
# ---------------------------------------------------------------------------


class TestTopologyEdgeCases:
    def test_no_cache_sysfs_defaults_single_ccd(self, tmp_path):
        """When L3 cache sysfs is entirely absent, ccds defaults to 1."""
        topo = parse_cpuinfo_from_text(CPUINFO_DUAL_CCD_SMT)
        cpu_dir = tmp_path / "cpu"
        cpu_dir.mkdir()
        (cpu_dir / "online").write_text("0-15")

        with patch("corecycler.engine.topology.SYSFS_CPU", cpu_dir):
            _detect_ccd_layout(topo)

        assert topo.ccds == 1

    def test_no_sysfs_cpu_dir(self, tmp_path):
        """When SYSFS_CPU doesn't exist, parsing does not crash."""
        topo = parse_cpuinfo_from_text(CPUINFO_DUAL_CCD_SMT)
        fake_dir = tmp_path / "nonexistent"

        with patch("corecycler.engine.topology.SYSFS_CPU", fake_dir):
            _parse_sysfs(topo)
            _detect_ccd_layout(topo)

        assert topo.ccds == 1

    def test_missing_core_id_in_cpuinfo(self):
        """cpuinfo with processor but no core_id should not crash."""
        text = (
            "processor\t: 0\n"
            "vendor_id\t: AuthenticAMD\n"
            "cpu family\t: 26\n"
            "model\t\t: 68\n"
            "model name\t: AMD Ryzen 9 9950X3D\n"
            "\n"
        )
        topo = parse_cpuinfo_from_text(text)
        assert topo.physical_cores == 0

    def test_harvested_cpu_core_ids_not_sequential(self):
        """Harvested CPU: core IDs skip numbers (0,1,2,3,4,5 + 8,9,10,11,12,13)."""
        from tests.conftest import CPUINFO_ZEN5_9900X_HARVESTED

        topo = parse_cpuinfo_from_text(CPUINFO_ZEN5_9900X_HARVESTED)
        assert topo.physical_cores == 12
        assert topo.family == 26
        assert "9900X" in topo.model_name


class TestTopologyDriftEdges:
    def test_malformed_online_range_entries_skipped(self, tmp_path):
        """A malformed cpu online entry (non-numeric single or range bound) is
        skipped without crashing; valid entries still count."""
        cpu_dir = tmp_path / "cpu"
        cpu_dir.mkdir()
        (cpu_dir / "online").write_text("0-3,bad,9-z,5\n")
        topo = CPUTopology()
        with patch("corecycler.engine.topology.SYSFS_CPU", cpu_dir):
            _parse_sysfs(topo)
        assert topo.logical_cpus_count == 5

    def test_x3d_multi_ccd_skips_none_ccd_and_missing_cache(self, tmp_path):
        """A core with no CCD assignment and a core whose cache sysfs is absent
        are skipped; the CCD whose L3 proves V-Cache is still marked."""
        topo = CPUTopology(model_name="AMD Ryzen 9 7950X3D 16-Core Processor", ccds=2)
        topo.cores = {
            0: PhysicalCore(core_id=0, ccd=0, logical_cpus=(0,)),
            1: PhysicalCore(core_id=1, ccd=None, logical_cpus=(1,)),
            8: PhysicalCore(core_id=8, ccd=1, logical_cpus=(8,)),
        }
        cpu_dir = tmp_path / "cpu"
        cache_dir = cpu_dir / "cpu0" / "cache" / "index3"
        cache_dir.mkdir(parents=True)
        (cache_dir / "level").write_text("3")
        (cache_dir / "size").write_text("96M")
        with patch("corecycler.engine.topology.SYSFS_CPU", cpu_dir):
            _detect_x3d(topo)
        assert topo.is_x3d is True
        assert [pc.has_vcache for pc in topo.cores.values()] == [True, False, False]


class TestCpusAllOnline:
    def _run(self, tmp_path, online, present):
        cpu_dir = tmp_path / "cpu"
        cpu_dir.mkdir()
        (cpu_dir / "online").write_text(online)
        if present is not None:
            (cpu_dir / "present").write_text(present)
        topo = CPUTopology()
        with patch("corecycler.engine.topology.SYSFS_CPU", cpu_dir):
            _parse_sysfs(topo)
        return topo

    def test_all_online_when_sets_match(self, tmp_path):
        topo = self._run(tmp_path, "0-15\n", "0-15\n")
        assert topo.cpus_all_online is True

    def test_offline_cpu_detected(self, tmp_path):
        topo = self._run(tmp_path, "0-3,6-15\n", "0-15\n")
        assert topo.cpus_all_online is False

    def test_nosmt_pattern_detected_as_offline(self, tmp_path):
        topo = self._run(tmp_path, "0,2,4,6\n", "0-7\n")
        assert topo.cpus_all_online is False

    def test_missing_present_file_leaves_proof_unknown(self, tmp_path):
        topo = self._run(tmp_path, "0-7\n", None)
        assert topo.cpus_all_online is None

    @pytest.mark.parametrize("reads", [(OSError(),), ("0-7", OSError())])
    def test_unreadable_online_or_present_file_leaves_proof_unknown(self, tmp_path, reads):
        cpu_dir = tmp_path / "cpu"
        cpu_dir.mkdir()
        (cpu_dir / "online").touch()
        (cpu_dir / "present").touch()
        topo = CPUTopology()
        with (
            patch("corecycler.engine.topology.SYSFS_CPU", cpu_dir),
            patch.object(Path, "read_text", side_effect=reads),
        ):
            _parse_sysfs(topo)
        assert topo.cpus_all_online is None
