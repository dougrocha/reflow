"""Conservative checks for OCR text used as navigation or a document title, and the whitespace
normaliser the passes compare text with."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING

from reflow.model import Node, Source

if TYPE_CHECKING:
    from reflow.extract.lines import Line
    from reflow.extract.ocrwords import GradedWord


def squash(text: str) -> str:
    """The text with each run of whitespace made one space, and no space at either end."""
    return " ".join(text.split())


def fold(text: str) -> str:
    """The text squashed and case-folded: the form two texts are compared in when only their
    words count, not their spacing or case."""
    return squash(text).casefold()


def ocr_noise_reason(text: str, unicode_errors: int = 0) -> str | None:
    """Explain visible OCR corruption without guessing corrections or requiring a dictionary.

    These checks apply only to OCR. Suspect text can still be read as a paragraph, but
    its font size is insufficient evidence for promoting it to document navigation.
    """
    if unicode_errors:
        return "unmapped characters in OCR text"
    chars = [ch for ch in text if not ch.isspace() and ch not in "()[]-–—.,:;'’\"/&"]
    if not chars or sum(ch.isalpha() for ch in chars) / len(chars) < 0.6:
        return "low letter share in OCR text"
    if re.search(r"\d+[^\W\d_]{3,}|[^\W\d_]{3,}\d+[^\W\d_]", text):
        return "digits inside OCR words"
    for word in re.findall(r"[^\W\d_]+", text):
        if (word.lower() != word.upper()
                and not (word.islower() or word.isupper() or word.istitle())):
            return "implausible capitalization in OCR words"
    return None


def fragment_reason(text: str) -> str | None:
    """OCR of a drawing or a scrawl reads as scattered letters: two or more tokens, more than half
    of them of at most two characters, and none with four letters ("s S", "b LR SR S", "A el el
    e"). "Part A", "IV. B" and "U.S. EPA" are not fragments."""
    tokens = text.split()
    if (len(tokens) >= 2 and sum(len(t) <= 2 for t in tokens) * 2 > len(tokens)
            and not any(sum(ch.isalpha() for ch in t) >= 4 for t in tokens)):
        return "fragmentary OCR text"
    return None


def ocr_text_reason(text: str, unicode_errors: int = 0) -> str | None:
    """The lexical gate for OCR text whose words were not graded: `ocr_noise_reason`, then the
    fragment rule. The one text rule for ungraded OCR lines and nodes alike."""
    return ocr_noise_reason(text, unicode_errors) or fragment_reason(text)


def lowercase_start_reason(text: str) -> str | None:
    """OCR text whose first letter or digit is a lowercase letter is a piece of a sentence or a
    word ("and more...", "noted.", "ree D", "r--- I"), whatever OCR's confidence: never a heading."""
    first = next((ch for ch in text if ch.isalnum()), "")
    return "OCR text starts in lowercase: a piece of a sentence" if first.islower() else None


REMNANT_REASON = "most of its OCR line could not be read"
STRUCK_REASON = "most of its OCR words are struck through"


def doubt_reason(word: str) -> str:
    return f"OCR is unsure of the word ‘{word}’"


def ungraded_reason(lines: Iterable[Line]) -> str | None:
    """`ocr_text_reason` over the OCR lines whose words were not graded (`grades` None)."""
    for line in lines:
        if line.ocr and line.grades is None:
            if reason := ocr_text_reason(line.text, line.unicode_errors):
                return reason
    return None


def struck_text(words: Sequence[GradedWord]) -> bool:
    """Whether at least half the words are struck through (GradedWord.struck): a deletion,
    never a heading or a Contents entry, whatever the page's tracked-change gate says."""
    return sum(w.struck for w in words) * 2 >= len(words) > 0


def ocr_gate(lines: Iterable[Line], ranking: bool = False) -> str | None:
    """Why OCR text may not be navigation or a title, the first reason over the lines in order
    None when every line passes. The one rule for "may this OCR text be a heading":
      * OCR text that starts in lowercase fails (`lowercase_start_reason`, on the first line);
      * a graded line (`grades` a tuple) fails when at least half its words are struck through
        (`struck_text`), on a word graded doubtful (but a kept layer's word that is doubtful
        only because no reading confirms it, `unconfirmed`, sends its line to the lexical gate
        instead), and on a word left alone
        on its OCR line when OCR could not read most of that line (`GradedWord.remnant`: the
        confident letters of a scrawl, the word before a struck-out passage), and passes
        otherwise: its words were read confidently, whatever their spelling;
      * an ungraded OCR line (no grades: --ocr none, someone else's layer, lost grades) goes
        through the lexical gate, `ocr_text_reason`;
      * a born-digital line always passes.
    With `ranking` (headings.ranked_style: may this heading's type rank its style), a kept
    layer's word doubtful because a sure reading contradicts it (`contradicted`) counts as
    unconfirmed: the contradiction keeps it out of navigation, not its style out of the ranking."""
    for i, line in enumerate(lines):
        if not line.ocr:
            continue
        if i == 0 and (reason := lowercase_start_reason(line.text)):
            return reason
        grades = line.grades
        if grades is None:
            reason = ocr_text_reason(line.text, line.unicode_errors)
        else:
            def unsure(w: GradedWord) -> bool:
                return w.unconfirmed or (ranking and w.contradicted)
            word = next((w.text for w in grades if w.grade == "doubt" and not unsure(w)), None)
            unconfirmed = any(w.grade == "doubt" and unsure(w) for w in grades)
            reason = (STRUCK_REASON if grades and struck_text(grades)
                      else doubt_reason(word) if word is not None
                      else REMNANT_REASON if any(w.remnant for w in grades)
                      else ocr_text_reason(line.text, line.unicode_errors)
                      if unconfirmed else None)
        if reason:
            return reason
    return None


def node_gate(node: Node) -> str | None:
    """`ocr_gate` for a tree node, for every engine (a heading, a paragraph a tag would promote,
    the nodes of a title): it reads only the node.
      1. a `doubt` mark: OCR is unsure of that word;
      2. OCR text that starts in lowercase (`lowercase_start_reason`);
      3. a word left alone on a mostly unreadable OCR line (`attrs["ocr_remnant"]`, which
         text_evidence.flag_ocr_marks writes), or words at least half struck through
         (`attrs["ocr_struck"]`, likewise);
      4. else an OCR node whose words were not graded (`attrs["ocr"] == "ungraded"`, which
         flag_ocr_marks writes; or a Source.OCR node it has not seen), or that holds a kept
         layer's doubtful word no reading confirms (`attrs["ocr_unconfirmed"]`):
         `ocr_text_reason`;
      5. else None: born-digital, or OCR with every word graded and none doubtful."""
    mark = next((m for m in node.marks if m.kind == "doubt"), None)
    if mark is not None:
        return doubt_reason(mark.text)
    ocr = node.attrs.get("ocr")
    if ocr is None and node.source != Source.OCR:
        return None
    if reason := lowercase_start_reason(node.text or ""):
        return reason
    if node.attrs.get("ocr_remnant"):
        return REMNANT_REASON
    if node.attrs.get("ocr_struck"):
        return STRUCK_REASON
    if ocr == "ungraded" or ocr is None or node.attrs.get("ocr_unconfirmed"):
        return ocr_text_reason(node.text or "", int(node.attrs.get("unicode_errors", 0) or 0))
    return None
