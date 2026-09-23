"""y-cruncher stress test backend.

The stress command line has no thread option: y-cruncher sizes its pool from
the machine topology, ignores the cpuset, and tries to pin one thread to every
logical CPU. Inside a lane's cgroup that crams a thread per machine CPU onto the
lane and prints "Failed to set core affinity" for the rest. Only a config file
can name the CPUs, so every launch runs one written for its lane.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING

from corecycler.engine.backends import register_backend

from .base import StressBackend, StressConfig, StressMode

if TYPE_CHECKING:
    from pathlib import Path

CONFIG_NAME = "stress.cfg"
_HEADLESS_FLAGS = ("skip-warnings", "pause:-2", "status:none")
_PER_TEST_SECONDS = 30
# Two lane threads at 32 MiB stay inside one CCD's L3, so the core rather than
# DRAM bounds the test; 256 MiB per thread spills far past any L3.
_CACHE_MIB_PER_THREAD = 32
_COUPLED_MIB_PER_THREAD = 256

VALID_COMPONENT_TESTS: frozenset[str] = frozenset({"BKT", "BBP", "SFTv4", "SNT", "SVT", "FFTv4", "N63", "VT3"})

MODE_TO_ALGORITHMS: dict[StressMode, tuple[str, ...]] = {
    StressMode.SSE: ("BKT",),
    StressMode.AVX: ("BKT", "BBP", "SFTv4", "SNT", "SVT"),
    StressMode.AVX2: ("BKT", "FFTv4", "N63", "VT3"),
    StressMode.AVX512: ("BKT", "FFTv4", "N63", "VT3"),
    StressMode.CUSTOM: (),
}

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

_UNPINNED_RE = re.compile(r"Failed to set core affinity to core: *\d+", re.IGNORECASE)

_COMPUTATION_PATTERNS: tuple[str, ...] = (
    r"Error\(s\) encountered",
    r"Coefficient is too large",
    r"Checksum mismatch",
)

_ERROR_PATTERNS: tuple[str, ...] = (
    r"Invalid Parameter",
    r"\bFAIL(?:ED)?\b",
    r"(?<!Stop on )\bError\b(?!\s+Checking)",
)


def render_config(config: StressConfig) -> str:
    """The stress-test config y-cruncher runs for one lane."""
    if not config.cpus:
        raise RuntimeError("refusing to launch y-cruncher without lane CPUs: it would pin a thread to every CPU")
    tests = config.tests if config.tests is not None else MODE_TO_ALGORITHMS.get(config.mode, ())
    unknown_tests = sorted(set(tests) - VALID_COMPONENT_TESTS)
    if unknown_tests:
        raise ValueError(f"Unknown y-cruncher component test(s): {', '.join(unknown_tests)}")
    if not tests:
        raise RuntimeError(f"no y-cruncher component tests for mode {config.mode.name}")
    if config.memory_mb and config.memory_mb > 0:
        memory_mib = config.memory_mb
    else:
        per_thread = _COUPLED_MIB_PER_THREAD if config.memory_coupled else _CACHE_MIB_PER_THREAD
        memory_mib = per_thread * len(config.cpus)
    test_seconds = max(config.test_seconds if config.test_seconds is not None else _PER_TEST_SECONDS, 1)
    test_lines = "".join(f'            "{test}"\n' for test in tests)
    return (
        "{\n"
        '    Action : "StressTest"\n'
        "    StressTest : {\n"
        '        AllocateLocally : "true"\n'
        f"        LogicalCores : [{' '.join(str(cpu) for cpu in config.cpus)}]\n"
        f"        TotalMemory : {memory_mib * 1024 * 1024}\n"
        f"        SecondsPerTest : {test_seconds}\n"
        "        SecondsTotal : 0\n"
        '        StopOnError : "true"\n'
        "        Tests : [\n"
        f"{test_lines}"
        "        ]\n"
        "    }\n"
        "}\n"
    )


@register_backend("y-cruncher")
class YCruncherBackend(StressBackend):
    name = "y-cruncher"

    def get_command(self, config: StressConfig, work_dir: Path) -> list[str]:
        return [self.require_binary(), *_HEADLESS_FLAGS, "config", str(work_dir / CONFIG_NAME)]

    def get_supported_modes(self) -> list[StressMode]:
        return [StressMode.SSE, StressMode.AVX, StressMode.AVX2, StressMode.AVX512]

    def workload(self, config: StressConfig) -> tuple[str, ...]:
        return config.tests if config.tests is not None else MODE_TO_ALGORITHMS.get(config.mode, ())

    def prepare(self, work_dir: Path, config: StressConfig) -> None:
        text = render_config(config)
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / CONFIG_NAME).write_text(text)

    def assert_prepared(self, work_dir: Path) -> None:
        path = work_dir / CONFIG_NAME
        if not path.is_file() or not os.access(path, os.R_OK):
            raise OSError(
                f"refusing to launch y-cruncher: {path} is missing or unreadable, "
                "and without it y-cruncher pins a thread to every CPU"
            )

    def parse_output(self, stdout: str, stderr: str, returncode: int) -> tuple[bool, str | None]:
        combined = _ANSI_RE.sub("", stdout + "\n" + stderr)

        unpinned = _UNPINNED_RE.search(combined)
        if unpinned:
            return False, f"harness error: y-cruncher could not keep a thread on its lane ({unpinned.group(0)})"

        for pattern in _COMPUTATION_PATTERNS:
            match = re.search(pattern, combined, re.IGNORECASE)
            if match:
                return False, f"y-cruncher computation error: {match.group(0)}"

        for pattern in _ERROR_PATTERNS:
            match = re.search(pattern, combined, re.IGNORECASE)
            if match:
                return False, f"y-cruncher error: {match.group(0)}"

        return self.indefinite_exit_verdict(returncode)

    def cleanup(self, work_dir: Path, *, preserve_on_error: bool = False) -> None:
        pass
