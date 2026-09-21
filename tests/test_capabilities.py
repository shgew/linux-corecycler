"""The launcher's capability reaches this process and stops there."""

from __future__ import annotations

import logging
import subprocess
import sys as _sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from corecycler import capabilities

_RAWIO_BIT = 1 << capabilities.CAP_SYS_RAWIO


class FakeLibc:
    """A libc whose capability syscalls are scripted and recorded."""

    def __init__(self, *, effective: int = 0, prctl_rc: int = 0, capget_rc: int = 0, capset_rc: int = 0) -> None:
        self.effective = effective
        self.prctl_rc = prctl_rc
        self.capget_rc = capget_rc
        self.capset_rc = capset_rc
        self.prctl_calls: list[tuple[int, ...]] = []
        self.capset_calls: list[list[tuple[int, int, int]]] = []

    def prctl(self, *args: int) -> int:
        self.prctl_calls.append(args)
        return self.prctl_rc

    def capget(self, _header, data) -> int:
        if self.capget_rc == 0:
            data[0].effective = self.effective
            data[0].permitted = self.effective
            data[0].inheritable = self.effective
        return self.capget_rc

    def capset(self, _header, data) -> int:
        self.capset_calls.append([(w.effective, w.permitted, w.inheritable) for w in data])
        return self.capset_rc


def _confine_with(libc: FakeLibc) -> bool:
    with patch.object(capabilities, "_libc", return_value=libc):
        return capabilities.confine()


class TestConfine:
    def test_reports_the_capability_the_launcher_handed_over(self):
        libc = FakeLibc(effective=_RAWIO_BIT)
        assert _confine_with(libc) is True

    def test_reports_nothing_when_launched_without_the_capability(self):
        assert _confine_with(FakeLibc()) is False

    def test_empties_the_ambient_set_so_no_payload_inherits_raw_io(self):
        libc = FakeLibc(effective=_RAWIO_BIT)
        _confine_with(libc)
        assert libc.prctl_calls == [(capabilities._PR_CAP_AMBIENT, capabilities._PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0)]

    def test_keeps_the_capability_here_while_clearing_what_children_would_get(self):
        libc = FakeLibc(effective=_RAWIO_BIT)
        _confine_with(libc)
        assert libc.capset_calls == [[(_RAWIO_BIT, _RAWIO_BIT, 0), (0, 0, 0)]]

    def test_unclearable_ambient_set_gives_the_capability_up_entirely(self, caplog):
        libc = FakeLibc(effective=_RAWIO_BIT, prctl_rc=-1)
        with caplog.at_level(logging.ERROR):
            assert _confine_with(libc) is False
        assert libc.capset_calls == [[(0, 0, 0), (0, 0, 0)]]
        assert "dropping every capability" in caplog.text

    def test_reports_the_leak_when_neither_clearing_nor_dropping_works(self, caplog):
        libc = FakeLibc(effective=_RAWIO_BIT, prctl_rc=-1, capset_rc=-1)
        with caplog.at_level(logging.ERROR):
            assert _confine_with(libc) is False
        assert "may inherit CAP_SYS_RAWIO" in caplog.text

    def test_unreadable_capabilities_report_none_after_the_ambient_set_is_cleared(self):
        libc = FakeLibc(effective=_RAWIO_BIT, capget_rc=-1)
        assert _confine_with(libc) is False
        assert libc.prctl_calls  # inheritance was severed before giving up
        assert libc.capset_calls == []

    def test_a_refused_inheritable_clear_does_not_hide_the_capability(self, caplog):
        libc = FakeLibc(effective=_RAWIO_BIT, capset_rc=-1)
        with caplog.at_level(logging.WARNING):
            assert _confine_with(libc) is True
        assert "inheritable capability set" in caplog.text

    def test_a_libc_without_the_syscalls_confines_nothing(self):
        with patch.object(capabilities, "_libc", return_value=None):
            assert capabilities.confine() is False


class TestLibc:
    def test_resolves_the_capability_syscalls_of_the_running_libc(self):
        assert capabilities._libc() is not None

    def test_unloadable_libc_is_not_an_error(self, caplog):
        with (
            patch.object(capabilities.ctypes, "CDLL", side_effect=OSError("no libc")),
            caplog.at_level(logging.DEBUG),
        ):
            assert capabilities._libc() is None
        assert "no libc" in caplog.text

    def test_missing_symbol_is_not_an_error(self):
        partial = SimpleNamespace(prctl=lambda *a: 0, capget=lambda *a: 0)
        with patch.object(capabilities.ctypes, "CDLL", return_value=partial):
            assert capabilities._libc() is None


class TestRealProcess:
    def test_a_real_process_ends_up_with_empty_inheritable_sets(self):
        src = Path(__file__).parent.parent / "src"
        script = (
            f"import sys; sys.path.insert(0, {str(src)!r})\n"
            "from corecycler import capabilities\n"
            "capabilities.confine()\n"
            "print(open('/proc/self/status').read())\n"
        )
        out = subprocess.run([_sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        sets = dict(line.split(":", 1) for line in out.stdout.splitlines() if line.startswith(("CapInh", "CapAmb")))
        assert int(sets["CapInh"], 16) == 0
        assert int(sets["CapAmb"], 16) == 0
