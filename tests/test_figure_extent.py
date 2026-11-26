"""A chart's figure box covers its axis text; the pieces of one picture are one figure."""

from reflow.analyze.figures import figure_boxes, figure_groups
from reflow.extract.drawings import Drawing
from reflow.extract.lines import Line
from reflow.extract.pages import PageContent


def _label(text, x0, top, x1, bottom, rotated=False):
    return Line(1, (x0, top, x1, bottom), text, 8.0, False, False, "F", rotated=rotated)


def test_a_chart_box_takes_in_wrapped_axis_labels_and_the_rotated_axis_title():
    bars = Drawing((154, 187, 490, 409), paths=16, rules=0, marks=(("bars", 6),))
    lines = [_label("Race and", 279, 412, 310, 422), _label("ethnicity", 279, 421, 309, 431),
             _label("Income", 393, 412, 418, 422), _label("and", 399, 421, 412, 431),
             _label("education", 389, 430, 423, 440),
             _label("NOTE: Subgroups are shown", 125, 447, 458, 457)]
    title = _label("Number of Stats", 142, 262, 151, 334, rotated=True)
    page = PageContent(2, 612, 792, lines=lines + [title], images=[], drawings=[bars])

    [found] = figure_boxes(page, lines, 9.0)

    box, labels = found.bbox, [page.lines[i] for i in found.labels]
    assert 440 <= box[3] < 447  # the third label line, not the note under it
    assert box[0] <= 142  # the rotated title
    assert {l.text for l in labels} >= {"ethnicity", "education", "Race and"}
    assert all(not l.text.startswith("NOTE") for l in labels)


def test_images_that_overlap_with_the_same_top_and_bottom_are_one_figure():
    page = PageContent(2, 792, 612, lines=[], drawings=[],
                       images=[(-1, -1, 505, 225), (335, -1, 793, 226)])
    assert [f.bbox for f in figure_boxes(page, [])] == [(-1, -1, 793, 226)]


def test_photos_in_a_grid_with_gutters_stay_separate():
    page = PageContent(2, 792, 612, lines=[], drawings=[],
                       images=[(50, 50, 250, 250), (260, 50, 460, 250), (50, 260, 250, 460)])
    assert len(figure_boxes(page, [])) == 3


def test_columns_and_stacked_pieces_under_one_caption_are_one_figure():
    # Three networks side by side, the first in three stacked pieces; one caption spans them all.
    figures = [(50, 78, 123, 365), (52, 355, 123, 518), (53, 507, 123, 618),
               (144, 78, 194, 591), (212, 78, 285, 591)]
    groups = figure_groups(figures, [(50, 634, 286, 696)], 10.0)
    assert groups == [figures]


def test_side_by_side_figures_with_their_own_captions_stay_apart():
    figures = [(50, 100, 250, 300), (300, 100, 450, 300)]
    captions = [(50, 310, 250, 330), (300, 310, 450, 330)]
    assert figure_groups(figures, captions, 10.0) == [[figures[0]], [figures[1]]]


def test_pieces_without_a_caption_beneath_stay_apart():
    figures = [(50, 100, 250, 300), (260, 100, 460, 300)]
    assert figure_groups(figures, [], 10.0) == [[figures[0]], [figures[1]]]
