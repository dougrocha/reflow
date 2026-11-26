"""Box arithmetic, defined once for every analysis and extraction module.

Boxes are the model's BBox: PDF points, top-left origin, (x0, top, x1, bottom).
"""

from __future__ import annotations

from collections.abc import Iterable

from reflow.model import BBox


def rounded(box: Iterable[float], places: int = 2) -> list[float]:
    """The box's numbers rounded, as a list: what a fact, a prompt or a cache key records."""
    return [round(v, places) for v in box]


def area(b: BBox) -> float:
    """Width × height, 0 for an inverted box."""
    return max(b[2] - b[0], 0) * max(b[3] - b[1], 0)


def intersection(a: BBox, b: BBox) -> BBox:
    """The overlapping box (may be inverted when they don't meet; `area` of it is then 0)."""
    return (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))


def overlap(a: BBox, b: BBox) -> float:
    """Area shared by the two boxes (`area(intersection(a, b))`, without the temporary box)."""
    return (max(min(a[2], b[2]) - max(a[0], b[0]), 0)
            * max(min(a[3], b[3]) - max(a[1], b[1]), 0))


def share(part: BBox, whole: BBox) -> float:
    """Share of `part`'s own area that lies inside `whole`, 0..1 (0 for an empty `part`)."""
    own = max(part[2] - part[0], 0) * max(part[3] - part[1], 0)
    if not own:
        return 0.0
    return overlap(part, whole) / own


def iou(a: BBox, b: BBox) -> float:
    """Intersection over union, 0..1."""
    shared = overlap(a, b)
    union_area = (max(a[2] - a[0], 0) * max(a[3] - a[1], 0)
                  + max(b[2] - b[0], 0) * max(b[3] - b[1], 0) - shared)
    if union_area <= 0:
        return 0.0
    return shared / union_area


def inside(inner: BBox, outer: BBox, slack: float = 2.0) -> bool:
    """Whether `inner` lies within `outer` grown by `slack` points on every side."""
    return (inner[0] >= outer[0] - slack and inner[1] >= outer[1] - slack
            and inner[2] <= outer[2] + slack and inner[3] <= outer[3] + slack)


def core(box: BBox) -> BBox:
    """The middle half of a line's box, by height: its glyphs without the font's ascent and
    descent, which can overhang the rules of a thin table row."""
    quarter = (box[3] - box[1]) / 4
    return (box[0], box[1] + quarter, box[2], box[3] - quarter)


def union(boxes: Iterable[BBox]) -> BBox | None:
    """The smallest box holding every box, or None for no boxes."""
    it = iter(boxes)
    first = next(it, None)
    if first is None:
        return None
    x0, top, x1, bottom = first[0], first[1], first[2], first[3]
    for b in it:
        if b[0] < x0: x0 = b[0]
        if b[1] < top: top = b[1]
        if b[2] > x1: x1 = b[2]
        if b[3] > bottom: bottom = b[3]
    return (x0, top, x1, bottom)


def horizontal_overlap(a: BBox, b: BBox) -> float:
    """Length of the x-range the boxes share, 0 when they are side by side."""
    return max(min(a[2], b[2]) - max(a[0], b[0]), 0.0)


def vertical_gap(a: BBox, b: BBox) -> float:
    """Distance between the boxes along y, 0 when they overlap vertically."""
    return max(a[1] - b[3], b[1] - a[3], 0.0)


def centre(box: BBox) -> tuple[float, float]:
    return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2


def contains(box: BBox, x: float, y: float) -> bool:
    """Whether the point lies in the box, edges included."""
    return box[0] <= x <= box[2] and box[1] <= y <= box[3]


def centre_in(inner: BBox, outer: BBox) -> bool:
    """Whether `inner`'s centre lies in `outer`, edges included: the containment rule for a
    line, word or mark that may cross the edge of the box that holds it."""
    return contains(outer, *centre(inner))


def centre_strictly_in(inner: BBox, outer: BBox) -> bool:
    """Whether `inner`'s centre lies inside `outer`, edges excluded."""
    x, y = centre(inner)
    return outer[0] < x < outer[2] and outer[1] < y < outer[3]


def intersects(a: BBox, b: BBox) -> bool:
    """Whether the boxes share area (touching edges do not count)."""
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def edge_distance(a: BBox, b: BBox) -> float:
    """The straight-line distance between the boxes' nearest edges; 0 when they touch or overlap."""
    dx = max(a[0] - b[2], b[0] - a[2], 0.0)
    dy = max(a[1] - b[3], b[1] - a[3], 0.0)
    return (dx * dx + dy * dy) ** 0.5


def axis_gap(a: BBox, b: BBox) -> float:
    """The larger of the boxes' gaps along x and along y; 0 when they touch or overlap. Unlike
    `edge_distance`, a box diagonal to the other is as near as the farther of its two gaps."""
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return max(dx, dy)


def group_boxes(found: Iterable[BBox], gap: float) -> list[BBox]:
    """The word boxes `found`, those within `gap` of each other grouped into one box, top to
    bottom (the crops of "ocr-words-omitted" and "words-missing"). A word that
    reaches two groups joins them."""
    def near(a: list[float] | BBox, b: list[float] | BBox) -> bool:
        return (b[0] - gap <= a[2] and a[0] - gap <= b[2]
                and b[1] - gap <= a[3] and a[1] - gap <= b[3])

    boxes: list[list[float]] = []
    for b in sorted(found, key=lambda b: (b[1], b[0])):
        merged = list(b)
        while reached := [box for box in boxes if near(box, merged)]:
            for box in reached:
                boxes.remove(box)
                merged = [min(box[0], merged[0]), min(box[1], merged[1]),
                          max(box[2], merged[2]), max(box[3], merged[3])]
        boxes.append(merged)
    boxes.sort(key=lambda b: (b[1], b[0]))
    return [tuple(round(v, 2) for v in b) for b in boxes]
