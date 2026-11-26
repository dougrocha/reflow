"""The two page shells render_html writes: index.html (title, contents, the first chunks inline,
a slot for each other chunk) and chunks/NNNN.html (one chunk as a standalone page).

Every value is escaped except the chunk HTML, which the renderer already escaped."""

from __future__ import annotations

from markupsafe import escape

from reflow.names import ASSETS_DIR, INDEX_HTML, ORIGINAL_PDF, TOC_JSON
from reflow.render.chunks import Chunk
from reflow.render.pagenames import page_count_text

VIEWER_CSS = f"{ASSETS_DIR}/viewer.css"
VIEWER_JS = f"{ASSETS_DIR}/viewer.js"


def _head(title: str, lang: str, rtl: bool, version: str, base: str = "") -> str:
    v = f"?v={escape(version)}" if version else ""
    return (f'<!doctype html>\n<html lang="{escape(lang)}"{" dir=\"rtl\"" if rtl else ""}>\n'
            '<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f"{base}<title>{escape(title)}</title>\n"
            f'<link rel="stylesheet" href="{VIEWER_CSS}{v}">\n'
            f'<script src="{VIEWER_JS}{v}" defer></script>\n</head>\n')


def _toc_list(entries: list[dict]) -> str:
    items = "".join(f'<li><a href="{escape(e["href"])}">{escape(e["text"])}</a>'
                    f'{_toc_list(e["children"]) if e["children"] else ""}</li>\n'
                    for e in entries)
    return f'<ol role="list">\n{items}</ol>\n'


def index_page(*, title: str, lang: str, rtl: bool, page_count: int, version: str,
               chunks: list[Chunk], inlined: list[str],
               toc: list[dict], toc_complete: bool) -> str:
    """index.html. `inlined` is the HTML of the first chunks, shown in place; each later chunk
    gets a stub the viewer loads. `toc` is nested (reflow.render.toc.nest_toc)."""
    v = f"?v={escape(version)}" if version else ""
    out = [_head(title, lang, rtl, version),
           '<body>\n<a class="skip" href="#content">Skip to content</a>\n'
           f'<header class="doc-header">\n<h1>{escape(title)}</h1>\n'
           f'<p class="doc-meta">{page_count_text(page_count)}'
           f' · <a href="{ORIGINAL_PDF}">Download the original PDF</a></p>\n'
           '<button type="button" id="load-all" hidden>Load the entire document</button>\n'
           '<p id="status" class="sr-only" role="status" aria-live="polite"></p>\n</header>\n']
    if toc:
        note = ("" if toc_complete else
                '<p class="toc-note">Only top-level sections are listed. All headings are in '
                f'<a href="{TOC_JSON}">{TOC_JSON}</a>.</p>\n')
        out.append('<nav class="toc" aria-labelledby="toc-heading">\n'
                   f'<h2 id="toc-heading">Contents</h2>\n{_toc_list(toc)}{note}</nav>\n')
    out.append('<main id="content" class="reflow-transcript" tabindex="-1">\n')
    for i, c in enumerate(chunks):
        attrs = (f'class="chunk" data-src="{escape(c.href)}{v}" '
                 f'data-first="{c.pages[0]}" data-last="{c.pages[-1]}"')
        if i < len(inlined):
            out.append(f'<div {attrs} data-loaded="1">{inlined[i]}</div>\n')
        else:
            heading = f" ({escape(c.first_heading)})" if c.first_heading else ""
            out.append(f'<div {attrs}>\n<p class="chunk-stub"><a href="{escape(c.href)}">'
                       f"Continue reading: pages {c.page_range}{heading}</a></p>\n</div>\n")
    out.append("</main>\n</body>\n</html>")
    return "".join(out)


def chunk_page(*, title: str, lang: str, rtl: bool, page_count: int, version: str,
               chunk: Chunk, content: str,
               prev: Chunk | None, next: Chunk | None) -> str:
    """chunks/NNNN.html: `content` is the chunk's HTML, with a pager to its neighbours."""
    heading = f"{escape(title)}: pages {chunk.page_range}"
    pager = "".join([
        f'<li><a href="{escape(prev.href)}" rel="prev">Previous: pages {prev.page_range}</a></li>\n'
        if prev else "",
        f'<li><a href="{INDEX_HTML}">Contents</a></li>\n',
        f'<li><a href="{escape(next.href)}" rel="next">Next: pages {next.page_range}</a></li>\n'
        if next else ""])
    return (_head(f"{title}: pages {chunk.page_range}", lang, rtl, version, base='<base href="../">\n')
            + f'<body>\n<a class="skip" href="{escape(chunk.href)}#content">Skip to content</a>\n'
            f'<header class="doc-header">\n<p class="doc-meta"><a href="{INDEX_HTML}">'
            f"{escape(title)}</a> · {page_count_text(page_count)}</p>\n<h1>{heading}</h1>\n"
            f'</header>\n<nav class="pager" aria-label="Sections">\n<ul>\n{pager}</ul>\n</nav>\n'
            '<main id="content" class="reflow-transcript" tabindex="-1">\n'
            f'<div class="chunk-content">{content}</div>\n</main>\n</body>\n</html>')
