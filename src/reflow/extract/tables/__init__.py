"""Table detection that reads the PDF's drawn rules (reflow.extract.tables.ruled) and what a table
is (.cells). The tables found from text lines and OCR words are made in the analysis
(reflow.analyze.tables).

This file imports nothing: importing any `reflow.extract.tables.*` module runs it, so whatever it
imported would join the extraction stage's key in the stage cache (reflow.stagecache), which
holds only `ruled` and `cells`."""
