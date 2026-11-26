"""How pages and page counts are named. Mirrors web/src/lib/pages.ts: the same rules, and the
same case table in tests/test_pagenames.py and web/src/lib/pages.test.ts."""


def page_count_text(n: int) -> str:
    """"1 page", "12 pages", "1,234 pages"."""
    return f"{n:,} {'page' if n == 1 else 'pages'}"


def page_text(number: int, printed: str | None) -> str:
    """The PDF page index, with the printed number when the page prints a different one:
    "5 (printed 3)", or "5"."""
    return f"{number} (printed {printed})" if printed and printed != str(number) else str(number)
