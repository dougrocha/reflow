"""Ingest: open a PDF, read document facts, classify each page → an empty Document.

The source pass is `reflow.prepare`. This file imports nothing: importing any
`reflow.ingest.*` module runs it, so whatever it imported would join every ingest and OCR stage's
key in the stage cache (reflow.stagecache)."""
