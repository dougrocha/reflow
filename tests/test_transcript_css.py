"""The transcript's colour tokens hold WCAG AA contrast against the page and the callout tint,
in the light theme and the dark one."""

import re
from pathlib import Path

import pytest

from reflow.model import ACCENTS

VIEWER = Path(__file__).parents[1] / "src" / "reflow" / "viewer"
CSS = VIEWER / "transcript.css"
WEB_CSS = Path(__file__).parents[1] / "web" / "src" / "app" / "globals.css"
AA = 4.5
DARK = re.compile(r"@media \(prefers-color-scheme: dark\)\s*\{")
FILL_SHARE = 0.06   # --rt-fill-bg: the text colour at 6% over the page


def _themes_of(path: Path) -> tuple[str, str]:
    """The stylesheet's light rules, then its dark ones (the first dark media block)."""
    css = path.read_text()
    dark = DARK.search(css)
    return css[:dark.start()], css[dark.end():].split("\n}")[0]


def _oklch_gray(value: str) -> tuple[float, float, float]:
    """An achromatic oklch(L 0 0) as sRGB: its linear light is L cubed."""
    lightness = float(re.fullmatch(r"oklch\(\s*([\d.]+)\s+0\s+0\s*\)", value).group(1))
    linear = lightness ** 3
    encoded = 12.92 * linear if linear <= 0.0031308 else 1.055 * linear ** (1 / 2.4) - 0.055
    return (round(encoded * 255),) * 3


def _pages() -> dict[str, list[tuple[tuple[float, float, float], tuple[float, float, float]]]]:
    """(text, page background) of each place that shows the transcript, light then dark, read
    from the stylesheets: the standalone viewer (viewer.css) and the web app (globals.css)."""
    pages = {}
    for theme, viewer, web in zip(("light", "dark"), _themes_of(VIEWER / "viewer.css"),
                                  _themes_of(WEB_CSS)):
        standalone = [_hex(re.search(rf"--{name}:\s*(#[0-9a-fA-F]{{6}})", viewer).group(1))
                      for name in ("fg", "bg")]
        shown = [_oklch_gray(re.search(rf"--{name}:\s*(oklch\([^)]*\))", web).group(1))
                 for name in ("foreground", "background")]
        pages[theme] = [tuple(standalone), tuple(shown)]
    return pages


def _hex(value: str) -> tuple[int, int, int]:
    return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))


def _luminance(rgb: tuple[float, float, float]) -> float:
    lin = [(c / 255 / 12.92) if c / 255 <= 0.03928 else ((c / 255 + 0.055) / 1.055) ** 2.4
           for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _accents(block: str) -> dict[str, str]:
    return dict(re.findall(r"--rt-ink-(\w+):\s*(#[0-9a-fA-F]{6})", block))


def _themes() -> dict[str, dict[str, str]]:
    light, dark = _themes_of(CSS)
    return {"light": _accents(light), "dark": _accents(dark)}


def test_there_is_one_theme_safe_value_per_accent_in_each_theme():
    themes = _themes()
    assert set(themes["light"]) == set(themes["dark"]) == set(ACCENTS)


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("accent", ["red", "green"])
def test_an_accent_holds_aa_on_the_page_and_on_a_callout_tint(theme, accent):
    colour = _hex(_themes()[theme][accent])
    for text, page in _pages()[theme]:
        tint = tuple(p + (t - p) * FILL_SHARE for p, t in zip(page, text))
        assert contrast(colour, page) >= AA, (theme, accent, page)
        assert contrast(colour, tint) >= AA, (theme, accent, page, "tint")
        assert contrast(text, tint) >= AA, (theme, page, "text on tint")
