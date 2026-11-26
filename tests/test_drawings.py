"""chart_marks: the repeated bars, dots, wedges and ticks that make a drawing a chart."""

from reflow.extract.drawings import SubPath, chart_marks


def _sub(kind, x0, top, x1, bottom, fill=True, nline=4, ncurve=0, vertices=()):
    return SubPath(kind, (x0, top, x1, bottom), fill, nline, ncurve, vertices)


def test_chart_marks_count_repeated_bars_dots_wedges_and_ticks():
    bars = [_sub("rect", 100, 100 + 20 * i, 100 + w, 112 + 20 * i)
            for i, w in enumerate([300, 240, 180, 150])]
    assert chart_marks(bars) == (("bars", 4),)
    dots = [_sub("curve", 50 + 20 * i, 300, 58 + 20 * i, 308, fill=False, nline=0, ncurve=4)
            for i in range(4)]
    assert chart_marks(dots) == (("dots", 4),)
    ticks = [_sub("line", 100 + 9 * i, 400, 100 + 9 * i, 406, fill=False, nline=1) for i in range(10)]
    assert chart_marks(ticks) == (("ticks", 10),)
    wedges = [_sub("curve", 200, 300, 400, 500, nline=2, ncurve=1,
                   vertices=((300.0, 400.0), (200.0 + 10 * i, 350.0))) for i in range(8)]
    assert chart_marks(wedges) == (("wedges", 8),)


def test_chart_marks_reject_a_zebra_band_table_cells_and_polygons_that_are_not_curves():
    zebra = [_sub("rect", 30, 100 + 20 * i, 330, 118 + 20 * i) for i in range(6)]
    assert chart_marks(zebra) == ()  # one length along the bands: not bars
    tiles = [_sub("rect", 100 + sum([30, 22, 38][:c]), 300 + 14 * r,
                  100 + sum([30, 22, 38][:c + 1]), 311 + 14 * r)
             for r in range(4) for c in range(3)]
    assert chart_marks(tiles) == ()  # table cells tile each row (they touch end to end)
    wide_rows = [_sub("rect", 100 + 40 * c + 3 * r, 300 + 14 * r, 124 + 40 * c + 3 * r, 311 + 14 * r)
                 for r in range(3) for c in range(5)]
    assert chart_marks(wide_rows) == ()  # five cells to a row: more than grouped bars have series
    rules = [_sub("rect", 60, 200 + 6 * i, 60 + 40 * (i + 1), 200.5 + 6 * i) for i in range(6)]
    assert chart_marks(rules) == ()  # thin filled rules, not bars
    polygons = [_sub("polyline", 200, 300, 400, 500, nline=3,
                     vertices=((300.0, 400.0), (200.0 + 10 * i, 350.0))) for i in range(8)]
    assert chart_marks(polygons) == ()  # shared vertex, but no curves: not wedges
