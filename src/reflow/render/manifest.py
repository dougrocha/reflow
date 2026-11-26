"""manifest.json: the chunks, pages and text sizes the viewer reads (make_manifest)."""

from __future__ import annotations

import math
from collections import Counter

from reflow.model import Document, Node, Role
from reflow.render.chunks import Chunk


def _lines(node: Node) -> int:
    """Rows of text a block takes: a paragraph that breaks its lines counts each line."""
    lines = node.attrs.get("lines")
    return len(lines) if lines else 1


def page_text_sizes(doc: Document) -> dict[int, list[int]]:
    """Per page: [characters, blocks, lines] of the transcript text, so a front end can
    estimate how tall the page's transcript is before it loads. A block is a paragraph,
    heading, caption, note, figure, list item or table row. Artifacts are not printed,
    so they are not counted."""
    sizes = {p.number: [0, 0, 0] for p in doc.pages}
    # A ruled box counts as the blocks it holds.
    for node in (c for n in doc.flow() for c in (n.children if n.role == Role.BOX else [n])):
        if node.role == Role.ARTIFACT or node.page not in sizes:
            continue
        size = sizes[node.page]
        if node.role == Role.LIST:
            stack = list(node.children)
            while stack:  # items, and the items of any list nested in them
                item = stack.pop()
                if item.role == Role.LIST:
                    stack.extend(item.children)
                    continue
                size[0] += len(item.text or "")
                size[1] += 1
                size[2] += _lines(item)
                stack.extend(c for c in item.children if c.role == Role.LIST)
        elif node.role == Role.TABLE:
            for row in node.children:
                if row.role == Role.TR:
                    size[0] += sum(len(cell.text or "") for cell in row.children)
                    size[1] += 1
                    size[2] += 1
                elif row.role == Role.CAPTION:
                    size[0] += len(row.text or "")
                    size[1] += 1
                    size[2] += 1
        else:
            size[0] += len(node.text or "")
            size[1] += 1
            size[2] += _lines(node)
    return sizes


TEXT_SIZE_BASE = 1.1  # the manifest's "textBase": each size step is a factor of 1.1


def encode_size(n: int) -> int:
    """`n` as a step on a geometric scale: k = round(log(n + 1) / log(TEXT_SIZE_BASE)).
    Decoded (round(base**k - 1), as the viewer does) it is off by at most half a step, a
    relative error of sqrt(1.1) - 1 = 4.9%, plus up to 0.5 of integer rounding: at most 5.3% for
    counts from 30 up, at most 1 below."""
    return round(math.log(n + 1) / math.log(TEXT_SIZE_BASE))


def decode_size(k: int) -> int:
    """The Python statement of the viewer's decoder (count = round(textBase**step - 1)). Nothing
    in a conversion calls it: the round-trip tests use it to bound encode_size's error against
    what the viewer will read."""
    return round(TEXT_SIZE_BASE ** k - 1)


def make_manifest(doc: Document, *, title: str, lang: str, rtl: bool, ltr_lang: str | None,
                  chunks: list[Chunk], toc: list[dict], toc_complete: bool,
                  title_on_page: bool, labels: dict[int, str], page_widths: tuple[int, ...], version: str) -> dict:
    """The whole of manifest.json, which the viewer reads (schema/manifest.schema.json).

    The most common page size is the default and only the pages that differ have an entry;
    the page-to-chunk mapping is derived from `chunks`. "text" is one [characters, blocks,
    lines] triple per page, in page order, each count encoded by encode_size (see
    page_text_sizes): a parallel array rather than page entries, because every page has one.
    `page_widths` are the page image widths written, `()` for a document with no page images.
    "v" is `version`, the digest of the published output the viewer appends to its asset URLs
    (reflow.output.stamp_version), or the format number 2 when there is none."""
    page_dims = [(round(p.width, 1), round(p.height, 1)) for p in doc.pages]
    page_size = Counter(page_dims).most_common(1)[0][0]

    # Printed numbering usually runs at a fixed offset from the PDF page number
    # (page 310 prints "308"), so labels are stored only where they break that pattern.
    offsets = Counter(int(v) - n for n, v in labels.items() if v.isdigit())
    label_offset = offsets.most_common(1)[0][0] if offsets else 0

    sizes = page_text_sizes(doc)

    pages = {}
    for p in doc.pages:
        overrides = {}
        if (round(p.width, 1), round(p.height, 1)) != page_size:
            overrides["w"] = p.width
            overrides["h"] = p.height
        label = labels.get(p.number, str(p.number))
        if label != str(p.number + label_offset):
            overrides["label"] = label
        if overrides:
            pages[str(p.number)] = overrides

    origin = next((f.data.get("origin") for f in doc.root.facts if f.code == "title"), None)
    manifest = {
        "v": version or 2,
        "title": title,
        # The converter found no title and fell back to the file name (analyze.title).
        **({"titleFromFileName": True} if origin == "filename" else {}),
        # Page 1 already opens with the title (a title the converter found on it, which no
        # contents entry lists), so the viewer does not print it a second time.
        **({"titleOnPage": True} if title_on_page else {}),
        "lang": lang,
        **({"ltrLang": ltr_lang} if ltr_lang else {}),
        "dir": "rtl" if rtl else "ltr",  # the text direction of the whole document
        "pageCount": len(doc.pages),
        "pageSize": list(page_size),
        "labelOffset": label_offset,
        # The first width is pages/NNNN.webp; each other is pages/NNNN-<width>.webp. Viewers
        # build srcset from them. Absent: the document has no page images.
        **({"imageWidths": list(page_widths)} if page_widths else {}),
        "pages": pages,
        "chunks": [{"href": c.href, "first": c.pages[0], "last": c.pages[-1]} for c in chunks],
        "toc": [{"level": e["level"], "text": e["text"], "href": e["href"], "page": e["page"]}
                for e in toc],
        "tocComplete": toc_complete,
        # Only used to size a page's placeholder before its chunk loads, so the counts are
        # stored as steps of a geometric scale (encode_size; "textBase" marks it): exact numbers
        # cost ~10 KB gzip on a 2,600-page document, and a step is within 4.9% of the count, so
        # a placeholder is never more than ~5% off.
        "textBase": TEXT_SIZE_BASE,
        "text": [[encode_size(c), encode_size(b), encode_size(l)]
                 for c, b, l in (sizes[p.number] for p in doc.pages)],
    }
    if any(n.role == Role.FIELD and n.attrs.get("name") for n in doc.root.walk()):
        manifest["fillable"] = True  # the original PDF has named AcroForm fields to fill
    # A font the converter decoded from OCR votes (ingest.font_map): its review item stays
    # until a reviewer answers it, and a reject restores the layer's reading.
    decoded = next((f for f in doc.root.facts if f.code == "font-map"), None)
    if decoded is not None:
        manifest["decoded"] = {"fonts": len(decoded.data.get("fonts", [])),
                               "codes": int(decoded.data.get("count", 0))}
    return manifest
