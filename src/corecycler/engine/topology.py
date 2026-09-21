"""CPU topology detection — cores, CCDs, CCXs, SMT, X3D V-Cache identification."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path

CPUINFO = Path("/proc/cpuinfo")
SYSFS_CPU = Path("/sys/devices/system/cpu")


@dataclass(frozen=True, slots=True)
class LogicalCPU:
    logical_id: int
    physical_core: int
    package_id: int
    core_cpus: tuple[int, ...]  # all logical CPUs sharing this physical core (SMT siblings)


@dataclass(frozen=True, slots=True)
class PhysicalCore:
    core_id: int
    ccd: int | None
    ccx: int | None
    logical_cpus: tuple[int, ...]
    has_vcache: bool = False


@dataclass(slots=True)
class CPUTopology:
    model_name: str = ""
    vendor: str = ""
    family: int = 0
    model: int = 0
    stepping: int = 0
    physical_cores: int = 0
    logical_cpus_count: int = 0
    smt_enabled: bool = False
    ccds: int = 0
    is_x3d: bool = False
    # False when a present CPU is offline. A fully-offlined core vanishes from
    # /proc/cpuinfo and fakes a hole in the core-id space, so gap-based
    # physical-numbering proofs are only trustworthy when this is True.
    cpus_all_online: bool = True
    cores: dict[int, PhysicalCore] = field(default_factory=dict)
    logical_map: dict[int, LogicalCPU] = field(default_factory=dict)


def detect_topology() -> CPUTopology:
    topo = CPUTopology()
    _parse_cpuinfo(topo)
    _parse_sysfs(topo)
    _detect_ccd_layout(topo)
    _detect_x3d(topo)
    return topo


def _field_int(line: str) -> int | None:
    """Extract the integer after the colon in a "key: value" line.

    Returns None for a missing colon or a non-numeric value, so a malformed or
    non-x86 /proc/cpuinfo line is skipped rather than crashing topology detection.
    """
    parts = line.split(":", 1)
    if len(parts) < 2:
        return None
    try:
        return int(parts[1].strip())
    except ValueError:
        return None


def _l3_id_sort_key(item: tuple[str, list[int]]) -> tuple[int, int, str]:
    """Order L3 groups by the numeric id from the sysfs cache `id` file.

    A plain integer sorts by value (the normal case). A malformed, empty, or
    non-decimal id (a transient zero-byte read, a hex string) sorts last by its
    string form instead of crashing CCD detection with ValueError on int().
    """
    l3_id = item[0]
    if l3_id.lstrip("-").isdigit():
        return (0, int(l3_id), "")
    return (1, 0, l3_id)


def _parse_cpuinfo(topo: CPUTopology) -> None:
    if not CPUINFO.exists():
        return
    text = CPUINFO.read_text()

    cores_seen: dict[int, list[int]] = {}  # physical_core -> [logical_ids]
    current_proc = -1
    current_core = -1
    current_pkg = 0

    for line in text.splitlines():
        if line.startswith("processor"):
            v = _field_int(line)
            if v is not None:
                current_proc = v
        elif line.startswith("core id"):
            v = _field_int(line)
            if v is not None:
                current_core = v
        elif line.startswith("physical id"):
            v = _field_int(line)
            if v is not None:
                current_pkg = v
        elif line.startswith("model name") and not topo.model_name:
            parts = line.split(":", 1)
            if len(parts) > 1:
                topo.model_name = parts[1].strip()
        elif line.startswith("vendor_id") and not topo.vendor:
            parts = line.split(":", 1)
            if len(parts) > 1:
                topo.vendor = parts[1].strip()
        elif line.startswith("cpu family") and topo.family == 0:
            topo.family = _field_int(line) or 0
        elif line.startswith("model\t") and topo.model == 0:
            topo.model = _field_int(line) or 0
        elif line.startswith("stepping") and topo.stepping == 0:
            topo.stepping = _field_int(line) or 0
        elif line == "":
            if current_proc >= 0 and current_core >= 0:
                cores_seen.setdefault(current_core, []).append(current_proc)
                topo.logical_map[current_proc] = LogicalCPU(
                    logical_id=current_proc,
                    physical_core=current_core,
                    package_id=current_pkg,
                    core_cpus=(),  # filled later
                )
            current_proc = -1
            current_core = -1

    # handle last entry (no trailing blank line)
    if current_proc >= 0 and current_core >= 0:
        cores_seen.setdefault(current_core, []).append(current_proc)
        topo.logical_map[current_proc] = LogicalCPU(
            logical_id=current_proc,
            physical_core=current_core,
            package_id=current_pkg,
            core_cpus=(),
        )

    topo.physical_cores = len(cores_seen)
    topo.logical_cpus_count = sum(len(v) for v in cores_seen.values())
    topo.smt_enabled = any(len(v) > 1 for v in cores_seen.values())

    # backfill core_cpus tuples
    for logical_id, lcpu in list(topo.logical_map.items()):
        siblings = tuple(sorted(cores_seen.get(lcpu.physical_core, [logical_id])))
        topo.logical_map[logical_id] = LogicalCPU(
            logical_id=lcpu.logical_id,
            physical_core=lcpu.physical_core,
            package_id=lcpu.package_id,
            core_cpus=siblings,
        )


def _parse_cpu_ranges(text: str) -> set[int]:
    """Parse a sysfs CPU list ("0-15,32-47") into a set of CPU ids.

    A malformed part is skipped rather than crashing, so a transient bad read
    degrades to a smaller set instead of taking topology detection down.
    """
    cpus: set[int] = set()
    for part in text.strip().split(","):
        try:
            if "-" in part:
                lo, hi = part.split("-", 1)
                cpus.update(range(int(lo), int(hi) + 1))
            elif part:
                cpus.add(int(part))
        except ValueError:
            continue
    return cpus


def _parse_sysfs(topo: CPUTopology) -> None:
    """Read sysfs for additional topology info (online/present status)."""
    if not SYSFS_CPU.exists():
        return
    online_path = SYSFS_CPU / "online"
    present_path = SYSFS_CPU / "present"
    online: set[int] | None = None
    if online_path.exists():
        online = _parse_cpu_ranges(online_path.read_text())
        if topo.logical_cpus_count == 0 and online:
            topo.logical_cpus_count = len(online)
    if online is not None and present_path.exists():
        present = _parse_cpu_ranges(present_path.read_text())
        if present and online != present:
            topo.cpus_all_online = False


def _l3_cache_dir(logical_cpu: int) -> Path | None:
    cache_dir = SYSFS_CPU / f"cpu{logical_cpu}" / "cache"
    if not cache_dir.exists():
        return None
    for idx_dir in sorted(cache_dir.iterdir()):
        level_file = idx_dir / "level"
        if level_file.exists() and level_file.read_text().strip() == "3":
            return idx_dir
    return None


_L3_UNIT_KIB = {"K": 1, "M": 1024, "G": 1048576}
# A stacked V-Cache die adds 64 MiB to the 32 MiB a Zen 3/4/5 CCD carries on
# its own, so any CCD at or above this is carrying one.
_VCACHE_L3_MIN_KIB = 64 * 1024


def _l3_size_kib(idx_dir: Path) -> int | None:
    size_file = idx_dir / "size"
    if not size_file.exists():
        return None
    m = re.match(r"(\d+)([KMG])?", size_file.read_text().strip())
    if not m:
        return None
    return int(m.group(1)) * _L3_UNIT_KIB[m.group(2) or "K"]


def _detect_ccd_layout(topo: CPUTopology) -> None:
    """Detect CCD assignment for each core using L3 cache topology."""
    l3_groups: dict[str, list[int]] = {}  # l3_id -> [core_ids]

    for lcpu in topo.logical_map.values():
        if lcpu.logical_id != min(lcpu.core_cpus):
            continue
        idx_dir = _l3_cache_dir(lcpu.logical_id)
        if idx_dir is None:
            continue
        id_file = idx_dir / "id"
        if id_file.exists():
            l3_groups.setdefault(id_file.read_text().strip(), []).append(lcpu.physical_core)

    ccd_map: dict[int, int] = {}  # physical_core -> ccd_index
    for ccd_idx, (_l3_id, core_ids) in enumerate(sorted(l3_groups.items(), key=_l3_id_sort_key)):
        for cid in core_ids:
            ccd_map[cid] = ccd_idx

    topo.ccds = len(l3_groups) if l3_groups else 1

    for lcpu in topo.logical_map.values():
        pc = lcpu.physical_core
        if pc not in topo.cores:
            topo.cores[pc] = PhysicalCore(core_id=pc, ccd=ccd_map.get(pc), ccx=None, logical_cpus=lcpu.core_cpus)


def _detect_x3d(topo: CPUTopology) -> None:
    """Mark every core on a CCD that carries stacked V-Cache.

    Decided per CCD from its L3 size, so a part with V-Cache on both CCDs
    (9950X3D2) marks both. A single-CCD X3D whose sysfs exposes no cache
    size still has its one CCD marked: the name alone proves it.
    """
    topo.is_x3d = "x3d" in topo.model_name.lower()
    if not topo.is_x3d:
        return

    vcache_ccds: set[int] = set()
    seen_ccds: set[int] = set()
    for core in topo.cores.values():
        if core.ccd is None or core.ccd in seen_ccds:
            continue
        seen_ccds.add(core.ccd)
        idx_dir = _l3_cache_dir(core.logical_cpus[0])
        size_kib = _l3_size_kib(idx_dir) if idx_dir is not None else None
        if size_kib is not None and size_kib >= _VCACHE_L3_MIN_KIB:
            vcache_ccds.add(core.ccd)

    whole_part = not vcache_ccds and topo.ccds == 1
    for core_id, core in topo.cores.items():
        if whole_part or core.ccd in vcache_ccds:
            topo.cores[core_id] = replace(core, has_vcache=True)


def get_first_logical_cpu(topo: CPUTopology, physical_core: int) -> int:
    """Get the first (non-SMT) logical CPU for a physical core."""
    core = topo.cores.get(physical_core)
    if core and core.logical_cpus:
        return core.logical_cpus[0]
    return physical_core


def get_physical_core_list(topo: CPUTopology) -> list[int]:
    """Get sorted list of physical core IDs."""
    return sorted(topo.cores.keys())
