from PIL import Image

from reflow.extract.crops import render_figures
from reflow.extract.parallel import make_pool
from reflow.render.pages import page_filename, render_pages
from reflow.pageimages import LARGE_WIDTH_MAX_PAGES, PAGE_WIDTHS, widths_for


def test_render_pages_writes_one_webp_per_page(mixed_pdf, tmp_path):
    out = tmp_path / "pages"
    done = []
    render_pages(mixed_pdf, out, 4, width=200, on_done=done.append)
    assert sorted(p.name for p in out.iterdir()) == [page_filename(n) for n in range(1, 5)]
    assert sum(done) == 4
    with Image.open(out / "0001.webp") as im:
        assert im.width == 200


def test_render_pages_on_a_made_pool(mixed_pdf, tmp_path):
    # pdfium is not fork-safe once the parent has initialized it, so make_pool must spawn.
    with make_pool(2) as pool:
        assert pool._mp_context.get_start_method() == "spawn"
        render_pages(mixed_pdf, tmp_path / "pages", 4, width=200, workers=2, executor=pool)
    assert len(list((tmp_path / "pages").iterdir())) == 4


def test_default_page_image_is_800_px_wide(mixed_pdf, tmp_path):
    render_pages(mixed_pdf, tmp_path, 1)
    with Image.open(tmp_path / "0001.webp") as im:
        assert im.width == 800


def test_render_figures_crops_to_bbox(mixed_pdf, tmp_path):
    sizes = render_figures(mixed_pdf, [(1, (72, 72, 172, 122), "fig.webp")], tmp_path)
    assert sizes == {"fig.webp": (200, 100)}  # 100 x 50 pt at 2 px/pt
    assert (tmp_path / "fig.webp").exists()


def test_extra_widths_write_suffixed_files_and_keep_the_base_name(mixed_pdf, tmp_path):
    out = tmp_path / "pages"
    render_pages(mixed_pdf, out, 2, width=200, extra_widths=(400,), workers=1)
    assert sorted(p.name for p in out.iterdir()) == [
        "0001-400.webp", "0001.webp", "0002-400.webp", "0002.webp"]
    with Image.open(out / "0001.webp") as small, Image.open(out / "0001-400.webp") as big:
        assert (small.width, big.width) == (200, 400)
    assert page_filename(7) == "0007.webp" and page_filename(7, 1200) == "0007-1200.webp"


def test_large_width_is_skipped_past_the_page_threshold():
    assert widths_for(PAGE_WIDTHS, 10) == PAGE_WIDTHS
    assert widths_for(PAGE_WIDTHS, LARGE_WIDTH_MAX_PAGES) == PAGE_WIDTHS
    assert widths_for(PAGE_WIDTHS, LARGE_WIDTH_MAX_PAGES + 1) == (800,)
    assert widths_for(PAGE_WIDTHS, 5000, max_pages=0) == PAGE_WIDTHS  # 0: no limit
    assert widths_for(PAGE_WIDTHS, 5000, max_pages=None) == PAGE_WIDTHS
    assert widths_for(PAGE_WIDTHS, 11, max_pages=10) == (800,)
