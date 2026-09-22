"""Contract tests that run a real y-cruncher binary.

They fail loudly if an upstream y-cruncher update breaks a backend assumption
(config format, algorithm names, output format, or the kill-signal exit code),
rather than letting the fixture-based unit tests pass on stale assumptions.
The tests skip when no y-cruncher binary is present unless hardware-contract
mode requires the resource.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from corecycler.engine import containment
from corecycler.engine.backends.base import KILLED_BY_US_CODES, StressConfig, StressMode
from corecycler.engine.backends.ycruncher import CONFIG_NAME, VALID_COMPONENT_TESTS, YCruncherBackend
from tests._contract_hw import require

pytestmark = [pytest.mark.slow, pytest.mark.contract]

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _discover_binary() -> str | None:
    override = os.environ.get("YCRUNCHER_BIN")
    if override and Path(override).is_file():
        return override
    backend = YCruncherBackend()
    if backend.is_available():
        return backend._binary
    return None


_BINARY = _discover_binary()
_COMPONENT_TESTS = sorted(VALID_COMPONENT_TESTS)


@pytest.fixture(autouse=True)
def _require_ycruncher() -> None:
    require(
        _BINARY is not None,
        "no y-cruncher binary (set YCRUNCHER_BIN or put y-cruncher on PATH)",
    )


def _lane_cpus(count: int = 1) -> tuple[int, ...]:
    return tuple(sorted(os.sched_getaffinity(0))[:count])


def _backend() -> YCruncherBackend:
    backend = YCruncherBackend()
    backend._binary = _BINARY
    return backend


def _run(cmd: list[str], work_dir: Path, run_seconds: float) -> tuple[int, str]:
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=work_dir,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
    )
    time.sleep(run_seconds)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    try:
        out, _ = proc.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        out, _ = proc.communicate()
    return proc.returncode, _ANSI_RE.sub("", out)


def _stress(work_dir: Path, run_seconds: float, *, prefix: tuple[str, ...] = (), **config: object) -> tuple[int, str]:
    stress_config = StressConfig(**{"cpus": _lane_cpus(), "test_seconds": 2, **config})
    backend = _backend()
    backend.prepare(work_dir, stress_config)
    backend.assert_prepared(work_dir)
    return _run([*prefix, *backend.get_command(stress_config, work_dir)], work_dir, run_seconds)


class TestYCruncherBinaryContract:
    def test_backend_command_reaches_stress_loop(self, tmp_path):
        _rc, out = _stress(tmp_path, 6, mode=StressMode.SSE)
        assert "Exception Encountered" not in out
        assert "Press ENTER" not in out
        assert "Start Stress-Testing!" in out
        assert "Iteration:" in out

    def test_scheduler_kill_yields_killed_by_us_code(self, tmp_path):
        rc, _out = _stress(tmp_path, 4, tests=("BKT",))
        assert rc in KILLED_BY_US_CODES, (
            f"SIGTERM produced exit {rc}; the backend treats any non-killed exit as a failure, "
            "so a trapped signal here would flip every real run to a false verdict"
        )

    def test_pass_line_format_present(self, tmp_path):
        _rc, out = _stress(tmp_path, 6, tests=("BKT",))
        assert re.search(r"Running\s+BKT:\s*Passed", out), (
            "y-cruncher stopped emitting the 'Running <algo>: Passed' line the backend was built against"
        )

    @pytest.mark.parametrize("algo", _COMPONENT_TESTS)
    def test_valid_component_test_still_accepted(self, tmp_path, algo):
        _rc, out = _stress(tmp_path, 3, tests=(algo,))
        assert "Exception Encountered" not in out, f"y-cruncher rejected {algo!r}; upstream may have renamed it"
        assert "Start Stress-Testing!" in out

    def test_an_invalid_component_test_is_never_a_pass(self, tmp_path):
        backend = _backend()
        config = StressConfig(cpus=_lane_cpus(), tests=("BKT",), test_seconds=2)
        backend.prepare(tmp_path, config)
        path = tmp_path / CONFIG_NAME
        path.write_text(path.read_text().replace('"BKT"', '"NOSUCHTEST"'))
        rc, out = _run(backend.get_command(config, tmp_path), tmp_path, 3)
        assert "InvalidParametersException" in out
        passed, _msg = backend.parse_output(out, "", rc)
        assert not passed

    def test_parse_output_agrees_with_real_killed_run(self, tmp_path):
        rc, out = _stress(tmp_path, 5, tests=("BKT",))
        passed, msg = YCruncherBackend().parse_output(out, "", rc)
        assert passed, msg

    def test_config_keeps_every_thread_on_a_contained_lane(self, tmp_path):
        """Inside the lane's cpuset, the stress command line ran one thread per
        machine CPU and failed to pin all but the lane's; the config must not."""
        require(containment.available_mechanism() is not None, "no systemd cgroup containment mechanism")
        cpus = _lane_cpus(2)
        contained = containment.contain(cpus)
        rc, out = _stress(tmp_path, 6, prefix=tuple(contained.prefix), cpus=cpus, tests=("BKT",))
        assert re.search(rf"Logical Cores:\s+{len(cpus)}\s*$", out, re.MULTILINE), out
        passed, msg = YCruncherBackend().parse_output(out, "", rc)
        assert passed, msg
