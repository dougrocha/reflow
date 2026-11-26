"""What each page weighs, and runs of pages cut by it.

Pages differ in cost by a factor of ten (in p17 the 12 pages of ruled tax tables take 0.66 s each
to extract and the other 130 pages a median 0.08 s), so runs of equal page counts leave one worker
with the dense pages while the rest wait. The weight is the bytes of a page's content streams as
the file declares them. It ranks the dense pages high (22 KB against a median 10 KB) without
predicting their cost exactly, which is enough to cut and order the work by.
"""

from __future__ import annotations

from pathlib import Path

import pikepdf


def page_weights(path: Path, page_count: int) -> list[int] | None:
    """What each page weighs: the bytes of its content streams, as the file declares them (no
    stream is read). None when the file can't say, or when every page weighs nothing."""
    try:
        with pikepdf.open(path) as doc:
            if len(doc.pages) != page_count:
                return None
            weights = []
            for page in doc.pages:
                contents = page.obj.get("/Contents")
                streams = (list(contents) if isinstance(contents, pikepdf.Array)
                           else [] if contents is None else [contents])
                weights.append(sum(int(s.get("/Length", 0)) for s in streams))
    except Exception:  # noqa: BLE001 - a file pikepdf can't read gets the plain chunks
        return None
    return weights if any(weights) else None


def weighted_chunks(weights: list[int], parts: int) -> list[tuple[int, int]]:
    """1-based inclusive (first, last) runs covering every page in order, each run closing once
    it has about 1/parts of the total weight: heavy pages end up in short runs, light pages in
    long ones."""
    target = sum(weights) / max(parts, 1)
    ranges, first, held = [], 1, 0
    for number, weight in enumerate(weights, 1):
        held += weight
        if held >= target:
            ranges.append((first, number))
            first, held = number + 1, 0
    if first <= len(weights):
        ranges.append((first, len(weights)))
    return ranges
