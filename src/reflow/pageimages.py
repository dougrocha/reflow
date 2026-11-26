"""The sizes of the page images ("view original"): the defaults of Options, the renderer's own
fallbacks and the manifest's."""

# 800 px wide (about 94 dpi on letter) at WebP quality 60. Benchmarked on 60 sampled pages: 1000
# px was ~112 KB/page; 800 px is ~80 KB (about -29%), with 8 pt text still legible at native
# size. Quality 50 saves little more and costs visible text.
PAGE_WIDTH = 800  # px
# srcset: the viewer's page card is up to 1024 css px wide. 1200 px covers 1x fully and is
# visibly sharper at 2x, for 2.25x the pixels of 800 px (4x for 1600 px) on a secondary surface.
# Measured on a 142-page IRS publication: 17.5 MB at 800 px, 49.8 MB with 1200 px added (2.8x
# the bytes).
# The first width of a list keeps the plain name NNNN.webp, so older viewers and links still work.
PAGE_WIDTHS = (PAGE_WIDTH, 1200)
# Past this many pages only the first width is written: a long document would cost the large
# images hundreds of MB. `reflow convert --srcset-max-pages` overrides it (0 = no limit).
LARGE_WIDTH_MAX_PAGES = 300


def widths_for(widths: tuple[int, ...], page_count: int,
               max_pages: int | None = LARGE_WIDTH_MAX_PAGES) -> tuple[int, ...]:
    """The widths to write for a document of `page_count` pages: all of them, or only the first
    (the base) when the document has more than `max_pages` pages. None or 0 means no limit."""
    return widths[:1] if max_pages and page_count > max_pages else tuple(widths)
