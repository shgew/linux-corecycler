"""Edge coverage for history context and settings profiles pending ownership moves."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from corecycler.config import settings
from corecycler.history import context


class TestHistoryContextEdges:
    def test_read_bios_version_unreadable(self, tmp_path):
        d = tmp_path / "bios_version"
        d.mkdir()  # exists() True, read_text() raises OSError
        assert context.read_bios_version(d) == ""


class TestSettingsCoProfile:
    def test_save_then_load_roundtrip(self, tmp_path):
        p = tmp_path / "sub" / "co.json"
        settings.save_co_profile({0: -30, 5: -15}, p, cpu_model="Ryzen", source="tuner")
        assert p.exists()
        assert settings.load_co_profile(p) == {0: -30, 5: -15}
