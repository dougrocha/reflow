"""Dot leaders on contents pages ("Title ........ 12") and the text cleanup that goes with them.

Some PDFs draw a leader with a glyph the font has no Unicode mapping for, so the text comes
out as U+FFFD. A leader is a run of three or more fill characters (U+FFFD, dots, middle dots,
ellipses, underscores; spaces allowed between them). A contents entry is a title, a leader
and a page label at the end of the line.
"""

from __future__ import annotations

import re

from reflow.model import Document, Node, Why

REPLACEMENT = "�"
FILL = "�.·…_"
_FILL = f"[{re.escape(FILL)}]"
# A budget line code opens a table row: four to six digits, not a year ("3510 Budget authority",
# "234011 Guaranteed 502 …"). Years ("2024 Annual Report … 5") stay contents entries.
ROW_CODE = re.compile(r"^(?!(?:19|20)\d{2}\s)\d{4,6}\s+(?=\S)")
# A run of at least three fill characters, spaces allowed between them.
LEADER = re.compile(rf"{_FILL}(?:[ \t]*{_FILL}){{2,}}")
# TITLE LEADER PAGE, with the page label at the very end. The title ends on a character that
# is not fill, so the leader is the whole fill run before the label ("Dept. ... 12" loses the
# period to the leader; titles ending in a period are rare in contents).
ENTRY = re.compile(
    rf"^(?P<title>.*[^\s{re.escape(FILL)}])[ \t]*(?P<leader>{_FILL}(?:[ \t]*{_FILL}){{2,}})[ \t]*"
    r"(?P<page>[0-9]{1,4}|[ivxlcdm]{1,6})$",
    re.IGNORECASE,
)


def parse_contents_entry(text: str) -> tuple[str, str] | None:
    """(title, page label) if `text` is a contents entry, else None. A title needs a letter,
    so "1...2" is not an entry. A line that opens with a budget line code ("235999 Total guaranteed
    loan reestimates ........ 172") is a table row, with leaders or not: it is never an entry."""
    match = ENTRY.match(text)
    if not match or not re.search(r"[A-Za-z]", match["title"]) or ROW_CODE.match(text):
        return None
    return match["title"], match["page"]


def is_contents_entry(text: str) -> bool:
    return parse_contents_entry(text) is not None


def normalize_text(text: str) -> tuple[str, int, int]:
    """(text, leaders, stray). U+FFFD inside a leader becomes a dot; any other U+FFFD is
    dropped. `leaders` and `stray` count the replaced and dropped characters."""
    leaders = 0

    def dots(match: re.Match[str]) -> str:
        nonlocal leaders
        leaders += match.group(0).count(REPLACEMENT)
        return match.group(0).replace(REPLACEMENT, ".")

    fixed = LEADER.sub(dots, text)
    stray = fixed.count(REPLACEMENT)
    return fixed.replace(REPLACEMENT, ""), leaders, stray


def normalize_document(doc: Document) -> dict[str, int]:
    """Run normalize_text over every text in the document (node text and the address lines
    and page labels in attrs). Returns the counts: leaders repaired and stray dropped."""
    totals = {"leaders": 0, "stray": 0}

    def fix(value: str) -> str:
        text, leaders, stray = normalize_text(value)
        totals["leaders"] += leaders
        totals["stray"] += stray
        return text

    def walk(node: Node) -> None:
        if node.text:
            node.set_text(fix(node.text))
        for key, value in node.attrs.items():
            if isinstance(value, str):
                node.attrs[key] = fix(value)
            elif isinstance(value, list) and all(isinstance(v, str) for v in value):
                # a `why` entry keeps its code (model.Why) over the repaired words
                node.attrs[key] = [Why(fix(v), v.code) if isinstance(v, Why) else fix(v)
                                   for v in value]
        for child in node.children:
            walk(child)

    walk(doc.root)
    return totals
