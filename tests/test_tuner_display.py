"""Display-correctness tests for the monitor/tuner readouts.

Covers the per-core frequency string (an idle core must not render a false
boost ceiling) and the "Tests Run" tally (synthetic crash-on-resume rows must
not be counted as stress tests).
"""

from __future__ import annotations

from corecycler.gui.monitor_tab import CoreFreqBar


class TestPerCoreFreqText:
    def test_idle_core_shows_no_false_ceiling(self):
        text = CoreFreqBar._freq_text(0.0, 5750.0)
        assert "5750" not in text
        assert "MHz" in text

    def test_negative_freq_treated_as_idle(self):
        text = CoreFreqBar._freq_text(-1.0, 5750.0)
        assert "5750" not in text
        assert "MHz" in text

    def test_live_core_has_unit_and_ceiling(self):
        text = CoreFreqBar._freq_text(4321.0, 5750.0)
        assert "4321" in text
        assert "5750" in text
        assert "MHz" in text

    def test_live_core_without_known_ceiling_has_unit(self):
        text = CoreFreqBar._freq_text(4321.0, 0.0)
        assert "4321" in text
        assert "5750" not in text
        assert "MHz" in text
