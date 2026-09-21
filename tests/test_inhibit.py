"""Suspend inhibition: one lock behind many owners, and never a raise."""

from __future__ import annotations

import subprocess
import sys as _sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

_sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from corecycler import inhibit

# The suite-wide fixture stubs spawning out; these tests are the ones that
# exercise it, so they hold the real function from import time.
_real_spawn = inhibit._spawn


def _fake_proc(*, wait_for_release: bool = False) -> MagicMock:
    proc = MagicMock(spec=subprocess.Popen)
    proc.pid = 4242
    proc.stdin = MagicMock()
    if wait_for_release:
        released = threading.Event()
        proc.stdin.close.side_effect = released.set
        proc.wait.side_effect = lambda timeout=None: (
            0 if released.wait(timeout) else (_ for _ in ()).throw(subprocess.TimeoutExpired("fake", timeout))
        )
    return proc


class TestSpawn:
    def test_missing_binary_leaves_the_run_unprotected(self):
        with patch("corecycler.config.tools.shutil.which", return_value=None):
            assert _real_spawn("why") is None

    def test_blocks_sleep_and_idle_with_the_reason(self):
        with (
            patch("corecycler.config.tools.shutil.which", return_value="/usr/bin/systemd-inhibit"),
            patch("corecycler.inhibit.subprocess.Popen", return_value=_fake_proc()) as popen,
        ):
            assert _real_spawn("tuning") is not None
        argv = popen.call_args[0][0]
        assert argv[0] == "/usr/bin/systemd-inhibit"
        assert "--what=sleep:idle" in argv
        assert "--mode=block" in argv
        assert "--why=tuning" in argv
        assert argv[-3:] == list(inhibit._PARK)
        assert popen.call_args.kwargs["stdin"] is subprocess.PIPE
        assert popen.call_args.kwargs["start_new_session"] is True

    def test_launch_failure_is_swallowed(self):
        with (
            patch("corecycler.config.tools.shutil.which", return_value="/usr/bin/systemd-inhibit"),
            patch("corecycler.inhibit.subprocess.Popen", side_effect=OSError("boom")),
        ):
            assert _real_spawn("why") is None

    def test_the_parked_command_exits_on_stdin_eof(self):
        proc = subprocess.Popen(inhibit._PARK, stdin=subprocess.PIPE)
        proc.stdin.close()
        assert proc.wait(timeout=30) == 0


class TestTerminate:
    def test_closing_stdin_ends_the_lock(self):
        proc = _fake_proc()
        inhibit._terminate(proc)
        proc.stdin.close.assert_called_once()
        proc.wait.assert_called_once()

    def test_a_deaf_inhibitor_is_killed(self):
        proc = _fake_proc()
        proc.wait.side_effect = [subprocess.TimeoutExpired("systemd-inhibit", 5), None]
        with (
            patch("corecycler.inhibit.os.getpgid", return_value=4242),
            patch("corecycler.inhibit.os.killpg") as killpg,
        ):
            inhibit._terminate(proc)
        assert killpg.call_args[0][0] == 4242

    def test_a_closed_pipe_is_not_an_error(self):
        proc = _fake_proc()
        proc.stdin.close.side_effect = OSError("already gone")
        inhibit._terminate(proc)
        proc.wait.assert_called_once()

    def test_a_process_without_stdin_still_waits(self):
        proc = _fake_proc()
        proc.stdin = None
        inhibit._terminate(proc)
        proc.wait.assert_called_once()


class TestSharedLock:
    def test_reaps_an_inhibitor_that_exits_before_release(self):
        shared = inhibit._SharedLock()
        proc = _fake_proc()
        proc.wait.return_value = 7
        with patch("corecycler.inhibit._spawn", return_value=proc):
            shared.claim("rejected")

        deadline = time.monotonic() + 5
        while shared.held and time.monotonic() < deadline:
            time.sleep(0.01)

        assert not shared.held
        proc.wait.assert_called_once_with()
        shared.drop()

    def test_watcher_failure_leaves_the_inhibitor_for_release_to_reap(self, caplog):
        shared = inhibit._SharedLock()
        proc = _fake_proc()
        proc.wait.side_effect = [OSError("wait failed"), None]
        with patch("corecycler.inhibit._spawn", return_value=proc), caplog.at_level("DEBUG"):
            shared.claim("rejected")
            deadline = time.monotonic() + 5
            while "watcher failed: wait failed" not in caplog.text and time.monotonic() < deadline:
                time.sleep(0.01)

        assert shared.held
        assert "watcher failed: wait failed" in caplog.text

        shared.drop()

        assert not shared.held
        proc.stdin.close.assert_called_once()
        assert proc.wait.call_count == 2

    def test_second_owner_reuses_the_one_process(self):
        shared = inhibit._SharedLock()
        proc = _fake_proc(wait_for_release=True)
        with patch("corecycler.inhibit._spawn", return_value=proc) as spawn:
            shared.claim("first")
            shared.claim("second")
        assert spawn.call_count == 1
        assert shared.held

    def test_the_lock_outlives_every_owner_but_the_last(self):
        shared = inhibit._SharedLock()
        proc = _fake_proc(wait_for_release=True)
        with patch("corecycler.inhibit._spawn", return_value=proc):
            shared.claim("first")
            shared.claim("second")
        shared.drop()
        assert shared.held
        proc.stdin.close.assert_not_called()
        shared.drop()
        assert not shared.held
        proc.stdin.close.assert_called_once()

    def test_dropping_an_unavailable_lock_does_nothing(self):
        shared = inhibit._SharedLock()
        with patch("corecycler.inhibit._spawn", return_value=None):
            shared.claim("why")
        shared.drop()
        assert not shared.held


class TestSleepInhibitor:
    def test_holding_twice_claims_once(self):
        shared = inhibit._SharedLock()
        proc = _fake_proc(wait_for_release=True)
        with (
            patch("corecycler.inhibit._shared", shared),
            patch("corecycler.inhibit._spawn", return_value=proc) as spawn,
        ):
            owner = inhibit.SleepInhibitor("why")
            owner.hold()
            owner.hold()
            assert spawn.call_count == 1
            owner.release()
            assert not shared.held

    def test_releasing_what_was_never_held_does_nothing(self):
        shared = inhibit._SharedLock()
        with patch("corecycler.inhibit._shared", shared):
            inhibit.SleepInhibitor("why").release()
        assert not shared.held

    def test_the_context_manager_releases_on_error(self):
        shared = inhibit._SharedLock()
        proc = _fake_proc(wait_for_release=True)
        with (
            patch("corecycler.inhibit._shared", shared),
            patch("corecycler.inhibit._spawn", return_value=proc),
        ):
            try:
                with inhibit.SleepInhibitor("why") as owner:
                    assert owner is not None
                    assert shared.held
                    raise RuntimeError("test blew up")
            except RuntimeError:
                pass
        assert not shared.held
