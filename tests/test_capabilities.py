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
        self.inheritable = effective
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
            data[0].inheritable = self.inheritable
        return self.capget_rc

    def capset(self, _header, data) -> int:
        self.capset_calls.append([(w.effective, w.permitted, w.inheritable) for w in data])
        if self.capset_rc == 0:
            self.effective = data[0].effective
            self.inheritable = data[0].inheritable
        return self.capset_rc


def _confine_with(libc: FakeLibc) -> capabilities.ConfinementResult:
    with (
        patch.object(capabilities, "_libc", return_value=libc),
        patch.object(capabilities, "_status_result", return_value=None),
    ):
        return capabilities.confine()


class TestConfine:
    def test_reports_safe_confinement_with_the_launcher_capability(self):
        result = _confine_with(FakeLibc(effective=_RAWIO_BIT))
        assert result == capabilities.ConfinementResult(safe=True, has_rawio=True)

    def test_reports_safe_confinement_without_the_capability(self):
        result = _confine_with(FakeLibc())
        assert result == capabilities.ConfinementResult(safe=True, has_rawio=False)

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
            result = _confine_with(libc)
        assert result == capabilities.ConfinementResult(safe=True, has_rawio=False)
        assert libc.capset_calls == [[(0, 0, 0), (0, 0, 0)]]
        assert "dropping every capability" in caplog.text

    def test_reports_the_leak_when_neither_clearing_nor_dropping_works(self, caplog):
        libc = FakeLibc(effective=_RAWIO_BIT, prctl_rc=-1, capset_rc=-1)
        with caplog.at_level(logging.ERROR):
            result = _confine_with(libc)
        assert result == capabilities.ConfinementResult(safe=False, has_rawio=True)
        assert "may inherit CAP_SYS_RAWIO" in caplog.text

    def test_unreadable_capabilities_are_unsafe_when_proc_is_unavailable(self):
        libc = FakeLibc(effective=_RAWIO_BIT, capget_rc=-1)
        assert _confine_with(libc).safe is False
        assert libc.prctl_calls
        assert libc.capset_calls == []

    def test_a_refused_inheritable_clear_is_unsafe(self, caplog):
        libc = FakeLibc(effective=_RAWIO_BIT, capset_rc=-1)
        with caplog.at_level(logging.WARNING):
            result = _confine_with(libc)
        assert result == capabilities.ConfinementResult(safe=False, has_rawio=True)
        assert "inheritable capability set" in caplog.text

    def test_a_libc_without_syscalls_uses_proc_status(self):
        fallback = capabilities.ConfinementResult(safe=True, has_rawio=False)
        with (
            patch.object(capabilities, "_libc", return_value=None),
            patch.object(capabilities, "_status_result", return_value=fallback),
        ):
            assert capabilities.confine() == fallback

    def test_unverifiable_inheritable_clear_is_unsafe(self, caplog):
        libc = FakeLibc(effective=_RAWIO_BIT)

        def ignore_clear(_header, data):
            libc.capset_calls.append([(word.effective, word.permitted, word.inheritable) for word in data])
            return 0

        libc.capset = ignore_clear
        with caplog.at_level(logging.ERROR):
            result = _confine_with(libc)

        assert result == capabilities.ConfinementResult(safe=False, has_rawio=True)
        assert "could not verify an empty inheritable set" in caplog.text

    def test_proc_status_can_verify_an_inheritable_clear(self):
        libc = FakeLibc(effective=_RAWIO_BIT)

        def ignore_clear(_header, data):
            libc.capset_calls.append([(word.effective, word.permitted, word.inheritable) for word in data])
            return 0

        libc.capset = ignore_clear
        fallback = capabilities.ConfinementResult(safe=True, has_rawio=True)
        with (
            patch.object(capabilities, "_libc", return_value=libc),
            patch.object(capabilities, "_status_result", return_value=fallback),
        ):
            result = capabilities.confine()

        assert result == capabilities.ConfinementResult(safe=True, has_rawio=True)


class TestStatusResult:
    def test_reports_capability_bits_from_proc_status(self, tmp_path):
        status = tmp_path / "status"
        status.write_text(f"Name:\tpython\nCapInh:\t0\nCapEff:\t{_RAWIO_BIT:x}\nCapAmb:\t0\n")

        assert capabilities._status_result(status) == capabilities.ConfinementResult(safe=True, has_rawio=True)

    def test_reports_inheritable_or_ambient_capabilities_as_unsafe(self, tmp_path):
        status = tmp_path / "status"
        status.write_text("CapInh:\t1\nCapEff:\t0\nCapAmb:\t2\n")

        assert capabilities._status_result(status) == capabilities.ConfinementResult(safe=False, has_rawio=False)

    def test_unreadable_malformed_or_incomplete_status_is_unavailable(self, tmp_path):
        malformed = tmp_path / "malformed"
        malformed.write_text("CapInh:\tnot-hex\nCapEff:\t0\nCapAmb:\t0\n")
        incomplete = tmp_path / "incomplete"
        incomplete.write_text("CapInh:\t0\nCapEff:\t0\n")

        assert capabilities._status_result(tmp_path / "missing") is None
        assert capabilities._status_result(malformed) is None
        assert capabilities._status_result(incomplete) is None


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
