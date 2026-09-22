"""Argument parsing for the login-autostart entry point."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import corecycler.main as main_mod
from corecycler.main import _parse_auto_resume


class TestParseAutoResume:
    def test_absent_returns_none(self):
        assert _parse_auto_resume([]) is None
        assert _parse_auto_resume(["--other"]) is None

    def test_present_with_seconds(self):
        assert _parse_auto_resume(["--auto-resume", "300"]) == 300

    def test_present_without_value_defaults(self):
        assert _parse_auto_resume(["--auto-resume"]) == 120

    def test_malformed_value_fails_closed_to_default(self):
        assert _parse_auto_resume(["--auto-resume", "soon"]) == 120

    def test_negative_clamped_to_zero(self):
        assert _parse_auto_resume(["--auto-resume", "-5"]) == 0


def test_unknown_subcommand_is_refused_before_gui_bootstrap(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["corecycler", "bogus"])
    monkeypatch.setattr(
        main_mod,
        "_bootstrap_sudo_session",
        lambda: (_ for _ in ()).throw(AssertionError("unknown command reached GUI bootstrap")),
    )

    assert main_mod.main() == 5
    assert "bogus" in capsys.readouterr().err


def test_tune_refuses_when_capability_confinement_is_unproven(monkeypatch, capsys):
    from corecycler import capabilities, cli

    monkeypatch.setattr(sys, "argv", ["corecycler", "tune"])
    monkeypatch.setattr(main_mod, "setup_logging", lambda: None)
    monkeypatch.setattr(capabilities, "confine", lambda: capabilities.ConfinementResult(safe=False, has_rawio=True))
    monkeypatch.setattr(cli, "cli_main", lambda _argv: (_ for _ in ()).throw(AssertionError("unsafe tune started")))

    assert main_mod.main() == cli.EXIT_REFUSED
    assert "capability confinement" in capsys.readouterr().err


def test_gui_refuses_when_capability_confinement_is_unproven(monkeypatch, capsys):
    from corecycler import capabilities, cli

    monkeypatch.setattr(sys, "argv", ["corecycler"])
    monkeypatch.setattr(main_mod, "setup_logging", lambda: None)
    monkeypatch.setattr(capabilities, "confine", lambda: capabilities.ConfinementResult(safe=False, has_rawio=True))
    monkeypatch.setattr(
        main_mod,
        "_bootstrap_sudo_session",
        lambda: (_ for _ in ()).throw(AssertionError("unsafe GUI started")),
    )

    assert main_mod.main() == cli.EXIT_REFUSED
    assert "capability confinement" in capsys.readouterr().err
