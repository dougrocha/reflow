"""Scanned pages of prose set in real fonts, for checking that the scan rules take no round letter
for a field: the page as an image at the rules' resolution, with the OCR line of each row.

Shared by tests/test_scan_rules.py and scripts/eval_scan_fields.py."""

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from reflow.analyze.scan_rules import SCALE
from reflow.extract.lines import Line

FONT_DIRS = ("/usr/share/fonts/liberation", "/usr/share/fonts/TTF", "/usr/share/fonts/truetype/liberation",
             "/usr/share/fonts/truetype/dejavu")
FONTS = {"sans": "LiberationSans-Regular.ttf", "bold": "LiberationSans-Bold.ttf",
         "serif": "LiberationSerif-Regular.ttf", "dejavu": "DejaVuSans.ttf",
         "mono": "LiberationMono-Regular.ttf"}
# Lines of ordinary text with a lone round glyph in them: a word, an initial, a number or a unit.
# They are plain ASCII, so the font Pillow bundles ("bundled") can set them too.
PROSE_ASCII = [
    "uno o dos o tres o cuatro o cinco personas",
    "Marque si o no en cada caso",
    "Type O negative and Type O positive blood",
    "John O Smith and Mary O Jones",
    "Grade 0 or 0 points, score 0",
    "LLC or C corporation, see 8 or 0 or O only",
    "Items: o apples o pears o plums o figs",
]
# A lone round letter that opens or closes a line of words, where it has a word on one side only.
PROSE_EDGES = [
    "o apples and pears for sale",
    "apples and pears for sale o",
    "O brother where art thou",
    "Marque si o",
    "Vitamin A or vitamin D or O",
]
# A degree sign and hollow bullets, smaller than the smallest printed circle (TINY_CIRCLE).
PROSE_MARKS = [
    "Temperature 25 °C and 35 ° outside",
    "Options: ○ one ○ two ○ three",
    "Items ◦ first ◦ second ◦ third",
]


def font(name: str, size: float) -> ImageFont.FreeTypeFont | None:
    """The font at `size` pt on the rules' raster, or None when this machine lacks it."""
    if name == "bundled":
        return ImageFont.load_default(round(size * SCALE))
    for directory in FONT_DIRS:
        try:
            return ImageFont.truetype(f"{directory}/{FONTS[name]}", round(size * SCALE))
        except OSError:
            continue
    return None


def prose_page(name: str, size: float, paper: int = 255, blur: float = 0.0, marks: bool = True,
               texts: list[str] | None = None) -> tuple[Image.Image, list[Line]] | None:
    """A scanned prose page set in font `name` at `size` pt: (image, OCR lines), or None when the
    font is missing. `paper` is the gray of the paper, `blur` the radius in pixels of a scanner's
    softening, `marks` adds the lines with a degree sign and hollow bullets, `texts` sets those lines
    instead of the usual ones."""
    face = font(name, size)
    if face is None:
        return None
    texts = texts or (PROSE_ASCII if name == "bundled" else PROSE_ASCII + (PROSE_MARKS if marks else []))
    step = size * 2.2
    image = Image.new("L", (round(612 * SCALE), round((60 + step * len(texts)) * SCALE)), paper)
    draw = ImageDraw.Draw(image)
    lines = []
    for i, text in enumerate(texts):
        x, y = 72, 30 + step * i
        draw.text((x * SCALE, y * SCALE), text, font=face, fill=0)
        bbox = draw.textbbox((x * SCALE, y * SCALE), text, font=face)
        lines.append(Line(1, tuple(v / SCALE for v in bbox), text, size, False, False, "OCR", ocr=True))
    if blur:
        image = image.filter(ImageFilter.GaussianBlur(blur))
    return image, lines


def list_page(name: str, size: float, items: list[str], marker: str = "○", ocr_marker: str = "o",
              x: float = 90, paper: int = 255, blur: float = 0.0
              ) -> tuple[Image.Image, list[Line]] | None:
    """A scanned page with one list item per line, each opened by `marker` set in font `name` at
    `size` pt: (image, OCR lines), or None when the font is missing. The OCR line of an item
    starts with `ocr_marker` (tesseract reads a hollow bullet as "o", "O" or "0")."""
    face = font(name, size)
    if face is None:
        return None
    step = size * 2.2
    image = Image.new("L", (round(612 * SCALE), round((60 + step * len(items)) * SCALE)), paper)
    draw = ImageDraw.Draw(image)
    lines = []
    for i, item in enumerate(items):
        y = 30 + step * i
        draw.text((x * SCALE, y * SCALE), f"{marker} {item}", font=face, fill=0)
        bbox = draw.textbbox((x * SCALE, y * SCALE), f"{marker} {item}", font=face)
        lines.append(Line(1, tuple(v / SCALE for v in bbox), f"{ocr_marker} {item}", size, False, False,
                          "OCR", ocr=True))
    if blur:
        image = image.filter(ImageFilter.GaussianBlur(blur))
    return image, lines
