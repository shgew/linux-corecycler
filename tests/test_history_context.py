"""Tests for complete hardware context capture and identity."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from corecycler.history.context import (
    SystemContext,
    capture_system_context,
    compute_context_hash,
    detect_bios_change,
    read_bios_version,
)
from corecycler.history.db import HistoryDB, TuningContextRecord


@pytest.fixture
def db():
    history = HistoryDB(":memory:")
    yield history
    history.close()


def _hash(**changes):
    values = {
        "cpu_model": "AMD Ryzen 9 9950X3D2",
        "physical_cores": 16,
        "ccds": 2,
        "co": tuple(range(-16, 0)),
        "pbo_scalar": 1.0,
        "boost_limit_mhz": 200,
        "ppt_limit_w": 200.0,
        "tdc_limit_a": 160.0,
        "edc_limit_a": 225.0,
        "bios_version": "2402",
    }
    values.update(changes)
    return compute_context_hash(**values)


class TestComputeContextHash:
    def test_is_deterministic(self):
        assert _hash() == _hash()
        assert len(_hash()) == 64

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("cpu_model", "different"),
            ("physical_cores", 8),
            ("ccds", 1),
            ("co", (0,) * 16),
            ("pbo_scalar", None),
            ("boost_limit_mhz", None),
            ("ppt_limit_w", None),
            ("tdc_limit_a", None),
            ("edc_limit_a", None),
            ("bios_version", "different"),
        ],
    )
    def test_every_operating_point_field_changes_identity(self, field, value):
        assert _hash(**{field: value}) != _hash()

    def test_missing_core_does_not_equal_an_absent_core(self):
        assert _hash(co=(-30, None, -20)) != _hash(co=(-30, -20))


class TestReadBiosVersion:
    def test_reads_and_strips_file(self, tmp_path):
        bios_file = tmp_path / "bios_version"
        bios_file.write_text("  2101  \n")
        assert read_bios_version(bios_file) == "2101"

    def test_missing_or_unreadable_path_is_empty(self, tmp_path):
        assert read_bios_version(tmp_path / "missing") == ""
        assert read_bios_version(tmp_path) == ""


class TestCaptureSystemContext:
    def test_complete_capture_contains_full_vector(self, tmp_path, monkeypatch):
        import corecycler.smu.pmtable as pmtable_mod

        bios_file = tmp_path / "bios_version"
        bios_file.write_text("2402")
        monkeypatch.setattr(pmtable_mod, "read_power_limits", lambda: (200.0, 160.0, 225.0))
        smu = MagicMock()
        smu.get_all_co_offsets.return_value = {0: -30, 1: -25}
        smu.get_pbo_scalar.return_value = 1.0
        smu.get_boost_limit.return_value = 200

        context = capture_system_context(
            smu,
            2,
            bios_file,
            cpu_model="AMD Ryzen 9 9950X3D2",
            ccds=2,
        )

        assert context.co == (-30, -25)
        assert context.complete is True
        assert context.missing == ()
        assert context.ppt_limit_w == 200.0
        assert context.context_hash == _hash(
            physical_cores=2,
            co=(-30, -25),
            bios_version="2402",
            boost_limit_mhz=200,
        )

    def test_optional_smu_values_may_be_unavailable(self, tmp_path, monkeypatch):
        import corecycler.smu.pmtable as pmtable_mod

        monkeypatch.setattr(pmtable_mod, "read_power_limits", lambda: (None, None, None))
        smu = MagicMock()
        smu.get_all_co_offsets.return_value = {0: -30}
        smu.get_pbo_scalar.side_effect = OSError
        smu.get_boost_limit.side_effect = OSError

        context = capture_system_context(smu, 1, tmp_path / "missing", cpu_model="CPU", ccds=1)

        assert context.complete is True
        assert context.pbo_scalar is None
        assert context.boost_limit_mhz is None
        assert context.ppt_limit_w is None

    def test_power_limits_may_be_unavailable(self, tmp_path, monkeypatch):
        import corecycler.smu.pmtable as pmtable_mod

        smu = MagicMock()
        smu.get_all_co_offsets.return_value = {0: -30}
        smu.get_pbo_scalar.return_value = 1.0
        smu.get_boost_limit.return_value = 200
        monkeypatch.setattr(pmtable_mod, "read_power_limits", MagicMock(side_effect=OSError("unavailable")))

        context = capture_system_context(smu, 1, tmp_path / "missing", cpu_model="CPU")

        assert context.complete is True
        assert context.ppt_limit_w is None
        assert context.tdc_limit_a is None
        assert context.edc_limit_a is None

    def test_missing_and_offset_gaps_are_explicit(self, tmp_path):
        smu = MagicMock()
        smu.get_all_co_offsets.return_value = {0: -30}
        smu.get_pbo_scalar.return_value = None
        smu.get_boost_limit.return_value = None

        context = capture_system_context(smu, 2, tmp_path / "missing")

        assert context.complete is False
        assert context.co == (-30, None)
        assert context.missing == ("cpu_model", "co[1]")

    def test_failed_offset_read_is_incomplete(self, tmp_path):
        smu = MagicMock()
        smu.get_all_co_offsets.side_effect = OSError
        smu.get_pbo_scalar.return_value = None
        smu.get_boost_limit.return_value = None

        context = capture_system_context(smu, 1, tmp_path / "missing", cpu_model="CPU")

        assert context.complete is False
        assert context.missing == ("co[0]",)

    def test_no_smu_or_cores_is_incomplete(self, tmp_path):
        context = capture_system_context(None, 0, tmp_path / "missing")
        assert context.complete is False
        assert context.missing == ("cpu_model", "co")

    def test_system_context_converts_to_persisted_record(self):
        context = SystemContext(
            cpu_model="CPU",
            physical_cores=2,
            ccds=1,
            co=(-30, None),
            pbo_scalar=None,
            boost_limit_mhz=None,
            ppt_limit_w=None,
            tdc_limit_a=None,
            edc_limit_a=None,
            bios_version="2402",
            context_hash="hash",
            complete=False,
            missing=("co[1]",),
        )
        record = context.to_record()
        assert record.cpu_model == "CPU"
        assert record.co_offsets_json == '{"0":-30,"1":null}'
        assert record.context_hash == "hash"


class TestStoredContexts:
    def test_get_or_create_reuses_complete_identity(self, db):
        context = TuningContextRecord(bios_version="2402", context_hash="same")
        first = db.get_or_create_context(context)
        second = db.get_or_create_context(TuningContextRecord(bios_version="2402", context_hash="same"))
        assert first == second

    def test_different_context_hash_or_bios_is_distinct(self, db):
        first = db.get_or_create_context(TuningContextRecord(bios_version="2402", context_hash="one"))
        second = db.get_or_create_context(TuningContextRecord(bios_version="2402", context_hash="two"))
        third = db.get_or_create_context(TuningContextRecord(bios_version="2403", context_hash="one"))
        assert len({first, second, third}) == 3


class TestDetectBiosChange:
    def test_no_previous_contexts(self, db, tmp_path):
        bios_file = tmp_path / "bios_version"
        bios_file.write_text("2101")
        assert detect_bios_change(db, bios_file) == (False, "", "2101")

    def test_same_and_changed_bios(self, db, tmp_path):
        db.get_or_create_context(TuningContextRecord(bios_version="2101", context_hash="one"))
        bios_file = tmp_path / "bios_version"
        bios_file.write_text("2101")
        assert detect_bios_change(db, bios_file) == (False, "2101", "2101")
        bios_file.write_text("2201")
        assert detect_bios_change(db, bios_file) == (True, "2101", "2201")
