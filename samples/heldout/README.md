# The held-out corpus

Every other sample in this repository is both tuning data and test data: the page specs, the
robustness grid and the evals were all written while looking at the same documents. A number
measured on them says how well the converter fits them, not how it does on a document nobody has
seen. This corpus is the documents nobody has seen.

## The rule

No one opens these files, reads their converted output, or writes specs, rules or fixes
for them. Only a scorer reads them, and the evaluator reads only the score it prints. A document
someone has looked at is no longer held out, so it comes off the list.

Whoever picks the documents has seen them, so a list is chosen by a blind procedure or signed
off by someone who does not work on the converter.

## The manifest

`MANIFEST.tsv` has one row per document, tab-separated:

| Column | Meaning |
| --- | --- |
| `id` | a short name: lowercase letters, digits and `-` |
| `url` | where the PDF is fetched from |
| `sha256` | the hash of the file, so a changed download is refused |
| `licence` | why the file may be used: a licence, public domain or the source's terms |
| `added` | the date the row was added, `YYYY-MM-DD` |

Lines starting with `#` are comments. The files are not committed. Fetch them into
`samples/heldout/files/` of the main checkout (gitignored, shared by every worktree) with:

```sh
.venv/bin/python scripts/fetch_heldout.py           # every missing file, each hash checked
.venv/bin/python scripts/fetch_heldout.py --list    # each row and whether its file is here
```

The scorer is a separate item. It needs references checked by a person, such as hand
transcripts, before its number can be trusted.

## How the documents were chosen

On 2026-11-21 the choice was handed to a blind procedure, `scripts/select_heldout.py`, so that
no one looked at any document to pick it. The script reads listings, HTTP headers, and for each file its size, its
SHA-256, its first five bytes and its page count, and nothing else. It prints no title.

The seed is `reflow-heldout-2026-11-21` (the `SEED` constant). There are four strata, 30
documents in all. The id of a row is its stratum and its number in the walk.

| Stratum | Count | Population | Licence basis |
| --- | --- | --- | --- |
| `us-forms` | 10 | the PDFs of the IRS download list (`https://www.irs.gov/downloads/irs-pdf`) and of the CMS forms list | US federal work, public domain (17 U.S.C. 105) |
| `us-reports` | 8 | the GAO reports of govinfo's `GAOREPORTS` collection, by its sitemaps (1989 to 2008) | US federal work, public domain |
| `papers` | 5 | arXiv papers of the `cs` set with an OAI datestamp from 2025-03-03 to 2025-03-09, a CC BY 4.0 licence and one version, from the OAI-PMH `arXivRaw` format; the file is the paper's v1 | CC BY 4.0 |
| `scanned` | 7 | the Federal Register's issues of 1936 to 1993 on govinfo, which were printed pages before the register was born digital | US federal record, public domain |

The procedure, per stratum:

1. `scripts/select_heldout.py collect` gathers the candidate URLs into a sorted, deduplicated list
   and commits it as `candidates/<stratum>.txt`. It first drops what has been seen:
   any URL in a spec, a `SOURCES.tsv`, a sample or research record, a form whose filename stem
   matches a form in the samples (`fw9`, `iw9` and `w9` are one stem), and a document
   at the same path under another arXiv version. `candidates/<stratum>.excluded.txt`
   lists what it dropped and why.
2. `select` shuffles the list with `random.Random(f"{SEED}:{stratum}")` and walks it in order. It
   keeps a candidate when it downloads, starts with `%PDF-`, has a SHA-256 that is not that of any PDF under
   `samples/` or `web/public/pdfs` (nor of an earlier pick), is under 50 MB and has 1 to
   150 pages. Nothing else skips a candidate. It stops at the stratum's count.
   `candidates/<stratum>.walk.tsv` logs every candidate it examined, in order, with the outcome.
3. Every pick's file is in `files/<id>.pdf` and its hash is in `MANIFEST.tsv`, as
   `scripts/fetch_heldout.py` checks.

A re-draw, for a URL that has died, goes the same way: `select_heldout.py redraw <id>` retires the
row, and `select_heldout.py select` continues down the same shuffled list from where the walk
stopped. It never starts a new list, so a re-draw is not a choice. A retired row stays in the walk
log. Do not run `collect` again for a walked stratum: a new list shuffles differently.

Sources that did not work, and where their documents went, are in the next section.

### Sources that did not work

No stratum came up short, so no count moved to another stratum.

- `us-forms`: SSA forms (`ssa.gov`) cannot be fetched automatically, so none is in the
  population. The population is the IRS and CMS lists.
  The IRS list's pages overlap (3,136 rows name about 2,070 files) and the order of rows with
  one date is not stable between requests, so `collect` reads it three times and joins the URLs.
  The committed candidate list is the population; it is not every file on the site.
- `us-reports`: gao.gov cannot be fetched automatically, and govinfo's `GAOREPORTS`
  collection ends in 2008, so this stratum's reports are 1989 to 2008 ones.
- `scanned`: the National Archives catalog's v2 API needs a key. Its public search page's own
  endpoint returned unstable totals and ignored its filters, so no list could be drawn from it.
  The Federal Register's issues from before 1994, the fallback, fill
  the stratum. `scanned` is a claim about the source, not something the script checked: it
  never looks at a page. An issue over 50 MB is skipped by the size rule, and four were.
