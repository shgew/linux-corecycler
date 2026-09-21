"""Interface to the ryzen_smu kernel module via sysfs.

IMPORTANT: CO (Curve Optimizer) values written via ryzen_smu are VOLATILE.
They are stored in SMU firmware SRAM and reset to zero on every reboot,
S3 sleep, or driver reload. Your BIOS PBO Curve Optimizer settings are
never modified by this tool — BIOS values are applied by firmware during
POST, and SMU writes here overlay (replace) them until the next power cycle.
"""

from __future__ import annotations

import contextlib
import logging
import os
import struct
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .commands import (
    CPUGeneration,
    SMUCommandSet,
    decode_co_arg,
    encode_boost_limit_arg,
    encode_co_arg,
    encode_pbo_limit_arg,
    encode_pbo_scalar_arg,
)

log = logging.getLogger(__name__)

SYSFS_BASE = Path("/sys/kernel/ryzen_smu_drv")

# Sane bounds for PBO power/current limits (PPT watts, TDC/EDC amps). The firmware
# enforces the true per-chip cap; this only rejects the unambiguously malformed:
# non-positive (a negative wraps to a huge unsigned limit once encoded as value*1000,
# effectively removing the cap) or absurdly large (a caller/UI bug). No real or
# near-future AMD CPU approaches 2000 W / 2000 A.
_PBO_LIMIT_MIN: int = 1
_PBO_LIMIT_MAX: int = 2000

# Physical core slots per CCD on every uniform layout this driver models.
# A denser L3 group is outside that verified model and blocks per-core CO.
_SLOTS_PER_CCD: int = 8

# CCD stride inside the core-disable fuse address space: CCD n's fuse sits at
# ``core_fuse_addr + (n << 25)`` (ZenStates-Core Cpu.cs, ryzen_monitor).
_CCD_FUSE_SHIFT: int = 25


def _check_pbo_limit(name: str, value: int) -> None:
    """Fail closed on a malformed PBO limit before it is encoded and written."""
    if not _PBO_LIMIT_MIN <= value <= _PBO_LIMIT_MAX:
        raise ValueError(f"{name} limit {value} out of sane range [{_PBO_LIMIT_MIN}, {_PBO_LIMIT_MAX}]")


class CoreMapError(Exception):
    """The OS-core to SMU-slot mapping could not be discovered.

    Raised internally during ``RyzenSMU.set_topology`` and stored as
    ``core_map_error``; per-core CO operations then refuse instead of
    addressing slots that may belong to different or fused-off cores.
    """


def core_map_blocked(smu) -> str | None:
    """The SMU's core-map failure reason, or None when per-core CO is usable.

    Accepts None and duck-typed test doubles: only a real, non-empty ``str``
    (the type of ``RyzenSMU.core_map_error``) blocks.
    """
    err = getattr(smu, "core_map_error", None)
    if isinstance(err, str) and err:
        return err
    return None


@dataclass(frozen=True, slots=True)
class SMUResponse:
    success: bool
    args: tuple[int, ...]
    raw: bytes


@dataclass(slots=True)
class SystemPBOState:
    """Snapshot of the current PBO/CO state from SMU and sysfs.

    Populated by ``RyzenSMU.detect_system_state()``. All values are
    runtime state — they reflect BIOS settings plus any SMU overrides.
    """

    # Per-core CO offsets (physical core id -> offset)
    co_offsets: dict[int, int | None] = field(default_factory=dict)

    # PBO power limits (from PM table or SMU query)
    ppt_limit_w: float | None = None
    tdc_limit_a: float | None = None
    edc_limit_a: float | None = None

    # PBO scalar (1.0 to 10.0)
    pbo_scalar: float | None = None

    # Boost frequency limit (MHz)
    boost_limit_mhz: int | None = None

    # Max observed frequency from cpufreq (accounts for boost override + BCLK)
    max_freq_mhz: float | None = None

    # Whether OC mode is enabled
    oc_mode: bool | None = None

    # Fastest core index (from SMU)
    fastest_core: int | None = None

    # CPU generation detected
    generation: CPUGeneration | None = None

    # Whether ryzen_smu driver is available
    smu_available: bool = False


class RyzenSMU:
    """Interface to the ryzen_smu kernel module for reading/writing CO offsets.

    CO offsets set through this driver are VOLATILE — they live in SMU firmware
    SRAM and are lost on reboot, sleep, or driver reload. BIOS PBO settings
    are never touched.

    Safety features:
      - ``dry_run`` mode: logs intended writes without touching hardware
      - Read-back verification after every CO write
      - ``backup_co_offsets()`` / ``restore_co_offsets()`` for save/restore
      - Permission pre-check before attempting writes
    """

    def __init__(
        self,
        commands: SMUCommandSet,
        sysfs_path: Path = SYSFS_BASE,
        dry_run: bool = False,
    ) -> None:
        self.commands = commands
        self.sysfs = sysfs_path
        self.dry_run = dry_run
        self._smu_lock = threading.Lock()
        self._backup: dict[int, int | None] | None = None
        # Legacy CCD map {core_id: ccd_index} from L3 topology. Uniform
        # eight-slot generations replace it with the verified _core_map.
        # Unmodelled generations retain legacy core-id-derived addressing.
        self._topology_ccd: dict[int, int] | None = None
        # Discovered addressing map {core_id: (ccd_index, physical_slot)} and
        # its fail-closed error state. See set_topology.
        self._core_map: dict[int, tuple[int, int]] | None = None
        self._core_map_error: str | None = None
        self._known_core_ids: list[int] | None = None

    def set_topology(self, topology) -> None:
        """Discover how OS core ids map onto SMU (CCD, physical slot) addresses.

        The SMU addresses cores by physical slot within a CCD, counting
        fused-off cores. The kernel's /proc/cpuinfo "core id" (the APIC-ID
        decode from CPUID Fn8000_001E) is that physical numbering on most
        machines -- a fused-off core leaves a hole -- but some BIOS/AGESA
        builds renumber core ids contiguously on harvested parts instead
        (field-reported on a 5600X, issue #11), and the ids alone then hide
        which slots are fused off.

        Mapping rules, per L3 group (falling back to ``core_id // 8`` grouping
        when L3 info is absent):
          - A full 8-core group maps to identity slots under any numbering
            scheme -- no SMU traffic.
          - A group with a hole INTERNAL to its own 8-slot window proves the
            physical numbering: slot = ``core_id % 8``, CCD from L3. Only
            internal holes count -- a hole BETWEEN windows proves nothing,
            because a per-CCD-compacting firmware keeps the window stride --
            and only while every present CPU is online, because a
            fully-offlined core fakes a hole.
          - Every other harvested group is ambiguous (its slots form the
            0..n-1 prefix, it spans windows, or its holes are untrusted): the
            CCD's core-disable fuse is read over SMN and the group's cores map
            onto the live slots in ascending order (the same order-preserving
            mapping the Windows tools build from that fuse). The CO read
            cannot stand in for it -- it answers on every in-range slot,
            fused-off ones included (issue #11).
          - A fuse that cannot be read or disagrees with the OS core count stores
            ``core_map_error`` and every per-core CO operation refuses --
            failing closed beats tuning a different core than the one under
            test. While discovery runs, ``core_map_error`` holds a sentinel so
            a concurrent reader refuses rather than falling open to legacy
            addressing.

        Discovery runs only for generations declaring ``uniform_8core_ccds``
        and refuses an L3 group larger than the verified eight-slot layout.
        Generations without that declaration retain legacy core-id addressing.
        """
        self._topology_ccd = {}
        for core_id, core_info in topology.cores.items():
            if core_info.ccd is not None:
                self._topology_ccd[core_id] = core_info.ccd
        self._core_map = None
        self._core_map_error = "core map discovery in progress"
        ids = sorted(topology.cores)
        self._known_core_ids = ids or None
        if not ids or not self.commands.has_co or not self.commands.uniform_8core_ccds:
            self._core_map_error = None
            return
        if getattr(topology, "ccd_layout_known", True) is False:
            self._core_map_error = "CCD layout is unproven; per-core CO stays disabled"
            return
        try:
            groups = self._group_cores(topology, ids)
        except CoreMapError as exc:
            self._core_map_error = str(exc)
            log.error("SMU core mapping unavailable: %s", exc)
            return
        online_state = getattr(topology, "cpus_all_online", None)
        if online_state is not True:
            state = "some CPUs are offline" if online_state is False else "online CPU state is unproven"
            self._core_map_error = f"{state}; online all cores before CO tuning"
            return
        core_map: dict[int, tuple[int, int]] = {}
        try:
            for encode_ccd in sorted(groups):
                group_ids = groups[encode_ccd]
                slots = self._derive_group_slots(group_ids, True)
                if slots is None:
                    slots = self._fuse_group_slots(encode_ccd, len(group_ids))
                core_map.update((cid, (encode_ccd, slot)) for cid, slot in zip(group_ids, slots, strict=True))
        except CoreMapError as exc:
            self._core_map_error = str(exc)
            log.error("SMU core mapping unavailable: %s", exc)
            return
        self._core_map = core_map
        self._core_map_error = None

    @staticmethod
    def _derive_group_slots(group_ids: list[int], holes_trusted: bool) -> list[int] | None:
        """Slots this group's numbering proves by itself; None means read the fuse.

        A full 8-core group is identity under any numbering scheme. Only a
        hole INTERNAL to the group's own 8-slot window proves physical
        numbering, and only while every present CPU is online (an offlined
        core fakes a hole). A 0..n-1 prefix is ambiguous, and a group spanning
        windows is renumbered -- a per-CCD-compacting firmware keeps the
        window stride, so a hole BETWEEN windows proves nothing.
        """
        windows = {cid // _SLOTS_PER_CCD for cid in group_ids}
        if len(windows) != 1:
            return None
        rel = [cid % _SLOTS_PER_CCD for cid in group_ids]
        if rel == list(range(len(group_ids))):
            return rel if len(group_ids) == _SLOTS_PER_CCD else None
        return rel if holes_trusted else None

    @staticmethod
    def _group_cores(topology, ids: list[int]) -> dict[int, list[int]]:
        """Group core ids by encode-CCD index and reject unmodelled layouts."""
        by_l3 = all(topology.cores[cid].ccd is not None for cid in ids)
        groups: dict[int, list[int]] = {}
        for cid in ids:
            key = topology.cores[cid].ccd if by_l3 else cid // _SLOTS_PER_CCD
            groups.setdefault(key, []).append(cid)
        largest = max(len(members) for members in groups.values())
        if largest > _SLOTS_PER_CCD:
            raise CoreMapError(
                f"detected L3 group contains {largest} cores, exceeding the verified eight-slot CCD layout; "
                "per-core CO stays disabled"
            )
        return groups

    def check_smn_readable(self) -> tuple[bool, str]:
        """Whether the SMN node can be driven at all. Returns (ok, message).

        Reading an SMN register means WRITING its address to ``smn`` and
        reading the result back, so an SMN read needs write permission on a
        file ryzen_smu ships root-only.
        """
        path = self.sysfs / "smn"
        if not path.exists():
            return False, f"sysfs file not found: {path}"
        if not os.access(path, os.W_OK):
            return False, (
                f"no write permission on {path} — an SMN read is a write of the "
                f"address, and ryzen_smu ships this file root-only; grant it to "
                f"the corecycler group like the other SMU files, or run as root"
            )
        return True, "OK"

    def read_smn(self, address: int) -> int | None:
        """Read one 32-bit SMN register, or None unless the transfer is exact."""
        path = self.sysfs / "smn"
        request = struct.pack("<I", address)
        with self._smu_lock:
            try:
                if path.write_bytes(request) != len(request):
                    raise OSError("truncated SMN address write")
                reply = path.read_bytes()
                if len(reply) != 4:
                    raise OSError("invalid SMN response length")
                return struct.unpack("<I", reply)[0]
            except (OSError, struct.error) as exc:
                log.debug("SMN read of %#010x failed: %s", address, exc)
                return None

    def _fuse_group_slots(self, encode_ccd: int, want: int) -> list[int]:
        """This CCD's live physical slots, from its SMN core-disable fuse.

        The fuse is the SMU's own record of which of the 8 slots exist (a set
        bit is a fused-off slot), and the only thing that resolves a renumbered
        core-id space -- the CO read answers on every in-range slot and so
        proves nothing (issue #11). Returns the live slots ascending when their
        count matches the ``want`` cores the OS reports for this CCD; raises
        CoreMapError, naming what to do about it, otherwise.
        """
        addr = self.commands.core_fuse_addr
        if addr is None:
            raise CoreMapError(
                f"core ids on this CPU are renumbered and no core-disable fuse "
                f"address is verified for {self.commands.generation.name}, so "
                f"the fused-off slots cannot be located -- per-core CO stays "
                f"disabled instead of writing to the wrong cores; please report "
                "this output"
            )
        ok, msg = self.check_smn_readable()
        if not ok:
            raise CoreMapError(
                f"core ids on this CPU are renumbered, so the SMU core-disable "
                f"fuse has to be read to find the fused-off slots: {msg} -- "
                f"per-core CO stays disabled until then"
            )
        fuse = self.read_smn(addr + (encode_ccd << _CCD_FUSE_SHIFT))
        if fuse is None:
            raise CoreMapError(
                f"core ids on this CPU are renumbered and the CCD-{encode_ccd} "
                f"core-disable fuse read failed on an otherwise writable smn "
                f"file -- per-core CO stays disabled instead of writing to the "
                f"wrong cores; please report this output"
            )
        live = [slot for slot in range(_SLOTS_PER_CCD) if not (fuse >> slot) & 1]
        if len(live) == want:
            return live
        raise CoreMapError(
            f"core ids on this CPU are renumbered and the CCD-{encode_ccd} "
            f"core-disable fuse disagrees with the OS: fuse {fuse & 0xFF:#04x} "
            f"leaves {len(live)} live slots {live} but the OS reports {want} "
            f"cores -- per-core CO stays disabled instead of writing to the "
            "wrong cores; please report this output"
        )

    @property
    def core_map(self) -> dict[int, tuple[int, int]] | None:
        """Discovered {core_id: (ccd, slot)} addressing map, if any."""
        return dict(self._core_map) if self._core_map is not None else None

    @property
    def core_map_error(self) -> str | None:
        """Why per-core CO is refused, or None when addressing is usable."""
        return self._core_map_error

    def _co_address(self, core_id: int) -> tuple[int | None, int | None] | None:
        """(ccd, slot) for encode_co_arg, or None when the write must refuse.

        None means either the map discovery failed (core_map_error holds why)
        or the caller named a core the discovered map does not contain.
        """
        if self._core_map_error is not None:
            return None
        if self._core_map is not None:
            return self._core_map.get(core_id)
        ccd = self._topology_ccd.get(core_id) if self._topology_ccd else None
        return (ccd, None)

    @staticmethod
    def is_available(sysfs_path: Path = SYSFS_BASE) -> bool:
        """Check if ryzen_smu driver is loaded and accessible."""
        return sysfs_path.exists() and (sysfs_path / "smu_args").exists()

    def check_writable(self) -> tuple[bool, str]:
        """Check if the sysfs files are writable before attempting any write.

        Returns (ok, message).  Call this early to give the user a clear
        error instead of a cryptic ``PermissionError`` mid-write.
        """
        for name in ("smu_args", self._get_cmd_filename()):
            p = self.sysfs / name
            if not p.exists():
                return False, f"sysfs file not found: {p}"
            if not os.access(p, os.W_OK):
                return False, f"No write permission on {p} — run as root or fix udev rules"
        return True, "OK"

    # ------------------------------------------------------------------
    # Backup / restore
    # ------------------------------------------------------------------

    def backup_co_offsets(self, num_cores: int) -> dict[int, int]:
        """Save current CO offsets for all cores before modification."""
        offsets = self.get_all_co_offsets(num_cores)
        self._backup = dict(offsets)
        readable = {core_id: value for core_id, value in offsets.items() if value is not None}
        log.info("Backed up CO offsets for %d cores: %s", len(readable), readable)
        return readable

    def restore_co_offsets(self) -> tuple[bool, list[int]]:
        """Restore captured offsets and report every core that could not be restored."""
        if self._backup is None:
            log.warning("restore_co_offsets called with no backup available")
            return False, []
        failed: list[int] = []
        for core_id, value in self._backup.items():
            if value is None or not self.set_co_offset(core_id, value):
                failed.append(core_id)
        ok = not failed
        if ok:
            log.info("Restored CO offsets from backup successfully")
        else:
            log.error("Failed to restore CO offsets for cores: %s", failed)
        return ok, failed

    def has_backup(self) -> bool:
        """Return whether every requested core has a captured offset."""
        return self._backup is not None and all(value is not None for value in self._backup.values())

    # ------------------------------------------------------------------
    # Low-level SMU communication
    # ------------------------------------------------------------------

    def _get_cmd_filename(self) -> str:
        if self.commands.mailbox == "mp1":
            return "mp1_smu_cmd"
        return "rsmu_cmd"

    def _send_get_co(self, arg: int) -> SMUResponse:
        """Send the CO read on its own mailbox.

        APU generations set CO via MP1 but read it back via RSMU
        (get_co_mailbox="rsmu"); everything else reads on the default mailbox.
        """
        mailbox = self.commands.get_co_mailbox or self.commands.mailbox
        if mailbox == "rsmu" and self.commands.mailbox != "rsmu":
            return self._send_rsmu_command(self.commands.get_co_cmd, (arg,))
        return self._send_command(self.commands.get_co_cmd, (arg,))

    def _get_cmd_path(self) -> Path:
        """Get the command file path based on mailbox type."""
        return self.sysfs / self._get_cmd_filename()

    def _send_command(self, cmd: int, args: tuple[int, ...] = (0, 0, 0, 0, 0, 0)) -> SMUResponse:
        """Send a command through the generation's default mailbox."""
        return self._mailbox_transaction(self._get_cmd_path(), cmd, args)

    def _mailbox_transaction(self, cmd_path: Path, cmd: int, args: tuple[int, ...]) -> SMUResponse:
        with self._smu_lock:
            args_path = self.sysfs / "smu_args"
            padded_args = (args + (0,) * 6)[:6]
            try:
                packed_args = struct.pack("<6I", *padded_args)
                packed_cmd = struct.pack("<I", cmd)
                if args_path.write_bytes(packed_args) != len(packed_args):
                    raise OSError("truncated SMU argument write")
                if cmd_path.write_bytes(packed_cmd) != len(packed_cmd):
                    raise OSError("truncated SMU command write")
                resp_cmd = cmd_path.read_bytes()
                resp_args_raw = args_path.read_bytes()
                if len(resp_cmd) != 4 or len(resp_args_raw) != 24:
                    raise OSError("invalid SMU response length")
                status = struct.unpack("<I", resp_cmd)[0]
                resp_args = struct.unpack("<6I", resp_args_raw)
            except (OSError, struct.error) as exc:
                log.debug("SMU command %#x via %s failed: %s", cmd, cmd_path.name, exc)
                return SMUResponse(success=False, args=(0,) * 6, raw=b"")
            return SMUResponse(success=status == 1, args=resp_args, raw=resp_args_raw)

    def _send_rsmu_command(self, cmd: int, args: tuple[int, ...] = (0, 0, 0, 0, 0, 0)) -> SMUResponse:
        """Send an RSMU command regardless of the default mailbox."""
        return self._mailbox_transaction(self.sysfs / "rsmu_cmd", cmd, args)

    # ------------------------------------------------------------------
    # CO offset read/write
    # ------------------------------------------------------------------

    def get_co_offset(self, core_id: int) -> int | None:
        """Read the current CO offset for a physical core.

        CO values are VOLATILE and reset to zero on reboot.
        """
        if not self.commands.has_co:
            return None
        addr = self._co_address(core_id)
        if addr is None:
            log.error(
                "CO read refused for core %d: %s",
                core_id,
                self._core_map_error or "core is not in the discovered core map",
            )
            return None
        ccd, slot = addr
        arg = encode_co_arg(core_id, 0, self.commands.generation, ccd=ccd, slot=slot)
        resp = self._send_get_co(arg)
        if not resp.success:
            return None
        return decode_co_arg(core_id, resp.args[0], self.commands.generation)

    def set_co_offset(self, core_id: int, value: int) -> bool:
        """Set the CO offset for a physical core. Returns True on success.

        CO values are VOLATILE — they live in SMU SRAM and reset on reboot.
        Your BIOS PBO settings are never modified.

        Safety:
          - Range-checked against the generation's CO limits
          - In ``dry_run`` mode, logs the intended write without touching HW
          - Verifies via read-back that the value was applied correctly
          - Pre-checks file permissions before writing
        """
        if not self.commands.has_co:
            log.error(
                "Generation %s does not support Curve Optimizer",
                self.commands.generation.name,
            )
            return False

        co_min, co_max = self.commands.co_range
        if not co_min <= value <= co_max:
            raise ValueError(f"CO value {value} out of range [{co_min}, {co_max}] for {self.commands.generation.name}")

        # --- core-address guard (before dry-run: never simulate an
        # unaddressable write) ---
        addr = self._co_address(core_id)
        if addr is None:
            log.error(
                "CO write refused for core %d: %s",
                core_id,
                self._core_map_error or "core is not in the discovered core map",
            )
            return False

        # --- dry-run guard ---
        if self.dry_run:
            log.info("[DRY RUN] Would set core %d CO to %d (not written)", core_id, value)
            return True

        # --- permission pre-check ---
        ok, msg = self.check_writable()
        if not ok:
            log.error("Permission check failed before CO write: %s", msg)
            return False

        ccd, slot = addr
        arg = encode_co_arg(core_id, value, self.commands.generation, ccd=ccd, slot=slot)
        resp = self._send_command(self.commands.set_co_cmd, (arg,))
        if not resp.success:
            log.error("SMU rejected CO write for core %d value %d", core_id, value)
            return False

        # --- read-back verification ---
        readback = self.get_co_offset(core_id)
        if readback != value:
            log.error(
                "CO read-back mismatch for core %d: wrote %d, read back %s",
                core_id,
                value,
                readback,
            )
            return False

        log.info("Set core %d CO to %d (verified)", core_id, value)
        return True

    def set_all_co(self, value: int) -> bool:
        """Set CO offset for ALL cores at once. Returns True on success.

        Uses the SetAllDldoPsmMargin command if available.
        Returns False if the set-all command is not available for this generation.
        """
        if not self.commands.has_co:
            return False

        co_min, co_max = self.commands.co_range
        if not co_min <= value <= co_max:
            raise ValueError(f"CO value {value} out of range [{co_min}, {co_max}] for {self.commands.generation.name}")

        # The all-cores command takes no core address, but its read-back
        # verification does — with no usable core map the write would be
        # unverifiable, so it refuses like the per-core path.
        if self._core_map_error is not None:
            log.error("set_all_co refused: %s", self._core_map_error)
            return False

        if self.dry_run:
            log.info("[DRY RUN] Would set all cores CO to %d (not written)", value)
            return True

        if self.commands.set_all_co_cmd is not None:
            margin = value & 0xFFFF
            resp = self._send_command(self.commands.set_all_co_cmd, (margin,))
            if not resp.success:
                log.error("SMU rejected set_all_co with value %d", value)
                return False

            # Read-back verification on the first known core (core id 0 can be
            # a fused-off slot on harvested parts) to confirm the write took.
            readback_core = self._known_core_ids[0] if self._known_core_ids else 0
            readback = self.get_co_offset(readback_core)
            if readback != value:
                log.error(
                    "set_all_co read-back mismatch: wrote %d, read back %s from core %d",
                    value,
                    readback,
                    readback_core,
                )
                return False
            return True
        return False

    def reset_all_co(self) -> bool:
        """Reset all CO offsets to 0 by delegating to ``set_all_co``.

        The delegate owns the whole refusal contract (range check, core-map
        guard, dry-run), so a dry-run reset can never report success for an
        operation the real path refuses.

        CO values are VOLATILE — this resets them to 0 for the current
        session only.  On reboot they return to whatever the BIOS sets.
        """
        return self.set_all_co(0)

    def get_all_co_offsets(self, num_cores: int) -> dict[int, int | None]:
        """Read CO offsets for all cores.

        With a loaded topology this iterates the machine's real core-id set
        (ids are not contiguous on gap-preserving harvested parts: a 5900X
        runs 0-5 and 8-13), and ``num_cores`` is only the fallback domain
        ``range(num_cores)`` for callers that never loaded one.

        CO values are VOLATILE — they reset to zero on reboot.
        """
        core_ids = self._known_core_ids if self._known_core_ids is not None else range(num_cores)
        return {core_id: self.get_co_offset(core_id) for core_id in core_ids}

    # ------------------------------------------------------------------
    # Boost frequency
    # ------------------------------------------------------------------

    def get_boost_limit(self) -> int | None:
        """Read the boost frequency limit (MHz). Zen 4/5 only."""
        cmd = self.commands.get_boost_limit_cmd
        if cmd is None:
            return None
        resp = self._send_rsmu_command(cmd)
        if not resp.success:
            return None
        return resp.args[0]

    def set_boost_limit(self, mhz: int) -> bool:
        """Set the boost frequency limit for all cores (MHz). Zen 4/5 only.

        Like CO offsets, this is VOLATILE and resets on reboot.
        No artificial cap is imposed — the hardware/firmware enforce
        actual limits. Users with PBO boost override +200 and BCLK 105+
        may see effective clocks above 6 GHz; this is expected.
        """
        cmd = self.commands.set_boost_limit_cmd
        if cmd is None:
            return False
        if self.dry_run:
            log.info("[DRY RUN] Would set boost limit to %d MHz (not written)", mhz)
            return True
        resp = self._send_rsmu_command(cmd, (encode_boost_limit_arg(mhz),))
        return resp.success

    # ------------------------------------------------------------------
    # PBO limits (PPT, TDC, EDC)
    # ------------------------------------------------------------------

    def set_ppt_limit(self, watts: int) -> bool:
        """Set PPT (Package Power Tracking) limit in watts. VOLATILE.

        Range-checked and raises ValueError on a malformed value before any write.
        """
        cmd = self.commands.set_ppt_cmd
        if cmd is None:
            return False
        _check_pbo_limit("PPT", watts)
        if self.dry_run:
            log.info("[DRY RUN] Would set PPT limit to %d W", watts)
            return True
        resp = self._send_rsmu_command(cmd, (encode_pbo_limit_arg(watts),))
        return resp.success

    def set_tdc_limit(self, amps: int) -> bool:
        """Set TDC (Thermal Design Current) limit in amps. VOLATILE.

        Range-checked and raises ValueError on a malformed value before any write.
        """
        cmd = self.commands.set_tdc_cmd
        if cmd is None:
            return False
        _check_pbo_limit("TDC", amps)
        if self.dry_run:
            log.info("[DRY RUN] Would set TDC limit to %d A", amps)
            return True
        resp = self._send_rsmu_command(cmd, (encode_pbo_limit_arg(amps),))
        return resp.success

    def set_edc_limit(self, amps: int) -> bool:
        """Set EDC (Electrical Design Current) limit in amps. VOLATILE.

        Range-checked and raises ValueError on a malformed value before any write.
        """
        cmd = self.commands.set_edc_cmd
        if cmd is None:
            return False
        _check_pbo_limit("EDC", amps)
        if self.dry_run:
            log.info("[DRY RUN] Would set EDC limit to %d A", amps)
            return True
        resp = self._send_rsmu_command(cmd, (encode_pbo_limit_arg(amps),))
        return resp.success

    # ------------------------------------------------------------------
    # PBO scalar
    # ------------------------------------------------------------------

    def get_pbo_scalar(self) -> float | None:
        """Read current PBO scalar (1.0 to 10.0)."""
        cmd = self.commands.get_pbo_scalar_cmd
        if cmd is None:
            return None
        resp = self._send_rsmu_command(cmd)
        if not resp.success:
            return None
        # Response is an IEEE 754 float in the first arg word
        raw_bytes = struct.pack("<I", resp.args[0])
        return struct.unpack("<f", raw_bytes)[0]

    def set_pbo_scalar(self, scalar: float) -> bool:
        """Set PBO scalar (1.0 to 10.0). VOLATILE."""
        cmd = self.commands.set_pbo_scalar_cmd
        if cmd is None:
            return False
        if not 0.0 <= scalar <= 10.0:
            raise ValueError(f"PBO scalar {scalar} out of range [0.0, 10.0]")
        if self.dry_run:
            log.info("[DRY RUN] Would set PBO scalar to %.1f", scalar)
            return True
        resp = self._send_rsmu_command(cmd, (encode_pbo_scalar_arg(scalar),))
        return resp.success

    # ------------------------------------------------------------------
    # System state detection
    # ------------------------------------------------------------------

    def detect_system_state(self, num_cores: int) -> SystemPBOState:
        """Read the current PBO/CO state from SMU and sysfs.

        This provides a snapshot of the system's current configuration,
        including CO offsets, PBO limits, boost override, and max frequency.
        Call this before starting a test to understand the baseline.
        """
        state = SystemPBOState(
            generation=self.commands.generation,
            smu_available=True,
        )

        # Read CO offsets
        if self.commands.has_co:
            state.co_offsets = self.get_all_co_offsets(num_cores)

        # Read boost limit
        state.boost_limit_mhz = self.get_boost_limit()

        # Read PBO scalar
        state.pbo_scalar = self.get_pbo_scalar()

        # Read fastest core
        if self.commands.get_fastest_core_cmd is not None:
            resp = self._send_rsmu_command(self.commands.get_fastest_core_cmd)
            if resp.success:
                state.fastest_core = resp.args[0]

        # Read max frequency from cpufreq sysfs (accounts for boost override + BCLK)
        state.max_freq_mhz = _read_max_freq_sysfs()

        return state

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def get_fastest_core(self) -> int | None:
        """Query the SMU for the fastest core index."""
        cmd = self.commands.get_fastest_core_cmd
        if cmd is None:
            return None
        resp = self._send_rsmu_command(cmd)
        if not resp.success:
            return None
        return resp.args[0]


# ===========================================================================
# Sysfs helpers for system state detection
# ===========================================================================


def _read_max_freq_sysfs() -> float | None:
    """Read max boost frequency from cpufreq sysfs (MHz).

    This reflects the actual boost limit including PBO boost override
    and BCLK scaling.
    """
    path = Path("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq")
    with contextlib.suppress(ValueError, OSError):
        if path.exists():
            return int(path.read_text().strip()) / 1000.0
    return None
