# Corpus fidelity scorecard

Generated 2026-10-22 by `tests/pages/report.py` (all page checks, whole documents). Regenerate with:

    .venv/bin/python tests/pages/report.py --write docs/corpus-scorecard.md

| document | pages | pass | xfail | fail | xpass | mean recall | mean floor | review err | review warn | convert s | pages/s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| arxiv | 1 | 4 | 2 | 0 | 0 | 99.5 | 98.0 | 8 | 5 | 2.2 | 6.9 |
| az-deceased-member-affidavit | 1 | 2 | 0 | 0 | 0 | - | - | 1 | 0 | 0.3 | 5.9 |
| az-medical-claim-es | 1 | 2 | 0 | 0 | 0 | - | - | 4 | 1 | 0.8 | 2.7 |
| az-privacy-release-spanish | 1 | 3 | 0 | 0 | 0 | - | - | 5 | 1 | 0.4 | 4.5 |
| bluejourney-directory | 6 | 49 | 3 | 0 | 0 | 98.3 | 96.8 | 7 | 204 | 100.9 | 26.2 |
| budget-appendix | 9 | 53 | 1 | 0 | 0 | 99.1 | 97.5 | 4 | 1732 | 103.9 | 12.5 |
| cdc-antibiotics-korean | 1 | 4 | 1 | 0 | 0 | 100.0 | 100.0 | 4 | 3 | 0.5 | 4.0 |
| cdc-antibiotics-trifold | 1 | 6 | 0 | 0 | 0 | 98.9 | 98.5 | 3 | 4 | 0.4 | 4.8 |
| cdc-firstline-slides-es | 1 | 6 | 0 | 0 | 0 | 100.0 | 100.0 | 12 | 4 | 0.6 | 24.1 |
| cdc-hepb-arabic | 1 | 7 | 3 | 0 | 0 | 84.0 | 83.9 | 4 | 3 | 0.4 | 4.7 |
| census-p60-273 | 1 | 6 | 2 | 0 | 0 | 96.5 | 96.0 | 29 | 53 | 39.1 | 2.4 |
| docaccess-assessment-appeal | 3 | 28 | 0 | 0 | 0 | 99.7 | 90.0 | 3 | 2 | 0.8 | 5.1 |
| epa-ghg-inventory-2024 | 1 | 4 | 2 | 0 | 0 | 99.0 | 98.5 | 270 | 129 | 63.0 | 14.6 |
| frcp-2025 | 4 | 16 | 0 | 0 | 0 | 95.6 | 95.5 | 2 | 25 | 3.2 | 44.8 |
| fw9 | 4 | 36 | 2 | 0 | 0 | 98.6 | 97.0 | 0 | 12 | 0.8 | 7.1 |
| irs-w4-2026 | 1 | 6 | 3 | 0 | 0 | 100.0 | 100.0 | 0 | 11 | 1.0 | 5.1 |
| maricopa-directory | 5 | 44 | 0 | 0 | 0 | 97.0 | 95.4 | 9 | 118 | 69.3 | 37.7 |
| naca | 3 | 22 | 0 | 0 | 0 | 99.6 | 96.7 | 13 | 27 | 4.2 | 2.9 |
| nasa-se-handbook | 1 | 7 | 0 | 0 | 0 | 98.5 | 98.0 | 49 | 9 | 8.2 | 43.2 |
| nchs-bar-chart | 1 | 6 | 1 | 0 | 0 | 100.0 | 100.0 | 2 | 0 | 0.3 | 6.9 |
| p17 | 7 | 73 | 0 | 0 | 0 | 91.9 | 90.5 | 40 | 286 | 28.2 | 5.0 |
| pima-directory | 6 | 56 | 0 | 0 | 0 | 96.5 | 94.7 | 6 | 44 | 14.0 | 48.8 |
| practitioner-data-form | 4 | 26 | 11 | 0 | 0 | 93.6 | 92.0 | 27 | 3 | 1.8 | 6.7 |
| provider-guide-05-admin | 5 | 39 | 8 | 0 | 0 | 93.8 | 92.0 | 0 | 9 | 0.9 | 15.9 |
| resnet | 10 | 101 | 0 | 0 | 0 | 96.9 | 94.7 | 7 | 9 | 1.7 | 7.0 |
| scan | 1 | 4 | 2 | 0 | 0 | - | 60.0 | 0 | 14 | 0.9 | 6.8 |
| scotus-mahmoud-v-taylor | 1 | 4 | 2 | 0 | 0 | 96.8 | 96.5 | 121 | 4 | 4.2 | 32.6 |
| uscis-i9 | 3 | 14 | 0 | 0 | 0 | 99.3 | 97.8 | 0 | 5 | 1.0 | 4.2 |

28 documents, 84 page checks: 628 pass, 43 xfail, 0 fail, 0 xpass

- pages = page checks tested. mean recall = measured word recall over those pages; mean floor = their min_recall floors. Only pages with a min_recall check count.
- review err / warn = items in the conversion's review.json.
- convert s = summed stage seconds of the conversion; pages/s = pages converted divided by that. "-" for a cache entry made before timing was recorded: run with --reconvert to record it.
