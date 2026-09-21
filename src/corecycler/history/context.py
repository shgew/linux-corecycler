"""Capture and identify the complete CPU tuning context."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from corecycler.history.db import HistoryDB, TuningContextRecord
    from corecycler.smu.driver import RyzenSMU

log = logging.getLogger(__name__)

BIOS_VERSION_PATH = Path("/sys/class/dmi/id/bios_version")
_CONTEXT_HASH_VERSION = 1


@dataclass(frozen=True, slots=True)
class SystemContext:
    cpu_model: str
    physical_cores: int
    ccds: int
    co: tuple[int | None, ...]
    pbo_scalar: float | None
    boost_limit_mhz: int | None
    ppt_limit_w: float | None
    tdc_limit_a: float | None
    edc_limit_a: float | None
    bios_version: str
    context_hash: str
    complete: bool
    missing: tuple[str, ...]

    def to_record(self) -> TuningContextRecord:
        from corecycler.history.db import TuningContextRecord

        return TuningContextRecord(
            bios_version=self.bios_version,
            cpu_model=self.cpu_model,
            physical_cores=self.physical_cores,
            ccds=self.ccds,
            co_offsets_json=json.dumps(
                {str(core_id): offset for core_id, offset in enumerate(self.co)},
                separators=(",", ":"),
            ),
            context_hash=self.context_hash,
            pbo_scalar=self.pbo_scalar,
            boost_limit_mhz=self.boost_limit_mhz,
            ppt_limit_w=self.ppt_limit_w,
            tdc_limit_a=self.tdc_limit_a,
            edc_limit_a=self.edc_limit_a,
        )


def read_bios_version(path: Path = BIOS_VERSION_PATH) -> str:
    try:
        if path.exists():
            return path.read_text().strip()
    except OSError:
        log.debug("Could not read BIOS version from %s", path, exc_info=True)
    return ""


def compute_context_hash(
    *,
    cpu_model: str,
    physical_cores: int,
    ccds: int,
    co: tuple[int | None, ...],
    pbo_scalar: float | None,
    boost_limit_mhz: int | None,
    ppt_limit_w: float | None,
    tdc_limit_a: float | None,
    edc_limit_a: float | None,
    bios_version: str,
) -> str:
    payload = {
        "version": _CONTEXT_HASH_VERSION,
        "cpu_model": cpu_model,
        "physical_cores": physical_cores,
        "ccds": ccds,
        "co": list(co),
        "pbo_scalar": pbo_scalar,
        "boost_limit_mhz": boost_limit_mhz,
        "ppt_limit_w": ppt_limit_w,
        "tdc_limit_a": tdc_limit_a,
        "edc_limit_a": edc_limit_a,
        "bios_version": bios_version,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def capture_system_context(
    smu: RyzenSMU | None = None,
    num_cores: int = 0,
    bios_path: Path = BIOS_VERSION_PATH,
    *,
    cpu_model: str = "",
    ccds: int = 0,
) -> SystemContext:
    bios_version = read_bios_version(bios_path)
    offsets: dict[int, int | None] = {}
    pbo_scalar: float | None = None
    boost_limit_mhz: int | None = None
    ppt_limit_w: float | None = None
    tdc_limit_a: float | None = None
    edc_limit_a: float | None = None

    if smu is not None and num_cores > 0:
        try:
            offsets = smu.get_all_co_offsets(num_cores)
        except Exception:
            log.warning("Failed to read CO offsets from SMU", exc_info=True)
        try:
            pbo_scalar = smu.get_pbo_scalar()
        except Exception:
            log.debug("Failed to read PBO scalar", exc_info=True)
        try:
            boost_limit_mhz = smu.get_boost_limit()
        except Exception:
            log.debug("Failed to read boost limit", exc_info=True)
        try:
            from corecycler.smu.pmtable import read_power_limits

            ppt_limit_w, tdc_limit_a, edc_limit_a = read_power_limits()
        except Exception:
            log.debug("Failed to read PBO power limits", exc_info=True)

    co = tuple(offsets.get(core_id) for core_id in range(num_cores))
    missing = []
    if not cpu_model:
        missing.append("cpu_model")
    missing.extend(f"co[{core_id}]" for core_id, offset in enumerate(co) if offset is None)
    if num_cores <= 0:
        missing.append("co")
    missing_tuple = tuple(missing)
    context_hash = compute_context_hash(
        cpu_model=cpu_model,
        physical_cores=num_cores,
        ccds=ccds,
        co=co,
        pbo_scalar=pbo_scalar,
        boost_limit_mhz=boost_limit_mhz,
        ppt_limit_w=ppt_limit_w,
        tdc_limit_a=tdc_limit_a,
        edc_limit_a=edc_limit_a,
        bios_version=bios_version,
    )
    return SystemContext(
        cpu_model=cpu_model,
        physical_cores=num_cores,
        ccds=ccds,
        co=co,
        pbo_scalar=pbo_scalar,
        boost_limit_mhz=boost_limit_mhz,
        ppt_limit_w=ppt_limit_w,
        tdc_limit_a=tdc_limit_a,
        edc_limit_a=edc_limit_a,
        bios_version=bios_version,
        context_hash=context_hash,
        complete=not missing_tuple,
        missing=missing_tuple,
    )


def detect_bios_change(
    db: HistoryDB,
    bios_path: Path = BIOS_VERSION_PATH,
) -> tuple[bool, str, str]:
    current = read_bios_version(bios_path)
    contexts = db.list_contexts(limit=1)
    if not contexts:
        return False, "", current
    old = contexts[0].bios_version
    return old != current, old, current
