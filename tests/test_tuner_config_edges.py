"""Validation-error and helper coverage for TunerConfig."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from corecycler.tuner.config import FIELD_BOUNDS, TunerConfig


def _errors(**kw) -> list[str]:
    return TunerConfig(**kw).validate()


class TestTunerConfigValidation:
    def test_direction_invalid(self):
        assert any("direction must be" in e for e in _errors(direction=0))

    def test_cores_to_test_empty(self):
        assert any("cores_to_test is empty" in e for e in _errors(cores_to_test=[]))

    def test_search_duration_too_low(self):
        assert any("search_duration_seconds" in e for e in _errors(search_duration_seconds=0))

    def test_confirm_duration_too_low(self):
        assert any("confirm_duration_seconds" in e for e in _errors(confirm_duration_seconds=0))

    def test_apparatus_streak_out_of_range(self):
        assert any("apparatus_failure_streak must be 0-100" in e for e in _errors(apparatus_failure_streak=101))

    def test_apparatus_streak_not_above_confirm_retries(self):
        errs = _errors(apparatus_failure_streak=2, max_confirm_retries=2)
        assert any("must exceed max_confirm_retries" in e for e in errs)

    def test_battery_entry_not_a_dict(self):
        assert _errors(battery=["nope"]) == ["battery[0] must be a dict"]

    def test_battery_unknown_regime(self):
        entry = {**TunerConfig().battery[0], "regime": "unknown"}
        assert _errors(battery=[entry]) == [
            "battery[0].regime must be one of ['boost', 'coupled', 'current', 'transient']"
        ]

    def test_battery_unknown_profile(self):
        entry = {**TunerConfig().battery[0], "profile": "unknown"}
        assert _errors(battery=[entry]) == ["battery[0].profile must be one of ['spectrum', 'sustained', 'transient']"]

    @pytest.mark.parametrize("value", [True, 1, None, [], {}, "unknown"])
    def test_coarse_regime_elements_are_rejected_from_json(self, value):
        with pytest.raises(ValueError) as exc_info:
            TunerConfig.from_json(json.dumps({"coarse_regimes": [value]}))

        assert str(exc_info.value) == ("coarse_regimes[0] must be one of ['boost', 'coupled', 'current', 'transient']")

    def test_ycruncher_unknown_test_tag(self):
        entry = {**TunerConfig().battery[1], "tests": ["NOPE"]}
        assert _errors(battery=[entry]) == ["battery[0].tests has unknown tags: NOPE"]

    @staticmethod
    def test_anneal_max_strikes_range():
        for value in (0, 11):
            assert _errors(anneal_max_strikes=value) == ["anneal_max_strikes must be 1-10"]

    @staticmethod
    def test_regime_floor_pct_range():
        for value in (0, 101):
            assert any("regime_floor_pct" in error for error in _errors(regime_floor_pct=value))

    def test_over_temp_grace_negative(self):
        assert any("over_temp_grace_seconds" in e for e in _errors(over_temp_grace_seconds=-1.0))

    def test_over_temp_hard_margin_negative(self):
        assert any("over_temp_hard_margin_c" in e for e in _errors(over_temp_hard_margin_c=-1.0))

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("coarse_step", 16),
            ("fine_step", 6),
            ("search_duration_seconds", 601),
            ("confirm_duration_seconds", 1801),
            ("validate_duration_seconds", 3601),
            ("max_confirm_retries", 6),
        ],
    )
    def test_documented_upper_bounds_are_enforced(self, field, value):
        assert any(field in error for error in _errors(**{field: value}))

    @pytest.mark.parametrize(
        ("field", "value"),
        [("test_order", "typo"), ("stress_mode", "typo"), ("fft_preset", "typo")],
    )
    def test_unknown_workload_selectors_are_rejected(self, field, value):
        assert any(field in error for error in _errors(**{field: value}))

    def test_duplicate_coarse_regimes_are_rejected(self):
        assert any("duplicate" in error for error in _errors(coarse_regimes=["current", "current"]))

    @pytest.mark.parametrize(
        "config, co_range",
        [
            (TunerConfig(start_offset=-51), (-50, 10)),
            (TunerConfig(max_offset=11), (-50, 10)),
            (TunerConfig(start_offset=-10, max_offset=0, direction=-1), (-50, 10)),
            (TunerConfig(start_offset=0, max_offset=-10, direction=1), (-50, 10)),
        ],
    )
    def test_offsets_respect_hardware_range_and_direction(self, config, co_range):
        assert config.validate(co_range)

    def test_field_bounds_cover_every_validated_numeric_field(self):
        numeric = {
            field.name
            for field in __import__("dataclasses").fields(TunerConfig)
            if isinstance(getattr(TunerConfig(), field.name), (int, float))
            and not isinstance(getattr(TunerConfig(), field.name), bool)
        }
        assert set(FIELD_BOUNDS) == numeric
