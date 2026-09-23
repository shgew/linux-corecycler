"""What the hermetic gate is not allowed to touch.

Two leaks are structural rather than accidental. A test that opens a socket
makes the suite depend on a network the CI sandbox does not have and the
developer's box does. And a Ring B test carries no marker that `-m "not slow"`
filters, so without an explicit gate it runs its real systemd scopes and real
stress binaries as a side effect of the everyday run.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from tests._contract_hw import hw_contracts_requested
from tests.conftest import _LIVE_TIER


class TestNoNetwork:
    def test_an_outbound_connection_is_refused(self):
        with pytest.raises(RuntimeError, match="network"):
            socket.create_connection(("example.com", 80), timeout=0.1)

    def test_a_raw_socket_connect_is_refused(self):
        with pytest.raises(RuntimeError, match="network"), socket.socket() as sock:
            sock.connect(("192.0.2.1", 80))

    def test_a_name_lookup_is_refused(self):
        with pytest.raises(RuntimeError, match="network"):
            socket.getaddrinfo("example.com", 80)

    def test_a_local_unix_socket_is_still_allowed(self, tmp_path):
        path = str(tmp_path / "sock")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(path)
            server.listen(1)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(path)
                assert client.getpeername() == path


class TestRingBIsOptIn:
    @pytest.mark.parametrize(
        "argv",
        [
            (),
            ("-m", "contract"),
            ("-m", "not (contract)"),
            ("-m", "not   contract"),
            ("-k", "contract"),
        ],
    )
    def test_pytest_selection_never_enables_live_hardware(self, monkeypatch, argv):
        monkeypatch.setattr("sys.argv", ["pytest", *argv])
        assert hw_contracts_requested({}) is False

    def test_only_the_hardware_contract_switch_enables_live_hardware(self):
        assert hw_contracts_requested({"CORECYCLER_HW_CONTRACTS": "1"}) is True
        assert hw_contracts_requested({"CORECYCLER_HW_PRIVILEGED": "1"}) is False
        assert hw_contracts_requested({"CORECYCLER_HW_CONTRACTS": "0"}) is False

    def test_nothing_live_is_collected_to_run_in_this_session(self, request):
        if hw_contracts_requested():
            pytest.skip("this run asked for Ring B")
        live = [
            item.nodeid
            for item in request.session.items
            if item.get_closest_marker("contract") and not item.get_closest_marker("skip")
        ]
        assert live == []


@pytest.mark.skipif(_LIVE_TIER, reason="the live tier deliberately keeps the real environment")
class TestIsolatedHome:
    def test_home_is_a_throwaway(self):
        assert Path(os.environ["HOME"]).is_relative_to(tempfile.gettempdir())

    def test_the_app_agrees_where_home_is(self):
        from corecycler.config.paths import user_home

        assert user_home() == Path(os.environ["HOME"])

    def test_the_state_the_app_creates_lands_there(self):
        from corecycler.history.db import DATA_DIR

        assert DATA_DIR.is_relative_to(os.environ["HOME"])


@pytest.mark.skipif(_LIVE_TIER, reason="the live tier deliberately keeps the real environment")
class TestNoDesktop:
    def test_qt_never_reaches_the_users_session(self):
        pyside = pytest.importorskip("PySide6")
        if not getattr(pyside, "__path__", None):
            pytest.skip("Qt stub, not the real PySide6")
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        assert app.platformName() == "offscreen"


class TestCleanWorkerExit:
    def test_a_leaked_queued_qt_call_does_not_crash_the_worker_at_exit(self, tmp_path):
        pyside = pytest.importorskip("PySide6")
        if not getattr(pyside, "__path__", None):
            pytest.skip("Qt stub, not the real PySide6")
        (tmp_path / "test_leak.py").write_text(
            "from PySide6.QtCore import QTimer\n\n\ndef test_leak():\n    QTimer.singleShot(0, lambda: None)\n"
        )
        pytest_args = ["-p", "tests.conftest", "-p", "no:cacheprovider", "-n0", "-q", str(tmp_path)]
        result = subprocess.run(
            [sys.executable, "-m", "pytest", *pytest_args],
            cwd=Path(__file__).parent.parent,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stdout + result.stderr
