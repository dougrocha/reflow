import pytest

from reflow.render.pagenames import page_count_text, page_text
from reflow.render.pages import page_filename
from reflow.render.sidecars import sidecar_name


# The same case table is in web/src/lib/pages.test.ts. Keep the two in step.
@pytest.mark.parametrize("n, text", [(1, "1 page"), (2, "2 pages"), (1234, "1,234 pages")])
def test_page_count_text(n, text):
    assert page_count_text(n) == text


@pytest.mark.parametrize("printed, text", [("3", "5 (printed 3)"), ("5", "5"), (None, "5")])
def test_page_text(printed, text):
    assert page_text(5, printed) == text


@pytest.mark.parametrize("n, stem", [(1, "0001"), (7, "0007"), (123, "0123"), (12345, "12345")])
def test_page_file_names(n, stem):
    assert page_filename(n) == f"{stem}.webp"
    assert sidecar_name(n) == f"layout/{stem}.json"
