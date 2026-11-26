"""Tunables and patterns that more than one layout module reads."""

from __future__ import annotations

import re

from reflow.model import Why


# Headings: at least this much larger than body text, or bold and short.
HEADING_SCALE = 1.15
# A bold line this close to the body size (points) or larger may be a heading.
BODY_SIZE_SLACK = 0.5

# Docling's marker for a title: the font size its node carries, ranked above every heading
# (analyze.docling_layout; analyze.direct_html does the same for the model's h1).
DOCLING_TITLE_SIZE = 1000.0
# Confidence of nodes built from heuristics.
CONF_FIGURE_NO_ALT = 0.2  # a figure with no alt text
CONF_PARAGRAPH = 0.9
CONF_TABLE = 0.6  # structure from ruling lines; header row is a guess

# Symbol and Wingdings draw bullets from the Private Use Area: the page shows a bullet, but the
# text layer holds U+F0xx, which Unicode leaves undefined. Each code point maps to the bullet it
# shows. F0B7 and F0D8 are confirmed on NASA p47. The others are from the Symbol and Wingdings
# charts and are unverified against a sample.
PUA_BULLETS = {
    0xF0B7: "•",  # Symbol bullet
    0xF0A7: "▪",  # Wingdings small square
    0xF0D8: "➢",  # Wingdings arrowhead
    0xF0FC: "✓",  # Wingdings check mark
    0xF06C: "●",  # Wingdings filled circle
    0xF06E: "■",  # Wingdings filled square
    0xF075: "◆",  # Wingdings filled diamond
    0xF0A8: "◦",  # outline bullet (Symbol/Wingdings)
    0xF076: "❖",  # Wingdings diamond with plus
    0xF0D7: "➔",  # Wingdings heavy arrow
    0xF0E0: "⇨",  # Wingdings arrow
    0xF0F0: "⇨",  # Wingdings arrow
    0xF0B2: "▫",  # Wingdings small outline square
}

# A page reference at the end of a line: "Emergency Fund ....1201", "Tax ..... 3, 10, 15".
PAGE_REFERENCE = re.compile(r"\.{3,}\s*\d[\d,\s–-]*$")

# Dot leaders: "Office for Civil Rights ........ 339" in an index or a table of contents.
DOT_LEADER = re.compile(r"\.{4,}|(\.\s){4,}")

# An index entry that ends in page numbers without leaders: "Losses 23", "Tax 3, 10, 15".
INDEX_REFERENCE = re.compile(r"\s\d[\d,\s–-]*$")

TERMINAL = (".", ":", ";", "?", "!", ",")

BULLET_GLYPHS = "•◦▪▫●○■□‣⁃➢✓◆❖➔⇨"  # unambiguous bullets: a list on their own
LIST_BULLETS = BULLET_GLYPHS + "-–—*·"  # a bullet when a space follows: a dash, a star, a middle dot
OPTION_MARKER = re.compile(r"[\uf000-\uf8ff" + BULLET_GLYPHS + "]")  # what starts a row's option label

# Where a form's label sits against its field, in points (analyze.forms, analyze.form_grid, and
# the scan detector, analyze.scan_rules, which places its boxes by the same reach).
ABOVE_GAP = 14.0  # a label above a field ends at most this far over it
LEFT_GAP = 36.0  # a label left of a field ends at most this far from it
RIGHT_GAP = 24.0  # a checkbox's label starts at most this far right of it
HANG = 16.0  # in a form grid's box, the second line of a caption may be indented this far

# Bullets, "1." / "12)", "(1)", "a." / "(a)", "iv." / "(iv)". Parenthesized numbers stop
# at two digits so phone area codes like "(520) 555-0100" aren't list markers. The bullet
# set includes every glyph in PUA_BULLETS.
LIST_MARKER = re.compile(rf"^([{re.escape(LIST_BULLETS)}]|[0-9]{{1,3}}[.)]|\([0-9]{{1,2}}\)|\(?[a-z][.)]"
                         r"|\(?[ivx]{1,4}[.)])\s+", re.I)
# Two legal labels run together, the outer one opening the inner: "(a)(1) The Judicial", "(A)(i)"
LEGAL_CHAIN = re.compile(r"^(\((?:[a-z]{1,4}|[A-Z]{1,4}|\d{1,2})\))(\((?:[a-z]{1,4}|[A-Z]{1,4}|\d{1,2})\))\s+")
# Repeated context at the top of a page ("CARDIOLOGY (continued)"): not a new section.
CONTINUED = re.compile(r"\(\s*continued\s*\)\s*$", re.I)

# A callout's label on its own line ("TIP", "CAUTION:", "REMEMBER"): it names the note that follows
CALLOUT_LABEL = re.compile(r"^(TIP|CAUTION|NOTE|WARNING|IMPORTANT|REMEMBER|REMINDER)\s*:?$")
# The label of a line of a form, set in capitals beside its picture or in the list that explains
# it ("LINE 11", "LINE 24a LINE 24d", "Insured's name (LINE 4)"). It points at a line of the
# form: text, never a heading. Matched on the text without its spaces, since OCR runs these
# together ("LINE33") or splits them ("J. LI N E 24a": a stray column letter and its full stop
# may lead). Capitals only: "Line 7" is how a tax guide names a heading for the line's rules.
FORM_LINE_LABEL = re.compile(r"(?:[A-Z]\.)?(?:LINE\d{1,2}[a-z]?)+")
FORM_LINE_REFERENCE = re.compile(r"[^()]{1,40}\(LINE\s*\d{1,2}[a-z]?\)")


def is_form_line_label(text: str) -> bool:
    """Whether the text only names a line of a form: "LINE 1a LINE 2", "LINE33", or a short
    caption that ends in the line it points at ("ID number (LINE 1a)")."""
    compact = "".join(text.split())
    return (FORM_LINE_LABEL.fullmatch(compact) is not None
            or FORM_LINE_REFERENCE.fullmatch(text.strip()) is not None)

# A wrapped line of a list item sits right of its marker by this many font sizes or more
# (the hanging indent). A line at or left of the marker, with no marker, ends the item.
HANGING_MIN = 0.5

# A line belongs to a list item when it sits at the item's text x (the x of the first character
# after the marker), within this share of the font size. Right of the marker is not enough: a
# paragraph's first line can be indented just past the marker, and its later lines fall back.
ALIGN_TOL = 0.3

ROMAN_VALUES = {"i": 1, "v": 5, "x": 10}

# Lines whose middles sit within this many points of each other form a centred block (a title).
CENTRE_TOL = 3.0

# Blocks this short (words) are fragments: table cells, form labels, chart labels.
SHORT_BLOCK_WORDS = 3

WORKSHEET_SUB_START = BULLET_GLYPHS + "{}"  # a bullet or a bracket: a sub-list, its own block

# How a region's reading order was found (xy_cut's second result; the max over regions).
SPLIT_NONE, SPLIT_COLUMNS, SPLIT_SOFT = 0, 1, 2
SPLIT_NAMES = {SPLIT_NONE: "single reading order", SPLIT_COLUMNS: "columns (clean gutters)",
               SPLIT_SOFT: "columns (soft gutter, crossed by a few lines)"}


def reading_order_why(split: int) -> Why:
    """The reason a node of a page with this reading-order split records (attrs["why"]). The soft
    split's carries the code of its Fact, "soft-columns" (confidence.legacy_facts)."""
    return Why(f"reading order: {SPLIT_NAMES[split]}", "soft-columns" if split == SPLIT_SOFT else None)


# The reason a line a table's cells did not hold is kept in the text (attrs["why"] of its block),
# with the code of its Fact (confidence.legacy_facts).
UNHELD_WHY = Why("kept in the text: the table's cells did not hold this line", "unheld-lines")

# A form's numbered section title: "3. PROPERTY IDENTIFICATION INFORMATION", "Section 1. Applicant".
NUMBERED_TITLE = re.compile(r"^(?:section\s+)?\d{1,2}\.\s+[A-Z]", re.I)
