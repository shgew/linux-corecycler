"""Tests for TunerConfig dataclass."""

from __future__ import annotations

import json
 
import pytest

from corecycler.tuner.config import TunerConfig


class TestTunerConfigDefaults:
    def test_json_roundtrip(self):
        cfg = TunerConfig(coarse_step=10, max_offset=-40, cores_to_test=[0, 1, 2])
        json_str = cfg.to_json()
        restored = TunerConfig.from_json(json_str)
        assert restored.coarse_step == 10
        assert restored.max_offset == -40
        assert restored.cores_to_test == [0, 1, 2]
        assert restored.start_offset == cfg.start_offset

    def test_json_roundtrip_defaults(self):
        cfg = TunerConfig()
        restored = TunerConfig.from_json(cfg.to_json())
        assert restored.coarse_step == cfg.coarse_step
        assert restored.direction == cfg.direction
        assert restored.cores_to_test == cfg.cores_to_test

    @pytest.mark.parametrize("payload", [
        '{broken', '[]', '{"max_temperatur_c": 80}', '{"max_temperature_c": "80"}',
        '{"hardening_tiers": null}', '{"auto_validate": 1}', '{"start_offset": -10.5}',
        '{"search_duration_seconds": NaN}', '{"over_temp_grace_seconds": Infinity}',
        '{"cores_to_test": [0, "1"]}', '{"cores_to_test": [0, 0]}', '{"hardening_tiers": [7]}',
        '{"hardening_tiers": [{"backend": [], "stress_mode": "SSE", "fft_preset": "SMALL"}]}',
    ])
    def test_invalid_json_is_rejected_instead_of_using_defaults(self, payload):
        with pytest.raises(ValueError):
            TunerConfig.from_json(payload)

    def test_direct_config_rejects_wrong_types_before_comparisons(self):
        cfg = TunerConfig(coarse_step="five", search_duration_seconds=float("nan"))
        errors = cfg.validate()
        assert any("coarse_step" in error for error in errors)
        assert any("search_duration_seconds" in error for error in errors)

    def test_from_json_keeps_valid_typed_fields(self):
        """The type guard must not reject legitimate values."""
        cfg = TunerConfig.from_json(
            json.dumps(
                {
                    "coarse_step": 7,
                    "cores_to_test": [0, 2],
                    "auto_validate": False,
                    "max_temperature_c": 90.0,
                    "hardening_tiers": [],
                }
            )
        )
        assert cfg.coarse_step == 7
        assert cfg.cores_to_test == [0, 2]
        assert cfg.auto_validate is False
        assert cfg.max_temperature_c == 90.0
        assert cfg.hardening_tiers == []

    def test_from_json_accepts_json_int_for_float_field(self):
        """JSON has no float/int distinction — a bare int for a float field is
        valid (60 for max_temperature_c), not a type error."""
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
    def test_hardening_tiers_default(self):
        cfg = TunerConfig()
        assert cfg.hardening_tiers == [
            {"backend": "mprime", "stress_mode": "AVX2", "fft_preset": "SMALL"},
            {"backend": "mprime", "stress_mode": "SSE", "fft_preset": "LARGE"},
            {"backend": "mprime", "stress_mode": "SSE", "fft_preset": "SMALL", "profile": "spectrum"},
        ]

    def test_max_core_time_default(self):
        cfg = TunerConfig()
        assert cfg.max_core_time_seconds == 7200

    def test_crash_penalty_steps_default(self):
        cfg = TunerConfig()
        assert cfg.crash_penalty_steps == 3

    def test_validate_transitions_default(self):
        cfg = TunerConfig()
        assert cfg.validate_transitions is True

    def test_validate_memory_default_and_roundtrips(self):
        cfg = TunerConfig()
        assert cfg.validate_memory is True
        restored = TunerConfig.from_json(TunerConfig(validate_memory=False).to_json())
        assert restored.validate_memory is False

    def test_hardening_tiers_json_roundtrip(self):
        cfg = TunerConfig()
        restored = TunerConfig.from_json(cfg.to_json())
        assert restored.hardening_tiers == cfg.hardening_tiers
        assert restored.max_core_time_seconds == cfg.max_core_time_seconds
        assert restored.crash_penalty_steps == cfg.crash_penalty_steps
        assert restored.validate_transitions == cfg.validate_transitions

    def test_empty_hardening_tiers_valid(self):
        cfg = TunerConfig(hardening_tiers=[])
        errors = cfg.validate()
        assert not any("hardening" in e.lower() for e in errors)

    def test_validate_crash_penalty_range(self):
        cfg = TunerConfig(crash_penalty_steps=0)
        errors = cfg.validate()
        assert any("crash_penalty" in e.lower() for e in errors)

    def test_validate_max_core_time_range(self):
        cfg = TunerConfig(max_core_time_seconds=100)
        errors = cfg.validate()
        assert any("max_core_time" in e.lower() for e in errors)

    def test_validate_max_apparatus_retries_range(self):
        cfg = TunerConfig(max_apparatus_retries=-1)
        errors = cfg.validate()
        assert any("max_apparatus_retries" in e.lower() for e in errors)

    def test_spectrum_tier_profile_validated(self):
        good = TunerConfig(
            hardening_tiers=[
                {"backend": "mprime", "stress_mode": "SSE", "fft_preset": "SMALL", "profile": "spectrum"},
            ]
        )
        assert not any("profile" in e for e in good.validate())
        bad = TunerConfig(
            hardening_tiers=[
                {"backend": "mprime", "stress_mode": "SSE", "fft_preset": "SMALL", "profile": "bogus"},
            ]
        )
        assert any("profile" in e for e in bad.validate())

    def test_default_tiers_include_spectrum(self):
        assert TunerConfig().hardening_tiers[-1]["profile"] == "spectrum"


class TestEnduranceConfig:
    """The endurance workload matrix: thread counts, knob ranges, round-trip."""

    def _workload(self, **kw):
        return {"backend": "mprime", "stress_mode": "SSE", "fft_preset": "SMALL", **kw}

    @pytest.mark.parametrize("field", ["hardening_tiers", "endurance_workloads"])
    @pytest.mark.parametrize("threads", [True, 0, -1, "2", 1.0])
    def test_non_positive_int_threads_rejected(self, field, threads):
        errors = TunerConfig(**{field: [self._workload(threads=threads)]}).validate()
        assert any(f"{field}[0].threads must be a positive integer" == e for e in errors)

    @pytest.mark.parametrize("field", ["hardening_tiers", "endurance_workloads"])
    def test_positive_int_threads_accepted(self, field):
        assert TunerConfig(**{field: [self._workload(threads=2)]}).validate() == []

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
        restored = TunerConfig.from_json(cfg.to_json())
        assert restored.endurance is True
        assert restored.endurance_workloads == cfg.endurance_workloads
        assert restored.endurance_workloads[0]["threads"] == 2
        assert restored.endurance_slot_seconds == 600
        assert restored.endurance_slot_max_seconds == 3600


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
