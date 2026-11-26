"""Render the document model as chunked, accessible HTML (spec §6): render_html writes the site.

Output layout (paths relative to the output directory):
    index.html          shell: title, contents, first chunk inline, a slot per chunk
    chunks/0001.html    each chunk as a full standalone page (works without JS)
    toc.json            every heading → chunk + anchor
    assets/             viewer.css (transcript.css + viewer.css), viewer.js
review.json, document.json, the layout sidecars, page images (pages/) and figures (figures/)
are written by reflow.output.publish and reflow.analyze.enrich.describe; the renderer scores
nothing and reads the document as it is given.
"""

from __future__ import annotations

import json
import shutil
from importlib import resources
from pathlib import Path

from reflow.analyze.language import (
    document_language,
    ltr_block_language,
    page_languages,
    second_block_language,
)
from reflow.model import Document
from reflow.names import ASSETS_DIR, CHUNKS_DIR, INDEX_HTML, MANIFEST_JSON, TOC_JSON
from reflow.render.alt import figure_headings
from reflow.render.casing import Casing
from reflow.render.chunks import inline_count, make_chunks, resolve_links
from reflow.render.html import NodeRenderer
from reflow.render.manifest import make_manifest
from reflow.render.shell import VIEWER_CSS, VIEWER_JS, chunk_page, index_page
from reflow.render.toc import drop_repeats, nest_toc, toc_entries

# Headings up to this level appear in the inline contents; toc.json has all of them.
TOC_DEPTH = 3


TOC_MAX_INLINE = 400  # past this many entries, inline contents only lists h2


def render_html(doc: Document, title: str, out_dir: Path, *,
                figures: dict[str, tuple[str, int, int]] | None = None,
                labels: dict[int, str] | None = None, version: str = "",
                page_widths: tuple[int, ...]) -> dict:
    """Write the HTML output. Returns what it measured: chunks, headings, index_bytes,
    largest_chunk_bytes (reflow.output.SiteStats). `page_widths` are the page image widths
    written, `()` when there are none; `version` is the digest of the output the manifest
    carries as "v" (see make_manifest)."""
    labels = labels or {}
    page_of_label: dict[str, int] = {}
    for page in doc.pages:
        page_of_label.setdefault(labels.get(page.number, str(page.number)), page.number)
    lang, rtl = document_language(doc)
    ltr_lang = ltr_block_language(doc) if rtl else None
    second_lang = second_block_language(doc, lang, ltr_lang)
    r = NodeRenderer(figures or {}, labels, page_of_label, rtl_document=rtl, ltr_lang=ltr_lang,
                     second_lang=second_lang, doc_lang=lang, version=version,
                     page_langs=({} if rtl else page_languages(doc, lang)),
                     headings=figure_headings(doc),
                     casing=Casing(n.text or "" for n in doc.root.walk()))
    chunks = make_chunks(doc, r, bool(page_widths))
    chunk_of_page = {number: chunk for chunk in chunks for number in chunk.pages}
    inlined = inline_count(chunks)
    toc = toc_entries(doc, chunks)
    inline = [e for e in toc if e["level"] <= TOC_DEPTH]
    if len(inline) > TOC_MAX_INLINE:
        inline = [e for e in toc if e["level"] == 2]
    shown = len(inline) == len(toc)
    inline = drop_repeats(inline)  # entries only the depth cut made adjacent

    common = dict(title=title, lang=lang, rtl=rtl, page_count=len(doc.pages), version=version)

    (out_dir / CHUNKS_DIR).mkdir(parents=True, exist_ok=True)
    for i, chunk in enumerate(chunks):
        page = chunk_page(
            **common, chunk=chunk, content=resolve_links(chunk.html, chunk_of_page),
            prev=chunks[i - 1] if i else None,
            next=chunks[i + 1] if i + 1 < len(chunks) else None)
        (out_dir / chunk.href).write_text(page, encoding="utf-8")

    index = index_page(
        **common, chunks=chunks,
        inlined=[resolve_links(c.html, chunk_of_page) for c in chunks[:inlined]],
        toc=nest_toc(inline),
        toc_complete=shown)
    (out_dir / INDEX_HTML).write_text(index, encoding="utf-8")
    (out_dir / TOC_JSON).write_text(
        json.dumps([{k: v for k, v in e.items() if k != "parent"} for e in toc],
                   ensure_ascii=False), encoding="utf-8")
    # The manifest carries the same top-level contents as the inline list; the full
    # contents (every provider in a directory) stays in toc.json.
    manifest = make_manifest(doc, title=title, lang=lang, rtl=rtl, ltr_lang=ltr_lang,
                             chunks=chunks, toc=inline, toc_complete=shown, title_on_page=1 in r.title_pages,
                             labels=labels,
                             page_widths=page_widths, version=version)
    (out_dir / MANIFEST_JSON).write_text(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (out_dir / ASSETS_DIR).mkdir(exist_ok=True)
    viewer = resources.files("reflow.viewer")
    # One stylesheet: the transcript's rules, shared with the web viewer, then the page's own.
    (out_dir / VIEWER_CSS).write_text(
        (viewer / "transcript.css").read_text(encoding="utf-8") + "\n"
        + (viewer / "viewer.css").read_text(encoding="utf-8"), encoding="utf-8")
    with resources.as_file(viewer / "viewer.js") as src:
        shutil.copyfile(src, out_dir / VIEWER_JS)

    return {"chunks": len(chunks), "index_bytes": len(index.encode()),
            "largest_chunk_bytes": max(len(c.html.encode()) for c in chunks),
            "headings": len(toc)}
