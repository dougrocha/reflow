"""The extracted line: its text, font, style runs and position, as every pass reads it."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal, NamedTuple

from reflow.model import BBox

if TYPE_CHECKING:
    from reflow.extract.ocrwords import GradedWord

SOFT_HYPHEN = "­"


class StyleRun(NamedTuple):
    """A maximal run of one style: offsets into Line.text. `symbol` is set for a run drawn in
    a symbol font (Wingdings, ZapfDingbats, Symbol): a check box or bullet, never emphasis.
    `accent` is the named colour ("red", "green", "blue") the run is printed in, or "" for the
    text colour (see extract.chars.accent_of)."""
    start: int
    end: int
    bold: bool
    italic: bool
    symbol: bool = False
    accent: str = ""


@dataclass
class Line:
    page: int
    bbox: BBox
    text: str
    size: float  # most common font size, points
    bold: bool
    italic: bool
    font: str
    unicode_errors: int = 0  # chars pdfium couldn't map to Unicode (SRC-TXT-002)
    rotated: bool = False  # vertical/rotated text, e.g. the arXiv stamp in the margin
    turn: int = 0  # rotated text's reading direction: 1 upward, -1 downward, 2 upside down
    angle: float = 0.0  # the baseline's direction in [0, 2π), counter-clockwise (rotated text)
    ink: float = 1.0  # how dark the glyphs look on white: 1 black, 0 white (see _ink)
    ocr: bool = False  # invisible text layer, including OCR already present in the input PDF
    # Runs of small, raised characters inside `text` (start, end offsets): the footnote
    # markers set as superscripts in body text. See _raised_runs.
    sup: tuple[tuple[int, int], ...] = ()
    # Words holding an "f" drawn as wide as a fi or fl ligature but extracted as a bare "f":
    # the glyph's other letter is missing from the text layer (see _dropped_ligature_words).
    ligature_words: tuple[str, ...] = ()
    # The line's style runs, when its characters differ in style (bold/italic as `bold` and
    # `italic` collapse them to one boolean each). Empty when every character has one style.
    runs: tuple[StyleRun, ...] = ()
    # The graded words of an OCR line (extract.ocrwords): None when it is ungraded (no
    # current grades for its page, or they did not match its text); a tuple, even empty, when
    # every word OCR kept on it was graded.
    grades: tuple[GradedWord, ...] | None = None
    # The stroke weight of an OCR line on a scanned page over the page's median
    # (scan.stroke_ratios): about 2 for bold type; None when it was not measured.
    stroke: float | None = None

    def cut(self, start: int, end: int, *, strip: bool = False) -> Line:
        """The part of the line in text[start:end], its runs and `sup` clipped and rebased.
        With `strip`, the offsets move in past the spaces at either end, so the cut keeps
        every offset field in step (a `.strip()` on the text would not)."""
        text = self.text
        start, end = max(start, 0), min(end, len(text))
        if strip:
            while start < end and text[start].isspace():
                start += 1
            while end > start and text[end - 1].isspace():
                end -= 1
        end = max(end, start)
        runs = tuple(r._replace(start=max(r.start, start) - start, end=min(r.end, end) - start)
                     for r in self.runs if r.start < end and r.end > start)
        sup = tuple((max(a, start) - start, min(b, end) - start)
                    for a, b in self.sup if a < end and b > start)
        return replace(self, text=text[start:end], runs=runs, sup=sup,
                       grades=_kept_grades(self.grades, text[start:end]))

    def joined(self, other: Line, sep: str = " ") -> Line:
        """This line followed by `other` on one row: the text joined by `sep`, the boxes
        united, `other`'s runs and `sup` shifted by the length of this text and `sep`. The
        result takes `other`'s size, style and font, as a list marker takes the style of its
        text."""
        at = len(self.text) + len(sep)
        runs = _merge_runs(
            [*(self.runs or _whole(self)), *(r._replace(start=r.start + at, end=r.end + at)
                                             for r in (other.runs or _whole(other)))], at)
        if len(runs) == 1 and not runs[0].symbol:
            runs = ()                       # one style throughout: the line's booleans say it
        return replace(
            other, page=self.page, text=self.text + sep + other.text,
            bbox=(min(self.x0, other.x0), min(self.top, other.top),
                  max(self.x1, other.x1), max(self.bottom, other.bottom)),
            unicode_errors=self.unicode_errors + other.unicode_errors, ocr=self.ocr or other.ocr,
            sup=(*self.sup, *((a + at, b + at) for a, b in other.sup)), runs=tuple(runs),
            grades=_joined_grades(self, other), stroke=None)

    def retext(self, new: str) -> Line:
        """The line with other text. Its runs and `sup` are kept only when the new text is as
        long as the old; otherwise they are dropped, and `bold` and `italic` stay as they
        were (a cosmetic loss, never a wrong mark)."""
        if len(new) == len(self.text):
            return replace(self, text=new, grades=_kept_grades(self.grades, new))
        return replace(self, text=new, runs=(), sup=(), grades=_kept_grades(self.grades, new))

    def edge_style(self, edge: Literal["head", "tail"]) -> tuple[bool, bool]:
        """(bold, italic) of the first or last non-space character; (bold, italic) of the whole
        line when it has one style, or when the text was edited after the runs were recorded."""
        if not self.runs or self.runs[-1].end != len(self.text):
            return self.bold, self.italic
        text = self.text
        at = len(text) - len(text.lstrip()) if edge == "head" else len(text.rstrip()) - 1
        if not 0 <= at < len(text):
            return self.bold, self.italic
        run = next(r for r in self.runs if r.start <= at < r.end)
        return run.bold, run.italic

    @property
    def x0(self) -> float: return self.bbox[0]
    @property
    def top(self) -> float: return self.bbox[1]
    @property
    def x1(self) -> float: return self.bbox[2]
    @property
    def bottom(self) -> float: return self.bbox[3]
    @property
    def height(self) -> float: return self.bbox[3] - self.bbox[1]


def _kept_grades(grades: tuple[GradedWord, ...] | None, text: str
                 ) -> tuple[GradedWord, ...] | None:
    """The graded words that a cut or rewritten line still prints (a word follows its text,
    since the box of a cut line is not narrowed)."""
    if grades is None:
        return None
    return tuple(w for w in grades if not w.layer_text or w.layer_text in text)


def _joined_grades(a: Line, b: Line) -> tuple[GradedWord, ...] | None:
    """The grades of two lines set on one row: ungraded when an OCR part is ungraded."""
    parts = [line.grades for line in (a, b) if line.ocr]
    if any(g is None for g in parts) or not parts:
        return None
    return tuple(w for g in parts for w in g)


def _whole(line: Line) -> tuple[StyleRun, ...]:
    """The line's one style as a single run, for a line that has no runs of its own."""
    return (StyleRun(0, len(line.text), line.bold, line.italic),) if line.text else ()


def _merge_runs(runs: list[StyleRun], at: int) -> list[StyleRun]:
    """Runs in order, the first part's last run reaching to `at` (the separator takes the style
    before it), neighbours of one style merged."""
    out: list[StyleRun] = []
    for i, run in enumerate(runs):
        if i and runs[i - 1].end < run.start == at:
            out[-1] = out[-1]._replace(end=at)
        if out and out[-1][2:] == run[2:]:
            out[-1] = out[-1]._replace(end=run.end)
        else:
            out.append(run)
    if out and out[0].start:
        out[0] = out[0]._replace(start=0)
    return out
