"""Whether a page's text layer can be trusted, and the second witness that checks the ones that
can't (#308, tracker #303). This module is the one owner of that decision and the only writer of
its fact codes:

  text-disputed             the witness (OCR of the rendered page, reflow.ingest.witness) reads
                            other words than the text layer over these regions
  text-witness-unavailable  the page looked suspect, and no witness could read it (a language
                            Tesseract has no model for here, no Tesseract, OCR failed, or
                            it read none of the layer's lines). Unassessed never means agrees.

Both carry weight 0 (analyze.confidence): they put a page in the review queue and change no
node's confidence. Page trust is not stored; it is derived from these facts when it is read.

`preflight` runs inside reflow.prepare, after the pages are classified and before replacement
OCR (ingest.ocr.needs_ocr), on the pages whose text the conversion will read from the layer:

  1. `census` reads every visible character of a page from pdfium: its text, its font's name
     and whether pdfium could map it (FPDFText_HasUnicodeMapError).
  2. `judge_page` (pure) calls the page trust, doubt or gibberish, with reasons.
  3. Only the doubt and gibberish pages are witnessed: rendered and read by Tesseract, one
     process per worker slot, gathered in page order (ingest.witness.read_pages).
  4. `compare` aligns the witness's words with the layer's characters and returns the boxes of
     the spans where they disagree. Nothing is replaced: the transcript keeps the layer's text.

A clean page starts no Tesseract process and gets no fact.
"""

from __future__ import annotations

import ctypes
import difflib
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Literal

import pikepdf
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from reflow import geometry
from reflow.fontcensus import FontCensus, font_census
from reflow.ingest import witness
from reflow.ingest.ocr import MIN_LETTER_SHARE, letter_share
from reflow.model import BBox, Document, Fact, Node, PageKind, Role, Source
from reflow.pdfobjects import INVISIBLE, SOFT_BREAK, private_use, to_top_left

__all__ = ["CODES", "POLICY", "TrustReport", "Judgment", "judge_page", "preflight", "identity"]

FACT_DISPUTED = "text-disputed"
FACT_UNAVAILABLE = "text-witness-unavailable"
CODES = (FACT_DISPUTED, FACT_UNAVAILABLE)
# The version of the rules below. It is in the model key (reflow.saved.identify) with the
# witness's languages and Tesseract data, so a change to either makes a saved model stale.
# 2: the witness disputes a line of private-use characters (#342).
POLICY = 2

Verdict = Literal["trust", "doubt", "gibberish"]

# --- the judge's thresholds (tuned on the spec corpus and the robustness kinds; see the PR of
# #308 for the measurements). The first three follow Docling's broken-text-layer check
# (docling/utils/text_quality.py).
LEGACY_FONT = re.compile(r"navajo", re.IGNORECASE)   # as reflow.extract.text.LEGACY_FONT
BAD_SHARE = 0.03          # PUA, U+FFFD and C0 controls among the non-space characters: gibberish
MIN_LETTERS = 200         # letters in 3-20-letter tokens before the Latin rules speak
MIN_VOWELS = 0.25         # vowels among those letters, under which Latin text is gibberish
# A reason on a page already suspect, never a verdict on its own: a shifted alphabet loses every
# common word, but a provider directory's names and addresses keep only 1-3% of them.
COMMON_WORDS = 0.04
MIN_TOKENS = 40           # tokens of 3-20 letters before the common-word rule speaks
MAP_ERRORS = 0.02         # characters pdfium could not map: doubt at or above this share
MIXED_SCRIPT_WORDS = 10   # words mixing Latin with Greek or Cyrillic letters: doubt from this many
LATIN_LANGS = frozenset({"", "en", "es"})   # /Lang values the Latin rules apply to

VOWELS = frozenset("aeiouáéíóúàèìòùäëïöüâêîôûAEIOUÁÉÍÓÚÀÈÌÒÙÄËÏÖÜÂÊÎÔÛ")
# Common words of English and Spanish of three letters or more (the tokens the rules count).
COMMON = frozenset("""
the and for are was with that this from have not but you your they their them will would
can all any has had his her our out who which what when where there here than then into
about more other some such only also each been were may must should shall could under over
after before between number name date form part line page see one two new use used yes
del las los una por con para que como más sus este esta son está pero sin sobre entre
también cuando usted ustedes nombre fecha número página parte información
""".split())

# A Latin-script word: ASCII letters and the Latin-1 and Latin Extended-A letters. CJK, Greek or
# Cyrillic letters end a token, so they never count as consonants of Latin text.
_TOKEN = re.compile(r"[A-Za-z\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u017f]+")
_LETTER = re.compile(r"[^\W\d_]")
MIN_LATIN = 0.8           # Latin letters among all the letters before the Latin rules speak


@dataclass(frozen=True)
class CensusChar:
    ch: str
    font: str
    map_error: bool
    box: BBox            # top-left displayed points (pdfobjects.to_top_left)


@dataclass(frozen=True)
class Judgment:
    verdict: Verdict
    reasons: tuple[str, ...] = ()

    @property
    def suspect(self) -> bool:
        return self.verdict != "trust"


@dataclass
class TrustReport:
    """What the preflight found, for later passes (#310 reads `witnessed`): the judgment of each
    page it read, and the witness's words of each page it witnessed."""
    judged: dict[int, Judgment] = field(default_factory=dict)
    witnessed: dict[int, tuple[witness.WitnessWord, ...]] = field(default_factory=dict)
    unavailable: dict[int, Unavailable] = field(default_factory=dict)   # page -> why


def census(page: pdfium.PdfPage, invisible: bool = False) -> list[CensusChar]:
    """The page's characters in pdfium's order (spaces included; render mode 3 only when
    `invisible`): text, font name, map error, box. One FFI call per character and one font read
    per text object."""
    textpage = page.get_textpage()
    buf = ctypes.create_string_buffer(256)
    fonts: dict[int, tuple[str, bool]] = {}
    out: list[CensusChar] = []
    try:
        for i in range(textpage.count_chars()):
            code = pdfium_c.FPDFText_GetUnicode(textpage.raw, i)
            obj = pdfium_c.FPDFText_GetTextObject(textpage.raw, i)
            if not obj:   # a space or break pdfium generated: kept, it parts words and lines
                ch = chr(code) if code < 0x110000 else " "
                if ch.isspace():
                    out.append(CensusChar(ch, "", False, (0.0, 0.0, 0.0, 0.0)))
                continue
            key = ctypes.cast(obj, ctypes.c_void_p).value or 0
            if key not in fonts:
                font = pdfium_c.FPDFTextObj_GetFont(obj)
                name = ""
                if font:
                    n = pdfium_c.FPDFFont_GetBaseFontName(font, buf, len(buf))
                    name = buf.value.decode("latin-1") if n else ""
                fonts[key] = (name, pdfium_c.FPDFTextObj_GetTextRenderMode(obj) == INVISIBLE)
            name, hidden = fonts[key]
            if hidden and not invisible:
                continue
            ch = chr(code) if code < 0x110000 else "�"
            box = to_top_left(page, textpage.get_charbox(i))
            out.append(CensusChar(ch, name, bool(pdfium_c.FPDFText_HasUnicodeMapError(textpage.raw, i)),
                                  box))
    finally:
        textpage.close()
    return out


def _bad(ch: str) -> bool:
    """A character no text layer means: the Private Use Area, U+FFFD, or a C0 control other than
    whitespace and pdfium's soft-hyphen mark."""
    o = ord(ch)
    return (private_use(ch) or ch == "�"
            or (o < 0x20 and not ch.isspace() and ch != SOFT_BREAK))


def _script(ch: str) -> str:
    name = unicodedata.name(ch, "")
    return "latin" if name.startswith("LATIN") else "greek" if name.startswith("GREEK") else \
        "cyrillic" if name.startswith("CYRILLIC") else "other"


def latin_stats(text: str) -> tuple[int, int, float, float]:
    """(tokens, letters, vowel share, common-word share) of the 3-20-letter Latin tokens of
    `text`; all zero when under MIN_LATIN of its letters are Latin."""
    tokens = [t for t in _TOKEN.findall(text) if 3 <= len(t) <= 20]
    letters = sum(len(t) for t in tokens)
    if not tokens or sum(len(t) for t in _TOKEN.findall(text)) < MIN_LATIN * len(_LETTER.findall(text)):
        return 0, 0, 0.0, 0.0
    vowels = sum(ch in VOWELS for t in tokens for ch in t)
    common = sum(t.lower() in COMMON for t in tokens)
    return len(tokens), letters, vowels / letters, common / len(tokens)


def judge_page(chars: Sequence[CensusChar], lang: str = "",
               census: FontCensus | None = None, page: int = 0) -> Judgment:
    """Trust, doubt or gibberish for a page's characters, with reasons. Pure.
    gibberish: a font named for a legacy encoding (Navajo); PUA, U+FFFD and C0 controls make up
               BAD_SHARE of the non-space characters; or, for Latin text (/Lang en, es or
               none) with MIN_LETTERS letters in 3-20-letter tokens, fewer than MIN_VOWELS
               vowels.
    doubt:     letters in a font whose /ToUnicode reads them as other letters than its encoding
               names them (reflow.fontcensus `remapped`); MAP_ERRORS of the characters unmapped;
               MIXED_SCRIPT_WORDS words mixing Latin with Greek or Cyrillic.
    A page whose /Lang the Latin rules don't read is at most doubt. Two signals only add a
    reason to a page already suspect, as they flag clean pages on their own (measured on the
    spec corpus): fewer than COMMON_WORDS common words (a provider directory has 1-3%), and a
    font the audit flags (Type3 or symbolic without /ToUnicode: TeX's fonts all are)."""
    lang = (lang or "").split("-")[0].lower()
    shown = [c for c in chars if not c.ch.isspace()]
    if not shown:
        return Judgment("trust")
    gib: list[str] = []
    doubt: list[str] = []
    hint = ""
    legacy = sorted({c.font for c in shown if LEGACY_FONT.search(c.font)})
    if legacy:
        gib.append(f"legacy-encoded font {legacy[0]}")
    bad = sum(_bad(c.ch) for c in shown)
    if bad and bad / len(shown) >= BAD_SHARE:
        gib.append(f"{bad} of {len(shown)} characters are private-use, U+FFFD or controls")
    if lang in LATIN_LANGS:
        tokens, letters, vowels, common = latin_stats("".join(c.ch for c in chars))
        if letters >= MIN_LETTERS and vowels < MIN_VOWELS:
            gib.append(f"{vowels:.0%} vowels in {letters} letters")
        if tokens >= MIN_TOKENS and common < COMMON_WORDS:
            hint = f"{common:.0%} common words in {tokens} words"   # corroborates only
    if census is not None:
        moved = census.remapped_on(page)
        if moved and any(c.font.rpartition("+")[2] in moved and c.ch.isalpha() for c in shown):
            doubt.append("the /ToUnicode of " + ", ".join(sorted(moved)[:3]) + " reads its "
                         "letters as other letters than its encoding names them")
    errors = sum(c.map_error for c in shown)
    if errors and errors / len(shown) >= MAP_ERRORS:
        doubt.append(f"{errors} of {len(shown)} characters unmapped")
    words = re.findall(r"\S+", "".join(c.ch for c in chars))
    mixed = sum(1 for w in words if "latin" in (s := {_script(ch) for ch in w if ch.isalpha()})
                and s & {"greek", "cyrillic"})
    if mixed >= MIXED_SCRIPT_WORDS:
        doubt.append(f"{mixed} words mix Latin with Greek or Cyrillic letters")
    if hint and (gib or doubt):
        doubt.append(hint)
    if census is not None and (gib or doubt):   # corroborates only: TeX's fonts are all flagged
        flagged = census.no_unicode_on(page)
        if flagged and any(c.font.rpartition("+")[2] in flagged and c.ch.isalpha() for c in shown):
            doubt.append("a Type3 or symbolic font without /ToUnicode: " + ", ".join(sorted(flagged)[:3]))
    if gib and lang in LATIN_LANGS:
        return Judgment("gibberish", tuple(gib + doubt))
    if gib:   # a language the witness may not read: at most doubt
        return Judgment("doubt", tuple(gib + doubt))
    if doubt:
        return Judgment("doubt", tuple(doubt))
    return Judgment("trust")


# --- the comparison ---------------------------------------------------------------------

LINE_LETTERS = 8          # letters a layer line needs before it can be disputed
LINE_AGREES = 0.6         # character similarity at or above which a line agrees with the witness
WITNESSED = 0.5           # share of a line's letters the witness must cover to testify on it
PAGE_DISPUTED = 0.1       # share of the witnessed letters in disputed lines a page needs
# Why a page the witness read is still unavailable: no layer line of LINE_LETTERS letters had
# WITNESSED of its letters read back (faint or tiny print, or sideways text Tesseract reads as
# noise). Not a failure: the same image reads the same way again, so a saved model keeps it.
UNREAD = "the OCR read none of the text layer's lines"


class WhyNot(StrEnum):
    """Why no witness read a page: the code of an Unavailable. reflow.saved asks for OCR_FAILED
    (a later run must OCR again); the others it keeps, as the same image reads the same way."""
    OCR_OFF = "ocr-off"
    NO_TESSERACT = "no-tesseract"
    NO_MODEL = "no-model"        # detail: the document's language
    OCR_FAILED = "ocr-failed"    # detail: the error
    UNREAD = "unread"


@dataclass(frozen=True)
class Unavailable:
    """Why a page was not read, as a code with its detail; `str()` is the words a fact says."""
    code: WhyNot
    detail: str = ""

    def __str__(self) -> str:
        match self.code:
            case WhyNot.OCR_OFF:
                return "OCR is off (--ocr none)"
            case WhyNot.NO_TESSERACT:
                return "Tesseract is not installed"
            case WhyNot.NO_MODEL:
                return f"no OCR model here for the document's language ({self.detail})"
            case WhyNot.OCR_FAILED:
                return f"OCR failed: {self.detail}"
            case WhyNot.UNREAD:
                return UNREAD


@dataclass(frozen=True)
class LayerLine:
    text: str
    box: BBox
    words: tuple[tuple[str, BBox], ...]


def layer_lines(chars: Sequence[CensusChar]) -> list[LayerLine]:
    """The page's text as lines of words, in pdfium's order: a line ends at a line break pdfium
    put in its text or where the next character starts on another line."""
    lines: list[list[list[CensusChar]]] = []
    words: list[list[CensusChar]] = []
    word: list[CensusChar] = []

    def end_word() -> None:
        nonlocal word
        if word:
            words.append(word)
        word = []

    def end_line() -> None:
        nonlocal words
        end_word()
        if words:
            lines.append(words)
        words = []

    last: CensusChar | None = None
    for c in chars:
        if c.ch in "\r\n":
            end_line()
            last = None
            continue
        if c.ch.isspace():
            end_word()
            continue
        if last is not None:
            height = max(1.0, last.box[3] - last.box[1])
            if abs(c.box[1] - last.box[1]) > 0.6 * height and abs(c.box[3] - last.box[3]) > 0.6 * height:
                end_line()
        word.append(c)
        last = c
    end_line()
    out = []
    for ws in lines:
        made = tuple(("".join(c.ch for c in w), _union([c.box for c in w])) for w in ws)
        out.append(LayerLine(" ".join(t for t, _ in made), _union([b for _, b in made]), made))
    return out


def _union(boxes: Sequence[BBox]) -> BBox:
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def _letters(text: str) -> str:
    """The letters and digits of `text`, folded, with its private-use characters: what the
    layer reads where it should read a letter, which the witness's letters then dispute."""
    return "".join(ch for ch in text.casefold() if ch.isalnum() or private_use(ch))


def _inside(word: BBox, line: BBox) -> bool:
    """The word's centre is in the line's box, widened by half its height up and down."""
    cx, cy = (word[0] + word[2]) / 2, (word[1] + word[3]) / 2
    pad = (line[3] - line[1]) / 2
    return line[0] - pad <= cx <= line[2] + pad and line[1] - pad <= cy <= line[3] + pad


def compare(lines: Sequence[LayerLine], words: Sequence[witness.WitnessWord]
            ) -> tuple[list[BBox], int, int]:
    """(disputed line boxes, letters in them, letters witnessed). Each witness word goes to the
    first layer line whose box holds its centre; a line is witnessed when the words it holds
    cover WITNESSED of its letters, and disputed when its letters and theirs, aligned in order
    (difflib), agree less than LINE_AGREES. A line the witness did not read is unassessed,
    never disputed. A private-use character is a letter of the layer's here: a font whose
    /ToUnicode reads into the Private Use Area gives its lines no other."""
    held: list[list[witness.WitnessWord]] = [[] for _ in lines]
    for w in words:
        for i, line in enumerate(lines):
            if _inside(w.box, line.box):
                held[i].append(w)
                break
    disputed: list[BBox] = []
    bad = seen = 0
    for line, ws in zip(lines, held, strict=True):
        mine = _letters(line.text)
        if sum(ch.isalpha() or private_use(ch) for ch in mine) < LINE_LETTERS or not ws:
            continue
        theirs = _letters(" ".join(w.text for w in sorted(ws, key=lambda w: w.box[0])))
        if len(theirs) < WITNESSED * len(mine):
            continue
        seen += len(mine)
        if difflib.SequenceMatcher(None, mine, theirs, autojunk=False).ratio() < LINE_AGREES:
            disputed.append(line.box)
            bad += len(mine)
    return disputed, bad, seen


# --- the preflight ----------------------------------------------------------------------

def identity() -> dict[str, object]:
    """What the witness's readings depend on beyond the code and tool versions, for the model
    key (reflow.saved.identify): the policy version and, per language, its data (None when it
    can't be identified, or the language isn't installed)."""
    models = sorted(set(witness.LANG_MODELS.values()))
    return {"policy": POLICY, "scale": witness.SCALE,
            "data": {m: witness.environment((m,)) for m in models}}


def preflight(doc: Document, working: Path, *, lock, executor=None, enabled: bool = True,
              reader=None) -> TrustReport:
    """Judge the text layer of the document's BORN_DIGITAL pages, and of its MIXED pages whose
    text replacement OCR keeps (a letter share of MIN_LETTER_SHARE or more: their visible text,
    and an invisible layer someone else's OCR made), witness the suspect ones, and write the
    facts (module docstring) onto those pages. `enabled` False (--ocr none) witnesses
    nothing, so a suspect page is unavailable. `lock` is the pdfium lock: held while this
    reads the working copy here, released while the witness tasks run on `executor`, and held
    around them when there is none (a serial run renders in this process). `reader`, when given, makes
    the witness's page reader (witness.read_pages `read`; the stage cache's when it is on), and is
    called only when a page is witnessed."""
    report = TrustReport()
    numbers = [p.number for p in doc.pages if p.kind in (PageKind.BORN_DIGITAL, PageKind.MIXED)]
    mixed = {p.number for p in doc.pages if p.kind is PageKind.MIXED}
    if not numbers:
        return report
    lang = str(doc.meta.get("lang") or "")
    lines: dict[int, list[LayerLine]] = {}
    with lock:
        with pikepdf.open(working) as pk:
            fonts = font_census(pk)
        pdf = pdfium.PdfDocument(working)
        try:
            for n in numbers:
                page = pdf[n - 1]
                try:
                    chars = census(page, invisible=n in mixed)
                finally:
                    page.close()
                if n in mixed and letter_share("".join(c.ch for c in chars)) < MIN_LETTER_SHARE:
                    continue   # replacement OCR reads this page again (ingest.ocr.needs_ocr)
                judged = judge_page(chars, lang, fonts, n)
                report.judged[n] = judged
                if judged.suspect:
                    lines[n] = layer_lines(chars)
        finally:
            pdf.close()
    if not lines:
        return report
    langs = witness.languages(lang)
    if not enabled:
        why = Unavailable(WhyNot.OCR_OFF)
    elif not witness.available():
        why = Unavailable(WhyNot.NO_TESSERACT)
    elif langs is None:
        why = Unavailable(WhyNot.NO_MODEL, lang)
    else:
        why = None
    got_pages: dict[int, witness.WitnessPage] = {}
    if why is None:
        got_pages = witness.read_pages(working, {n: langs for n in lines},
                                       executor=executor, lock=lock,
                                       read=reader() if reader is not None else None)
    by_number = {p.number: p for p in doc.pages}
    for n in sorted(lines):
        judged = report.judged[n]
        page = by_number[n]
        got = got_pages.get(n)
        if got is None or got.error:
            reason = why or Unavailable(WhyNot.OCR_FAILED, got.error if got else "not read")
            report.unavailable[n] = reason
            page.facts.append(Fact(FACT_UNAVAILABLE, Source.HEURISTIC,
                                   "The text layer looks wrong and could not be checked against "
                                   f"the page image: {reason}",
                                   {"verdict": judged.verdict, "reasons": list(judged.reasons)}))
            continue
        boxes, bad, seen = compare(lines[n], got.words)
        if not seen:   # unassessed never means agrees: no line the witness could testify on
            reason = Unavailable(WhyNot.UNREAD)
            report.unavailable[n] = reason
            page.facts.append(Fact(FACT_UNAVAILABLE, Source.HEURISTIC,
                                   "The text layer looks wrong and could not be checked against "
                                   f"the page image: {reason}",
                                   {"verdict": judged.verdict, "reasons": list(judged.reasons)}))
            continue
        report.witnessed[n] = got.words
        if boxes and seen and bad / seen >= PAGE_DISPUTED:
            page.facts.append(Fact(FACT_DISPUTED, Source.OCR,
                                   "OCR of the page image reads other words than the text layer "
                                   "here; the text is kept as the layer gives it",
                                   {"verdict": judged.verdict, "reasons": list(judged.reasons),
                                    "region_boxes": [round(v, 2) for b in boxes for v in b]}))
    return report


def recheck(doc: Document, working: Path, report: TrustReport, pages: Sequence[int], *,
            lock) -> None:
    """After the font repair (ingest.font_map) changed the text of `pages` in the working copy:
    judge each again and compare it again with the witness words the preflight read (no new
    OCR), so its facts describe the text the conversion now reads. A page now trusted loses
    them; a witnessed page keeps text-disputed only where the patched text still disagrees; a
    page the witness could not read keeps its text-witness-unavailable while it looks wrong.
    `report.unavailable` is left as it is (reflow.saved reads `failed` from it)."""
    numbers = [n for n in sorted(set(pages)) if n in report.judged]
    if not numbers:
        return
    lang = str(doc.meta.get("lang") or "")
    mixed = {p.number for p in doc.pages if p.kind is PageKind.MIXED}
    by_number = {p.number: p for p in doc.pages}
    with lock:
        with pikepdf.open(working) as pk:
            fonts = font_census(pk)
        pdf = pdfium.PdfDocument(working)
        try:
            read: dict[int, list[CensusChar]] = {}
            for n in numbers:
                page = pdf[n - 1]
                try:
                    read[n] = census(page, invisible=n in mixed)
                finally:
                    page.close()
        finally:
            pdf.close()
    for n in numbers:
        judged = judge_page(read[n], lang, fonts, n)
        report.judged[n] = judged
        page = by_number[n]
        kept = [f for f in page.facts if f.code == FACT_UNAVAILABLE] if judged.suspect else []
        page.facts = [f for f in page.facts if f.code not in CODES] + kept
        if not judged.suspect or n not in report.witnessed:
            continue
        boxes, bad, seen = compare(layer_lines(read[n]), report.witnessed[n])
        if boxes and seen and bad / seen >= PAGE_DISPUTED:
            page.facts.append(Fact(FACT_DISPUTED, Source.OCR,
                                   "OCR of the page image reads other words than the text layer "
                                   "here; the text is kept as the layer gives it",
                                   {"verdict": judged.verdict, "reasons": list(judged.reasons),
                                    "region_boxes": [round(v, 2) for b in boxes for v in b]}))


def failed(report: TrustReport | MissingReport) -> list[int]:
    """The pages whose witness (or flag_missing's OCR) failed to run (not those it has no model
    for): a saved model must not keep them (reflow.saved)."""
    return sorted(n for n, why in report.unavailable.items() if why.code is WhyNot.OCR_FAILED)


# --- words printed on the page that the transcript lacks (#310) --------------------------
#
#   words-missing  OCR reads confident words in ink that the page's text layer does not draw
#                  and no figure covers (analyze.page_integrity.covers_words), more of them
#                  than the transcript holds beyond the layer's own words: text an
#                  annotation's appearance, a pattern or outlines draw. An error of weight 0, with the boxes of the words, grouped
#                  into crops. A page without it makes no claim unless it was assessed
#                  (MissingReport.assessed): coverage holds only there.

FACT_MISSING = "words-missing"
MISSING_CONF = 80.0       # Tesseract confidence a word needs before it is missing
TALL_WORD = 14.0          # points of height from which a token of two letters or digits is a
                          # word (`_tall`): a title's "W-9", type of 20 pt or so
MASK_PAD = 0.25           # a layer run's box grows by this share of its height in the mask
DARK = 160                # grey under which a pixel of the masked render is ink
MIN_INK = 24              # pixels at 72 dpi of ink that could be a word (_ink) before OCR
                          # runs
RULE = 24                 # points of unbroken ink, across or down, that make a rule (_ink)
SMEAR = 3                 # points along x that join a word's letters (_ink)
WORD = 9                  # points along x a smeared word runs at least: two 8 pt digits
RUNNING = 6               # points a word may move between pages and still be one mark (_running)
_WORD = re.compile(r"[^\W_]+")


@dataclass
class MissingReport:
    """What flag_missing assessed: the pages it compared, and per page the figures that covered
    their area (its `covers`), recorded apart from the words the transcript covers: the OCR never
    reads inside them, so they cover what they show whatever it is."""
    assessed: list[int] = field(default_factory=list)
    figures: dict[int, int] = field(default_factory=dict)
    unavailable: dict[int, Unavailable] = field(default_factory=dict)   # page -> why not assessed


def _tokens(text: str) -> list[str]:
    return _WORD.findall(unicodedata.normalize("NFKC", text).casefold())


def _word(text: str) -> bool:
    """A word that can be reported missing: a run of three letters or digits, or a number of
    two digits ("100", "12"). Shorter pieces are what OCR makes of rules, boxes and marks, and
    of the edge of a word the mask cut ("ol", "SS")."""
    return any(len(t) >= 3 or (len(t) == 2 and t.isdigit())
               for t in _WORD.findall(text.casefold()))


def _tall(word: witness.WitnessWord) -> bool:
    """A short token set in large type, which `_word` would not report: a title drawn by an
    annotation reads "W-9". The pieces OCR makes of rules, boxes and cut edges are a glyph's
    height at most, so a token TALL_WORD points tall with two letters or digits is a word."""
    return (word.box[3] - word.box[1] >= TALL_WORD
            and sum(ch.isalnum() for ch in word.text) >= 2)


def _running(missing: dict[int, list[witness.WitnessWord]]) -> set[tuple[int, int]]:
    """(page, index) of each missing word that another page misses too, the same word within
    RUNNING points of the same place: a letterhead or running mark, which reflow leaves out of
    a transcript as it does a running header."""
    def key(w: witness.WitnessWord) -> tuple[str, float, float]:
        return (w.text.casefold(), (w.box[0] + w.box[2]) / 2, (w.box[1] + w.box[3]) / 2)

    places: dict[str, list[tuple[int, float, float]]] = {}
    for n, words in missing.items():
        for w in words:
            text, x, y = key(w)
            places.setdefault(text, []).append((n, x, y))
    out = set()
    for n, words in missing.items():
        for i, w in enumerate(words):
            text, x, y = key(w)
            if any(m != n and abs(mx - x) <= RUNNING and abs(my - y) <= RUNNING
                   for m, mx, my in places[text]):
                out.add((n, i))
    return out


def _grown(box: BBox, share: float) -> BBox:
    pad = share * (box[3] - box[1])
    return (box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad)


def _run(ink, length: int, op, step: int):
    """`op` (ImageChops.darker or lighter) of `ink` and its copies shifted by `step` x 0 ..
    length-1 pixels along x, by doubling: a min over a window ahead (step -1) or a max over a
    window behind (step 1). The image carries a blank border, so the wrap-around of
    ImageChops.offset brings in nothing."""
    from PIL import ImageChops

    out, k = ink, 1
    while 2 * k <= length:
        out = op(out, ImageChops.offset(out, step * k, 0))
        k *= 2
    return op(out, ImageChops.offset(out, step * (length - k), 0)) if k < length else out


def _runs(ink, length: int):
    """The pixels of `ink` (L: 255 is ink) in a run of `length` or more along x."""
    from PIL import ImageChops

    return _run(_run(ink, length, ImageChops.darker, -1), length, ImageChops.lighter, 1)


def _layer(page: pdfium.PdfPage) -> tuple[list[BBox], Counter[str]]:
    """The boxes of the page's text layer, as pdfium joins its characters into runs on a line
    (top-left points, grown by MASK_PAD), and the words of its text, counted. Invisible and
    white text are in both: a word no reader sees is no missing word."""
    textpage = page.get_textpage()
    try:
        boxes = [_grown(to_top_left(page, textpage.get_rect(i)), MASK_PAD)
                 for i in range(textpage.count_rects())]
        return boxes, Counter(_tokens(textpage.get_text_range()))
    finally:
        textpage.close()


def _render(page: pdfium.PdfPage, scale: float, boxes: Sequence[BBox]):
    """The page rendered at `scale` in grey, with `boxes` (top-left points) painted white."""
    from PIL import ImageDraw

    bitmap = page.render(scale=scale)
    try:
        image = bitmap.to_pil().convert("L")
    finally:
        bitmap.close()
    draw = ImageDraw.Draw(image)
    for b in boxes:
        draw.rectangle((b[0] * scale, b[1] * scale, b[2] * scale, b[3] * scale), fill=255)
    return image


def _ink(page: pdfium.PdfPage, boxes: Sequence[BBox]) -> tuple[int, BBox | None]:
    """The pixels of the page at 72 dpi, outside `boxes`, of ink that could be a word, and their
    bounding box (top-left points, grown by RULE): rules and filled boxes taken out (ink in a
    run of RULE points or more, across or down), the rest smeared by SMEAR points along x so a
    word's letters join, and only what then runs WORD points or more along x kept: a lone
    glyph, a checkmark or an icon is no word. PIL only."""
    from PIL import Image, ImageChops, ImageOps

    grey = _render(page, 1.0, boxes)
    ink = ImageOps.expand(grey.point(lambda v: 255 if v < DARK else 0), border=RULE + WORD,
                          fill=0)
    turn = Image.Transpose.TRANSPOSE
    across, down = _runs(ink, RULE), _runs(ink.transpose(turn), RULE).transpose(turn)
    ink = ImageChops.subtract(ink, ImageChops.lighter(across, down))
    words = _runs(_run(ink, SMEAR + 1, ImageChops.lighter, 1), WORD)
    found, pad = words.getbbox(), RULE + WORD
    if found is None:
        return 0, None
    return words.histogram()[255], (max(0.0, found[0] - pad - RULE), max(0.0, found[1] - pad - RULE),
                                    min(grey.width, found[2] - pad + RULE),
                                    min(grey.height, found[3] - pad + RULE))


def flag_missing(doc: Document, working: Path, trust: TrustReport | None, *, lock,
                 covers: Callable[[Node], bool] = lambda figure: False,
                 skip: frozenset[int] | set[int] = frozenset(), enabled: bool = True
                 ) -> MissingReport:
    """Flag the words printed on a BORN_DIGITAL page that its transcript lacks: "words-missing"
    (above) with the boxes of the missing words. Runs after the alt text exists (pipeline.finish),
    so a figure counts as covered only when `covers` says so (the pipeline passes
    analyze.page_integrity.covers_words: its alt describes it, or a finding already sends a
    reviewer to it). Pages in `skip` (the ones reflow OCR'd: their unread words are
    "ocr-words-omitted") and pages without transcript text ("empty-transcript" is theirs) are
    not assessed.

    The words compared: on a page the preflight witnessed (trust.witnessed), the witness's
    words outside the text layer's runs and the covering figures; on another page, OCR of the
    ink the layer's runs and the covering figures leave, in this process and only when ink
    other than rules is left (`_ink`, MIN_INK). A word is three letters or digits, a two-digit
    number (`_word`), or a short token set tall (`_tall`). Those words are counted against the
    transcript's words beyond the layer's own, one occurrence each: one surviving "100" covers
    one missing "100", not three. A word another page misses at the same place is a running
    mark (`_running`), not reported. `lock` is the pdfium lock, held while this reads the
    working copy and released while Tesseract reads. Running again replaces this pass's
    facts."""
    report = MissingReport()
    for info in doc.pages:
        info.facts = [f for f in info.facts if f.code != FACT_MISSING]
    said: dict[int, Counter[str]] = {}
    covered: dict[int, list[BBox]] = {}
    for node in doc.root.walk():
        if node.page is None:
            continue
        if node.role == Role.FIGURE and node.bbox is not None and covers(node):
            covered.setdefault(node.page, []).append(node.bbox)
        if node.role not in (Role.ARTIFACT, Role.TR) and node.text and node.text.strip():
            said.setdefault(node.page, Counter()).update(_tokens(node.text))
    numbers = [p.number for p in doc.pages if p.kind is PageKind.BORN_DIGITAL
               and p.number in said and p.number not in skip]
    if not numbers:
        return report
    lang = str(doc.meta.get("lang") or "")
    langs = witness.languages(lang)
    why = (Unavailable(WhyNot.OCR_OFF) if not enabled else
           Unavailable(WhyNot.NO_TESSERACT) if not witness.available() else
           Unavailable(WhyNot.NO_MODEL, lang) if langs is None else None)
    if why is not None:
        report.unavailable = dict.fromkeys(numbers, why)
        return report
    witnessed = trust.witnessed if trust is not None else {}
    layer: dict[int, tuple[list[BBox], Counter[str]]] = {}
    images = {}
    missing: dict[int, list[witness.WitnessWord]] = {}
    with lock:
        pdf = pdfium.PdfDocument(working)
        try:
            for n in numbers:
                page = pdf[n - 1]
                try:
                    boxes, drawn = _layer(page)
                    layer[n] = boxes, drawn
                    masked = boxes + covered.get(n, [])
                    if n in witnessed:
                        continue
                    ink, area = _ink(page, masked)
                    if ink >= MIN_INK and area is not None:   # read only around that ink
                        s = witness.SCALE
                        images[n] = area, _render(page, s, masked).crop(
                            tuple(round(v * s) for v in area))
                finally:
                    page.close()
        finally:
            pdf.close()
    for n in numbers:
        boxes, drawn = layer[n]
        if n in witnessed:
            read = [w for w in witnessed[n] if not any(geometry.intersects(w.box, b) for b in boxes)]
        elif n in images:
            (x, y, _, _), image = images[n]
            try:
                read = [witness.WitnessWord(w.text, (round(w.box[0] + x, 2), round(w.box[1] + y, 2),
                                                     round(w.box[2] + x, 2), round(w.box[3] + y, 2)),
                                            w.conf)
                        for w in witness.read_image(image, langs, sparse=True)]
            except Exception as error:  # noqa: BLE001 - an unread page is unassessed, never fatal
                report.unavailable[n] = Unavailable(WhyNot.OCR_FAILED,
                                                    f"{type(error).__name__}: {error}")
                continue
        else:
            read = []
        report.assessed.append(n)
        if covered.get(n):
            report.figures[n] = len(covered[n])
        pool = said[n] - drawn   # what the transcript holds beyond the layer's own words
        missing[n] = []
        for w in sorted(read, key=lambda w: (w.box[1], w.box[0])):
            if w.conf < MISSING_CONF or not (_word(w.text) or _tall(w)):
                continue
            if any(geometry.centre_in(w.box, f) for f in covered.get(n, ())):
                continue
            lost = False
            for t in _tokens(w.text):
                if pool[t] > 0:
                    pool[t] -= 1
                else:
                    lost = True
            if lost:
                missing[n].append(w)
    running = _running(missing)
    by_number = {p.number: p for p in doc.pages}
    for n, words in missing.items():
        lost = [w for i, w in enumerate(words) if (n, i) not in running]
        if not lost:
            continue
        gap = sorted(w.box[3] - w.box[1] for w in lost)[len(lost) // 2]
        crops = geometry.group_boxes([w.box for w in lost], gap)
        data: dict[str, object] = {"count": len(lost), "words": [w.text for w in lost[:10]],
                                   "region_boxes": [v for b in crops for v in b]}
        if report.figures.get(n):
            data["figures_covered"] = report.figures[n]
        by_number[n].facts.append(Fact(FACT_MISSING, Source.OCR,
                                       f"{len(lost)} word(s) printed on the page are not in "
                                       "the transcript; see the original page", data))
    return report
