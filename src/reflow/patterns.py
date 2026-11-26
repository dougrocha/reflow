"""Printed-text grammar the extraction and the analysis both read. It imports nothing from
reflow: a pattern here is a fact about how documents are printed, not about either stage.

Caption labels ("Table 3", "Figure D-1") are read at several strictnesses, each named for the
question it answers; the variants differ on purpose and are not interchangeable."""

import re

# A caption sets its label apart: "Table 3. Error rates", "Figure 5: ...". "Table 3 shows
# that ..." is body text that mentions a table.
CAPTION_BLOCK = re.compile(r"^(figure|fig\.|chart|exhibit|image|table)\s+[A-Z]{0,2}-?[0-9]+"
                           r"(-[0-9]+|-[A-Z])?[a-z]?\s*[.:]", re.I)  # also "Table A-1.", "Table ES-1:", "Figure 4-A."
# A caption label inside a chart stays in the text flow, with the title and subtitle set under
# it, which may be larger. At most one letter before the number.
CAPTION_KEPT = re.compile(r"^(figure|fig\.|chart|exhibit|image|table)\s+[A-Z]?-?[0-9]+(-[0-9]+)?[a-z]?\s*[.:]",
                          re.I)  # "Figure D-1." too (CAPTION_BLOCK has digits only)
# A caption label with its title and no punctuation: "Table D-1 Requirements Verification"
CAPTION_TITLE = re.compile(r"^(figure|fig\.|chart|exhibit|table)\s+[A-Z]{0,2}-?[0-9]+(-[0-9]+|-[A-Z])?\s+[A-Za-z]",
                           re.I)
# A figure label as a figure's text: a plain number, no punctuation needed.
CAPTION_LABEL = re.compile(r"^(figure|fig\.|chart|exhibit|image|table)\s+[0-9]+[a-z]?\b", re.I)
# The start of a caption in a table's cells: the label and a digit, a space optional.
CAPTION_START = re.compile(r"^(table|figure|fig\.|chart|exhibit)\s*[0-9]", re.I)


# A cell that is a number: 1,234 / –426 / (12) / 8.43† / 9% / $1,000 / $ (1,000), or a dash for
# "none". A soft hyphen (U+00AD) is the minus sign of some exports. A currency sign may stand
# apart from its figure ("$ 8,426,474").
NUMBER = re.compile(r"^(?:\$\s?)?[(\[]?[-–—−\u00ad+]?\$?\d[\d,]*(?:\.\d+)?%?[)\]]?[*†‡§]?\d?$"
                    r"|^(?:\$\s?)?[-–—−\u00ad]$")

# A value with a note in brackets: "7.54 (7.72±0.16)". The value is what lines up.
NUMBER_WITH_NOTE = re.compile(r"^([-–—−+]?\d[\d,]*(?:\.\d+)?%?)\s*\([^()]*\)$")

# Dot leaders ("Operating forces ............ 8,268"): the leader is cut from the cell text.
LEADER_TAIL = re.compile(r"(?:\s*[.·…_�]){3,}\s*$")

# Budget schedules print a column of actuals and estimates under year headings, and a line code
# before each row. The variants differ in where they may sit and what else may share the text.
YEAR_CELL = re.compile(r"^(19|20)\d{2}$")
YEAR_LABEL = re.compile(r"^(19|20)\d{2}\s+(actual|est\.?|enacted)$", re.I)  # "2023 actual"
# A column heading: "2024 est." over "est."
YEAR_HEADING_START = re.compile(r"^(actual|est\.?|enacted)\b", re.I)
# A year heading inside a table's body ("2025 est.", "2023 actual") means two schedules' headings
# have run together: a header-less table of figures is not kept then.
YEAR_HEADING_ANY = re.compile(r"\b(est\.?|actual)(\s|$)", re.I)
SCHEDULE_HEAD = re.compile(r"(\bactual|\best\.?|\benacted)$|^identification code", re.I)
CODE_START = re.compile(r"^(\d{3,6})\s+(?=\S)")  # budget line codes: "0799 Total direct …"
CODE_CELL = re.compile(r"^(?!(?:19|20)\d{2}$)\d{4,6}$")  # a budget line code (not a year)
