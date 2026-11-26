"""Evidence about tables, from their values rather than their character counts.

Two uses share the tokens below:
  * hybrid adoption (`compare`): a Docling table may replace heuristic content only if it
    keeps every value of it whole and keeps each heuristic row's numbers in one row. A
    multiset of letters and digits can't tell 12 from 21.
  * the review queue (`flag_structure`): tables and headings whose shape says the
    conversion is probably wrong get a `warn` code, so a document with known structural
    failures doesn't show "No issues".
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from reflow.analyze.blocks import Break
from reflow.model import HEADING_ROLES, Node, Role, code_of
from reflow.text_quality import squash

# Punctuation around a value that isn't part of it: "(12.5)," → "12.5". Signs, decimal
# points, percent and currency marks inside or leading a value are kept.
_EDGE = "()[]{}\"'“”‘’,;:!?…*†‡§"
_FOLD = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "“": '"', "”": '"',
    "„": '"', "‟": '"', "‐": "-", "‑": "-", "‒": "-", "–": "-",
    "—": "-", "―": "-", "−": "-",
})
_SUPER = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "          ")
_NUMBER = re.compile(r"(?<![\w-])[-+−–±]?[$€£¥]?\d+(?:[.,]\d+)*%?")
_THOUSANDS = re.compile(r"^[-+±]?[$€£¥]?\d{1,3}(?:,\d{3})+(?:\.\d+)?%?$")
# The heuristic content a Docling table replaces must retain its letters and digits.
RECALL_MIN = 0.95


def tokens(text: str) -> list[str]:
    """Whitespace-delimited tokens after compatibility and quote/dash folding. Surrounding
    punctuation is stripped, internal punctuation stays: "12.5%" and "1,024" stay whole."""
    out = []
    normalized = unicodedata.normalize("NFKC", text).translate(_FOLD).casefold()
    for raw in normalized.split():
        tok = raw.strip(_EDGE)
        if tok:
            out.append(tok)
    return out


def is_number(token: str) -> bool:
    """Whether a token from `tokens` is one numeric value ("12.5%", "1,024", "-3")."""
    return _NUMBER.fullmatch(token) is not None


def numbers(text: str) -> list[str]:
    """Extract normalized numeric values without joining across whitespace."""
    normalized = unicodedata.normalize("NFKC", text.translate(_SUPER)).translate(_FOLD)
    result = []
    for match in _NUMBER.finditer(normalized):
        value = match.group().replace("−", "-").replace("–", "-")
        if _THOUSANDS.fullmatch(value):
            value = value.replace(",", "")
        result.append(value)
    return result


def _letters_digits(texts: Sequence[str]) -> Counter[str]:
    chars = "".join(texts).translate(_SUPER).translate(_FOLD)
    normalized = unicodedata.normalize("NFKC", chars).casefold()
    return Counter(char for char in normalized if char.isalpha() or char.isdigit())


def _letters(texts: Sequence[str]) -> Counter[str]:
    """Multiset of letters used to identify a row's label across spacing changes."""
    chars = "".join(texts).translate(_SUPER).translate(_FOLD)
    normalized = unicodedata.normalize("NFKC", chars).casefold()
    return Counter(char for char in normalized if char.isalpha())


def node_text(node: Node) -> list[str]:
    """Text content, excluding captions and TR row labels."""
    if node.role == Role.CAPTION:
        return []
    parts: list[str] = []
    if node.text and node.role != Role.TR:
        parts.append(node.text)
    for child in node.children:
        parts.extend(node_text(child))
    return parts


@dataclass(frozen=True)
class Comparison:
    """How a candidate table holds the content it would replace."""
    recall: float                      # share of the replaced tokens (as a multiset) it holds
    missing_numbers: tuple[str, ...]   # numeric tokens of the replaced content it lacks
    split_rows: int                    # replaced table rows whose numbers no candidate row holds

    def problem(self) -> str | None:
        reasons = []
        if self.missing_numbers:
            shown = ", ".join(self.missing_numbers[:5])
            if len(self.missing_numbers) > 5:
                shown += ", …"
            reasons.append(f"numeric values missing: {shown}")
        if self.split_rows:
            reasons.append(f"numeric values split across rows: {self.split_rows}")
        if self.recall < RECALL_MIN:
            reasons.append(f"text recall {self.recall:.2f} below {RECALL_MIN}")
        return "; ".join(reasons) or None


def compare(replaced: Sequence[Node], candidate: Node) -> Comparison:
    """Compare a candidate TABLE with the heuristic nodes it would replace (tables, figures,
    text blocks). Row checks apply only to replaced TABLE nodes; text blocks have no rows.
    Row labels and captions are not content."""
    wanted = [part for node in replaced for part in node_text(node)]
    have = node_text(candidate)
    wanted_count = _letters_digits(wanted)
    have_count = _letters_digits(have)
    total = sum(wanted_count.values())
    recall = sum((wanted_count & have_count).values()) / total if total else 1.0

    wanted_numbers = Counter(number for value in wanted for number in numbers(value))
    have_numbers = Counter(number for value in have for number in numbers(value))
    missing = tuple(number for number, count in (wanted_numbers - have_numbers).items()
                    for _ in range(count))

    candidate_rows = [row for row in candidate.children if row.role == Role.TR]
    replaced_tables = [node for node in replaced if node.role == Role.TABLE]
    candidate_cols = max(
        (sum(cell.attrs.get("colspan", 1) for cell in row.children)
         for row in candidate_rows), default=0)
    # Row positions are comparable only when both tables have the same shape. When
    # extraction flattened or split rows, the global numeric multiset checks values.
    comparable_rows = any(
        len([child for child in table.children if child.role == Role.TR]) == len(candidate_rows)
        and max((sum(cell.attrs.get("colspan", 1) for cell in row.children)
                 for row in table.children if row.role == Role.TR), default=0) == candidate_cols
        for table in replaced_tables
    )
    split_rows = 0
    for node in replaced_tables if comparable_rows else ():
        for row in (child for child in node.children if child.role == Role.TR):
            row_parts = [part for cell in row.children for part in node_text(cell)]
            row_numbers = Counter(number for value in row_parts for number in numbers(value))
            row_letters = _letters(row_parts)
            # When a row has letters, a candidate row must preserve those letters as
            # well as all its numbers. This tolerates spacing and punctuation changes.
            if row_numbers and not any(
                    not (row_numbers - Counter(
                        number for cell in candidate_row.children
                        for value in node_text(cell) for number in numbers(value)))
                    and (not row_letters or (
                        sum((row_letters & _letters([
                            part for cell in candidate_row.children for part in node_text(cell)
                        ])).values()) / sum(row_letters.values()) >= RECALL_MIN
                    ))
                    for candidate_row in candidate_rows):
                split_rows += 1
    return Comparison(recall, missing, split_rows)


def table_warning(table: Node) -> str | None:
    """A review `warn` code for a TABLE whose shape is probably wrong ("table-giant-cell",
    "table-collapsed", both named in confidence.CODES), else None."""
    rows = [row for row in table.children if row.role == Role.TR]
    cells = [cell for row in rows for cell in row.children
             if cell.role in (Role.TD, Role.TH)]
    nonempty = [cell for cell in cells if (cell.text or "").strip()]
    giant = any(len(cell.text or "") >= 1000 and len(numbers(cell.text or "")) >= 30
                for cell in nonempty)
    if giant or (len(nonempty) == 1 and len(nonempty[0].text or "") >= 200):
        return "table-giant-cell"
    informative = [row for row in rows if any((cell.text or "").strip() for cell in row.children)]

    # A column collapse usually leaves several numeric values together in one data cell
    # across multiple rows. A single multi-number cell can be a label or a subtotal.
    data_rows = informative[1:] if len(informative) > 1 else informative
    collapsed_rows = 0
    for row in data_rows:
        numeric_cells = []
        for cell in row.children:
            if cell.role not in (Role.TD, Role.TH):
                continue
            values = tokens(cell.text or "")
            if len(values) >= 3 and all(is_number(token) for token in values):
                numeric_cells.append(len(values))
        if numeric_cells:
            collapsed_rows += 1
    if collapsed_rows >= 2:
        return "table-collapsed"

    return None


def heading_warnings(siblings: Sequence[Node]) -> dict[int, str]:
    """Flag obvious labels and adjacent, geometrically close short headings in one pass."""
    headings = [n for n in siblings if n.role in HEADING_ROLES]
    warnings: dict[int, str] = {}
    for heading in headings:
        text = squash(heading.text or "")
        normalized = text.casefold().strip(" .:;–—-")
        if ((normalized in {"tip", "caution"} and _callout_label(heading, siblings))
                or re.fullmatch(r"\d+(?:\.\d+)*", normalized)):
            warnings[id(heading)] = "heading-fragment"
    for left, right in zip(headings, headings[1:]):
        left_words = (left.text or "").split()
        right_words = (right.text or "").split()
        if (left.page == right.page and left.role == right.role
                and len(left_words) in (1, 2) and len(right_words) in (1, 2)
                and _nearby_headings(left, right)):
            warnings[id(left)] = warnings[id(right)] = "heading-fragment"
    return warnings


def _callout_label(heading: Node, siblings: Sequence[Node]) -> bool:
    """A TIP/CAUTION heading should sit beside the indented text it labels."""
    if not heading.bbox:
        return False
    if any(code_of(why) == Break.HYPHEN_COLUMN for why in heading.attrs.get("why", [])):
        return False
    try:
        index = next(i for i, node in enumerate(siblings) if node is heading)
    except StopIteration:
        return False
    for text_index in (index - 1, index + 1):
        if not 0 <= text_index < len(siblings):
            continue
        text = siblings[text_index]
        if text.role != Role.P or text.page != heading.page or not text.bbox:
            continue
        gap = text.bbox[0] - heading.bbox[2]
        vertical_overlap = min(heading.bbox[3], text.bbox[3]) - max(heading.bbox[1], text.bbox[1])
        vertical_gap = max(heading.bbox[1] - text.bbox[3], text.bbox[1] - heading.bbox[3], 0)
        if 4 <= gap <= 32 and (vertical_overlap >= 0 or vertical_gap <= 4):
            return True
    return False


def _nearby_headings(left: Node, right: Node) -> bool:
    """Whether short headings are close enough to plausibly be one split label."""
    if not left.bbox or not right.bbox:
        return False
    vertical_overlap = min(left.bbox[3], right.bbox[3]) - max(left.bbox[1], right.bbox[1])
    vertical_gap = max(left.bbox[1] - right.bbox[3], right.bbox[1] - left.bbox[3], 0)
    horizontal_gap = max(left.bbox[0] - right.bbox[2], right.bbox[0] - left.bbox[2], 0)
    return (vertical_overlap >= 0 or vertical_gap <= 2) and horizontal_gap <= 36


def flag_structure(root: Node) -> Counter[str]:
    """Set `warn` on every table and heading under `root` that the checks above flag,
    leaving any `warn` already set alone. Returns the count per code."""
    counts: Counter[str] = Counter()

    def visit(parent: Node) -> None:
        warned = heading_warnings(parent.children)
        for node in parent.children:
            warning = None
            if node.role == Role.TABLE:
                warning = table_warning(node)
            elif node.role in HEADING_ROLES:
                warning = warned.get(id(node))
            if warning:
                if not node.attrs.get("warn"):
                    node.attrs["warn"] = warning
                    counts[warning] += 1
            visit(node)

    visit(root)
    return counts
