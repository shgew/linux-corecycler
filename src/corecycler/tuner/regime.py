"""Regimes, offset masks, and the workload battery.

Curve Optimizer instability is condition-dependent: a core that survives hours
of heavy AVX can die in seconds at high boost under light load, or while merely
sitting idle with its offset resident. No single stress workload covers all of
it, so coverage comes from layering distinct regimes rather than from crowning
one workload the winner.

Four regimes are enough to span the failure space, and few enough that each one
accumulates meaningful confidence:

``BOOST``
    One thread, light instruction mix, so the core reaches its single-core
    ceiling. Catches margin that only fails at high frequency and low current.
``CURRENT``
    Heavy vectorised work on every SMT sibling. Catches load Vdroop in the
    low-frequency, high-current, high-heat corner.
``TRANSIENT``
    Sub-millisecond idle-to-load swings. The clock ramps faster than the
    voltage rail settles, so Vmin is violated momentarily. How fast the clock
    swings matters more here than how high it peaks.
``COUPLED``
    Memory-coupled work. Catches the memory controller, and doubles as the
    check that separates a core fault from a platform fault.

Backend, instruction set, FFT preset and thread count are *implementations* of
a regime, not regimes themselves, so several workloads can bank into one.

Orthogonal to the regime is the offset mask. ``ISOLATED`` parks every other
core at stock, which is what makes a failure attributable to the core under
test. ``LIVE`` leaves every other core at its own best-known offset, which is
the only condition the machine ever actually runs in. Isolated evidence is a
hypothesis about a core's limit; only live evidence is allowed to bank
confidence, because a search that proves an offset with the rest of the vector
at stock has proven it in a world that does not exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


class Regime(StrEnum):
    BOOST = "boost"
    CURRENT = "current"
    TRANSIENT = "transient"
    COUPLED = "coupled"


class Mask(StrEnum):
    #: Every other core at stock. Attributable, but not a real operating point.
    ISOLATED = "isolated"
    #: Every other core at its best-known offset. The condition that ships.
    LIVE = "live"


class Profile(StrEnum):
    SUSTAINED = "sustained"
    SPECTRUM = "spectrum"
    TRANSIENT = "transient"


@dataclass(frozen=True, slots=True)
class Workload:
    """One concrete implementation of a regime."""

    regime: Regime
    backend: str
    stress_mode: str
    fft_preset: str
    threads: int | None = None
    profile: Profile = Profile.SUSTAINED
    tests: tuple[str, ...] | None = None
    memory_coupled: bool = False

    @property
    def label(self) -> str:
        return workload_label(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "regime": str(self.regime),
            "backend": self.backend,
            "stress_mode": self.stress_mode,
            "fft_preset": self.fft_preset,
            "profile": str(self.profile),
        }
        if self.threads is not None:
            data["threads"] = self.threads
        if self.tests is not None:
            data["tests"] = list(self.tests)
        if self.memory_coupled:
            data["memory_coupled"] = True
        return data

    @classmethod
    def from_dict(cls, data: dict) -> Workload:
        return cls(
            regime=Regime(data["regime"]),
            backend=str(data["backend"]),
            stress_mode=str(data["stress_mode"]),
            fft_preset=str(data["fft_preset"]),
            threads=data.get("threads"),
            profile=Profile(data.get("profile", "sustained")),
            tests=tuple(data["tests"]) if data.get("tests") else None,
            memory_coupled=bool(data.get("memory_coupled", False)),
        )


def workload_label(data: Mapping[str, object]) -> str:
    """Human name of a serialized workload, test-log row or slot description."""
    parts = [str(data[key]) for key in ("backend", "stress_mode", "fft_preset") if data.get(key)]
    if data.get("threads"):
        parts.append(f"{data['threads']}T")
    tests = data.get("tests")
    if tests:
        parts.append("/".join(str(test) for test in tests))
    profile = data.get("profile")
    if profile and profile != Profile.SUSTAINED:
        parts.append(str(profile))
    return " ".join(parts)


#: The shipped battery. One entry minimum per regime; several entries in a
#: regime rotate freely, since they all bank into the same confidence bucket.
DEFAULT_BATTERY: tuple[Workload, ...] = (
    Workload(Regime.BOOST, "mprime", "SSE", "SMALL", threads=1),
    Workload(Regime.BOOST, "ycruncher", "AVX2", "SMALL", threads=1, tests=("BKT",)),
    Workload(Regime.CURRENT, "mprime", "AVX2", "SMALL", threads=2),
    Workload(Regime.CURRENT, "ycruncher", "AVX2", "SMALL", threads=2, tests=("FFTv4", "N63")),
    Workload(Regime.TRANSIENT, "mprime", "AVX2", "SMALL", threads=2, profile=Profile.TRANSIENT),
    Workload(Regime.COUPLED, "mprime", "AVX2", "LARGE", threads=2, memory_coupled=True),
    Workload(Regime.COUPLED, "ycruncher", "AVX2", "SMALL", threads=2, tests=("VT3",), memory_coupled=True),
)

#: Coarse search runs only the regimes most likely to fail fast, because a
#: short pass proves nothing anyway and a short fail is conclusive.
COARSE_REGIMES: tuple[Regime, ...] = (Regime.CURRENT, Regime.TRANSIENT)

VALID_TAGS: frozenset[str] = frozenset({"BKT", "BBP", "SFTv4", "SNT", "SVT", "FFTv4", "N63", "VT3"})


def workload_errors(name: str, index: int, item: object) -> list[str]:
    """Validate one serialized workload entry."""
    if not isinstance(item, dict):
        return [f"{name}[{index}] must be a dict"]
    try:
        workload_regime = Regime(item.get("regime"))
    except (TypeError, ValueError):
        return [f"{name}[{index}].regime must be one of {sorted(r.value for r in Regime)}"]
    if not all(isinstance(item.get(k), str) and item[k].strip() for k in ("backend", "stress_mode", "fft_preset")):
        return [f"{name}[{index}] requires non-blank string backend, stress_mode, fft_preset"]
    try:
        profile = Profile(item.get("profile", "sustained"))
    except (TypeError, ValueError):
        return [f"{name}[{index}].profile must be one of {sorted(p.value for p in Profile)}"]
    if workload_regime is Regime.TRANSIENT and profile is not Profile.TRANSIENT:
        return [f"{name}[{index}].regime transient requires profile transient"]
    if profile is Profile.TRANSIENT and workload_regime is not Regime.TRANSIENT:
        return [f"{name}[{index}].profile transient requires regime transient"]
    threads = item.get("threads")
    if not (threads is None or (type(threads) is int and threads >= 1)):
        return [f"{name}[{index}].threads must be a positive integer"]
    tests = item.get("tests")
    if tests is not None:
        if not isinstance(tests, list) or not all(isinstance(t, str) for t in tests):
            return [f"{name}[{index}].tests must be a list of strings"]
        unknown = sorted(set(tests) - VALID_TAGS)
        if unknown:
            return [f"{name}[{index}].tests has unknown tags: {', '.join(unknown)}"]
    return []


def regimes_covered(battery: list[dict]) -> set[Regime]:
    return {Regime(entry["regime"]) for entry in battery}
