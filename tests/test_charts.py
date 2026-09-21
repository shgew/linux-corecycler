"""LiveChart widget: data handling and paint under empty/single/degenerate ranges."""

from __future__ import annotations

import sys as _sys

import pytest

if not hasattr(_sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("GUI tests require real PySide6", allow_module_level=True)


def _chart(**kw):
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from corecycler.gui.widgets.charts import LiveChart

    return LiveChart(**kw)


def _image(chart):
    return chart.grab().toImage()


def _has_color(image, color) -> bool:
    target = color.rgba()
    return any(image.pixelColor(x, y).rgba() == target for x in range(image.width()) for y in range(image.height()))


class TestLiveChart:
    def test_add_value_tracks_current(self):
        c = _chart(title="Freq", unit="MHz", min_val=0, max_val=6000)
        c.add_value(5200)
        assert c._current == 5200
        assert list(c._data) == [5200]

    def test_ring_buffer_bounded(self):
        from corecycler.gui.widgets.charts import MAX_POINTS

        c = _chart(max_val=100)
        for i in range(MAX_POINTS + 50):
            c.add_value(i)
        assert len(c._data) == MAX_POINTS

    def test_clear_resets(self):
        c = _chart()
        c.add_value(10)
        c.clear()
        assert not c._data
        assert c._current == 0

    def test_paint_empty_renders_background_and_border(self):
        from PySide6.QtGui import QColor

        from corecycler.gui.style import theme

        chart = _chart()
        chart.resize(200, 100)
        image = _image(chart)
        assert image.pixelColor(100, 50) == QColor(theme.BG_PANEL_DARK)
        assert image.pixelColor(0, 50) == QColor(theme.BORDER_DARKER)

    def test_paint_single_point_renders_series_color(self):
        from PySide6.QtGui import QColor

        from corecycler.gui.style import theme

        chart = _chart(min_val=0, max_val=100)
        chart.resize(200, 100)
        chart.add_value(42)
        assert _has_color(_image(chart), QColor(theme.CHART_FREQ))

    def test_paint_many_points_renders_series_and_fill(self):
        from PySide6.QtGui import QColor

        from corecycler.gui.style import theme

        chart = _chart(min_val=0, max_val=100)
        chart.resize(200, 100)
        for value in range(80):
            chart.add_value(value)
        image = _image(chart)
        assert _has_color(image, QColor(theme.CHART_FREQ))
        background = QColor(theme.BG_PANEL_DARK).rgba()
        assert sum(image.pixelColor(x, 75).rgba() != background for x in range(20, 180)) > 20

    def test_paint_degenerate_range_clamps_visible_point(self):
        from PySide6.QtGui import QColor

        from corecycler.gui.style import theme

        chart = _chart(min_val=50, max_val=50)
        chart.resize(200, 100)
        chart.add_value(50)
        image = _image(chart)
        series = QColor(theme.CHART_FREQ).rgba()
        assert any(image.pixelColor(x, y).rgba() == series for x in range(2, 9) for y in range(91, 99))
