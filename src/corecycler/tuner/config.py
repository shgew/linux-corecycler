"""Tuner configuration — all search parameters with best-practice defaults."""

from __future__ import annotations

import dataclasses
import json
import math
from dataclasses import asdict, dataclass

from corecycler.tuner import regime


def _json_value_ok(default: object, value: object) -> bool:
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(default, float):
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and (not isinstance(value, float) or math.isfinite(value))
        )
    if isinstance(default, str):
        return isinstance(value, str)
    if isinstance(default, list):
        return isinstance(value, list)
    return value is None or isinstance(value, list)


def _workload_errors(name: str, i: int, item: object) -> list[str]:
    """Validate one workload entry shared by the battery and endurance lists."""
    return regime.workload_errors(name, i, item)


@dataclass(slots=True)
class TunerConfig:
    """Configuration for the automated PBO Curve Optimizer tuner.

    All fields have sensible defaults for a typical Zen 4/5 CPU.
    ``max_offset`` is auto-clamped to the CPU generation's CO range
    by the engine before use.
    """

    # Search parameters
    start_offset: int = 0
    coarse_step: int = 5
    fine_step: int = 1
    direction: int = -1  # -1 = negative (undervolting), +1 = positive

    # Test durations (seconds)
    search_duration_seconds: int = 60
    confirm_duration_seconds: int = 300
    validate_duration_seconds: int = 300

    # Limits
    max_offset: int = -50
    max_confirm_retries: int = 2

    # Behavior
    cores_to_test: list[int] | None = None  # None = all physical cores
    test_order: str = "sequential"  # sequential, round_robin, weakest_first, ccd_alternating, ccd_round_robin
    backend: str = "mprime"
    stress_mode: str = "SSE"
    fft_preset: str = "SMALL"

    stretch_threshold_pct: float = 3.0

    # Backoff algorithm
    midpoint_jump_threshold: int = 3  # after this many consecutive backoff fails, jump to midpoint

    # Safety
    abort_on_consecutive_failures: int = 0  # 0 = disabled

    # Contradictory failures pause without discarding the new fail bound. Zero disables.
    apparatus_failure_streak: int = 12

    # Resume-crash circuit breaker. After this many consecutive crash-resumes with
    # no surviving test in between, force every core to CO=0 (stock) and quarantine
    # the session instead of re-applying a profile that keeps crashing the machine.
    resume_crash_quarantine_threshold: int = 3

    # Crash hunt (evidence-based attribution). When a hard crash cannot be
    # attributed - no kernel MCE trace and multiple cores held offsets - the
    # tuner never guesses a culprit: it runs isolated per-core hunts
    # (candidate at its tuned value, every other core at stock) under
    # variable/idle load. Repeated fruitless hunts pause the session for the
    # user instead of continuing blind.
    max_unattributed_crash_hunts: int = 2

    # Fail closed when no CPU temperature sensor is readable: refuse to drive a
    # stress test with zero thermal protection unless the user explicitly opts in.
    allow_missing_thermal_sensor: bool = False

    # Thermal safety (plumbed into the per-core scheduler). A thermal stop is
    # not a stability verdict: the tuner retries the same offset rather than
    # recording a failure, up to max_thermal_retries, then aborts.
    max_temperature_c: float = 95.0  # stop a test if CPU exceeds this
    over_temp_grace_seconds: float = 3.0  # sustained over-limit time before stopping
    over_temp_hard_margin_c: float = 8.0  # instant stop at max + this (runaway)
    max_thermal_retries: int = 3  # retry same offset this many times before aborting
    thermal_cooldown_seconds: float = 5.0  # real wall-clock cooldown before a thermal retry

    # An apparatus fault (stall, external kill, unattributable machine check)
    # is not a stability verdict either: retry the same step without recording
    # a verdict, up to this many consecutive faults, then stop honestly.
    max_apparatus_retries: int = 3

    # Inherit current CO offsets from SMU as starting point
    inherit_current: bool = False

    # Automatically run multi-core validation after all cores are individually confirmed
    auto_validate: bool = True

    # Backoff tuning
    backoff_preconfirm_multiplier: float = 2.0

    # The workload battery. Every slot runs the regimes this list covers, so
    # an offset is never called good on the strength of one instruction mix.
    battery: list[dict] = dataclasses.field(default_factory=lambda: [w.to_dict() for w in regime.DEFAULT_BATTERY])
    # Coarse search runs only the fastest-failing regimes; the full battery
    # starts at fine search, where a wrong answer actually costs something.
    coarse_regimes: list[str] = dataclasses.field(default_factory=lambda: [str(r) for r in regime.COARSE_REGIMES])

    # No regime may be starved below this share of slot time, however poorly
    # it has performed: absence of failures in a regime is the thing being
    # proven, so it can never be scheduled away entirely.
    regime_floor_pct: float = 15.0

    # Unattributed-failure pipeline. The control run tests the competing
    # hypothesis that the platform, not the offsets, is at fault; it must
    # reproduce this many times at stock before we call it a platform fault.
    control_run_confirmations: int = 2
    # Probe budget: max(base, mttf_multiplier x observed time-to-failure),
    # grown per bisection level and again for the final single-core check,
    # because a false clean near the leaves costs the whole answer.
    probe_base_seconds: int = 1800
    probe_mttf_multiplier: float = 4.0
    probe_level_multiplier: float = 1.5
    probe_final_multiplier: float = 4.0
    # The statistical fallback only acts on a clear winner.
    suspicion_separation: float = 2.0
    suspicion_min_failures: int = 3

    # Annealing: banked clean time in EVERY regime earns one probe a step
    # deeper. A failed probe doubles the bar; this many strikes and the core
    # stops probing that depth.
    anneal_bank_hours: float = 6.0
    anneal_max_strikes: int = 3

    # Per-core time budget for search phases (seconds)
    max_core_time_seconds: int = 7200

    # Backoff steps after system crash (multiplied by fine_step direction)
    crash_penalty_steps: int = 3

    # Enable S4 rapid transition validation
    validate_transitions: bool = True

    # S5 per-core light-load spectrum slots (all offsets live): max-boost
    # bursts, load transitions and idle watch — the load class that exposes
    # marginality sustained stress cannot reach.
    validate_spectrum: bool = True
    spectrum_slot_seconds: int = 60

    # S6 all-core memory-load stress (all offsets live): one memory stressor
    # per core at once, catching CO marginality that only appears under
    # memory-controller load. Skipped if no memory stress tool is installed.
    validate_memory: bool = True

    # S7 real-world soak: after a clean validation pass, watch the kernel
    # error stream for this long with NO synthetic load while the machine is
    # used normally. Zero events = DONE.
    validate_soak: bool = True
    soak_duration_seconds: int = 1800

    # Perpetual endurance: the search never terminates, it converges and then
    # keeps proving the vector, annealing a step deeper whenever a core has
    # banked enough clean time in every regime. A failing slot demotes.
    endurance: bool = True
    endurance_workloads: list[dict] = dataclasses.field(
        default_factory=lambda: [w.to_dict() for w in regime.DEFAULT_BATTERY]
    )
    endurance_slot_seconds: int = 600
    endurance_slot_max_seconds: int = 3600

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, data: str) -> TunerConfig:
        """Reject malformed configuration rather than replacing requested safety limits."""
        d = json.loads(data)
        if not isinstance(d, dict):
            raise ValueError("tuner config must be a JSON object")
        unknown = sorted(d.keys() - set(cls.__slots__))
        if unknown:
            raise ValueError(f"unknown tuner config fields: {', '.join(unknown)}")
        config = cls(**d)
        errors = config.validate()
        if errors:
            raise ValueError("; ".join(errors))
        return config

    def validate(self) -> list[str]:
        """Return list of validation errors, empty if config is valid."""
        defaults = type(self)()
        if not isinstance(self.coarse_regimes, list):
            return ["coarse_regimes must be a list of regime names"]
        errors = [
            f"{field.name} has an invalid type or non-finite value"
            for field in dataclasses.fields(self)
            if not _json_value_ok(getattr(defaults, field.name), getattr(self, field.name))
        ]
        if errors:
            return errors
        if self.direction not in (-1, 1):
            errors.append(f"direction must be -1 or 1, got {self.direction}")
        if self.coarse_step < 1:
            errors.append(f"coarse_step must be >= 1, got {self.coarse_step}")
        if self.fine_step < 1:
            errors.append(f"fine_step must be >= 1, got {self.fine_step}")
        if self.fine_step > self.coarse_step:
            errors.append(f"fine_step ({self.fine_step}) must be <= coarse_step ({self.coarse_step})")
        if self.cores_to_test is not None:
            if not self.cores_to_test:
                errors.append("cores_to_test is empty - no cores to test")
            elif any(type(core) is not int or core < 0 for core in self.cores_to_test):
                errors.append("cores_to_test must contain non-negative integer core IDs")
            elif len(set(self.cores_to_test)) != len(self.cores_to_test):
                errors.append("cores_to_test contains duplicate core IDs")
        if self.search_duration_seconds < 1:
            errors.append("search_duration_seconds must be >= 1")
        if self.confirm_duration_seconds < 1:
            errors.append("confirm_duration_seconds must be >= 1")
        if not 1 <= self.crash_penalty_steps <= 10:
            errors.append("crash_penalty_steps must be 1-10")
        if not 1 <= self.resume_crash_quarantine_threshold <= 20:
            errors.append("resume_crash_quarantine_threshold must be 1-20")
        if not 30 <= self.spectrum_slot_seconds <= 600:
            errors.append("spectrum_slot_seconds must be 30-600")
        if not 60 <= self.soak_duration_seconds <= 14400:
            errors.append("soak_duration_seconds must be 60-14400")
        if not 1 <= self.max_unattributed_crash_hunts <= 10:
            errors.append("max_unattributed_crash_hunts must be 1-10")
        if not 0 <= self.apparatus_failure_streak <= 100:
            errors.append("apparatus_failure_streak must be 0-100 (0 disables)")
        if 0 < self.apparatus_failure_streak <= self.max_confirm_retries:
            errors.append(
                "apparatus_failure_streak must exceed max_confirm_retries (legitimate confirm retries would trip it)"
            )
        if not 1800 <= self.max_core_time_seconds <= 14400:
            errors.append("max_core_time_seconds must be 1800-14400")
        for i, entry in enumerate(self.battery):
            errors.extend(_workload_errors("battery", i, entry))
        if not errors and not self.battery:
            errors.append("battery must have at least one workload")
        if not errors:
            covered = regime.regimes_covered(self.battery)
            missing = sorted(str(r) for r in regime.Regime if r not in covered)
            if missing:
                errors.append(f"battery does not cover regimes: {', '.join(missing)}")
            valid_regimes = {str(r) for r in regime.Regime}
            if not self.coarse_regimes:
                errors.append("coarse_regimes must name at least one regime")
            else:
                invalid_coarse = [
                    i
                    for i, value in enumerate(self.coarse_regimes)
                    if type(value) is not str or value not in valid_regimes
                ]
                for i in invalid_coarse:
                    errors.append(f"coarse_regimes[{i}] must be one of {sorted(valid_regimes)}")
                if not invalid_coarse:
                    missing_coarse = sorted(set(self.coarse_regimes) - {str(r) for r in covered})
                    if missing_coarse:
                        errors.append(f"coarse_regimes not present in battery: {', '.join(missing_coarse)}")
        if not 0 < self.regime_floor_pct <= 100 / len(regime.Regime):
            errors.append(f"regime_floor_pct must be 0-{100 / len(regime.Regime):.0f}")
        if not 1 <= self.control_run_confirmations <= 10:
            errors.append("control_run_confirmations must be 1-10")
        if not 60 <= self.probe_base_seconds <= 86400:
            errors.append("probe_base_seconds must be 60-86400")
        if self.probe_mttf_multiplier <= 0:
            errors.append("probe_mttf_multiplier must be > 0")
        if self.probe_level_multiplier < 1:
            errors.append("probe_level_multiplier must be >= 1")
        if self.probe_final_multiplier < 1:
            errors.append("probe_final_multiplier must be >= 1")
        if self.suspicion_separation < 1:
            errors.append("suspicion_separation must be >= 1")
        if self.suspicion_min_failures < 1:
            errors.append("suspicion_min_failures must be >= 1")
        if self.anneal_bank_hours <= 0:
            errors.append("anneal_bank_hours must be > 0")
        if not 1 <= self.anneal_max_strikes <= 10:
            errors.append("anneal_max_strikes must be 1-10")
        for i, workload in enumerate(self.endurance_workloads):
            errors.extend(_workload_errors("endurance_workloads", i, workload))
        if self.endurance and not self.auto_validate:
            errors.append("endurance requires auto_validate")
        if self.endurance and not self.endurance_workloads:
            errors.append("endurance requires at least one endurance_workloads entry")
        if not 60 <= self.endurance_slot_seconds <= 14400:
            errors.append("endurance_slot_seconds must be 60-14400")
        if not self.endurance_slot_seconds <= self.endurance_slot_max_seconds <= 14400:
            errors.append("endurance_slot_max_seconds must be endurance_slot_seconds-14400")
        if not 60 <= self.max_temperature_c <= 110:
            errors.append(f"max_temperature_c must be 60-110, got {self.max_temperature_c}")
        if self.over_temp_grace_seconds < 0:
            errors.append("over_temp_grace_seconds must be >= 0")
        if self.over_temp_hard_margin_c < 0:
            errors.append("over_temp_hard_margin_c must be >= 0")
        if self.max_thermal_retries < 0:
            errors.append("max_thermal_retries must be >= 0")
        if self.max_apparatus_retries < 0:
            errors.append("max_apparatus_retries must be >= 0")
        if self.thermal_cooldown_seconds < 0:
            errors.append("thermal_cooldown_seconds must be >= 0")
        if self.validate_duration_seconds < 1:
            errors.append("validate_duration_seconds must be >= 1")
        if self.max_confirm_retries < 0:
            errors.append("max_confirm_retries must be >= 0")
        if self.midpoint_jump_threshold < 1:
            errors.append("midpoint_jump_threshold must be >= 1")
        if self.abort_on_consecutive_failures < 0:
            errors.append("abort_on_consecutive_failures must be >= 0")
        if self.backoff_preconfirm_multiplier <= 0:
            errors.append("backoff_preconfirm_multiplier must be > 0")
        if self.stretch_threshold_pct < 0:
            errors.append("stretch_threshold_pct must be >= 0")
        return errors

    def clamp_max_offset(self, co_range: tuple[int, int]) -> None:
        """Clamp max_offset to the CPU generation's supported CO range."""
        if self.direction < 0:
            self.max_offset = max(self.max_offset, co_range[0])
        else:
            self.max_offset = min(self.max_offset, co_range[1])
