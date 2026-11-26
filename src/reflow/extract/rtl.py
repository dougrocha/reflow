"""Right-to-left text: Arabic presentation forms back to letters, and runs of right-to-left
characters in reading order."""

from __future__ import annotations

import unicodedata
from functools import lru_cache

from reflow.extract.chars import Char, same_box
from reflow.model import BBox

# A gap, in font sizes, between two right-to-left letters of one run that is a word space (see
# _logical_rtl_runs). Letters of a word are at most 0.02 apart; word gaps start at 0.14.
RTL_WORD_GAP = 0.1


# Unicode blocks of Arabic letters and of their presentation forms (U+FB50-FDFF, U+FE70-FEFF).
PRESENTATION_FORMS = ((0xFB50, 0xFDFF), (0xFE70, 0xFEFF))
ARABIC_BLOCKS = ((0x0600, 0x06FF), *PRESENTATION_FORMS)


_LAM, _ALEF = "ل", "ا"


def _normalize_presentation(ch: str) -> str:
    """NFKC for Arabic presentation forms only, so a lam-alef ligature (U+FEFB) reads as the
    logical "لا". Every other character is returned as it is."""
    code = ord(ch)
    if any(lo <= code <= hi for lo, hi in PRESENTATION_FORMS):
        return unicodedata.normalize("NFKC", ch)
    return ch


@lru_cache(maxsize=4096)
def _is_arabic(text: str) -> bool:
    """Whether every character of `text` is an Arabic letter (digits and marks are not)."""
    return bool(text) and all(
        ch.isalpha() and any(lo <= ord(ch) <= hi for lo, hi in ARABIC_BLOCKS) for ch in text)


def logical_order(chars: list[Char | None]) -> None:
    """Put right-to-left text into logical order and Arabic presentation forms back to their
    letters, in place. The letters are normalised last: reordering reads the ligature U+FEFB
    as one glyph, and its NFKC is the two letters "لا"."""
    _logical_joined_arabic(chars)
    _logical_rtl_runs(chars)
    for c in chars:
        if c is not None:
            c.ch = _normalize_presentation(c.ch)


def _char_box(c: Char) -> BBox:
    return (c.x0, c.top, c.x1, c.bottom)


def _logical_joined_arabic(chars: list[Char | None]) -> None:
    """Put Arabic letters that share one glyph box into logical order, in place.

    A joined or ligated Arabic glyph (lam-alef, or a letter with its neighbour in one glyph)
    is one box. The font's ToUnicode gives its letters in logical order, but pdfium reports
    them reversed, the order they are drawn left to right: alef before lam for the lam-alef
    ligature. Each group of such letters is reversed back to the ToUnicode order. A group that
    pdfium already reports as lam then alef is the lam-alef presentation form (U+FEFB), which
    pdfium expands in logical order, so it is left alone. Letters in separate boxes are not
    touched, so ordinary Arabic and mixed digits keep pdfium's order.
    """
    i = 0
    while i < len(chars):
        first = chars[i]
        if first is None or not _is_arabic(first.ch):
            i += 1
            continue
        j = i + 1
        while (j < len(chars) and chars[j] is not None and _is_arabic(chars[j].ch)
               and same_box(_char_box(chars[j]), _char_box(first))):
            j += 1
        group = chars[i:j]
        texts = [c.ch for c in group if c is not None]
        if j - i > 1 and texts != [_LAM, _ALEF]:
            for c, text in zip(group, reversed(texts)):
                if c is not None:
                    c.ch = text
        i = j


# Bidi classes of a run pdfium reverses as right-to-left: RTL letters, their combining marks
# and punctuation. A run with digits or Latin letters mixes directions and is left alone.
_RTL_STRONG = frozenset({"AL", "R"})


_RTL_RUN = _RTL_STRONG | {"NSM", "CS", "ON", "ES", "ET"}


_RTL_ATTACHED = frozenset({"NSM", "CS", "ON"})  # marks and punctuation


def _centre_x(c: Char) -> float:
    return (c.x0 + c.x1) / 2


def _logical_rtl_runs(chars: list[Char | None]) -> None:
    """Put the letters of each right-to-left text object into logical order, in place.

    pdfium reverses a text object whose text is right to left, which is right when the PDF
    draws its glyphs from left to right. Some producers (InDesign, Word) draw each connected
    piece of a word left to right but the pieces in logical order, so the reversal scrambles
    the letters ("إذا" comes out "اذإ"). On one line, right-to-left text reads from right to
    left, so a run that pdfium does not report right to left is sorted by its glyphs' centres.
    A combining mark follows the letter it sits on. A run already in order is not touched.
    Word spaces are then where pdfium put them, or a gap of RTL_WORD_GAP font sizes: these
    producers set word gaps with kerning, not a space character.

    pdfium also reports a space before a run of marks or punctuation drawn against the
    right-to-left letter before it (the colon of "تنبيه:", a vowel mark of its own); such a run
    has no space before it."""
    i = 0
    while i < len(chars):
        first = chars[i]
        if first is None or first.run is None:
            i += 1
            continue
        j = i + 1
        while j < len(chars) and chars[j] is not None and chars[j].run == first.run:
            j += 1
        run = chars[i:j]
        classes = [unicodedata.bidirectional(c.ch) for c in run]
        if (sum(k in _RTL_STRONG for k in classes) >= 2 and all(k in _RTL_RUN for k in classes)
                and not first.rotated):
            letters = sorted((c for c, k in zip(run, classes) if k != "NSM"), key=_centre_x,
                             reverse=True)
            marks: dict[int, list[Char]] = {}  # letter → the marks on it, in pdfium's order
            for c, k in zip(run, classes):
                if k == "NSM":  # on the letter whose centre is nearest
                    base = min(letters, key=lambda b: abs(_centre_x(b) - _centre_x(c)))
                    marks.setdefault(id(base), []).append(c)
            order = [m for c in letters for m in (c, *marks.get(id(c), ()))]
            if any(a is not b for a, b in zip(order, run)):
                spaced = {frozenset((id(a), id(b))) for a, b in zip(run, run[1:]) if b.space_before}
                lead = first.space_before
                for a, b in zip(order, order[1:]):
                    b.space_before = (frozenset((id(a), id(b))) in spaced
                                      or a.x0 - b.x1 > RTL_WORD_GAP * max(a.size, b.size))
                order[0].space_before = lead
                chars[i:j] = order
        elif (i and (prev := chars[i - 1]) is not None and prev.run != first.run
              and unicodedata.bidirectional(prev.ch) in _RTL_STRONG
              and all(k in _RTL_ATTACHED for k in classes)
              and prev.x0 - RTL_WORD_GAP * prev.size <= run[0].x1 and run[-1].x0 <= prev.x1):
            first.space_before = False
        i = j
