# reflow web

This Next.js app shows reflow's accessible transcripts. It has a list of the converted
documents, a viewer with the transcript beside the page images, an inspector, the review items,
"Ask a question", and conversion jobs. The app runs on bun.

## Run the app

```sh
bun install
bun run docs:build resnet fw9   # convert samples into .reflow/ and public/docs/
bun run dev
```

With no arguments, `docs:build` converts every sample in `src/docs.ts`. The big samples take
minutes and hundreds of MB of page images. `docs:build` passes `REFLOW_ENGINE` and
`REFLOW_ALT_TEXT` through to `reflow convert-many`. `REFLOW_ALT_TEXT=ollama bun run docs:build
carmel` drafts figure alt text through a local Ollama server (see
[docs/assist.md](../docs/assist.md#figure-alt-text)). It is off unless you set it.

Each build records what its conversion read. `bun run docs:check fw9` says whether a document is
current against this checkout, and why. `bun run docs:build --stale fw9 resnet` rebuilds only the
ones that are not current, and `--stage-cache` reuses each PDF's reading (OCR, extraction) from the
page-check harness's cache. See [docs/provenance.md](../docs/provenance.md).

Under `bun run dev`, you can also rebuild a document from the `/builds` page. See
[docs/builds.md](../docs/builds.md). If no Ollama is running, the dev server starts its own
Ollama for "Ask a question". See [docs/ask.md](../docs/ask.md).

With the builds on (the dev server, or `REFLOW_BUILDS=1`), the inspector and the document page also
record review decisions: accept, keep or correct an OCR reading, write alt text, dismiss a warning,
undo and change. The dev server listens on `127.0.0.1` only, because those writes are not
authenticated. A decision takes effect at the next build. See
[docs/review-decisions.md](../docs/review-decisions.md#recording-decisions-from-the-web).

The [top-level README](../README.md#web-app) lists the other scripts and checks.

## Source layout

- `src/reflow/core.ts` is the headless core, with no framework. It loads `manifest.json` and
  splits the chunk pages into per-page HTML.
- `src/reflow/react/` holds the React bindings:
  - `useReflowDocument` holds the state.
  - `DocumentViewer` is the default UI: the page cards, the switch between transcript and
    original, find, and the sidebar accordion of contents and Ask.
  - `ReflowLinks` opens the viewer for a PDF link that has a converted version. It keeps the
    open document in `#reflow=...`.
- `src/app/` holds the pages (`/`, `/docs/<slug>`, `/view/<slug>`, `/inspect/<slug>`,
  `/review`, `/builds`, and `/about`) and the API routes (`/api/ask`, `/api/builds`,
  `/api/files`, and `/api/source`).
- `src/lib/ask/` holds the retrieval, the model providers, the quote checks, and the Ollama
  supervisor.
- `src/lib/builds/` holds the shared build record, the runner, and the gate.
- `src/lib/decisions/` holds the decision store, its fold, the write checks, the `/api/decisions`
  handlers, and the browser's side (`client.ts`, `client-store.ts`). `src/components/decisions/`
  holds the shared `useDecisions(slug)` hook and the form pieces; the panel is
  `components/inspector/DecisionPanel.tsx` and the list is `components/document/decisions-list.tsx`.
- `scripts/` holds `build-docs.ts` (`docs:build`), `a11y-check.ts` (`a11y`), the Ask
  checks, and `crossing/` (`check:crossing`, the check to rerun after a Next upgrade).
