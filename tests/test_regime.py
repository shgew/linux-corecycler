"""Workload labels, serialization, battery coverage, and user-facing validation."""

from __future__ import annotations

from corecycler.tuner.config import TunerConfig
from corecycler.tuner.regime import Profile, Regime, Workload, regimes_covered, workload_errors


def _entry(**changes: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "regime": "current",
        "backend": "mprime",
        "stress_mode": "AVX2",
        "fft_preset": "SMALL",
    }
    entry.update(changes)
    return entry


def test_workload_labels_describe_the_actual_stress_recipe():
    assert Workload(Regime.CURRENT, "mprime", "AVX2", "SMALL", threads=2).label == "mprime AVX2 SMALL 2T"
    assert (
        Workload(
            Regime.BOOST,
            "ycruncher",
            "AVX2",
            "SMALL",
            threads=1,
            tests=("BKT", "BBP"),
        ).label
        == "ycruncher AVX2 SMALL 1T BKT/BBP"
    )
    assert (
        Workload(
            Regime.TRANSIENT,
            "mprime",
            "SSE",
            "SMALLEST",
            profile=Profile.TRANSIENT,
        ).label
        == "mprime SSE SMALLEST transient"
    )


def test_workload_serialization_preserves_optional_execution_details():
    serialized = {
        "regime": "coupled",
        "backend": "ycruncher",
        "stress_mode": "AVX2",
        "fft_preset": "SMALL",
        "profile": "spectrum",
        "threads": 2,
        "tests": ["VT3", "N63"],
        "memory_coupled": True,
    }

    assert Workload.from_dict(serialized).to_dict() == serialized


def test_regimes_covered_reports_each_distinct_load_class():
    battery = [
        _entry(regime="boost"),
        _entry(regime="current"),
        _entry(regime="boost", backend="ycruncher"),
        _entry(regime="transient"),
        _entry(regime="coupled"),
    ]

    assert regimes_covered(battery) == {
        Regime.BOOST,
        Regime.CURRENT,
        Regime.TRANSIENT,
        Regime.COUPLED,
    }


def test_workload_errors_explain_malformed_entries_exactly():
    assert workload_errors("battery", 2, "not a mapping") == ["battery[2] must be a dict"]
    assert workload_errors("battery", 2, _entry(regime="voltage")) == [
        "battery[2].regime must be one of ['boost', 'coupled', 'current', 'transient']"
    ]
    assert workload_errors("battery", 2, _entry(backend=None)) == [
        "battery[2] requires non-blank string backend, stress_mode, fft_preset"
    ]
    assert workload_errors("battery", 2, _entry(stress_mode="   ")) == [
        "battery[2] requires non-blank string backend, stress_mode, fft_preset"
    ]
    assert workload_errors("battery", 2, _entry(profile="bursty")) == [
        "battery[2].profile must be one of ['spectrum', 'sustained', 'transient']"
    ]
    assert workload_errors("battery", 2, _entry(threads=0)) == ["battery[2].threads must be a positive integer"]
    assert workload_errors("battery", 2, _entry(threads=True)) == ["battery[2].threads must be a positive integer"]
    assert workload_errors("battery", 2, _entry(tests="BKT")) == ["battery[2].tests must be a list of strings"]
    assert workload_errors("battery", 2, _entry(tests=["ZZZ", "AAA"])) == [
        "battery[2].tests has unknown tags: AAA, ZZZ"
    ]
    assert workload_errors("battery", 2, _entry(threads=2, tests=["BKT"])) == []


def test_config_rejects_blank_fields_in_search_and_endurance_batteries():
    config = TunerConfig(
        battery=[_entry(backend="")],
        endurance_workloads=[_entry(fft_preset="\t")],
    )

    errors = config.validate()

    assert "battery[0] requires non-blank string backend, stress_mode, fft_preset" in errors
    assert "endurance_workloads[0] requires non-blank string backend, stress_mode, fft_preset" in errors
