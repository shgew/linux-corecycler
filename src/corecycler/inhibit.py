"""Best-effort suspend inhibition while a run is in flight. Never raises.

A tuning session is hours in which the machine looks idle to logind: nobody
touches the keyboard, and an idle soak runs no load at all. Suspending in the
middle of one loses the session and leaves an unproven Curve Optimizer offset
resident, so every path that runs load - or deliberately watches an idle
machine - holds a logind sleep+idle lock for as long as it lasts.

A machine with no systemd-inhibit, or a logind that refuses the lock, is a
normal condition: the run carries on unprotected and says so at debug.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import subprocess
import sys
import threading

from corecycler.config import tools

log = logging.getLogger(__name__)

_WHO = "CoreCycler"
_WHAT = "sleep:idle"
RELEASE_TIMEOUT = 5.0

# systemd-inhibit owns the lock only for as long as the command it runs, and
# this one does nothing but wait on stdin: closing the pipe ends it, and so
# does this process dying, since the pipe's only writer goes with it. The
# interpreter already running us is the one binary certain to be here.
_PARK = (sys.executable, "-c", "import sys; sys.stdin.read()")


def _spawn(reason: str) -> subprocess.Popen | None:
    resolution = tools.resolve("systemd-inhibit")
    if resolution.path is None:
        log.debug("systemd-inhibit unavailable (%s) - the machine may suspend mid-run", resolution.problem)
        return None
    try:
        return subprocess.Popen(
            [
                str(resolution.path),
                f"--who={_WHO}",
                f"--what={_WHAT}",
                f"--why={reason}",
                "--mode=block",
                *_PARK,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as e:
        log.debug("systemd-inhibit failed to start: %s", e)
        return None


def _terminate(proc: subprocess.Popen) -> None:
    if proc.stdin:
        with contextlib.suppress(OSError):
            proc.stdin.close()
    try:
        proc.wait(timeout=RELEASE_TIMEOUT)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError, ProcessLookupError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=RELEASE_TIMEOUT)


class _SharedLock:
    """One inhibitor process behind however many owners want it held."""

    def __init__(self) -> None:
        self._mutex = threading.Lock()
        self._holders = 0
        self._proc: subprocess.Popen | None = None

    def claim(self, reason: str) -> None:
        with self._mutex:
            self._holders += 1
            if self._proc is None:
                proc = _spawn(reason)
                self._proc = proc
                if proc is not None:
                    threading.Thread(target=self._watch, args=(proc,), daemon=True).start()
                    log.debug("sleep inhibited: %s", reason)

    def _watch(self, proc: subprocess.Popen) -> None:
        try:
            returncode = proc.wait()
        except Exception as exc:
            log.debug("sleep inhibitor watcher failed: %s", exc)
            return
        with self._mutex:
            if self._proc is proc:
                self._proc = None
        if returncode:
            log.debug("sleep inhibitor exited before release with status %s", returncode)

    def drop(self) -> None:
        with self._mutex:
            self._holders -= 1
            if self._holders > 0 or self._proc is None:
                return
            proc, self._proc = self._proc, None
        _terminate(proc)
        log.debug("sleep inhibitor released")

    @property
    def held(self) -> bool:
        with self._mutex:
            return self._proc is not None


_shared = _SharedLock()


class SleepInhibitor:
    """One owner's claim on the shared lock, idempotent at both ends.

    The tuner drives this from a status transition that repeats, and nested
    runs claim it while their session already holds it, so holding twice must
    claim once and releasing what was never held must do nothing.
    """

    __slots__ = ("_held", "_reason")

    def __init__(self, reason: str) -> None:
        self._reason = reason
        self._held = False

    def hold(self) -> None:
        if self._held:
            return
        self._held = True
        _shared.claim(self._reason)

    def release(self) -> None:
        if not self._held:
            return
        self._held = False
        _shared.drop()

    def __enter__(self) -> SleepInhibitor:
        self.hold()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()
