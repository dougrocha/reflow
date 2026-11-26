"""HTML attributes, and the links of the transcript: the ones a span carries and the URLs,
addresses and phone numbers found in text."""

from __future__ import annotations

import os
import re
import warnings

from markupsafe import Markup, escape

from reflow.model import Node, Span


def attribute(name: str, *parts: object) -> str:
    """Write one quoted attribute, or omit invalid None values in production.

    Tests enable REFLOW_STRICT_HTML_ATTRIBUTES to expose malformed model states.
    Parts allow generated prefixes/suffixes without stringifying None first.
    """
    if any(part is None for part in parts):
        message = f"HTML attribute {name!r} cannot be None"
        if os.environ.get("REFLOW_STRICT_HTML_ATTRIBUTES") == "1":
            raise ValueError(message)
        warnings.warn(message, RuntimeWarning, stacklevel=2)
        return ""
    return f'{name}="' + "".join(str(escape(part)) for part in parts) + '"'


# Patterns for what linkify turns into a link. URLs: http(s):// or www., up to whitespace or a
# closing paren (trailing punctuation is cut off the link). Emails. US phone numbers, formatted
# only, so plain 10-digit figures (budget amounts, IDs) stay text: (480) 555-0142,
# 480-555-0142, 1-800-555-1212. Groups of the phone pattern are 4/5 = area (with or without
# parentheses), 6 = exchange, 7 = subscriber.
_URL = r'https?://[^\s)]+|www\.[^\s)]*[^\s).,;:?!]'


_EMAIL = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'


_PHONE = (r'(?<![\d-])(?:\+?1[-.\s])?(?:\(([0-9]{3})\)\s?|([0-9]{3})[-.])'
          r'([0-9]{3})[-.]([0-9]{4})(?![\d-])')


_AUTO_LINK = re.compile(f'({_URL})|({_EMAIL})|({_PHONE})')


def auto_links(text: str) -> list[tuple[int, int, str]]:
    """The URLs, emails and phone numbers in `text` as (start, end, href): the range the link
    covers, without the punctuation that ends a sentence after a URL."""
    found = []
    for match in _AUTO_LINK.finditer(text):
        if match.group(1):
            url = match.group(1)
            while url and url[-1] in '.,;:)?!':
                url = url[:-1]
            href = url if url.startswith('http') else f'https://{url}'
            found.append((match.start(), match.start() + len(url), href))
        elif match.group(2):
            found.append((match.start(), match.end(), f'mailto:{match.group(2)}'))
        else:
            tel = f'+1{match.group(4) or match.group(5)}{match.group(6)}{match.group(7)}'
            found.append((match.start(), match.end(), f'tel:{tel}'))
    return found


def linkify(text: str) -> str:
    out, at = [], 0
    for start, end, href in auto_links(text):
        out.append(f"{escape(text[at:start])}<a {attribute('href', href)}>{escape(text[start:end])}</a>")
        at = end
    out.append(str(escape(text[at:])))
    return Markup("".join(out))


SAFE_LINK = re.compile(r"^(https?://|mailto:|tel:|ftp://|#)", re.I)


# A link to a page, or to an element on it, is written as a placeholder until the chunks exist:
# the chunk a page lands in is only known after rendering (chunks.resolve_links fills them in).
# "<p12>" points at page 12; "<p12:n-ab>" at the element n-ab, which is on page 12. It is
# markup, and every string from the PDF is escaped on its way into the HTML, so no text can
# spell one (a NUL-delimited form could be: PDF strings keep NUL, and squash does not strip it).
PAGE_LINK = re.compile(r"<p(\d+)(?::([^<>]*))?>")


def page_link(page: int | str, fragment: str | None = None) -> Markup:
    return Markup(f"<p{page}" + (f":{escape(fragment)}" if fragment else "") + ">")


def anchor(node: Node) -> str:
    """Stable HTML id from the node id ("12:ab12cd" → "n12-ab12cd")."""
    return "n" + re.sub(r"[^0-9a-zA-Z_-]", "-", node.id)


def _href(target: str) -> str | None:
    """The href a link span's target becomes, or None when the target is not one a page may
    link to. "#p12" is a page: a placeholder resolve_links points at the chunk file holding it."""
    page = re.fullmatch(r"#p(\d+)", target)
    if page:
        return page_link(page.group(1))
    return target if SAFE_LINK.match(target) else None


def note_id(note: str) -> str:
    return f"n-{note}"


def ref_id(note: str, count: str | int = "") -> str:
    """The id of a note reference: "r-<note>" for the first, "r-<note>-<n>" for the n-th."""
    return f"r-{note}" + (f"-{count}" if count and str(count) != "1" else "")


def anchor_tags(span: Span, page: int | None) -> tuple[str, str]:
    """The opening and closing tags of a span's anchor; none for a link target a page may not
    link to (its text stays plain)."""
    if span.kind == "noteref":
        # The note is on the same page; a bare "#n-<id>" would resolve against the chunk's
        # <base href="../">, so it goes through the page placeholder like a page link. The
        # marker is raised, as the page sets it (notes.find_references reads only raised runs).
        note, _, count = span.target.partition("#")
        frag = note_id(note)
        href = page_link(page, frag) if page is not None else f"#{frag}"
        return (f"""<sup><a role="doc-noteref" {attribute('href', href)} {attribute('id', ref_id(note, count))}>""",
                "</a></sup>")
    href = _href(span.target)
    if href is None:
        return "", ""
    rel = ' rel="noopener"' if re.match(r"https?://", href, re.I) else ""
    return f"<a {attribute('href', href)}{rel}>", "</a>"
