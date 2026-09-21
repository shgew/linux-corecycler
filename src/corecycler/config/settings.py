"""Application settings and test profile management."""

from __future__ import annotations

import contextlib
import json
import logging
from dataclasses import asdict, dataclass, field, replace
from typing import TYPE_CHECKING, Any

from corecycler.engine.backends.base import FFTPreset, StressMode

from .paths import atomic_write, ensure_directory, user_home

if TYPE_CHECKING:
    from pathlib import Path

log = logging.getLogger(__name__)

CONFIG_DIR = user_home() / ".config" / "corecycler"
DEFAULT_PROFILE = CONFIG_DIR / "default.json"


@dataclass(slots=True)
class TestProfile:
    __test__ = False
    name: str = "Default"
    backend: str = "mprime"
    stress_mode: str = "SSE"
    fft_preset: str = "SMALL"
    fft_min: int | None = None
    fft_max: int | None = None
    threads: int = 1
    seconds_per_core: int = 600
    iterations_per_core: int = 0
    cycle_count: int = 1
    stop_on_error: bool = False
    test_smt: bool = False
    cores_to_test: list[int] | None = None
    max_temperature: float = 95.0
    test_mode: str = "STANDARD"
    variable_load: bool = False
    idle_stability_test: float = 0.0
    idle_between_cores: float = 0.0
    _extra: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def get_stress_mode(self) -> StressMode:
        return StressMode[self.stress_mode]

    def get_fft_preset(self) -> FFTPreset:
        return FFTPreset[self.fft_preset]


@dataclass(slots=True)
class AppSettings:
    work_dir: str = ""
    theme: str = "system"
    poll_interval: float = 1.0
    show_smt_threads: bool = False
    profiles: list[TestProfile] = field(default_factory=lambda: [TestProfile()])
    active_profile_idx: int = 0
    window_width: int = 1200
    window_height: int = 800
    record_history: bool = True
    record_telemetry: bool = True
    history_retention_days: int = 90
    notify_on_completion: bool = True
    _extra: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def active_profile(self) -> TestProfile:
        if 0 <= self.active_profile_idx < len(self.profiles):
            return self.profiles[self.active_profile_idx]
        return self.profiles[0] if self.profiles else TestProfile()

    def update_active_profile(self, profile: TestProfile) -> None:
        if not 0 <= self.active_profile_idx < len(self.profiles):
            raise ValueError("active profile index does not select a saved profile")
        current = self.profiles[self.active_profile_idx]
        self.profiles[self.active_profile_idx] = replace(profile, name=current.name)


_PROFILE_TYPES: dict[str, type | tuple[type, ...]] = {
    "name": str,
    "backend": str,
    "stress_mode": str,
    "fft_preset": str,
    "fft_min": (int, type(None)),
    "fft_max": (int, type(None)),
    "threads": int,
    "seconds_per_core": int,
    "iterations_per_core": int,
    "cycle_count": int,
    "stop_on_error": bool,
    "test_smt": bool,
    "cores_to_test": (list, type(None)),
    "max_temperature": (int, float),
    "test_mode": str,
    "variable_load": bool,
    "idle_stability_test": (int, float),
    "idle_between_cores": (int, float),
}

_SETTINGS_TYPES: dict[str, type | tuple[type, ...]] = {
    "work_dir": str,
    "theme": str,
    "poll_interval": (int, float),
    "show_smt_threads": bool,
    "active_profile_idx": int,
    "window_width": int,
    "window_height": int,
    "record_history": bool,
    "record_telemetry": bool,
    "history_retention_days": int,
    "notify_on_completion": bool,
}


def _matches(value: Any, expected: type | tuple[type, ...]) -> bool:
    allowed = expected if isinstance(expected, tuple) else (expected,)
    if bool not in allowed and isinstance(value, bool):
        return False
    return isinstance(value, allowed)


def _validated(data: dict[str, Any], schema: dict[str, type | tuple[type, ...]], subject: str) -> dict[str, Any]:
    known: dict[str, Any] = {}
    for key, expected in schema.items():
        if key not in data:
            continue
        value = data[key]
        if not _matches(value, expected):
            raise TypeError(f"{subject}.{key} has invalid type {type(value).__name__}")
        known[key] = value
    return known


def _decode_profile(raw: Any) -> TestProfile:
    if not isinstance(raw, dict):
        raise TypeError("profile must be an object")
    values = _validated(raw, _PROFILE_TYPES, "profile")
    cores = values.get("cores_to_test")
    if cores is not None and any(not isinstance(core, int) or isinstance(core, bool) for core in cores):
        raise TypeError("profile.cores_to_test must contain integers")
    extra = {key: value for key, value in raw.items() if key not in _PROFILE_TYPES}
    return TestProfile(**values, _extra=extra)


def _decode_settings(raw: Any) -> AppSettings:
    if not isinstance(raw, dict):
        raise TypeError("settings must be an object")
    values = _validated(raw, _SETTINGS_TYPES, "settings")
    if values.get("work_dir") == "/tmp/corecycler":
        values["work_dir"] = ""
    if "profiles" in raw:
        if not isinstance(raw["profiles"], list):
            raise TypeError("settings.profiles must be a list")
        profiles = [_decode_profile(profile) for profile in raw["profiles"]]
    else:
        profiles = [TestProfile()]
    extra = {key: value for key, value in raw.items() if key not in _SETTINGS_TYPES and key != "profiles"}
    return AppSettings(**values, profiles=profiles, _extra=extra)


def _encode_profile(profile: TestProfile) -> dict[str, Any]:
    data = asdict(profile)
    extra = data.pop("_extra")
    return {**extra, **data}


def _encode_settings(settings: AppSettings) -> dict[str, Any]:
    data = asdict(settings)
    extra = data.pop("_extra")
    data["profiles"] = [_encode_profile(profile) for profile in settings.profiles]
    return {**extra, **data}


def load_settings() -> AppSettings:
    """Load settings from disk, or return defaults on unreadable state."""
    settings_file = CONFIG_DIR / "settings.json"
    try:
        ensure_directory(CONFIG_DIR)
        if not settings_file.exists():
            return AppSettings()
        content = settings_file.read_text()
    except OSError as exc:
        log.warning("Settings file unavailable (%s), using defaults", exc)
        return AppSettings()

    try:
        return _decode_settings(json.loads(content))
    except (json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
        corrupt = settings_file.with_suffix(".json.corrupt")
        log.warning("Settings file unreadable (%s) - moved to %s, using defaults", exc, corrupt)
        with contextlib.suppress(OSError):
            settings_file.replace(corrupt)
        return AppSettings()


def save_settings(settings: AppSettings) -> None:
    """Save settings to disk."""
    ensure_directory(CONFIG_DIR)
    atomic_write(CONFIG_DIR / "settings.json", json.dumps(_encode_settings(settings), indent=2))


def load_profile(path: Path) -> TestProfile:
    """Load a test profile from a JSON file."""
    return _decode_profile(json.loads(path.read_text()))


def save_profile(profile: TestProfile, path: Path) -> None:
    """Save a test profile to a JSON file."""
    ensure_directory(path.parent)
    atomic_write(path, json.dumps(_encode_profile(profile), indent=2))


def save_co_profile(
    offsets: dict[int, int],
    path: Path,
    cpu_model: str = "",
    source: str = "manual",
) -> None:
    """Save a CO offset profile to a JSON file."""
    from datetime import UTC, datetime

    data = {
        "format": "corecycler-co-profile",
        "version": 1,
        "cpu_model": cpu_model,
        "created_at": datetime.now(UTC).isoformat(),
        "source": source,
        "offsets": {str(k): v for k, v in sorted(offsets.items())},
    }
    ensure_directory(path.parent)
    atomic_write(path, json.dumps(data, indent=2))


def load_co_profile(path: Path) -> dict[int, int]:
    """Load a CO offset profile from a JSON file."""
    data = json.loads(path.read_text())
    raw = data.get("offsets", data)
    return {int(k): int(v) for k, v in raw.items() if k.lstrip("-").isdigit()}
