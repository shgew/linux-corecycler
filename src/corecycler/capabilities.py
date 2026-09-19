"""Confine the launcher's capabilities to this process.

Opening ``/dev/cpu/N/msr`` needs CAP_SYS_RAWIO: ``msr_open`` in
``arch/x86/kernel/msr.c`` refuses the open before any file mode is consulted,
so no group or udev rule can grant MSR reads on its own. A setcap launcher
(``security.wrappers.corecycler`` on NixOS) hands the capability over through
the ambient set, the only set that survives the exec of an interpreted entry
point - and the set every descendant inherits.

A stress payload must never run with raw I/O rights, so the ambient set is
emptied here, before the first subprocess exists. If it cannot be emptied, the
capability is dropped instead: losing MSR telemetry is the safe outcome.
"""

from __future__ import annotations

import ctypes
import logging
import os

log = logging.getLogger(__name__)

CAP_SYS_RAWIO = 17

_PR_CAP_AMBIENT = 47
_PR_CAP_AMBIENT_CLEAR_ALL = 4
_CAP_VERSION_3 = 0x20080522
_CAP_WORDS = 2


class _CapHeader(ctypes.Structure):
    _fields_ = (
        ("version", ctypes.c_uint32),
        ("pid", ctypes.c_int),
    )


class _CapData(ctypes.Structure):
    _fields_ = (
        ("effective", ctypes.c_uint32),
        ("permitted", ctypes.c_uint32),
        ("inheritable", ctypes.c_uint32),
    )


def confine() -> bool:
    """Empty the inheritable sets and report whether CAP_SYS_RAWIO survives here."""
    libc = _libc()
    if libc is None:
        return False

    if libc.prctl(_PR_CAP_AMBIENT, _PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0) != 0:
        log.error(
            "cannot clear the ambient capability set (%s); dropping every capability "
            "so no stress payload inherits one, MSR telemetry stays unavailable",
            os.strerror(ctypes.get_errno()),
        )
        _drop_everything(libc)
        return False

    data = _capget(libc)
    if data is None:
        return False

    holds_rawio = bool(data[0].effective & (1 << CAP_SYS_RAWIO))
    for word in data:
        word.inheritable = 0
    if not _capset(libc, data):
        log.warning("cannot clear the inheritable capability set: %s", os.strerror(ctypes.get_errno()))
    return holds_rawio


def _libc() -> ctypes.CDLL | None:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        for symbol in ("prctl", "capget", "capset"):
            getattr(libc, symbol)
    except (OSError, AttributeError) as exc:
        log.debug("capability confinement unavailable on this libc: %s", exc)
        return None
    return libc


def _capget(libc: ctypes.CDLL) -> ctypes.Array[_CapData] | None:
    header = _CapHeader(_CAP_VERSION_3, 0)
    data = (_CapData * _CAP_WORDS)()
    if libc.capget(ctypes.byref(header), data) != 0:
        log.debug("capget failed: %s", os.strerror(ctypes.get_errno()))
        return None
    return data


def _capset(libc: ctypes.CDLL, data: ctypes.Array[_CapData]) -> bool:
    header = _CapHeader(_CAP_VERSION_3, 0)
    return libc.capset(ctypes.byref(header), data) == 0


def _drop_everything(libc: ctypes.CDLL) -> None:
    data = (_CapData * _CAP_WORDS)()
    if not _capset(libc, data):
        log.error(
            "cannot drop capabilities either (%s); a stress payload launched from "
            "this process may inherit CAP_SYS_RAWIO",
            os.strerror(ctypes.get_errno()),
        )
