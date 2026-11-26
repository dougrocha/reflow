import random

from reflow import geometry
from reflow.model import BBox


def random_box(rng: random.Random, inverted: bool = False) -> BBox:
    # Sorted per axis so most boxes are well formed and often overlap; `inverted` swaps x0/x1.
    x0, x1 = sorted((rng.uniform(0, 200), rng.uniform(0, 200)))
    y0, y1 = sorted((rng.uniform(0, 200), rng.uniform(0, 200)))
    if inverted:
        x0, x1 = x1, x0
    return (x0, y0, x1, y1)


def test_disjoint_boxes_share_nothing():
    a = (0.0, 0.0, 10.0, 10.0)
    b = (20.0, 20.0, 30.0, 30.0)
    assert geometry.overlap(a, b) == 0
    assert geometry.iou(a, b) == 0
    assert geometry.share(a, b) == 0
    assert geometry.horizontal_overlap(a, b) == 0
    assert geometry.vertical_gap(a, b) == 10.0


def test_touching_boxes_have_zero_overlap_and_zero_gap_on_shared_edge():
    left = (0.0, 0.0, 10.0, 10.0)
    right = (10.0, 0.0, 20.0, 10.0)
    below = (0.0, 10.0, 10.0, 20.0)
    assert geometry.overlap(left, right) == 0
    assert geometry.horizontal_overlap(left, right) == 0
    assert geometry.vertical_gap(left, right) == 0
    assert geometry.vertical_gap(left, below) == 0
    assert geometry.horizontal_overlap(left, below) == 10.0


def test_nested_boxes():
    outer = (0.0, 0.0, 100.0, 100.0)
    inner = (10.0, 10.0, 30.0, 30.0)
    assert geometry.share(inner, outer) == 1
    assert geometry.share(outer, inner) == 400 / 10000
    assert geometry.overlap(inner, outer) == geometry.area(inner)
    assert geometry.iou(inner, outer) == geometry.area(inner) / geometry.area(outer)
    assert geometry.inside(inner, outer)
    assert not geometry.inside(outer, inner)


def test_identical_boxes():
    a = (5.0, 6.0, 25.0, 46.0)
    assert geometry.area(a) == 20 * 40
    assert geometry.overlap(a, a) == geometry.area(a)
    assert geometry.iou(a, a) == 1
    assert geometry.share(a, a) == 1
    assert geometry.inside(a, a)


def test_inverted_boxes_have_no_area_and_share_nothing():
    inverted = (30.0, 30.0, 10.0, 10.0)
    whole = (0.0, 0.0, 100.0, 100.0)
    assert geometry.area(inverted) == 0
    assert geometry.overlap(inverted, whole) == 0
    assert geometry.share(inverted, whole) == 0
    assert geometry.iou(inverted, whole) == 0


def test_empty_part_gives_zero_share():
    assert geometry.share((5.0, 5.0, 5.0, 9.0), (0.0, 0.0, 10.0, 10.0)) == 0


def test_share_of_a_box_with_itself_is_one():
    rng = random.Random(7)
    for _ in range(50):
        box = random_box(rng)
        if geometry.area(box):
            assert geometry.share(box, box) == 1


def test_iou_is_symmetric_and_bounded():
    rng = random.Random(11)
    for _ in range(50):
        a, b = random_box(rng), random_box(rng)
        assert geometry.iou(a, b) == geometry.iou(b, a)
        assert 0 <= geometry.iou(a, b) <= 1


def test_union_of_no_boxes_is_none():
    assert geometry.union([]) is None


def test_union_holds_every_box():
    boxes = [(0.0, 5.0, 10.0, 15.0), (-3.0, 2.0, 4.0, 8.0), (6.0, 1.0, 9.0, 40.0)]
    assert geometry.union(boxes) == (-3.0, 1.0, 10.0, 40.0)
    assert geometry.union(iter(boxes)) == (-3.0, 1.0, 10.0, 40.0)
    assert geometry.union([boxes[0]]) == boxes[0]


def test_inside_respects_slack_on_every_side():
    outer = (0.0, 0.0, 100.0, 100.0)
    assert geometry.inside((-2.0, -2.0, 102.0, 102.0), outer)
    assert not geometry.inside((-2.5, 10.0, 50.0, 50.0), outer)
    assert not geometry.inside((10.0, 10.0, 50.0, 102.5), outer)
    assert not geometry.inside((-1.0, 0.0, 50.0, 50.0), outer, slack=0.5)
    assert geometry.inside((-1.0, 0.0, 50.0, 50.0), outer, slack=1.0)
    assert geometry.inside((10.0, 10.0, 50.0, 50.0), (10.0, 10.0, 50.0, 50.0), slack=0.0)


def test_horizontal_overlap_and_vertical_gap_partial():
    a = (0.0, 0.0, 50.0, 10.0)
    b = (30.0, 25.0, 80.0, 40.0)
    assert geometry.horizontal_overlap(a, b) == 20.0
    assert geometry.vertical_gap(a, b) == 15.0
    assert geometry.vertical_gap(b, a) == 15.0



def test_overlap_share_iou_and_union_follow_their_definitions():
    """The module writes each one out without temporary boxes; they must equal the definitions."""
    rng = random.Random(23)
    for _ in range(500):
        a, b = random_box(rng, rng.random() < 0.2), random_box(rng, rng.random() < 0.2)
        assert geometry.overlap(a, b) == geometry.area(geometry.intersection(a, b))
        own = geometry.area(a)
        assert geometry.share(a, b) == (geometry.overlap(a, b) / own if own else 0.0)
        union_area = geometry.area(a) + geometry.area(b) - geometry.overlap(a, b)
        assert geometry.iou(a, b) == (geometry.overlap(a, b) / union_area if union_area > 0 else 0.0)
        boxes = [random_box(rng, rng.random() < 0.2) for _ in range(rng.randint(1, 6))]
        assert geometry.union(boxes) == (min(x[0] for x in boxes), min(x[1] for x in boxes),
                                         max(x[2] for x in boxes), max(x[3] for x in boxes))


def test_centre_in_reads_the_centre_not_the_edges():
    box = (100, 100, 300, 300)
    assert geometry.centre_in((220, 150, 360, 160), box)       # crosses the right edge, centre inside
    assert not geometry.centre_in((280, 150, 500, 160), box)   # centre outside
    assert geometry.centre_in((300, 100, 300, 100), box)       # on the corner
    assert not geometry.centre_strictly_in((300, 100, 300, 100), box)


def test_edge_distance_is_straight_and_axis_gap_is_the_larger_axis():
    a, b = (0, 0, 10, 10), (13, 14, 20, 20)
    assert geometry.edge_distance(a, b) == 5.0
    assert geometry.axis_gap(a, b) == 4.0
    assert geometry.edge_distance(a, (5, 5, 20, 20)) == 0.0
    assert geometry.axis_gap(a, (5, 5, 20, 20)) == 0.0
