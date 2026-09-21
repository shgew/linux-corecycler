"""Tests for TunerConfig dataclass."""

from __future__ import annotations

import json

import pytest

from corecycler.tuner.config import TunerConfig


class TestTunerConfigDefaults:
    def test_json_roundtrip(self):
        cfg = TunerConfig(coarse_step=10, max_offset=-40, cores_to_test=[0, 1, 2])
        assert TunerConfig.from_json(cfg.to_json()) == cfg

    def test_json_roundtrip_defaults(self):
        cfg = TunerConfig()
        assert TunerConfig.from_json(cfg.to_json()) == cfg

    @pytest.mark.parametrize(
        "payload",
        [
            "{broken",
            "[]",
            '{"max_temperature_c": "80"}',
            '{"battery": null}',
            '{"coarse_regimes": null}',
            '{"auto_validate": 1}',
            '{"start_offset": -10.5}',
            '{"search_duration_seconds": NaN}',
            '{"cores_to_test": [0, "1"]}',
            '{"cores_to_test": [0, 0]}',
            '{"battery": [7]}',
        ],
    )
    def test_invalid_json_is_rejected_instead_of_using_defaults(self, payload):
        with pytest.raises(ValueError):
            TunerConfig.from_json(payload)

    def test_unknown_field_is_rejected(self):
        with pytest.raises(ValueError, match="^unknown tuner config fields: hardening_tiers$"):
            TunerConfig.from_json('{"hardening_tiers": []}')

    def test_direct_config_rejects_wrong_types_before_comparisons(self):
        cfg = TunerConfig(coarse_step="five", search_duration_seconds=float("nan"))
        errors = cfg.validate()
        assert any("coarse_step" in error for error in errors)
        assert any("search_duration_seconds" in error for error in errors)

    def test_from_json_keeps_valid_typed_fields(self):
        """The type guard must not reject legitimate values."""
        expected = TunerConfig(
            coarse_step=7,
            cores_to_test=[0, 2],
            validate_memory=False,
            max_temperature_c=90.0,
            battery=TunerConfig().battery,
            coarse_regimes=["current"],
            regime_floor_pct=10.0,
            control_run_confirmations=3,
            probe_base_seconds=900,
            probe_mttf_multiplier=3.0,
            probe_level_multiplier=2.0,
            probe_final_multiplier=5.0,
            suspicion_separation=3.0,
            suspicion_min_failures=4,
            anneal_bank_hours=8.0,
            anneal_max_strikes=4,
        )
        assert TunerConfig.from_json(expected.to_json()) == expected

    def test_from_json_accepts_json_int_for_float_field(self):
        """JSON has no float/int distinction — a bare int for a float field is valid."""
        cfg = TunerConfig.from_json(json.dumps({"max_temperature_c": 90}))
        assert cfg.max_temperature_c == 90

    def test_clamp_max_offset_negative_direction(self):
        cfg = TunerConfig(max_offset=-100, direction=-1)
        cfg.clamp_max_offset((-60, 10))  # Zen 5
        assert cfg.max_offset == -60

    def test_clamp_max_offset_within_range(self):
        cfg = TunerConfig(max_offset=-40, direction=-1)
        cfg.clamp_max_offset((-60, 10))
        assert cfg.max_offset == -40  # already within range

    def test_clamp_max_offset_positive_direction(self):
        cfg = TunerConfig(max_offset=50, direction=1)
        cfg.clamp_max_offset((-30, 30))  # Zen 3
        assert cfg.max_offset == 30

    def test_clamp_max_offset_zen3_range(self):
        cfg = TunerConfig(max_offset=-50, direction=-1)
        cfg.clamp_max_offset((-30, 30))  # Zen 3
        assert cfg.max_offset == -30


class TestNewConfigOptions:
    def test_defaults(self):
        cfg = TunerConfig()
        assert len(cfg.battery) == 7
        assert {entry["regime"] for entry in cfg.battery} == {
            "boost",
            "current",
            "transient",
            "coupled",
        }
        assert cfg.coarse_regimes == ["current", "transient"]
        assert cfg.regime_floor_pct == 15.0
        assert cfg.control_run_confirmations == 2
        assert cfg.probe_base_seconds == 1800
        assert cfg.probe_mttf_multiplier == 4.0
        assert cfg.probe_level_multiplier == 1.5
        assert cfg.probe_final_multiplier == 4.0
        assert cfg.suspicion_separation == 2.0
        assert cfg.suspicion_min_failures == 3
        assert cfg.anneal_bank_hours == 6.0
        assert cfg.anneal_max_strikes == 3
        assert cfg.max_core_time_seconds == 7200
        assert cfg.crash_penalty_steps == 3
        assert cfg.validate_transitions is True
        assert cfg.validate_memory is True

    def test_new_fields_json_roundtrip(self):
        defaults = TunerConfig()
        cfg = TunerConfig(
            battery=list(reversed(defaults.battery)),
            coarse_regimes=["boost"],
            regime_floor_pct=10.0,
            control_run_confirmations=3,
            probe_base_seconds=900,
            probe_mttf_multiplier=3.0,
            probe_level_multiplier=2.0,
            probe_final_multiplier=5.0,
            suspicion_separation=3.0,
            suspicion_min_failures=4,
            anneal_bank_hours=8.0,
            anneal_max_strikes=4,
        )
        assert TunerConfig.from_json(cfg.to_json()) == cfg

    def test_validate_memory_roundtrips(self):
        cfg = TunerConfig(validate_memory=False)
        assert TunerConfig.from_json(cfg.to_json()) == cfg

    def test_validate_crash_penalty_range(self):
        errors = TunerConfig(crash_penalty_steps=0).validate()
        assert any("crash_penalty" in error for error in errors)

    def test_validate_max_core_time_range(self):
        cfg = TunerConfig(max_core_time_seconds=100)
        errors = cfg.validate()
        assert any("max_core_time" in e.lower() for e in errors)

    def test_validate_max_apparatus_retries_range(self):
        cfg = TunerConfig(max_apparatus_retries=-1)
        errors = cfg.validate()
        assert any("max_apparatus_retries" in e.lower() for e in errors)


class TestEnduranceConfig:
    """The endurance workload matrix: thread counts, knob ranges, round-trip."""

    def _workload(self, **kw):
        return {
            "regime": "current",
            "backend": "mprime",
            "stress_mode": "SSE",
            "fft_preset": "SMALL",
            **kw,
        }

    @pytest.mark.parametrize("field", ["battery", "endurance_workloads"])
    @pytest.mark.parametrize("threads", [True, 0, -1, "2", 1.0])
    def test_non_positive_int_threads_rejected(self, field, threads):
        errors = TunerConfig(**{field: [self._workload(threads=threads)]}).validate()
        assert any(f"{field}[0].threads must be a positive integer" == e for e in errors)

    def test_positive_int_threads_accepted(self):
        workloads = [dict(entry) for entry in TunerConfig().battery]
        workloads[0]["threads"] = 2
        assert TunerConfig(battery=workloads).validate() == []
        assert TunerConfig(endurance_workloads=[self._workload(threads=2)]).validate() == []

    def test_endurance_requires_auto_validate(self):
        errors = TunerConfig(endurance=True, auto_validate=False).validate()
        assert "endurance requires auto_validate" in errors

    def test_endurance_requires_a_workload(self):
        errors = TunerConfig(endurance=True, endurance_workloads=[]).validate()
        assert "endurance requires at least one endurance_workloads entry" in errors

    def test_slot_bounds_are_ordered_and_capped(self):
        assert any("endurance_slot_seconds must be" in e for e in TunerConfig(endurance_slot_seconds=30).validate())
        errors = TunerConfig(endurance_slot_seconds=1200, endurance_slot_max_seconds=600).validate()
        assert any("endurance_slot_max_seconds must be" in e for e in errors)
        assert any("endurance_slot_max_seconds" in e for e in TunerConfig(endurance_slot_max_seconds=20000).validate())

    def test_endurance_json_roundtrip(self):
        cfg = TunerConfig(endurance=True)
        assert TunerConfig.from_json(cfg.to_json()) == cfg


class TestConfigValidationFailsClosed:
    """Invalid configs must be rejected (fail closed). A step size < 1 would make
    the search advance by 0 and loop forever, so it must never validate."""

    def _cfg(self, **kw):
        return TunerConfig(cores_to_test=[0], **kw)

    def test_default_config_is_valid(self):
        assert self._cfg().validate() == []

    def test_zero_coarse_step_rejected(self):
        errors = self._cfg(coarse_step=0, fine_step=0).validate()
        assert any("coarse_step" in e for e in errors)

    def test_zero_fine_step_rejected(self):
        errors = self._cfg(fine_step=0).validate()
        assert any("fine_step" in e for e in errors)

    @pytest.mark.parametrize(
        ("settings", "message"),
        [
            ({"apparatus_failure_streak": -1}, "apparatus_failure_streak must be 0-100 (0 disables)"),
            (
                {"apparatus_failure_streak": 2},
                "apparatus_failure_streak must exceed max_confirm_retries (legitimate confirm retries would trip it)",
            ),
            ({"max_core_time_seconds": 1799}, "max_core_time_seconds must be 1800-14400"),
            ({"regime_floor_pct": 0}, "regime_floor_pct must be 0-25"),
            ({"control_run_confirmations": 0}, "control_run_confirmations must be 1-10"),
            ({"probe_base_seconds": 59}, "probe_base_seconds must be 60-86400"),
            ({"probe_mttf_multiplier": 0}, "probe_mttf_multiplier must be > 0"),
            ({"probe_level_multiplier": 0.5}, "probe_level_multiplier must be >= 1"),
            ({"probe_final_multiplier": 0.5}, "probe_final_multiplier must be >= 1"),
            ({"suspicion_separation": 0.5}, "suspicion_separation must be >= 1"),
            ({"suspicion_min_failures": 0}, "suspicion_min_failures must be >= 1"),
            ({"anneal_bank_hours": 0}, "anneal_bank_hours must be > 0"),
            ({"anneal_max_strikes": 0}, "anneal_max_strikes must be 1-10"),
        ],
    )
    def test_battery_and_hunt_knob_ranges_are_rejected(self, settings, message):
        assert self._cfg(**settings).validate() == [message]

    def test_battery_must_not_be_empty(self):
        assert self._cfg(battery=[]).validate() == ["battery must have at least one workload"]

    def test_battery_must_cover_every_regime(self):
        current = [entry for entry in TunerConfig().battery if entry["regime"] == "current"]
        assert self._cfg(battery=current, coarse_regimes=["current"]).validate() == [
            "battery does not cover regimes: boost, coupled, transient"
        ]

    def test_coarse_regimes_must_be_present_in_the_battery(self):
        assert self._cfg(coarse_regimes=["unknown"]).validate() == ["coarse_regimes not present in battery: unknown"]

    def test_at_least_one_coarse_regime_is_required(self):
        assert self._cfg(coarse_regimes=[]).validate() == ["coarse_regimes must name at least one regime"]

    def test_each_invalid_numeric_field_is_rejected(self):
        cases = {
            "validate_duration_seconds": 0,
            "max_confirm_retries": -1,
            "midpoint_jump_threshold": 0,
            "abort_on_consecutive_failures": -1,
            "backoff_preconfirm_multiplier": 0.0,
            "stretch_threshold_pct": -1.0,
            "resume_crash_quarantine_threshold": 0,
            "hunt_slot_seconds": 10,
            "max_unattributed_crash_hunts": 0,
            "spectrum_slot_seconds": 10,
            "soak_duration_seconds": 10,
        }
        for field, bad in cases.items():
            errors = self._cfg(**{field: bad}).validate()
            assert any(field in e for e in errors), f"{field}={bad} not rejected: {errors}"
