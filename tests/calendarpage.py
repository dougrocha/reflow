"""Synthetic month grids laid out like a printed wall calendar, for tests."""

import calendar

from reflow.extract.lines import Line

PITCH = 28.0
ROW = 25.0


def _line(x0, top, x1, bottom, text, size=8.5):
    return Line(1, (x0, top, x1, bottom), text, size, False, False, "Sans")


def spaced(title: str) -> str:
    name, year = title.split()
    return f"{' '.join(name)} {' '.join(year)}"


def month_lines(year, month, x, y, skip=(), shift=0, extra=()):
    """The lines of one printed month: its title at (x, y), then the header, then the days.
    Columns are PITCH apart from x + 14; `skip` leaves dates out, `shift` moves every date that
    many columns right, `extra` adds (date, column, row) lines."""
    name = calendar.month_name[month].upper()
    out = [_line(x + 20, y, x + 160, y + 11, spaced(f"{name} {year}"), 10.5)]
    for c, letter in enumerate("SMTWTFS"):
        cx = x + 14 + c * PITCH
        out.append(_line(cx - 3, y + 22, cx + 3, y + 33, letter, 10.6))
    first = (calendar.monthrange(year, month)[0] + 1) % 7
    for d in range(1, calendar.monthrange(year, month)[1] + 1):
        if d in skip:
            continue
        slot = first + d - 1 + shift
        r, c = divmod(slot, 7)
        cx = x + 14 + c * PITCH
        out.append(_line(cx - 5, y + 46 + r * ROW, cx + 5, y + 55 + r * ROW, f"{d:02d}"))
    for d, c, r in extra:
        cx = x + 14 + c * PITCH
        out.append(_line(cx - 5, y + 46 + r * ROW, cx + 5, y + 55 + r * ROW, f"{d:02d}"))
    return out
