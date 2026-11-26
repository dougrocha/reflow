"""The school district calendar's twelve month grids, found on the real page.

`da-school-district-calendar` is one page of twelve month grids (three across, four down), a colour
key and a dated list. Each month must be found from where its lines lie, and each must pass the
weekday arithmetic.
"""

from __future__ import annotations

import pytest

from corpus import absent_reason, source
from reflow.analyze.calendar import find_calendars
from reflow.extract.pages import extract_pages

pytestmark = pytest.mark.pages

MONTHS = [f"{name} {year}" for name, year in
          [("JULY", 2024), ("AUGUST", 2024), ("SEPTEMBER", 2024), ("OCTOBER", 2024),
           ("NOVEMBER", 2024), ("DECEMBER", 2024), ("JANUARY", 2025), ("FEBRUARY", 2025),
           ("MARCH", 2025), ("APRIL", 2025), ("MAY", 2025), ("JUNE", 2025)]]
OUTSIDE = ["COLOR KEY:", "DATES TO REMEMBER", "September 2: Labor Day",
           "February 17 will be a Bad Weather Make-up Day."]


@pytest.fixture(scope="module")
def page():
    if reason := absent_reason("docaccess-calendar"):
        pytest.skip(reason)
    return next(iter(extract_pages(source("docaccess-calendar"), 1, 1)))


@pytest.fixture(scope="module")
def grids(page):
    return find_calendars([l for l in page.lines if not l.rotated])


def test_twelve_months_from_july_2024_to_june_2025(grids):
    assert [g.title for g in grids] == MONTHS


def test_every_month_passes_the_weekday_arithmetic(grids):
    assert [g.check() for g in grids if g.check()] == []
    assert sum(1 for g in grids for week in g.weeks for l in week if l) == 365


def test_the_key_and_the_dated_list_stay_in_the_flow(page, grids):
    held = set().union(*(g.lines for g in grids))
    for text in OUTSIDE:
        (line,) = [l for l in page.lines if l.text.strip() == text]
        assert id(line) not in held, text
