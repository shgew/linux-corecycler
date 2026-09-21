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
import tempfile
from pathlib import Path

import pytest

from tests._contract_hw import ring_b_requested
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
        ("markexpr", "expected"),
        [
            ("", False),
            ("not slow", False),
            ("not contract", False),
            ("not slow and not contract", False),
            ("contract", True),
            ("not slow and contract", True),
            ("contract and hardware", True),
        ],
    )
    def test_only_an_explicit_marker_selection_asks_for_it(self, markexpr, expected):
        assert ring_b_requested(markexpr, env={}) is expected

    @pytest.mark.parametrize("flag", ["CORECYCLER_HW_CONTRACTS", "CORECYCLER_HW_PRIVILEGED"])
    def test_a_live_run_flag_asks_for_it(self, flag):
        assert ring_b_requested("not slow", env={flag: "1"}) is True

    def test_nothing_live_is_collected_to_run_in_this_session(self, request):
        if ring_b_requested(request.config.getoption("markexpr")):
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
