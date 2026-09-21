"""Ring B live contract tests -- run the REAL hardware or tools and assert our
readers/parsers still match reality. Marked slow + contract; the nix sandbox
deselects `slow` so it never runs them here. Run them on real AMD hardware:
`CORECYCLER_HW_CONTRACTS=1 pytest -m contract`, where an absent resource fails
loud instead of skipping green. MSR (CAP_SYS_RAWIO), dmidecode (root) and the
SMU mailbox (corecycler group) skip unless the run also holds those rights;
add CORECYCLER_HW_PRIVILEGED=1 as root to make them fatal too.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from corecycler.monitor.memory import parse_dmidecode_output
from corecycler.monitor.msr import MSRReader
from corecycler.smu.commands import CPUGeneration, detect_generation, get_commands

sys.path.insert(0, str(Path(__file__).parent))

from _contract_hw import require, require_privileged

pytestmark = [pytest.mark.slow, pytest.mark.contract]


def _read_cpuinfo() -> tuple[int, int, str] | None:
    p = Path("/proc/cpuinfo")
    if not p.exists():
        return None
    family: int | None = None
    model: int | None = None
    name = ""
    for line in p.read_text().splitlines():
        if line.startswith("cpu family") and family is None:
            family = int(line.split(":")[1])
        elif line.startswith("model name"):
            if not name:
                name = line.split(":", 1)[1].strip()
        elif line.startswith("model") and model is None:
            model = int(line.split(":")[1])
    if family is None or model is None:
        return None
    return family, model, name


def _is_amd_zen() -> bool:
    info = _read_cpuinfo()
    return info is not None and info[0] in (23, 25, 26)


def test_real_cpu_resolves_to_supported_generation():
    require(_is_amd_zen(), "requires a real AMD Zen CPU")
    info = _read_cpuinfo()
    assert info is not None
    family, model, name = info
    gen = detect_generation(family, model, name)
    assert gen is not CPUGeneration.UNKNOWN, f"unmapped CPU family={family} model={model} name={name!r}"
    assert get_commands(gen) is not None


def test_msr_reads_are_plausible():
    reader = MSRReader()
    require_privileged(
        reader.is_available(),
        "requires CAP_SYS_RAWIO on /dev/cpu/0/msr: the kernel gates msr_open() on the "
        "capability, so a udev group grant alone opens the file mode but still EPERMs",
    )
    unit = reader._get_energy_unit()
    assert unit is not None and 1e-6 < unit < 1e-2, f"implausible RAPL energy unit {unit}"
    reader.read_clock_stretch([0])
    time.sleep(0.1)
    stretch = reader.read_clock_stretch([0])
    reader.close()
    if 0 in stretch:
        assert 0.0 < stretch[0].ratio <= 1.6, f"implausible APERF/MPERF ratio {stretch[0].ratio}"


def test_dmidecode_parses_real_dimms():
    require(shutil.which("dmidecode") is not None, "requires dmidecode")
    result = subprocess.run(["dmidecode", "-t", "memory"], capture_output=True, text=True, timeout=15)
    require_privileged(result.returncode == 0 and bool(result.stdout), "dmidecode -t memory needs root")
    dimms = parse_dmidecode_output(result.stdout)
    assert len(dimms) >= 1, "no DIMMs parsed from real dmidecode output"
    assert all(d.size_gb > 0 for d in dimms)


def test_proc_stat_cpu_line_matches_the_pinned_fields():
    """The stall watchdog reads idle+iowait from /proc/stat; drift here blinds it."""
    from corecycler.engine.execution import cpu_times as _cpu_times

    require(Path("/proc/stat").exists(), "/proc/stat not readable")
    sample = _cpu_times(0)
    require(sample is not None, "no cpu0 line in /proc/stat")
    idle, total = sample
    assert 0 < idle < total
    fields = next(line.split() for line in Path("/proc/stat").read_text().splitlines() if line.startswith("cpu0 "))
    assert len(fields) >= 6
    assert idle == int(fields[4]) + int(fields[5])


def test_core_slot_map_matches_the_live_core_disable_fuse():
    """The mapping's ground truth, on real silicon: this generation's
    core-disable fuse address decodes to exactly the physical slots the
    discovered map uses, per CCD. On a machine whose numbering proves itself
    the map comes from the core ids and the fuse is read independently, so a
    wrong fuse address for the die fails here rather than silently shipping."""
    require(_is_amd_zen(), "requires a real AMD Zen CPU")
    from corecycler.engine.topology import detect_topology
    from corecycler.smu.driver import RyzenSMU

    info = _read_cpuinfo()
    assert info is not None
    commands = get_commands(detect_generation(*info))
    require(commands is not None and commands.has_co, "requires a CO-capable generation")
    require(commands.uniform_8core_ccds, "requires a classic 8-slot-per-CCD die")
    require(
        commands.core_fuse_addr is not None,
        "requires a verified core-disable fuse address for this die",
    )
    require(RyzenSMU.is_available(), "requires the ryzen_smu module")
    smu = RyzenSMU(commands)
    smn_ok, smn_msg = smu.check_smn_readable()
    require_privileged(smn_ok, f"requires SMN access: {smn_msg}")
    topo = detect_topology()
    smu.set_topology(topo)
    assert smu.core_map_error is None, smu.core_map_error
    core_map = smu.core_map
    assert core_map is not None
    assert set(core_map) == set(topo.cores)
    per_ccd: dict[int, list[int]] = {}
    for _core_id, (ccd, slot) in sorted(core_map.items()):
        per_ccd.setdefault(ccd, []).append(slot)
    for ccd, mapped_slots in per_ccd.items():
        fuse = smu.read_smn(commands.core_fuse_addr + (ccd << 25))
        assert fuse is not None, f"CCD {ccd} core-disable fuse unreadable"
        live = [s for s in range(8) if not (fuse >> s) & 1]
        assert live == mapped_slots, (ccd, hex(fuse), live, mapped_slots)


def test_co_read_answers_on_every_slot_so_it_cannot_find_fused_off_cores():
    """Issue #11's falsified premise, pinned against real silicon: the CO read
    is NOT a liveness probe. Every in-range slot of a CCD answers it, so a
    harvested CCD cannot be resolved by asking the mailbox -- only by the
    fuse. If a die ever did discriminate here this fails and says so."""
    require(_is_amd_zen(), "requires a real AMD Zen CPU")
    from corecycler.engine.topology import detect_topology
    from corecycler.smu.commands import encode_co_arg
    from corecycler.smu.driver import RyzenSMU

    info = _read_cpuinfo()
    assert info is not None
    generation = detect_generation(*info)
    commands = get_commands(generation)
    require(commands is not None and commands.has_co, "requires a CO-capable generation")
    require(commands.uniform_8core_ccds, "requires a classic 8-slot-per-CCD die")
    require(RyzenSMU.is_available(), "requires the ryzen_smu module")
    smu = RyzenSMU(commands)
    require_privileged(smu.check_writable()[0], "requires ryzen_smu mailbox access")
    ccds = {c.ccd for c in detect_topology().cores.values() if c.ccd is not None}
    require(bool(ccds), "requires L3-detected CCDs")
    for ccd in sorted(ccds):
        answered = [
            slot for slot in range(8) if smu._send_get_co(encode_co_arg(0, 0, generation, ccd=ccd, slot=slot)).success
        ]
        assert answered == list(range(8)), (ccd, answered)


def _l3_size_kib(logical_cpu: int) -> int:
    cache_dir = Path(f"/sys/devices/system/cpu/cpu{logical_cpu}/cache")
    for index in cache_dir.glob("index*"):
        if (index / "level").read_text().strip() != "3":
            continue
        raw = (index / "size").read_text().strip().upper()
        multiplier = {"K": 1, "M": 1024, "G": 1024 * 1024}[raw[-1]]
        return int(raw[:-1]) * multiplier
    raise AssertionError(f"no L3 cache for logical CPU {logical_cpu}")


def test_9950x3d2_dual_vcache_layout():
    info = _read_cpuinfo()
    require(info is not None and "9950X3D2" in info[2], "requires a Ryzen 9 9950X3D2")
    assert info is not None
    assert info[:2] == (0x1A, 0x44)

    from corecycler.engine.topology import detect_topology

    topology = detect_topology()
    assert topology.physical_cores == 16
    assert topology.logical_cpus_count == 32
    assert topology.ccds == 2
    assert topology.is_x3d
    per_ccd = {ccd: [core for core in topology.cores.values() if core.ccd == ccd] for ccd in range(2)}
    assert {ccd: len(cores) for ccd, cores in per_ccd.items()} == {0: 8, 1: 8}
    assert all(core.has_vcache for cores in per_ccd.values() for core in cores)
    l3_sizes = {
        ccd: _l3_size_kib(min(cores, key=lambda core: core.core_id).logical_cpus[0]) for ccd, cores in per_ccd.items()
    }
    assert l3_sizes == {0: 96 * 1024, 1: 96 * 1024}


def test_ryzen_smu_sysfs_exposes_required_nodes():
    info = _read_cpuinfo()
    require(info is not None and _is_amd_zen(), "requires a real AMD Zen CPU")
    assert info is not None
    commands = get_commands(detect_generation(*info))
    require(commands is not None, "requires a supported ryzen_smu generation")

    from corecycler.smu.driver import SYSFS_BASE

    require(SYSFS_BASE.is_dir(), "requires the ryzen_smu module")
    nodes = {path.name for path in SYSFS_BASE.iterdir()}
    command_node = "mp1_smu_cmd" if commands.mailbox == "mp1" else "rsmu_cmd"
    required_nodes = {"smu_args", "smn", command_node}
    if commands.get_co_mailbox == "rsmu":
        required_nodes.add("rsmu_cmd")
    assert required_nodes <= nodes
    pm_nodes = {"pm_table", "pm_table_version", "pm_table_size"}
    present_pm_nodes = pm_nodes & nodes
    assert not present_pm_nodes or present_pm_nodes == pm_nodes


def test_granite_ridge_core_disable_fuse_matches_live_slots():
    info = _read_cpuinfo()
    require(info is not None and info[:2] == (0x1A, 0x44), "requires Granite Ridge family 0x1A model 0x44")
    assert info is not None
    commands = get_commands(CPUGeneration.ZEN5_GRANITE_RIDGE)
    assert commands is not None and commands.core_fuse_addr == 0x304A03DC

    from corecycler.engine.topology import detect_topology
    from corecycler.smu.driver import RyzenSMU

    require(RyzenSMU.is_available(), "requires the ryzen_smu module")
    smu = RyzenSMU(commands)
    smn_ok, smn_message = smu.check_smn_readable()
    require_privileged(smn_ok, f"requires SMN access: {smn_message}")
    topology = detect_topology()
    assert topology.physical_cores == 16 and topology.ccds == 2
    smu.set_topology(topology)
    assert smu.core_map_error is None
    per_ccd: dict[int, list[int]] = {0: [], 1: []}
    for ccd, slot in smu.core_map.values():
        per_ccd[ccd].append(slot)
    for ccd in (0, 1):
        fuse = smu.read_smn(0x304A03DC + (ccd << 25))
        assert fuse is not None
        live_slots = [slot for slot in range(8) if not (fuse >> slot) & 1]
        assert sorted(per_ccd[ccd]) == live_slots
