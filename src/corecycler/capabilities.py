"""Confine launcher capabilities to the CoreCycler process."""

from __future__ import annotations

import ctypes
import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

CAP_SYS_RAWIO = 17

_PR_CAP_AMBIENT = 47
_PR_CAP_AMBIENT_CLEAR_ALL = 4
_CAP_VERSION_3 = 0x20080522
_CAP_WORDS = 2


@dataclass(frozen=True, slots=True)
class ConfinementResult:
    safe: bool
    has_rawio: bool


class _CapHeader(ctypes.Structure):
    _fields_ = (("version", ctypes.c_uint32), ("pid", ctypes.c_int))


class _CapData(ctypes.Structure):
    _fields_ = (
        ("effective", ctypes.c_uint32),
        ("permitted", ctypes.c_uint32),
        ("inheritable", ctypes.c_uint32),
    )


def confine() -> ConfinementResult:
    """Clear inherited capability paths and report whether confinement is proven."""
    libc = _libc()
    if libc is None:
        return _status_result() or ConfinementResult(False, False)

    if libc.prctl(_PR_CAP_AMBIENT, _PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0) != 0:
        log.error(
            "cannot clear the ambient capability set (%s); dropping every capability so no stress payload inherits one",
            os.strerror(ctypes.get_errno()),
        )
        if not _drop_everything(libc):
            log.error("capability confinement failed; a stress payload may inherit CAP_SYS_RAWIO")
            return ConfinementResult(False, True)
        status = _status_result()
        return status if status is not None else ConfinementResult(True, False)

    data = _capget(libc)
    if data is None:
        return _status_result() or ConfinementResult(False, False)
    has_rawio = bool(data[0].effective & (1 << CAP_SYS_RAWIO))
    for word in data:
        word.inheritable = 0
    if not _capset(libc, data):
        log.warning("cannot clear the inheritable capability set: %s", os.strerror(ctypes.get_errno()))
        return ConfinementResult(False, has_rawio)

    verified = _capget(libc)
    if verified is None or any(word.inheritable for word in verified):
        status = _status_result()
        if status is None or not status.safe:
            log.error("capability confinement could not verify an empty inheritable set")
            return ConfinementResult(False, has_rawio)
    return ConfinementResult(True, has_rawio)


def _status_result(path: Path = Path("/proc/self/status")) -> ConfinementResult | None:
    try:
        values = {
            key: int(value.strip(), 16)
            for key, value in (line.split(":", 1) for line in path.read_text().splitlines() if ":" in line)
            if key in {"CapEff", "CapInh", "CapAmb"}
        }
    except (OSError, ValueError):
        return None
    if values.keys() != {"CapEff", "CapInh", "CapAmb"}:
        return None
    safe = values["CapInh"] == 0 and values["CapAmb"] == 0
    return ConfinementResult(safe, bool(values["CapEff"] & (1 << CAP_SYS_RAWIO)))


def _libc() -> ctypes.CDLL | None:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        libc.prctl.restype = ctypes.c_int
        libc.capget.argtypes = [ctypes.POINTER(_CapHeader), ctypes.POINTER(_CapData)]
        libc.capget.restype = ctypes.c_int
        libc.capset.argtypes = [ctypes.POINTER(_CapHeader), ctypes.POINTER(_CapData)]
        libc.capset.restype = ctypes.c_int
    except (AttributeError, OSError) as exc:
        log.debug("Linux capability syscalls unavailable: %s", exc)
        return None
    return libc


def _capget(libc: ctypes.CDLL) -> ctypes.Array[_CapData] | None:
    header = _CapHeader(_CAP_VERSION_3, 0)
    data = (_CapData * _CAP_WORDS)()
    if libc.capget(ctypes.byref(header), data) != 0:
        log.error("capget failed: %s", os.strerror(ctypes.get_errno()))
        return None
    return data


def _capset(libc: ctypes.CDLL, data: ctypes.Array[_CapData]) -> bool:
    header = _CapHeader(_CAP_VERSION_3, 0)
    return libc.capset(ctypes.byref(header), data) == 0


def _drop_everything(libc: ctypes.CDLL) -> bool:
    data = (_CapData * _CAP_WORDS)()
    if not _capset(libc, data):
        log.error("cannot drop process capabilities: %s", os.strerror(ctypes.get_errno()))
        return False
    return True
