# TODO

- [ ] Add two sample PDFs. One needs a user password to open, to test `--password`. The other
      has permissions that block assistive technology, for SRC-META-004.
      `samples/azblue/provider-guide-05-admin.pdf` (not redistributed in this repository) already covers owner-only restrictions.
- [ ] Let `inspect` and `ingest` accept a URL, or add a `--source-url` option. Keep the URL in
      `meta`, and report where it disagrees with the document. A year in the path or file name
      can disagree with the title, the dates, or the first-page text. A language hint such as
      `-sp`, `/es/`, or `-en` can disagree with `/Lang` and with the detected text language.
      The check of `/Lang` against the text already exists.
- [ ] Make `inspect` list the empty pages by page number, and the "blank" pages whose only text
      is a page number. Today it prints only the count of each page kind, and it counts a page
      that holds only a number as born-digital.
- [ ] Extend `bun run a11y` (`web/scripts/a11y-check.ts`) to `/inspect`, `/review`, `/builds`,
      and the Ask panel. Measure the SPEC §6 timing budgets on a production build. Zero axe
      violations is not full WCAG conformance.
- [ ] Find the scanned-form fields that the rules still miss: dashed boxes, one rule under
      several captions, faint or merged circles and squares, and dotted lines on pages that
      look like contents pages. See [docs/assist.md](docs/assist.md), "Other open limits".
- [ ] Stamp the text that a reader types into the fields of a scanned form onto the scan, so
      that the reader can download the scan as a filled PDF. See [docs/assist.md](docs/assist.md).
- [ ] Count command-line builds against `REFLOW_BUILDS_CONCURRENCY`. Today the limit applies
      only to the jobs of the web app. See [docs/builds.md](docs/builds.md),
      "The runner starts page builds inside the Next server".
