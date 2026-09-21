"""Abstract base class for stress test backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING

from corecycler.config import tools

if TYPE_CHECKING:
    from pathlib import Path

# Return codes indicating the process was intentionally killed by the scheduler
KILLED_BY_US_CODES: frozenset[int] = frozenset({-9, -15, 137, 143})

# Signal codes indicating CPU instability (CO too aggressive, hardware fault).
# SIGILL/SIGFPE are included: aggressive undervolting corrupts instruction decode
# and FPU results, so a worker dying with -4/-8 is instability, not a clean exit.
CRASH_SIGNALS: dict[int, str] = {
    -4: "SIGILL",
    -5: "SIGTRAP",
    -6: "SIGABRT",
    -7: "SIGBUS",
    -8: "SIGFPE",
    -11: "SIGSEGV",
}


class StressMode(Enum):
    SSE = auto()
    AVX = auto()
    AVX2 = auto()
    AVX512 = auto()
    CUSTOM = auto()


class FFTPreset(Enum):
    SMALLEST = "smallest"  # 4K-21K
    SMALL = "small"  # 36K-248K
    LARGE = "large"  # 426K-8192K
    HUGE = "huge"  # 8960K-65536K
    ALL = "all"  # 4K-65536K
    MODERATE = "moderate"  # 1344K-4096K
    HEAVY = "heavy"  # 4K-1344K
    HEAVY_SHORT = "heavy_short"  # 4K-160K
    CUSTOM = "custom"


@dataclass(frozen=True, slots=True)
class DutyCycle:
    """Millisecond-or-finer load/idle cycling applied to a running payload.

    ``burst_us``/``idle_us`` drive a fixed metronome. ``random_phases`` instead
    draws a fresh phase length and target duty every phase, so utilisation
    wanders the whole 0-100% range the way real work does; a phase boundary
    from idle straight to full load is the deepest Vmin transient available.
    """

    burst_us: int = 5000
    idle_us: int = 5000
    random_phases: bool = False


@dataclass(slots=True)
class StressConfig:
    mode: StressMode = StressMode.SSE
    fft_preset: FFTPreset = FFTPreset.SMALL
    fft_min: int | None = None  # custom range
    fft_max: int | None = None
    threads: int = 1
    memory_mb: int | None = None  # for linpack-style tests
    # Backend-specific algorithm selection (y-cruncher component tests). None
    # means the backend picks; an explicit tuple is what per-regime confidence
    # accounting requires, since "whatever the tool defaults to" is not a
    # workload identity.
    tests: tuple[str, ...] | None = None
    duty_cycle: DutyCycle | None = None
    test_seconds: int | None = None


@dataclass(slots=True)
class StressResult:
    core_id: int
    passed: bool
    duration_seconds: float
    error_message: str | None = None
    error_type: str | None = None  # "computation", "mce", "timeout", "crash"
    iterations_completed: int = 0
    last_fft_size: int | None = None


class StressBackend(ABC):
    """Base class for stress test backends (mprime, y-cruncher, stress-ng)."""

    name: str = "base"

    def __init__(self) -> None:
        self._binary: str | None = None

    def is_available(self) -> bool:
        """Locate the backend binary through the one resolver (see config.tools)."""
        resolution = self.resolution()
        self._binary = str(resolution.path) if resolution.path else None
        return self._binary is not None

    def resolution(self) -> tools.Resolution:
        """Where this backend's binary resolved from, or why it did not."""
        return tools.resolve(self.name)

    def require_binary(self) -> str:
        """The resolved binary path, or a RuntimeError naming the backend."""
        if not self._binary:
            self.is_available()
        if not self._binary:
            raise RuntimeError(f"{self.name} binary not found")
        return self._binary

    @abstractmethod
    def get_command(self, config: StressConfig, work_dir: Path) -> list[str]:
        """Build the command line for the stress test (without the containment prefix)."""

    @abstractmethod
    def parse_output(self, stdout: str, stderr: str, returncode: int) -> tuple[bool, str | None]:
        """Parse stress test output. Returns (passed, error_message)."""

    @abstractmethod
    def get_supported_modes(self) -> list[StressMode]:
        """Return selectable stress modes implemented by this backend."""

    def instruction_set(self, config: StressConfig) -> StressMode | None:
        """Return the ISA constraint the backend can enforce, if any."""
        return None

    def workload(self, config: StressConfig) -> tuple[str, ...]:
        """Return the concrete backend workload independently of its ISA target."""
        return config.tests or ()

    def get_supported_fft_presets(self) -> list[FFTPreset]:
        """Return list of FFT presets this backend supports. Override if applicable."""
        return []

    def default_memory_mb(self, lanes: int = 1) -> int | None:
        """Return the per-lane share of a backend's default batch memory budget."""
        return None

    def prepare(self, work_dir: Path, config: StressConfig) -> None:  # noqa: B027
        """Prepare working directory and config files before running. Override if needed."""

    def assert_prepared(self, work_dir: Path) -> None:  # noqa: B027
        """Raise OSError when the prepared state a launch depends on is not readable.

        A backend that launches against absent config silently falls back to its
        own defaults (mprime: one self-pinned worker per detected core), so the
        launch must be refused instead. Override where config files exist."""

    def poll_errors(self, work_dir: Path) -> str | None:
        """Check for fatal errors mid-run (e.g. a results file the stress tool
        appends to while continuing). Returns an error message or None.
        Override for backends whose errors do not stop the process."""
        return None

    def cleanup(self, work_dir: Path, *, preserve_on_error: bool = False) -> None:  # noqa: B027
        """Clean up after test run. Override if needed.

        Args:
            preserve_on_error: If True, keep diagnostic files (results, logs)
                for post-mortem analysis of failures.
        """

    def indefinite_exit_verdict(self, returncode: int) -> tuple[bool, str | None]:
        """Classify termination after backend-specific errors have been excluded."""
        if returncode in KILLED_BY_US_CODES:
            return True, None
        signal_name = CRASH_SIGNALS.get(returncode)
        if signal_name:
            return False, f"{self.name} crashed with {signal_name} (exit {returncode})"
        return False, f"{self.name} exited with code {returncode} - verdict unavailable"

    @staticmethod
    def classify_exit_code(returncode: int) -> str | None:
        """Classify a process exit code."""
        if returncode in KILLED_BY_US_CODES:
            return "killed_by_us"
        signal_name = CRASH_SIGNALS.get(returncode)
        if signal_name:
            return f"crash:{signal_name}"
        return None
