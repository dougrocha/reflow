# Sample PDFs

This folder holds real-world PDFs for trying out `reflow` and for the page checks in `tests/pages/`.

## Samples at the top of the folder

`arxiv.pdf` and `resnet.pdf` are not committed (they are gitignored).
Run `scripts/fetch_samples.py` to download the two arXiv papers, which it checks against pinned
SHA-256 hashes.

| File | Source | Why it's here |
|---|---|---|
| `fw9.pdf` | IRS Form W-9, https://www.irs.gov/pub/irs-pdf/fw9.pdf | Tagged government form |
| `arxiv.pdf` (not committed; `scripts/fetch_samples.py`) | "Attention Is All You Need", https://arxiv.org/pdf/1706.03762 | Untagged LaTeX paper with no title or language |
| `scan.pdf` | `fw9.pdf` rendered at 150 DPI grayscale and saved as images with Pillow | Image-only "scanned" document. Pillow set `/Title` to the filename. |
| `scan-i9.pdf` | Pages 1, 3 and 4 of `corpus/uscis-i9.pdf`, made with `scripts/make_scan.py` (150 DPI grayscale JPEG pages, fixed dates) | Scanned form with no AcroForm. The 130 widgets in the source are the field ground truth. |
| `scan-w4.pdf` | Pages 1, 3 and 4 of `corpus/irs-w4-2026.pdf`, made with `scripts/make_scan.py` | Scanned form with no AcroForm. The widgets on those pages of the source are the field ground truth. |
| `scan-census-1970.pdf` | Pages 1 and 2 of the 1970 Census questionnaire, https://www.census.gov/content/dam/Census/programs-surveys/decennial/technical-documentation/questionnaires/1970_questionnaire.pdf (US Census Bureau, a US government work). pikepdf copied the pages unchanged. The SHA-256 of this file is `e9218d1faf6068475e2022a090b020cf589572218c2a0748ad85c12d8930a51c`, and of the 7-page original `a864ce06b9aceb34fb61a067155ab36e453cfd918a34246d592bebbbcefcd71f`. Retrieved 2026-10-22. | A real paper scan with 400 dpi CCITT images, no fonts and no text layer (`pdftotext` returns nothing). Adobe Acrobat 7.0 "Image Conversion" made it in 2008. Page 1 is 1189 x 814 pt. The write-in places are dashed rules and fill-in circles, with no squares. Skew is under 0.1 degrees (a row-profile estimate). The ground truth for the scan-field eval is hand-labelled in `tests/scan_fields/fields.json`, under `census1970-scan`. |
| `scan-census-1960.pdf` | Pages 2 and 4 of the 1960 Census of Population and Housing questionnaire, https://www.census.gov/content/dam/Census/programs-surveys/decennial/technical-documentation/questionnaires/1960censusquestionnaire-2.pdf (US Census Bureau, a US government work). pikepdf copied the pages unchanged. The SHA-256 of this file is `d5dd113f15146b19639d4d235e296dd14854d3d91037a77226c1f3ab02003795`, and of the 16-page original `df9c11ed5aca9b2d2a9336964f58a8f2e51547612f0cf3f1b17b7ff2c4bef342`. Retrieved 2026-10-22. | A real paper scan with 600 dpi JPEG page images on grey paper and a poor Acrobat Paper Capture OCR layer ("PU.ASE OTE"), made in 2014. It has square checkboxes and dotted write-in lines. Skew is under 0.1 degrees. The ground truth for the scan-field eval is hand-labelled, under `census1960-scan`. |
| `p17.pdf` | IRS Publication 17, https://www.irs.gov/pub/irs-pdf/p17.pdf | Long (142 pages) and tagged |
| `budget-app.pdf` | FY2025 Budget Appendix, https://www.govinfo.gov/content/pkg/BUDGET-2025-APP/pdf/BUDGET-2025-APP.pdf | 1,298 pages, 21 of them blank. It has a structure tree but isn't marked as tagged. |
| `naca-ocr.pdf` | NACA report via NASA NTRS, https://ntrs.nasa.gov/api/citations/19930091979/downloads/19930091979.pdf | Scan with a poor Acrobat "Paper Capture" OCR layer, which makes "mixed" pages with words such as "Tirne" and "rnile". The file is kept byte for byte. It has 436 bytes of wrapper before `%PDF-` and is linearized, so pikepdf and pdfium read it but pdfplumber sees 0 pages. |
| `resnet.pdf` (not committed; `scripts/fetch_samples.py`) | "Deep Residual Learning for Image Recognition", https://arxiv.org/pdf/1512.03385 | Two-column CVPR layout, for reading order |

## Real badly made PDFs in `robustness/`

`robustness/MANIFEST.tsv` lists real PDFs that tools made badly, for the robustness harness. Small
files we may redistribute are committed in `robustness/files/`; the rest are fetched into the
gitignored `robustness/fetched/`. `robustness/README.md` has the sources and licences.

## Gitignored samples

These paths are not committed (see `.gitignore`).

- `samples/arxiv.pdf` and `samples/resnet.pdf` are the two arXiv papers. `scripts/fetch_samples.py`
  downloads them from arxiv.org and checks their pinned SHA-256 hashes.
- Some page checks (`az-*` and `docaccess-*`) look for PDFs under `samples/azblue/` and
  `samples/docaccess/`. Those PDFs are not redistributed here, and the checks skip without them.
- `samples/robustness/fetched/` holds the real badly made PDFs we may not redistribute.
  `scripts/fetch_robustness_corpus.py` downloads them (see `robustness/README.md`).
- `samples/heldout/files/` holds the held-out PDFs. `scripts/fetch_heldout.py` downloads them (see
  `heldout/README.md`).

## Public-domain test PDFs in `samples/corpus/`

These files were downloaded on 2026-10-07 from the official sites of their publishers. Each one is
a US federal government work or has a confirmed basis for redistribution, listed with the file.
The corpus has 12 files, 33,342,786 bytes (33.3 MB) in total.

Page numbers are 1-based PDF indexes. Where the printed page number differs, it follows in
parentheses.

Each file was checked with pypdfium2 and pikepdf for magic bytes, page count, tag status
(`StructTreeRoot` and `MarkInfo/Marked`), AcroForm widgets, rotation, and page-object types. Its
pages were rendered and checked by eye.

### `corpus/nchs-bar-chart.pdf`

- Source: https://stacks.cdc.gov/view/cdc/84217/cdc_84217_DS1.pdf (record: https://stacks.cdc.gov/view/cdc/84217)
- Publisher: CDC and the National Center for Health Statistics. QuickStats, "Celebrating 15 years of QuickStats from NCHS" (MMWR, 2020)
- License basis: US government work. The STACKS rights statement is "Public Domain" (confirmed 2026-10-07).
- Retrieved: 2026-10-07. Pages: 2. Size: 164,901 bytes
- SHA-256: `a5a1a346d06eefa6b6ca0f74928eafc1fd912c4d2f40ccb09dd1cec2fab26da9`
- Covers: a vector bar chart. Page 2 has six bars drawn as path objects, and the file has no raster images. Page 1 is a vector pie chart with text labels on its slices. Tagged.
- Pages to check: p2 (bar chart, "by population characteristics") and p1 (pie chart, "by topic").

### `corpus/census-p60-273.pdf`

- Source: https://www.census.gov/content/dam/Census/library/publications/2021/demo/p60-273.pdf (landing: https://www.census.gov/library/publications/2021/demo/p60-273.html)
- Publisher: U.S. Census Bureau. Income and Poverty in the United States: 2020 (P60-273)
- License basis: US government work (17 U.S.C. 105). The landing page states no license.
- Retrieved: 2026-10-07. Pages: 92. Size: 2,166,539 bytes
- SHA-256: `7c856144b5d9cb0ebfd108b8370c2871de8d8c7abd4db6596c59501ea1e01ab4`
- Covers: a tagged report with many figures and tables. Figure 7 is a vector horizontal bar chart with two series. Figure 8 is a line chart. Figure 1 is a dot plot. Pages 36-46, 54-56 and 63 have `/Rotate 90`, so a portrait mediabox shows as landscape. These pages include wide tables such as Table A-2. The AcroForm dictionary is present with no fields.
- Pages to check: p21 (Figure 7, printed p13, bar chart), p22 (Figure 8, printed p14, line chart), p11 (Figure 1, printed p3, dot plot), and p36 (rotated table, printed p28).

### `corpus/cdc-firstline-slides-es.pdf`

- Source: https://stacks.cdc.gov/view/cdc/153850/cdc_153850_DS1.pdf (record: https://stacks.cdc.gov/view/cdc/153850)
- Publisher: CDC and the National Training Collaborative. Project Firstline (Spanish, 20-minute version)
- License basis: US government work. The STACKS rights statement is "Public Domain" (confirmed 2026-10-07).
- Retrieved: 2026-10-07. Pages: 14 slides. Size: 966,766 bytes
- SHA-256: `cee5c3635b69f67349dc9d32eee5ae1b579c28aeee9de4521aeaba5f8f01908f`
- Covers: a slide deck exported to PDF, with Spanish text that has accents and inverted punctuation. Landscape, 960 x 540 pt. Not tagged. It has title slides with photos, bullet slides and a centred question slide. OpenOffice Impress created it.
- Pages to check: p1 (title slide with four photos), p3 (bullet slide, "Objetivos de aprendizaje"), p9 (centred question slide, "Reflexiones"), and p5 (title slide with an episode label).

### `corpus/uscis-i9.pdf`

- Source: https://www.uscis.gov/sites/default/files/document/forms/i-9.pdf (form edition 01/20/25, landing: https://www.uscis.gov/i-9)
- Publisher: USCIS, Form I-9, Employment Eligibility Verification
- License basis: US government work (17 U.S.C. 105)
- Retrieved: 2026-10-07. Pages: 4. Size: 524,095 bytes
- SHA-256: `780f348c34df694bb0b4dbbfaf9f22b99b9757b80d16a37ba89aadf069597281`
- Covers: a tagged, fillable AcroForm with 130 widgets. There are 117 text fields, 5 choice fields (combo or list), and 8 checkboxes (checkbox flags, not radio or push button). Page 1 has the Section 1 citizenship checkbox group (1-4) and the Section 2 "alternative procedure" checkbox. The layout is a grid with signature blocks.
- Pages to check: p1 (checkboxes 1-4, the alternative-procedure checkbox, and fields). Nobody checked pages 2-4 by eye.

### `corpus/irs-w4-2026.pdf`

- Source: https://www.irs.gov/pub/irs-pdf/fw4.pdf (landing: https://www.irs.gov/forms-pubs/about-form-w-4)
- Publisher: IRS. 2026 Form W-4, Employee's Withholding Certificate (created 12/8/25)
- License basis: US government work (17 U.S.C. 105)
- Retrieved: 2026-10-07. Pages: 5. Size: 208,845 bytes
- SHA-256: `92444d8856ce55d9e25dca8b6d1420634fc68b11e1ab1f760916ea29ddd312b2`
- Covers: tagged. The AcroForm has 48 widgets, 43 text fields and 5 checkboxes. The catalog also has an XFA entry (pdfinfo reports `Form: XFA`), so this is an XFA-enabled AcroForm. Page 1 has the Step 1(c) filing-status checkboxes, the Step 2(c) checkbox and the exemption checkbox. Page 4 is the Step 4(b) Deductions Worksheet, with numbered lines and amount fields.
- Pages to check: p1 (fields, checkboxes, Steps 1-5) and p4 (Deductions Worksheet).

### `corpus/cdc-hepb-arabic.pdf`

- Source: https://stacks.cdc.gov/view/cdc/82549/cdc_82549_DS1.pdf (record: https://stacks.cdc.gov/view/cdc/82549)
- Publisher: CDC. "Hepatitis B: Are You At Risk?" (Arabic on the front, English on the back, October 2016)
- License basis: US government work. The STACKS rights statement is "Public Domain" (confirmed 2026-10-07).
- Retrieved: 2026-10-07. Pages: 2. Size: 1,283,374 bytes
- SHA-256: `5a15d215285cbe09db5b03e49a0c34ce844fd4390ed1cd952e8e703068dbd1a3`
- Covers: right-to-left Arabic in the text layer, about 2,000 Arabic characters on p1. Latin text, digits and a URL (`www.cdc.gov/hepatitis`) sit inside Arabic lines. Tagged, with document language `ar`. The extracted text is not in logical order. Lines and words come out shuffled and some glyphs are duplicated, which makes the file a useful RTL ordering test. The AcroForm dictionary has no fields.
- Pages to check: p1 (Arabic, RTL, headings, bullets, inline URL) and p2 (English, LTR).

### `corpus/scotus-mahmoud-v-taylor.pdf`

- Source: https://www.supremecourt.gov/opinions/24pdf/606us2r63_n648.pdf (index: https://www.supremecourt.gov/opinions/slipopinion/24, No. 24-297, decided June 27, 2025)
- Publisher: Supreme Court of the United States. Mahmoud v. Taylor, preliminary print (606 U.S. 522)
- License basis: US court opinion. Judicial opinions are not copyrightable (17 U.S.C. 105, and the government edicts doctrine in Georgia v. Public.Resource.Org, 2020). The Website Policies page of the Supreme Court states no reuse restriction on the opinions. This is a legal basis, not an explicit license grant, so check it again if the terms of the site change.
- Retrieved: 2026-10-07. Pages: 136. Size: 4,771,326 bytes
- SHA-256: `0a28233df1d708765ce3398e2897d44e11bbf1875ee13100d18920c542b8273b`
- Covers: tagged, with numbered in-page footnotes, a syllabus, a caption, and concurring and dissenting opinions. It carries a "Page Proof Pending Publication" watermark. It is encrypted with only an owner password (RC4 128, change not permitted), so it opens without a password.
- Pages to check: p2 (caption and syllabus, printed p522), p39 (footnote 9, printed p559), and p72 (start of the Sotomayor dissent, printed p592).

### `corpus/frcp-2025.pdf`

- Source: https://www.govinfo.gov/content/pkg/CPRT-119HPRT61922/pdf/CPRT-119HPRT61922.pdf (record: https://www.govinfo.gov/app/details/CPRT-119HPRT61922)
- Publisher: U.S. House of Representatives Committee on the Judiciary, via GovInfo. Federal Rules of Civil Procedure, December 1, 2025, committee print. The official reference is H. Doc. 119-44.
- License basis: US government work (17 U.S.C. 105)
- Retrieved: 2026-10-07. Pages: 142. Size: 457,622 bytes
- SHA-256: `ac1e7a419ce1a07e2ec81cd3c2be223d766f3258949943f3f360176d672202c4`
- Covers: nested enumerations ((a)(1)(A)(i)), running headers, roman-numeral front matter, and committee-note text. Not tagged (no `StructTreeRoot`). The AcroForm has one signature field (`USGPOSignature`) and no other fields.
- Pages to check: p16 (table of contents, printed p. XV), p20 (Rule 1), and p58 (Rule 26(a)(1)(A)(i)-(iv), printed p39, nested enumerations).

### `corpus/nasa-se-handbook.pdf`

- Source: https://ntrs.nasa.gov/api/citations/20170001761/downloads/20170001761.pdf (citation: https://ntrs.nasa.gov/citations/20170001761)
- Publisher: NASA. NASA/SP-2016-6105 Rev 2, NASA Systems Engineering Handbook
- License basis: NTRS "Public Use Permitted". The NASA Images and Media Usage Guidelines say that NASA content is generally not subject to copyright. The copyright references in the handbook are bibliographic citations, not reproduced third-party material.
- Retrieved: 2026-10-07. Pages: 356. Size: 4,122,125 bytes
- SHA-256: `3153ae2e53e29452d5997efafe280a5f05cd21b43a047e988a17e1dd5207a38e`
- Covers: a long technical handbook with a Venn-style figure with callout labels, shaded call-out boxes in the style of sidebars, wide tables and nested bullets. It is not tagged in the usual sense. A `StructTreeRoot` is present but `MarkInfo/Marked` is missing, so pdfinfo reports "Tagged: no". These pages have `/Rotate 90` and display as landscape: 30, 31, 34, 35, 57, 58, 200-204, 208, 254, 255, 257, 293-296. Page 29 has a landscape mediabox.
- Pages to check: p18 (Figure 2.0-1, printed p5, callout labels), p47 (shaded call-out box, printed p34), and p30 (rotated competency table, printed p17).

### `corpus/epa-ghg-inventory-2024.pdf`

- Source: https://www.epa.gov/system/files/documents/2024-04/us-ghg-inventory-2024-main-text_04-18-2024.pdf (landing: https://www.epa.gov/ghgemissions/inventory-us-greenhouse-gas-emissions-and-sinks-1990-2022)
- Publisher: U.S. EPA. Inventory of U.S. Greenhouse Gas Emissions and Sinks: 1990-2022, main text (EPA 430-R-24-004)
- License basis: US government work (17 U.S.C. 105). The document has no copyright statement.
- Retrieved: 2026-10-07. Pages: 919. Size: 16,723,135 bytes (16.7 MB, under the 40 MB limit)
- SHA-256: `f3a3309d3f9308f58d5f467bd82445dfa887ce0393b359c50da6c5fdb00872a3`
- Covers: a very long single-column report with many figures and long tables. Tagged. It has five landscape pages, body footnotes, a list of figures, and a bar and line chart (Figure ES-1) beside a long table (Table ES-2).
- Pages to check: p41 (Figure ES-1 and Table ES-2, printed ES-5), p40 (body text with footnote 10, printed ES-4), and p29 (list of figures, printed xxviii).

### `corpus/cdc-antibiotics-trifold.pdf`

- Source: https://stacks.cdc.gov/view/cdc/49713/cdc_49713_DS1.pdf (record: https://stacks.cdc.gov/view/cdc/49713)
- Publisher: CDC. "Antibiotics Aren't Always the Answer" tri-fold brochure (November 2, 2017)
- License basis: US government work. The STACKS rights statement is "Public Domain" (confirmed 2026-10-07).
- Retrieved: 2026-10-07. Pages: 2 sheets (6 panels). Size: 1,017,433 bytes
- SHA-256: `3a526160662d9ca43c92178ab757c8e63150aeddee6b4a1dfe16c199d191917d`
- Covers: a tri-fold with two landscape sides (792 x 612 pt), tagged. p1 is the outside, with the flap on the left, the back panel in the middle and the cover on the right. p2 is the inside, with three panels. Both sides are upright, with no rotated panels.
- Pages to check: p1 (outside, with the cover and back panel) and p2 (inside, with three panels).

### `corpus/cdc-antibiotics-korean.pdf`

- Source: https://stacks.cdc.gov/view/cdc/58740/cdc_58740_DS1.pdf (record: https://stacks.cdc.gov/view/cdc/58740)
- Publisher: CDC. "Antibiotics Aren't Always the Answer" fact sheet in Korean (December 26, 2017)
- License basis: US government work. The STACKS rights statement is "Public Domain" (confirmed 2026-10-07).
- Retrieved: 2026-10-07. Pages: 2. Size: 936,625 bytes
- SHA-256: `3a1f4711eeef97b1242b1535cf0b75fbd8f5003d8360d0c0de2947b87b074814`
- Covers: Hangul text (about 1,370 characters in the text layer) mixed with Latin text, digits and italics. Tagged. The catalog `/Lang` value is `Korean`, which is not a valid language tag. Page 2 has a table with check-mark glyphs and a footnote.
- Pages to check: p1 (headings, body, panel of a photo with text) and p2 (table with check marks and footnote).
