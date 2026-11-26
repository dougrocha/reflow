"""Heading levels from printed section numbers.

`heading_levels` ranks heading styles by size, so "3.1 CAD SYSTEM" at 14 pt and "3.2 INTERFACES"
at 11 pt bold could never be siblings. In a document that numbers its sections the number says
what the size cannot: "3.2" sits beside "3.1" and "3.1.1" sits under it.
"""

from __future__ import annotations

import re

from reflow.model import HEADING_BY_LEVEL, HEADING_ROLES, Node, Role

MIN_NUMBERED = 3  # headings with a section number, for a document to count as numbered
MIN_SHARE = 0.7  # of the headings that start with a number, the share that must parse
TOP_LEVEL = 2  # level 1 is the title

DOTTED = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){1,2})(?=\s|$)")
SECTION = re.compile(r"^section\s+(\d{1,2})\s*[-–—:]", re.IGNORECASE)
BARE = re.compile(r"^(\d{1,2})\s*(?:[-–—:]|(?=\s+[A-Z]{2,}(?:\s|$)))")


def section_number(text: str) -> tuple[int, ...] | None:
    """The section number a heading prints, or None. Four forms: `N`, `Section N` (a dash or
    colon after it), `N.N` and `N.N.N`. A bare `N` needs a following dash, colon or all-caps word,
    so "2024 Annual Report" and "3 months" are not numbered."""
    text = text.strip()
    if m := DOTTED.match(text):
        return tuple(int(part) for part in m.group(1).split("."))
    if m := SECTION.match(text) or BARE.match(text):
        return (int(m.group(1)),)
    return None


def relevel_numbered(nodes: list[Node]) -> None:
    """Give each numbered heading the level its number implies, nested under the nearest earlier
    heading whose number is a printed prefix of its own; otherwise at the top (level 2).
    Runs only when at least 3 headings are numbered and at least 70% of the headings that start
    with a number parse. Unnumbered headings keep their style level."""
    headings = [n for n in nodes if n.role in HEADING_ROLES and n.role != Role.H1 and n.text]
    numbers = [(n, section_number(n.text)) for n in headings]
    numbered = [(n, num) for n, num in numbers if num]
    digit_led = [num for n, num in numbers if n.text.strip()[:1].isdigit()]
    if len(numbered) < MIN_NUMBERED or sum(1 for num in digit_led if num) < MIN_SHARE * len(digit_led):
        return
    open_sections: list[tuple[tuple[int, ...], int]] = []  # (number, level), earlier headings
    for node, number in numbered:
        level = TOP_LEVEL
        for earlier, earlier_level in reversed(open_sections):
            if number[:len(earlier)] == earlier and len(earlier) < len(number):
                level = earlier_level + 1
                break
        level = min(level, 6)
        node.role = HEADING_BY_LEVEL[level - 1]
        open_sections.append((number, level))
