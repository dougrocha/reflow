"""Every name a conversion writes at the top of its output directory, once, here.

This module imports nothing: the model's code (ingest and analyze) reads these names without
importing reflow.output, whose renderer is not model code (reflow.saved keys the model on the
code it runs, so an edit to the renderer must not change the model's key)."""

ORIGINAL_PDF = "original.pdf"      # the input, byte for byte: the reader's copy
WORKING_PDF = "working.pdf"        # qpdf's repaired, decrypted, linearized copy: what the passes read
OCR_WORDS = "ocr-words.json"       # the working copy's OCR word grades (ingest.ocr_trust)
DOCUMENT_JSON = "document.json"    # the whole model
REVIEW_JSON = "review.json"        # the review queue (analyze.review)
MODEL_JSON = "model.json"          # what the model passes know besides the Document (reflow.pipeline)
BUILD_JSON = "build.json"          # the identities that built this output (reflow.saved)
ALT_CACHE = "alt-text-cache.json"  # drafted alt text, carried to the next run
ASSIST_CACHE = "ai-assist-cache.json"
PROPOSALS_JSON = "page-vision-proposals.json"   # page vision's proposals (analyze.page_vision)
FORM_REPAIR_JSON = "form-repair-proposals.json"   # form repair's readings (analyze.form_repair)
PROPOSAL_DECISIONS_JSON = "proposal-decisions.json"  # a reviewer's decisions on them (analyze.proposals)
DECISIONS_LEDGER = "decisions.json"             # what a reviewer's decisions file did (analyze.decisions_apply)
INDEX_HTML = "index.html"
MANIFEST_JSON = "manifest.json"
TOC_JSON = "toc.json"
CHUNKS_DIR = "chunks"
PAGES_DIR = "pages"                # page images, NNNN.webp (+ NNNN-<width>.webp)
FIGURES_DIR = "figures"            # figure crops, pNNNN-i.webp
ASSETS_DIR = "assets"
TRACE_DIR = "trace"                # the heuristic engine's per-page record (and the old single file)
TRACE_JSON = "trace.json"
LAYOUT_DIR = "layout"              # the per-page sidecars (render.sidecars)
PASSAGES_JSONL = "passages.jsonl"  # the passages Ask reads (render.passages)

# What a reader of the published site is meant to see at the top of the output directory.
PUBLIC_NAMES = frozenset({
    ORIGINAL_PDF, INDEX_HTML, MANIFEST_JSON, TOC_JSON, REVIEW_JSON, CHUNKS_DIR, PAGES_DIR,
    FIGURES_DIR, ASSETS_DIR, DECISIONS_LEDGER,
})
# Working files a conversion keeps beside the public ones. They are not for the reader.
INTERNAL_NAMES = frozenset({
    WORKING_PDF, OCR_WORDS, DOCUMENT_JSON, MODEL_JSON, BUILD_JSON, ALT_CACHE, ASSIST_CACHE,
    PROPOSALS_JSON, FORM_REPAIR_JSON, PROPOSAL_DECISIONS_JSON, TRACE_JSON, TRACE_DIR, LAYOUT_DIR,
    PASSAGES_JSONL,
})
# The files that carry the version, and the directories served immutable at a versioned URL
# (web/next.config.ts): together, everything the version must change with (reflow.output).
VERSIONED = (INDEX_HTML, MANIFEST_JSON, TOC_JSON)
IMMUTABLE_DIRS = (ASSETS_DIR, CHUNKS_DIR, FIGURES_DIR, PAGES_DIR)
# Everything a conversion writes at the top of its output directory.
OUTPUT_NAMES = PUBLIC_NAMES | INTERNAL_NAMES
# Kept from the previous output: drafted alt text, so a rerun only describes changed figures,
# the assist answers, and a reviewer's decisions on page-vision proposals.
CARRIED = (ALT_CACHE, ASSIST_CACHE, PROPOSAL_DECISIONS_JSON)
# What the model passes write (reflow.pipeline), the saved model a render-only change renders
# again (reflow.saved): the Document, the review queue, what else the passes knew, the engine's
# trace, the figure crops, and the working copy with its OCR word grades.
MODEL_NAMES = (DOCUMENT_JSON, REVIEW_JSON, MODEL_JSON, TRACE_DIR, FIGURES_DIR, WORKING_PDF,
               OCR_WORDS)
