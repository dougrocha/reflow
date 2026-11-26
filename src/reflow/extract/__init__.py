"""Text extraction: characters → lines with font size, weight, style and position.

reflow.extract.pages runs a page (extract_pages), .text builds its lines from .chars, with .fonts
and .rtl, into .lines, and .drawings and .images read its paths and pictures. Always run it on the
normalized copy from `reflow.ingest.load.normalize`, never the original. Coordinates follow the
model: PDF points, top-left origin, relative to the crop box.

This file imports nothing: importing any `reflow.extract.*` module runs it, so whatever it
imported would join the extraction stage's key in the stage cache (reflow.stagecache)."""
