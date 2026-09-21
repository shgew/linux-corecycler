"""y-cruncher stress test backend."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from corecycler.engine.backends import register_backend

from .base import StressBackend, StressConfig, StressMode

if TYPE_CHECKING:
    from pathlib import Path

_HEADLESS_FLAGS = ("skip-warnings", "pause:-2", "status:none")
_DEFAULT_MEMORY_MIB = 1024
_PER_TEST_SECONDS = 30

VALID_COMPONENT_TESTS: frozenset[str] = frozenset({"BKT", "BBP", "SFTv4", "SNT", "SVT", "FFTv4", "N63", "VT3"})

MODE_TO_ALGORITHMS: dict[StressMode, tuple[str, ...]] = {
    StressMode.SSE: ("BKT",),
    StressMode.AVX: ("BKT", "BBP", "SFTv4", "SNT", "SVT"),
    StressMode.AVX2: ("BKT", "FFTv4", "N63", "VT3"),
    StressMode.AVX512: ("BKT", "FFTv4", "N63", "VT3"),
    StressMode.CUSTOM: (),
}

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

_ERROR_PATTERNS: tuple[str, ...] = (
    r"Error\(s\) encountered",
    r"Coefficient is too large",
    r"Invalid Parameter",
    r"Checksum mismatch",
    r"\bFAIL(?:ED)?\b",
    r"(?<!Stop on )\bError\b(?!\s+Checking)",
)


@register_backend("y-cruncher")
class YCruncherBackend(StressBackend):
    name = "y-cruncher"

    def get_command(self, config: StressConfig, work_dir: Path) -> list[str]:
        algorithms = config.tests if config.tests is not None else MODE_TO_ALGORITHMS.get(config.mode, ())
        unknown_tests = sorted(set(algorithms) - VALID_COMPONENT_TESTS)
        if unknown_tests:
            raise ValueError(f"Unknown y-cruncher component test(s): {', '.join(unknown_tests)}")
        binary = self.require_binary()
        memory_mib = config.memory_mb if config.memory_mb and config.memory_mb > 0 else self.default_memory_mb()
        test_seconds = max(config.test_seconds if config.test_seconds is not None else _PER_TEST_SECONDS, 1)
        cmd = [
            binary,
            *_HEADLESS_FLAGS,
            "stress",
            f"-M:{memory_mib}M",
            f"-D:{test_seconds}",
        ]
        cmd.extend(algorithms)
        return cmd

    def get_supported_modes(self) -> list[StressMode]:
        return [StressMode.SSE, StressMode.AVX, StressMode.AVX2, StressMode.AVX512]

    def workload(self, config: StressConfig) -> tuple[str, ...]:
        return config.tests if config.tests is not None else MODE_TO_ALGORITHMS.get(config.mode, ())

    def default_memory_mb(self, lanes: int = 1) -> int:
        return max(1, _DEFAULT_MEMORY_MIB // max(1, lanes))

    def prepare(self, work_dir: Path, config: StressConfig) -> None:
        work_dir.mkdir(parents=True, exist_ok=True)

    def parse_output(self, stdout: str, stderr: str, returncode: int) -> tuple[bool, str | None]:
        combined = _ANSI_RE.sub("", stdout + "\n" + stderr)

        for pattern in _ERROR_PATTERNS:
            match = re.search(pattern, combined, re.IGNORECASE)
            if match:
                return False, f"y-cruncher error: {match.group(0)}"

        return self.indefinite_exit_verdict(returncode)

    def cleanup(self, work_dir: Path, *, preserve_on_error: bool = False) -> None:
        pass
