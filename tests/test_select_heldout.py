"""scripts/select_heldout.py: the blind, seeded choice of the held-out corpus."""
import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("select_heldout", ROOT / "scripts/select_heldout.py")
sel = importlib.util.module_from_spec(_spec)
sys.modules["select_heldout"] = sel
_spec.loader.exec_module(sel)


@pytest.mark.parametrize("a, b", [
    ("https://www.irs.gov/pub/irs-pdf/fw9.pdf", "http://irs.gov/pub/irs-pdf/FW9.pdf?x=1#top"),
    ("https://arxiv.org/pdf/1706.03762", "https://arxiv.org/pdf/1706.03762v7.pdf"),
    ("https://arxiv.org/abs/1706.03762v2", "https://export.arxiv.org/pdf/1706.03762"),
])
def test_one_document_has_one_url_key(a, b):
    assert sel.url_key(a) == sel.url_key(b)


def test_different_documents_have_different_keys():
    assert sel.url_key("https://arxiv.org/pdf/1706.03762") != sel.url_key("https://arxiv.org/pdf/1706.03763")
    assert sel.url_key("https://www.irs.gov/pub/irs-pdf/fw9.pdf") != sel.url_key("https://www.irs.gov/pub/irs-pdf/fw4.pdf")


@pytest.mark.parametrize("stem, core", [("fw9", "w9"), ("iw9", "w9"), ("p17", "17"), ("f1040", "1040"),
                                        ("i1040gi", "1040gi"), ("w9", "w9")])
def test_a_form_stem_ignores_its_prefix(stem, core):
    assert sel.stem_core(stem) == core


@pytest.mark.parametrize("name, stem", [("irs-w4-2026.pdf", "w4"), ("uscis-i9.pdf", "i9"), ("fw9.pdf", "fw9"),
                                        ("scan-w4.pdf", "w4")])
def test_a_sample_name_is_a_form_stem(name, stem):
    assert sel.sample_stem(name) == stem


def seen(keys=(), stems=(), hashes=()):
    return sel.Seen({sel.url_key(k) for k in keys}, set(stems), set(hashes))


def test_the_population_is_sorted_deduplicated_and_without_what_was_seen():
    urls = ["https://x.gov/b.pdf", "https://x.gov/a.pdf", "https://x.gov/a.pdf", "https://x.gov/seen.pdf",
            "https://www.irs.gov/pub/irs-pdf/iw9.pdf", "https://www.irs.gov/pub/irs-pdf/f1065.pdf"]
    kept, dropped = sel.population(urls, seen(keys=["https://x.gov/seen.pdf"], stems=["w9"]), forms=True)
    assert kept == ["https://www.irs.gov/pub/irs-pdf/f1065.pdf", "https://x.gov/a.pdf", "https://x.gov/b.pdf"]
    assert dropped == ["https://www.irs.gov/pub/irs-pdf/iw9.pdf\tstem", "https://x.gov/seen.pdf\turl"]


def test_a_stem_only_drops_a_form_in_a_forms_stratum():
    kept, _ = sel.population(["https://www.irs.gov/pub/irs-pdf/fw9.pdf"], seen(stems=["w9"]), forms=False)
    assert kept == ["https://www.irs.gov/pub/irs-pdf/fw9.pdf"]


def test_the_shuffle_is_fixed_by_the_seed_and_the_stratum():
    urls = [f"https://x.gov/{n}.pdf" for n in range(50)]
    assert sel.shuffled("papers", urls) == sel.shuffled("papers", urls)
    assert sel.shuffled("papers", urls) != sel.shuffled("other", urls)
    assert sel.shuffled("papers", urls) != urls
    assert sorted(sel.shuffled("papers", urls)) == sorted(urls)
    # Pinned: a change to SEED or to how the list is shuffled changes the choice.
    assert sel.SEED == "reflow-heldout-2026-11-21"
    assert "".join(sel.shuffled("papers", list("abcdefgh"))) == "gbeahcfd"


def pdf(n=1, tag=b""):
    return b"%PDF-1.7 " + tag + b" " * n


@pytest.fixture
def served(monkeypatch):
    """Serve `files` by URL, with a page count per body, and no waiting."""
    files, pages = {}, {}

    def download(url):
        if url not in files:
            raise OSError("404")
        if files[url] == "big":
            raise ValueError("over")
        return files[url]

    monkeypatch.setattr(sel, "download", download)
    monkeypatch.setattr(sel, "page_count", lambda data: pages[data])
    return files, pages


def test_a_candidate_is_kept_only_by_the_rules(served):
    files, pages = served
    good = pdf(tag=b"good")
    files.update({"u/good": good, "u/html": b"<html>", "u/dead-url": None, "u/big": "big",
                  "u/long": pdf(tag=b"long"), "u/empty": pdf(tag=b"empty"), "u/seen": pdf(tag=b"seen")})
    del files["u/dead-url"]
    pages.update({good: 150, files["u/long"]: 151, files["u/empty"]: 0, files["u/seen"]: 3})
    hashes = {hashlib.sha256(files["u/seen"]).hexdigest()}
    outcome = {u: sel.examine(u, hashes)[0] for u in ["u/good", "u/html", "u/dead-url", "u/big", "u/long",
                                                      "u/empty", "u/seen"]}
    assert outcome == {"u/good": "kept", "u/html": "not-pdf", "u/dead-url": "no-download", "u/big": "too-big",
                       "u/long": "pages-out-of-range", "u/empty": "pages-out-of-range", "u/seen": "seen-hash"}


@pytest.fixture
def heldout(tmp_path, monkeypatch, served):
    """A heldout folder in tmp_path with one two-document stratum."""
    folder = tmp_path / "heldout"
    (folder / "candidates").mkdir(parents=True)
    monkeypatch.setattr(sel, "CANDIDATES", folder / "candidates")
    monkeypatch.setattr(sel, "MANIFEST", folder / "MANIFEST.tsv")
    monkeypatch.setattr(sel, "files_dir", lambda: folder / "files")
    monkeypatch.setattr(sel, "load_seen", lambda: seen())
    monkeypatch.setattr(sel, "STRATA", {"s": sel.Stratum(2, "CC0", lambda: [])})
    files, pages = served
    urls = []
    for n in range(8):
        url = f"https://x/{n}.pdf"
        files[url] = pdf(tag=str(n).encode())
        pages[files[url]] = 1 + n
        urls.append(url)
    sel.candidates_file("s").write_text("".join(f"{u}\n" for u in sorted(urls)))
    return folder, files


def test_select_walks_the_shuffled_list_and_writes_the_manifest(heldout):
    folder, files = heldout
    order = sel.shuffled("s", sel.read_candidates("s"))
    sel.cmd_select(["s"])
    rows = sel.read_walk("s")
    assert [r["url"] for r in rows] == order[:2]
    assert [r["id"] for r in rows] == ["s-1", "s-2"]
    assert [r["sha256"] for r in rows] == [hashlib.sha256(files[u]).hexdigest() for u in order[:2]]
    assert (folder / "files/s-1.pdf").read_bytes() == files[order[0]]
    lines = (folder / "MANIFEST.tsv").read_text().splitlines()
    assert lines[1] == "\t".join(sel.MANIFEST_COLUMNS)
    assert [line.split("\t")[0] for line in lines[2:]] == ["s-1", "s-2"]


def test_a_dead_url_is_logged_and_the_walk_goes_on_down_the_list(heldout):
    _, files = heldout
    order = sel.shuffled("s", sel.read_candidates("s"))
    del files[order[0]]
    sel.cmd_select(["s"])
    rows = sel.read_walk("s")
    assert [(r["outcome"], r["id"]) for r in rows] == [("no-download", "-"), ("kept", "s-1"), ("kept", "s-2")]
    assert [r["url"] for r in rows] == order[:3]


def test_a_redraw_continues_from_where_the_walk_stopped(heldout):
    folder, files = heldout
    order = sel.shuffled("s", sel.read_candidates("s"))
    sel.cmd_select(["s"])
    sel.cmd_redraw(["s-1"])
    assert not (folder / "files/s-1.pdf").exists()
    assert [line.split("\t")[0] for line in (folder / "MANIFEST.tsv").read_text().splitlines()[2:]] == ["s-2"]
    sel.cmd_select(["s"])
    rows = sel.read_walk("s")
    assert [r["outcome"] for r in rows] == ["retired", "kept", "kept"]
    assert [r["url"] for r in rows] == order[:3]
    assert [r["id"] for r in rows if r["outcome"] == "kept"] == ["s-2", "s-3"]


def test_a_list_that_runs_out_ends_the_walk(heldout, capsys):
    files = heldout[1]
    for url in list(files)[1:]:
        del files[url]
    sel.cmd_select(["s"])
    assert "ran out with 1 of 2" in capsys.readouterr().out


def test_collect_refuses_to_change_a_list_that_has_been_walked(heldout, capsys):
    sel.cmd_select(["s"])
    assert sel.cmd_collect(["s"], force=False) == 1
    assert "a walk exists" in capsys.readouterr().out
