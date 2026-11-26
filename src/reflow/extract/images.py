"""A page's images and the small ones that work as icons."""

from __future__ import annotations

import ctypes
import hashlib
from dataclasses import dataclass

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from reflow.model import BBox
from reflow.pdfobjects import iter_objects, to_top_left

# Images no larger than this (points) on both sides are icons: status marks, checkboxes,
# bullets. They get a content key so the layout step can match them to a legend.
ICON_MAX_SIDE = 16.0


@dataclass
class Icon:
    """A tiny image. Images with the same `key` show the same picture."""
    bbox: BBox
    key: str

    @property
    def width(self) -> float: return self.bbox[2] - self.bbox[0]


def _image_key(obj: pdfium.PdfObject) -> str | None:
    """Hash of the image's raw (still encoded) stream: every use of one XObject gives the
    same bytes, and icons are a few hundred bytes, so this is cheap. Falls back to the
    decoded data for images pdfium can't hand over raw."""
    for get in (pdfium_c.FPDFImageObj_GetImageDataRaw,
                pdfium_c.FPDFImageObj_GetImageDataDecoded):
        n = get(obj.raw, None, 0)
        if n:
            buf = ctypes.create_string_buffer(n)
            get(obj.raw, buf, n)
            return hashlib.blake2b(buf.raw, digest_size=8).hexdigest()
    return None


def page_images_and_icons(page: pdfium.PdfPage) -> tuple[list[BBox], list[Icon]]:
    """Every image's box, and the tiny ones (≤ ICON_MAX_SIDE) as keyed icons."""
    images: list[BBox] = []
    icons: list[Icon] = []
    for obj, box in iter_objects(page, {pdfium_c.FPDF_PAGEOBJ_IMAGE}):
        bbox = to_top_left(page, box)
        images.append(bbox)
        if bbox[2] - bbox[0] <= ICON_MAX_SIDE and bbox[3] - bbox[1] <= ICON_MAX_SIDE:
            key = _image_key(obj)
            if key is not None:
                icons.append(Icon(bbox, key))
    return images, icons
