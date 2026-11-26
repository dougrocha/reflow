#!/usr/bin/env python3
"""Choose the held-out corpus by blind, seeded sampling. Nobody reads a document to choose it.

The choice was handed to this procedure on 2026-11-21 (samples/heldout/README.md). The
script reads only listings and HTTP headers, and for each file its size, its SHA-256, its first
five bytes and its page count. It never reads a PDF's text, title, metadata or pictures, and it
prints no document title.

    scripts/select_heldout.py collect [stratum...]   # fetch listings -> samples/heldout/candidates/<stratum>.txt
    scripts/select_heldout.py select [stratum...]    # walk the shuffled lists -> MANIFEST.tsv and files/
    scripts/select_heldout.py redraw <id>...         # retire a dead row; `select` then continues the walk

The procedure, per stratum:

1. `collect` gathers candidate URLs from public listings into a sorted, deduplicated list, drops
   the URLs and stems of documents that have been seen (see `Seen`), and commits the rest to
   candidates/<stratum>.txt (the dropped ones go to <stratum>.excluded.txt). The population is
   auditable, and `select` needs no network access to the listings.
2. `select` shuffles the list with random.Random(f"{SEED}:{stratum}") and walks it in order.
   A candidate is kept when it downloads, starts with %PDF-, has a SHA-256 that no seen file
   or earlier pick has, is under 50 MB and has 1 to 150 pages. Nothing else skips a candidate.
   The walk stops when the stratum's count is reached, and logs every candidate it examined in
   candidates/<stratum>.walk.tsv, so the walk can be replayed and continued.
3. `redraw` retires a row whose URL died. The next `select` continues down the same shuffled list
   from where the walk stopped, so a re-draw is never a free choice.

The files go to samples/heldout/files/<id>.pdf of the main checkout (gitignored, shared).
Run it with the main checkout's venv (it needs pypdfium2, to count pages).
"""

from __future__ import annotations

import argparse
import hashlib
import html
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from checkout import main_checkout  # noqa: E402

SEED = "reflow-heldout-2026-11-21"
ADDED = "2026-11-21"
MAX_PAGES = 150
MAX_BYTES = 50 * 1024 * 1024
AGENT = "reflow-heldout-corpus/1 (test corpus selection)"
PUBLIC_DOMAIN = "US federal work, public domain (17 U.S.C. 105)"

HELDOUT = REPO / "samples/heldout"
CANDIDATES = HELDOUT / "candidates"
MANIFEST = HELDOUT / "MANIFEST.tsv"
MANIFEST_HEADER = "# The held-out corpus: public PDFs that only a scorer reads. See samples/heldout/README.md."
MANIFEST_COLUMNS = ["id", "url", "sha256", "licence", "added"]
WALK_COLUMNS = ["pos", "url", "outcome", "pages", "bytes", "sha256", "id"]

# The arXiv window: a week of the cs set, by datestamp.
ARXIV_FROM, ARXIV_UNTIL, ARXIV_SET = "2025-03-03", "2025-03-09", "cs"
CC_BY_4 = "http://creativecommons.org/licenses/by/4.0/"
# The scanned stratum is the Federal Register's issues from before it was born digital.
FR_LAST_SCANNED_YEAR = 1993


def request(url: str) -> urllib.request.Request:
    """A GET request that names this tool, except at irs.gov, which is sent urllib's own
    default."""
    host = urllib.parse.urlsplit(url).hostname or ""
    return urllib.request.Request(url, headers={} if host.endswith("irs.gov") else {"User-Agent": AGENT})


def get(url: str, *, tries: int = 3, timeout: int = 120, pause: float = 1.0) -> bytes:
    """GET with a polite pause and a retry on a timeout, a connection error or a 429/5xx."""
    last: Exception | None = None
    for attempt in range(tries):
        time.sleep(pause if attempt == 0 else 5 * attempt)
        req = request(url)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504):
                raise
            last = e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
    assert last is not None
    raise last


def get_text(url: str, **kw: float) -> str:
    return get(url, **kw).decode("utf-8", "replace")  # type: ignore[arg-type]


# --- Collectors: each returns the URLs a listing names, and reads nothing else. -------------


def collect_irs() -> list[str]:
    """Every PDF the IRS download list shows. Its pages overlap and the order of rows with one
    date is not stable between requests, so the list is read three times and the URLs joined."""
    urls: list[str] = []
    for _ in range(3):
        for page in range(300):
            body = get_text(f"https://www.irs.gov/downloads/irs-pdf?page={page}")
            found = re.findall(r'href="(https://www\.irs\.gov/pub/irs-pdf/[^"]+\.pdf)"', body)
            if not found:
                break
            urls += found
    return urls


def collect_cms() -> list[str]:
    base = "https://www.cms.gov"
    listing = get_text(f"{base}/medicare/cms-forms/cms-forms/cms-forms-list")
    items = sorted(set(re.findall(r'(/medicare/cms-forms/cms-forms/cms-forms-items/[a-z0-9-]+)', listing)))
    urls: list[str] = []
    for item in items:
        try:
            page = get_text(base + item)
        except urllib.error.HTTPError:
            continue
        urls += [base + p for p in re.findall(r'href="(/medicare/cms-forms/cms-forms/downloads/[^"]+\.pdf)"', page)]
    return urls


def sitemap_locs(url: str) -> list[str]:
    return [html.unescape(u) for u in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", get_text(url, timeout=300))]


def collect_gao() -> list[str]:
    index = sitemap_locs("https://www.govinfo.gov/sitemap/GAOREPORTS_sitemap_index.xml")
    urls = []
    for year_map in sorted(index):
        for loc in sitemap_locs(year_map):
            m = re.fullmatch(r"https://www\.govinfo\.gov/app/details/(GAOREPORTS-[^/\s]+)", loc)
            if m:
                urls.append(f"https://www.govinfo.gov/content/pkg/{m[1]}/pdf/{m[1]}.pdf")
    return urls


def collect_fr() -> list[str]:
    index = sitemap_locs("https://www.govinfo.gov/sitemap/FR_sitemap_index.xml")
    urls = []
    for year_map in sorted(index):
        year = re.search(r"FR_(\d{4})_sitemap", year_map)
        if not year or int(year[1]) > FR_LAST_SCANNED_YEAR:
            continue
        for loc in sitemap_locs(year_map):
            m = re.fullmatch(r"https://www\.govinfo\.gov/app/details/(FR-\d{4}-\d{2}-\d{2})", loc)
            if m:
                urls.append(f"https://www.govinfo.gov/content/pkg/{m[1]}/pdf/{m[1]}.pdf")
    return urls


def collect_arxiv() -> list[str]:
    """Papers of one week of the cs set whose licence is CC BY 4.0 and that have one version.

    The week is by OAI datestamp. Only the id, the licence and the number of versions are read
    from each record. A paper with a later version has a licence that may not cover its first
    version, so it is out of the population; a paper with one version is stable at v1.
    """
    oai, raw = "{http://www.openarchives.org/OAI/2.0/}", "{http://arxiv.org/OAI/arXivRaw/}"
    base = "https://oaipmh.arxiv.org/oai?verb=ListRecords"
    url = f"{base}&metadataPrefix=arXivRaw&set={ARXIV_SET}&from={ARXIV_FROM}&until={ARXIV_UNTIL}"
    urls: list[str] = []
    while url:
        root = ET.fromstring(get(url, tries=6, pause=5.0, timeout=300))
        for rec in root.iter(f"{oai}record"):
            meta = rec.find(f"{oai}metadata/{raw}arXivRaw")
            if meta is None:
                continue
            ident = meta.findtext(f"{raw}id")
            if (ident and meta.findtext(f"{raw}license") == CC_BY_4
                    and len(meta.findall(f"{raw}version")) == 1):
                urls.append(f"https://arxiv.org/pdf/{ident}v1")
        token = root.find(f"{oai}ListRecords/{oai}resumptionToken")
        url = f"{base}&resumptionToken={urllib.parse.quote(token.text)}" if token is not None and token.text else ""
    return urls


@dataclass(frozen=True)
class Stratum:
    count: int
    licence: str
    collect: Callable[[], list[str]]
    forms: bool = False  # also drop candidates whose filename stem names a seen form


STRATA: dict[str, Stratum] = {
    "us-forms": Stratum(10, PUBLIC_DOMAIN, lambda: collect_irs() + collect_cms(), forms=True),
    "us-reports": Stratum(8, PUBLIC_DOMAIN, collect_gao),
    "papers": Stratum(5, "CC BY 4.0", collect_arxiv),
    "scanned": Stratum(7, "US federal record, public domain (17 U.S.C. 105)", collect_fr),
}


# --- What has been seen -------------------------------------------------------------------

URL_RE = re.compile(r"https?://[^\s\"'<>)\]|,\\]+")
HASH_RE = re.compile(r"\b[0-9a-f]{64}\b")
TEXT_SUFFIXES = {".json", ".tsv", ".md", ".txt", ".csv"}


def url_key(url: str) -> str:
    """A URL reduced to what names the document: host and path, lowercase, without `www.`, the
    query, the fragment, a trailing slash or an arXiv version."""
    u = urllib.parse.urlsplit(url.strip().rstrip(".;"))
    host = (u.hostname or "").lower().removeprefix("www.")
    path = urllib.parse.unquote(u.path).lower().rstrip("/")
    if host in ("arxiv.org", "export.arxiv.org"):
        m = re.fullmatch(r"/(?:pdf|abs)/(.+?)(?:v\d+)?(?:\.pdf)?", path)
        if m:
            return f"arxiv:{m[1]}"
    return host + path


def stem_core(stem: str) -> str:
    """A form's filename stem without its f/i/p prefix, so `fw9`, `iw9` and `w9` are one form."""
    stem = stem.lower()
    return re.sub(r"^(?:f|i|p)(?=[a-z]?\d)", "", stem)


def sample_stem(name: str) -> str:
    """A sample file's name as a form stem: `irs-w4-2026.pdf` -> `w4`."""
    stem = re.sub(r"\.pdf$", "", name.lower())
    stem = re.sub(r"^(?:scan|irs|uscis|cms|ssa)-", "", stem)
    return re.sub(r"-\d{4}$", "", stem)


@dataclass
class Seen:
    keys: set[str]
    stems: set[str]
    hashes: set[str]

    def url_seen(self, url: str) -> bool:
        return url_key(url) in self.keys

    def stem_seen(self, url: str) -> bool:
        stem = Path(urllib.parse.unquote(urllib.parse.urlsplit(url).path)).stem
        return stem_core(stem) in self.stems


def load_seen(repo: Path = REPO) -> Seen:
    """Every URL, form stem and file hash of a document that has been seen.

    The URLs come from the text of samples/, tests/pages/specs, tests/robustness and research/
    (and docs/); the hashes from every PDF under samples/, web/public/pdfs and research/, and from
    every 64-hex string in those texts. A PDF is hashed as bytes and never opened.
    """
    main = main_checkout(repo)
    keys: set[str] = set()
    stems: set[str] = set()
    hashes: set[str] = set()
    text_roots = [main / "samples", main / "research", main / "docs", repo / "tests/pages/specs",
                  repo / "tests/pages/README.md", repo / "tests/robustness", repo / "samples", repo / "docs"]
    for root in text_roots:
        files = [root] if root.is_file() else sorted(root.rglob("*")) if root.is_dir() else []
        for f in files:
            if not f.is_file() or "heldout" in f.parts or f.suffix.lower() not in TEXT_SUFFIXES:
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for u in URL_RE.findall(text):
                keys.add(url_key(u))
                host = urllib.parse.urlsplit(u).hostname or ""
                if host.endswith(("irs.gov", "cms.gov", "ssa.gov", "uscis.gov")):
                    stems.add(stem_core(Path(urllib.parse.unquote(urllib.parse.urlsplit(u).path)).stem))
            hashes.update(HASH_RE.findall(text))
    pdf_roots = [main / "samples", main / "web/public/pdfs", main / "research", repo / "samples"]
    for root in pdf_roots:
        if not root.is_dir():
            continue
        for f in sorted(root.rglob("*.pdf")):
            if "heldout" in f.parts or not f.is_file():
                continue
            hashes.add(hashlib.sha256(f.read_bytes()).hexdigest())
            if f.parent.name in ("samples", "pdfs", "corpus"):
                stems.add(stem_core(sample_stem(f.name)))
    stems.discard("")
    return Seen(keys, stems, hashes)


# --- collect ------------------------------------------------------------------------------


def candidates_file(stratum: str) -> Path:
    return CANDIDATES / f"{stratum}.txt"


def walk_file(stratum: str) -> Path:
    return CANDIDATES / f"{stratum}.walk.tsv"


def population(urls: Iterable[str], seen: Seen, forms: bool) -> tuple[list[str], list[str]]:
    """The sorted, deduplicated candidates, and the dropped URLs with the reason for each.

    Two URLs with one url_key are one document, and the first in sort order stays.
    """
    kept: dict[str, str] = {}
    dropped: list[str] = []
    for url in sorted(set(urls)):
        key = url_key(url)
        if key in kept:
            continue
        if seen.url_seen(url):
            dropped.append(f"{url}\turl")
        elif forms and seen.stem_seen(url):
            dropped.append(f"{url}\tstem")
        else:
            kept[key] = url
    return sorted(kept.values()), dropped


def cmd_collect(names: list[str], force: bool) -> int:
    seen = load_seen()
    print(f"seen: {len(seen.keys)} URLs, {len(seen.stems)} form stems, {len(seen.hashes)} hashes")
    CANDIDATES.mkdir(parents=True, exist_ok=True)
    failed = 0
    for name in names:
        if walk_file(name).exists() and not force:
            print(f"{name}: a walk exists; collecting again would change the shuffle (--force)")
            return 1
        spec = STRATA[name]
        try:
            urls = spec.collect()
        except (OSError, ET.ParseError) as e:
            print(f"{name}: the source failed: {e}")
            failed += 1
            continue
        kept, dropped = population(urls, seen, spec.forms)
        candidates_file(name).write_text("".join(f"{u}\n" for u in kept), encoding="utf-8")
        (CANDIDATES / f"{name}.excluded.txt").write_text("".join(f"{d}\n" for d in dropped), encoding="utf-8")
        print(f"{name}: {len(set(urls))} listed, {len(dropped)} dropped as seen, {len(kept)} candidates")
    return 1 if failed else 0


# --- select -------------------------------------------------------------------------------


def shuffled(stratum: str, urls: list[str]) -> list[str]:
    order = list(urls)
    random.Random(f"{SEED}:{stratum}").shuffle(order)
    return order


def read_candidates(stratum: str) -> list[str]:
    return [u for u in candidates_file(stratum).read_text(encoding="utf-8").splitlines() if u]


def read_walk(stratum: str) -> list[dict[str, str]]:
    path = walk_file(stratum)
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [dict(zip(WALK_COLUMNS, line.split("\t"), strict=True)) for line in lines[1:]]


def write_walk(stratum: str, rows: list[dict[str, str]]) -> None:
    lines = ["\t".join(WALK_COLUMNS)] + ["\t".join(r[c] for c in WALK_COLUMNS) for r in rows]
    walk_file(stratum).write_text("\n".join(lines) + "\n", encoding="utf-8")


def download(url: str) -> bytes:
    """The file's bytes. Raises OSError (a refused or failed request, a timeout) or ValueError
    when it is over the size cap."""
    last: Exception | None = None
    for attempt in range(3):
        time.sleep(1.0 if attempt == 0 else 5 * attempt)
        req = request(url)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                length = r.headers.get("Content-Length")
                if length and int(length) > MAX_BYTES:
                    raise ValueError(f"over {MAX_BYTES} bytes")
                data = r.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    raise ValueError(f"over {MAX_BYTES} bytes")
                return data
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504):
                raise
            last = e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
    assert last is not None
    raise last


def page_count(data: bytes) -> int:
    """The number of pages, and nothing else of the document."""
    import pypdfium2

    pdf = pypdfium2.PdfDocument(data)
    try:
        return len(pdf)
    finally:
        pdf.close()


def examine(url: str, seen_hashes: set[str]) -> tuple[str, int, int, str, bytes]:
    """Judge one candidate by the rules and nothing else: (outcome, pages, bytes, sha256, data)."""
    try:
        data = download(url)
    except ValueError:
        return "too-big", 0, 0, "-", b""
    except OSError:
        return "no-download", 0, 0, "-", b""
    sha = hashlib.sha256(data).hexdigest()
    if data[:5] != b"%PDF-":
        return "not-pdf", 0, len(data), sha, b""
    if sha in seen_hashes:
        return "seen-hash", 0, len(data), sha, b""
    try:
        pages = page_count(data)
    except Exception:  # noqa: BLE001  a file pdfium can't open has no page count
        return "no-page-count", 0, len(data), sha, b""
    if not 1 <= pages <= MAX_PAGES:
        return "pages-out-of-range", pages, len(data), sha, b""
    return "kept", pages, len(data), sha, data


def next_number(rows: list[dict[str, str]]) -> int:
    used = [int(r["id"].rsplit("-", 1)[1]) for r in rows if r["id"] != "-"]
    return max(used, default=0) + 1


def files_dir() -> Path:
    return main_checkout(REPO) / "samples/heldout/files"


def cmd_select(names: list[str]) -> int:
    seen = load_seen()
    for name in names:
        spec = STRATA[name]
        order = shuffled(name, read_candidates(name))
        rows = read_walk(name)
        taken = {r["sha256"] for r in rows if r["outcome"] == "kept"}
        # `retired` rows keep their place in the walk, and their hash stays out of it.
        taken |= {r["sha256"] for r in rows if r["outcome"] == "retired"}
        kept = sum(r["outcome"] == "kept" for r in rows)
        pos = len(rows)
        while kept < spec.count and pos < len(order):
            url = order[pos]
            outcome, pages, size, sha, data = examine(url, seen.hashes | taken)
            row = {"pos": str(pos), "url": url, "outcome": outcome, "pages": str(pages), "bytes": str(size),
                   "sha256": sha, "id": "-"}
            if outcome == "kept":
                row["id"] = f"{name}-{next_number(rows)}"
                files_dir().mkdir(parents=True, exist_ok=True)
                (files_dir() / f"{row['id']}.pdf").write_bytes(data)
                taken.add(sha)
                kept += 1
            rows.append(row)
            write_walk(name, rows)
            print(f"{name} {pos}: {outcome} {row['id']} {pages}p {url}")
            pos += 1
        if kept < spec.count:
            print(f"{name}: the list ran out with {kept} of {spec.count}")
    write_manifest()
    return 0


def cmd_redraw(ids: list[str]) -> int:
    for ident in ids:
        name = ident.rsplit("-", 1)[0]
        rows = read_walk(name)
        row = next((r for r in rows if r["id"] == ident and r["outcome"] == "kept"), None)
        if row is None:
            print(f"{ident}: not a kept row")
            return 1
        row["outcome"] = "retired"
        write_walk(name, rows)
        (files_dir() / f"{ident}.pdf").unlink(missing_ok=True)
        print(f"{ident}: retired; run `select {name}` to continue the walk")
    write_manifest()
    return 0


def write_manifest() -> None:
    out = [MANIFEST_HEADER, "\t".join(MANIFEST_COLUMNS)]
    for name, spec in STRATA.items():
        for r in read_walk(name):
            if r["outcome"] == "kept":
                out.append("\t".join([r["id"], r["url"], r["sha256"], spec.licence, ADDED]))
    MANIFEST.write_text("\n".join(out) + "\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for cmd in ("collect", "select"):
        p = sub.add_parser(cmd)
        p.add_argument("strata", nargs="*", help=f"any of {', '.join(STRATA)}; default all")
        if cmd == "collect":
            p.add_argument("--force", action="store_true")
    sub.add_parser("redraw").add_argument("ids", nargs="+")
    args = ap.parse_args(argv)
    if args.cmd == "redraw":
        return cmd_redraw(args.ids)
    names = args.strata or list(STRATA)
    unknown = [n for n in names if n not in STRATA]
    if unknown:
        ap.error(f"unknown stratum {unknown[0]}")
    return cmd_collect(names, args.force) if args.cmd == "collect" else cmd_select(names)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
